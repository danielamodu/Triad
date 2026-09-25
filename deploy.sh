#!/usr/bin/env bash
# Triad bot deploy, in one shot: pull -> test -> bounce main.py -> verify.
# Same shape as the dashboard-bounce one-liner: no manual tmux dance, just run it.
#
# Paper is the default; any flags pass straight through to main.py
# (e.g. `./deploy.sh --live`, which still needs TRIAD_LIVE_OK=1 and no
# logs/KILL — main.py enforces that itself).
#
# By default the bot is relaunched DETACHED via nohup (survives logout),
# logging to bot.log. To keep it in the current pane (tmux) instead, run
# `RELAUNCH=exec ./deploy.sh`. If the tests fail the old bot stays stopped;
# fix the failure and run again.
set -euo pipefail

cd "$(dirname "$0")"

# Prefer the venv interpreter if it's here (override with PYTHON=...).
PYTHON="${PYTHON:-python}"
[ -x .venv/bin/python ] && PYTHON=.venv/bin/python
echo "[deploy] repo $(pwd) | python: $PYTHON"

# Match the running bot by interpreter+script, in ANY launch form
# (`python main.py` or `python /home/ubuntu/Triad/main.py`), so a redeploy
# always replaces every old instance. "python" keeps it off the dashboard
# (server.py) and an editor that merely has main.py open.
BOT_PAT='python.*main\.py'

# Refuse to deploy over uncommitted tracked changes. Runtime data (logs/,
# bot.log) is gitignored, so it never blocks this.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "[deploy] ABORT: uncommitted changes to tracked files; stash or commit first:" >&2
    git status --short >&2
    exit 1
fi

echo "[deploy] pulling latest (fast-forward only)..."
git pull --ff-only

echo "[deploy] running tests..."
"$PYTHON" -m pytest tests -q

# Stop the running bot and wait for it to actually die.
PIDS="$(pgrep -f "$BOT_PAT" || true)"
if [ -n "$PIDS" ]; then
    echo "[deploy] stopping bot: $PIDS"
    kill $PIDS || true
    for _ in 1 2 3 4 5 6; do
        sleep 2
        pgrep -f "$BOT_PAT" >/dev/null || break
    done
    if pgrep -f "$BOT_PAT" >/dev/null; then
        echo "[deploy] ABORT: bot did not stop (still: $(pgrep -f "$BOT_PAT" | tr '\n' ' '))." >&2
        exit 1
    fi
else
    echo "[deploy] no running bot found; starting fresh."
fi

# Foreground option for a tmux pane: exec keeps the fresh process in the pane.
if [ "${RELAUNCH:-nohup}" = "exec" ]; then
    echo "[deploy] tests green — launching in foreground (paper unless flags given)..."
    exec "$PYTHON" main.py "$@"
fi

# Default: relaunch detached, logging to bot.log.
echo "[deploy] tests green — launching detached -> bot.log (paper unless flags given)..."
nohup "$PYTHON" main.py "$@" > bot.log 2>&1 &
sleep 8
if pgrep -f "$BOT_PAT" >/dev/null; then
    echo "[deploy] bot up (pid $(pgrep -f "$BOT_PAT" | tr '\n' ' ')). Recent log:"
    tail -n 15 bot.log || true
else
    echo "[deploy] ERROR: bot not running after start; last log:" >&2
    tail -n 30 bot.log >&2 || true
    exit 1
fi
