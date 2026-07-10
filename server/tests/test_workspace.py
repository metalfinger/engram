"""Workspace coordination engine — presence, roster, handoff, room refs, snapshot."""

from __future__ import annotations

from datetime import timedelta

import pytest

from engram_server.config import Settings
from engram_server.errors import KBError
from engram_server.frontmatter import read_meta
from engram_server.kbstore import KBStore, _utcnow
from engram_server.reconcile import _scan


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


def _write_presence(root, session: str, updated: str, **fields) -> None:
    """Drop a presence record straight onto the checkout (controls `updated` for TTL/age
    tests without going through the heartbeat clock)."""
    meta_lines = [
        "type: presence",
        f"title: 'Presence: {session}'",
        f"description: {fields.get('working_on', session)}",
        f"session: {session}",
        f"status: {fields.get('status', 'working')}",
        f"working_on: {fields.get('working_on', '')}",
        f"repo: {fields.get('repo', '')}",
        f"updated: {updated}",
    ]
    p = root / "workspace" / "presence" / f"{session}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + "\n".join(meta_lines) + "\n---\n", encoding="utf-8", newline="\n")


# ------------------------------------------------------------------ presence + roster


async def test_presence_upsert_overwrites_not_duplicates(store: KBStore, settings: Settings) -> None:
    first = await store.kb_presence("pc1-cc", name="CC", status="working", working_on="tests")
    assert first["session"] == "pc1-cc"
    assert first["roster_active"] == 1

    second = await store.kb_presence("pc1-cc", name="CC", status="blocked", working_on="waiting on CI")

    pdir = settings.brain_path / "workspace" / "presence"
    files = [f for f in pdir.glob("*.md") if f.name != "index.md"]
    assert len(files) == 1  # upsert: one file per session, not two
    meta = read_meta(files[0])
    assert meta["session"] == "pc1-cc"
    assert meta["status"] == "blocked"  # reflects the SECOND announce
    assert meta["working_on"] == "waiting on CI"
    assert second["roster_active"] == 1


async def test_presence_slugs_session_and_validates(store: KBStore) -> None:
    res = await store.kb_presence("PC1 Claude Code")
    assert res["session"] == "pc1-claude-code"
    with pytest.raises(KBError):
        await store.kb_presence("   ")  # nothing slug-able
    with pytest.raises(KBError):
        await store.kb_presence("ok-sess", status="napping")  # not a valid status


async def test_presence_indexed_into_parent(store: KBStore, settings: Settings) -> None:
    await store.kb_presence("pc2-web", name="Web")
    idx = (settings.brain_path / "workspace" / "presence" / "index.md").read_text(encoding="utf-8")
    assert "pc2-web.md" in idx


