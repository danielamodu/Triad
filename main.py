"""Triad heartbeat loop: 3 signals -> decision -> risk cage -> execute.

    python main.py            # loop forever, 300s between ticks
    python main.py --once     # single tick, then exit (testing)

Event wake: if price divergence is detected, the loop acts immediately
and skips the sleep. Nothing here can crash the loop: every stage is
guarded and failures are logged to logs/trades.jsonl.
"""
import argparse
import sys
import time
import traceback
from datetime import datetime, timezone

import config
from src import cli
from src.decision.engine import decide
from src.execution.executor import execute, get_positions, size_for_confidence
from src.logger import append_jsonl, append_log
from src.risk import state as risk_state
from src.risk.cage import validate
from src.risk.state import iter_fills
from src.signals.event_signal import get_event
from src.signals.price_divergence import get_divergence
from src.signals.sentiment_signal import get_sentiment

SYMBOLS = [config.RTOKEN_SYMBOL, config.CRYPTO_SYMBOL]

# ── Risk-ledger persistence ───────────────────────────────────────
# Tracks whether the last risk-state save succeeded. A failed save
# fails the cage closed on the next tick (stale exposure must never
# silently permit a new position).
_STATE_SAVE_OK = True

# ── Agent memory (short-term) ─────────────────────────────────────
# Last 10 decision+outcome dicts:
# {decision, confidence, executed, running_pnl}. The last 3 are passed
# to the Groq prompt each tick for cross-tick context.
MEMORY: list = []


def _memory_context(n: int = 3) -> list:
    """Last n memory entries (copies) for the decision prompt."""
    return [dict(m) for m in MEMORY[-n:]] if MEMORY else []


def _push_memory(decision: dict, action_taken: dict,
                 running_pnl: float) -> None:
    """Append one decision+outcome, keeping only the last 10."""
    try:
        MEMORY.append({
            "decision": str((decision or {}).get("decision", "HOLD")),
            "confidence": (decision or {}).get("confidence", 0),
            "executed": bool((action_taken or {}).get("executed", False)),
            "running_pnl": running_pnl,
            "engine": str((decision or {}).get("engine_used", "")),
            "fallback_decision": str(
                (decision or {}).get("fallback_decision", "")),
        })
        del MEMORY[:-10]
    except Exception:
        pass


def _update_groq_breaker(rstate: dict, decision: dict) -> None:
    """Drift breaker: sustained Groq-vs-fallback disagreement forces the
    deterministic fallback for GROQ_COOLDOWN_TICKS ticks. Never raises."""
    try:
        decision = decision or {}
        if decision.get("engine_used") != "groq":
            if decision.get("fallback_reason", "").startswith(
                    "groq cooldown"):
                left = int(rstate.get("groq_cooldown", 0) or 0)
                rstate["groq_cooldown"] = max(0, left - 1)
            return
        if decision.get("fallback_agree", True):
            rstate["groq_streak"] = 0
            return
        streak = int(rstate.get("groq_streak", 0) or 0) + 1
        if streak >= config.GROQ_MAX_DISAGREE:
            rstate["groq_cooldown"] = config.GROQ_COOLDOWN_TICKS
            rstate["groq_streak"] = 0
            print(f"[triad] WARNING: Groq drift breaker tripped "
                  f"({config.GROQ_MAX_DISAGREE} disagreements); "
                  f"fallback for {config.GROQ_COOLDOWN_TICKS} ticks.",
                  flush=True)
        else:
            rstate["groq_streak"] = streak
    except Exception:
        pass


def _write_groq_trace(decision: dict) -> None:
    """Append the prompt + raw verdict to the audit trace. Never raises."""
    try:
        if not isinstance(decision, dict):
            return
        if decision.get("engine_used") != "groq" or "_prompt" not in decision:
            return
        append_jsonl(config.GROQ_TRACE_FILE,
                       {"model": config.GROQ_MODEL,
                        "decision": decision.get("decision"),
                        "confidence": decision.get("confidence"),
                        "fallback_decision": decision.get(
                            "fallback_decision"),
                        "fallback_agree": decision.get("fallback_agree"),
                        "latency_ms": decision.get("latency_ms"),
                        "prompt": decision.get("_prompt"),
                        "raw_response": decision.get("_raw_response")})
    except Exception:
        pass

