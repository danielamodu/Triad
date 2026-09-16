"""Sentiment (positioning) signal: contrarian read on crowded futures.

Composite of two backtestable components, both bearish-tilted when
longs crowd (positive funding, positive perp-over-spot basis):

  funding z-score (0.7): current 8h funding vs its trailing-90-reading
      (~30d) mean/std. z >= +3 (extremely crowded longs) scores ~0.0,
      z <= -3 scores ~1.0. Calibrated from history, not fixed bounds.
  perp basis (0.3): USDT-FUTURES last vs SPOT last on the crypto leg.
      +10bps or more = crowded longs (bearish tilt); symmetric short
      side. Bounded heuristic, labelled in the reason.

Falls back through: skill keyword scan (if it ever yields text) ->
composite -> funding-only -> neutral. Never raises.
"""
import subprocess

import config
from src import cli

BULLISH = ("bullish", "greed", "accumulation", "inflow", "longs crowded",
           "aggressive buying", "risk-on", "optimism")
BEARISH = ("bearish", "fear", "extreme fear", "outflow", "shorts crowded",
           "aggressive selling", "risk-off", "capitulation", "panic")

TIMEOUT = 60

FUNDING_TRAIL = 90  # readings (~30d at 8h)
FUNDING_WEIGHT = 0.7
BASIS_WEIGHT = 0.3
BASIS_FULL_BPS = 10.0  # +/-bps that saturates the basis component


def _fetch_raw() -> str:
    import shutil
    bgc = shutil.which("bgc") or "bgc"
    proc = subprocess.run(
        [bgc, "bitget-signal", "--skill", "sentiment-analyst"],
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def _keyword_score(text: str) -> tuple[str, float] | None:
    low = text.lower()
    bull = sum(low.count(k) for k in BULLISH)
    bear = sum(low.count(k) for k in BEARISH)
    if bull == 0 and bear == 0:
        return None
    score = 0.5 + 0.5 * (bull - bear) / (bull + bear)
    label = "bullish" if score > 0.55 else "bearish" if score < 0.45 else "neutral"
    return label, round(max(0.0, min(1.0, score)), 3)


def funding_zscore(readings: list) -> tuple:
    """Pure z-score math over funding-rate floats, newest first.

    Returns (z, n). z==0.0 when history is too thin or flat. The
    caller maps z -> score (contrarian). Never raises.
    """
    try:
        vals = [float(v) for v in (readings or [])]
        vals = [v for v in vals if v == v]  # drop NaN
        if len(vals) < 10:
            return 0.0, len(vals)
        cur, trail = vals[0], vals[1:]
        mean = sum(trail) / len(trail)
        var = sum((v - mean) ** 2 for v in trail) / len(trail)
        std = var ** 0.5
        if std < 1e-9:
            return 0.0, len(vals)
        return round((cur - mean) / std, 3), len(vals)
    except (TypeError, ValueError):
        return 0.0, 0


def _z_to_score(z: float) -> float:
    """Contrarian map: crowded longs (z>0) -> bearish (<0.5)."""
    try:
        return round(max(0.0, min(1.0, 0.5 - 0.5 * max(-1.0, min(1.0, z / 3.0)))), 3)
    except (TypeError, ValueError):
        return 0.5


def _funding_component() -> tuple:
    """(score, detail). Raises BgcError when history is unusable."""
    rows = cli.funding_rate_history(config.FUTURES_CATEGORY,
                                    config.CRYPTO_SYMBOL, FUNDING_TRAIL + 1)
    vals = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            vals.append(float(row.get("fundingRate", "")))
        except (TypeError, ValueError):
            continue
    z, n = funding_zscore(vals)
    if n < 10:
        raise cli.BgcError("thin funding history")
    return _z_to_score(z), f"funding z {z:+.2f} ({n} obs)"


def _last_price(category: str, symbol: str) -> float:
    rows = cli.tickers(category, symbol)
    row = next((r for r in rows if isinstance(r, dict)
                and str(r.get("symbol", "")).upper() == symbol.upper()),
               rows[0] if rows else {})
    return cli.fnum(row.get("lastPrice", row.get("last", 0)))


def _basis_component() -> tuple:
    """(score, detail). Perp-over-spot in bps, contrarian. Raises
    BgcError when either mark is unreadable."""
    spot = _last_price(config.SPOT_CATEGORY, config.CRYPTO_SYMBOL)
    perp = _last_price(config.FUTURES_CATEGORY, config.CRYPTO_SYMBOL)
    if spot <= 0 or perp <= 0:
        raise cli.BgcError("no perp/spot marks")
    bps = (perp / spot - 1) * 10000
    score = round(max(0.0, min(1.0,
                               0.5 - 0.5 * max(-1.0, min(1.0, bps / BASIS_FULL_BPS)))), 3)
    return score, f"basis {bps:+.1f}bps"


def _label(score: float) -> str:
    return "bullish" if score > 0.55 else "bearish" if score < 0.45 else "neutral"


def get_sentiment() -> dict:
    """Return {sentiment, score} with score in 0.0..1.0."""
    try:
        text = _fetch_raw()
        if text.strip():
            hit = _keyword_score(text)
            if hit:
                label, score = hit
                return {"sentiment": label, "score": score,
                        "reason": "skill keyword scan"}
    except Exception:
        pass
    fund_score, fund_detail, basis_score, basis_detail = None, "", None, ""
    try:
        fund_score, fund_detail = _funding_component()
    except Exception:
        pass
    try:
        basis_score, basis_detail = _basis_component()
    except Exception:
        pass
    if fund_score is not None and basis_score is not None:
        score = round(FUNDING_WEIGHT * fund_score + BASIS_WEIGHT * basis_score, 3)
        return {"sentiment": _label(score), "score": score,
                "reason": fund_detail + " + " + basis_detail}
    if fund_score is not None:
        return {"sentiment": _label(fund_score), "score": fund_score,
                "reason": fund_detail + " (no basis)"}
    return {"sentiment": "neutral", "score": 0.5,
            "reason": "positioning unavailable"}
