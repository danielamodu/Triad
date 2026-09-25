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


def test_run_backtest_labels_its_symbol():
    days = [("2026-01-02", 0.04, 0.0, 100.0),
            ("2026-01-04", -0.04, 0.0, 120.0)]
    rep = harness.run_backtest(days, symbol="RNVDAUSDT")
    assert rep["symbol"] == "RNVDAUSDT"  # ledger + report label the leg
    sig = harness.build_price_signal(0.04, 0.01, 100.0, 90000.0, "RTSLAUSDT")
    assert sig["selected_rtoken"] == "RTSLAUSDT"
    assert sig["basket_scores"] == {"RTSLAUSDT": 0.03}


def test_pool_reports_aggregates_math():
    # Two independent legs, one winner one loser, pooled by hand-built
    # reports so the aggregation math is pinned without a full replay.
    rep_a = {"symbol": "A", "n_days": 3, "n_trades": 1, "win_rate": 1.0,
             "total_pnl": 100.0, "final_equity": 10100.0,
             "profit_factor": float("inf"), "max_drawdown_pct": 0.0,
             "benchmark_bh_pnl": 10.0,
             "cage_blocks": {"drawdown": 1, "daily": 0, "exposure": 0,
                             "other": 0},
             "trades": [{"pnl": 100.0, "bucket": "high", "fees": 1.0}]}
    rep_b = {"symbol": "B", "n_days": 3, "n_trades": 1, "win_rate": 0.0,
             "total_pnl": -40.0, "final_equity": 9960.0,
             "profit_factor": 0.0, "max_drawdown_pct": 0.4,
             "benchmark_bh_pnl": -5.0,
             "cage_blocks": {"drawdown": 0, "daily": 2, "exposure": 0,
                             "other": 0},
             "trades": [{"pnl": -40.0, "bucket": "mid", "fees": 1.0}]}
    pooled = harness.pool_reports([rep_a, rep_b])
    assert pooled["n_trades"] == 2 and pooled["win_rate"] == 0.5
    assert pooled["profit_factor"] == 2.5  # 100 / 40
    assert pooled["trade_pnl"] == 60.0 and pooled["total_pnl"] == 60.0
    assert pooled["start_cash"] == 20000.0
    assert pooled["final_equity"] == 20060.0
    assert pooled["fees_paid"] == 2.0
    assert pooled["buckets"]["high"]["trades"] == 1
    assert pooled["buckets"]["mid"]["pnl"] == -40.0
    assert pooled["cage_blocks"] == {"drawdown": 1, "daily": 2,
                                     "exposure": 0, "other": 0}
    assert pooled["benchmark_bh_pnl"] == 5.0
    assert [t["symbol"] for t in pooled["trades"]] == ["A", "B"]


def test_run_basket_pools_two_legs():
    winner = [("2026-01-02", 0.04, 0.0, 100.0),
              ("2026-01-03", 0.0, 0.0, 110.0),
              ("2026-01-04", -0.04, 0.0, 120.0)]   # +98.80 like the single
    loser = [("2026-01-02", 0.04, 0.0, 100.0),
             ("2026-01-03", 0.0, 0.0, 95.0),
             ("2026-01-04", -0.04, 0.0, 90.0)]     # closes below entry
    rep = harness.run_basket({"RAAPLUSDT": winner, "RNVDAUSDT": loser})
    assert rep["pooled"] is True and rep["n_symbols"] == 2
    assert rep["symbols"] == ["RAAPLUSDT", "RNVDAUSDT"]
    assert rep["n_trades"] == 2 and rep["win_rate"] == 0.5
    assert {t["symbol"] for t in rep["trades"]} == {"RAAPLUSDT", "RNVDAUSDT"}
    assert rep["start_cash"] == 20000.0
    # portfolio PnL is the sum of the legs' equity deltas (no shared cash)
    assert rep["total_pnl"] == round(
        sum(s["total_pnl"] for s in rep["per_symbol"]), 2)


def test_run_basket_skips_empty_legs():
    winner = [("2026-01-02", 0.04, 0.0, 100.0),
              ("2026-01-04", -0.04, 0.0, 120.0)]
    rep = harness.run_basket({"RAAPLUSDT": winner, "RNVDAUSDT": []})
    assert rep["n_symbols"] == 1 and rep["symbols"] == ["RAAPLUSDT"]
    assert rep["n_trades"] == 1


def test_walk_forward_basket_pools_out_of_sample():
    days = [("2026-01-0%d" % i, 0.04 if i % 2 else -0.04, 0.0, 100.0)
            for i in range(2, 8)]
    wf = harness.walk_forward_basket(
        {"RAAPLUSDT": days, "RNVDAUSDT": days}, 0.5)
    assert set(wf) == {"split", "train", "test"}
    assert wf["train"]["pooled"] is True and wf["test"]["pooled"] is True
    assert wf["train"]["n_symbols"] == 2 and wf["test"]["n_symbols"] == 2