DECISION_SYMBOL = {
    "LONG_RTOKEN": config.RTOKEN_SYMBOL,
    "HEDGE_CRYPTO": config.CRYPTO_SYMBOL,
    "EXIT": config.RTOKEN_SYMBOL,
    "HOLD": "",
}

# ── In-memory position tracker ────────────────────────────────────
# {SYMBOL: {symbol, side, size_usd, entry_price, current_price, pnl}}.
# side is "long" (buy) or "short" (sell). Refreshed every tick from the
# latest market price; opened/closed from executed fills.
OPEN_POSITIONS: dict = {}


def _current_price(symbol: str, price_signal: dict) -> float:
    """Latest market price for a symbol from the price signal."""
    price_signal = price_signal or {}
    symbol = (symbol or "").upper()
    try:
        basket = [str(s or "").upper() for s in
                  getattr(config, "RTOKEN_BASKET", [config.RTOKEN_SYMBOL])]
    except Exception:
        basket = [config.RTOKEN_SYMBOL.upper()]
    if symbol in basket or symbol == config.RTOKEN_SYMBOL.upper():
        raw = price_signal.get("rtoken_last", "")
    elif symbol == config.CRYPTO_SYMBOL.upper():
        raw = price_signal.get("crypto_last", "")
    else:
        raw = ""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _update_positions(price_signal: dict) -> None:
    """Refresh current_price/pnl of every open position in place."""
    for symbol, pos in list(OPEN_POSITIONS.items()):
        if not isinstance(pos, dict):
            continue
        current = _current_price(symbol, price_signal)
        if current <= 0:
            continue
        pos["current_price"] = current
        try:
            entry = float(pos.get("entry_price", 0) or 0)
            size = float(pos.get("size_usd", 0) or 0)
        except (TypeError, ValueError):
            continue
        if entry <= 0 or size <= 0:
            pos["pnl"] = 0.0
            continue
        if str(pos.get("side", "long")).lower() == "short":
            pnl = (entry - current) / entry * size
        else:
            pnl = (current - entry) / entry * size
        pos["pnl"] = round(pnl, 4)
        pos["usd"] = size  # compat with get_positions() shape


def _close_realized(pos: dict, leg: dict) -> float:
    """Realized PnL for closing a tracked leg.

    Prefers fill proceeds vs booked size; falls back to the stored
    mark-to-market pnl when no fill data is present. Never raises.
    """
    try:
        size = float(pos.get("size_usd", 0) or 0)
        fill_value = float((leg or {}).get("fill_value", 0) or 0)
        if fill_value > 0 and size > 0:
            if str(pos.get("side", "long")).lower() == "short":
                return round(size - fill_value, 4)  # sold high, bought back
            return round(fill_value - size, 4)  # bought, sold proceeds
        return round(float(pos.get("pnl", 0) or 0), 4)
    except (TypeError, ValueError):
        return 0.0


# A close whose fill covers >=99.9% of the leg's market value is a full
# close (exact fill-vs-size booking). Anything smaller is a partial:
# only its pro-rata share of unrealized counts as realized, and the
# residual stays open. (Found on the live box 2026-09-16: the startup
# reconcile adopts the whole wallet balance as one leg while HEDGE only
# sells $1000 of it — booking fill-minus-size as if the full leg closed
# minted a phantom -$719k realized loss.)
_CLOSE_FULL_FRACTION = 0.999


def _close_leg(pos: dict, leg: dict) -> tuple:
    """Close (part of) a tracked leg. Returns (realized, residual).

    residual None means fully closed; otherwise the shrunken position
    to keep open. Never raises.
    """
    try:
        fill = float((leg or {}).get("fill_value", 0) or 0)
    except (TypeError, ValueError):
        fill = 0.0
    if fill <= 0:
        # No fill data: full close at the stored mark (old behavior).
        return _close_realized(pos, leg), None
    try:
        size = float((pos or {}).get("size_usd", 0) or 0)
        pnl = float((pos or {}).get("pnl", 0) or 0)
    except (TypeError, ValueError):
        return _close_realized(pos, leg), None
    short = str((pos or {}).get("side", "long")).lower() == "short"
    value = size - pnl if short else size + pnl
    if value <= 0 or fill >= value * _CLOSE_FULL_FRACTION:
        return _close_realized(pos, leg), None
    frac = fill / value
    realized = round(frac * pnl, 4)
    residual = dict(pos) if isinstance(pos, dict) else {}
    residual["size_usd"] = round(size * (1 - frac), 4)
    residual["pnl"] = round(pnl * (1 - frac), 4)
    residual["usd"] = residual["size_usd"]
    return realized, residual


