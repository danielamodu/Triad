"""Tests for boot-time resume-from-broker reconciliation."""
import os

import config
import main
from src.risk import state as risk_state


def _boot(monkeypatch, tmp_path, snapshot, opens=None, state_ok=True):
    monkeypatch.setattr(config, "RISK_STATE_FILE",
                        os.path.join(str(tmp_path), "risk_state.json"))
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
