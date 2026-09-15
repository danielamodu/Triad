"""Sentiment signal.

Runs the spec'd `bgc bitget-signal --skill sentiment-analyst` command;
if it yields scorable text, keywords decide. Otherwise falls back to a
real positioning proxy computed from bgc derivatives data (funding
rate: positive = crowded longs -> contrarian bearish tilt, and vice
versa), labelled as such in the reason.
"""
import subprocess

import config
from src import cli

BULLISH = ("bullish", "greed", "accumulation", "inflow", "longs crowded",
           "aggressive buying", "risk-on", "optimism")
BEARISH = ("bearish", "fear", "extreme fear", "outflow", "shorts crowded",
           "aggressive selling", "risk-off", "capitulation", "panic")

TIMEOUT = 60


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


def _funding_proxy() -> tuple[str, float, str]:
    """Positioning proxy from USDT-FUTURES funding on the crypto leg."""
    rows = cli.funding_rate(config.FUTURES_CATEGORY, config.CRYPTO_SYMBOL)
    row = next((r for r in rows if isinstance(r, dict)), {})
    fr = None
    for key in ("fundingRate", "rate", "currentRate"):
        if row.get(key) not in (None, ""):
            fr = cli.fnum(row[key])
            break
    if fr is None:
        raise cli.BgcError("no funding rate available")
    # Contrarian read: very positive funding = crowded longs.
    score = max(0.0, min(1.0, 0.5 - fr * 100))
    label = "bullish" if score > 0.55 else "bearish" if score < 0.45 else "neutral"
    return label, round(score, 3), f"funding proxy {fr:+.5f}"


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
    try:
        label, score, reason = _funding_proxy()
        return {"sentiment": label, "score": score, "reason": reason}
    except Exception as exc:
        return {"sentiment": "neutral", "score": 0.5,
                "reason": f"sentiment unavailable: {exc}"[:160]}
