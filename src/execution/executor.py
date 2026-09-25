"""Order execution via bgc (paper by default, live via --live gate).

Decision map:
  LONG_RTOKEN  -> spot market BUY  selected rToken leg (qty in USDT)
  HEDGE_CRYPTO -> spot market SELL CRYPTO_SYMBOL (trim crypto exposure)
  EXIT         -> spot market SELL each open leg (flatten)
  HOLD         -> no-op
"""
import uuid

import config
from src import cli

# ── Confidence-scaled position sizing ────────────────────────────
# Below MID_CONF_T -> skip (LOW_CONFIDENCE_SKIP). At/above it, size
# scales linearly with conviction from SIZE_FLOOR (at MID_CONF_T) up to
# the risk-cage ceiling (RISK_MAX_POSITION_USD, at confidence 1.0), so
# every trade is sized to its conviction instead of snapping to a fixed
# tier. This is what a real book looks like: continuous sizes, not two.
# (Was a 2-bucket step ($500/$1000) — every trade came out identical,
# which read as synthetic. Same risk envelope, smooth in between.)
MID_CONF_T = 0.6      # below this, no trade
SIZE_FLOOR = 250.0    # smallest live order, placed at exactly MID_CONF_T


def size_for_confidence(confidence) -> float:
    """Map decision confidence -> position size in USD.

    Continuous: 0.0 below MID_CONF_T (skip), then SIZE_FLOOR rising
    linearly to RISK_MAX_POSITION_USD at confidence 1.0. Capped at the
    risk ceiling. Never raises.
    """
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        return 0.0
    if conf < MID_CONF_T:
        return 0.0
    cap = float(config.RISK_MAX_POSITION_USD)
    floor = min(SIZE_FLOOR, cap)
    span = max(1e-9, 1.0 - MID_CONF_T)
    frac = min(1.0, max(0.0, (conf - MID_CONF_T) / span))
    return round(min(cap, floor + (cap - floor) * frac), 2)


def _base_coin(symbol: str) -> str:
    return symbol.upper().replace("USDT", "").replace("USDC", "")


def _account_rows(paper: bool = True):
    """Asset rows from account_overview.

    Returns None when the broker call itself failed (vs [] for a healthy
    but empty/parsable response) so callers can fail closed.
    """
    try:
        data = cli._run("account_overview", paper=paper)
    except Exception:
        return None
    if isinstance(data, dict):
        node = data.get("assets", {})
        if isinstance(node, dict):
            inner = node.get("data", node)
            if isinstance(inner, dict):
                rows = inner.get("assets", []) or []
                return [r for r in rows if isinstance(r, dict)]
    return []


def get_balance(coin: str, paper: bool = True) -> float:
    """Available balance for a coin (0.0 when missing/unreadable)."""
    try:
        want = str(coin or "").upper()
        for row in _account_rows(paper) or []:
            if str(row.get("coin", "")).upper() == want:
                return max(0.0, cli.fnum(row.get("available", 0)))
    except Exception:
        pass
    return 0.0


def get_positions(paper: bool = True) -> dict:
    """UTA balances -> {SYMBOL: {"usd": float}}. Only tracks the
    Triad universe (rToken + crypto legs).

    paper=False reads the LIVE account snapshot. Callers must obtain
    paper from the --live gate, never default it.

    A "__ok": True key marks a successful broker snapshot so callers can
    tell "empty account" apart from "broker call failed" (which returns
    {}).     Dunder keys are account metadata, not positions: strip them
    before treating the dict as a position book."""
    book: dict = {}
    rows = _account_rows(paper)
    if rows is None:
        return book  # no __ok: broker call failed (fail closed upstream)
    for leg in (config.RTOKEN_SYMBOL, config.CRYPTO_SYMBOL):
        base = _base_coin(leg)
        for row in rows:
            if str(row.get("coin", "")).upper() == base:
                try:
                    book[leg] = {"usd": float(row.get("usdValue", 0) or 0),
                                 "pnl_pct": 0.0}
                except (TypeError, ValueError):
                    pass
    book["__ok"] = True
    return book


