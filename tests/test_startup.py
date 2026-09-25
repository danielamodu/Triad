"""Tests for boot-time resume-from-broker reconciliation."""
import json
import os

import config
import main
from src.risk import state as risk_state


def _boot(monkeypatch, tmp_path, snapshot, opens=None, state_ok=True):
    monkeypatch.setattr(config, "RISK_STATE_FILE",
                        os.path.join(str(tmp_path), "risk_state.json"))
    monkeypatch.setattr(config, "POSITIONS_FILE",
                        os.path.join(str(tmp_path), "positions.json"))
    # Isolated trade log: the adopted-baseline repair reads fill history,
    # which must not leak in from the developer's real log file.
    log_target = os.path.join(str(tmp_path), "trades.jsonl")
    with open(log_target, "w", encoding="utf-8") as fh:
        fh.write("")
    monkeypatch.setattr(config, "LOG_FILE", log_target)
    if not state_ok:
        with open(os.path.join(str(tmp_path), "risk_state.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("corrupt{{{")
    monkeypatch.setattr(main, "get_divergence",
                        lambda: {"signal": "STABLE", "direction": "FLAT",
                                 "divergence_score": 0.0, "rtoken_change": 0.0,
                                 "crypto_change": 0.0, "rtoken_last": "300",
                                 "crypto_last": "90000"})
    monkeypatch.setattr(main, "get_positions", lambda **kw: dict(snapshot))
    monkeypatch.setattr(main.cli, "open_orders", lambda *a, **kw: opens or [])
    main.OPEN_POSITIONS.clear()
    main._STATE_SAVE_OK = True
    report = main.reconcile_startup(False)
    adopted = dict(main.OPEN_POSITIONS)
    main.OPEN_POSITIONS.clear()
    return report, adopted


def test_adopts_unknown_broker_balance_and_seeds_ledger(tmp_path, monkeypatch):
    report, adopted = _boot(monkeypatch, tmp_path,
                            {"BTCUSDT": {"usd": 500.0}, "__ok": True})
    assert adopted["BTCUSDT"]["size_usd"] == 500.0
    assert adopted["BTCUSDT"]["reconciled"] is True
    assert adopted["BTCUSDT"]["entry_price"] == 90000.0
    assert len(report["adopted"]) == 1
    loaded, ok = risk_state.load_state(
        os.path.join(str(tmp_path), "risk_state.json"))
    assert ok is True and loaded["exposure"] == {"BTCUSDT": 500.0}
    assert loaded["adopted"] == {"BTCUSDT": 500.0}  # external baseline kept


def test_boot_never_marks_bot_money_adopted(tmp_path, monkeypatch):
    _boot(monkeypatch, tmp_path, {"BTCUSDT": {"usd": 500.0}, "__ok": True})
    target = os.path.join(str(tmp_path), "risk_state.json")
    st, _ = risk_state.load_state(target)
    # Bot deploys 1000 of its own on a new symbol, then reboots with the
    # broker showing it: ledger money is not external funds.
    risk_state.record_fills(st, {"executed": True, "symbol": "RAAPLUSDT",
                                 "side": "buy", "notional_usdt": 1000.0})
    assert risk_state.save_state(st, target) is True
    # Fills always hit the trade log too: the repair tells bot money from
    # adopted money by subtracting logged net fills from the ledger.
    with open(os.path.join(str(tmp_path), "trades.jsonl"), "a",
              encoding="utf-8") as fh:
        fh.write(json.dumps(
            {"action_taken": {"executed": True, "symbol": "RAAPLUSDT",
                              "side": "buy", "notional_usdt": 1000.0,
                              "fill_value": 1000.0}}) + "\n")
    monkeypatch.setattr(main, "get_positions",
                        lambda **kw: {"RAAPLUSDT": {"usd": 1000.0},
                                      "__ok": True})
    main.OPEN_POSITIONS.clear()
    main.reconcile_startup(False)
    main.OPEN_POSITIONS.clear()
    reloaded, _ = risk_state.load_state(target)
    assert reloaded["exposure"]["RAAPLUSDT"] == 1000.0
    assert reloaded["adopted"].get("RAAPLUSDT", 0.0) == 0.0


def test_second_boot_does_not_double_seed(tmp_path, monkeypatch):
    _boot(monkeypatch, tmp_path, {"BTCUSDT": {"usd": 500.0}, "__ok": True})
    # Re-run with the ledger now holding 500 and broker still 500.
    monkeypatch.setattr(config, "RISK_STATE_FILE",
                        os.path.join(str(tmp_path), "risk_state.json"))
    report2, adopted2 = _boot(monkeypatch, tmp_path,
                              {"BTCUSDT": {"usd": 500.0}, "__ok": True})
    loaded, _ = risk_state.load_state(
        os.path.join(str(tmp_path), "risk_state.json"))
    assert loaded["exposure"] == {"BTCUSDT": 500.0}
    assert adopted2["BTCUSDT"]["size_usd"] == 500.0


def test_dust_balances_ignored(tmp_path, monkeypatch):
    report, adopted = _boot(monkeypatch, tmp_path,
                            {"BTCUSDT": {"usd": 0.5}, "__ok": True})
    assert adopted == {} and report["adopted"] == []


def test_broker_outage_warns_and_continues(tmp_path, monkeypatch):
    report, adopted = _boot(monkeypatch, tmp_path, {})
    assert adopted == {}
    assert any("startup" in w or "snapshot" in w for w in report["warnings"])


def test_resting_orders_reported_not_touched(tmp_path, monkeypatch):
    opens = [{"orderId": "1", "symbol": "BTCUSDT", "side": "sell",
              "qty": "0.01", "orderStatus": "live"}]
    report, _ = _boot(monkeypatch, tmp_path, {"__ok": True}, opens=opens)
    assert len(report["open_orders"]) == 1
    assert any("resting" in w for w in report["warnings"])


def test_corrupt_state_starts_fresh_with_warning(tmp_path, monkeypatch):
    report, _ = _boot(monkeypatch, tmp_path, {"__ok": True}, state_ok=False)
    assert any("fresh ledger" in w for w in report["warnings"])


def _write_legacy_state(tmp_path, exposure):
    target = os.path.join(str(tmp_path), "risk_state.json")
    with open(target, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "exposure": exposure, "peak_pnl": 0.0,
                   "peak_exposure_usd": 500.0, "day": "", "day_start_pnl": 0.0,
                   "broker_fail_streak": 0, "realized_pnl": 0.0,
                   "groq_streak": 0, "groq_cooldown": 0}, fh)
    return target


def test_repair_seeds_baseline_for_legacy_ledger(tmp_path, monkeypatch):
    target = _write_legacy_state(tmp_path, {"BTCUSDT": 500.0})
    _boot(monkeypatch, tmp_path, {"BTCUSDT": {"usd": 500.0}, "__ok": True})
    loaded, ok = risk_state.load_state(target)
    assert ok is True
    assert loaded["adopted"] == {"BTCUSDT": 500.0}
    assert risk_state.bot_exposure(loaded) == {"BTCUSDT": 0.0}


def test_repair_keeps_existing_baseline(tmp_path, monkeypatch):
    target = _write_legacy_state(tmp_path, {"BTCUSDT": 1500.0})
    st, _ = risk_state.load_state(target)
    st["adopted"] = {"BTCUSDT": 500.0}
    assert risk_state.save_state(st, target) is True
    _boot(monkeypatch, tmp_path, {"BTCUSDT": {"usd": 1500.0}, "__ok": True})
    loaded, _ = risk_state.load_state(target)
    assert loaded["adopted"] == {"BTCUSDT": 500.0}
    assert risk_state.bot_exposure(loaded) == {"BTCUSDT": 1000.0}
