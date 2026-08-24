#!/usr/bin/env bash
# Engram turn awareness — FOREGROUND on purpose, unlike the presence hook:
# a backgrounded hook's stdout goes nowhere, and stdout is the whole point here
# (Claude Code injects it as context on UserPromptSubmit). It only reads a local
# cache file, so it cannot add meaningful latency. Always exits 0.
set +e
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3 || command -v python)"
[ -z "$PY" ] && exit 0
"$PY" "$HOOK_DIR/engram_turns_hook.py"
exit 0
