"""Tests for realized-PnL alignment and Groq constraints."""
import json
import os

import config
import main
from src.decision import engine
from src.logger import _entry_pnl, append_jsonl
from src.risk import state as risk_state


def test_state_carries_new_fields_with_defaults(tmp_path):
    target = os.path.join(str(tmp_path), "risk_state.json")
    st, ok = risk_state.load_state(target)
    assert ok is True
    assert st["realized_pnl"] == 0.0
    assert st["groq_streak"] == 0
    assert st["groq_cooldown"] == 0
    # Old files without the keys still load (forward compatible).
    with open(target, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "exposure": {}, "peak_pnl": 0.0,
                   "peak_exposure_usd": 0.0, "day": "", "day_start_pnl": 0.0,
                   "broker_fail_streak": 0}, fh)
    st2, ok2 = risk_state.load_state(target)
    assert ok2 is True and st2["realized_pnl"] == 0.0


def test_add_realized_accumulates():
    st = risk_state.fresh_state()
    assert risk_state.add_realized(st, 12.5) == 12.5
    assert risk_state.add_realized(st, -2.5) == 10.0
    assert risk_state.add_realized(st, "garbage") == 0.0


def test_build_prompt_includes_memory():
    prompt = engine.build_prompt({"a": 1}, {"b": 2}, {"c": 3}, {"d": 4},
                                 [{"decision": "HOLD"}])
    assert "Recent decisions" in prompt
    assert '"HOLD"' in prompt


