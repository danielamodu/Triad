"""Tests for the Layer 1 tick wiring + broker-health marker."""
import os
import threading

import config
import main
from src.execution import executor


def _quiet_tick(monkeypatch, tmp_path, decide_result, execute_result=None,
                cash=25000.0):
    """Run main.tick() fully mocked; return the logged entry."""
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
                                 "basket_scores": {"RAAPLUSDT": 0.0}})
    monkeypatch.setattr(main, "get_event",
                        lambda: {"signal": "NEUTRAL", "confidence": 0.5})
    monkeypatch.setattr(main, "get_sentiment",
                        lambda: {"sentiment": "neutral", "score": 0.5})
    monkeypatch.setattr(main, "get_positions", lambda **kw: {"__ok": True})
    monkeypatch.setattr(main, "get_balance", lambda coin, **kw: cash)
    monkeypatch.setattr(main, "decide",
                        lambda signals, pos, mem, **kw: decide_result)
    if execute_result is not None:
        monkeypatch.setattr(main, "execute",
                            lambda dec, sym, **kw: dict(execute_result))
    captured = {}
    monkeypatch.setattr(main, "append_log",
                        lambda entry: captured.update(entry) or "mock-path")
    main.MEMORY.clear()
    main.OPEN_POSITIONS.clear()
    main._STATE_SAVE_OK = True
    main.tick()
    main.MEMORY.clear()
    main.OPEN_POSITIONS.clear()
    return captured


def test_tick_logs_intended_and_executed_size(monkeypatch, tmp_path):
    fill = {"executed": True, "order_id": "7", "symbol": "RAAPLUSDT",
            "side": "buy", "notional_usdt": 1000.0,
            "details": {"symbol": "RAAPLUSDT", "side": "buy",
                        "notional_usdt": 1000.0, "executed": True}}
    entry = _quiet_tick(monkeypatch, tmp_path,
                        {"decision": "LONG_RTOKEN", "confidence": 0.9,
                         "reasoning": "t", "scores": {},
                         "engine_used": "test"},
                        fill)
    assert entry["position_size_usd"] == executor.size_for_confidence(0.9)
    assert entry["executed_notional_usd"] == 1000.0  # actually filled
    assert "drawdown_pct" in entry
    assert isinstance(entry["memory_summary"], list)


def test_blocked_tick_logs_zero_executed_notional(monkeypatch, tmp_path):
    entry = _quiet_tick(monkeypatch, tmp_path,
                        {"decision": "HOLD", "confidence": 0.0,
                         "reasoning": "t", "scores": {},
                         "engine_used": "test"})
    assert entry["position_size_usd"] == 0.0
    assert entry["executed_notional_usd"] == 0.0
    assert entry["action_taken"]["executed"] is False


def test_tick_persists_fills_to_state_file(monkeypatch, tmp_path):
    fill = {"executed": True, "order_id": "9", "symbol": "RAAPLUSDT",
            "side": "buy", "notional_usdt": 500.0,
            "details": {"symbol": "RAAPLUSDT", "side": "buy",
                        "notional_usdt": 500.0, "executed": True}}
    _quiet_tick(monkeypatch, tmp_path,
                {"decision": "LONG_RTOKEN", "confidence": 0.7,
                 "reasoning": "t", "scores": {}, "engine_used": "test"},
                fill)
    from src.risk import state as risk_state
    loaded, ok = risk_state.load_state(
        os.path.join(str(tmp_path), "risk_state.json"))
    assert ok is True
    assert loaded["exposure"] == {"RAAPLUSDT": 500.0}


def test_tick_persists_open_leg_for_restart(monkeypatch, tmp_path):
    fill = {"executed": True, "order_id": "11", "symbol": "RAAPLUSDT",
            "side": "buy", "notional_usdt": 500.0, "fill_price": 100.0,
            "details": {"symbol": "RAAPLUSDT", "side": "buy",
                        "notional_usdt": 500.0, "fill_price": 100.0,
                        "executed": True}}
    _quiet_tick(monkeypatch, tmp_path,
                {"decision": "LONG_RTOKEN", "confidence": 0.7,
                 "reasoning": "t", "scores": {}, "engine_used": "test"},
                fill)
    from src.risk import positions as position_book
    book, ok = position_book.load_positions(
        os.path.join(str(tmp_path), "positions.json"))
    assert ok is True
    assert book["RAAPLUSDT"]["entry_price"] == 100.0
    assert book["RAAPLUSDT"]["side"] == "long"


def test_tick_fails_closed_on_corrupt_state(monkeypatch, tmp_path):
    target = os.path.join(str(tmp_path), "risk_state.json")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("garbage{{{")
    entry = _quiet_tick(monkeypatch, tmp_path,
                        {"decision": "LONG_RTOKEN", "confidence": 0.9,
                         "reasoning": "t", "scores": {},
                         "engine_used": "test"},
                        {"executed": True, "order_id": "x"})
    assert entry["action_taken"]["executed"] is False
    assert "fail-closed" in entry["risk"]["blocked_reason"]


