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

# ── Loop (dashboard: next decision countdown) ──────────────────
HEARTBEAT_INTERVAL = 300  # 5 minutes between calm ticks
MIN_TICK_INTERVAL = 60  # floor between ticks even on an event wake
# Event wake: a tick fires early on any of three reasons (architecture:
# three wake-ups, not one): price divergence, a high-conviction event
# signal, or extreme sentiment positioning.
EVENT_WAKE_CONFIDENCE = 0.75  # event conviction |conf-0.5|*2 that wakes
SENTIMENT_WAKE_LO = 0.15  # sentiment score at/below this wakes (extreme fear)
SENTIMENT_WAKE_HI = 0.85  # sentiment score at/above this wakes (extreme greed)

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

# ── Safety limits (dashboard: Safety limits; code name: risk) ──
RISK_MAX_POSITION_USD = 1000  # max single bet (money in play per asset)
RISK_MAX_DRAWDOWN_PCT = 0.05
RISK_MAX_DAILY_LOSS_PCT = 0.02  # halt new trading for the rest of the UTC day
RISK_BROKER_FAIL_TICKS = 3  # consecutive failed broker snapshots -> halt
RISK_STATE_FILE = os.path.join(BASE_DIR, "logs", "risk_state.json")
# Persisted bot position book (open legs survive a restart so per-leg
# brackets stay armed and true entry prices are not lost to reconcile).
POSITIONS_FILE = os.path.join(BASE_DIR, "logs", "positions.json")
RISK_MAX_PRICE_DRIFT_PCT = 0.01  # abort if mark moved >1% since the signal
# Per-leg brackets (bot-opened legs only; adopted inventory excluded).
# A breach flattens the book via EXIT on the next tick. Starting values,
# uncalibrated — tighten/loosen only with replay evidence behind them.
STOP_PCT = 0.02  # adverse move from entry that forces an exit
TAKE_PCT = 0.03  # favorable move from entry that locks the win

# ── Signals ──────────────────────────────────────────────────────
DIVERGENCE_THRESHOLD = 0.015  # 1.5pp gap between rToken and BTC 24h change

# ── AI decision (dashboard: AI check; code name: Groq) ──────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
# NOTE: llama-3.1-8b-instant was retired by Groq (404). Current pick
# verified against the live /models list.
GROQ_MODEL = "openai/gpt-oss-20b"
GROQ_TIMEOUT_SEC = 10
GROQ_TRACE_FILE = "logs/groq_trace.jsonl"  # every prompt + raw verdict
GROQ_MAX_DISAGREE = 5  # consecutive Groq-vs-fallback disagreements -> cooldown
GROQ_COOLDOWN_TICKS = 10  # forced-fallback ticks after the breaker trips
# Rate-limit handling. Free-tier Groq caps calls/day: a call spent on a
# calm HOLD is wasted, and once the quota trips every call 429s and falls
# back to rules anyway. So (a) gate the LLM to ticks that actually need
# judgement, and (b) give a 429 one short retry — never long enough to
# stall the loop (a server cooldown past the cap just falls back this tick;
# the loop never blocks on the LLM).
GROQ_SENTIMENT_WAKE = 0.15  # |sentiment-0.5| that makes a calm tick worth an AI call
GROQ_MAX_RETRIES = 1        # extra attempts after a 429 (0 disables retry)
GROQ_RETRY_BASE_SEC = 1.0   # backoff when a 429 carries no Retry-After header
GROQ_RETRY_CAP_SEC = 3.0    # never block the loop longer than this on a retry


def has_credentials() -> bool:
    return bool(BITGET_API_KEY and BITGET_SECRET_KEY and BITGET_PASSPHRASE)


def masked_key() -> str:
    if not BITGET_API_KEY or len(BITGET_API_KEY) < 8:
        return "(missing)"
    return BITGET_API_KEY[:6] + "…" + BITGET_API_KEY[-4:]
