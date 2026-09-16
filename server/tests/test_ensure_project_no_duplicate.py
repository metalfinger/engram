"""ensure_project must not create a duplicate id — the 2026-09-16 incident.

`kb_attach_project` (backed by `ensure_project`) checked only the literal top-level
path, so an id already living inside a folder (`projects/vibechk-suite/x`) was
invisible to it. A second session naming that id got a brand-new top-level
duplicate instead of the real project. Every id-based tool then preferred the
duplicate (top-level wins when both exist), silently orphaning the real one.

`ensure_project` now calls `_find_project_dir` — the same whole-tree resolver
`kb_read`/`kb_load` already use — before deciding to create anything.
"""

import pytest

from engram_server.config import Settings
from engram_server.kbstore import KBStore


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


def _project(root, rel: str, title: str) -> None:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "context.md").write_text(
        f"---\ntype: project\ntitle: {title}\ndescription: {title} desc\n"
        f"status: active\n---\n\n# About\n\nReal.\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_an_id_that_exists_in_a_folder_resolves_not_duplicates(store, settings):
    """THE INCIDENT, reproduced exactly: admin-entity-graph already lives under
    vibechk-suite/. Attaching it again must resolve there — never fabricate a
    second, top-level project sharing the id."""
    root = settings.brain_path
    _project(root, "projects/vibechk-suite/admin-entity-graph", "Admin Entity Graph")

    result = await store.ensure_project("admin-entity-graph", "some description")

    assert result["created"] is False
    assert result["folder"] == "vibechk-suite"
    assert not (root / "projects" / "admin-entity-graph").exists(), (
        "must never fabricate a top-level duplicate of an id that exists elsewhere"
    )


@pytest.mark.asyncio
async def test_a_genuinely_new_id_still_creates_at_top_level(store, settings):
    """The fix must not cost the ordinary case: a brand-new project id with no
    existing directory anywhere still creates normally."""
    root = settings.brain_path
    result = await store.ensure_project("brand-new-thing", "fresh project")

    assert result["created"] is True
    assert result["folder"] == ""
    assert (root / "projects" / "brand-new-thing" / "context.md").is_file()


@pytest.mark.asyncio
async def test_an_existing_top_level_project_is_still_recognized(store, settings):
    """Regression guard: the common case (project already at the top level) must
    keep working exactly as before."""
    root = settings.brain_path
    _project(root, "projects/existing-thing", "Existing Thing")

    result = await store.ensure_project("existing-thing", "ignored")

    assert result["created"] is False
    assert result["folder"] == ""


@pytest.mark.asyncio
async def test_repeated_attach_after_the_fix_never_produces_a_second_directory(
    store, settings
):
    """Calling ensure_project twice for a folder-nested id — the exact sequence
    that split the bundle in the incident — must be idempotent both times."""
    root = settings.brain_path
    _project(root, "projects/vibechk-suite/thing", "Thing")

    first = await store.ensure_project("thing", "d")
    second = await store.ensure_project("thing", "d")

    assert first == second
    assert not (root / "projects" / "thing").exists()
    # Exactly one context.md for this id, anywhere.
    matches = list((root / "projects").rglob("thing/context.md"))
    assert len(matches) == 1
