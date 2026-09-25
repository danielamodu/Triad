"""Tests for the Layer 2 live gate and fill reconciliation."""
import os

import config
import main
from src import cli
from src.execution import executor
from src.execution.executor import parse_fill

# Real order-detail shape observed from bgc (paper BTCUSDT sell fill).
REAL_FILL = {"orderId": "1483537240947015680", "symbol": "BTCUSDT",
             "side": "sell", "orderType": "market", "qty": "0.012766",
             "cumExecQty": "0.012766", "cumExecValue": "999.69805572",
             "avgPrice": "78309.42", "orderStatus": "filled",
             "feeDetail": [{"feeCoin": "USDT", "fee": "0.99969805"}]}


def test_live_gate_defaults_to_paper():
    live, reason = config.live_trading_enabled(False)
    assert live is False and reason == ""


def test_live_gate_needs_env_confirmation(monkeypatch):
    monkeypatch.setattr(config, "TRIAD_LIVE_OK", False)
    live, reason = config.live_trading_enabled(True)
    assert live is False and "TRIAD_LIVE_OK" in reason


def test_live_gate_passes_with_flag_and_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TRIAD_LIVE_OK", True)
    monkeypatch.setattr(config, "KILL_FILE",
                        os.path.join(str(tmp_path), "KILL"))
    assert config.live_trading_enabled(True) == (True, "")


def test_live_gate_refuses_on_kill_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TRIAD_LIVE_OK", True)
    kill = os.path.join(str(tmp_path), "KILL")
    with open(kill, "w", encoding="utf-8") as fh:
        fh.write("stop")
    monkeypatch.setattr(config, "KILL_FILE", kill)
    live, reason = config.live_trading_enabled(True)
    assert live is False and "KILL" in reason


class _Proc:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def test_cli_run_adds_paper_flag_by_default(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **kw: seen.update(cmd=cmd) or
                        _Proc('{"ok": true, "data": {}}'))
    cli._run("market", "--action", "tickers")
    assert "--paper-trading" in seen["cmd"]


def test_cli_run_omits_paper_flag_when_live(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **kw: seen.update(cmd=cmd) or
                        _Proc('{"ok": true, "data": {}}'))
    cli._run("order", "--action", "place", paper=False)
    assert "--paper-trading" not in seen["cmd"]


def test_place_order_forwards_client_oid_and_mode(monkeypatch):
    seen = {}
    def fake_run(*argv, **kw):
        seen["argv"] = argv
        seen["paper"] = kw.get("paper", True)
        return {"orderId": "1"}
    monkeypatch.setattr(cli, "_run", fake_run)
    cli.place_order("SPOT", "BTCUSDT", "sell", "market", "0.01",
                    paper=False, client_oid="triad-abc")
    assert "--clientOid" in seen["argv"] and "triad-abc" in seen["argv"]
    assert seen["paper"] is False


def test_parse_fill_reads_real_shape():
    fill = parse_fill(REAL_FILL)
    assert fill["fill_price"] == 78309.42
    assert fill["fill_qty"] == 0.012766
    assert fill["fill_value"] == 999.6981  # rounded to 4dp
    assert fill["fee_usd"] == 0.999698  # rounded to 6dp
    assert fill["fee_coin"] == "USDT"
    assert fill["order_status"] == "filled"


def test_parse_fill_degrades_on_garbage():
    assert parse_fill({})["fill_price"] == 0.0
    assert parse_fill(None)["fee_usd"] == 0.0
    assert parse_fill({"avgPrice": "nope"})["fill_price"] == 0.0


def _mock_broker(monkeypatch, paper_seen):
    def fake_place(*a, **kw):
        paper_seen.append(kw.get("paper", True))
        return {"orderId": "OID1"}
    monkeypatch.setattr(cli, "place_order", fake_place)
    monkeypatch.setattr(cli, "get_order", lambda oid, **kw: dict(REAL_FILL))

    def fake_run(*argv, **kw):
        args = " ".join(argv)
        if "instruments" in args:
            return [{"symbol": "BTCUSDT", "status": "online",
                     "minOrderQty": "0.000001", "minOrderAmount": "1",
                     "quantityPrecision": 6}]
        if "account_overview" in args:
            return {"assets": {"assets": [
                {"coin": "USDT", "usdValue": "50000", "available": "50000"},
                {"coin": "BTC", "usdValue": "90000", "available": "1.0"}]}}
        return [{"symbol": "BTCUSDT", "lastPrice": "90000",
                 "quantityPrecision": 6}]
    monkeypatch.setattr(executor.cli, "_run", fake_run)