def test_tick_halts_after_repeated_broker_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RISK_STATE_FILE",
                        os.path.join(str(tmp_path), "risk_state.json"))
    monkeypatch.setattr(config, "POSITIONS_FILE",
                        os.path.join(str(tmp_path), "positions.json"))
    monkeypatch.setattr(main, "get_divergence",
                        lambda: {"signal": "STABLE", "direction": "FLAT",
                                 "divergence_score": 0.0, "rtoken_change": 0.0,
                                 "crypto_change": 0.0, "rtoken_last": "100",
                                 "crypto_last": "90000"})
    monkeypatch.setattr(main, "get_event",
                        lambda: {"signal": "NEUTRAL", "confidence": 0.5})
    monkeypatch.setattr(main, "get_sentiment",
                        lambda: {"sentiment": "neutral", "score": 0.5})
    monkeypatch.setattr(main, "get_positions", lambda **kw: {})  # outage
    monkeypatch.setattr(main, "get_balance", lambda coin, **kw: 25000.0)
    monkeypatch.setattr(main, "decide",
                        lambda s, p, m, **kw: {"decision": "LONG_RTOKEN",
                                         "confidence": 0.9, "reasoning": "t",
                                         "scores": {}, "engine_used": "test"})
    monkeypatch.setattr(main, "execute",
                        lambda dec, sym, **kw: {"executed": True, "order_id": "x",
                                          "symbol": sym, "side": "buy",
                                          "notional_usdt": 1000.0,
                                          "details": {"symbol": sym,
                                                      "side": "buy",
                                                      "notional_usdt": 1000.0,
                                                      "executed": True}})
    captured = {}
    monkeypatch.setattr(main, "append_log",
                        lambda e: captured.update(e) or "mock")
    main.MEMORY.clear()
    main.OPEN_POSITIONS.clear()
    main._STATE_SAVE_OK = True
    reasons = []
    for _ in range(config.RISK_BROKER_FAIL_TICKS):
        main.tick()
        reasons.append(captured["risk"].get("blocked_reason", ""))
    main.MEMORY.clear()
    main.OPEN_POSITIONS.clear()
    assert "broker snapshot failing" in reasons[-1]


def test_get_positions_marks_successful_snapshot(monkeypatch):
    monkeypatch.setattr(executor.cli, "_run",
                        lambda *a, **k: {"assets": {"assets": []}})
    assert executor.get_positions().get("__ok") is True


