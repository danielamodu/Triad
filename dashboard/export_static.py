"""Export a static judge snapshot of the Triad dashboard for Vercel.

Reads the live log files (gitignored) and writes a self-contained
static site to public/ (committed):

    python dashboard/export_static.py

Output (data only — the real frontend bundle in public/ is deployed
as-is and never overwritten here; use dashboard/box_snapshot.py for
fresh box data):

    python dashboard/export_static.py

Re-run + redeploy whenever you want a fresher snapshot. The live box
keeps running; this is only what the judge sees. Stdlib only.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from dashboard import server  # noqa: E402
from src.logger import get_stats  # noqa: E402

PUBLIC_DIR = os.path.join(BASE_DIR, "public")
DATA_DIR = os.path.join(PUBLIC_DIR, "data")

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Triad Dashboard</title>
<style>
  body { font-family: monospace; background: #0d1117; color: #c9d1d9; margin: 2em; }
  h1 { color: #58a6ff; }
  h2 { color: #8b949e; font-size: 1em; margin-top: 2em; }
  nav { display: flex; gap: 1.5em; margin: 1em 0; color: #8b949e; flex-wrap: wrap; }
  pre { background: #161b22; padding: 1em; overflow: auto; border-radius: 6px; }
  .row { display: flex; gap: 1em; flex-wrap: wrap; }
  .card { flex: 1 1 300px; }
  .badge { color: #3fb950; }
  .snap { color: #d29922; }
</style>
</head>
<body>
<h1>Triad Dashboard</h1>
<nav>
  <span>Trades</span>
  <span>AI check</span>
  <span>Past calls</span>
  <span>Safety limits</span>
  <span>Profit chart</span>
</nav>
<p class="snap" id="meta">Static snapshot for judges · Live · practice · loading…</p>
<p>
  <span id="mode" class="badge">Live · practice</span>
  · <span id="safety">within safe limits</span>
</p>
<div class="row">
  <div class="card"><h2>Profit (profit chart)</h2><pre id="equity">loading…</pre></div>
  <div class="card"><h2>Money in play (open)</h2><pre id="risk">loading…</pre></div>
</div>
<div class="row">
  <div class="card"><h2>Safety check passed? (Safety limits)</h2><pre id="state">loading…</pre></div>
  <div class="card"><h2>Decided by AI / Backup rules (AI check)</h2><pre id="groq">loading…</pre></div>
</div>
<div class="row">
  <div class="card"><h2>Stats — asset / bet / size / profit so far</h2><pre id="stats">loading…</pre></div>
  <div class="card"><h2>Health — failed orders · Emergency stop · answer speed</h2><pre id="health">loading…</pre></div>
</div>
<h2>Past calls — picked by AI/backup, safety passed, profit (See details)</h2>
<pre id="logs">loading…</pre>
<h2>Backtest (offline replay, deterministic)</h2>
<pre id="backtest">loading…</pre>
<script>
async function load(id, path) {
  try {
    const r = await fetch(path);
    const j = await r.json();
    document.getElementById(id).textContent = JSON.stringify(j, null, 2);
  } catch (e) {
    document.getElementById(id).textContent = "error: " + e;
  }
}
async function refresh() {
  load("stats", "data/stats.json");
  load("state", "data/state.json");
  load("logs", "data/logs.json");
  load("risk", "data/risk.json");
  load("groq", "data/groq.json");
  load("equity", "data/equity.json");
  load("health", "data/health.json");
  load("backtest", "data/backtest.json");
  try {
    const [m, h] = await Promise.all([
      fetch("data/meta.json").then(r => r.json()),
      fetch("data/health.json").then(r => r.json())
    ]);
    document.getElementById("meta").textContent =
      "Static snapshot for judges · exported " + m.exported_at +
      " · Live · practice · source: github.com/danielamodu/Triad";
    document.getElementById("mode").textContent =
      h.mode === "live" ? "Live" : "Live · practice";
    document.getElementById("safety").textContent =
      h.kill_present ? "Emergency stop ON" : "within safe limits";
  } catch (e) { /* keep defaults */ }
}
refresh();
</script>
</body>
</html>
"""


def _commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=15)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _sanitize(obj):
    """Make obj strict-JSON-safe: non-finite floats (Infinity/NaN, e.g.
    profit_factor with no losing trades) become readable strings."""
    if isinstance(obj, float):
        if obj != obj:
            return "NaN"
        if obj in (float("inf"), float("-inf")):
            return "infinite (no losing trades)" if obj > 0 else "-infinite"
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def main() -> int:
    os.makedirs(DATA_DIR, exist_ok=True)
    entries = server._read_entries()
    snap = {
        "stats": get_stats(),
        "state": entries[-1] if entries else {},
        "logs": entries[-20:],
        "equity": server._equity_curve(),
        "risk": server._risk_view(),
        "groq": server._groq_view(),
        "backtest": server._backtest_view(),
        "health": server._health_view(),
        "meta": {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "commit": _commit(),
            "ticks": len(entries),
            "mode": "paper",
            "note": ("static snapshot for judges; live box keeps ticking; "
                     "no secrets in this export (trade log is scrubbed)"),
        },
    }
    for name, obj in snap.items():
        if name == "meta":
            continue
        with open(os.path.join(DATA_DIR, name + ".json"),
                  "w", encoding="utf-8") as fh:
            json.dump(_sanitize(obj), fh, indent=2, allow_nan=False)
    print(f"[export] {len(entries)} ticks -> {DATA_DIR}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