def test_execute_paper_by_default_keeps_demo_flag(monkeypatch):
    paper_seen = []
    _mock_broker(monkeypatch, paper_seen)
    leg = executor.execute({"decision": "HEDGE_CRYPTO", "confidence": 0.9},
                           "BTCUSDT")
    assert leg["executed"] is True
    assert paper_seen == [True]
    assert leg["live"] is False
    assert leg["fill_price"] == 78309.42
    assert leg["client_oid"].startswith("triad-")


def test_execute_live_drops_demo_flag(monkeypatch):
    paper_seen = []
    _mock_broker(monkeypatch, paper_seen)
    leg = executor.execute({"decision": "HEDGE_CRYPTO", "confidence": 0.9},
                           "BTCUSDT", live=True)
    assert paper_seen == [False]
    assert leg["live"] is True


def test_execute_survives_fill_fetch_failure(monkeypatch):
    def fake_place(*a, **kw):
        return {"orderId": "OID9"}
    monkeypatch.setattr(cli, "place_order", fake_place)
    def boom(oid, **kw):
        raise RuntimeError("detail down")
    monkeypatch.setattr(cli, "get_order", boom)

    def fake_run(*argv, **kw):
        args = " ".join(argv)
        if "instruments" in args:
            return [{"symbol": "RAAPLUSDT", "status": "online",
                     "minOrderQty": "0.000001", "minOrderAmount": "1",
                     "quantityPrecision": 6}]
        if "account_overview" in args:
            return {"assets": {"assets": [
                {"coin": "USDT", "usdValue": "50000", "available": "50000"}]}}
        return [{"symbol": "RAAPLUSDT", "lastPrice": "300",
                 "quantityPrecision": 6}]
    monkeypatch.setattr(executor.cli, "_run", fake_run)
    leg = executor.execute({"decision": "LONG_RTOKEN", "confidence": 0.9},
                           "RAAPLUSDT")
    assert leg["executed"] is True  # order placed; fill unknown
    assert leg["fill_price"] == 0.0  # downstream falls back to signal price
    assert leg["fill_unknown"] is True


def test_sync_positions_prefers_fill_price():
    main.OPEN_POSITIONS.clear()
    main._sync_positions(
        {"executed": True,
         "details": {"symbol": "BTCUSDT", "side": "sell",
                     "notional_usdt": 500.0, "executed": True,
                     "fill_price": 80000.0}},
        {"rtoken_last": "100", "crypto_last": "90000"}, "HEDGE_CRYPTO")
    pos = main.OPEN_POSITIONS["BTCUSDT"]
    assert pos["entry_price"] == 80000.0  # broker fill, not signal 90000
    assert pos["current_price"] == 90000.0  # mark stays live
    main.OPEN_POSITIONS.clear()
    main._sync_positions(
        {"executed": True,
         "details": {"symbol": "BTCUSDT", "side": "buy",
                     "notional_usdt": 500.0, "executed": True}},
        {"rtoken_last": "100", "crypto_last": "90000"}, "LONG_RTOKEN")
    assert main.OPEN_POSITIONS["BTCUSDT"]["entry_price"] == 90000.0
    main.OPEN_POSITIONS.clear()


