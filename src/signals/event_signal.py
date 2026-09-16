"""Event signal: abnormal market activity detector.

Primary: volume/range expansion on the crypto leg's 1H candles. When the
latest closed hour's range AND quote volume both exceed 1.5x their
trailing-48h medians, something real happened (liquidations, news,
flows) — direction comes from the hour's return sign, conviction from
the expansion multiples. Fully backtestable: same math replays on any
candle series via expansion_event().

Secondary: the spec'd `bgc bitget-signal --skill news-briefing` keyword
scan. There is no news feed behind it (verified: no such tool), so it
only ever overrides when it yields a non-neutral read; otherwise the
expansion detector decides. Empty everything -> NEUTRAL.
"""
import subprocess

import config
from src import cli

BULLISH = ("bullish", "surge", "rally", "breakout", "etf approval",
           "rate cut", "adoption", "all-time high", "record inflow",
           "upgrade", "bull run", "accumulat")
BEARISH = ("bearish", "crash", "plunge", "hack", "exploit", "lawsuit",
           "ban", "crackdown", "liquidation", "sell-off", "selloff",
           "fud", "downgrade", "bankrupt")

TIMEOUT = 60

# Expansion gate: both multiples must clear it before an event fires.
EXPANSION_MIN_MULT = 1.5
BASELINE_HOURS = 48


def _fetch_raw() -> str:
    """Run the news-briefing skill command; return stdout (may be empty)."""
    import shutil
    bgc = shutil.which("bgc") or "bgc"
    proc = subprocess.run(
        [bgc, "bitget-signal", "--skill", "news-briefing"],
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def _classify(text: str) -> tuple[str, float]:
    low = text.lower()
    bull = sum(low.count(k) for k in BULLISH)
    bear = sum(low.count(k) for k in BEARISH)
    total = bull + bear
    if total == 0:
        return "NEUTRAL", 0.5
    if bull > bear:
        return "BULLISH", round(0.5 + 0.5 * (bull - bear) / total, 3)
    if bear > bull:
        return "BEARISH", round(0.5 - 0.5 * (bear - bull) / total, 3)
    return "NEUTRAL", 0.5


def _hour_stats(row) -> tuple:
    """(range_pct, quote_vol, ret) for a candle row. Raises on garbage."""
    ts, o, h, lo, c = int(row[0]), float(row[1]), float(
        row[2]), float(row[3]), float(row[4])
    qvol = float(row[6]) if len(row) > 6 else 0.0
    if o <= 0:
        raise ValueError("bad open")
    return (h - lo) / o, qvol, (c - o) / o


def _median(values: list) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def expansion_event(rows: list) -> dict:
    """Pure expansion math over candle rows (newest last, current
    forming row optional — dropped when its volume trails the median,
    i.e. it is still printing).

    Returns {signal, confidence, range_mult, vol_mult, reason}.
    Never raises.
    """
    try:
        parsed = []
        for row in rows or []:
            try:
                parsed.append(_hour_stats(row))
            except (TypeError, ValueError, IndexError):
                continue
        if len(parsed) < BASELINE_HOURS + 2:
            return {"signal": "NEUTRAL", "confidence": 0.5,
                    "range_mult": 0.0, "vol_mult": 0.0,
                    "reason": "insufficient candle history"}
        # A still-forming hour has near-zero volume vs its peers.
        if parsed[-1][1] < _median([p[1] for p in parsed[:-1]]) * 0.5:
            parsed = parsed[:-1]
        baseline, latest = parsed[:-1], parsed[-1]
        base_range = _median([p[0] for p in baseline]) or 1e-9
        base_vol = _median([p[1] for p in baseline]) or 1e-9
        range_mult = round(latest[0] / base_range, 3)
        vol_mult = round(latest[1] / base_vol, 3)
        if range_mult < EXPANSION_MIN_MULT or vol_mult < EXPANSION_MIN_MULT:
            return {"signal": "NEUTRAL", "confidence": 0.5,
                    "range_mult": range_mult, "vol_mult": vol_mult,
                    "reason": (f"no expansion ({range_mult}x range, "
                               f"{vol_mult}x vol)")}
        excess = (range_mult - 1) + (vol_mult - 1)
        confidence = round(min(0.9, 0.5 + excess * 0.2), 3)
        signal = "BULLISH" if latest[2] >= 0 else "BEARISH"
        return {"signal": signal, "confidence": confidence,
                "range_mult": range_mult, "vol_mult": vol_mult,
                "reason": (f"expansion {range_mult}x range, {vol_mult}x "
                           f"vol, hour {latest[2]:+.2%}")}
    except Exception as exc:
        return {"signal": "NEUTRAL", "confidence": 0.5,
                "range_mult": 0.0, "vol_mult": 0.0,
                "reason": f"expansion error: {exc}"[:160]}


def get_event() -> dict:
    """Return {signal, confidence, ...}; BULLISH | BEARISH | NEUTRAL."""
    try:
        text = _fetch_raw()
        if text.strip():
            signal, confidence = _classify(text)
            if signal != "NEUTRAL":
                return {"signal": signal, "confidence": confidence,
                        "reason": f"keyword scan over {len(text)} chars"}
    except Exception:
        pass
    try:
        rows = cli.candles(config.SPOT_CATEGORY, config.CRYPTO_SYMBOL,
                           "1H", BASELINE_HOURS + 2)
        return expansion_event(rows)
    except Exception as exc:
        return {"signal": "NEUTRAL", "confidence": 0.5,
                "reason": f"event unavailable: {exc}"[:160]}
