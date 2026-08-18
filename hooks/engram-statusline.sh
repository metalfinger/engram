#!/usr/bin/env bash
# Engram status line — prints one line of ambient situation for Claude Code.
# Always exits 0: a broken status line must never make Claude Code look broken.
set +e
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# macOS usually has no bare `python`, only `python3` — the same trap that made the
# presence hook install cleanly on a Mac and silently write nothing.
PY="$(command -v python3 || command -v python)"
[ -z "$PY" ] && exit 0
"$PY" "$HOOK_DIR/engram_statusline.py"
exit 0
