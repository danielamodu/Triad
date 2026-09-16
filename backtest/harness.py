"""Offline backtest: replay candles through the live decision stack.

    python -m backtest.harness [--refresh] [--sweep] [--fee 0.001]
        [--interval 1D] [--split 0.7] [--spread-bps 0] [--slip-bps 0]

What it replays for real (same code as production):
  - price-divergence math (gap, direction, threshold)
  - weighted_decision() scorer (Groq excluded: non-deterministic)
  - confidence sizing buckets (parametrized for the sweep)
  - the safety limits (code name: risk cage, validate) with a real ledger (state.py math):
    no-doubling money in play, drop-from-peak halt, daily-loss halt
  - event detection (expansion_event) and positioning sentiment
    (funding z-score + perp basis) when --with-overlays is given;
    otherwise both stay fixed neutral (legacy behavior, and what the
    pinned unit tests assert)

What it simulates:
  - fills at the bar close; per-side cost = fee + spread + slippage
    (defaults 0.1% fee as observed on a live paper fill, zero
    spread/slip unless passed explicitly)
  - one rToken position at a time; HEDGE_CRYPTO and EXIT both flatten it

Deliberate approximations (read before trusting the numbers):
  - EXIT never fires here (it needs a BEARISH event); live it can.
  - HEDGE is modeled as flattening the rToken leg; live it trims BTC
    while keeping rToken exposure. The replay is the more conservative
    trade (realizes PnL, cuts exposure).
  - the ledger is fed equity PnL (realized + unrealized), matching live
    since the realized-PnL alignment (live used to feed unrealized only,
    understating drawdown after a closed loss).
  - fills at close ignore intraday path (wider bars = wider lie).

Cache: backtest/data/<SYMBOL>_<INT>.json (gitignored). Report: printed +
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
from src.signals.event_signal import expansion_event
from src.signals.sentiment_signal import funding_zscore

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
FEE_PCT = 0.001
START_CASH = 10000.0
BENCH_SIZE = 1000.0  # buy-and-hold deploys the max position, like the bot

NEUTRAL_EVENT = {"signal": "NEUTRAL", "confidence": 0.5}
NEUTRAL_SENTIMENT = {"sentiment": "neutral", "score": 0.5}

INTERVAL_MS = {"1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000,
               "30m": 1800000, "1H": 3600000, "4H": 14400000,
               "6H": 21600000, "12H": 43200000, "1D": 86400000}


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
                try:
                    qvol = float(row.get("quoteVol", row.get("qvol", 0)) or 0)
                except (TypeError, ValueError):
                    qvol = 0.0
            else:
                ts = int(row[0])
                o, h, lo, c = (float(row[1]), float(row[2]),
                               float(row[3]), float(row[4]))
                try:
                    qvol = float(row[6]) if len(row) > 6 else 0.0
                except (TypeError, ValueError):
                    qvol = 0.0
            if ts > 0 and c > 0:
                out.append({"ts": ts, "open": o, "high": h, "low": lo,
                            "close": c, "qvol": qvol})
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


def fetch_candles_range(symbol: str, category: str, interval: str,
                        start_ms: int, end_ms: int) -> list:
    """Page candlesHistory across [start, end). Ascending candle dicts.
    Never raises (short/empty on failure)."""
    out, seen = [], set()
    # NOTE: history-candles serves ~90 bars per request; wider windows
    # come back empty (found 2026-09-16). Page in 80-bar steps.
    step = INTERVAL_MS.get(interval, 86400000) * 80
    cursor = start_ms
    try:
        guard = 0
        while cursor < end_ms and guard < 50:
            guard += 1
            rows = cli.candles_history(category, symbol, interval,
                                       cursor, min(cursor + step, end_ms),
                                       100)
            if not rows:
                cursor += step  # empty window (too old?): skip ahead
                continue
            for row in rows:
                try:
                    ts = int(row[0])
                    if ts in seen:
                        continue
                    seen.add(ts)
                    out.append({"ts": ts, "open": float(row[1]),
                                "high": float(row[2]), "low": float(row[3]),
                                "close": float(row[4]),
                                "qvol": float(row[6]) if len(row) > 6
                                else 0.0})
                except (TypeError, ValueError, IndexError):
                    continue
            try:
                cursor = max(r["ts"] for r in out) + 1
            except ValueError:
                break
            if len(rows) < 2:
                break
    except Exception:
        pass
    return sorted([r for r in out if start_ms <= r["ts"] < end_ms],
                  key=lambda r: r["ts"])


def fetch_funding_series(symbol: str, pages: int = 12) -> list:
    """Historical funding, ascending [(ts_ms, rate)]. Cached to disk
    (refresh with --refresh). Never raises."""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "funding_%s.json" % symbol)
    try:
        with open(path, encoding="utf-8") as fh:
            cached = json.load(fh)
        if isinstance(cached, list) and cached:
            return [(int(ts), float(rate)) for ts, rate in cached]
    except (OSError, ValueError, TypeError):
        pass
    out = []
    try:
        for page in range(1, pages + 1):
            rows = cli.funding_rate_history(config.FUTURES_CATEGORY,
                                            symbol, 100, str(page))
            if not rows:
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    out.append((int(row.get("fundingRateTimestamp", 0)),
                                float(row.get("fundingRate", ""))))
                except (TypeError, ValueError):
                    continue
    except Exception:
        pass
    out = sorted(set(o for o in out if o[0] > 0))
    if not out:
        return []  # never cache a failed fetch (it would pin the hole)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(out, fh)
    except OSError:
        pass
    return out


def _z_to_score(z: float) -> float:
    try:
        return round(max(0.0, min(1.0, 0.5 - 0.5 * max(-1.0, min(1.0, z / 3.0)))), 3)
    except (TypeError, ValueError):
        return 0.5


def build_overlays(r_rows: list, funding: list, fut_rows: list) -> dict:
    """Per-date {event, sentiment} replayed from history.

    date -> {"event": expansion_event over trailing daily candles,
    "sentiment": funding z-score (0.7) + perp basis (0.3)}. Dates with
    thin funding history degrade to neutral components. Pure function
    of its inputs (deterministic).
    """
    closes = {}
    for row in fut_rows or []:
        if isinstance(row, dict) and row.get("ts") and row.get("close"):
            closes[datetime.fromtimestamp(row["ts"] / 1000,
                                          tz=timezone.utc).date().isoformat()] \
                = float(row["close"])
    fund_by_date: dict = {}
    for ts, rate in funding or []:
        day = datetime.fromtimestamp(ts / 1000,
                                     tz=timezone.utc).date().isoformat()
        fund_by_date.setdefault(day, []).append(rate)
    ordered_days = sorted(fund_by_date)
    overlays: dict = {}
    changes = daily_changes(r_rows)
    closes_by_date = {d: p for d, _, p in changes}
    # r_rows index by date for the trailing expansion window
    by_date = {}
    for row in r_rows:
        if isinstance(row, dict) and row.get("ts"):
            by_date[datetime.fromtimestamp(
                row["ts"] / 1000, tz=timezone.utc).date().isoformat()] = row
    sorted_dates = sorted(by_date)
    for i, day in enumerate(sorted_dates):
        window = [by_date[d] for d in sorted_dates[max(0, i - 49):i + 1]]
        # qvol defaults to 0.0 (old caches predate volume): without
        # volume the expansion gate honestly stays shut.
        rows = [[r.get("ts", 0), r.get("open", 0), r.get("high", 0),
                 r.get("low", 0), r.get("close", 0), 0,
                 r.get("qvol", 0.0)] for r in window]
        exp = expansion_event(rows)
        event = {"signal": exp["signal"], "confidence": exp["confidence"]}
        # Funding readings strictly before this date's close.
        trail = []
        for d in ordered_days:
            if d <= day:
                trail.extend(fund_by_date[d])
        trail = trail[-91:]
        if len(trail) >= 11:
            # funding_zscore wants newest-first; trail is oldest-first.
            z, _ = funding_zscore(list(reversed(trail))[:91])
            fund_score = _z_to_score(z)
        else:
            fund_score = 0.5
        basis_score = 0.5
        if day in closes and day in closes_by_date and closes_by_date[day] > 0:
            bps = (closes[day] / closes_by_date[day] - 1) * 10000
            basis_score = round(max(0.0, min(1.0, 0.5 - 0.5 * max(
                -1.0, min(1.0, bps / 10.0)))), 3)
        score = round(0.7 * fund_score + 0.3 * basis_score, 3)
        sentiment = {"sentiment": "bullish" if score > 0.55
                     else "bearish" if score < 0.45 else "neutral",
                     "score": score}
        overlays[day] = {"event": event, "sentiment": sentiment}
    return overlays


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
                 mid_cut: float = 0.6, high_cut: float = 0.8,
                 spread_bps: float = 0.0, slip_bps: float = 0.0,
                 overlays: dict = None) -> dict:
    """Replay aligned days. A day is (date, rtoken_chg, crypto_chg, price).

    Per-side cost = fee_pct + spread + slippage (bps args). overlays
    maps date -> {"event", "sentiment"}; missing dates replay neutral
    (legacy behavior). Returns the full report dict (metrics, buckets,
    trades, equity). Deterministic: same days in, same report out.
    Never raises on data (a crash bug would be a harness bug: let it
    raise).
    """
    ledger = risk_state.fresh_state()
    cash, realized = START_CASH, 0.0
    cost_rate = fee_pct + (spread_bps + slip_bps) / 10000.0
    open_pos = None  # {qty, entry, size, bucket, conf, date}
    trades, equity_curve = [], []
    cage_blocks = {"drawdown": 0, "daily": 0, "exposure": 0, "other": 0}
    peak_equity, max_dd = START_CASH, 0.0
    overlays = overlays or {}

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

        overlay = overlays.get(date) or {}
        event = overlay.get("event", NEUTRAL_EVENT)
        sentiment = overlay.get("sentiment", NEUTRAL_SENTIMENT)
        decision = weighted_decision(price_sig, event, sentiment)
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
            cost = size * cost_rate
            qty = (size - cost) / price
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
            cost = proceeds * cost_rate
            cash += proceeds - cost
            pnl = (proceeds - cost) - open_pos["size"]
            realized += pnl
            trades.append({"entry_date": open_pos["date"], "exit_date": date,
                           "exit": name, "bucket": open_pos["bucket"],
                           "conf": open_pos["conf"], "size": open_pos["size"],
                           "pnl": round(pnl, 2),
                           "fees": round(open_pos["size"] * cost_rate + cost, 2)})
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


def walk_forward(days: list, split: float = 0.7, **kwargs) -> dict:
    """Chronological train/test split. Returns {"train": rep, "test":
    rep}. A strategy that only works in-sample shows it here."""
    cut = max(1, int(len(days) * split))
    return {"split": split,
            "train": run_backtest(days[:cut], **kwargs),
            "test": run_backtest(days[cut:], **kwargs)}


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
    parser.add_argument("--spread-bps", type=float, default=0.0,
                        help="per-side spread in basis points (default 0)")
    parser.add_argument("--slip-bps", type=float, default=0.0,
                        help="per-side slippage in basis points (default 0)")
    parser.add_argument("--split", type=float, default=0.0,
                        help="walk-forward train fraction, e.g. 0.7 "
                             "(default 0 = full-sample replay)")
    parser.add_argument("--with-overlays", action="store_true",
                        help="replay event (expansion) + sentiment (funding "
                             "z + basis) from history instead of neutral")
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
    overlays: dict = {}
    if args.with_overlays:
        funding = fetch_funding_series(config.CRYPTO_SYMBOL)
        fut_rows = load_or_fetch_fut(args.refresh)
        overlays = build_overlays(r_rows, funding, fut_rows)
        used = sum(1 for d, _, _, _ in days if d in overlays)
        print(f"[backtest] overlays: {used}/{len(days)} bars", flush=True)
    kwargs: dict = {"fee_pct": args.fee, "spread_bps": args.spread_bps,
                    "slip_bps": args.slip_bps, "overlays": overlays}
    if args.split > 0:
        wf = walk_forward(days, args.split, **kwargs)
        for name in ("train", "test"):
            _print_report(wf[name], name + " ",
                          fee=args.fee, show_buckets=(name == "train"))
        print(f"[backtest] walk-forward @{args.split}: train "
              f"${wf['train']['total_pnl']:+,.2f} vs test "
              f"${wf['test']['total_pnl']:+,.2f}")
        rep = wf["test"]
    else:
        rep = run_backtest(days, **kwargs)
        _print_report(rep, "", fee=args.fee, show_buckets=True)
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


def load_or_fetch_fut(refresh: bool = False) -> list:
    """Cached BTCUSDT USDT-FUTURES daily candles (for basis replay)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "BTCUSDT_FUT_1D.json")
    if not refresh:
        try:
            with open(path, encoding="utf-8") as fh:
                rows = json.load(fh)
            if isinstance(rows, list) and rows:
                return rows
        except (OSError, ValueError):
            pass
    import time
    now_ms = int(time.time() * 1000)
    rows = fetch_candles_range(config.CRYPTO_SYMBOL,
                               config.FUTURES_CATEGORY, "1D",
                               now_ms - 400 * 86400000, now_ms)
    if not rows:
        return []  # never cache a failed fetch
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
    except OSError:
        pass
    return rows


def _print_report(rep: dict, prefix: str = "", fee: float = FEE_PCT,
                  show_buckets: bool = True) -> None:
    print(f"[backtest] {prefix}{rep['first_day']}..{rep['last_day']} "
          f"({rep['n_days']} days, fee={fee})")
    print(f"  equity ${START_CASH:,.0f} -> ${rep['final_equity']:,.0f} "
          f"({rep['return_pct']:+.2f}%, PnL ${rep['total_pnl']:+,.2f})")
    print(f"  max drawdown {rep['max_drawdown_pct']:.2f}% | "
          f"trades {rep['n_trades']} | win rate {rep['win_rate']:.0%} | "
          f"profit factor {rep['profit_factor']} | fees ${rep['fees_paid']:,.2f}")
    if show_buckets:
        print(f"  buckets: high {rep['buckets']['high']} | "
              f"mid {rep['buckets']['mid']}")
    print(f"  cage blocks: {rep['cage_blocks']}")
    print(f"  buy-and-hold ${BENCH_SIZE:,.0f}: ${rep['benchmark_bh_pnl']:+,.2f}"
          f"{' | position open at end' if rep['open_at_end'] else ''}")


if __name__ == "__main__":
    raise SystemExit(main())
