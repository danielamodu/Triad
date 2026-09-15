"""Offline backtest: replay daily candles through the live decision stack.

    python -m backtest.harness [--refresh] [--sweep] [--fee 0.001]

What it replays for real (same code as production):
  - price-divergence math (gap, direction, threshold)
  - weighted_decision() scorer (Groq excluded: non-deterministic)
  - confidence sizing buckets (parametrized for the sweep)
  - the risk cage (validate) with a real ledger (state.py math):
    no-doubling exposure, drawdown halt, daily-loss halt

What it simulates:
  - fills at the daily close, fee_pct per side (default 0.1%, as observed
    on a live paper fill: 0.9997 USDT on ~1000 USDT)
  - one rToken position at a time; HEDGE_CRYPTO and EXIT both flatten it

Deliberate approximations (read before trusting the numbers):
  - event is fixed NEUTRAL and sentiment fixed neutral: the replay
    isolates the price edge. Live, those inputs move votes.
  - EXIT never fires here (it needs a BEARISH event); live it can.
  - HEDGE is modeled as flattening the rToken leg; live it trims BTC
    while keeping rToken exposure. The replay is the more conservative
    trade (realizes PnL, cuts exposure).
  - the ledger is fed equity PnL (realized + unrealized), matching live
    since the realized-PnL alignment (live used to feed unrealized only,
    understating drawdown after a closed loss).
  - fills at close ignore intraday slippage and spread.

Cache: backtest/data/<SYMBOL>_1D.json (gitignored). Report: printed +
backtest/data/report.json. Stdlib only.
"""
import argparse
import json
import os
from datetime import datetime, timezone

import config
from src import cli
from src.decision.engine import weighted_decision
from src.risk import state as risk_state
from src.risk.cage import validate

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
FEE_PCT = 0.001
START_CASH = 10000.0
BENCH_SIZE = 1000.0  # buy-and-hold deploys the max position, like the bot

NEUTRAL_EVENT = {"signal": "NEUTRAL", "confidence": 0.5}
NEUTRAL_SENTIMENT = {"sentiment": "neutral", "score": 0.5}


def fetch_candles(symbol: str, interval: str = "1D",
                  limit: int = 100) -> list:
    """Daily candles, ascending: [{ts, open, high, low, close}]."""
    rows = cli.candles(config.SPOT_CATEGORY, symbol, interval, limit)
    out = []
    for row in rows:
        try:
            if isinstance(row, dict):
                ts = int(row.get("ts", row.get("time", row.get("t", 0))))
                o, h, lo, c = (float(row.get("open", 0)),
                               float(row.get("high", 0)),
                               float(row.get("low", 0)),
                               float(row.get("close", 0)))
            else:
                ts = int(row[0])
                o, h, lo, c = (float(row[1]), float(row[2]),
                               float(row[3]), float(row[4]))
            if ts > 0 and c > 0:
                out.append({"ts": ts, "open": o, "high": h, "low": lo,
                            "close": c})
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    out.sort(key=lambda r: r["ts"])
    return out


def load_or_fetch(symbol: str, refresh: bool = False) -> list:
    """Cached candles (refetch with refresh=True). Never returns None."""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{symbol}_1D.json")
    if not refresh:
        try:
            with open(path, encoding="utf-8") as fh:
                rows = json.load(fh)
            if isinstance(rows, list) and rows:
                return rows
        except (OSError, ValueError):
            pass
    rows = fetch_candles(symbol)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
    except OSError:
        pass
    return rows


def daily_changes(candles: list) -> list:
    """[(date, change)] of close-to-close returns, ascending."""
    ordered = sorted((c for c in candles if isinstance(c, dict)),
                     key=lambda r: r.get("ts", 0))
    out = []
    for prev, cur in zip(ordered, ordered[1:]):
        try:
            chg = (cur["close"] - prev["close"]) / prev["close"]
        except (KeyError, TypeError, ZeroDivisionError):
            continue
        date = datetime.fromtimestamp(cur["ts"] / 1000,
                                      tz=timezone.utc).date().isoformat()
        out.append((date, chg, cur["close"]))
    return out


def build_price_signal(rtoken_chg: float, crypto_chg: float,
                       rtoken_last: float, crypto_last: float) -> dict:
    """Same math as price_divergence.get_divergence for one pair."""
    gap = rtoken_chg - crypto_chg
    detected = abs(gap) >= config.DIVERGENCE_THRESHOLD
    direction = ("RTOKEN_OUTPERFORMING" if gap > 0
                 else "CRYPTO_OUTPERFORMING" if gap < 0 else "FLAT")
    return {"signal": "DIVERGENCE_DETECTED" if detected else "STABLE",
            "direction": direction, "divergence_score": round(gap, 6),
            "rtoken_change": round(rtoken_chg, 6),
            "crypto_change": round(crypto_chg, 6),
            "rtoken_last": str(rtoken_last), "crypto_last": str(crypto_last),
            "selected_rtoken": config.RTOKEN_SYMBOL,
            "basket_scores": {config.RTOKEN_SYMBOL: round(gap, 6)}}