def _sync_positions(action_taken: dict, price_signal: dict,
                    decision_name: str = "") -> float:
    """Open/close in-memory positions from executed fills.

    Returns the PnL realized by closed legs this call (0.0 when nothing
    closed). Never raises.
    """
    realized = 0.0
    try:
        if not isinstance(action_taken, dict) or not action_taken.get(
                "executed"):
            return realized
        details = action_taken.get("details")
        if isinstance(details, list):
            legs = [leg for leg in details
                    if isinstance(leg, dict) and leg.get("executed")]
        elif isinstance(details, dict) and details.get("symbol"):
            legs = [details]
        elif isinstance(action_taken.get("symbol"), str) and \
                action_taken.get("symbol"):
            legs = [action_taken]
        else:
            return realized
        if str(decision_name or "").upper() == "EXIT":
            by_symbol = {str(leg.get("symbol", "")).upper(): leg
                         for leg in legs}
            for symbol in list(OPEN_POSITIONS):
                pos = OPEN_POSITIONS.pop(symbol)
                if isinstance(pos, dict):
                    part, residual = _close_leg(pos, by_symbol.get(symbol))
                    realized += part
                    if residual is not None:
                        OPEN_POSITIONS[symbol] = residual
            return round(realized, 4)
        for leg in legs:
            symbol = str(leg.get("symbol", "")).upper()
            side = str(leg.get("side", "")).lower()
            # Book what actually filled (partials!), falling back to the
            # intended notional when no fill data is present.
            try:
                notional = float(leg.get("fill_value", 0)
                                 or leg.get("notional_usdt", 0) or 0)
            except (TypeError, ValueError):
                notional = 0.0
            if not symbol or notional <= 0:
                continue
            # Prefer the broker's fill price; fall back to the signal's
            # last price when the fill fetch failed (paper gaps, BTC leg
            # fills while rToken legs reject). current_price stays the
            # live mark either way.
            try:
                fill_price = float(leg.get("fill_price", 0) or 0)
            except (TypeError, ValueError):
                fill_price = 0.0
            current = _current_price(symbol, price_signal)
            entry_price = fill_price if fill_price > 0 else current
            if side == "buy":
                OPEN_POSITIONS[symbol] = {
                    "symbol": symbol, "side": "long",
                    "size_usd": notional, "entry_price": entry_price,
                    "current_price": current, "pnl": 0.0,
                    "usd": notional}
            elif side == "sell":
                if symbol in OPEN_POSITIONS:
                    # Closing (part of) a tracked leg. Partial fills keep
                    # a shrunken residual open instead of booking the
                    # whole leg as closed.
                    pos = OPEN_POSITIONS.pop(symbol)
                    if isinstance(pos, dict):
                        part, residual = _close_leg(pos, leg)
                        realized += part
                        if residual is not None:
                            OPEN_POSITIONS[symbol] = residual
                else:
                    OPEN_POSITIONS[symbol] = {
                        "symbol": symbol, "side": "short",
                        "size_usd": notional, "entry_price": entry_price,
                        "current_price": current, "pnl": 0.0,
                        "usd": notional}
        return round(realized, 4)
    except Exception:
        return round(realized, 4)


def _running_pnl() -> float:
    """Total unrealized PnL across open positions."""
    total = 0.0
    for pos in OPEN_POSITIONS.values():
        try:
            total += float(pos.get("pnl", 0) or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 4)


def _executed_notional(action_taken: dict) -> float:
    """Gross USD actually filled by an action (0.0 unless executed).

    Sums |notional| over executed legs. This is what moved; contrast
    position_size_usd, which is what confidence sizing *intended*.
    Never raises.
    """
    try:
        total = sum(abs(notional)
                    for _, _, notional, executed
                    in iter_fills(action_taken) if executed)
        return round(float(total), 4)
    except Exception:
        return 0.0


