"""Engram turn-awareness hook (UserPromptSubmit).

Prints ONE line of context when a thread or room is waiting on this machine —
your turn to speak, or a question blocked on the person. Claude Code adds a
UserPromptSubmit hook's stdout to the model's context, so the session learns
"thread X is waiting on you" the moment the user talks to it, with zero tool
calls and zero polling.

WHY THIS EXISTS. A Claude Code session that has ended its turn cannot be woken —
there is no injection primitive, no equivalent of Pi's sendUserMessage. The only
moments it can learn anything are (a) inside a tool call and (b) when the user
prompts it. This hook covers (b): every prompt doubles as a checkpoint, which is
exactly when knowing "someone is waiting on you" changes what the session does
next. Native cross-session messaging covers the same-machine case; this covers
every case, because it rides on the server's own view.

DELIBERATELY A DIFFERENT HOOK from the presence spool writer. That one is
backgrounded with stdout discarded — correct for a write that must never add
latency, and useless for injection, since a backgrounded hook's output goes
nowhere. This one runs in the FOREGROUND and must therefore never touch the
network: it reads the status cache the status line already maintains
(~/.engram/status-cache.json) and nothing else. Sub-10ms, no sockets.

Failure-soft like every hook here: no cache, stale cache, no config — print
nothing, exit 0. A missing line is a missing convenience; a hook that errors
loudly on every prompt would be far worse than the silence it fixes.
"""

import json
import os
import sys
import time

CACHE = os.path.join(os.path.expanduser("~"), ".engram", "status-cache.json")
# Older than this and the world has likely moved on — say nothing rather than
# report a turn someone may already have taken. The status line refreshes the
# cache every ~30s while any session is rendering, so a fresh cache is the
# normal case on a machine that is in use.
STALE_AFTER = 180  # seconds


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    try:
        with open(CACHE, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except Exception:
        return
    if time.time() - float(cached.get("at") or 0) > STALE_AFTER:
        return

    parts = []
    turn = [str(t) for t in (cached.get("your_turn") or []) if t]
    if turn:
        parts.append(
            # ACCOUNT-level nudge, session-level verification. This hook reads a
            # per-machine cache that cannot know WHICH session a floor belongs to,
            # so it must not say "your turn" — the session it fires in may never
            # have joined that conversation. It names the fact and points at the
            # calls whose answer IS session-accurate: kb_threads()'s
            # waiting_on_you / floor.is_you are computed against this session's
            # own speaker key.
            "a turn is waiting somewhere on this account: " + ", ".join(turn[:3])
            + " — kb_threads() / kb_rooms() tells you whether it is YOURS "
            "(waiting_on_you / your_turn per conversation)"
        )
    asks = [str(a) for a in (cached.get("needs_you") or []) if a]
    if asks:
        parts.append("blocked on the user: " + ", ".join(asks[:3]))
    if parts:
        # One line, prefixed so the model knows the source. This is CONTEXT, not
        # an instruction — the session decides whether now is the moment.
        sys.stdout.write("[engram] " + " · ".join(parts) + "\n")


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.exit(0)
