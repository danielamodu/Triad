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

# ── Logging ──────────────────────────────────────────────────────
LOG_FILE = "logs/trades.jsonl"
KILL_FILE = os.path.join(BASE_DIR, "logs", "KILL")  # create to halt trading

# ── Risk ─────────────────────────────────────────────────────────
RISK_MAX_POSITION_USD = 1000
RISK_MAX_DRAWDOWN_PCT = 0.05
RISK_MAX_DAILY_LOSS_PCT = 0.02  # halt new trading for the rest of the UTC day
RISK_BROKER_FAIL_TICKS = 3  # consecutive failed broker snapshots -> halt
RISK_STATE_FILE = os.path.join(BASE_DIR, "logs", "risk_state.json")

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