def _action_fees(action_taken: dict) -> float:
    """Total broker fees (USD-ish) reported by executed legs. Never raises."""
    try:
        if not isinstance(action_taken, dict):
            return 0.0
        details = action_taken.get("details")
        if isinstance(details, list):
            legs = [leg for leg in details if isinstance(leg, dict)
                    and leg.get("executed")]
        elif isinstance(details, dict) and details.get("executed"):
            legs = [details]
        elif action_taken.get("executed"):
            legs = [action_taken]
        else:
            return 0.0
        total = 0.0
        for leg in legs:
            try:
                total += float(leg.get("fee_usd", 0) or 0)
            except (TypeError, ValueError):
                continue
        return round(total, 6)
    except Exception:
        return 0.0


def safe(stage: str, fn, *args, fallback):
    try:
        return fn(*args)
    except Exception as exc:
        return {**fallback, "reason": f"{stage} failed: {exc}"[:200]}


# Balances below this are dust: left alone, never adopted, never traded.
_RECONCILE_DUST_USD = 1.0


def reconcile_startup(live: bool) -> dict:
    """Resume-from-broker: adopt live balances into tracking on boot.

    Compares the broker snapshot against the persisted ledger and the
    (always empty at boot) in-memory book. Broker balances above dust
    with no tracked leg are adopted as long legs at the current mark
    and seeded into the ledger (max of ledger/broker, so a restart can
    only tighten the no-doubling gate, never loosen it). Resting open
    orders are reported, never touched.

    In-memory state from a previous process is never trusted: this is
    the only writer of OPEN_POSITIONS outside tick fills. Returns a
    report dict; prints it. Never raises.
    """
    report: dict = {"mode": "live" if live else "paper", "adopted": [],
                    "open_orders": [], "warnings": []}
    try:
        price = safe("price", get_divergence, fallback={
            "signal": "STABLE", "direction": "FLAT", "divergence_score": 0.0,
            "rtoken_change": 0.0, "crypto_change": 0.0})
    except Exception:
        price = {}
    try:
        rstate, state_ok = risk_state.load_state()
        if not state_ok:
            report["warnings"].append(
                "risk state unreadable at startup; starting fresh ledger "
                "(verify the broker is flat before trading)")
            rstate = risk_state.fresh_state()
    except Exception:
        rstate, state_ok = risk_state.fresh_state(), True
    try:
        account = get_positions(paper=not live)
        broker_ok = (isinstance(account, dict)
                     and account.get("__ok", False) is True)
    except Exception:
        account, broker_ok = {}, False
    if not broker_ok:
        report["warnings"].append("broker snapshot failed at startup; "
                                  "positions unknown until ticks succeed")
        account = {}
    try:
        opens = cli.open_orders(config.SPOT_CATEGORY, paper=not live)
        for row in opens if isinstance(opens, list) else []:
            if isinstance(row, dict):
                report["open_orders"].append(
                    {k: row.get(k) for k in
                     ("orderId", "symbol", "side", "qty", "orderStatus")})
        if report["open_orders"]:
            report["warnings"].append(
                f"{len(report['open_orders'])} resting open order(s): "
                "left untouched, review manually")
    except Exception:
        pass
    try:
        exposure = rstate.setdefault("exposure", {})
        for symbol in (config.RTOKEN_SYMBOL, config.CRYPTO_SYMBOL):
            try:
                usd = float((account.get(symbol, {}) or {}).get("usd", 0)
                            or 0)
            except (TypeError, ValueError):
                usd = 0.0
            if usd < _RECONCILE_DUST_USD or symbol in OPEN_POSITIONS:
                continue
            mark = _current_price(symbol, price)
            OPEN_POSITIONS[symbol] = {
                "symbol": symbol, "side": "long",
                "size_usd": round(usd, 4), "entry_price": mark,
                "current_price": mark, "pnl": 0.0, "usd": round(usd, 4),
                "reconciled": True}
            try:
                prior = float(exposure.get(symbol, 0.0) or 0.0)
            except (TypeError, ValueError):
                prior = 0.0
            if usd > prior:
                exposure[symbol] = round(usd, 4)
            report["adopted"].append({"symbol": symbol,
                                      "usd": round(usd, 4),
                                      "ledger_was": round(prior, 4)})
        total = sum(abs(v) for v in exposure.values()
                    if isinstance(v, (int, float)))
        if total > float(rstate.get("peak_exposure_usd", 0.0) or 0.0):
            rstate["peak_exposure_usd"] = round(float(total), 4)
        global _STATE_SAVE_OK
        if not risk_state.save_state(rstate):
            _STATE_SAVE_OK = False
            report["warnings"].append(
                "risk state save failed at startup; cage fails closed")
    except Exception as exc:
        report["warnings"].append(f"reconcile error: {exc}"[:160])
    print(f"[triad] reconcile ({report['mode']}): "
          f"{len(report['adopted'])} adopted, "
          f"{len(report['open_orders'])} open orders, "
          f"{len(report['warnings'])} warnings.", flush=True)
    for warning in report["warnings"]:
        print(f"[triad] reconcile warning: {warning}", flush=True)
    return report


