"""Tests for src/risk/state.py: ledger persistence and math."""
import os

import config
from src.risk import state


def test_missing_file_starts_fresh_and_ok(tmp_path):
    target = os.path.join(str(tmp_path), "risk_state.json")
    loaded, ok = state.load_state(target)
    assert ok is True
    assert loaded["exposure"] == {}
    assert loaded["peak_pnl"] == 0.0
    assert loaded["broker_fail_streak"] == 0


def test_corrupt_file_fails_closed(tmp_path):
    target = os.path.join(str(tmp_path), "risk_state.json")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    _, ok = state.load_state(target)
    assert ok is False


def test_wrong_shape_fails_closed(tmp_path):
    target = os.path.join(str(tmp_path), "risk_state.json")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write('{"version": 999, "exposure": {}}')
    _, ok = state.load_state(target)
    assert ok is False


def test_roundtrip_preserves_state(tmp_path):
    target = os.path.join(str(tmp_path), "risk_state.json")
    st, _ = state.load_state(target)
    st["exposure"] = {"RAAPLUSDT": 500.0}
    st["peak_pnl"] = 12.5
    assert state.save_state(st, target) is True
    reloaded, ok = state.load_state(target)
    assert ok is True
    assert reloaded["exposure"] == {"RAAPLUSDT": 500.0}
    assert reloaded["peak_pnl"] == 12.5


def test_record_fills_single_and_multi_leg():
    st = state.fresh_state()
    state.record_fills(st, {"executed": True, "symbol": "RAAPLUSDT",
                            "side": "buy", "notional_usdt": 500.0})
    assert st["exposure"] == {"RAAPLUSDT": 500.0}
    state.record_fills(st, {"executed": True, "details": [
        {"symbol": "RAAPLUSDT", "side": "sell", "notional_usdt": 200.0,
         "executed": True},
        {"symbol": "BTCUSDT", "side": "sell", "notional_usdt": 100.0,
         "executed": True},
    ]})
    assert st["exposure"]["RAAPLUSDT"] == 300.0
    assert st["exposure"]["BTCUSDT"] == -100.0
    assert st["peak_exposure_usd"] == 500.0


def test_record_fills_ignores_unexecuted():
    st = state.fresh_state()
    state.record_fills(st, {"executed": False, "details": "blocked: x"})
    state.record_fills(st, {"executed": True, "details": [
        {"symbol": "RAAPLUSDT", "side": "buy", "notional_usdt": 500.0,
         "executed": False},
    ]})
    assert st["exposure"] == {}
    assert st["peak_exposure_usd"] == 0.0


def test_drawdown_advances_peak_and_measures_drop():
    st = state.fresh_state()
    assert state.drawdown_pct(st, 20.0) == 0.0  # new peak, no drop
    assert st["peak_pnl"] == 20.0
    # base = max(1000, 0) = 1000 -> 50/1000 = 5%
    assert state.drawdown_pct(st, -30.0) == 0.05
    # peak unchanged by a losing print
    assert st["peak_pnl"] == 20.0


def test_drawdown_base_uses_peak_exposure():
    st = state.fresh_state()
    st["peak_exposure_usd"] = 2000.0
    state.drawdown_pct(st, 0.0)  # anchor peak at 0
    assert state.drawdown_pct(st, -100.0) == 0.05  # 100/2000


def test_roll_day_resets_anchor_on_date_change():
    st = state.fresh_state()
    state.roll_day(st, "2026-09-15", 10.0)
    assert st["day"] == "2026-09-15"
    assert st["day_start_pnl"] == 10.0
    assert state.day_loss_pct(st, 5.0) == 0.005  # 5/1000
    state.roll_day(st, "2026-09-15", 99.0)  # same day: anchor kept
    assert st["day_start_pnl"] == 10.0
    state.roll_day(st, "2026-09-16", 5.0)  # new day: re-anchored
    assert state.day_loss_pct(st, 5.0) == 0.0


def test_broker_streak_counts_consecutive_failures():
    st = state.fresh_state()
    assert state.note_broker(st, True) == 0
    assert state.note_broker(st, False) == 1
    assert state.note_broker(st, False) == 2
    assert state.note_broker(st, True) == 0


def test_iter_fills_skips_garbage():
    assert list(state.iter_fills({})) == []
    assert list(state.iter_fills(None)) == []
    assert list(state.iter_fills({"executed": True, "details": "nope"})) == []
    fills = list(state.iter_fills({"executed": True, "symbol": "X",
                                   "side": "buy", "notional_usdt": "bad"}))
    assert fills == []
    assert config.RISK_MAX_POSITION_USD == 1000  # base assumption documented


def test_adopted_baseline_roundtrips(tmp_path):
    target = os.path.join(str(tmp_path), "risk_state.json")
    st, _ = state.load_state(target)
    assert st["adopted"] == {}
    st["exposure"] = {"BTCUSDT": 338328.0}
    st["adopted"] = {"BTCUSDT": 337516.0}
    assert state.save_state(st, target) is True
    reloaded, ok = state.load_state(target)
    assert ok is True
    assert reloaded["adopted"] == {"BTCUSDT": 337516.0}


def test_bot_exposure_nets_out_adopted():
    st = state.fresh_state()
    st["exposure"] = {"BTCUSDT": 338328.0, "RAAPLUSDT": 1000.0}
    st["adopted"] = {"BTCUSDT": 337516.0}
    assert state.bot_exposure(st) == {"BTCUSDT": 812.0, "RAAPLUSDT": 1000.0}
    # Bot sales of adopted funds never go negative.
    st["exposure"] = {"BTCUSDT": 300000.0}
    assert state.bot_exposure(st) == {"BTCUSDT": 0.0}
    # Missing baseline degrades to the full ledger.
    assert state.bot_exposure({"exposure": {"X": 5.0}}) == {"X": 5.0}
    assert state.bot_exposure({}) == {}
