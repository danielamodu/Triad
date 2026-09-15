"""Order execution via bgc (paper by default, live via --live gate).

Decision map:
  LONG_RTOKEN  -> spot market BUY  RTOKEN_SYMBOL (qty in USDT)
  HEDGE_CRYPTO -> spot market SELL CRYPTO_SYMBOL (trim crypto exposure)
  EXIT         -> spot market SELL RTOKEN_SYMBOL (close rToken leg)
  HOLD         -> no-op
"""
import uuid

import config
from src import cli

# ── Confidence-based position sizing ─────────────────────────────
# confidence > 0.8  -> $1000
# confidence 0.6-0.8 -> $500
# confidence < 0.6  -> skip (LOW_CONFIDENCE_SKIP)
SIZE_HIGH_CONF = 1000.0
SIZE_MID_CONF = 500.0
HIGH_CONF_T = 0.8
MID_CONF_T = 0.6


def size_for_confidence(confidence) -> float:
    """Map decision confidence -> position size in USD.

    Returns 1000.0 / 500.0 / 0.0 (0.0 means skip). Never raises.
    """
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        return 0.0
    if conf > HIGH_CONF_T:
        return SIZE_HIGH_CONF
    if conf >= MID_CONF_T:
        return SIZE_MID_CONF
    return 0.0


def _base_coin(symbol: str) -> str:
    return symbol.upper().replace("USDT", "").replace("USDC", "")


def get_positions(paper: bool = True) -> dict:
    """UTA balances -> {SYMBOL: {"usd": float}}. Only tracks the
    Triad universe (rToken + crypto legs).

    paper=False reads the LIVE account snapshot. Callers must obtain
    paper from the --live gate, never default it.

    A "__ok": True key marks a successful broker snapshot so callers can
    tell "empty account" apart from "broker call failed" (which returns
    {}). Dunder keys are account metadata, not positions: strip them
    before treating the dict as a position book."""
    book: dict = {}
    try:
        data = cli._run("account_overview", paper=paper)
    except Exception:
        return book
    assets = []
    if isinstance(data, dict):
        node = data.get("assets", {})
        if isinstance(node, dict):
            inner = node.get("data", node)
            if isinstance(inner, dict):
                assets = inner.get("assets", []) or []
    for leg in (config.RTOKEN_SYMBOL, config.CRYPTO_SYMBOL):
        base = _base_coin(leg)
        for row in assets:
            if not isinstance(row, dict):
                continue
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


def _fetch_fill(order_id: str, paper: bool) -> dict:
    """Best-effort order-detail fetch. Returns parse_fill({}) on failure."""
    try:
        if not order_id:
            return parse_fill({})
        return parse_fill(cli.get_order(order_id, paper=paper))
    except Exception:
        return parse_fill({})


def execute(decision: dict, symbol: str, live: bool = False) -> dict:
    """Execute an approved decision. Returns {executed, order_id, details}.

    Confidence sizing: >0.8 -> $1000, 0.6-0.8 -> $500, <0.6 -> skip
    with details "LOW_CONFIDENCE_SKIP".

    live=False (default) routes to the demo environment. live=True
    trades the LIVE account and must only come from main.py's --live
    gate. Every order carries a triad- clientOid; on a fill the order
    detail is fetched so legs report fill_price/fee (falling back to
    signal prices downstream when the fetch fails).
    """
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
    side = ""
    client_oid = "triad-" + uuid.uuid4().hex[:16]
    try:
        if name == "LONG_RTOKEN":
            side = "buy"
            data = cli.place_order(config.SPOT_CATEGORY, symbol, "buy",
                                   "market", str(notional), dry_run=False,
                                   paper=paper, client_oid=client_oid)
        elif name in ("HEDGE_CRYPTO", "EXIT"):
            side = "sell"
            qty = _sell_qty_base(symbol, notional)
            data = cli.place_order(config.SPOT_CATEGORY, symbol, "sell",
                                   "market", qty, dry_run=False,
                                   paper=paper, client_oid=client_oid)
        else:
            return {"executed": False, "order_id": "",
                    "details": f"unknown decision {name}"}
    except Exception as exc:
        return {"executed": False, "order_id": "",
                "details": f"execution failed: {exc}"[:300]}

    order_id = ""
    if isinstance(data, dict):
        order_id = str(data.get("orderId", ""))
    fill = _fetch_fill(order_id, paper) if order_id else parse_fill({})
    return {"executed": bool(order_id), "order_id": order_id,
            "details": data, "symbol": symbol, "side": side,
            "notional_usdt": notional, "position_size_usd": notional,
            "client_oid": client_oid, "live": live, **fill}
