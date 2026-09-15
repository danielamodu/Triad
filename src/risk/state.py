"""Persisted risk state: the cage's memory across restarts.

Stored at config.RISK_STATE_FILE (logs/risk_state.json), written
atomically (tmp file + os.replace) so a crash can never leave half
a JSON document behind. Schema (version 1):

  exposure: {SYMBOL: net_usd}   bot-opened exposure from executed fills
                                (buys add, sells subtract)
  peak_pnl: float               highest running_pnl observed (drawdown anchor)
  peak_exposure_usd: float      highest total |exposure| observed
  day: str                      UTC date (YYYY-MM-DD) of day_start_pnl
  day_start_pnl: float          running_pnl at the first tick of `day`
  broker_fail_streak: int       consecutive ticks the broker snapshot failed

Drawdown percentages are measured against
max(RISK_MAX_POSITION_USD, peak_exposure_usd): adverse excursion as a
fraction of the most capital the bot has ever deployed (floored at the
max position size, so the base is never zero).

Unlike logs/trades.jsonl (append-only audit trail), this file is the
live risk ledger. If it is missing, a fresh one is started. If it is
unreadable, load_state() reports ok=False and the cage fails closed.
"""
import json
import os

import config

VERSION = 1


def fresh_state() -> dict:
    """Blank state. Never raises."""
    return {"version": VERSION, "exposure": {}, "peak_pnl": 0.0,
            "peak_exposure_usd": 0.0, "day": "", "day_start_pnl": 0.0,
            "broker_fail_streak": 0}


def _coerce(raw) -> dict | None:
    """Validate a decoded document. Returns a clean state or None."""
    if not isinstance(raw, dict) or raw.get("version") != VERSION:
        return None
    try:
        exposure = {str(k).upper(): float(v)
                    for k, v in (raw.get("exposure") or {}).items()}
        return {"version": VERSION, "exposure": exposure,
                "peak_pnl": float(raw.get("peak_pnl", 0.0)),
                "peak_exposure_usd": float(
                    raw.get("peak_exposure_usd", 0.0)),
                "day": str(raw.get("day", "") or ""),
                "day_start_pnl": float(raw.get("day_start_pnl", 0.0)),
                "broker_fail_streak": int(
                    raw.get("broker_fail_streak", 0))}
    except (TypeError, ValueError):
        return None


def load_state(path: str = "") -> tuple:
    """Load risk state. Returns (state, ok).

    Missing file -> (fresh_state(), True). Unreadable or invalid file
    -> (fresh_state(), False) so the caller can fail closed. Never raises.
    """
    target = path or config.RISK_STATE_FILE
    try:
        with open(target, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        if not os.path.exists(target):
            return fresh_state(), True
        return fresh_state(), False
    state = _coerce(raw)
    if state is None:
        return fresh_state(), False
    return state, True


def save_state(state: dict, path: str = "") -> bool:
    """Atomically persist risk state. Returns True on success. Never raises."""
    target = path or config.RISK_STATE_FILE
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, separators=(",", ":"))
        os.replace(tmp, target)
        return True
    except (OSError, TypeError, ValueError):
        return False


def iter_fills(action_taken: dict):
    """Yield (symbol, side, notional, executed) for each leg in an action.

    Handles single-leg actions (fields at top level) and multi-leg
    actions (details=[...]). Never raises; skips malformed legs.
    """
    try:
        if not isinstance(action_taken, dict):
            return
        details = action_taken.get("details")
        if isinstance(details, list):
            legs = [leg for leg in details if isinstance(leg, dict)]
        elif isinstance(details, dict) and details.get("symbol"):
            legs = [details]
        elif isinstance(action_taken.get("symbol"), str) and \
                action_taken.get("symbol"):
            legs = [action_taken]
        else:
            return
        for leg in legs:
            symbol = str(leg.get("symbol", "")).upper()
            side = str(leg.get("side", "")).lower()
            # Book what actually filled (partials!), falling back to the
            # intended notional on old log shapes without fill data.
            try:
                notional = float(leg.get("fill_value", 0)
                                 or leg.get("notional_usdt", 0) or 0)
            except (TypeError, ValueError):
                notional = 0.0
            if not symbol or notional <= 0:
                continue
            yield symbol, side, notional, bool(leg.get("executed"))
    except Exception:
        return


def record_fills(state: dict, action_taken: dict) -> None:
    """Fold executed fills into state exposure. Updates the exposure peak.

    Unexecuted actions (blocked/skipped/failed) leave exposure unchanged.
    Never raises.
    """
    try:
        exposure = state.setdefault("exposure", {})
        for symbol, side, notional, executed in iter_fills(action_taken):
            if not executed:
                continue
            exposure[symbol] = exposure.get(symbol, 0.0) + (
                notional if side == "buy" else -notional)
        total = sum(abs(v) for v in exposure.values())
        try:
            total = float(total)
        except (TypeError, ValueError):
            total = 0.0
        if total > float(state.get("peak_exposure_usd", 0.0) or 0.0):
            state["peak_exposure_usd"] = round(total, 4)
    except Exception:
        pass


def _base(state: dict) -> float:
    """Drawdown denominator: never zero. See module docstring."""
    try:
        peak_exp = float(state.get("peak_exposure_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        peak_exp = 0.0
    return max(float(config.RISK_MAX_POSITION_USD), peak_exp, 1e-9)


def drawdown_pct(state: dict, running_pnl: float) -> float:
    """Peak-to-current drawdown as a fraction. Advances the peak.

    peak_pnl ratchets up to running_pnl; drawdown is (peak - pnl) / base,
    floored at 0. Never raises.
    """
    try:
        pnl = float(running_pnl or 0.0)
    except (TypeError, ValueError):
        pnl = 0.0
    try:
        peak = float(state.get("peak_pnl", 0.0) or 0.0)
        if pnl > peak:
            peak = pnl
            state["peak_pnl"] = round(peak, 4)
        return max(0.0, (peak - pnl) / _base(state))
    except Exception:
        return 0.0


def roll_day(state: dict, today: str, running_pnl: float) -> None:
    """Reset the daily anchor when the UTC date changes. Never raises."""
    try:
        if state.get("day") != today:
            state["day"] = today
            state["day_start_pnl"] = round(float(running_pnl or 0.0), 4)
    except Exception:
        pass


def day_loss_pct(state: dict, running_pnl: float) -> float:
    """Today's loss as a fraction of base. Negative means profit.

    Never raises.
    """
    try:
        pnl = float(running_pnl or 0.0)
        start = float(state.get("day_start_pnl", 0.0) or 0.0)
        return (start - pnl) / _base(state)
    except Exception:
        return 0.0


def note_broker(state: dict, ok: bool) -> int:
    """Record a broker-snapshot success/failure. Returns the streak."""
    try:
        if ok:
            state["broker_fail_streak"] = 0
        else:
            state["broker_fail_streak"] = int(
                state.get("broker_fail_streak", 0) or 0) + 1
        return int(state["broker_fail_streak"])
    except Exception:
        return 0
