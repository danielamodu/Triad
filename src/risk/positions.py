"""Persisted bot position book (the cage's memory of open legs).

OPEN_POSITIONS lives in memory in main.py and, before this module,
vanished on restart. Reconcile then re-adopted the bot's own legs as
pre-existing wallet funds at the current mark, which silently:
  * disarmed per-leg brackets (stops/takes skip reconciled inventory),
  * reset entry prices, so unrealized PnL snapped to zero, and
  * dropped the legs from the bot-attributed profit card.

This file persists only BOT-opened legs. Adopted/reconciled inventory
is re-derived from the live broker snapshot on every boot, so it never
needs saving here. Stored at config.POSITIONS_FILE
(logs/positions.json), written atomically (tmp file + os.replace) so a
crash can never leave half a JSON document behind — the same discipline
as the risk ledger (state.py).

Schema (version 1):
  positions: {SYMBOL: {symbol, side, size_usd, entry_price,
                       current_price, pnl, usd}}
             side is "long"/"short"; reconciled legs are never written.

Missing file -> ({}, True): a fresh run with no open book is normal.
Unreadable/invalid -> ({}, False): the caller keeps today's behavior
(reconcile rebuilds the book from the broker) rather than trusting a
corrupt file.
"""
import json
import os

import config

VERSION = 1


def _coerce_leg(symbol, raw) -> dict | None:
    """Validate one persisted leg. Returns a clean dict or None.

    A leg needs a symbol and a positive size and entry price to be
    tradable/measurable; anything else is dropped. Never raises.
    """
    if not isinstance(raw, dict):
        return None
    sym = str(symbol or raw.get("symbol", "")).upper()
    if not sym:
        return None
    try:
        entry = float(raw.get("entry_price", 0.0) or 0.0)
        size = float(raw.get("size_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    if entry <= 0 or size <= 0:
        return None
    side = "short" if str(raw.get("side", "long")).lower() == "short" \
        else "long"
    try:
        current = float(raw.get("current_price", entry) or entry)
    except (TypeError, ValueError):
        current = entry
    try:
        pnl = float(raw.get("pnl", 0.0) or 0.0)
    except (TypeError, ValueError):
        pnl = 0.0
    return {"symbol": sym, "side": side, "size_usd": round(size, 4),
            "entry_price": entry, "current_price": current,
            "pnl": round(pnl, 4), "usd": round(size, 4)}


def load_positions(path: str = "") -> tuple:
    """Load the persisted bot book. Returns (positions, ok).

    Missing file -> ({}, True). Unreadable or invalid -> ({}, False) so
    the caller can fall back to rebuilding from the broker. Reconciled
    legs, should any have slipped into an old file, are dropped: this
    book is bot-only. Never raises.
    """
    target = path or config.POSITIONS_FILE
    try:
        with open(target, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        if not os.path.exists(target):
            return {}, True
        return {}, False
    if not isinstance(raw, dict) or raw.get("version") != VERSION:
        return {}, False
    legs = raw.get("positions")
    if not isinstance(legs, dict):
        return {}, False
    out: dict = {}
    for symbol, leg in legs.items():
        if isinstance(leg, dict) and leg.get("reconciled"):
            continue
        clean = _coerce_leg(symbol, leg)
        if clean is not None:
            out[clean["symbol"]] = clean
    return out, True


def save_positions(positions: dict, path: str = "") -> bool:
    """Atomically persist bot-opened legs. Returns True on success.

    Adopted (reconciled) legs are filtered out: they are rebuilt from
    the broker at boot and must never be trusted from a stale file.
    Never raises.
    """
    target = path or config.POSITIONS_FILE
    try:
        book: dict = {}
        for symbol, leg in (positions or {}).items():
            if not isinstance(leg, dict) or leg.get("reconciled"):
                continue
            clean = _coerce_leg(symbol, leg)
            if clean is not None:
                book[clean["symbol"]] = clean
        payload = {"version": VERSION, "positions": book}
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(tmp, target)
        return True
    except (OSError, TypeError, ValueError):
        return False
