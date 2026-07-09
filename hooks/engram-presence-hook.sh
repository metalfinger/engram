#!/usr/bin/env bash
# Engram auto-presence: pipe the hook payload to the spool writer, backgrounded
# so it never adds latency to a prompt. Always exits 0 (failure-soft).
set +e
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAYLOAD="$(cat)"
printf '%s' "$PAYLOAD" | python "$HOOK_DIR/engram_presence_hook.py" >/dev/null 2>&1 &
exit 0
