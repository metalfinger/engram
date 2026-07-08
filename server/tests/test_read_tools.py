"""kb_projects / kb_load / kb_read against the seeded skeleton checkout."""

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


async def test_projects_lists_all_with_index_descriptions(store: KBStore) -> None:
    rows = await store.kb_projects()
    assert [r["id"] for r in rows] == [
        "alt",
        "arete",
        "deccan-transcon",
        "fx-stuthi",
        "hyprlocl",
        "materia-verde",
        "metalfinger",
    ]
    by_id = {r["id"]: r for r in rows}
    # titles/descriptions come from the parent index bullets, both link forms accepted
    assert by_id["alt"]["title"] == "Alt Inc"
    assert by_id["alt"]["description"].startswith("Paid trial at Alt Inc")
    assert by_id["deccan-transcon"]["description"] == "Freelance: Webflow, SEO, CMS."
    assert by_id["metalfinger"]["title"] == "Metalfinger"
    assert by_id["metalfinger"]["description"] == "Content channel: strategy, videos, journal."
    assert all(r["status"] == "active" for r in rows)
    assert by_id["alt"]["last_session"] == "2026-07-04"
    assert by_id["metalfinger"]["last_session"] == "2026-07-04"
    assert all(r["unread_messages"] == 0 for r in rows)


async def test_load_assembles_project_and_applies_exclusions(store: KBStore) -> None:
    res = await store.kb_load("alt")
    assert res["project"] == "alt"
    assert res["context_md"] is not None
    assert res["context_md"].startswith("---")
    assert "type: project" in res["context_md"]

    tree = res["index_tree"]
    assert tree["path"] == "projects/alt"
    assert tree["title"] == "Alt Inc"  # decorated from projects/index.md
    # messages/ subtree excluded from the walk
    assert [d["name"] for d in tree["dirs"]] == ["assets", "decisions", "people", "specs"]
    # index.md and log.md excluded everywhere
    assert [f["name"] for f in tree["files"]] == ["context.md"]
    ctx = tree["files"][0]
    assert ctx["path"] == "projects/alt/context.md"
    assert ctx["title"] == "Context"
    assert ctx["description"].startswith("Living state")
    decisions = next(d for d in tree["dirs"] if d["name"] == "decisions")
    assert decisions["title"] == "Decisions"

    logs = res["recent_log"]
    assert len(logs) == 1
    assert logs[0]["date"] == "2026-07-04"
    assert logs[0]["title"] == "Bundle initialized"
    assert "Skeleton created" in logs[0]["body"]

    assert res["unread_messages"] == []
    # alt's only concept files are context.md (excluded) — nothing active besides it
    assert res["active_concepts"] == []


async def test_load_metalfinger_has_no_context(store: KBStore) -> None:
    res = await store.kb_load("metalfinger")
    assert res["context_md"] is None
    tree = res["index_tree"]
    assert [d["name"] for d in tree["dirs"]] == ["videos"]
    videos = tree["dirs"][0]
    assert [f["name"] for f in videos["files"]] == ["helix.md"]
    assert videos["files"][0]["title"] == "Helix"
    assert res["unread_messages"] == []
    active_paths = [c["path"] for c in res["active_concepts"]]
    assert active_paths == ["metalfinger/videos/helix.md"]


async def test_load_unknown_project_teaches_kb_projects(store: KBStore) -> None:
    with pytest.raises(KBError, match="kb_projects"):
        await store.kb_load("no-such-project")
    with pytest.raises(KBError, match="kb_projects"):
        await store.kb_load("Not Valid!")


async def test_read_returns_content_and_meta(store: KBStore, settings: Settings) -> None:
    raw = (settings.brain_path / "projects/alt/context.md").read_text(encoding="utf-8")
    res = await store.kb_read("projects/alt/context.md")
    assert res["path"] == "projects/alt/context.md"
    assert res["content"] == raw
    assert res["meta"]["type"] == "project"
    assert res["meta"]["title"] == "alt"
    assert "links" not in res  # depth=0 default

    # backslash paths normalize to POSIX
    res2 = await store.kb_read("projects\\alt\\context.md")
    assert res2["path"] == "projects/alt/context.md"


