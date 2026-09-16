"""Thin wrapper around the `bgc` CLI (paper-trading backend).

All parsing is defensive: unexpected shapes degrade to exceptions with
the raw payload attached, and signal modules convert those into a
neutral score instead of trading blind.

Trading mode: writes go to the Bitget demo environment when paper=True
(`--paper-trading` flag) and to the LIVE account when paper=False.
Reads are unaffected. Every write helper defaults to paper=True; live
must be explicitly threaded through from main.py's --live gate.
"""
import json
import shlex
import subprocess

import config


class BgcError(RuntimeError):
    pass


def _run(*argv: str, timeout: int = 30, paper: bool = True) -> dict:
    """Run `bgc <argv...>` and return the `data` payload.

    paper=True appends `--paper-trading` (demo). paper=False omits it,
    routing writes to the LIVE account -- only reachable via the --live
    gate in main.py.
    """
    import shutil
    bgc = shutil.which("bgc") or "bgc"
    cmd = [bgc, *argv]
    if paper:
        cmd.append("--paper-trading")
    # NOTE: pass a list (never a pre-quoted string): with shell=True
    # Python applies Windows-correct quoting itself. shlex.quote emits
    # POSIX single-quotes that cmd.exe rejects.
    proc = subprocess.run(
        cmd,
        shell=_needs_shell(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = (proc.stdout or "").strip()
    if not out:
        raise BgcError(f"empty stdout (rc={proc.returncode}): "
                       f"{(proc.stderr or '')[:300]}")
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise BgcError(f"non-JSON stdout: {out[:300]}") from exc
    if isinstance(payload, dict) and payload.get("ok") is False:
        err = payload.get("error", payload)
        raise BgcError(f"bgc error: {json.dumps(err)[:300]}")
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _needs_shell() -> bool:
    # On Windows `bgc` resolves to bgc.cmd, which needs cmd.exe.
    import os
    return os.name == "nt"


def check_cli() -> str:
    import shutil
    bgc = shutil.which("bgc") or "bgc"
    proc = subprocess.run(
        f"{bgc} --help" if _needs_shell() else [bgc, "--help"],
        shell=_needs_shell(),
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0 or "Bitget Agent CLI" not in (proc.stdout or ""):
        raise BgcError("bgc CLI not found or not working")
    return "ok"


def as_list(data) -> list:
    """Normalize bgc list-ish payloads to a python list."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "list", "rows", "data", "resultList",
                    "result"):
            if isinstance(data.get(key), list):
                return data[key]
        # single object -> one row
        return [data]
    return []


def fnum(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ── Market reads used by signals ─────────────────────────────────
def tickers(category: str, symbol: str = "") -> list:
    args = ["market", "--action", "tickers", "--category", category]
    if symbol:
        args += ["--symbol", symbol]
    return as_list(_run(*args))


def candles(category: str, symbol: str, interval: str, limit: int) -> list:
    return as_list(_run("market", "--action", "candles",
                        "--category", category, "--symbol", symbol,
                        "--interval", interval, "--limit", str(limit)))


def funding_rate(category: str, symbol: str) -> list:
    return as_list(_run("market", "--action", "fundingRate",
                        "--category", category, "--symbol", symbol))


def funding_rate_history(category: str, symbol: str,
                         limit: int = 100, cursor: str = "") -> list:
    """Historical funding rates, newest first: [{symbol, fundingRate,
    fundingRateTimestamp}]. Never raises (returns [] on failure).

    NOTE: paper must be False here. The demo backend hangs on this
    endpoint (found 2026-09-16: --paper-trading never returns); reads
    are account-independent anyway.
    """
    try:
        args = ["market", "--action", "fundingRateHistory",
                "--category", category, "--symbol", symbol,
                "--limit", str(limit)]
        if cursor:
            args += ["--cursor", str(cursor)]
        return as_list(_run(*args, paper=False))
    except Exception:
        return []


def candles_history(category: str, symbol: str, interval: str,
                    start_ms: int = 0, end_ms: int = 0,
                    limit: int = 100) -> list:
    """Historical klines in a [start, end) ms window (caller's job to
    sort). Rows: [ts, o, h, l, c, baseVol, quoteVol]. Never raises.

    NOTE: both bounds are required — the endpoint hangs without them
    (found 2026-09-16). Missing bounds return [] immediately.
    """
    if start_ms <= 0 or end_ms <= 0 or end_ms <= start_ms:
        return []
    try:
        return as_list(_run("market", "--action", "candlesHistory",
                            "--category", category, "--symbol", symbol,
                            "--interval", interval,
                            "--startTime", str(start_ms),
                            "--endTime", str(end_ms),
                            "--limit", str(limit), paper=False))
    except Exception:
        return []


def orderbook(category: str, symbol: str) -> dict:
    """Top-of-book snapshot ({} on failure). Never raises."""
    try:
        rows = as_list(_run("market", "--action", "orderbook",
                            "--category", category, "--symbol", symbol))
        return rows[0] if rows and isinstance(rows[0], dict) else {}
    except Exception:
        return {}


def top_spread_bps(category: str, symbol: str) -> float:
    """Best ask/bid spread in basis points (0.0 when unreadable)."""
    try:
        book = orderbook(category, symbol)
        bids = book.get("bids", book.get("bid", []))
        asks = book.get("asks", book.get("ask", []))
        bid = float(bids[0][0]) if bids and bids[0] else 0.0
        ask = float(asks[0][0]) if asks and asks[0] else 0.0
        if bid > 0 and ask > bid:
            return round((ask - bid) / ((ask + bid) / 2) * 10000, 3)
    except (TypeError, ValueError, IndexError):
        pass
    return 0.0


def open_interest(category: str, symbol: str = "") -> list:
    args = ["market", "--action", "openInterest", "--category", category]
    if symbol:
        args += ["--symbol", symbol]
    return as_list(_run(*args))


# ── Trading (execution module) ───────────────────────────────────
def place_order(category: str, symbol: str, side: str, order_type: str,
                qty: str, dry_run: bool = False, paper: bool = True,
                client_oid: str = "") -> dict:
    args = ["order", "--action", "place", "--category", category,
            "--symbol", symbol, "--side", side, "--orderType", order_type,
            "--qty", qty]
    if client_oid:
        args += ["--clientOid", client_oid]
    if dry_run:
        args.append("--dry-run")
    else:
        args.append("--confirm")
    data = _run(*args, timeout=30, paper=paper)
    if isinstance(data, dict):
        return data
    return {"result": data}


def get_order(order_id: str, paper: bool = True) -> dict:
    data = _run("order", "--action", "detail", "--orderId", order_id,
                paper=paper)
    return data if isinstance(data, dict) else {"result": data}


def cancel_order(order_id: str, symbol: str = "",
                 category: str = "SPOT", paper: bool = True) -> dict:
    """Best-effort cancel of one order. Callers must tolerate failure."""
    args = ["order", "--action", "cancel", "--category", category]
    if symbol:
        args += ["--symbol", symbol]
    args += ["--orderId", order_id, "--confirm"]
    data = _run(*args, timeout=30, paper=paper)
    return data if isinstance(data, dict) else {"result": data}


def open_orders(category: str = "SPOT", symbol: str = "",
                paper: bool = True) -> list:
    """List resting open orders (empty when nothing rests). Never raises."""
    try:
        args = ["order", "--action", "open", "--category", category]
        if symbol:
            args += ["--symbol", symbol]
        return as_list(_run(*args, paper=paper))
    except Exception:
        return []
