"""Server capability manifest — surfaced in kb_load so ANY session (even a stale
chat whose tool list predates the latest deploy) can tell it's behind and advise
the user to start a fresh chat for the newest tools.

MCP negotiates a chat's tool list once at connection, so a server can't inject new
tools into a running chat. But kb_load runs in every session and reads this live,
so Claude can compare the server's current toolset against its own and self-diagnose.

Beyond the tool list, the manifest carries a live SKILL.md fingerprint (sha of the
brain's canonical skill file) and a short `changes` list — the ONLY channel that
reaches a new session on any PC and lets Claude Code refresh a stale local skill copy.

Bump SERVER_VERSION + CURRENT_TOOLS on any tool-surface change; update CHANGES when
the protocol shifts.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

SERVER_VERSION = "2026-07-10.meetings-widget"

# The full tool surface this server offers right now (kept in sync with the
# registry-guard test). A session missing any of these is running a stale tool list.
CURRENT_TOOLS = (
    "kb_projects", "kb_load", "kb_read", "kb_write", "kb_edit", "kb_move",
    "kb_append_log", "kb_leave_message", "kb_mark_read", "kb_search", "kb_artifacts",
    "kb_recipes", "kb_share_artifact", "kb_unshare_artifact", "kb_rename_project",
    "kb_import", "kb_inbox", "kb_doctor",
    "kb_thread_post", "kb_thread_read", "kb_threads",
    "kb_presence", "kb_roster", "kb_handoff", "kb_workspace",
    "kb_claim", "kb_release", "kb_claims",
    "kb_meetings", "meetings_state", "meeting_transcript", "meeting_reply",
    "kb_office", "office_state",
    "kb_contacts", "kb_add_contact", "kb_accept_contact",
    "kb_dm", "kb_messages", "kb_notifications",
    "kb_share_context", "kb_request_context", "kb_grant_request",
    "kb_shared_with_me", "kb_guest_read", "kb_guest_search", "kb_send",
)

# A SHORT, human-readable list of what's new — one line each, surfaced in every kb_load so
# a resuming session (any PC) learns the current protocol without re-reading the skill.
CHANGES = (
    "kb_office: glanceable live-office card in chat (desks/meetings, watch-only)",
    "kb_meetings: watch + reply to live threads from any claude.ai chat (mobile too)",
    "threads: long-poll — use wait_for_reply=True / wait_seconds, never 2-3s polling",
    "kb_load: lite=True for cheap resumes",
    "office.json: sessions[] is live-only (+recent[])",
    "presence: automatic via Claude Code hooks (hooks/README.md in the code repo)",
    "skill: token-thrift rules — sync your local copy",
)

# The canonical skill path inside the brain bundle — its sha is the skill fingerprint.
SKILL_REL_PATH = "skills/engram/SKILL.md"

# Per-process cache keyed by the resolved path -> ((mtime_ns, size), fingerprint). The sha
# is recomputed only when the file's mtime/size changes, so embedding it in every kb_load
# is effectively free.
_skill_cache: dict[str, tuple[tuple[int, int], dict[str, str]]] = {}


def _skill_fingerprint(skill_path: Path | None) -> dict[str, str] | None:
    """Live fingerprint of the brain's SKILL.md: {path, updated (ISO date), sha (12)}.

    ``sha`` is the first 12 hex chars of sha256 over the file's UTF-8 bytes — the canonical
    skill identity a session compares its local copy against. ``updated`` is the file's
    mtime as a UTC ISO date. Returns None when no path is given or the file is
    absent/unreadable (the manifest simply omits the skill block). Cached per-process by
    (mtime_ns, size) so the hash is computed at most once per skill edit.
    """
    if skill_path is None:
        return None
    try:
        st = skill_path.stat()
    except OSError:
        return None
    stat_key = (st.st_mtime_ns, st.st_size)
    cached = _skill_cache.get(str(skill_path))
    if cached is not None and cached[0] == stat_key:
        return cached[1]
    try:
        data = skill_path.read_bytes()
    except OSError:
        return None
    sha = hashlib.sha256(data).hexdigest()[:12]
    updated = dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).strftime("%Y-%m-%d")
    fp = {"path": SKILL_REL_PATH, "updated": updated, "sha": sha}
    _skill_cache[str(skill_path)] = (stat_key, fp)
    return fp


def server_manifest(skill_path: Path | None = None) -> dict:
    """The block kb_load embeds so sessions can self-check their tool list AND their skill.

    Pass the absolute path to the brain's SKILL.md (``<brain>/skills/engram/SKILL.md``) to
    include a live ``skill`` fingerprint block; omit it (tests, non-brain callers) and the
    block is simply absent.
    """
    manifest: dict = {
        "version": SERVER_VERSION,
        "tools": list(CURRENT_TOOLS),
        "changes": list(CHANGES),
        "note": (
            "This is the server's CURRENT tool set. If any tool listed here is NOT in "
            "your available tools, this chat was opened before the latest update and is "
            "running a stale tool list — tell the user to start a FRESH chat to use the "
            "newer tools (e.g. kb_edit, kb_move, kb_import, kb_doctor). Everything you "
            "write in this chat is still safe; only the newest verbs are missing. "
            "If you are Claude Code and ~/.claude/skills/engram/SKILL.md differs from "
            "skill.sha: kb_read('skills/engram/SKILL.md'), write it to "
            "~/.claude/skills/engram/SKILL.md verbatim, and tell Hiren the skill was "
            "refreshed. If the engram presence hooks are not installed on this PC (no "
            "~/.claude/hooks/engram-presence-hook.sh), tell Hiren to run the 3-step "
            "install in hooks/README.md."
        ),
    }
    skill = _skill_fingerprint(skill_path)
    if skill is not None:
        manifest["skill"] = skill
    return manifest
