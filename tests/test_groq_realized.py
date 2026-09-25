"""Tests for realized-PnL alignment and Groq constraints."""
import json
import os

import config
import main
from src.decision import engine
from src.logger import _entry_pnl, append_jsonl, bot_entry_pnl, get_stats
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
    assert st2["adopted"] == {}


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
    monkeypatch.setattr(config, "POSITIONS_FILE",
                        os.path.join(str(tmp_path), "positions.json"))
    monkeypatch.setattr(config, "GROQ_TRACE_FILE",
                        os.path.join(str(tmp_path), "groq.jsonl"))
    # A worthwhile tick (live divergence) so the loop actually consults
    # Groq — the calm-tick gate would otherwise force the fallback.
    monkeypatch.setattr(main, "get_divergence",
                        lambda: {"signal": "DIVERGENCE_DETECTED",
                                 "direction": "RTOKEN_OUTPERFORM",
                                 "divergence_score": 0.02, "rtoken_change": 0.02,
                                 "crypto_change": 0.0, "rtoken_last": "100",
                                 "crypto_last": "90000",
                                 "selected_rtoken": "RAAPLUSDT"})
    monkeypatch.setattr(main, "get_event",
                        lambda: {"signal": "NEUTRAL", "confidence": 0.5})
    monkeypatch.setattr(main, "get_sentiment",
                        lambda: {"sentiment": "neutral", "score": 0.5})
    monkeypatch.setattr(main, "get_positions", lambda **kw: {"__ok": True})
    monkeypatch.setattr(main, "get_balance", lambda coin, **kw: 25000.0)
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
    monkeypatch.setattr(config, "POSITIONS_FILE",
                        os.path.join(str(tmp_path), "positions.json"))
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


def test_bot_entry_pnl_excludes_reconciled_legs():
    entry = {"realized_pnl": -9.0,
             "positions": [
                 {"symbol": "BTCUSDT", "pnl": -114.0, "reconciled": True},
                 {"symbol": "RAAPLUSDT", "pnl": 4.5}]}
    assert bot_entry_pnl(entry) == -4.5  # -9 + 4.5, adopted drift excluded
    assert bot_entry_pnl({"realized_pnl": 2.0}) == 2.0  # no legs, all closed
    assert bot_entry_pnl({}) == 0.0
    assert bot_entry_pnl(None) == 0.0


def test_bot_running_pnl_excludes_reconciled():
    main.OPEN_POSITIONS.clear()
    try:
        main.OPEN_POSITIONS["BTCUSDT"] = {"symbol": "BTCUSDT", "pnl": -114.0,
                                          "reconciled": True}
        main.OPEN_POSITIONS["RAAPLUSDT"] = {"symbol": "RAAPLUSDT", "pnl": 4.5}
        assert main._running_pnl() == -109.5  # gates keep the full basis
        assert main._bot_running_pnl() == 4.5  # display uses the bot basis
    finally:
        main.OPEN_POSITIONS.clear()


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


def test_sync_positions_adds_to_long():
    # A second BUY on an open bot long extends it (blended entry), not a
    # second leg — routing every bullish call through BTC must net.
    main.OPEN_POSITIONS.clear()
    main.OPEN_POSITIONS["BTCUSDT"] = {
        "symbol": "BTCUSDT", "side": "long", "size_usd": 500.0,
        "entry_price": 90000.0, "current_price": 90000.0, "pnl": 0.0,
        "usd": 500.0}
    realized = main._sync_positions(
        {"executed": True,
         "details": {"symbol": "BTCUSDT", "side": "buy",
                     "notional_usdt": 500.0, "fill_value": 500.0,
                     "fill_price": 100000.0, "executed": True}},
        {"crypto_last": "100000"}, "LONG_RTOKEN")
    assert realized == 0.0  # adding to a leg realizes nothing
    leg = main.OPEN_POSITIONS["BTCUSDT"]
    assert leg["side"] == "long" and leg["size_usd"] == 1000.0
    assert leg["entry_price"] == 95000.0  # (90000*500 + 100000*500)/1000
    main.OPEN_POSITIONS.clear()


