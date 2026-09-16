"""JSONL trade log. One JSON object per line. Secrets are scrubbed."""
import json
import os
from datetime import datetime, timezone

import config

_SECRET_HINTS = ("APIKEY", "API_KEY", "SECRET", "PASSPHRASE")


def _scrub(obj):
    if isinstance(obj, dict):
        return {k: ("***" if any(h in str(k).upper() for h in _SECRET_HINTS)
                    else _scrub(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def append_jsonl(rel_path: str, entry: dict) -> str:
    """Append one scrubbed entry to a JSONL file under BASE_DIR.

    Used for the trade log and the Groq audit trace alike. Creates
    parent dirs. Returns the file path. Never raises on bad input
    (raises on I/O: callers decide whether a failed write halts).
    """
    path = os.path.join(config.BASE_DIR, rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record = {"timestamp": datetime.now(timezone.utc).isoformat(),
              **_scrub(entry if isinstance(entry, dict) else {})}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    return path


def append_log(entry: dict) -> str:
    """Append one entry. Required keys: signal_inputs, decision,
    action_taken, symbols, reasoning. Returns the file path."""
    return append_jsonl(config.LOG_FILE, entry)


def _read_entries(log_path: str = "") -> list:
    """Read all valid JSON entries from logs/trades.jsonl."""
    path = log_path or os.path.join(config.BASE_DIR, config.LOG_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return []
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _entry_pnl(entry: dict) -> float:
    """Per-entry PnL: prefers equity_pnl (realized + unrealized), then
    running_pnl, pnl/total_pnl, then the sum of open-position pnls."""
    for key in ("equity_pnl", "running_pnl", "pnl", "total_pnl"):
        try:
            val = entry.get(key)
            if val is not None:
                return float(val)
        except (TypeError, ValueError):
            continue
    try:
        positions = entry.get("positions")
        if isinstance(positions, dict):
            positions = list(positions.values())
        if isinstance(positions, list):
            return float(sum(float(p.get("pnl", 0) or 0)
                             for p in positions if isinstance(p, dict)))
    except (TypeError, ValueError):
        pass
    return 0.0


def bot_entry_pnl(entry: dict) -> float:
    """Bot-attributed all-time PnL for one log entry: realized closes plus
    unrealized on bot-opened legs only.

    Adopted (reconciled) wallet legs are excluded — their price drift is
    not trading performance. Entries without a usable positions list
    report realized closes only. Never raises."""
    try:
        entry = entry if isinstance(entry, dict) else {}
        try:
            realized = float(entry.get("realized_pnl", 0.0) or 0.0)
        except (TypeError, ValueError):
            realized = 0.0
        positions = entry.get("positions")
        if isinstance(positions, dict):
            positions = list(positions.values())
        if not isinstance(positions, list):
            return round(realized, 4)  # no legs recorded: closed P&L only
        bot_open = 0.0
        for pos in positions:
            if not isinstance(pos, dict) or pos.get("reconciled"):
                continue
            try:
                bot_open += float(pos.get("pnl", 0) or 0)
            except (TypeError, ValueError):
                continue
        return round(realized + bot_open, 4)
    except Exception:
        return 0.0


def get_stats(log_path: str = "") -> dict:
    """Summary stats over all entries in logs/trades.jsonl.

    Returns {total_ticks, total_trades, win_rate, avg_confidence,
    total_pnl, max_drawdown, sharpe_estimate, first_tick, last_tick}.

    total_trades counts ticks with an executed action. win_rate is the
    share of executed ticks sitting in bot profit (bot_entry_pnl > 0).
    total_pnl is the last bot-attributed PnL. max_drawdown is the largest
    peak-to-trough drop of the bot pnl curve (>= 0). sharpe_estimate is
    mean/std of per-tick pnl deltas scaled by sqrt(N). Missing/empty log
    returns zeros with "" timestamps. Never raises on corrupt lines.
    """
    entries = _read_entries(log_path)
    total_ticks = len(entries)
    if not entries:
        return {"total_ticks": 0, "total_trades": 0, "win_rate": 0.0,
                "avg_confidence": 0.0, "total_pnl": 0.0,
                "max_drawdown": 0.0, "sharpe_estimate": 0.0,
                "first_tick": "", "last_tick": ""}

    total_trades = sum(1 for e in entries
                       if isinstance(e.get("action_taken"), dict)
                       and e["action_taken"].get("executed"))

    wins = sum(1 for e in entries
               if isinstance(e.get("action_taken"), dict)
               and e["action_taken"].get("executed")
               and bot_entry_pnl(e) > 0)
    win_rate = round(wins / total_trades, 4) if total_trades else 0.0

    confs = []
    for e in entries:
        try:
            confs.append(float((e.get("decision") or {}).get("confidence", 0)
                               or 0))
        except (TypeError, ValueError):
            continue
    avg_confidence = round(sum(confs) / len(confs), 4) if confs else 0.0

    curve = [bot_entry_pnl(e) for e in entries]
    total_pnl = round(curve[-1], 4)

    peak = curve[0]
    max_drawdown = 0.0
    for value in curve:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, peak - value)
    max_drawdown = round(max_drawdown, 4)

    sharpe_estimate = 0.0
    if len(curve) >= 2:
        diffs = [curve[i] - curve[i - 1] for i in range(1, len(curve))]
        mean = sum(diffs) / len(diffs)
        var = sum((d - mean) ** 2 for d in diffs) / len(diffs)
        std = var ** 0.5
        if std > 0:
            sharpe_estimate = round(mean / std * (len(diffs) ** 0.5), 4)

    return {"total_ticks": total_ticks, "total_trades": total_trades,
            "win_rate": win_rate, "avg_confidence": avg_confidence,
            "total_pnl": total_pnl, "max_drawdown": max_drawdown,
            "sharpe_estimate": sharpe_estimate,
            "first_tick": str(entries[0].get("timestamp", "") or ""),
            "last_tick": str(entries[-1].get("timestamp", "") or "")}
