"""Triad configuration: env vars, thresholds, symbols.

Secrets come from the environment (or a local .env file, never committed).
No secrets are ever printed or logged.
"""
import os

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Credentials ──────────────────────────────────────────────────
BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "").strip()
BITGET_SECRET_KEY = os.environ.get("BITGET_SECRET_KEY", "").strip()
BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "").strip()

# ── Loop ─────────────────────────────────────────────────────────
HEARTBEAT_INTERVAL = 300  # 5 minutes between calm ticks
MIN_TICK_INTERVAL = 60  # floor between ticks even on divergence wake

# ── Universe ─────────────────────────────────────────────────────
# NOTE: "AAPLOLUSDT" does not exist on Bitget (verified via API).
# The Apple rToken trades as RAAPLUSDT on SPOT.
RTOKEN_SYMBOL = "RAAPLUSDT"  # Apple rToken (default leg)
RTOKEN_BASKET = ["RAAPLUSDT", "RNVDAUSDT", "RTSLAUSDT"]
CRYPTO_SYMBOL = "BTCUSDT"
SPOT_CATEGORY = "SPOT"
FUTURES_CATEGORY = "USDT-FUTURES"

# ── Trading mode ─────────────────────────────────────────────────
# Paper is the default and only path unless ALL three hold:
#   1. main.py --live flag,  2. TRIAD_LIVE_OK=1 in the environment,
#   3. no logs/KILL file. Missing any one -> paper (or refusal, if
#      --live was explicitly requested; see live_trading_enabled).
TRIAD_LIVE_OK = os.environ.get("TRIAD_LIVE_OK", "").strip() == "1"


def live_trading_enabled(cli_live_flag: bool) -> tuple:
    """Resolve whether this run may trade live. Returns (live, reason).

    live is True only when the --live flag, TRIAD_LIVE_OK=1, and no KILL
    file all hold. reason explains a refusal ("" when live). Never raises.
    """
    try:
        if not cli_live_flag:
            return False, ""
        if os.path.exists(KILL_FILE):
            return False, "refusing live: KILL file present"
        if not TRIAD_LIVE_OK:
            return False, ("refusing live: --live given without "
                           "TRIAD_LIVE_OK=1")
        return True, ""
    except Exception as exc:
        return False, f"refusing live: gate error: {exc}"[:160]

# ── Logging ──────────────────────────────────────────────────────
LOG_FILE = "logs/trades.jsonl"
KILL_FILE = os.path.join(BASE_DIR, "logs", "KILL")  # create to halt trading

# ── Risk ─────────────────────────────────────────────────────────
RISK_MAX_POSITION_USD = 1000
RISK_MAX_DRAWDOWN_PCT = 0.05
RISK_MAX_DAILY_LOSS_PCT = 0.02  # halt new trading for the rest of the UTC day
RISK_BROKER_FAIL_TICKS = 3  # consecutive failed broker snapshots -> halt
RISK_STATE_FILE = os.path.join(BASE_DIR, "logs", "risk_state.json")
RISK_MAX_PRICE_DRIFT_PCT = 0.01  # abort if mark moved >1% since the signal

# ── Signals ──────────────────────────────────────────────────────
DIVERGENCE_THRESHOLD = 0.015  # 1.5pp gap between rToken and BTC 24h change

# ── LLM decision engine (Groq) ───────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
# NOTE: llama-3.1-8b-instant was retired by Groq (404). Current pick
# verified against the live /models list.
GROQ_MODEL = "openai/gpt-oss-20b"
GROQ_TIMEOUT_SEC = 10


def has_credentials() -> bool:
    return bool(BITGET_API_KEY and BITGET_SECRET_KEY and BITGET_PASSPHRASE)


def masked_key() -> str:
    if not BITGET_API_KEY or len(BITGET_API_KEY) < 8:
        return "(missing)"
    return BITGET_API_KEY[:6] + "…" + BITGET_API_KEY[-4:]