def test_get_positions_failure_has_no_marker(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(executor.cli, "_run", boom)
    assert executor.get_positions() == {}


def test_tracked_book_excludes_dunder_keys(monkeypatch, tmp_path):
    entry = _quiet_tick(monkeypatch, tmp_path,
                        {"decision": "HOLD", "confidence": 0.0,
                         "reasoning": "t", "scores": {},
                         "engine_used": "test"})
    assert entry["risk"]["decision"] == "HOLD"  # cage saw a clean book


def test_tick_logs_wallet_and_balance_change(monkeypatch, tmp_path):
    main._LAST_WALLET_USD = None
    try:
        hold = {"decision": "HOLD", "confidence": 0.0, "reasoning": "t",
                "scores": {}, "engine_used": "test"}
        first = _quiet_tick(monkeypatch, tmp_path, hold)
        assert first["wallet_usd"] == 25000.0
        assert first["wallet"]["USDT"] == 25000.0
        assert first["balance_change_usd"] == 0.0  # baseline tick
        second = _quiet_tick(monkeypatch, tmp_path, hold, cash=25100.0)
        assert second["wallet_usd"] == 25100.0
        assert second["balance_change_usd"] == 100.0
    finally:
        main._LAST_WALLET_USD = None


def test_wallet_snapshot_unknown_on_broker_failure():
    wallet, total = main._wallet_snapshot({"__error": "down"}, False)
    assert wallet == {} and total is None
    assert main._balance_change(None) is None


def test_wallet_snapshot_sums_legs_and_cash(monkeypatch):
    monkeypatch.setattr(main, "get_balance", lambda coin, **kw: 500.0)
    wallet, total = main._wallet_snapshot(
        {"BTCUSDT": {"usd": 1000.0}, "__ok": True}, True)
    assert wallet == {"BTCUSDT": 1000.0, "USDT": 500.0}
    assert total == 1500.0


def _wake_summary(monkeypatch, tmp_path, price, event, sentiment):
    """Run main.tick() with fixed signals; return tick()'s summary dict."""
    monkeypatch.setattr(config, "RISK_STATE_FILE",
                        os.path.join(str(tmp_path), "risk_state.json"))
    monkeypatch.setattr(config, "POSITIONS_FILE",
                        os.path.join(str(tmp_path), "positions.json"))
    monkeypatch.setattr(main, "get_divergence", lambda: dict(price))
    monkeypatch.setattr(main, "get_event", lambda: dict(event))
    monkeypatch.setattr(main, "get_sentiment", lambda: dict(sentiment))
    monkeypatch.setattr(main, "get_positions", lambda **kw: {"__ok": True})
    monkeypatch.setattr(main, "decide",
                        lambda s, p, m, **kw: {"decision": "HOLD",
                                               "confidence": 0.0,
                                               "reasoning": "t", "scores": {},
                                               "engine_used": "test"})
    monkeypatch.setattr(main, "append_log", lambda e: "mock")
    main.MEMORY.clear()
    main.OPEN_POSITIONS.clear()
    main._STATE_SAVE_OK = True
    try:
        return main.tick()
    finally:
        main.MEMORY.clear()
        main.OPEN_POSITIONS.clear()


CALM_PRICE = {"signal": "STABLE", "direction": "FLAT", "divergence_score": 0.0,
              "rtoken_change": 0.0, "crypto_change": 0.0,
              "rtoken_last": "100", "crypto_last": "90000"}
CALM_EVENT = {"signal": "NEUTRAL", "confidence": 0.5}
CALM_SENT = {"sentiment": "neutral", "score": 0.5}


def test_wake_reason_divergence(monkeypatch, tmp_path):
    price = dict(CALM_PRICE, signal="DIVERGENCE_DETECTED")
    summary = _wake_summary(monkeypatch, tmp_path, price, CALM_EVENT,
                            CALM_SENT)
    assert summary["wake_reason"] == "divergence"
    assert summary["diverged"] is True


def test_wake_reason_event_needs_conviction(monkeypatch, tmp_path):
    hot = {"signal": "BULLISH", "confidence": 0.9}
    summary = _wake_summary(monkeypatch, tmp_path, CALM_PRICE, hot,
                            CALM_SENT)
    assert summary["wake_reason"] == "event"
    cold = {"signal": "BULLISH", "confidence": 0.5}
    summary = _wake_summary(monkeypatch, tmp_path, CALM_PRICE, cold,
                            CALM_SENT)
    assert summary["wake_reason"] == ""
    # Bearish reads score below 0.5 by construction; strong ones wake too.
    dread = {"signal": "BEARISH", "confidence": 0.05}
    summary = _wake_summary(monkeypatch, tmp_path, CALM_PRICE, dread,
                            CALM_SENT)
    assert summary["wake_reason"] == "event"


def test_wake_reason_sentiment_extremes(monkeypatch, tmp_path):
    fear = {"sentiment": "bearish", "score": 0.1}
    summary = _wake_summary(monkeypatch, tmp_path, CALM_PRICE, CALM_EVENT,
                            fear)
    assert summary["wake_reason"] == "sentiment"
    calm = _wake_summary(monkeypatch, tmp_path, CALM_PRICE, CALM_EVENT,
                         CALM_SENT)
    assert calm["wake_reason"] == ""
    assert calm["diverged"] is False


def test_wake_reason_never_raises_on_garbage():
    assert main.wake_reason({}, {}, {}) == ""
    assert main.wake_reason(None, None, None) == ""


def test_exit_fires_both_legs_simultaneously(monkeypatch, tmp_path):
    """EXIT's two legs must run concurrently: a barrier both legs have to
    reach proves simultaneity (sequential execution would time out)."""
    gate = threading.Barrier(2, timeout=10)
    calls = []
    monkeypatch.setattr(config, "RISK_STATE_FILE",
                        os.path.join(str(tmp_path), "risk_state.json"))
    monkeypatch.setattr(config, "POSITIONS_FILE",
                        os.path.join(str(tmp_path), "positions.json"))
    monkeypatch.setattr(main, "get_divergence", lambda: dict(CALM_PRICE))
    monkeypatch.setattr(main, "get_event", lambda: dict(CALM_EVENT))
    monkeypatch.setattr(main, "get_sentiment", lambda: dict(CALM_SENT))
    monkeypatch.setattr(main, "get_positions", lambda **kw: {"__ok": True})
    monkeypatch.setattr(main, "get_balance", lambda coin, **kw: 25000.0)
    monkeypatch.setattr(main, "decide",
                        lambda s, p, m, **kw: {"decision": "EXIT",
                                               "confidence": 0.9,
                                               "reasoning": "t", "scores": {},
                                               "engine_used": "test"})

    def fake_execute(dec, sym, **kw):
        calls.append(sym)
        gate.wait()  # both legs must be in flight together
        return {"executed": True, "order_id": "x-" + sym, "symbol": sym,
                "side": "sell", "notional_usdt": 100.0, "fill_value": 100.0}

    monkeypatch.setattr(main, "execute", fake_execute)
    captured = {}
    monkeypatch.setattr(main, "append_log",
                        lambda e: captured.update(e) or "mock")
    main.MEMORY.clear()
    main.OPEN_POSITIONS.clear()
    main._STATE_SAVE_OK = True
    try:
        main.tick()
    finally:
        main.MEMORY.clear()
        main.OPEN_POSITIONS.clear()
    assert sorted(calls) == ["BTCUSDT", "RAAPLUSDT"]
    details = captured["action_taken"]["details"]
    assert isinstance(details, list) and len(details) == 2
    assert captured["action_taken"]["executed"] is True
    # Deterministic log order regardless of thread scheduling.
    assert [leg["symbol"] for leg in details] == sorted(
        leg["symbol"] for leg in details)


def _bot_leg(symbol="RAAPLUSDT", side="long", entry=100.0, current=100.0,
             reconciled=False):
    leg = {"symbol": symbol, "side": side, "size_usd": 1000.0,
           "entry_price": entry, "current_price": current,
           "pnl": 0.0, "usd": 1000.0}
    if reconciled:
        leg["reconciled"] = True
    return leg


def test_bracket_stop_and_take():
    main.OPEN_POSITIONS.clear()
    try:
        main.OPEN_POSITIONS["RAAPLUSDT"] = _bot_leg(current=97.5)
        assert main._bracket_breach() == ("RAAPLUSDT", "stop")
        main.OPEN_POSITIONS["RAAPLUSDT"] = _bot_leg(current=103.5)
        assert main._bracket_breach() == ("RAAPLUSDT", "take")
        main.OPEN_POSITIONS["RAAPLUSDT"] = _bot_leg(
            side="short", current=97.0)
        assert main._bracket_breach() == ("RAAPLUSDT", "take")
        main.OPEN_POSITIONS["RAAPLUSDT"] = _bot_leg(current=101.0)
        assert main._bracket_breach() == ("", "")
    finally:
        main.OPEN_POSITIONS.clear()


def test_bracket_ignores_adopted_inventory():
    main.OPEN_POSITIONS.clear()
    try:
        main.OPEN_POSITIONS["BTCUSDT"] = _bot_leg(
            symbol="BTCUSDT", entry=90000.0, current=45000.0,
            reconciled=True)
        assert main._bracket_breach() == ("", "")
    finally:
        main.OPEN_POSITIONS.clear()
    assert main._bracket_breach() == ("", "")


def test_bracket_breach_forces_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RISK_STATE_FILE",
                        os.path.join(str(tmp_path), "risk_state.json"))
    monkeypatch.setattr(config, "POSITIONS_FILE",
                        os.path.join(str(tmp_path), "positions.json"))
    # Signal marks the bot leg 3% under its entry: the tick's own price
    # refresh trips the stop (marks always come from the feed, never test
    # fixtures sitting in the book).
    monkeypatch.setattr(main, "get_divergence",
                        lambda: dict(CALM_PRICE, rtoken_last="97"))
    monkeypatch.setattr(main, "get_event", lambda: dict(CALM_EVENT))
    monkeypatch.setattr(main, "get_sentiment", lambda: dict(CALM_SENT))
    monkeypatch.setattr(main, "get_positions", lambda **kw: {"__ok": True})
    monkeypatch.setattr(main, "get_balance", lambda coin, **kw: 25000.0)
    monkeypatch.setattr(main, "decide",
                        lambda s, p, m, **kw: {"decision": "HOLD",
                                               "confidence": 0.0,
                                               "reasoning": "t", "scores": {},
                                               "engine_used": "test"})
    seen = {}
    monkeypatch.setattr(main, "execute",
                        lambda dec, sym, **kw: seen.update(decision=dec) or
                        {"executed": True, "order_id": "z", "symbol": sym,
                         "side": "sell", "notional_usdt": 1000.0,
                         "fill_value": 1000.0})
    captured = {}
    monkeypatch.setattr(main, "append_log",
                        lambda e: captured.update(e) or "mock")
    main.MEMORY.clear()
    main.OPEN_POSITIONS.clear()
    main.OPEN_POSITIONS["RAAPLUSDT"] = _bot_leg(entry=100.0, current=100.0)
    main._STATE_SAVE_OK = True
    try:
        main.tick()
    finally:
        main.MEMORY.clear()
        main.OPEN_POSITIONS.clear()
    assert seen["decision"]["decision"] == "EXIT"
    assert captured["decision"]["bracket_trigger"] == "RAAPLUSDT:stop"
    assert "bracket stop" in captured["decision"]["reasoning"]
    assert captured["action_taken"]["executed"] is True