def test_sync_positions_adds_to_short():
    main.OPEN_POSITIONS.clear()
    main.OPEN_POSITIONS["BTCUSDT"] = {
        "symbol": "BTCUSDT", "side": "short", "size_usd": 500.0,
        "entry_price": 90000.0, "current_price": 90000.0, "pnl": 0.0,
        "usd": 500.0}
    realized = main._sync_positions(
        {"executed": True,
         "details": {"symbol": "BTCUSDT", "side": "sell",
                     "notional_usdt": 500.0, "fill_value": 500.0,
                     "fill_price": 80000.0, "executed": True}},
        {"crypto_last": "80000"}, "HEDGE_CRYPTO")
    assert realized == 0.0
    leg = main.OPEN_POSITIONS["BTCUSDT"]
    assert leg["side"] == "short" and leg["size_usd"] == 1000.0
    assert leg["entry_price"] == 85000.0  # (90000*500 + 80000*500)/1000
    main.OPEN_POSITIONS.clear()


def test_sync_positions_buy_covers_short():
    # A BUY against an open bot short covers it (realizes the spread) and
    # leaves the book flat — the two-sided path EXIT relies on.
    main.OPEN_POSITIONS.clear()
    main.OPEN_POSITIONS["BTCUSDT"] = {
        "symbol": "BTCUSDT", "side": "short", "size_usd": 500.0,
        "entry_price": 90000.0, "current_price": 89000.0, "pnl": 5.5556,
        "usd": 500.0}
    realized = main._sync_positions(
        {"executed": True,
         "details": {"symbol": "BTCUSDT", "side": "buy",
                     "notional_usdt": 500.0, "fill_value": 495.0,
                     "executed": True}},
        {"crypto_last": "89000"}, "LONG_RTOKEN")
    assert realized == 5.0  # sold-at-500 booked, bought back for 495
    assert "BTCUSDT" not in main.OPEN_POSITIONS
    main.OPEN_POSITIONS.clear()


def test_append_jsonl_writes_scrubbed(tmp_path):
    path = append_jsonl(os.path.join(str(tmp_path), "t.jsonl"),
                        {"a": 1, "APIKEY": "secret"})
    with open(path, encoding="utf-8") as fh:
        row = json.loads(fh.read().strip())
    assert row["a"] == 1 and row["APIKEY"] == "***" and "timestamp" in row


