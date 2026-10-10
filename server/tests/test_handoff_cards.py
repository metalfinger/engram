"""Handoff cards — direction, not knowledge.

Pins the parts a session must not get wrong: one live card per project+stream (a
new card supersedes the old in the same commit), the size cap that keeps knowledge
in concepts, take/done lifecycle, realign returning the card, and kb_ready.
"""

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


def _ctx(pid: str) -> str:
    return (
        f"---\ntype: project\ndescription: {pid} project\n---\n\n# About\n\n{pid}\n\n"
        "# Current Phase\n\nBuilding.\n\n# Open Loops\n\n- [ ] loop\n\n"
        "# Next Actions\n\n- [ ] act\n"
    )


async def _card(store: KBStore, **kw):
    base = dict(
        project="fever-dream", stream="harness", goal="Ship the harness pilot",
        first_action="run the pilot story", verify="pytest -q",
    )
    base.update(kw)
    return await store.kb_handoff("pane-a", **base)


async def test_card_fields_round_trip(store: KBStore, settings: Settings) -> None:
    res = await _card(
        store, outcome="partial", now="wiring the judge", sha="abc123",
        transcript="/x/s.jsonl", dead_ends=["tried Y, OOM"], open_questions=["which model?"],
        refs=["server/app.py:12"],
    )
    assert res["card"] is True and res["superseded"] == []
    meta = read_meta(settings.brain_path / res["path"])
    assert meta["card"] is True and meta["project"] == "fever-dream"
    assert meta["stream"] == "harness" and meta["first_action"] == "run the pilot story"
    assert meta["summary"] == "Ship the harness pilot"  # goal stands in for summary
    assert meta["dead_ends"] == ["tried Y, OOM"] and meta["sha"] == "abc123"
    body = (settings.brain_path / res["path"]).read_text(encoding="utf-8")
    for heading in ("## Goal", "## Now", "## First action", "## Verify", "## Dead ends",
                    "## Open questions", "## Refs", "## Anchor"):
        assert heading in body
    assert "**Status:** open" in body  # the reconcile stale-marker relies on this line


async def test_classic_call_unchanged(store: KBStore) -> None:
    res = await store.kb_handoff("a", "summary text", next_steps="do x")
    assert res["card"] is False
    with pytest.raises(KBError):
        await store.kb_handoff("a", "")  # no summary and no goal


async def test_stream_defaults_to_branch(store: KBStore, settings: Settings) -> None:
    res = await store.kb_handoff("a", project="p", branch="feat/x", goal="g")
    assert read_meta(settings.brain_path / res["path"])["stream"] == "feat/x"


async def test_cap_refuses_a_knowledge_dump(store: KBStore) -> None:
    with pytest.raises(KBError, match="too big"):
        await _card(store, now="x" * 6000)


async def test_secret_in_card_field_refused(store: KBStore) -> None:
    with pytest.raises(KBError):
        await _card(store, dead_ends=["used key ghp_" + "a" * 36])


async def test_bad_outcome_refused(store: KBStore) -> None:
    with pytest.raises(KBError, match="outcome"):
        await _card(store, outcome="finished")


async def test_new_card_supersedes_old_in_same_stream(store: KBStore, settings: Settings) -> None:
    first = await _card(store)
    other = await _card(store, stream="other-stream")
    second = await _card(store, goal="Ship it, round two")
    assert second["superseded"] == [first["path"]]
    old = read_meta(settings.brain_path / first["path"])
    assert old["status"] == "superseded" and old["superseded_by"] == second["path"]
    assert "valid_until" in old
    assert read_meta(settings.brain_path / second["path"])["supersedes"] == [first["path"]]
    # a different stream is untouched
    assert read_meta(settings.brain_path / other["path"])["status"] == "open"


async def test_take_is_idempotent_and_warns_second_taker(store: KBStore, settings: Settings) -> None:
    res = await _card(store)
    t1 = await store.kb_take_handoff(res["path"], "pane-b")
    assert t1["status"] == "taken" and t1["changed"]
    again = await store.kb_take_handoff(res["path"], "pane-b")
    assert again["changed"] is False
    t2 = await store.kb_take_handoff(res["path"], "pane-c")
    assert t2["warnings"] and "pane-b" in t2["warnings"][0]
    done = await store.kb_take_handoff(res["path"], "pane-c", done=True)
    assert done["status"] == "done"
    assert read_meta(settings.brain_path / res["path"])["done_by"] == "pane-c"