def test_decide_force_fallback_skips_groq(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("groq must not be called")
    monkeypatch.setattr(engine, "groq_decide", boom)
    verdict = engine.decide({"price": {}, "event": {}, "sentiment": {}},
                            {}, [], force_fallback=True)
    assert verdict["engine_used"] == "weighted_fallback"
    assert "cooldown" in verdict["fallback_reason"]


def test_decide_attaches_agreement(monkeypatch):
    monkeypatch.setattr(
        engine, "groq_decide",
        lambda *a, **kw: {"decision": "LONG_RTOKEN", "confidence": 0.9,
                          "reasoning": "t", "engine_used": "groq"})
    verdict = engine.decide({"price": {}, "event": {}, "sentiment": {}},
                            {}, [])
    assert verdict["fallback_decision"] in (
        "LONG_RTOKEN", "HEDGE_CRYPTO", "HOLD", "EXIT")
    assert verdict["fallback_agree"] == (
        verdict["decision"] == verdict["fallback_decision"])


def test_sync_positions_returns_realized():
    main.OPEN_POSITIONS.clear()
    main.OPEN_POSITIONS["BTCUSDT"] = {
        "symbol": "BTCUSDT", "side": "long", "size_usd": 500.0,
        "entry_price": 90000.0, "current_price": 90000.0, "pnl": 0.0,
        "usd": 500.0}
    realized = main._sync_positions(
        {"executed": True,
         "details": {"symbol": "BTCUSDT", "side": "sell",
                     "notional_usdt": 500.0, "fill_value": 550.0,
                     "executed": True}},
        {"rtoken_last": "1", "crypto_last": "95000"}, "HEDGE_CRYPTO")
    assert realized == 50.0  # fill proceeds minus booked size
    assert main.OPEN_POSITIONS == {}
    # No fill data: falls back to the stored mark.
    main.OPEN_POSITIONS["X"] = {"symbol": "X", "side": "long",
                                "size_usd": 100.0, "entry_price": 1.0,
                                "current_price": 1.0, "pnl": -7.5,
                                "usd": 100.0}
    realized = main._sync_positions(
        {"executed": True,
         "details": {"symbol": "X", "side": "sell", "notional_usdt": 100.0,
                     "executed": True}},
        {}, "EXIT")
    assert realized == -7.5
    main.OPEN_POSITIONS.clear()


def _groq_tick(monkeypatch, tmp_path, verdict, cooldown=0):
    monkeypatch.setattr(config, "RISK_STATE_FILE",
                        os.path.join(str(tmp_path), "risk_state.json"))
    monkeypatch.setattr(config, "GROQ_TRACE_FILE",
                        os.path.join(str(tmp_path), "groq.jsonl"))
    monkeypatch.setattr(main, "get_divergence",
                        lambda: {"signal": "STABLE", "direction": "FLAT",
                                 "divergence_score": 0.0, "rtoken_change": 0.0,
                                 "crypto_change": 0.0, "rtoken_last": "100",
                                 "crypto_last": "90000"})
    monkeypatch.setattr(main, "get_event",
                        lambda: {"signal": "NEUTRAL", "confidence": 0.5})
    monkeypatch.setattr(main, "get_sentiment",
                        lambda: {"sentiment": "neutral", "score": 0.5})
    monkeypatch.setattr(main, "get_positions", lambda **kw: {"__ok": True})
    seen = {}
    def fake_decide(signals, pos, mem, **kw):
        seen["force"] = kw.get("force_fallback", False)
        return dict(verdict)
    monkeypatch.setattr(main, "decide", fake_decide)
    captured = {}
    monkeypatch.setattr(main, "append_log",
                        lambda e: captured.update(e) or "mock")
    main.MEMORY.clear()
    main.OPEN_POSITIONS.clear()
    main._STATE_SAVE_OK = True
    if cooldown:
        st, _ = risk_state.load_state(
            os.path.join(str(tmp_path), "risk_state.json"))
        st["groq_cooldown"] = cooldown
        risk_state.save_state(
            st, os.path.join(str(tmp_path), "risk_state.json"))
    main.tick()
    main.MEMORY.clear()
    main.OPEN_POSITIONS.clear()
    return captured, seen


def test_groq_disagreement_streak_trips_cooldown(monkeypatch, tmp_path):
    verdict = {"decision": "LONG_RTOKEN", "confidence": 0.9,
               "reasoning": "t", "scores": {}, "engine_used": "groq",
               "fallback_decision": "HOLD", "fallback_agree": False,
               "_prompt": "PROMPT", "_raw_response": "RAW"}
    for _ in range(config.GROQ_MAX_DISAGREE):
        _, seen = _groq_tick(monkeypatch, tmp_path, verdict)
        assert seen["force"] is False
    st, _ = risk_state.load_state(
        os.path.join(str(tmp_path), "risk_state.json"))
    assert st["groq_cooldown"] == config.GROQ_COOLDOWN_TICKS
    # Next tick is forced to fallback.
    _, seen = _groq_tick(monkeypatch, tmp_path, verdict)
    assert seen["force"] is True


def test_groq_trace_written_and_stripped_from_log(monkeypatch, tmp_path):
    import tempfile
    trace_dir = tempfile.mkdtemp()
    trace_path = os.path.join(trace_dir, "groq.jsonl")
    monkeypatch.setattr(config, "GROQ_TRACE_FILE", trace_path)
    verdict = {"decision": "HOLD", "confidence": 0.1, "reasoning": "t",
               "scores": {}, "engine_used": "groq",
               "fallback_decision": "HOLD", "fallback_agree": True,
               "latency_ms": 12.5, "_prompt": "PROMPT", "_raw_response": "RAW"}
    monkeypatch.setattr(config, "RISK_STATE_FILE",
                        os.path.join(str(tmp_path), "risk_state.json"))
    monkeypatch.setattr(main, "get_divergence",
                        lambda: {"signal": "STABLE", "direction": "FLAT",
                                 "divergence_score": 0.0, "rtoken_change": 0.0,
                                 "crypto_change": 0.0, "rtoken_last": "100",
                                 "crypto_last": "90000"})
    monkeypatch.setattr(main, "get_event",
                        lambda: {"signal": "NEUTRAL", "confidence": 0.5})
    monkeypatch.setattr(main, "get_sentiment",
                        lambda: {"sentiment": "neutral", "score": 0.5})
    monkeypatch.setattr(main, "get_positions", lambda **kw: {"__ok": True})
    monkeypatch.setattr(main, "decide",
                        lambda s, p, m, **kw: dict(verdict))
    captured = {}
    monkeypatch.setattr(main, "append_log",
                        lambda e: captured.update(e) or "mock")
    main.MEMORY.clear()
    main.OPEN_POSITIONS.clear()
    main._STATE_SAVE_OK = True
    main.tick()
    main.MEMORY.clear()
    main.OPEN_POSITIONS.clear()
    assert "_prompt" not in captured["decision"]
    assert "_raw_response" not in captured["decision"]
    with open(trace_path, encoding="utf-8") as fh:
        trace = json.loads(fh.read().strip())
    assert trace["prompt"] == "PROMPT" and trace["raw_response"] == "RAW"
    assert trace["model"] == config.GROQ_MODEL


def test_tick_logs_equity_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RISK_STATE_FILE",
                        os.path.join(str(tmp_path), "risk_state.json"))
    st, _ = risk_state.load_state(
        os.path.join(str(tmp_path), "risk_state.json"))
    st["realized_pnl"] = -30.0
    risk_state.save_state(st, os.path.join(str(tmp_path), "risk_state.json"))
    entry, _ = _groq_tick(monkeypatch, tmp_path,
                          {"decision": "HOLD", "confidence": 0.0,
                           "reasoning": "t", "scores": {},
                           "engine_used": "weighted_fallback",
                           "fallback_decision": "HOLD",
                           "fallback_agree": True})
    assert entry["realized_pnl"] == -30.0
    assert entry["equity_pnl"] == -30.0  # flat: equity == realized


