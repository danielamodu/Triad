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
from src.logger import append_log
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
        })
        del MEMORY[:-10]
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


def _sync_positions(action_taken: dict, price_signal: dict,
                    decision_name: str = "") -> None:
    """Open/close in-memory positions from executed fills. Never raises."""
    try:
        if not isinstance(action_taken, dict) or not action_taken.get(
                "executed"):
            return
        if str(decision_name or "").upper() == "EXIT":
            OPEN_POSITIONS.clear()
            return
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
            return
        for leg in legs:
            symbol = str(leg.get("symbol", "")).upper()
            side = str(leg.get("side", "")).lower()
            try:
                notional = float(leg.get("notional_usdt", 0) or 0)
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
                    # Closing a tracked leg flattens it.
                    del OPEN_POSITIONS[symbol]
                else:
                    OPEN_POSITIONS[symbol] = {
                        "symbol": symbol, "side": "short",
                        "size_usd": notional, "entry_price": entry_price,
                        "current_price": current, "pnl": 0.0,
                        "usd": notional}
    except Exception:
        pass


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
    global _STATE_SAVE_OK
    rstate, state_ok = risk_state.load_state()
    today = datetime.now(timezone.utc).date().isoformat()
    risk_state.roll_day(rstate, today, pre_pnl)

    try:
        account = get_positions(paper=not live)
    except Exception as exc:
        account = {"__error": str(exc)[:160]}
    broker_ok = (isinstance(account, dict)
                 and account.get("__ok", False) is True)
    broker_streak = risk_state.note_broker(rstate, broker_ok)

    drawdown = risk_state.drawdown_pct(rstate, pre_pnl)
    day_loss = risk_state.day_loss_pct(rstate, pre_pnl)
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
                      _memory_context(3))

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
                                        symbol, live=live))
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

    _sync_positions(action_taken, price, risk.get("decision", ""))
    _update_positions(price)
    running_pnl = _running_pnl()
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

    last_tick = 0.0
    while True:
        try:
            summary = tick(live=live)
        except Exception:
            traceback.print_exc()
            try:
                append_log({"signal_inputs": {}, "decision": {},
                            "action_taken": {"executed": False,
                                             "details": "tick crashed"},
                            "symbols": SYMBOLS,
                            "positions": list(OPEN_POSITIONS.values()),
                            "running_pnl": _running_pnl(),
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
