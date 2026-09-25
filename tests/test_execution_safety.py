"""Tests for pre-trade validation, retry, and fill settlement."""
import time

from src import cli
from src.execution import executor


def _router(monkeypatch, instrument=None, balances=None, last="90000"):
    """Route cli._run: instruments / account_overview / tickers."""
    instrument = {"symbol": "BTCUSDT", "status": "online",
                  "minOrderQty": "0.000001", "minOrderAmount": "1",
                  "quantityPrecision": 6,
                  **(instrument or {})}
    balances = ([{"coin": "USDT", "usdValue": "50000", "available": "50000"},
                 {"coin": "BTC", "usdValue": "90000", "available": "1.0"}]
                if balances is None else balances)

    def fake_run(*argv, **kw):
        args = " ".join(argv)
        if "instruments" in args:
            sym = argv[argv.index("--symbol") + 1] if "--symbol" in argv else ""
            if sym.upper() == instrument["symbol"]:
                return [dict(instrument)]
            return []
        if "account_overview" in args:
            return {"assets": {"assets": balances}}
        return [{"symbol": "BTCUSDT", "lastPrice": last,
                 "quantityPrecision": 6}]
    monkeypatch.setattr(executor.cli, "_run", fake_run)


def test_validate_trade_happy_path(monkeypatch):
    _router(monkeypatch)
    assert executor.validate_trade("BTCUSDT", "sell", 1000.0,
                                   "0.011", paper=True) == (True, "")
    assert executor.validate_trade("BTCUSDT", "buy", 1000.0,
                                   paper=True) == (True, "")


def test_validate_trade_rejects_unlisted_and_offline(monkeypatch):
    _router(monkeypatch)
    ok, reason = executor.validate_trade("NOPEUSDT", "buy", 100.0, paper=True)
    assert ok is False and "not listed" in reason
    _router(monkeypatch, instrument={"symbol": "BTCUSDT",
                                     "status": "halt"})
    ok, reason = executor.validate_trade("BTCUSDT", "buy", 100.0, paper=True)
    assert ok is False and "not online" in reason


def test_validate_trade_checks_minimums_and_balances(monkeypatch):
    _router(monkeypatch)
    ok, reason = executor.validate_trade("BTCUSDT", "buy", 0.5, paper=True)
    assert ok is False and "min" in reason
    _router(monkeypatch, balances=[
        {"coin": "USDT", "usdValue": "10", "available": "10"}])
    ok, reason = executor.validate_trade("BTCUSDT", "buy", 1000.0, paper=True)
    assert ok is False and "insufficient USDT" in reason
    _router(monkeypatch, balances=[
        {"coin": "BTC", "usdValue": "1", "available": "0.0000001"}])
    ok, reason = executor.validate_trade("BTCUSDT", "sell", 1000.0,
                                         "0.011", paper=True)
    assert ok is False and "insufficient BTC" in reason


def test_execute_blocks_before_placing(monkeypatch):
    _router(monkeypatch)  # RNVDAUSDT absent from instruments
    calls = []
    monkeypatch.setattr(cli, "place_order",
                        lambda *a, **kw: calls.append(1) or {"orderId": "x"})
    leg = executor.execute({"decision": "LONG_RTOKEN", "confidence": 0.9},
                           "RNVDAUSDT")
    assert leg["executed"] is False and calls == []
    assert leg["details"].startswith("PRE_TRADE_BLOCKED")


def test_execute_aborts_on_price_drift(monkeypatch):
    _router(monkeypatch, last="95000")  # signal said 90000
    calls = []
    monkeypatch.setattr(cli, "place_order",
                        lambda *a, **kw: calls.append(1) or {"orderId": "x"})
    leg = executor.execute({"decision": "HEDGE_CRYPTO", "confidence": 0.9},
                           "BTCUSDT", ref_price=90000.0)
    assert leg["executed"] is False and calls == []
    assert leg["details"].startswith("PRICE_DRIFT_ABORT")


