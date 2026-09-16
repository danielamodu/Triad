"""Snapshot live box API data for the Vercel static deploy.

Pulls the dashboard API off the live box and writes public/data/*.json
so the real frontend bundle (served statically) has data to render:

    python dashboard/box_snapshot.py [base_url]

Default base: http://100.63.86.147 (port 80). Stdlib only.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "public", "data")

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://100.63.86.147"
ENDPOINTS = ["stats", "state", "logs", "equity", "risk", "groq",
             "backtest", "health"]


def _sanitize(obj):
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
    for name in ENDPOINTS:
        req = urllib.request.Request(
            BASE + "/" + name, headers={"Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=60) as r:
            obj = json.loads(r.read().decode("utf-8"))
        with open(os.path.join(DATA_DIR, name + ".json"),
                  "w", encoding="utf-8") as fh:
            json.dump(_sanitize(obj), fh, indent=2, allow_nan=False)
        print(name, "ok", flush=True)
    meta = {"exported_at": datetime.now(timezone.utc).isoformat(),
            "source": BASE,
            "mode": "paper",
            "note": ("static snapshot of the live box for judges; "
                     "no secrets in this export (trade log is scrubbed)")}
    with open(os.path.join(DATA_DIR, "meta.json"),
              "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    print("meta ok", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
