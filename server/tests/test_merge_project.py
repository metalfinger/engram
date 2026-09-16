"""kb_merge_project — the repair for the 2026-09-16 split-project incident.

The tool's job: fold one project directory into another with no silent data
loss. Every scenario here is a "what could go wrong" from the incident's own
account, not a happy-path smoke test - that is what found every real bug in
this subsystem all session (staleness, consolidation, the Zip Slip guard).
"""

from __future__ import annotations

import pytest

from engram_server.config import Settings
from engram_server.errors import KBError
from engram_server.kbstore import KBStore


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


def _project(root, rel: str, title: str, extra_frontmatter: str = "") -> None:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "context.md").write_text(
        f"---\ntype: project\ntitle: {title}\ndescription: {title} desc\n"
        f"status: active\n{extra_frontmatter}---\n\n# About\n\nReal.\n",
        encoding="utf-8",
    )


def _concept(root, rel: str, title: str, body: str = "content") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\ntype: note\ntitle: {title}\ndescription: d\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _commit(root) -> None:
    """Fixtures must be COMMITTED before any write path runs: the store refuses
    to commit over a dirty checkout, and that refusal is a real guard worth
    keeping — the brain has exactly one writer and a dirty tree means something
    else touched it."""
    import subprocess
    for args in (("add", "-A"), ("commit", "-m", "fixture", "--no-gpg-sign")):
        subprocess.run(["git", "-C", str(root), *args],
                       capture_output=True, encoding="utf-8")


# --------------------------------------------------------------- the incident


@pytest.mark.asyncio
async def test_the_incident_scenario_end_to_end(store, settings):
    """The exact shape: a real project inside a folder, a duplicate at the top
    level sharing its id, both with real content. Merge the duplicate INTO the
    real one, addressing the duplicate by PATH since the id alone is ambiguous
    once both exist."""
    root = settings.brain_path
    _project(root, "projects/vibechk-suite/admin-entity-graph", "Real")
    _concept(root, "projects/vibechk-suite/admin-entity-graph/decisions/x.md", "X",
             "the real decision")
    _project(root, "projects/admin-entity-graph", "Duplicate")
    _concept(root, "projects/admin-entity-graph/findings/y.md", "Y", "duplicate finding")
    _commit(root)

    # Path form on BOTH sides, deliberately: plain-id resolution for `into` would
    # ALSO hit top-level-wins and resolve to the very duplicate being merged away
    # (from == into) — exactly the ambiguity this tool exists to let a session
    # step around once it has found the real content via kb_search/kb_read.
    result = await store.kb_merge_project(
        "projects/admin-entity-graph", "projects/vibechk-suite/admin-entity-graph"
    )

    assert result["concepts_moved"] == 1
    assert not (root / "projects" / "admin-entity-graph").exists(), (
        "the emptied duplicate must be removed, not left as a stub"
    )
    moved = root / "projects" / "vibechk-suite" / "admin-entity-graph" / "findings" / "y.md"
    assert moved.is_file()
    assert "duplicate finding" in moved.read_text(encoding="utf-8")
    # The original, untouched concept must still be exactly where it was.
    assert (root / "projects/vibechk-suite/admin-entity-graph/decisions/x.md").is_file()


@pytest.mark.asyncio
async def test_path_form_disambiguates_a_shared_id(store, settings):
    """Ordinary id resolution can only ever name ONE of two colliding
    directories (top-level wins) - the path form must reach the other one."""
    root = settings.brain_path
    _project(root, "projects/f1/thing", "One")
    _project(root, "projects/thing", "Two, at top level")
    _commit(root)

    # Plain id 'thing' would resolve to the top-level one; explicitly naming the
    # nested one by path must still work.
    result = await store.kb_merge_project("projects/f1/thing", "thing")
    assert result["from"] == "projects/f1/thing"
    assert result["into"] == "projects/thing"


# ------------------------------------------------------------- no silent loss