def test_execute_retries_once_with_same_client_oid(monkeypatch):
    _router(monkeypatch)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    oids, attempts = [], []

    def flaky(*a, **kw):
        attempts.append(1)
        oids.append(kw.get("client_oid"))
        if len(attempts) == 1:
            raise RuntimeError('timeout {"retryable": true}')
        return {"orderId": "OID-R"}
    monkeypatch.setattr(cli, "place_order", flaky)
    monkeypatch.setattr(cli, "get_order",
                        lambda oid, **kw: {"orderStatus": "filled",
                                           "avgPrice": "90000",
                                           "cumExecQty": "0.011",
                                           "cumExecValue": "1000",
                                           "feeDetail": []})
    leg = executor.execute({"decision": "HEDGE_CRYPTO", "confidence": 0.9},
                           "BTCUSDT")
    assert leg["executed"] is True and len(attempts) == 2
    assert oids[0] == oids[1]  # idempotent retry


def test_execute_does_not_retry_rejections(monkeypatch):
    _router(monkeypatch)
    attempts = []

    def reject(*a, **kw):
        attempts.append(1)
        raise RuntimeError("Parameter X does not exist "
                           '{"retryable": false}')
    monkeypatch.setattr(cli, "place_order", reject)
    leg = executor.execute({"decision": "HEDGE_CRYPTO", "confidence": 0.9},
                           "BTCUSDT")
    assert leg["executed"] is False and len(attempts) == 1


def test_partial_fill_cancels_remainder_and_books_partial(monkeypatch):
    _router(monkeypatch)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "place_order",
                        lambda *a, **kw: {"orderId": "OID-P"})
    partial = {"orderStatus": "partially_filled", "avgPrice": "90000",
               "cumExecQty": "0.005", "cumExecValue": "450",
               "feeDetail": [{"feeCoin": "USDT", "fee": "0.45"}]}
    monkeypatch.setattr(cli, "get_order", lambda oid, **kw: dict(partial))
    cancelled = []
    monkeypatch.setattr(cli, "cancel_order",
                        lambda oid, **kw: cancelled.append(oid) or {})
    leg = executor.execute({"decision": "HEDGE_CRYPTO", "confidence": 0.9},
                           "BTCUSDT")
    assert leg["executed"] is True and leg["partial"] is True
    assert cancelled == ["OID-P"]
    assert leg["fill_qty"] == 0.005 and leg["fee_usd"] == 0.45


def test_confirmed_zero_fill_reports_no_fill(monkeypatch):
    _router(monkeypatch)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "place_order",
                        lambda *a, **kw: {"orderId": "OID-Z"})
    resting = {"orderStatus": "live", "avgPrice": "",
               "cumExecQty": "0", "cumExecValue": "0", "feeDetail": []}
    monkeypatch.setattr(cli, "get_order", lambda oid, **kw: dict(resting))
    cancelled = []
    monkeypatch.setattr(cli, "cancel_order",
                        lambda oid, **kw: cancelled.append(oid) or {})
    leg = executor.execute({"decision": "HEDGE_CRYPTO", "confidence": 0.9},
                           "BTCUSDT")
    assert leg["executed"] is False and cancelled == ["OID-Z"]
    assert leg["details"].startswith("NO_FILL")


def test_size_for_confidence_is_continuous_and_capped():
    """Sizing scales smoothly with conviction (no more two fixed tiers)."""
    import config
    cap = float(config.RISK_MAX_POSITION_USD)
    # Below the floor -> skip.
    assert executor.size_for_confidence(0.0) == 0.0
    assert executor.size_for_confidence(executor.MID_CONF_T - 0.01) == 0.0
    # At the threshold -> the floor; at/over full confidence -> the cap.
    assert executor.size_for_confidence(executor.MID_CONF_T) == round(
        min(executor.SIZE_FLOOR, cap), 2)
    assert executor.size_for_confidence(1.0) == cap
    assert executor.size_for_confidence(1.5) == cap  # clamped to the cage
    # Strictly increasing and all-distinct across the band: a real book,
    # not the old $500/$1000 step where every trade came out identical.
    sizes = [executor.size_for_confidence(c)
             for c in (0.6, 0.7, 0.8, 0.9, 1.0)]
    assert sizes == sorted(sizes)
    assert len(set(sizes)) == len(sizes)
    # Bad input never raises.
    assert executor.size_for_confidence("x") == 0.0
    assert executor.size_for_confidence(None) == 0.0
