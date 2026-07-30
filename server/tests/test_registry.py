"""Registry guard: the exact set of kb_* tools and prompts must stay registered.

The tool DOCSTRINGS are the product — for claude.ai sessions with no skill installed
they carry the entire Engram protocol. A tool that ships unregistered (or a rename that
drops one) is invisible to those sessions, so this test pins the full expected surface.
If you add or remove a tool/prompt on purpose, update the frozensets here in the same
commit — that edit is the intentional record of the change.
"""

from __future__ import annotations

import asyncio

EXPECTED_TOOLS = frozenset(
    {
        "kb_projects",
        "kb_load",
        "kb_read",
        "kb_write",
        "kb_edit",
        "kb_move",
        "kb_append_log",
        "kb_leave_message",
        "kb_mark_read",
        "kb_search",
        "kb_artifacts",
        "kb_recipes",
        "kb_share_artifact",
        "kb_unshare_artifact",
        "kb_rename_project",
        "kb_import",
        "kb_inbox",
        "kb_doctor",
        "kb_thread_post",
        "kb_thread_read",
        "kb_threads",
        "kb_presence",
        "kb_roster",
        "kb_handoff",
        "kb_workspace",
        "kb_claim",
        "kb_release",
        "kb_claims",
        "kb_meetings",
        "meetings_state",
        "meeting_transcript",
        "meeting_reply",
        "kb_contacts",
        "kb_add_contact",
        "kb_accept_contact",
        "kb_dm",
        "kb_messages",
        "kb_notifications",
        "kb_share_context",
        "kb_request_context",
        "kb_grant_request",
        "kb_shared_with_me",
        "kb_guest_read",
        "kb_guest_search",
        "kb_send",
        "kb_inbox_card",
        "social_state",
        "social_conversation",
        "social_send",
        "social_accept",
        "social_mark_read",
        "kb_publish",
        "kb_public",
        "kb_tag_project",
        "kb_attach_project",
        "kb_explore",
        "kb_read_public",
        "kb_follow",
        "kb_feed",
        "kb_ask",
        "kb_answer",
        "kb_asks",
        "kb_explore_card",
        "explore_state",
        "explore_profile",
        "explore_concept",
        "explore_follow",
        "explore_ask",
    }
)

EXPECTED_PROMPTS = frozenset(
    {
        "daily_briefing",
        "close_session",
        "build_artifact",
        "rebuild_artifact",
        "ask_brain",
    }
)


def _registered_tools() -> set[str]:
    import engram_server.app as app_module

    return {t.name for t in asyncio.run(app_module.mcp.list_tools())}


def _registered_prompts() -> set[str]:
    import engram_server.app as app_module

    return {p.name for p in asyncio.run(app_module.mcp.list_prompts())}


def test_every_expected_tool_is_registered() -> None:
    registered = _registered_tools()
    missing = EXPECTED_TOOLS - registered
    assert not missing, f"tools declared but not registered: {sorted(missing)}"


def test_no_unexpected_tool_registered() -> None:
    # Catches a new tool that shipped without being added to the expected set (and
    # therefore without a deliberate docstring review of its when/how/what).
    registered = _registered_tools()
    unexpected = registered - EXPECTED_TOOLS
    assert not unexpected, f"unregistered-in-guard tools present: {sorted(unexpected)}"


def test_every_expected_prompt_is_registered() -> None:
    registered = _registered_prompts()
    missing = EXPECTED_PROMPTS - registered
    assert not missing, f"prompts declared but not registered: {sorted(missing)}"


def test_no_unexpected_prompt_registered() -> None:
    registered = _registered_prompts()
    unexpected = registered - EXPECTED_PROMPTS
    assert not unexpected, f"unregistered-in-guard prompts present: {sorted(unexpected)}"


def test_every_tool_has_a_nonempty_docstring() -> None:
    # The docstrings ARE the protocol; an empty one is a silent discoverability hole.
    import engram_server.app as app_module

    tools = asyncio.run(app_module.mcp.list_tools())
    empty = [t.name for t in tools if not (t.description or "").strip()]
    assert not empty, f"tools with no description/docstring: {sorted(empty)}"
