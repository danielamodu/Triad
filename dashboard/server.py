"""Triad dashboard server: read-only views over the agent's logs.

    python dashboard/server.py        # serve on port 8080

API routes (all GET, read-only, CORS-enabled):
    /state    -> latest log entry as JSON ({} when empty)
    /logs     -> last 20 log entries as a JSON array (Past calls)
    /stats    -> get_stats() summary object (asset / bet / size / profit so far)
    /equity   -> full profit chart [{t, equity, executed}]
    /risk     -> safety-limits ledger + gate levels {exposure = gross open
                 inventory (money in play), exposure_bot = bot-deployed net,
                 adopted baseline (transparency), realized_pnl,
                 drawdown_pct (drop from peak), day_loss_pct,
                 limits, broker_streak (failed orders), kill_present
                 (Emergency stop)}
    /groq     -> last 20 AI-check trace rows, prompt/raw truncated
                 (decided by AI/Backup rules, answer speed = latency_ms)
    /backtest -> cached backtest report ({} when absent)
    /health   -> {alive, mode (Live · practice), kill_present (Emergency stop),
                 last_tick_age_s (next decision countdown)}

Frontend: when dashboard/dist exists (built Manus bundle), / serves
it with SPA fallback (/dashboard, /docs -> index.html); otherwise the
legacy debug dashboard/index.html is served. Stdlib only.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import config  # noqa: E402
from src.logger import bot_entry_pnl, get_stats  # noqa: E402
from src.risk.state import bot_exposure  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
LOG_PATH = os.path.join(BASE_DIR, config.LOG_FILE)
INDEX_PATH = os.path.join(BASE_DIR, "dashboard", "index.html")
DIST_DIR = os.path.join(BASE_DIR, "dashboard", "dist")
TRACE_PATH = os.path.join(BASE_DIR, config.GROQ_TRACE_FILE)

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
        ".css": "text/css", ".svg": "image/svg+xml",
        ".png": "image/png", ".ico": "image/x-icon",
        ".json": "application/json", ".woff2": "font/woff2",
        ".woff": "font/woff", ".ttf": "font/ttf"}


def _read_entries() -> list:
    try:
        with open(LOG_PATH, encoding="utf-8") as fh:
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


def _equity_curve() -> list:
    """Full profit chart (bot-attributed all-time PnL) for the hero chart.

    Adopted wallet drift is excluded via bot_entry_pnl so the chart
    tracks trading performance. Never raises."""
    try:
        return [{"t": e.get("timestamp", ""),
                 "equity": round(bot_entry_pnl(e), 4),
                 "executed": bool((e.get("action_taken") or {})
                                  .get("executed", False))}
                for e in _read_entries()]
    except Exception:
        return []


def _risk_view() -> dict:
    """Ledger + gate levels for the risk panel. Never raises.

    exposure is gross open inventory (all capital the bot has working,
    adopted funds included) — the money-in-play card basis. exposure_bot
    (ledger minus adopted baseline, floored at 0) and adopted are included
    for transparency. Gates read the full ledger; profit stays
    bot-attributed (see bot_entry_pnl)."""
    view: dict = {"exposure": {}, "realized_pnl": 0.0,
                  "drawdown_pct": 0.0, "day_loss_pct": 0.0,
                  "broker_streak": 0,
                  "limits": {"max_position_usd": config.RISK_MAX_POSITION_USD,
                             "max_drawdown_pct": config.RISK_MAX_DRAWDOWN_PCT,
                             "max_daily_loss_pct":
                             config.RISK_MAX_DAILY_LOSS_PCT},
                  "kill_present": os.path.exists(config.KILL_FILE)}
    try:
        with open(config.RISK_STATE_FILE, encoding="utf-8") as fh:
            state = json.load(fh)
        if isinstance(state, dict):
            gross = state.get("exposure", {}) or {}
            gross = gross if isinstance(gross, dict) else {}
            view["exposure"] = gross
            view["exposure_gross"] = gross
            adopted = state.get("adopted", {}) or {}
            view["adopted"] = adopted if isinstance(adopted, dict) else {}
            view["exposure_bot"] = bot_exposure(state)
            view["realized_pnl"] = float(state.get("realized_pnl", 0.0)
                                        or 0.0)
            view["broker_streak"] = int(state.get("broker_fail_streak", 0)
                                       or 0)
            view["groq_streak"] = int(state.get("groq_streak", 0) or 0)
            view["groq_cooldown"] = int(state.get("groq_cooldown", 0) or 0)
    except (OSError, ValueError, TypeError):
        pass
    try:
        entries = _read_entries()
        if entries:
            view["drawdown_pct"] = round(float(
                entries[-1].get("drawdown_pct", 0.0) or 0.0), 6)
            try:
                with open(config.RISK_STATE_FILE, encoding="utf-8") as fh:
                    state = json.load(fh)
                start = float((state or {}).get("day_start_pnl", 0.0) or 0.0)
                peak_exp = float((state or {}).get("peak_exposure_usd", 0.0)
                                 or 0.0)
                base = max(float(config.RISK_MAX_POSITION_USD), peak_exp,
                           1e-9)
                view["day_loss_pct"] = round(
                    (start - bot_entry_pnl(entries[-1])) / base, 6)
            except (OSError, ValueError, TypeError):
                pass
    except (TypeError, ValueError):
        pass
    return view


def _groq_view() -> list:
    """Last 20 trace rows, prompts/raw trimmed for the wire. Never
    raises (missing trace file -> [])."""
    out = []
    try:
        with open(TRACE_PATH, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh.read().split("\n")
                     if ln.strip()]
    except OSError:
        return []
    for line in lines[-20:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        prompt = str(row.get("prompt", "") or "")
        raw = str(row.get("raw_response", "") or "")
        out.append({"t": row.get("timestamp", ""),
                    "model": row.get("model", ""),
                    "decision": row.get("decision", ""),
                    "confidence": row.get("confidence", 0),
                    "fallback_decision": row.get("fallback_decision", ""),
                    "fallback_agree": row.get("fallback_agree"),
                    "latency_ms": row.get("latency_ms", 0),
                    "prompt": prompt[:500], "raw_response": raw[:500]})
    return out


def _backtest_view() -> dict:
    """Cached backtest report ({} when absent). Never raises."""
    path = os.path.join(BASE_DIR, "backtest", "data", "report.json")
    try:
        with open(path, encoding="utf-8") as fh:
            rep = json.load(fh)
        if isinstance(rep, dict):
            return {k: rep.get(k) for k in
                    ("n_days", "first_day", "last_day", "total_pnl",
                     "return_pct", "max_drawdown_pct", "n_trades",
                     "win_rate", "profit_factor", "buckets",
                     "benchmark_bh_pnl")}
    except (OSError, ValueError):
        pass
    return {}


def _health_view() -> dict:
    """Liveness for the header dot. Never raises."""
    mode, last_age = "paper", -1.0
    try:
        entries = _read_entries()
        if entries:
            last = entries[-1]
            mode = str(last.get("mode", "paper") or "paper")
            ts = str(last.get("timestamp", "") or "")
            if ts:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                last_age = round((time.time() - dt.timestamp()), 1)
    except Exception:
        pass
    return {"alive": last_age >= 0,
            "mode": "HALTED" if os.path.exists(config.KILL_FILE) else mode,
            "kill_present": os.path.exists(config.KILL_FILE),
            "last_tick_age_s": last_age}


class Handler(BaseHTTPRequestHandler):
    server_version = "TriadDashboard/1.0"

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _send_file(self, disk_path: str, mime: str) -> None:
        try:
            with open(disk_path, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send_json({"error": "not found"}, status=404)
            return
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        if "/assets/" in disk_path.replace("\\", "/"):
            # Hashed bundle filenames: safe to cache forever.
            self.send_header("Cache-Control",
                             "public, max-age=31536000, immutable")
        elif disk_path.endswith("index.html"):
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_spa(self, url_path: str) -> None:
        """Built frontend from dashboard/dist, SPA fallback to its
        index.html. Falls back to the legacy debug page when no
        bundle is deployed."""
        index = os.path.join(DIST_DIR, "index.html")
        if not os.path.exists(index):
            self._send_file(INDEX_PATH, "text/html; charset=utf-8")
            return
        if url_path in ("/", ""):
            self._send_file(index, "text/html; charset=utf-8")
            return
        candidate = os.path.normpath(os.path.join(
            DIST_DIR, url_path.lstrip("/")))
        if not candidate.startswith(DIST_DIR):
            self._send_json({"error": "not found"}, status=404)
            return
        if os.path.isdir(candidate):
            candidate = os.path.join(candidate, "index.html")
        if os.path.exists(candidate):
            _, ext = os.path.splitext(candidate)
            self._send_file(candidate, MIME.get(
                ext.lower(), "application/octet-stream"))
            return
        if "." not in os.path.basename(url_path):
            self._send_file(index, "text/html; charset=utf-8")
            return
        self._send_json({"error": "not found"}, status=404)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/state":
            entries = _read_entries()
            self._send_json(entries[-1] if entries else {})
        elif path == "/logs":
            entries = _read_entries()
            self._send_json(entries[-20:])
        elif path == "/stats":
            try:
                self._send_json(get_stats())
            except Exception as exc:
                self._send_json({"error": str(exc)[:200]}, status=500)
        elif path == "/equity":
            self._send_json(_equity_curve())
        elif path == "/risk":
            self._send_json(_risk_view())
        elif path == "/groq":
            self._send_json(_groq_view())
        elif path == "/backtest":
            self._send_json(_backtest_view())
        elif path == "/health":
            self._send_json(_health_view())
        elif path == "/index.html" and os.path.exists(
                os.path.join(DIST_DIR, "index.html")):
            self._serve_spa(path)
        elif path == "/" or path.startswith("/dashboard") \
                or path.startswith("/docs") or path.startswith("/assets"):
            self._serve_spa(path)
        else:
            # Static bundle assets (js/css/svg) or 404.
            dist_index = os.path.join(DIST_DIR, "index.html")
            if os.path.exists(dist_index):
                self._serve_spa(path)
            else:
                self._send_json({"error": "not found"}, status=404)

    def log_message(self, fmt, *args) -> None:
        sys.stderr.write("[dashboard] %s\n" % (fmt % args))


def main() -> int:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[dashboard] serving {BASE_DIR} on port {PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