def tick(live: bool = False) -> dict:
    """One full pass. Returns a summary dict (also logged).

    live=True trades the LIVE account and must only come from main()'s
    --live gate; the mode is recorded on every log entry.
    """
    price = safe("price", get_divergence, fallback={
        "signal": "STABLE", "direction": "FLAT", "divergence_score": 0.0,
        "rtoken_change": 0.0, "crypto_change": 0.0})
    event = safe("event", get_event, fallback={
        "signal": "NEUTRAL", "confidence": 0.5})
    sentiment = safe("sentiment", get_sentiment, fallback={
        "sentiment": "neutral", "score": 0.5})

    # Multi-rToken basket: this tick trades the widest-divergence leg.
    selected_rtoken = str(price.get("selected_rtoken", "")
                          or config.RTOKEN_SYMBOL).upper()
    tick_symbols = [selected_rtoken, config.CRYPTO_SYMBOL]

    _update_positions(price)
    pre_pnl = _running_pnl()

    # ── Risk ledger ──────────────────────────────────────────
    # Load once per tick; all gates below read this snapshot.
    # Gates see equity PnL (realized + unrealized), matching the
    # backtest: unrealized-only understates drawdown after a loss is
    # closed out (the peak remembers, the loss vanishes).
    global _STATE_SAVE_OK
    rstate, state_ok = risk_state.load_state()
    try:
        realized_total = float(rstate.get("realized_pnl", 0.0) or 0.0)
    except (TypeError, ValueError):
        realized_total = 0.0
    gate_pnl = round(pre_pnl + realized_total, 4)
    today = datetime.now(timezone.utc).date().isoformat()
    risk_state.roll_day(rstate, today, gate_pnl)

    try:
        account = get_positions(paper=not live)
    except Exception as exc:
        account = {"__error": str(exc)[:160]}
    broker_ok = (isinstance(account, dict)
                 and account.get("__ok", False) is True)
    broker_streak = risk_state.note_broker(rstate, broker_ok)

    drawdown = risk_state.drawdown_pct(rstate, gate_pnl)
    day_loss = risk_state.day_loss_pct(rstate, gate_pnl)
    if not risk_state.save_state(rstate):
        _STATE_SAVE_OK = False
        print("[triad] WARNING: risk state save failed; "
              "cage fails closed next tick.", flush=True)

    # Merge broker snapshot with tracked legs so the decision engine
    # and risk cage see live prices + pnl each tick (tracked wins).
    # Dunder keys (__ok, __error) are account metadata, not positions.
    tracked = {k: v for k, v in (account.items()
               if isinstance(account, dict) else [])
               if not str(k).startswith("__")}
    for symbol, pos in OPEN_POSITIONS.items():
        tracked[symbol] = pos

    risk_ctx = {
        "drawdown_pct": drawdown,
        "day_loss_pct": day_loss,
        "daily_halted": day_loss > config.RISK_MAX_DAILY_LOSS_PCT,
        "exposure": dict(rstate.get("exposure", {})),
        "corrupt": (not state_ok) or (not _STATE_SAVE_OK),
        "broker_dead": broker_streak >= config.RISK_BROKER_FAIL_TICKS,
        "broker_streak": broker_streak,
    }

    decision = decide({"price": price, "event": event,
                       "sentiment": sentiment}, tracked,
                      _memory_context(3),
                      force_fallback=int(rstate.get("groq_cooldown", 0)
                                         or 0) > 0)
    _update_groq_breaker(rstate, decision)
    _write_groq_trace(decision)
    # Audit keys stay out of the trade log (they live in the trace file).
    decision.pop("_prompt", None)
    decision.pop("_raw_response", None)

    # Confidence-based position sizing for the log (executor enforces
    # the same map and skips < 0.6 as LOW_CONFIDENCE_SKIP). This is the
    # INTENDED size; executed_notional_usd below reports what filled.
    if str(decision.get("decision", "HOLD")).upper() == "HOLD":
        position_size_usd = 0.0
    else:
        position_size_usd = size_for_confidence(
            decision.get("confidence", 0))

    # The no-doubling gate checks the leg about to be traded (the
    # basket's selected rToken for LONG, not the default symbol).
    decision_name = str(decision.get("decision", "HOLD")).upper()
    if decision_name == "LONG_RTOKEN":
        trade_symbol = selected_rtoken
    elif decision_name == "HEDGE_CRYPTO":
        trade_symbol = config.CRYPTO_SYMBOL
    else:
        trade_symbol = ""
    try:
        risk = validate(decision, tracked, risk_ctx, trade_symbol)
    except Exception as exc:
        risk = {"approved": False, "decision": decision.get("decision", "HOLD"),
                "blocked_reason": f"cage error: {exc}"[:200]}

    action_taken: dict = {"executed": False, "order_id": "",
                          "details": "not attempted"}
    if risk.get("approved") and risk.get("decision") != "HOLD":
        # EXIT flattens both legs; other decisions touch one symbol.
        # LONG_RTOKEN trades the selected basket leg this tick.
        if risk["decision"] == "EXIT":
            symbols = [selected_rtoken, config.CRYPTO_SYMBOL]
        elif risk["decision"] == "LONG_RTOKEN":
            symbols = [selected_rtoken]
        else:
            symbols = [DECISION_SYMBOL.get(risk["decision"], "")]
        legs = []
        try:
            for symbol in symbols:
                if symbol:
                    legs.append(execute({"decision": risk["decision"],
                                         "confidence": decision.get(
                                             "confidence", 0)},
                                        symbol, live=live,
                                        ref_price=_current_price(
                                            symbol, price)))
        except Exception as exc:
            legs.append({"executed": False, "order_id": "",
                         "details": f"executor crashed: {exc}"[:200]})
        filled = [leg for leg in legs if leg.get("executed")]
        action_taken = {"executed": bool(filled),
                        "order_id": ",".join(
                            leg.get("order_id", "") for leg in filled),
                        "details": legs if len(legs) > 1 else
                        (legs[0] if legs else "no symbol")}
    elif not risk.get("approved"):
        action_taken = {"executed": False, "order_id": "",
                        "details": f"blocked: {risk.get('blocked_reason')}"}

    closed_pnl = _sync_positions(action_taken, price,
                                 risk.get("decision", ""))
    realized_total = risk_state.add_realized(rstate, closed_pnl)
    _update_positions(price)
    running_pnl = _running_pnl()
    equity_pnl = round(running_pnl + realized_total, 4)
    memory_summary = _memory_context(3)
    executed_notional_usd = _executed_notional(action_taken)
    fees_usd = _action_fees(action_taken)

    # Fold fills into the persisted ledger (blocks/skips change nothing).
    risk_state.record_fills(rstate, action_taken)
    if not risk_state.save_state(rstate):
        _STATE_SAVE_OK = False
        print("[triad] WARNING: risk state save failed; "
              "cage fails closed next tick.", flush=True)

    entry = {"signal_inputs": {"price": price, "event": event,
                               "sentiment": sentiment},
             "decision": decision,
             "risk": risk,
             "action_taken": action_taken,
             "symbols": tick_symbols,
             "positions": list(OPEN_POSITIONS.values()),
             "running_pnl": running_pnl,
             "realized_pnl": realized_total,
             "equity_pnl": equity_pnl,
             "position_size_usd": position_size_usd,
             "executed_notional_usd": executed_notional_usd,
             "fees_usd": fees_usd,
             "drawdown_pct": round(drawdown, 6),
             "mode": "live" if live else "paper",
             "memory_summary": memory_summary,
             "reasoning": decision.get("reasoning", "")}
    try:
        append_log(entry)
    except Exception:
        traceback.print_exc()

    _push_memory(decision, action_taken, running_pnl)
    _print_status(price, event, sentiment, decision, risk, action_taken,
                  live)
    return {"diverged": price.get("signal") == "DIVERGENCE_DETECTED",
            "decision": decision.get("decision"),
            "executed": bool(action_taken.get("executed"))}