@pytest.mark.asyncio
async def test_a_destination_collision_refuses_the_whole_merge(store, settings):
    root = settings.brain_path
    _project(root, "projects/a", "A")
    _concept(root, "projects/a/notes/shared.md", "Shared A", "version A")
    _project(root, "projects/b", "B")
    _concept(root, "projects/b/notes/shared.md", "Shared B", "version B")
    _commit(root)

    with pytest.raises(KBError, match="already has a file"):
        await store.kb_merge_project("a", "b")

    # NOTHING moved - both directories fully intact, both original contents present.
    assert (root / "projects/a/notes/shared.md").is_file()
    assert (root / "projects/b/notes/shared.md").is_file()
    assert "version A" in (root / "projects/a/notes/shared.md").read_text(encoding="utf-8")
    assert "version B" in (root / "projects/b/notes/shared.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_non_colliding_file_alongside_a_colliding_one_still_refuses(store, settings):
    """A partial merge would be worse than no merge: one collision blocks
    everything, even files that would have moved cleanly."""
    root = settings.brain_path
    _project(root, "projects/a", "A")
    _concept(root, "projects/a/notes/unique.md", "Unique", "fine")
    _concept(root, "projects/a/notes/clash.md", "Clash", "A's version")
    _project(root, "projects/b", "B")
    _concept(root, "projects/b/notes/clash.md", "Clash", "B's version")
    _commit(root)

    with pytest.raises(KBError):
        await store.kb_merge_project("a", "b")

    assert not (root / "projects/b/notes/unique.md").exists(), (
        "no partial credit - a merge either fully happens or not at all"
    )


@pytest.mark.asyncio
async def test_identical_source_and_destination_refused(store, settings):
    root = settings.brain_path
    _project(root, "projects/a", "A")
    _commit(root)
    with pytest.raises(KBError, match="same directory"):
        await store.kb_merge_project("a", "a")


@pytest.mark.asyncio
async def test_metalfinger_cannot_be_merged(store, settings):
    root = settings.brain_path
    (root / "metalfinger").mkdir(parents=True, exist_ok=True)
    (root / "metalfinger" / "context.md").write_text(
        "---\ntype: project\ntitle: M\ndescription: d\n---\n\nx\n", encoding="utf-8"
    )
    _project(root, "projects/a", "A")
    _commit(root)
    with pytest.raises(KBError, match="metalfinger"):
        await store.kb_merge_project("metalfinger", "a")
    with pytest.raises(KBError, match="metalfinger"):
        await store.kb_merge_project("a", "metalfinger")


# ------------------------------------------------------------------- content


@pytest.mark.asyncio
async def test_links_are_rewritten_bundle_wide_not_just_within_the_two_dirs(store, settings):
    """A THIRD project's link into the source must still resolve after the
    merge - the whole point of reusing kb_move's link-rewrite primitives."""
    root = settings.brain_path
    _project(root, "projects/a", "A")
    _concept(root, "projects/a/notes/x.md", "X")
    _project(root, "projects/b", "B")
    _project(root, "projects/c", "C")
    _concept(
        root, "projects/c/notes/refers.md", "Refers",
        "See [x](../../a/notes/x.md) for detail.",
    )
    _commit(root)

    result = await store.kb_merge_project("a", "b")
    assert result["links_rewritten"] >= 1

    refers = (root / "projects/c/notes/refers.md").read_text(encoding="utf-8")
    assert "../../b/notes/x.md" in refers, (
        "a third, uninvolved project's link must be repointed at the new location"
    )
    assert (root / "projects/b/notes/x.md").is_file()


@pytest.mark.asyncio
async def test_log_history_is_preserved_with_provenance_not_discarded(store, settings):
    root = settings.brain_path
    _project(root, "projects/a", "A")
    (root / "projects/a/log.md").write_text(
        "# Log — a\n\n## 2026-09-10\n* **Old work** — something that happened.\n",
        encoding="utf-8",
    )
    _project(root, "projects/b", "B")
    _commit(root)

    result = await store.kb_merge_project("a", "b")
    assert result["log_entries_merged"] == 1

    dest_log = (root / "projects/b/log.md").read_text(encoding="utf-8")
    assert "Old work" in dest_log, "the original entry's content must survive verbatim"
    assert "something that happened" in dest_log
    assert "merged from projects/a" in dest_log
    assert "2026-09-10" in dest_log, "the TRUE original date must be recorded"


@pytest.mark.asyncio
async def test_messages_move_as_plain_files(store, settings):
    root = settings.brain_path
    _project(root, "projects/a", "A")
    msg = root / "projects/a/messages/2026-09-01-hello.md"
    msg.parent.mkdir(parents=True, exist_ok=True)
    msg.write_text(
        "---\ntype: message\ntitle: Hello\ndescription: d\nto: any\nstatus: unread\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )
    _project(root, "projects/b", "B")
    _commit(root)

    result = await store.kb_merge_project("a", "b")
    assert result["messages_moved"] == 1
    assert (root / "projects/b/messages/2026-09-01-hello.md").is_file()
    assert not msg.exists()


@pytest.mark.asyncio
async def test_messages_index_files_are_not_moved_as_messages(store, settings):
    """Found doing the real 2026-09-16 repair: messages/index.md and
    messages/archive/index.md are server-maintained at every level, same as
    every other index.md — moving them as plain messages would fight the real
    indexer instead of letting it regenerate them cleanly."""
    root = settings.brain_path
    _project(root, "projects/a", "A")
    for rel in (
        "projects/a/messages/index.md",
        "projects/a/messages/archive/index.md",
        "projects/a/messages/archive/2026-09-01-old.md",
    ):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("placeholder\n", encoding="utf-8")
    _project(root, "projects/b", "B")
    _commit(root)

    result = await store.kb_merge_project("a", "b")

    assert result["messages_moved"] == 1, "only the real message, not either index.md"
    assert (root / "projects/b/messages/archive/2026-09-01-old.md").is_file()
    assert not (root / "projects/b/messages/index.md").exists()
    assert not (root / "projects/b/messages/archive/index.md").exists()


@pytest.mark.asyncio
async def test_the_source_directory_is_fully_gone_no_stub(store, settings):
    root = settings.brain_path
    _project(root, "projects/a", "A")
    _project(root, "projects/b", "B")
    _commit(root)
    await store.kb_merge_project("a", "b")
    assert not (root / "projects" / "a").exists()


@pytest.mark.asyncio
async def test_both_projects_must_exist(store, settings):
    root = settings.brain_path
    _project(root, "projects/b", "B")
    _commit(root)
    with pytest.raises(KBError):
        await store.kb_merge_project("nonexistent-project", "b")


# ----------------------------------------------------- kb_move_project points here


@pytest.mark.asyncio
async def test_move_project_collision_error_names_the_repair_tool(store, settings):
    root = settings.brain_path
    _project(root, "projects/thing", "A")
    _project(root, "projects/folder/thing", "B")
    _commit(root)
    with pytest.raises(KBError, match="kb_merge_project"):
        await store.kb_move_project("thing", "folder")