def _write_log(tmp_path, rows):
    target = os.path.join(str(tmp_path), "t.jsonl")
    with open(target, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return target


def test_win_rate_counts_closed_trades(tmp_path):
    rows = [{"realized_pnl": 0.0, "action_taken": {"executed": True}},
            {"realized_pnl": 0.0, "action_taken": {"executed": False}},
            {"realized_pnl": 5.0, "action_taken": {"executed": True}},
            {"realized_pnl": 0.0, "action_taken":  # crashed tick: invisible
             {"executed": False, "details": "tick crashed"}},
            {"realized_pnl": 2.0, "action_taken": {"executed": True}}]
    stats = get_stats(_write_log(tmp_path, rows))
    assert stats["closed_trades"] == 2  # 0->5 win, 5->2 loss (crash skipped)
    assert stats["win_rate"] == 0.5
    assert get_stats(os.path.join(
        str(tmp_path), "missing.jsonl"))["win_rate"] == 0.0


def test_stats_totals_turnover_and_fees(tmp_path):
    rows = [{"executed_notional_usd": 1000.0, "fees_usd": 1.0,
             "realized_pnl": 0.0, "action_taken": {"executed": True}},
            {"executed_notional_usd": 500.0, "fees_usd": 0.5,
             "realized_pnl": 3.0, "action_taken": {"executed": True}},
            {"action_taken": {"executed": False}}]  # missing keys count 0
    stats = get_stats(_write_log(tmp_path, rows))
    assert stats["turnover_usd"] == 1500.0
    assert stats["total_fees_usd"] == 1.5
    assert stats["closed_trades"] == 1
    assert stats["win_rate"] == 1.0


def test_groq_worthwhile_gates_calm_ticks():
    main.OPEN_POSITIONS.clear()
    calm = {"signal": "STABLE"}
    neutral_evt = {"signal": "NEUTRAL"}
    neutral_sent = {"sentiment": "neutral", "score": 0.5}
    # Fully calm, flat book -> not worth an AI call.
    assert main._groq_worthwhile(calm, neutral_evt, neutral_sent) is False
    # Any real signal wakes the AI.
    assert main._groq_worthwhile(
        {"signal": "DIVERGENCE_DETECTED"}, neutral_evt, neutral_sent) is True
    assert main._groq_worthwhile(
        calm, {"signal": "BEARISH"}, neutral_sent) is True
    assert main._groq_worthwhile(
        calm, neutral_evt,
        {"score": 0.5 + config.GROQ_SENTIMENT_WAKE}) is True
    # A sub-threshold sentiment wobble stays calm.
    assert main._groq_worthwhile(
        calm, neutral_evt,
        {"score": 0.5 + config.GROQ_SENTIMENT_WAKE / 2}) is False
    # An open leg to manage always warrants the AI.
    main.OPEN_POSITIONS["BTCUSDT"] = {"symbol": "BTCUSDT", "pnl": 0.0}
    try:
        assert main._groq_worthwhile(calm, neutral_evt, neutral_sent) is True
    finally:
        main.OPEN_POSITIONS.clear()


def test_calm_tick_forces_fallback_without_calling_groq(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RISK_STATE_FILE",
                        os.path.join(str(tmp_path), "risk_state.json"))
    monkeypatch.setattr(config, "POSITIONS_FILE",
                        os.path.join(str(tmp_path), "positions.json"))
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
    monkeypatch.setattr(main, "get_balance", lambda coin, **kw: 25000.0)
    seen = {}

    def fake_decide(signals, pos, mem, **kw):
        seen["force"] = kw.get("force_fallback", False)
        seen["reason"] = kw.get("fallback_reason", "")
        return {"decision": "HOLD", "confidence": 0.0, "reasoning": "t",
                "scores": {}, "engine_used": "weighted_fallback",
                "fallback_decision": "HOLD", "fallback_agree": True}
    monkeypatch.setattr(main, "decide", fake_decide)
    monkeypatch.setattr(main, "append_log", lambda e: "mock")
    main.MEMORY.clear()
    main.OPEN_POSITIONS.clear()
    main._STATE_SAVE_OK = True
    main.tick()
    main.MEMORY.clear()
    main.OPEN_POSITIONS.clear()
    assert seen["force"] is True
    assert "calm tick" in seen["reason"]
# RETRY_TESTS_PLACEHOLDER


def test_retry_after_seconds_detects_429_and_reads_header():
    class Resp:
        headers = {"retry-after": "2.5"}

    class RateLimitError(Exception):
        status_code = 429
        response = Resp()
    assert engine._retry_after_seconds(RateLimitError("429 too many")) == 2.5

    class Bare(Exception):
        status_code = 429
    assert engine._retry_after_seconds(Bare("rate limit")) == float(
        config.GROQ_RETRY_BASE_SEC)
    # Non-429 -> negative sentinel, so the caller re-raises immediately.
    assert engine._retry_after_seconds(ValueError("bad json")) < 0


def _client_raising(exc_factory, succeed_on=None):
    calls = {"n": 0}

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    calls["n"] += 1
                    if succeed_on is not None and calls["n"] >= succeed_on:
                        return "OK"
                    raise exc_factory()
    return Client(), calls


def test_create_with_retry_retries_once_then_succeeds(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "GROQ_MAX_RETRIES", 1)
    monkeypatch.setattr(config, "GROQ_RETRY_CAP_SEC", 5.0)

    def make():
        e = RuntimeError("429 rate limit")
        e.status_code = 429
        e.response = type("R", (), {"headers": {"retry-after": "1"}})()
        return e
    client, calls = _client_raising(make, succeed_on=2)
    assert engine._create_with_retry(client, "p") == "OK"
    assert calls["n"] == 2


def test_create_with_retry_falls_back_when_cooldown_exceeds_cap(monkeypatch):
    import time
    import pytest
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(config, "GROQ_MAX_RETRIES", 1)
    monkeypatch.setattr(config, "GROQ_RETRY_CAP_SEC", 3.0)

    def make():
        e = RuntimeError("429 rate limit")
        e.status_code = 429
        e.response = type("R", (), {"headers": {"retry-after": "60"}})()
        return e
    client, calls = _client_raising(make)  # always raises
    with pytest.raises(RuntimeError):
        engine._create_with_retry(client, "p")
    assert calls["n"] == 1  # long server cooldown -> no retry, fall back now
    assert slept == []