def _print_status(price, event, sentiment, decision, risk, action_taken,
                  live: bool = False) -> None:
    bar = "=" * 64
    print(f"\n{bar}")
    print(f"  TRIAD tick "
          f"| {config.RTOKEN_SYMBOL}/{config.CRYPTO_SYMBOL} "
          f"| {'LIVE' if live else 'paper'}")
    print(f"  price     : {price.get('signal')} "
          f"{price.get('direction')} gap={price.get('divergence_score'):+.3%} "
          f"(r {price.get('rtoken_change'):+.2%} / "
          f"c {price.get('crypto_change'):+.2%})")
    print(f"  event     : {event.get('signal')} "
          f"conf={event.get('confidence')} :: {event.get('reason', '')}")
    print(f"  sentiment : {sentiment.get('sentiment')} "
          f"score={sentiment.get('score')} :: {sentiment.get('reason', '')}")
    print(f"  decision  : {decision.get('decision')} "
          f"conf={decision.get('confidence')} scores={decision.get('scores')} "
          f"[{decision.get('engine_used', 'weighted_fallback')}]")
    print(f"  why       : {decision.get('reasoning')}")
    if risk.get("approved"):
        print(f"  risk      : APPROVED ({risk.get('decision')})")
    else:
        print(f"  risk      : BLOCKED :: {risk.get('blocked_reason')}")
    if action_taken.get("executed"):
        print(f"  executed  : order {action_taken.get('order_id')}")
    else:
        print(f"  executed  : no :: {action_taken.get('details')}")
    print(f"{bar}\n", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Triad execution agent")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--live", action="store_true",
                        help="trade the LIVE account (needs TRIAD_LIVE_OK=1 "
                             "and no logs/KILL file; otherwise refused)")
    args = parser.parse_args()

    live, live_reason = config.live_trading_enabled(args.live)
    if args.live and not live:
        print(f"[triad] FATAL: {live_reason}", flush=True)
        return 2

    print(f"[triad] mode={'LIVE' if live else 'paper'} "
          f"key={config.masked_key()} rtoken={config.RTOKEN_SYMBOL} "
          f"crypto={config.CRYPTO_SYMBOL} interval={config.HEARTBEAT_INTERVAL}s",
          flush=True)
    if not config.has_credentials():
        print("[triad] WARNING: BITGET_* env missing; trading calls will fail.",
              flush=True)
    try:
        cli.check_cli()
    except Exception as exc:
        print(f"[triad] FATAL: bgc CLI unusable: {exc}", flush=True)
        return 2

    # Resume-from-broker before the first tick: adopt live balances,
    # report resting orders. Never resumes in-memory state (there is
    # none: a fresh process starts with empty books by construction).
    reconcile_startup(live)

    last_tick = 0.0
    while True:
        try:
            summary = tick(live=live)
        except Exception:
            traceback.print_exc()
            try:
                crash_pnl = _running_pnl()
                append_log({"signal_inputs": {}, "decision": {},
                            "action_taken": {"executed": False,
                                             "details": "tick crashed"},
                            "symbols": SYMBOLS,
                            "positions": list(OPEN_POSITIONS.values()),
                            "running_pnl": crash_pnl,
                            "realized_pnl": 0.0,
                            "equity_pnl": crash_pnl,
                            "position_size_usd": 0.0,
                            "executed_notional_usd": 0.0,
                            "fees_usd": 0.0,
                            "drawdown_pct": 0.0,
                            "mode": "live" if live else "paper",
                            "memory_summary": _memory_context(3),
                            "reasoning": traceback.format_exc()[-500:]})
            except Exception:
                pass
            summary = {"diverged": False}
        if args.once:
            return 0
        elapsed = time.time() - last_tick
        wait = config.MIN_TICK_INTERVAL - elapsed
        if summary.get("diverged"):
            # Event wake: act fast, but never faster than the minimum
            # tick interval (protects API budgets when a gap persists).
            if wait > 0:
                print(f"[triad] divergence wake: next tick in {wait:.0f}s.",
                      flush=True)
                time.sleep(wait)
            else:
                print("[triad] divergence wake: acting immediately.",
                      flush=True)
        else:
            time.sleep(config.HEARTBEAT_INTERVAL)
        last_tick = time.time()


if __name__ == "__main__":
    sys.exit(main())
