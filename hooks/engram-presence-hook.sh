#!/usr/bin/env bash
# Engram auto-presence: pipe the hook payload to the spool writer, backgrounded
# so it never adds latency to a prompt. Always exits 0 (failure-soft).
set +e
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAYLOAD="$(cat)"
# macOS usually has no bare `python` — only `python3`. This hook is failure-soft
# (always exits 0), so a missing interpreter meant presence silently never got
# written and the routing table looked broken for reasons nobody could see.
# Prefer python3, fall back to python, and give up quietly if neither exists.
PY="$(command -v python3 || command -v python)"
[ -z "$PY" ] && exit 0
printf '%s' "$PAYLOAD" | "$PY" "$HOOK_DIR/engram_presence_hook.py" >/dev/null 2>&1 &
exit 0