async def test_read_depth1_resolves_links_and_flags_missing(store: KBStore) -> None:
    content = (
        "---\n"
        "type: spec\n"
        "description: API spec with links.\n"
        "---\n"
        "# API\n\n"
        "See [Context](../context.md) and [Ghost](missing-thing.md) "
        "and [External](https://example.com/x.md).\n"
    )
    await store.kb_write("projects/alt/specs/api.md", content, "add api spec")
    res = await store.kb_read("projects/alt/specs/api.md", depth=1)
    by_path = {link["path"]: link for link in res["links"]}
    assert set(by_path) == {"projects/alt/context.md", "projects/alt/specs/missing-thing.md"}
    ctx = by_path["projects/alt/context.md"]
    assert ctx["missing"] is False
    assert ctx["meta"]["type"] == "project"
    assert by_path["projects/alt/specs/missing-thing.md"] == {
        "path": "projects/alt/specs/missing-thing.md",
        "missing": True,
    }


async def test_read_rejections(store: KBStore) -> None:
    with pytest.raises(KBError, match="escape"):
        await store.kb_read("../outside.md")
    with pytest.raises(KBError, match="kb_search"):
        await store.kb_read("projects/alt/never-existed.md")
    with pytest.raises(KBError, match="directory"):
        await store.kb_read("projects/alt")
    with pytest.raises(KBError, match="depth"):
        await store.kb_read("projects/alt/context.md", depth=2)
    with pytest.raises(KBError, match="off-limits"):
        await store.kb_read(".git/config")


async def test_search_runs_over_checkout(store: KBStore) -> None:
    results = await store.kb_search("helix video", project="metalfinger")
    assert results
    assert results[0]["path"] == "metalfinger/videos/helix.md"
    assert results[0]["title"] == "Helix"


async def test_read_depth1_includes_backlinks(store):
    target = "---\ntype: spec\ndescription: A spec others cite.\n---\n\nBody with a [link out](../context.md).\n"
    await store.kb_write("projects/alt/specs/cited.md", target, "add cited spec")
    citer = (
        "---\ntype: decision\ndescription: Cites the spec.\n---\n\n"
        "Implements [the spec](../specs/cited.md).\n"
    )
    await store.kb_write("projects/alt/decisions/2026-07-citer.md", citer, "add citing decision")

    r = await store.kb_read("projects/alt/specs/cited.md", depth=1)
    paths = [b["path"] for b in r["backlinks"]]
    assert "projects/alt/decisions/2026-07-citer.md" in paths
    # depth=0 stays lean
    r0 = await store.kb_read("projects/alt/specs/cited.md")
    assert "backlinks" not in r0


def test_read_text_retry_survives_transient_oserror(tmp_path, monkeypatch):
    """The Windows lock-free-read race: a reader hitting a file mid-replace gets
    OSError; the helper retries briefly instead of failing the tool call."""
    from pathlib import Path as _P

    from engram_server.kbstore import _read_text_retry

    target = tmp_path / "x.md"
    target.write_text("hello", encoding="utf-8")
    real = _P.read_text
    calls = {"n": 0}

    def flaky(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("file busy (simulated in-flight replace)")
        return real(self, *a, **kw)

    monkeypatch.setattr(_P, "read_text", flaky)
    assert _read_text_retry(target) == "hello"
    assert calls["n"] == 3

    calls["n"] = -100  # never succeeds within attempts
    def always(self, *a, **kw):
        raise PermissionError("still busy")
    monkeypatch.setattr(_P, "read_text", always)
    import pytest as _pytest
    with _pytest.raises(PermissionError):
        _read_text_retry(target, attempts=2, delay=0.001)


async def test_kb_load_carries_server_manifest(store):
    """kb_load embeds the live tool manifest so a stale chat can self-diagnose."""
    from engram_server.version import CURRENT_TOOLS

    projects = await store.kb_projects()
    assert projects, "fixture has no projects"
    r = await store.kb_load(projects[0]["id"])
    assert "server" in r
    assert r["server"]["version"]
    assert "kb_edit" in r["server"]["tools"]
    assert set(r["server"]["tools"]) == set(CURRENT_TOOLS)
    assert "stale tool list" in r["server"]["note"]
