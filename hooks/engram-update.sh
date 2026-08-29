#!/usr/bin/env bash
# Engram self-update: bring this machine's hooks and skills level with the
# server. Safe to run from launchd/cron/Task Scheduler — it exits 0 whether it
# updated, was already current, or could not reach the server.
set +e
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3 || command -v python)"
[ -z "$PY" ] && exit 0
"$PY" "$HOOK_DIR/engram_update.py"
exit 0