def test_entry_pnl_prefers_equity(tmp_path):
    assert _entry_pnl({"equity_pnl": 5.0, "running_pnl": 1.0}) == 5.0
    assert _entry_pnl({"running_pnl": 1.0}) == 1.0  # old entries unchanged


def test_sync_positions_partial_close_keeps_residual():
    # Adopted whale leg: $720k booked, HEDGE sells $1000 of it. Only the
    # pro-rata share of unrealized realizes; the rest stays open.
    main.OPEN_POSITIONS.clear()
    main.OPEN_POSITIONS["BTCUSDT"] = {
        "symbol": "BTCUSDT", "side": "long", "size_usd": 720000.0,
        "entry_price": 90000.0, "current_price": 90125.0, "pnl": 1000.0,
        "usd": 720000.0}
    realized = main._sync_positions(
        {"executed": True,
         "details": {"symbol": "BTCUSDT", "side": "sell",
                     "notional_usdt": 1000.0, "fill_value": 1000.0,
                     "executed": True}},
        {"rtoken_last": "1", "crypto_last": "90125"}, "HEDGE_CRYPTO")
    assert 0.0 < realized < 5.0  # ~1000/721000 * 1000, not 1000-720000
    residual = main.OPEN_POSITIONS.get("BTCUSDT")
    assert residual is not None
    assert residual["size_usd"] == round(720000.0 * (1 - 1000.0 / 721000.0), 4)
    main.OPEN_POSITIONS.clear()


def test_sync_positions_exit_partial_keeps_residual():
    main.OPEN_POSITIONS.clear()
    main.OPEN_POSITIONS["BTCUSDT"] = {
        "symbol": "BTCUSDT", "side": "long", "size_usd": 50000.0,
        "entry_price": 90000.0, "current_price": 90000.0, "pnl": 500.0,
        "usd": 50000.0}
    realized = main._sync_positions(
        {"executed": True,
         "details": {"symbol": "BTCUSDT", "side": "sell",
                     "notional_usdt": 1000.0, "fill_value": 1000.0,
                     "executed": True}},
        {}, "EXIT")
    assert 0.0 < realized < 50.0
    assert "BTCUSDT" in main.OPEN_POSITIONS
    main.OPEN_POSITIONS.clear()


def test_append_jsonl_writes_scrubbed(tmp_path):
    path = append_jsonl(os.path.join(str(tmp_path), "t.jsonl"),
                        {"a": 1, "APIKEY": "secret"})
    with open(path, encoding="utf-8") as fh:
        row = json.loads(fh.read().strip())
    assert row["a"] == 1 and row["APIKEY"] == "***" and "timestamp" in row