async def test_take_returns_the_whole_card_on_every_path(store: KBStore) -> None:
    """The first real relay needed an extra kb_read because take returned no card."""
    res = await _card(
        store, now="wiring the judge", sha="abc123", transcript="/x/s.jsonl",
        dead_ends=["tried Y, OOM"], open_questions=["which model?"], refs=["server/app.py:12"],
    )
    taken = await store.kb_take_handoff(res["path"], "pane-b")
    card = taken["card"]
    assert card["path"] == res["path"] and card["status"] == "taken"
    assert card["goal"] == "Ship the harness pilot"
    assert card["first_action"] == "run the pilot story" and card["verify"] == "pytest -q"
    assert card["now"] == "wiring the judge" and card["dead_ends"] == ["tried Y, OOM"]
    assert card["open_questions"] == ["which model?"] and card["refs"] == ["server/app.py:12"]
    assert card["anchor"]["sha"] == "abc123" and card["anchor"]["transcript"] == "/x/s.jsonl"
    assert card["taken_by"] == "pane-b"
    # the unchanged (already-taken) path carries the card too
    again = await store.kb_take_handoff(res["path"], "pane-b")
    assert again["changed"] is False and again["card"]["first_action"] == "run the pilot story"
    done = await store.kb_take_handoff(res["path"], "pane-b", done=True)
    assert done["card"]["status"] == "done"


async def test_take_refuses_superseded_and_non_handoff_paths(store: KBStore) -> None:
    first = await _card(store)
    second = await _card(store)
    with pytest.raises(KBError, match=second["path"]):
        await store.kb_take_handoff(first["path"], "pane-b")
    with pytest.raises(KBError):
        await store.kb_take_handoff("projects/x/context.md", "pane-b")
    with pytest.raises(KBError):
        await store.kb_take_handoff("workspace/handoffs/../../etc.md", "pane-b")


async def test_taken_card_is_superseded_by_next_handoff(store: KBStore, settings: Settings) -> None:
    first = await _card(store)
    await store.kb_take_handoff(first["path"], "pane-b")
    second = await _card(store)
    assert second["superseded"] == [first["path"]]


async def test_close_stream_marks_live_cards_done(store: KBStore, settings: Settings) -> None:
    res = await _card(store, stream="feat-x")
    closed = await store.kb_close_stream("fever-dream", "feat-x")
    assert closed == [res["path"]]
    assert read_meta(settings.brain_path / res["path"])["status"] == "done"
    assert await store.kb_close_stream("fever-dream", "feat-x") == []


async def test_realign_returns_the_card(store: KBStore) -> None:
    await store.kb_write("projects/fever-dream/context.md", _ctx("fever-dream"), "seed")
    await store.kb_write("projects/slate/context.md", _ctx("slate"), "seed")
    a = await _card(store, stream="main", goal="main work")
    b = await _card(store, stream="feat-y", goal="branch work")
    out = await store.kb_realign(project="fever-dream", branch="main")
    assert out["card"]["path"] == a["path"]
    assert out["card"]["first_action"] == "run the pilot story"
    assert "2) A handoff CARD" in out["instruction"] and "4) Report" in out["instruction"]
    assert [c["path"] for c in out["other_cards"]] == [b["path"]]
    # no branch match -> newest open card
    assert (await store.kb_realign(project="fever-dream"))["card"]["path"] == b["path"]
    # other projects see nothing
    plain = await store.kb_realign(project="slate")
    assert "card" not in plain and "handoff CARD" not in plain["instruction"]


async def test_realign_skips_taken_cards(store: KBStore) -> None:
    await store.kb_write("projects/fever-dream/context.md", _ctx("fever-dream"), "seed")
    a = await _card(store)
    await store.kb_take_handoff(a["path"], "pane-b")
    assert "card" not in await store.kb_realign(project="fever-dream")


async def test_ready_lists_open_cards_only(store: KBStore) -> None:
    a = await _card(store, stream="s1")
    b = await _card(store, stream="s2", project="slate")
    c = await _card(store, stream="s3")
    await store.kb_take_handoff(c["path"], "pane-x")
    legacy = await store.kb_handoff("old", "a classic handoff", next_steps="step one\nstep two")
    out = await store.kb_ready()
    paths = [r["path"] for r in out["ready"]]
    assert set(paths) == {a["path"], b["path"], legacy["path"]}
    row = next(r for r in out["ready"] if r["path"] == legacy["path"])
    assert row["first_action"] == "step one" and row["goal"] == "a classic handoff"
    only = await store.kb_ready(project="slate")
    assert [r["path"] for r in only["ready"]] == [b["path"]]


async def test_missing_decision_path_warns(store: KBStore) -> None:
    res = await _card(store, decisions=["projects/fever-dream/decisions/nope.md"])
    assert res["warnings"] and "nope.md" in res["warnings"][0]
