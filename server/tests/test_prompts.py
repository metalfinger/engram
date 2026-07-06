"""MCP prompts — one-tap workflows: registered, and each carries its key rails."""

from __future__ import annotations

import asyncio


def _prompt_text(name: str, arguments: dict | None = None) -> str:
    import engram_server.app as app_module

    result = asyncio.run(app_module.mcp.get_prompt(name, arguments or {}))
    return result.messages[0].content.text


def test_all_four_prompts_registered() -> None:
    import engram_server.app as app_module

    names = {p.name for p in asyncio.run(app_module.mcp.list_prompts())}
    assert {"daily_briefing", "close_session", "build_artifact", "rebuild_artifact"} <= names


def test_daily_briefing_rails() -> None:
    text = _prompt_text("daily_briefing")
    assert "kb_projects" in text
    assert "FIRST" in text  # messages surface first
    assert "navigate, never ingest" in text
    assert "under 20 lines" in text


def test_close_session_rails() -> None:
    text = _prompt_text("close_session", {"project": "engram"})
    assert "'engram'" in text
    assert "SHOW" in text and "BEFORE" in text  # draft shown before kb_append_log
    assert "kb_append_log" in text
    assert "context.md" in text
    assert "kb_leave_message" in text


def test_build_artifact_offers_save_with_provenance() -> None:
    text = _prompt_text("build_artifact", {"project": "engram", "ask": "a status report"})
    assert "ARTIFACT" in text
    assert "type: artifact" in text
    assert "sources:" in text
    assert "artifacts/YYYY-MM-" in text


def test_rebuild_artifact_is_a_recipe_runner() -> None:
    text = _prompt_text("rebuild_artifact", {"path": "projects/engram/artifacts/x.md"})
    assert "recipe" in text.lower()
    assert "CURRENT" in text
    assert "WHAT CHANGED" in text
    assert "SAME path" in text  # git-versioned living documents


def test_ask_brain_rails() -> None:
    text = _prompt_text("ask_brain", {"question": "why oauth"})
    assert "FROM MY KNOWLEDGE BASE" in text
    assert "kb_search" in text and "kb_read" in text
    assert "CITE" in text
    assert "kb_write" in text  # offer to persist a settled answer
