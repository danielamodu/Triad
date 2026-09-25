#!/usr/bin/env bash
# Triad deploy: pull -> test -> restart the systemd --user service.
#
# The bot runs as the `triad-agent` systemd --user service (auto-restart,
# survives logout via linger) — systemd owns the single instance. This script
# never launches its own copy; it pulls, tests, then asks systemd to restart
# the managed bot, so you can't end up with two bots double-writing the logs.
# Paper vs live lives in the unit's ExecStart, not here.
#
# Override the unit name with TRIAD_UNIT=... if yours differs.
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python}"
[ -x .venv/bin/python ] && PYTHON=.venv/bin/python
UNIT="${TRIAD_UNIT:-triad-agent}"
echo "[deploy] repo $(pwd) | python $PYTHON | unit $UNIT"

# Refuse to deploy over uncommitted tracked changes (logs/ is gitignored).
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "[deploy] ABORT: uncommitted changes to tracked files; stash or commit first:" >&2
    git status --short >&2
    exit 1
fi

echo "[deploy] pulling latest (fast-forward only)..."
git pull --ff-only

echo "[deploy] running tests..."
"$PYTHON" -m pytest tests -q

echo "[deploy] tests green — restarting $UNIT via systemd --user..."
systemctl --user restart "$UNIT.service"
sleep 6

echo "[deploy] status:"
systemctl --user --no-pager status "$UNIT.service" | head -n 12 || true

# Safety net: exactly one bot must be running, and it must be systemd's.
# A leftover manual/nohup copy would double-write the logs (the old bug).
mapfile -t BOTS < <(pgrep -f 'python.*main\.py' || true)
echo "[deploy] main.py processes: ${#BOTS[@]}"
for p in "${BOTS[@]}"; do
    u="$(grep -o '[a-zA-Z0-9_@-]*\.service' /proc/"$p"/cgroup 2>/dev/null | tail -1)"
    echo "    pid $p  unit=${u:-<stray!>}  :: $(ps -o cmd= -p "$p" 2>/dev/null)"
done
if [ "${#BOTS[@]}" -ne 1 ]; then
    echo "[deploy] WARNING: expected exactly 1 bot — kill any <stray!> above with 'kill <pid>'." >&2
fi