def _size_for(confidence: float, mid_cut: float, high_cut: float) -> float:
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        return 0.0
    if conf > high_cut:
        return 1000.0
    if conf >= mid_cut:
        return 500.0
    return 0.0


def run_backtest(days: list, fee_pct: float = FEE_PCT,
                 mid_cut: float = 0.6, high_cut: float = 0.8) -> dict:
    """Replay aligned days. A day is (date, rtoken_chg, crypto_chg, price).

    Returns the full report dict (metrics, buckets, trades, equity).
    Deterministic: same days in, same report out. Never raises on data
    (a crash bug would be a harness bug: let it raise).
    """
    ledger = risk_state.fresh_state()
    cash, realized = START_CASH, 0.0
    open_pos = None  # {qty, entry, size, bucket, conf, date}
    trades, equity_curve = [], []
    cage_blocks = {"drawdown": 0, "daily": 0, "exposure": 0, "other": 0}
    peak_equity, max_dd = START_CASH, 0.0

    for date, r_chg, c_chg, price in days:
        price_sig = build_price_signal(r_chg, c_chg, price, 0.0)
        unrealized = (open_pos["qty"] * price - open_pos["size"]
                      if open_pos else 0.0)
        total_pnl = realized + unrealized
        risk_state.roll_day(ledger, date, total_pnl)
        dd = risk_state.drawdown_pct(ledger, total_pnl)
        dl = risk_state.day_loss_pct(ledger, total_pnl)
        ctx = {"drawdown_pct": dd, "day_loss_pct": dl,
               "daily_halted": dl > config.RISK_MAX_DAILY_LOSS_PCT,
               "exposure": dict(ledger["exposure"]), "corrupt": False,
               "broker_dead": False, "broker_streak": 0}

        decision = weighted_decision(price_sig, NEUTRAL_EVENT,
                                     NEUTRAL_SENTIMENT)
        name = decision["decision"]
        conf = decision["confidence"]
        risk = validate(decision, {}, ctx, config.RTOKEN_SYMBOL)
        if not risk["approved"]:
            reason = risk.get("blocked_reason", "")
            if reason.startswith("drawdown"):
                cage_blocks["drawdown"] += 1
            elif reason.startswith("daily loss"):
                cage_blocks["daily"] += 1
            elif "no doubling" in reason or "exceed" in reason:
                cage_blocks["exposure"] += 1
            else:
                cage_blocks["other"] += 1
            name = "HOLD"

        size = 0.0 if name == "HOLD" else _size_for(conf, mid_cut, high_cut)
        if name == "LONG_RTOKEN" and size > 0 and open_pos is None \
                and cash >= size:
            fee = size * fee_pct
            qty = (size - fee) / price
            open_pos = {"qty": qty, "entry": price, "size": size,
                        "bucket": "high" if conf > high_cut else "mid",
                        "conf": conf, "date": date}
            cash -= size
            risk_state.record_fills(
                ledger, {"executed": True,
                         "details": {"symbol": config.RTOKEN_SYMBOL,
                                     "side": "buy", "notional_usdt": size,
                                     "executed": True}})
        elif name in ("HEDGE_CRYPTO", "EXIT") and open_pos is not None:
            proceeds = open_pos["qty"] * price
            fee = proceeds * fee_pct
            cash += proceeds - fee
            pnl = (proceeds - fee) - open_pos["size"]
            realized += pnl
            trades.append({"entry_date": open_pos["date"], "exit_date": date,
                           "exit": name, "bucket": open_pos["bucket"],
                           "conf": open_pos["conf"], "size": open_pos["size"],
                           "pnl": round(pnl, 2),
                           "fees": round(open_pos["size"] * fee_pct + fee, 2)})
            risk_state.record_fills(
                ledger, {"executed": True,
                         "details": {"symbol": config.RTOKEN_SYMBOL,
                                     "side": "sell",
                                     "notional_usdt": open_pos["size"],
                                     "executed": True}})
            open_pos = None

        equity = cash + (open_pos["qty"] * price if open_pos else 0.0)
        peak_equity = max(peak_equity, equity)
        max_dd = max(max_dd, (peak_equity - equity) / peak_equity)
        equity_curve.append({"date": date, "equity": round(equity, 2)})

    if open_pos is not None:  # mark leftover open at the last close
        equity = cash + open_pos["qty"] * days[-1][3]
    else:
        equity = cash
    wins = [t for t in trades if t["pnl"] > 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    buckets = {}
    for bucket in ("high", "mid"):
        bt = [t for t in trades if t["bucket"] == bucket]
        bw = [t for t in bt if t["pnl"] > 0]
        buckets[bucket] = {"trades": len(bt),
                           "win_rate": round(len(bw) / len(bt), 4) if bt else 0.0,
                           "pnl": round(sum(t["pnl"] for t in bt), 2)}
    first, last = days[0][3], days[-1][3]
    bench = BENCH_SIZE * (last / first - 1) if first > 0 else 0.0
    return {"n_days": len(days), "first_day": days[0][0],
            "last_day": days[-1][0], "fee_pct": fee_pct,
            "mid_cut": mid_cut, "high_cut": high_cut,
            "final_equity": round(equity, 2),
            "total_pnl": round(equity - START_CASH, 2),
            "return_pct": round((equity - START_CASH) / START_CASH * 100, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "n_trades": len(trades),
            "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2)
            if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
            "fees_paid": round(sum(t["fees"] for t in trades), 2),
            "buckets": buckets, "cage_blocks": cage_blocks,
            "benchmark_bh_pnl": round(bench, 2),
            "open_at_end": open_pos is not None,
            "trades": trades, "equity": equity_curve}


def sweep(days: list, fee_pct: float = FEE_PCT) -> list:
    """Re-run the replay under alternate sizing cutoffs."""
    out = []
    for mid_cut, high_cut in ((0.5, 0.7), (0.6, 0.8), (0.7, 0.9)):
        rep = run_backtest(days, fee_pct, mid_cut, high_cut)
        out.append({"mid_cut": mid_cut, "high_cut": high_cut,
                    "n_trades": rep["n_trades"],
                    "win_rate": rep["win_rate"],
                    "total_pnl": rep["total_pnl"],
                    "return_pct": rep["return_pct"],
                    "max_drawdown_pct": rep["max_drawdown_pct"],
                    "profit_factor": rep["profit_factor"]})
    return out


def _align(r_rows: list, c_rows: list) -> list:
    """Inner-join daily changes on date -> (date, r_chg, c_chg, r_close)."""
    r_map = {d: (c, p) for d, c, p in daily_changes(r_rows)}
    c_map = {d: c for d, c, _ in daily_changes(c_rows)}
    return [(d, r_map[d][0], c_map[d], r_map[d][1])
            for d in sorted(set(r_map) & set(c_map))]


def main() -> int:
    parser = argparse.ArgumentParser(description="Triad offline backtest")
    parser.add_argument("--refresh", action="store_true",
                        help="refetch candles instead of using the cache")
    parser.add_argument("--sweep", action="store_true",
                        help="also run alternate sizing cutoffs")
    parser.add_argument("--fee", type=float, default=FEE_PCT,
                        help="per-side fee fraction (default 0.001)")
    args = parser.parse_args()

    r_rows = load_or_fetch(config.RTOKEN_SYMBOL, args.refresh)
    c_rows = load_or_fetch(config.CRYPTO_SYMBOL, args.refresh)
    if not r_rows or not c_rows:
        print("[backtest] FATAL: no candle data (network down?)", flush=True)
        return 2
    days = _align(r_rows, c_rows)
    if not days:
        print("[backtest] FATAL: no overlapping dates", flush=True)
        return 2
    rep = run_backtest(days, args.fee)
    print(f"[backtest] {rep['first_day']}..{rep['last_day']} "
          f"({rep['n_days']} days, fee={args.fee})")
    print(f"  equity ${START_CASH:,.0f} -> ${rep['final_equity']:,.0f} "
          f"({rep['return_pct']:+.2f}%, PnL ${rep['total_pnl']:+,.2f})")
    print(f"  max drawdown {rep['max_drawdown_pct']:.2f}% | "
          f"trades {rep['n_trades']} | win rate {rep['win_rate']:.0%} | "
          f"profit factor {rep['profit_factor']} | fees ${rep['fees_paid']:,.2f}")
    print(f"  buckets: high {rep['buckets']['high']} | "
          f"mid {rep['buckets']['mid']}")
    print(f"  cage blocks: {rep['cage_blocks']}")
    print(f"  buy-and-hold ${BENCH_SIZE:,.0f}: ${rep['benchmark_bh_pnl']:+,.2f}"
          f"{' | position open at end' if rep['open_at_end'] else ''}")
    if args.sweep:
        print("  sweep (mid_cut, high_cut):")
        for row in sweep(days, args.fee):
            print(f"    ({row['mid_cut']}, {row['high_cut']}): "
                  f"n={row['n_trades']} win={row['win_rate']:.0%} "
                  f"pnl=${row['total_pnl']:+,.2f} "
                  f"dd={row['max_drawdown_pct']:.2f}% pf={row['profit_factor']}")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "report.json"), "w",
              encoding="utf-8") as fh:
        json.dump(rep, fh, indent=2)
    print(f"[backtest] report written to backtest/data/report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
