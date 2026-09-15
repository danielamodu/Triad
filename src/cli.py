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
        for key in ("items", "list", "rows", "data"):
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