def _live_tick(monkeypatch, tmp_path, live):
    monkeypatch.setattr(config, "RISK_STATE_FILE",
                        os.path.join(str(tmp_path), "risk_state.json"))
    monkeypatch.setattr(config, "POSITIONS_FILE",
                        os.path.join(str(tmp_path), "positions.json"))
    monkeypatch.setattr(main, "get_divergence",
                        lambda: {"signal": "STABLE", "direction": "FLAT",
                                 "divergence_score": 0.0, "rtoken_change": 0.0,
                                 "crypto_change": 0.0, "rtoken_last": "100",
                                 "crypto_last": "90000",
                                 "selected_rtoken": "RAAPLUSDT",
                                 "basket_scores": {}})
    monkeypatch.setattr(main, "get_event",
                        lambda: {"signal": "NEUTRAL", "confidence": 0.5})
    monkeypatch.setattr(main, "get_sentiment",
                        lambda: {"sentiment": "neutral", "score": 0.5})
    monkeypatch.setattr(main, "get_positions", lambda **kw: {"__ok": True})
    monkeypatch.setattr(main, "get_balance", lambda coin, **kw: 25000.0)
    monkeypatch.setattr(main, "decide",
                        lambda s, p, m, **kw: {"decision": "HOLD", "confidence": 0.0,
                                         "reasoning": "t", "scores": {},
                                         "engine_used": "test"})
    captured = {}
    monkeypatch.setattr(main, "append_log",
                        lambda e: captured.update(e) or "mock")
    main.MEMORY.clear()
    main.OPEN_POSITIONS.clear()
    main._STATE_SAVE_OK = True
    main.tick(live=live)
    main.MEMORY.clear()
    main.OPEN_POSITIONS.clear()
    return captured


def test_tick_logs_paper_mode_by_default(monkeypatch, tmp_path):
    assert _live_tick(monkeypatch, tmp_path, False)["mode"] == "paper"


def test_tick_logs_live_mode_when_enabled(monkeypatch, tmp_path):
    assert _live_tick(monkeypatch, tmp_path, True)["mode"] == "live"


def test_tick_threads_live_into_broker_and_executor(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(config, "RISK_STATE_FILE",
                        os.path.join(str(tmp_path), "risk_state.json"))
    monkeypatch.setattr(config, "POSITIONS_FILE",
                        os.path.join(str(tmp_path), "positions.json"))
    monkeypatch.setattr(main, "get_divergence",
                        lambda: {"signal": "STABLE", "direction": "FLAT",
                                 "divergence_score": 0.0, "rtoken_change": 0.0,
                                 "crypto_change": 0.0, "rtoken_last": "100",
                                 "crypto_last": "90000",
                                 "selected_rtoken": "RAAPLUSDT",
                                 "basket_scores": {}})
    monkeypatch.setattr(main, "get_event",
                        lambda: {"signal": "NEUTRAL", "confidence": 0.5})
    monkeypatch.setattr(main, "get_sentiment",
                        lambda: {"sentiment": "neutral", "score": 0.5})
    def fake_positions(**kw):
        seen["positions_paper"] = kw.get("paper", "unset")
        return {"__ok": True}
    monkeypatch.setattr(main, "get_positions", fake_positions)
    monkeypatch.setattr(main, "decide",
                        lambda s, p, m, **kw: {"decision": "LONG_RTOKEN",
                                         "confidence": 0.9, "reasoning": "t",
                                         "scores": {}, "engine_used": "test"})
    def fake_execute(dec, sym, **kw):
        seen["execute_live"] = kw.get("live", "unset")
        return {"executed": True, "order_id": "x", "symbol": sym,
                "side": "buy", "notional_usdt": 1000.0, "fee_usd": 1.0,
                "details": {"symbol": sym, "side": "buy",
                            "notional_usdt": 1000.0, "fee_usd": 1.0,
                            "executed": True}}
    monkeypatch.setattr(main, "execute", fake_execute)
    captured = {}
    monkeypatch.setattr(main, "append_log",
                        lambda e: captured.update(e) or "mock")
    main.MEMORY.clear()
    main.OPEN_POSITIONS.clear()
    main._STATE_SAVE_OK = True
    main.tick(live=True)
    main.MEMORY.clear()
    main.OPEN_POSITIONS.clear()
    assert seen["positions_paper"] is False
    assert seen["execute_live"] is True
    assert captured["mode"] == "live"
    assert captured["fees_usd"] == 1.0