async def test_roster_filters_window_orders_by_recency_and_ages(store: KBStore) -> None:
    root = store.root
    now = _utcnow()
    fresh_old = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_new = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = (now - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_presence(root, "sess-old", fresh_old, working_on="older")
    _write_presence(root, "sess-new", fresh_new, working_on="newer")
    _write_presence(root, "sess-stale", stale, working_on="gone")

    rows = store._presence_records_sync(15)
    ids = [r["session"] for r in rows]
    assert "sess-stale" not in ids  # outside the 15-min window
    assert ids == ["sess-new", "sess-old"]  # most-recently-updated first
    assert rows[0]["age_min"] is not None and rows[0]["age_min"] < rows[1]["age_min"]


async def test_roster_unparseable_timestamp_excluded_from_window(store: KBStore) -> None:
    _write_presence(store.root, "sess-bad", "not-a-timestamp")
    rows = store._presence_records_sync(15)
    assert all(r["session"] != "sess-bad" for r in rows)
    # ...but a wide-open (negative) window keeps it, age_min None
    everyone = store._presence_records_sync(-1)
    bad = next(r for r in everyone if r["session"] == "sess-bad")
    assert bad["age_min"] is None


# ------------------------------------------------------------------ handoff


async def test_handoff_writes_record(store: KBStore, settings: Settings) -> None:
    res = await store.kb_handoff(
        "session-a",
        "Search fusion half-done",
        repo="engram",
        branch="main",
        state="tests green, docstrings pending",
        next_steps="wire app.py tools",
        refs=["projects/engram/specs/workspace-coordination.md"],
    )
    assert res["path"].startswith("workspace/handoffs/")
    assert res["pushed"] is True
    meta = read_meta(settings.brain_path / res["path"])
    assert meta["type"] == "handoff"
    assert meta["from"] == "session-a"
    assert meta["to"] == "any"
    assert meta["status"] == "open"
    assert meta["next_steps"] == "wire app.py tools"
    assert meta["refs"] == ["projects/engram/specs/workspace-coordination.md"]


async def test_handoff_posts_pointer_into_room(store: KBStore) -> None:
    res = await store.kb_handoff(
        "session-a",
        "Handing deploy work to whoever is free",
        room="deploy-room",
        next_steps="run start-engram.ps1",
    )
    read = await store.kb_thread_read("deploy-room")
    assert read["status"] == "open"
    assert len(read["turns"]) == 1
    turn = read["turns"][0]
    assert turn["sender"] == "session-a"
    assert "Handing deploy work" in turn["message"]
    # the handoff record itself is shared into the room as a ref
    assert res["path"] in turn["refs"]


async def test_handoff_validation(store: KBStore) -> None:
    with pytest.raises(KBError):
        await store.kb_handoff("", "summary")  # no from_session
    with pytest.raises(KBError):
        await store.kb_handoff("a", "   ")  # empty summary
    with pytest.raises(KBError):
        await store.kb_handoff("a", "s", room="Bad Room")  # non-kebab room id


# ------------------------------------------------------------------ thread refs


async def test_thread_post_with_refs_stores_and_renders(store: KBStore, settings: Settings) -> None:
    shared = ["projects/engram/context.md", "projects/engram/specs/artifact-system.md"]
    await store.kb_thread_post("ref-room", "session-a", "here's what I built", refs=shared)

    # frontmatter of the turn carries refs
    turns = [
        f
        for f in (settings.brain_path / "threads/ref-room/turns").glob("*.md")
        if f.name != "index.md"
    ]
    assert len(turns) == 1
    assert read_meta(turns[0])["refs"] == shared

    # transcript renders a 'shared:' line with code-spanned paths
    transcript = (settings.brain_path / "threads/ref-room/thread.md").read_text(encoding="utf-8")
    assert "shared:" in transcript
    assert "`projects/engram/context.md`" in transcript

    # kb_thread_read surfaces refs on the turn
    read = await store.kb_thread_read("ref-room")
    assert read["turns"][0]["refs"] == shared


async def test_thread_post_without_refs_unchanged(store: KBStore, settings: Settings) -> None:
    """A plain post's turn frontmatter must not gain a refs key (byte-shape preserved)."""
    await store.kb_thread_post("plain-room", "session-a", "no refs here")
    turns = [
        f
        for f in (settings.brain_path / "threads/plain-room/turns").glob("*.md")
        if f.name != "index.md"
    ]
    assert "refs" not in read_meta(turns[0])
    read = await store.kb_thread_read("plain-room")
    assert read["turns"][0]["refs"] == []


# ------------------------------------------------------------------ snapshot


async def test_workspace_snapshot(store: KBStore) -> None:
    await store.kb_presence("pc1-cc", name="CC", working_on="workspace engine")
    await store.kb_thread_post("open-room", "session-a", "anyone around?")
    await store.kb_handoff("session-a", "leaving notes", next_steps="review PR")

    snap = await store.kb_workspace()
    assert set(snap) == {"roster", "rooms", "recent_handoffs"}
    assert any(r["session"] == "pc1-cc" for r in snap["roster"])
    assert any(t["thread"] == "open-room" for t in snap["rooms"])
    assert len(snap["recent_handoffs"]) == 1
    assert snap["recent_handoffs"][0]["from"] == "session-a"


async def test_workspace_handoffs_are_summary_only(store: KBStore) -> None:
    """kb_workspace trims handoffs to a summary — the full state/next_steps prose is dropped
    (kb_read the path for detail), keeping the glance cheap in tokens."""
    await store.kb_handoff(
        "session-a",
        "leaving notes",
        state="A very long running-state paragraph that must not be echoed in the glance.",
        next_steps="Another long next-steps block the workspace view should omit.",
    )
    snap = await store.kb_workspace()
    h = snap["recent_handoffs"][0]
    # summary shape: identity + summary + path, no bulky bodies
    assert set(h) == {"path", "from", "to", "summary", "repo", "branch", "created", "status"}
    assert "state" not in h
    assert "next_steps" not in h
    assert h["path"].startswith("workspace/handoffs/")
    # the trimmed prose is genuinely absent from the payload
    import json

    assert "running-state paragraph" not in json.dumps(snap, default=str)


# ------------------------------------------------------------------ reconcile exemption


async def test_reconcile_exempts_presence_and_handoff(store: KBStore) -> None:
    await store.kb_presence("pc1-cc", name="CC")
    handoff = await store.kb_handoff("session-a", "some work", next_steps="continue")

    scan = _scan(store.root)
    presence_rel = "workspace/presence/pc1-cc.md"
    # neither is inbound-linked, but as workspace outputs they must not be flagged
    assert presence_rel not in scan["orphans"] and presence_rel not in scan["dead"]
    assert handoff["path"] not in scan["orphans"] and handoff["path"] not in scan["dead"]
    # and both are index-linked, so no membership repair is queued
    assert presence_rel not in scan["index_repairs"]
    assert handoff["path"] not in scan["index_repairs"]
