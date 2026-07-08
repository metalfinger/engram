"""Server capability manifest — surfaced in kb_load so ANY session (even a stale
chat whose tool list predates the latest deploy) can tell it's behind and advise
the user to start a fresh chat for the newest tools.

MCP negotiates a chat's tool list once at connection, so a server can't inject new
tools into a running chat. But kb_load runs in every session and reads this live,
so Claude can compare the server's current toolset against its own and self-diagnose.

Bump SERVER_VERSION + CURRENT_TOOLS on any tool-surface change.
"""

from __future__ import annotations

SERVER_VERSION = "2026-07-08"

# The full tool surface this server offers right now (kept in sync with the
# registry-guard test). A session missing any of these is running a stale tool list.
CURRENT_TOOLS = (
    "kb_projects", "kb_load", "kb_read", "kb_write", "kb_edit", "kb_move",
    "kb_append_log", "kb_leave_message", "kb_mark_read", "kb_search", "kb_artifacts",
    "kb_recipes", "kb_share_artifact", "kb_unshare_artifact", "kb_rename_project",
    "kb_import", "kb_inbox", "kb_doctor",
)


def server_manifest() -> dict:
    """The block kb_load embeds so sessions can self-check their tool list."""
    return {
        "version": SERVER_VERSION,
        "tools": list(CURRENT_TOOLS),
        "note": (
            "This is the server's CURRENT tool set. If any tool listed here is NOT in "
            "your available tools, this chat was opened before the latest update and is "
            "running a stale tool list — tell the user to start a FRESH chat to use the "
            "newer tools (e.g. kb_edit, kb_move, kb_import, kb_doctor). Everything you "
            "write in this chat is still safe; only the newest verbs are missing."
        ),
    }
