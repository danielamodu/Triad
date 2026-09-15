"""Price-divergence signal: rToken vs BTC 24h performance gap.

Fetches both tickers via bgc and compares 24h change. A gap wider than
DIVERGENCE_THRESHOLD means the rToken decoupled from crypto beta.
"""
import config
from src import cli


def _change_24h(symbol: str) -> tuple[float, str]:
    """Return (fractional 24h change, last price) for a SPOT symbol."""
    rows = cli.tickers(config.SPOT_CATEGORY, symbol)
    row = next((r for r in rows
                if isinstance(r, dict)
                and str(r.get("symbol", "")).upper() == symbol.upper()),
               None)
    if row is None:
        raise cli.BgcError(f"{symbol} not found in SPOT tickers")
    pct = None
    for key in ("price24hPcnt", "change24h", "changePercent"):
        if row.get(key) not in (None, ""):
            pct = cli.fnum(row[key])
            break
    if pct is None:
        raise cli.BgcError(f"no 24h change field for {symbol}")
    if abs(pct) > 1:  # API returns percent (e.g. 2.49 = +2.49%)
        pct = pct / 100.0
    last = cli.fnum(row.get("lastPrice", row.get("last", 0)))
    return pct, str(row.get("lastPrice", row.get("last", "")))


def get_divergence() -> dict:
    """Scan the rToken basket against BTC and return the widest gap.

    Returns {signal, direction, divergence_score, rtoken_change,
    crypto_change, rtoken_last, crypto_last, selected_rtoken,
    basket_scores}. signal is DIVERGENCE_DETECTED | STABLE.

    selected_rtoken is the basket symbol with the highest absolute
    divergence (rtoken_change - crypto_change). basket_scores maps
    each scannable symbol -> its gap (rounded). Symbols that fail
    to fetch are skipped; if none scan, raises BgcError.
    """
    basket = list(getattr(config, "RTOKEN_BASKET", [config.RTOKEN_SYMBOL])
                  or [config.RTOKEN_SYMBOL])
    crypto_change, crypto_last = _change_24h(config.CRYPTO_SYMBOL)
    basket_scores: dict = {}
    best_symbol = ""
    best_gap = 0.0
    best_change = 0.0
    best_last = ""
    for symbol in basket:
        symbol = str(symbol or "").upper()
        if not symbol:
            continue
        try:
            change, last = _change_24h(symbol)
        except Exception:
            continue
        gap = change - crypto_change
        basket_scores[symbol] = round(gap, 6)
        if not best_symbol or abs(gap) > abs(best_gap):
            best_symbol = symbol
            best_gap = gap
            best_change = change
            best_last = last
    if not best_symbol:
        raise cli.BgcError("no basket rToken scannable")
    detected = abs(best_gap) >= config.DIVERGENCE_THRESHOLD
    if best_gap > 0:
        direction = "RTOKEN_OUTPERFORMING"
    elif best_gap < 0:
        direction = "CRYPTO_OUTPERFORMING"
    else:
        direction = "FLAT"
    return {
        "signal": "DIVERGENCE_DETECTED" if detected else "STABLE",
        "direction": direction,
        "divergence_score": round(best_gap, 6),
        "rtoken_change": round(best_change, 6),
        "crypto_change": round(crypto_change, 6),
        "rtoken_last": best_last,
        "crypto_last": crypto_last,
        "selected_rtoken": best_symbol,
        "basket_scores": basket_scores,
    }
