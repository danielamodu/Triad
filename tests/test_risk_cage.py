"""Tests for src/risk/cage.py: gates old and new."""
import json
import os

import config
from src.risk.cage import bot_exposure, validate

LONG = {"decision": "LONG_RTOKEN", "confidence": 0.9}
HOLD = {"decision": "HOLD", "confidence": 0.0}
HEALTHY = {"drawdown_pct": 0.0, "day_loss_pct": 0.0, "daily_halted": False,
           "exposure": {}, "corrupt": False, "broker_dead": False,
           "broker_streak": 0}


def test_hold_approved_when_healthy():
    assert validate(HOLD, {}, HEALTHY)["approved"] is True


def test_unknown_decision_blocked():
    r = validate({"decision": "YOLO"}, {}, HEALTHY)
    assert r["approved"] is False and "unknown" in r["blocked_reason"]


def test_kill_file_halts(tmp_path, monkeypatch):
    kill = os.path.join(str(tmp_path), "KILL")
    with open(kill, "w", encoding="utf-8") as fh:
        fh.write("stop")
    monkeypatch.setattr(config, "KILL_FILE", kill)
    assert validate(LONG, {}, HEALTHY)["blocked_reason"] == "KILL file present"


def test_corrupt_state_fails_closed():
    r = validate(LONG, {}, {**HEALTHY, "corrupt": True})
    assert r["approved"] is False and "fail-closed" in r["blocked_reason"]
    # HOLD is halted too: the log must show the halt, not a quiet pass.
    assert validate(HOLD, {}, {**HEALTHY, "corrupt": True})["approved"] is False


def test_broker_outage_fails_closed():
    r = validate(LONG, {}, {**HEALTHY, "broker_dead": True, "broker_streak": 3})
    assert r["approved"] is False and "broker" in r["blocked_reason"]


def test_drawdown_halt_from_ledger():
    r = validate(LONG, {}, {**HEALTHY, "drawdown_pct": 0.06})
    assert r["approved"] is False and "drawdown" in r["blocked_reason"]
    assert validate(LONG, {}, {**HEALTHY, "drawdown_pct": 0.04})["approved"] is True


def test_drawdown_legacy_key_still_works():
    r = validate(LONG, {"__drawdown_pct": 0.99})
    assert r["approved"] is False and "drawdown" in r["blocked_reason"]


def test_daily_loss_halt_blocks_sells_too():
    ctx = {**HEALTHY, "daily_halted": True, "day_loss_pct": 0.03}
    assert validate(LONG, {}, ctx)["approved"] is False
    assert validate({"decision": "EXIT"}, {}, ctx)["approved"] is False
    assert "daily loss" in validate(LONG, {}, ctx)["blocked_reason"]


def test_no_doubling_uses_supplied_exposure():
    ctx = {**HEALTHY, "exposure": {"RAAPLUSDT": 500.0}}
    r = validate(LONG, {}, ctx, trade_symbol="RAAPLUSDT")
    assert r["approved"] is False and "no doubling" in r["blocked_reason"]
    # A different leg is unaffected.
    assert validate(LONG, {}, ctx, trade_symbol="RNVDAUSDT")["approved"] is True


def test_sells_pass_size_gates():
    ctx = {**HEALTHY, "exposure": {"BTCUSDT": 500.0}}
    assert validate({"decision": "HEDGE_CRYPTO"}, {}, ctx)["approved"] is True
    assert validate({"decision": "EXIT"}, {}, ctx)["approved"] is True


def test_log_replay_fallback_when_no_risk_ctx(tmp_path, monkeypatch):
    # bot_exposure() joins BASE_DIR + LOG_FILE ("logs/trades.jsonl"),
    # so point BASE_DIR at tmp and write the log there.
    monkeypatch.setattr(config, "BASE_DIR", str(tmp_path))
    os.makedirs(os.path.join(str(tmp_path), "logs"), exist_ok=True)
    with open(os.path.join(str(tmp_path), "logs", "trades.jsonl"), "w",
              encoding="utf-8") as fh:
        fh.write(json.dumps({"action_taken": {"executed": True,
                                              "symbol": "RAAPLUSDT",
                                              "side": "buy",
                                              "notional_usdt": 500.0}}) + "\n")
    assert bot_exposure() == {"RAAPLUSDT": 500.0}
    r = validate(LONG, {})  # no risk ctx -> log replay, no doubling
    assert r["approved"] is False
