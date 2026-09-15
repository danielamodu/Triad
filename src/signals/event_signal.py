"""Event (news) signal.

Runs the spec'd `bgc bitget-signal --skill news-briefing` command and
scans its output for bullish/bearish keywords. There is currently no
news feed exposed through the bgc CLI (verified: no such tool, and the
news skills are LLM playbooks over an authenticated MCP feed), so an
empty/failed call degrades to NEUTRAL rather than blocking the loop.
"""
import subprocess

BULLISH = ("bullish", "surge", "rally", "breakout", "etf approval",
           "rate cut", "adoption", "all-time high", "record inflow",
           "upgrade", "bull run", "accumulat")
BEARISH = ("bearish", "crash", "plunge", "hack", "exploit", "lawsuit",
           "ban", "crackdown", "liquidation", "sell-off", "selloff",
           "fud", "downgrade", "bankrupt")

TIMEOUT = 60


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


def get_event() -> dict:
    """Return {signal, confidence}; signal is BULLISH | BEARISH | NEUTRAL."""
    try:
        text = _fetch_raw()
    except Exception as exc:
        return {"signal": "NEUTRAL", "confidence": 0.5,
                "reason": f"news feed unavailable: {exc}"[:160]}
    if not text.strip():
        return {"signal": "NEUTRAL", "confidence": 0.5,
                "reason": "empty news output"}
    signal, confidence = _classify(text)
    return {"signal": signal, "confidence": confidence,
            "reason": f"keyword scan over {len(text)} chars"}
