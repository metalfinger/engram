"""kb_thread_* — cross-session async thread channel (post / read / list + close)."""

from __future__ import annotations

import pytest

from engram_server.config import Settings
from engram_server.errors import KBError
from engram_server.frontmatter import read_meta
from engram_server.kbstore import KBStore


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


async def test_post_creates_thread_turn_and_transcript(store: KBStore, settings: Settings) -> None:
    res = await store.kb_thread_post("deploy-handoff", "session-a", "Are you up?", topic="Deploy sync")
    assert res["thread"] == "deploy-handoff"
    assert res["seq"] == 0
    assert res["status"] == "open"
    assert res["participants"] == ["session-a"]
    assert res["pushed"] is True

    root = settings.brain_path
    thread_path = root / "threads/deploy-handoff/thread.md"
    assert thread_path.is_file()
    tmeta = read_meta(thread_path)
    assert tmeta["type"] == "thread"
    assert tmeta["status"] == "open"
    assert tmeta["topic"] == "Deploy sync"
    assert tmeta["participants"] == ["session-a"]

    # Exactly one turn file, correctly typed, and the transcript carries the message.
    turns = list((root / "threads/deploy-handoff/turns").glob("*.md"))
    turns = [t for t in turns if t.name != "index.md"]
    assert len(turns) == 1
    turn_meta = read_meta(turns[0])
    assert turn_meta["type"] == "thread-turn"
    assert turn_meta["sender"] == "session-a"
    assert turn_meta["seq"] == 0
    transcript = thread_path.read_text(encoding="utf-8")
    assert "Are you up?" in transcript
    assert "session-a" in transcript

    # Indexed up to the bundle root so the explorer renders it.
    root_idx = (root / "index.md").read_text(encoding="utf-8")
    assert "threads/index.md" in root_idx


async def test_second_sender_appends_and_updates_participants(store: KBStore, settings: Settings) -> None:
    await store.kb_thread_post("sync-1", "session-a", "first")
    res = await store.kb_thread_post("sync-1", "session-b", "second")
    assert res["seq"] == 1
    assert res["participants"] == ["session-a", "session-b"]

    read = await store.kb_thread_read("sync-1")
    assert [t["sender"] for t in read["turns"]] == ["session-a", "session-b"]
    assert [t["message"] for t in read["turns"]] == ["first", "second"]


async def test_read_since_cursor_returns_only_new_turns(store: KBStore) -> None:
    await store.kb_thread_post("cursor-t", "a", "one")
    first = await store.kb_thread_read("cursor-t")
    assert len(first["turns"]) == 1
    cursor = first["cursor"]

    # No new turns since the cursor -> empty, cursor unchanged.
    again = await store.kb_thread_read("cursor-t", since=cursor)
    assert again["turns"] == []
    assert again["cursor"] == cursor

    # A new turn appears -> only it comes back, with a fresh cursor.
    await store.kb_thread_post("cursor-t", "b", "two")
    delta = await store.kb_thread_read("cursor-t", since=cursor)
    assert [t["message"] for t in delta["turns"]] == ["two"]
    assert delta["cursor"] != cursor


async def test_threads_lists_open_threads(store: KBStore) -> None:
    await store.kb_thread_post("alpha", "a", "hi", topic="Alpha topic")
    await store.kb_thread_post("beta", "a", "yo")
    listed = await store.kb_threads()
    ids = {t["thread"] for t in listed}
    assert {"alpha", "beta"} <= ids
    alpha = next(t for t in listed if t["thread"] == "alpha")
    assert alpha["topic"] == "Alpha topic"
    assert alpha["turn_count"] == 1
    assert alpha["status"] == "open"


async def test_close_sets_status_and_blocks_further_posts(store: KBStore, settings: Settings) -> None:
    await store.kb_thread_post("wrap", "a", "working on it")
    res = await store.kb_thread_post("wrap", "b", "done, closing", close=True)
    assert res["status"] == "closed"

    tmeta = read_meta(settings.brain_path / "threads/wrap/thread.md")
    assert tmeta["status"] == "closed"
    assert tmeta["closed_by"] == "b"
    assert tmeta.get("closed_at")

    read = await store.kb_thread_read("wrap")
    assert read["status"] == "closed"
    assert read["closed_by"] == "b"

    with pytest.raises(KBError):
        await store.kb_thread_post("wrap", "a", "one more")


async def test_unknown_thread_read_returns_none(store: KBStore) -> None:
    res = await store.kb_thread_read("never-created")
    assert res["status"] == "none"
    assert res["turns"] == []


async def test_kebab_validation_rejects_bad_id(store: KBStore) -> None:
    for bad in ("Bad Id", "has_underscore", "UPPER", "", "  "):
        with pytest.raises(KBError):
            await store.kb_thread_post(bad, "a", "hi")


async def test_empty_sender_or_message_rejected(store: KBStore) -> None:
    with pytest.raises(KBError):
        await store.kb_thread_post("ok-id", "  ", "hi")
    with pytest.raises(KBError):
        await store.kb_thread_post("ok-id", "a", "   ")


async def test_alternating_posters_ordered(store: KBStore) -> None:
    senders = ["a", "b", "a", "b", "a"]
    for i, s in enumerate(senders):
        await store.kb_thread_post("ping-pong", s, f"msg-{i}")
    read = await store.kb_thread_read("ping-pong")
    assert [t["seq"] for t in read["turns"]] == [0, 1, 2, 3, 4]
    assert [t["sender"] for t in read["turns"]] == senders
    assert [t["message"] for t in read["turns"]] == [f"msg-{i}" for i in range(5)]
