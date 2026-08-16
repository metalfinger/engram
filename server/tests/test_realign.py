"""kb_realign — the one-word orientation verb, made native.

The protocol lives in library/runbooks/realign.md; these tests pin the parts a
session must not get wrong: resolution ORDER (named -> learned route -> guess ->
ask), the routing table DERIVING itself from presence history rather than being
maintained by hand, and the refusal to silently file work under a neighbour.
"""

import pytest

from engram_server.kbstore import KBStore


@pytest.fixture()
async def store(settings):
    s = KBStore(settings)
    await s.start()
    return s


def _project(desc: str, phase: str = "Shipping.", loops: str = "- [ ] the top loop") -> str:
    return (
        f"---\ntype: project\ndescription: {desc}\n---\n\n"
        f"# About\n\n{desc}\n\n"
        f"# Current Phase\n\n{phase}\n\n"
        f"# Open Loops\n\n{loops}\n\n"
        f"# Next Actions\n\n- [ ] do the thing\n"
    )


async def _seed(store, *ids):
    for pid in ids:
        await store.kb_write(f"projects/{pid}/context.md", _project(f"{pid} project"), "seed")


# -- resolution order --------------------------------------------------------


@pytest.mark.asyncio
async def test_named_project_wins(store):
    await _seed(store, "engram", "slate")
    out = await store.kb_realign(project="slate")
    assert out["resolved"] and out["project"] == "slate"
    assert out["resolved_by"] == "named"


@pytest.mark.asyncio
async def test_loose_name_match(store):
    """'vibechk' should find vibechk-brand — the user says a word, not an id."""
    await _seed(store, "vibechk-brand", "engram")
    out = await store.kb_realign(project="vibechk")
    assert out["resolved"] and out["project"] == "vibechk-brand"


@pytest.mark.asyncio
async def test_ambiguous_name_asks_instead_of_picking(store):
    await _seed(store, "vibechk-brand", "vibechk-mcp-ux")
    out = await store.kb_realign(project="vibechk")
    assert out["resolved"] is False
    assert {p["id"] for p in out["candidates"]} == {"vibechk-brand", "vibechk-mcp-ux"}
    assert "ONE question" in out["instruction"]


@pytest.mark.asyncio
async def test_unresolved_never_files_under_a_neighbour(store):
    await _seed(store, "engram")
    out = await store.kb_realign(cwd="D:/somewhere/unrelated")
    assert out["resolved"] is False
    assert out["candidates"]
    assert "no project yet" in out["instruction"]
    # and nothing was created for the unmatched location
    assert "unrelated" not in {p["id"] for p in await store.kb_projects()}


# -- the routing table, derived ----------------------------------------------


@pytest.mark.asyncio
async def test_route_learned_from_presence_repo_remote(store):
    """No table to maintain: a session that attached to a project from a repo has
    already taught the route."""
    await _seed(store, "pixelpuri", "engram")
    await store.kb_presence(
        session="pc1-cc", project="pixelpuri",
        repo_remote="git@github.com:metalfinger/pixelpuri.git",
        cwd="D:/work/pixelpuri",
    )
    out = await store.kb_realign(repo="https://github.com/metalfinger/pixelpuri")
    assert out["resolved"] and out["project"] == "pixelpuri"
    assert "learned route" in out["resolved_by"]
    assert out["guess"] is False


@pytest.mark.asyncio
async def test_route_matches_a_subdirectory_of_a_known_cwd(store):
    await _seed(store, "engram")
    await store.kb_presence(session="pc1", project="engram", cwd="C:/code/engram")
    out = await store.kb_realign(cwd="C:/code/engram/server/engram_server")
    assert out["resolved"] and out["project"] == "engram"
    assert "learned route" in out["resolved_by"]


@pytest.mark.asyncio
async def test_route_table_self_maintains_when_a_repo_changes_project(store):
    """Newest presence record wins — a retired project stops capturing its old repo."""
    await _seed(store, "old-thing", "new-thing")
    await store.kb_presence(session="s1", project="old-thing", cwd="C:/code/shared")
    await store.kb_presence(session="s2", project="new-thing", cwd="C:/code/shared")
    out = await store.kb_realign(cwd="C:/code/shared")
    assert out["project"] == "new-thing"


@pytest.mark.asyncio
async def test_directory_name_guess_is_flagged(store):
    """Weakest signal, still usually right — but the caller must confirm it."""
    await _seed(store, "stellar-ux-exploration", "slate")
    out = await store.kb_realign(cwd="D:/Projects/slate")
    assert out["resolved"] and out["project"] == "slate"
    assert out["guess"] is True
    assert "guess" in out["resolved_by"]


# -- the orientation block ---------------------------------------------------


@pytest.mark.asyncio
async def test_orientation_block_is_compact_and_complete(store):
    await store.kb_write(
        "projects/engram/context.md",
        _project("engram project", phase="v3 shipped.", loops="- [ ] seed decisions"),
        "seed",
    )
    await store.kb_append_log("engram", "shipped rooms")
    await store.kb_leave_message("engram", "Read this first", "Do the thing.", to="any")
    out = await store.kb_realign(project="engram")

    assert out["phase"] == "v3 shipped."
    assert "seed decisions" in out["open_loops"]
    assert "do the thing" in out["next_actions"].lower()
    assert out["context_path"] == "projects/engram/context.md"
    assert out["pin_file"] == ".engram-project" and out["pin_content"] == "engram"
    assert len(out["unread_messages"]) == 1
    assert out["last_session"] is not None
    # the instruction carries the protocol a skill-less client needs
    assert "FIRST" in out["instruction"] and "ONE line" in out["instruction"]
    assert "drift" in out["instruction"].lower()


@pytest.mark.asyncio
async def test_sequence_prefers_a_backlog_file_over_context_loops(store):
    await _seed(store, "engram")
    await store.kb_write(
        "projects/engram/backlog.md",
        "---\ntype: note\ndescription: what's next\n---\n\n# Backlog\n\n1. ship it\n",
        "seed",
    )
    out = await store.kb_realign(project="engram")
    assert out["sequence_path"] == "projects/engram/backlog.md"
    assert "ship it" in out["sequence"]


@pytest.mark.asyncio
async def test_sequence_falls_back_to_open_loops(store):
    await _seed(store, "engram")
    out = await store.kb_realign(project="engram")
    assert out["sequence_path"] == "projects/engram/context.md"
    assert "the top loop" in out["sequence"]


@pytest.mark.asyncio
async def test_works_for_a_foldered_project(store):
    """Project-agnostic — and folders must not break resolution."""
    await _seed(store, "engram")
    await store.kb_move_project("engram", "personal")
    out = await store.kb_realign(project="engram")
    assert out["resolved"] and out["project"] == "engram"
    assert out["folder"] == "personal"
    assert out["context_path"] == "projects/personal/engram/context.md"


@pytest.mark.asyncio
async def test_map_is_noted_not_read(store):
    await _seed(store, "vibechk")
    await store.kb_write(
        "projects/vibechk/map.md",
        "---\ntype: note\ndescription: topic routing\n---\n\n# Map\n\nStuff.\n",
        "seed",
    )
    out = await store.kb_realign(project="vibechk")
    assert out["map_path"] == "projects/vibechk/map.md"
