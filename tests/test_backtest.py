"""Tests for the backtest harness: math, determinism, known scenario."""
from backtest import harness


def test_price_signal_mirrors_live_math():
    sig = harness.build_price_signal(0.04, 0.01, 100.0, 90000.0)
    assert sig["signal"] == "DIVERGENCE_DETECTED"
    assert sig["direction"] == "RTOKEN_OUTPERFORMING"
    assert sig["divergence_score"] == 0.03
    assert sig["selected_rtoken"] == "RAAPLUSDT"
    assert sig["basket_scores"] == {"RAAPLUSDT": 0.03}
    flat = harness.build_price_signal(0.005, 0.0, 100.0, 90000.0)
    assert flat["signal"] == "STABLE" and flat["direction"] == \
        "RTOKEN_OUTPERFORMING"


def test_known_round_trip_pnl():
    days = [("2026-01-02", 0.04, 0.0, 100.0),   # +4pp gap -> LONG $500
            ("2026-01-03", 0.0, 0.0, 110.0),    # flat -> HOLD
            ("2026-01-04", -0.04, 0.0, 120.0)]  # -4pp gap -> HEDGE closes
    rep = harness.run_backtest(days)
    assert rep["n_trades"] == 1
    assert rep["win_rate"] == 1.0
    assert rep["final_equity"] == 10098.8
    assert rep["total_pnl"] == 98.8
    assert rep["trades"][0]["bucket"] == "mid"  # conf exactly 0.8
    assert rep["open_at_end"] is False


def test_deterministic():
    days = [("2026-01-02", 0.04, 0.0, 100.0),
            ("2026-01-03", -0.05, 0.0, 90.0)]
    first = harness.run_backtest(days)
    second = harness.run_backtest(days)
    assert first == second


def test_sweep_returns_all_cutoffs():
    days = [("2026-01-02", 0.04, 0.0, 100.0),
            ("2026-01-03", 0.0, 0.0, 110.0)]
    rows = harness.sweep(days)
    assert [(r["mid_cut"], r["high_cut"]) for r in rows] == \
        [(0.5, 0.7), (0.6, 0.8), (0.7, 0.9)]


def test_align_inner_joins_dates():
    r = [{"ts": 2, "open": 1, "high": 1, "low": 1, "close": 110.0},
         {"ts": 1, "open": 1, "high": 1, "low": 1, "close": 100.0},
         {"ts": 3, "open": 1, "high": 1, "low": 1, "close": 121.0}]
    c = [{"ts": 1, "open": 1, "high": 1, "low": 1, "close": 50.0},
         {"ts": 2, "open": 1, "high": 1, "low": 1, "close": 50.0}]
    aligned = harness._align(r, c)
    assert [d for d, _, _, _ in aligned] == ["1970-01-01"]
    assert aligned[0][1] == 0.1  # (110-100)/100


def test_costs_reduce_pnl():
    days = [("2026-01-02", 0.04, 0.0, 100.0),
            ("2026-01-03", 0.0, 0.0, 110.0),
            ("2026-01-04", -0.04, 0.0, 120.0)]
    base = harness.run_backtest(days)
    costly = harness.run_backtest(days, spread_bps=10.0, slip_bps=10.0)
    assert costly["total_pnl"] < base["total_pnl"]
    assert costly["n_trades"] == base["n_trades"] == 1


def test_overlays_can_move_votes():
    days = [("2026-01-02", 0.04, 0.0, 100.0),   # price vote +0.4 -> LONG
            ("2026-01-03", 0.0, 0.0, 110.0),
            ("2026-01-04", -0.04, 0.0, 120.0)]  # -4pp gap -> HEDGE closes
    base = harness.run_backtest(days)
    overlays = {"2026-01-02": {
        "event": {"signal": "BEARISH", "confidence": 0.9},
        "sentiment": {"sentiment": "bearish", "score": 0.0}}}
    # price +0.4*0.5=+0.2, event -0.8*0.3=-0.24, sent -1.0*0.2=-0.2
    # final -0.24 -> HOLD: the overlays flip the trade away.
    changed = harness.run_backtest(days, overlays=overlays)
    assert base["n_trades"] == 1 and changed["n_trades"] == 0


def test_walk_forward_splits():
    days = [("2026-01-0%d" % i, 0.04 if i % 2 else -0.04, 0.0, 100.0)
            for i in range(2, 8)]
    wf = harness.walk_forward(days, 0.5)
    assert set(wf) == {"split", "train", "test"}
    assert wf["train"]["n_days"] == 3 and wf["test"]["n_days"] == 3
