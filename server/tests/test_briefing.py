"""Morning briefing generation."""

from __future__ import annotations

import pytest

from engram_server.briefing import build_briefing
from engram_server.config import Settings
from engram_server.frontmatter import read_meta
from engram_server.kbstore import KBStore


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


async def test_briefing_writes_artifact_and_leaves_message_when_notable(
    store: KBStore, settings: Settings
) -> None:
    # create something notable: an unread message
    await store.kb_leave_message("alt", "Verify DNS", "Check the CNAME before CMS work.", to="any")

    res = await build_briefing(store)
    assert res["path"].startswith("projects/engram/artifacts/")
    assert res["path"].endswith("-morning-briefing.md")
    assert res["notable"] is True
    assert res["message_left"] is True

    f = settings.brain_path / res["path"]
    meta = read_meta(f)
    assert meta["type"] == "artifact"
    assert meta["instruction"] == "daily morning briefing"
    text = f.read_text(encoding="utf-8")
    assert "# Morning Briefing" in text
    assert "Unread messages (1)" in text

    # a pointer message landed on the engram project
    unread = store._unread_messages("projects/engram")
    assert any("Morning briefing ready" in m["title"] for m in unread)


async def test_briefing_idempotent_same_day(store: KBStore) -> None:
    r1 = await build_briefing(store)
    r2 = await build_briefing(store)
    assert r1["path"] == r2["path"]  # overwrites the same-day path


async def test_briefing_quiet_day_leaves_no_message(store: KBStore, settings: Settings) -> None:
    # no unread, no stale artifacts, no inbox -> not notable
    res = await build_briefing(store)
    assert res["notable"] is False
    assert res["message_left"] is False


async def test_briefing_renders_workspace_section(store: KBStore, settings: Settings) -> None:
    await store.kb_presence(
        "pc1-cc", name="Alice", status="working", working_on="reconcile", repo="engram", branch="main"
    )
    await store.kb_handoff("session-a", "Finish the search exclusion work", repo="engram", branch="main")
    await store.kb_thread_post("deploy-room", "session-a", "anyone around?")

    res = await build_briefing(store)
    text = (settings.brain_path / res["path"]).read_text(encoding="utf-8")
    assert "## Workspace" in text
    assert "1 session(s) active" in text
    assert "Alice" in text and "engram@main" in text
    assert "deploy-room" in text            # open room listed
    assert "session-a" in text              # open handoff 'from'
    assert "Finish the search exclusion work" in text


async def test_briefing_omits_workspace_when_empty(store: KBStore, settings: Settings) -> None:
    res = await build_briefing(store)
    text = (settings.brain_path / res["path"]).read_text(encoding="utf-8")
    assert "## Workspace" not in text
