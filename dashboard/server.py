"""Triad dashboard server: read-only views over logs/trades.jsonl.

    python dashboard/server.py        # serve on port 8080

Routes:
    GET /       -> dashboard/index.html (404 JSON if missing)
    GET /state  -> latest log entry as JSON ({} when empty)
    GET /logs   -> last 20 log entries as a JSON array
    GET /stats  -> get_stats() summary object

CORS is enabled for all routes. Stdlib only.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import config  # noqa: E402
from src.logger import get_stats  # noqa: E402

PORT = 8080
LOG_PATH = os.path.join(BASE_DIR, config.LOG_FILE)
INDEX_PATH = os.path.join(BASE_DIR, "dashboard", "index.html")


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

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            try:
                with open(INDEX_PATH, "rb") as fh:
                    body = fh.read()
            except OSError:
                self._send_json({"error": "dashboard/index.html not found"},
                                status=404)
                return
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/state":
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
        else:
            self._send_json({"error": "not found"}, status=404)

    def log_message(self, fmt, *args) -> None:
        sys.stderr.write("[dashboard] %s\n" % (fmt % args))


def main() -> int:
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[dashboard] serving {BASE_DIR} on port {PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