def _qty_precision(symbol: str) -> int:
    """Exchange quantity decimals for a SPOT symbol (default 6)."""
    try:
        rows = cli.as_list(cli._run("market", "--action", "instruments",
                                     "--category", config.SPOT_CATEGORY,
                                     "--symbol", symbol))
        row = next((r for r in rows if isinstance(r, dict)
                    and str(r.get("symbol", "")).upper() == symbol.upper()),
                   rows[0] if rows else {})
        return max(0, int(float(row.get("quantityPrecision", 6))))
    except Exception:
        return 6


def _sell_qty_base(symbol: str, notional_usdt: float) -> str:
    from decimal import Decimal, ROUND_DOWN
    rows = cli.tickers(config.SPOT_CATEGORY, symbol)
    row = next((r for r in rows if isinstance(r, dict)
                and str(r.get("symbol", "")).upper() == symbol.upper()),
               rows[0] if rows else {})
    last = cli.fnum(row.get("lastPrice", row.get("last", 0)))
    if last <= 0:
        raise cli.BgcError(f"cannot size SELL {symbol}: no last price")
    precision = _qty_precision(symbol)
    qty = (Decimal(str(notional_usdt)) / Decimal(str(last))).quantize(
        Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)
    if qty <= 0:
        raise cli.BgcError(f"sized SELL {symbol} rounds to zero")
    return format(qty, "f")


def get_instrument(symbol: str, paper: bool = True) -> dict:
    """Instrument row for a SPOT symbol ({} when absent/unreadable)."""
    try:
        rows = cli.as_list(cli._run("market", "--action", "instruments",
                                     "--category", config.SPOT_CATEGORY,
                                     "--symbol", symbol, paper=paper))
        for row in rows:
            if isinstance(row, dict) and str(
                    row.get("symbol", "")).upper() == symbol.upper():
                return row
    except Exception:
        pass
    return {}


def validate_trade(symbol: str, side: str, notional_usdt: float,
                   qty_base: str = "", paper: bool = True) -> tuple:
    """Pre-trade checks. Returns (ok, reason); "" reason when ok.

    Verifies the symbol is listed and online, the size clears exchange
    minimums, and the available balance covers the order. Never raises.
    """
    try:
        symbol = (symbol or "").upper()
        side = (side or "").lower()
        try:
            notional = float(notional_usdt or 0)
        except (TypeError, ValueError):
            return False, "bad notional"
        if not symbol or side not in ("buy", "sell") or notional <= 0:
            return False, "bad order shape"
        inst = get_instrument(symbol, paper=paper)
        if not inst:
            return False, f"{symbol} not listed"
        if str(inst.get("status", "online")).lower() != "online":
            return False, (f"{symbol} not online "
                           f"(status={inst.get('status')})")
        if side == "buy":
            try:
                min_amt = float(inst.get("minOrderAmount", 1) or 1)
            except (TypeError, ValueError):
                min_amt = 1.0
            if notional < min_amt:
                return False, (f"notional ${notional:,.2f} < "
                               f"min ${min_amt:,.2f}")
            if get_balance("USDT", paper=paper) < notional:
                return False, "insufficient USDT balance"
        else:
            try:
                qty = float(qty_base or 0)
            except (TypeError, ValueError):
                qty = 0.0
            try:
                min_qty = float(inst.get("minOrderQty", 0) or 0)
            except (TypeError, ValueError):
                min_qty = 0.0
            if qty <= 0 or qty < min_qty:
                return False, f"sell qty {qty_base} < min {min_qty}"
            base = _base_coin(symbol)
            if get_balance(base, paper=paper) < qty:
                return False, f"insufficient {base} balance"
        return True, ""
    except Exception as exc:
        return False, f"validation error: {exc}"[:160]


def _is_retryable(exc: Exception) -> bool:
    """True when a failed place is worth one retry with the same clientOid.

    Matches broker-flagged retryable errors and transport failures. Never
    matches rejections (bad symbol, insufficient balance, ...). Never raises.
    """
    try:
        msg = str(exc or "").lower()
    except Exception:
        return False
    if '"retryable": true' in msg or '"retryable":true' in msg:
        return True
    return any(hint in msg for hint in
               ("timeout", "timed out", "network", "empty stdout",
                "econnreset", "rate limit", "429", "502", "503", "504"))


