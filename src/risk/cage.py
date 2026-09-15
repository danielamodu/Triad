"""Risk cage: hard gates every decision must pass.

Rules:
  1. Max single position RISK_MAX_POSITION_USD (default $1000), counted
     on BOT-OPENED exposure only (persisted risk state; log replay is the
     fallback when no state is supplied). Pre-existing funding is
     baseline, not bot risk.
  2. Halt ALL trading if drawdown > RISK_MAX_DRAWDOWN_PCT (default 5%).
     Drawdown is peak-to-current running_pnl over
     max(RISK_MAX_POSITION_USD, peak deployed exposure).
  3. Halt ALL trading for the rest of the UTC day if the daily loss
     exceeds RISK_MAX_DAILY_LOSS_PCT (default 2%).
  4. Fail closed: unreadable risk state or a sustained broker-snapshot
     outage blocks everything (no fail-open).
  5. Block if the bot already holds the same leg (no doubling).
  6. Sells (HEDGE_CRYPTO/EXIT) only ever reduce exposure, so they pass
     the size gates; drawdown/daily/KILL/state halts still apply.

validate(decision, positions, risk=None, trade_symbol="") returns
{approved, decision, blocked_reason}. `risk` carries the live ledger:
{drawdown_pct, day_loss_pct, daily_halted, exposure {SYM: net},
corrupt, broker_dead, broker_streak}. Omitted keys degrade to the
legacy behavior (log replay + "__drawdown_pct" position key) so old
callers keep working. `trade_symbol` overrides the leg checked by the
no-doubling gate (the basket's selected rToken); empty falls back to
the default mapping.
"""
import json
import os

import config
from src.risk.state import iter_fills


def bot_exposure(log_path: str = "") -> dict:
    """Net USD exposure per symbol from Triad-executed orders in the log.

    Buys add notional, sells subtract it. Returns {SYMBOL: net_usd}.
    Unknown/missing history -> {} (fail-open would be dangerous, so
    callers treat unreadable history as blocking via KILL-like caution;
    here we return {} and let the order caps limit damage).
    """
    path = log_path or os.path.join(config.BASE_DIR, config.LOG_FILE)
    exposure: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return exposure
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        action = entry.get("action_taken") or {}
        if not action.get("executed"):
            continue
        for symbol, side, notional, executed in iter_fills(action):
            if not executed:
                continue
            exposure[symbol] = exposure.get(symbol, 0.0) + (
                notional if side == "buy" else -notional)
    return exposure


def validate(decision: dict, positions: dict, risk: dict = None,
             trade_symbol: str = "") -> dict:
    """Return {approved, decision, blocked_reason}."""
    name = str((decision or {}).get("decision", "HOLD")).upper()
    book = positions if isinstance(positions, dict) else {}
    ctx = risk if isinstance(risk, dict) else {}

    if os.path.exists(config.KILL_FILE):
        return {"approved": False, "decision": name,
                "blocked_reason": "KILL file present"}

    # Fail closed: an unreadable risk ledger blocks everything.
    if ctx.get("corrupt"):
        return {"approved": False, "decision": name,
                "blocked_reason": "risk state unreadable (fail-closed)"}

    # Fail closed: sustained broker-snapshot outage blocks everything.
    if ctx.get("broker_dead"):
        try:
            streak = int(ctx.get("broker_streak", 0) or 0)
        except (TypeError, ValueError):
            streak = 0
        return {"approved": False, "decision": name,
                "blocked_reason": "broker snapshot failing "
                                  + str(streak) + " ticks"}

    # Drawdown halt: explicit ledger value wins, legacy position key
    # keeps old callers working.
    drawdown = 0.0
    try:
        raw = ctx.get("drawdown_pct", book.get("__drawdown_pct", 0.0))
        drawdown = float(raw or 0.0)
    except (TypeError, ValueError):
        drawdown = 0.0
    if drawdown > config.RISK_MAX_DRAWDOWN_PCT:
        return {"approved": False, "decision": name,
                "blocked_reason": "drawdown " + format(drawdown, ".2%")
                                  + " > "
                                  + format(config.RISK_MAX_DRAWDOWN_PCT, ".0%")
                                  + " halt"}

    # Daily-loss halt: applies to sells too (flat and stay flat).
    if ctx.get("daily_halted"):
        try:
            loss = float(ctx.get("day_loss_pct", 0.0) or 0.0)
        except (TypeError, ValueError):
            loss = 0.0
        return {"approved": False, "decision": name,
                "blocked_reason": "daily loss " + format(loss, ".2%")
                                  + " > "
                                  + format(config.RISK_MAX_DAILY_LOSS_PCT,
                                           ".0%") + " halt"}

    if name == "HOLD":
        return {"approved": True, "decision": name, "blocked_reason": ""}
    if name not in ("LONG_RTOKEN", "HEDGE_CRYPTO", "EXIT"):
        return {"approved": False, "decision": name,
                "blocked_reason": "unknown decision " + name}

    if trade_symbol:
        target = str(trade_symbol).upper()
    else:
        target = (config.RTOKEN_SYMBOL if name in ("LONG_RTOKEN", "EXIT")
                  else config.CRYPTO_SYMBOL)
    if isinstance(ctx.get("exposure"), dict):
        mine = ctx["exposure"].get(target.upper(), 0.0)
        try:
            mine = float(mine or 0.0)
        except (TypeError, ValueError):
            mine = 0.0
    else:
        mine = bot_exposure().get(target.upper(), 0.0)

    if name in ("HEDGE_CRYPTO", "EXIT"):
        # Sells only reduce exposure; size gates don't apply.
        return {"approved": True, "decision": name, "blocked_reason": ""}
    if mine + config.RISK_MAX_POSITION_USD > config.RISK_MAX_POSITION_USD:
        return {"approved": False, "decision": name,
                "blocked_reason": "bot holds " + target + " $"
                                  + format(mine, ",.0f")
                                  + "; new $"
                                  + format(config.RISK_MAX_POSITION_USD, ",.0f")
                                  + " would exceed max $"
                                  + format(config.RISK_MAX_POSITION_USD, ",.0f")
                                  + " (no doubling)"}
    return {"approved": True, "decision": name, "blocked_reason": ""}
