"""Duplicate project id detection — the 2026-09-16 incident, caught after the
fact rather than only prevented at creation.

`ensure_project` now refuses to CREATE a duplicate (see
test_ensure_project_no_duplicate.py), but a brain can still end up in a split
state some other way — a manual git operation, an import, a bug not yet found.
This is the safety net: reconcile walks the filesystem directly (never through
`kb_projects` or `_find_project_dir`, which both de-duplicate by id and would
therefore hide exactly the thing being audited for) and flags any id backed by
more than one real project directory.
"""

from __future__ import annotations

import pytest

from engram_server.config import Settings
from engram_server.kbstore import KBStore
from engram_server.reconcile import (
    _duplicate_project_ids,
    _render_duplicate_projects,
    _scan,
)


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
async def test_the_incident_shape_is_caught(store, settings):
    """THE INCIDENT, exactly: admin-entity-graph real at vibechk-suite/, and a
    second, competing top-level directory sharing the same id."""
    root = settings.brain_path
    _project(root, "projects/vibechk-suite/admin-entity-graph", "Real")
    _project(root, "projects/admin-entity-graph", "Duplicate")

    dupes = _duplicate_project_ids(root)

    assert len(dupes) == 1
    assert dupes[0]["id"] == "admin-entity-graph"
    assert sorted(dupes[0]["paths"]) == [
        "projects/admin-entity-graph",
        "projects/vibechk-suite/admin-entity-graph",
    ]


@pytest.mark.asyncio
async def test_kb_projects_hiding_the_split_does_not_hide_it_from_the_detector(
    store, settings
):
    """The exact trap the incident fell into: kb_projects lists ids, and its
    de-duplication would show ONE entry for admin-entity-graph even in the split
    state. The detector must not route through that same collapsing logic —
    it has to walk the filesystem directly."""
    root = settings.brain_path
    _project(root, "projects/vibechk-suite/admin-entity-graph", "Real")
    _project(root, "projects/admin-entity-graph", "Duplicate")

    listed = await store.kb_projects()
    ids = [p["id"] for p in listed]
    assert ids.count("admin-entity-graph") == 1, (
        "kb_projects collapsing to one entry is the documented, unchanged "
        "behaviour — the detector below must catch what this hides"
    )

    dupes = _duplicate_project_ids(root)
    assert any(d["id"] == "admin-entity-graph" for d in dupes)


@pytest.mark.asyncio
async def test_an_ordinary_brain_reports_clean(store, settings):
    root = settings.brain_path
    _project(root, "projects/thing-one", "One")
    _project(root, "projects/folder/thing-two", "Two")
    assert _duplicate_project_ids(root) == []


@pytest.mark.asyncio
async def test_metalfinger_is_exempt(store, settings):
    """metalfinger is a fixed, non-project top-level tree — never part of this
    id space, so a stray projects/metalfinger/ (if one ever existed) must not
    be treated as a duplicate of the real personal tree."""
    root = settings.brain_path
    _project(root, "projects/metalfinger", "Should never fire")
    _project(root, "projects/folder/metalfinger", "Also should never fire")
    assert _duplicate_project_ids(root) == []


@pytest.mark.asyncio
async def test_three_way_split_is_reported_as_one_finding(store, settings):
    root = settings.brain_path
    _project(root, "projects/thing", "A")
    _project(root, "projects/f1/thing", "B")
    _project(root, "projects/f2/thing", "C")
    dupes = _duplicate_project_ids(root)
    assert len(dupes) == 1
    assert len(dupes[0]["paths"]) == 3


@pytest.mark.asyncio
async def test_the_scan_surfaces_it_and_the_render_names_the_repair(store, settings):
    root = settings.brain_path
    _project(root, "projects/vibechk-suite/admin-entity-graph", "Real")
    _project(root, "projects/admin-entity-graph", "Duplicate")

    scan = _scan(root)
    assert scan["duplicate_projects"], "must surface through the full scan, not just the helper"

    rendered = _render_duplicate_projects(scan["duplicate_projects"])
    assert "admin-entity-graph" in rendered
    assert "kb_merge_project" in rendered, "must point at the repair tool"


def test_clean_report_says_none():
    assert "None" in _render_duplicate_projects([])