def _drift_ok(symbol: str, ref_price: float) -> tuple:
    """True when the live mark is within tolerance of the signal price.

    ref_price <= 0 skips the check (caller had no mark). A failed mark
    read also skips (don't block trading on a read failure). Never raises.
    """
    try:
        ref = float(ref_price or 0)
    except (TypeError, ValueError):
        return True, ""
    if ref <= 0:
        return True, ""
    try:
        rows = cli.tickers(config.SPOT_CATEGORY, symbol)
        row = next((r for r in rows if isinstance(r, dict)
                    and str(r.get("symbol", "")).upper() == symbol.upper()),
                   rows[0] if rows else {})
        last = cli.fnum(row.get("lastPrice", row.get("last", 0)))
    except Exception:
        return True, ""
    if last <= 0:
        return True, ""
    drift = abs(last - ref) / ref
    if drift > config.RISK_MAX_PRICE_DRIFT_PCT:
        return False, (f"{symbol} moved {drift:.2%} since signal "
                       f"({ref} -> {last})")
    return True, ""


def parse_fill(detail: dict) -> dict:
    """Extract fill facts from an order-detail payload.

    Returns {fill_price, fill_qty, fill_value, fee_usd, fee_coin,
    order_status}. Unknown/missing fields degrade to 0.0/"". Never raises.
    Observed shape: avgPrice, cumExecQty, cumExecValue, orderStatus,
    feeDetail=[{feeCoin, fee}].
    """
    out = {"fill_price": 0.0, "fill_qty": 0.0, "fill_value": 0.0,
           "fee_usd": 0.0, "fee_coin": "", "order_status": ""}
    try:
        if not isinstance(detail, dict):
            return out
        for key, field in (("avgPrice", "fill_price"),
                           ("averagePrice", "fill_price"),
                           ("cumExecQty", "fill_qty"),
                           ("executedQty", "fill_qty"),
                           ("cumExecValue", "fill_value"),
                           ("executedValue", "fill_value")):
            try:
                val = float(detail.get(key, 0) or 0)
            except (TypeError, ValueError):
                continue
            if val > 0 and out[field] <= 0:
                out[field] = val
        out["order_status"] = str(detail.get("orderStatus", "")
                                  or detail.get("status", "") or "")
        fees = detail.get("feeDetail", detail.get("fees", []))
        if isinstance(fees, dict):
            fees = [fees]
        total, coin = 0.0, ""
        if isinstance(fees, list):
            for row in fees:
                if not isinstance(row, dict):
                    continue
                try:
                    total += float(row.get("fee", row.get("amount", 0))
                                   or 0)
                except (TypeError, ValueError):
                    continue
                coin = coin or str(row.get("feeCoin", row.get("coin", ""))
                                   or "")
        out["fee_usd"] = round(total, 6)
        out["fee_coin"] = coin
        out["fill_price"] = round(out["fill_price"], 6)
        out["fill_qty"] = round(out["fill_qty"], 6)
        out["fill_value"] = round(out["fill_value"], 4)
        return out
    except Exception:
        return out


def _fetch_fill(order_id: str, paper: bool) -> tuple:
    """Best-effort order-detail fetch. Returns (fill, fetched_ok).

    fetched_ok False means the read itself failed (fill unknown), as
    opposed to a confirmed zero fill. Never raises.
    """
    try:
        if not order_id:
            return parse_fill({}), True
        return parse_fill(cli.get_order(order_id, paper=paper)), True
    except Exception:
        return parse_fill({}), False


def _cancel_quiet(order_id: str, symbol: str, paper: bool) -> None:
    """Best-effort cancel of a possibly-resting remainder. Never raises."""
    try:
        if order_id:
            cli.cancel_order(order_id, symbol=symbol, paper=paper)
    except Exception:
        pass


