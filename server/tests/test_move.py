"""kb_move: single-concept move with bundle-wide link rewrite, index maintenance,
frontmatter-ref rewrite, and collision/reserved guards."""

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


async def test_move_rewrites_links_and_both_indexes(store: KBStore) -> None:
    root = store.root
    await store.kb_write(
        "projects/alt/specs/api.md",
        "---\ntype: spec\ndescription: API spec.\n---\n\nSee [ctx](../context.md).\n",
        "add api",
    )
    await store.kb_write(
        "projects/alt/decisions/2026-07-uses-api.md",
        "---\ntype: decision\ndescription: Uses the API.\n---\n\nImplements [api](../specs/api.md).\n",
        "add decision",
    )

    r = await store.kb_move("projects/alt/specs/api.md", "projects/alt/reference/api-spec.md")
    assert r["old"] == "projects/alt/specs/api.md"
    assert r["new"] == "projects/alt/reference/api-spec.md"
    assert r["links_rewritten"] >= 1
    assert not (root / "projects/alt/specs/api.md").exists()
    assert (root / "projects/alt/reference/api-spec.md").exists()

    # a body link in ANOTHER tree now resolves to the new path
    dec = (root / "projects/alt/decisions/2026-07-uses-api.md").read_text(encoding="utf-8")
    assert "../reference/api-spec.md" in dec

    # the moved file's OWN relative link still resolves to context.md
    moved = (root / "projects/alt/reference/api-spec.md").read_text(encoding="utf-8")
    assert "../context.md" in moved

    # old index bullet removed, new index (and its chain) gained the entry
    assert "api.md" not in (root / "projects/alt/specs/index.md").read_text(encoding="utf-8")
    assert "api-spec.md" in (root / "projects/alt/reference/index.md").read_text(encoding="utf-8")
    assert "](reference/index.md)" in (root / "projects/alt/index.md").read_text(encoding="utf-8")


async def test_move_same_dir_rename_keeps_relative_links(store: KBStore) -> None:
    root = store.root
    await store.kb_write(
        "projects/alt/specs/api.md",
        "---\ntype: spec\ndescription: d.\n---\n\nSee [ctx](../context.md).\n",
        "add",
    )
    await store.kb_move("projects/alt/specs/api.md", "projects/alt/specs/api-v2.md")
    moved = (root / "projects/alt/specs/api-v2.md").read_text(encoding="utf-8")
    assert "../context.md" in moved  # unchanged: same directory
    assert "api-v2.md" in (root / "projects/alt/specs/index.md").read_text(encoding="utf-8")
    assert "](api.md)" not in (root / "projects/alt/specs/index.md").read_text(encoding="utf-8")


async def test_move_rewrites_frontmatter_refs(store: KBStore) -> None:
    root = store.root
    old = "projects/alt/decisions/2026-06-old.md"
    await store.kb_write(old, "---\ntype: decision\ndescription: Old.\n---\n\n[c](../context.md)\n", "old")
    await store.kb_write(
        "projects/alt/artifacts/2026-07-report.md",
        f"---\ntype: artifact\ndescription: A report.\nsources:\n  - {old}\n---\n\nBody.\n",
        "report",
    )

    await store.kb_move(old, "projects/alt/decisions/2026-06-renamed.md")
    art = (root / "projects/alt/artifacts/2026-07-report.md").read_text(encoding="utf-8")
    assert "projects/alt/decisions/2026-06-renamed.md" in art
    assert "2026-06-old.md" not in art


async def test_move_collision_reserved_and_identical_refused(store: KBStore) -> None:
    body = "---\ntype: spec\ndescription: d.\n---\n\n[c](../context.md)\n"
    await store.kb_write("projects/alt/specs/api.md", body, "a")
    await store.kb_write("projects/alt/specs/other.md", body, "b")

    with pytest.raises(KBError, match="already exists"):
        await store.kb_move("projects/alt/specs/api.md", "projects/alt/specs/other.md")
    with pytest.raises(KBError, match="cannot be moved"):
        await store.kb_move("projects/alt/index.md", "projects/alt/foo.md")
    with pytest.raises(KBError, match="identical"):
        await store.kb_move("projects/alt/specs/api.md", "projects/alt/specs/api.md")
    with pytest.raises(KBError, match="No such concept"):
        await store.kb_move("projects/alt/specs/ghost.md", "projects/alt/specs/new.md")