def execute(decision: dict, symbol: str, live: bool = False,
            ref_price: float = 0.0) -> dict:
    """Execute an approved decision. Returns {executed, order_id, details}.

    Pipeline: confidence sizing -> price-drift guard -> order sizing ->
    pre-trade validation -> place (one retry on retryable errors, same
    clientOid) -> fill settlement.

    Settlement: a broker-confirmed zero fill returns executed=False
    ("NO_FILL"); a partial fill cancels the remainder and returns
    executed=True with partial=True; an unreadable fill keeps
    executed=True with fill_unknown=True (the order exists; downstream
    falls back to signal prices). live=False (default) routes to demo;
    live=True must only come from main.py's --live gate.
    """
    import time

    name = str((decision or {}).get("decision", "HOLD")).upper()
    symbol = (symbol or "").upper()
    if name == "HOLD" or not symbol:
        return {"executed": False, "order_id": "",
                "details": "HOLD: nothing to do"}

    size = size_for_confidence((decision or {}).get("confidence", 0))
    if size <= 0:
        return {"executed": False, "order_id": "",
                "details": "LOW_CONFIDENCE_SKIP",
                "symbol": symbol, "side": "",
                "notional_usdt": 0.0, "position_size_usd": 0.0,
                "confidence": (decision or {}).get("confidence", 0)}
    notional = min(config.RISK_MAX_POSITION_USD, size)
    paper = not live
    side = "buy" if name == "LONG_RTOKEN" else \
        "sell" if name in ("HEDGE_CRYPTO", "EXIT") else ""
    if not side:
        return {"executed": False, "order_id": "",
                "details": f"unknown decision {name}"}
    base_result = {"symbol": symbol, "side": side,
                   "notional_usdt": notional,
                   "position_size_usd": notional,
                   "confidence": (decision or {}).get("confidence", 0),
                   "live": live, "partial": False, "fill_unknown": False}

    drift_ok, drift_reason = _drift_ok(symbol, ref_price)
    if not drift_ok:
        return {"executed": False, "order_id": "",
                "details": f"PRICE_DRIFT_ABORT: {drift_reason}"[:300],
                **base_result}

    try:
        if side == "buy":
            qty_arg, qty_base = str(notional), ""
        else:
            qty_arg = qty_base = _sell_qty_base(symbol, notional)
    except Exception as exc:
        return {"executed": False, "order_id": "",
                "details": f"execution failed: {exc}"[:300], **base_result}

    valid, reason = validate_trade(symbol, side, notional, qty_base, paper)
    if not valid:
        return {"executed": False, "order_id": "",
                "details": f"PRE_TRADE_BLOCKED: {reason}"[:300],
                **base_result}

    client_oid = "triad-" + uuid.uuid4().hex[:16]
    data, order_id, attempts = None, "", 0
    while True:
        attempts += 1
        try:
            data = cli.place_order(config.SPOT_CATEGORY, symbol, side,
                                   "market", qty_arg, dry_run=False,
                                   paper=paper, client_oid=client_oid)
            break
        except Exception as exc:
            if attempts <= 1 and _is_retryable(exc):
                time.sleep(2)
                continue
            return {"executed": False, "order_id": "",
                    "details": f"execution failed: {exc}"[:300],
                    **base_result, "client_oid": client_oid}
    if isinstance(data, dict):
        order_id = str(data.get("orderId", ""))

    fill, fetched = _fetch_fill(order_id, paper)
    if fetched and order_id and fill["fill_qty"] <= 0 \
            and fill["order_status"] != "filled":
        time.sleep(3)  # one re-check: fills can settle a heartbeat late
        fill, fetched = _fetch_fill(order_id, paper)
    if fetched and order_id and fill["fill_qty"] <= 0 \
            and fill["order_status"] != "filled":
        _cancel_quiet(order_id, symbol, paper)
        return {"executed": False, "order_id": order_id,
                "details": f"NO_FILL: broker confirms no fill "
                           f"(status={fill['order_status'] or '?'})"[:300],
                **base_result, "client_oid": client_oid, **fill}
    partial = bool(fetched and fill["fill_qty"] > 0
                   and fill["order_status"]
                   and fill["order_status"] != "filled")
    if partial:
        _cancel_quiet(order_id, symbol, paper)
    return {"executed": bool(order_id), "order_id": order_id,
            "details": data, **base_result, "client_oid": client_oid,
            "partial": partial,
            "fill_unknown": bool(order_id and not fetched), **fill}
