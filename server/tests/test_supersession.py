"""Structured supersession edge: kb_write `supersedes:` stamps the target, rejects
self/cycles, and the edge is walkable from kb_read depth=1."""

from __future__ import annotations

from datetime import datetime, timezone

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


def _pin_today(monkeypatch: pytest.MonkeyPatch, iso: str) -> None:
    fixed = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    monkeypatch.setattr("engram_server.kbstore._utcnow", lambda: fixed)


OLD = (
    "---\ntype: decision\ndescription: The old call.\nconfidence: high\n---\n\n"
    "See [ctx](../context.md).\n"
)


async def test_supersede_stamps_target_and_new_concept(
    store: KBStore, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_today(monkeypatch, "2026-07-08")
    old_rel = "projects/alt/decisions/2026-06-old.md"
    new_rel = "projects/alt/decisions/2026-07-new.md"
    await store.kb_write(old_rel, OLD, "old decision")

    new = (
        f"---\ntype: decision\ndescription: The new call.\nsupersedes: {old_rel}\n"
        "superseded_by: projects/alt/decisions/should-be-stripped.md\n---\n\n"
        "Replaces the old one. See [ctx](../context.md).\n"
    )
    r = await store.kb_write(new_rel, new, "new decision")
    assert r["superseded"] == [old_rel]

    old_meta = read_meta(settings.brain_path / old_rel)
    assert old_meta["confidence"] == "superseded"
    assert old_meta["superseded_by"] == new_rel
    assert old_meta["valid_until"] == "2026-07-08"
    # the target's BODY is untouched
    assert "See [ctx](../context.md)." in (settings.brain_path / old_rel).read_text(encoding="utf-8")

    # the new concept must NOT itself carry superseded_by (stray one is stripped)
    new_meta = read_meta(settings.brain_path / new_rel)
    assert "superseded_by" not in new_meta
    assert new_meta["supersedes"] == old_rel


async def test_supersede_preexisting_valid_until_preserved(
    store: KBStore, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_today(monkeypatch, "2026-07-08")
    old_rel = "projects/alt/decisions/2026-06-dated.md"
    await store.kb_write(
        old_rel,
        "---\ntype: decision\ndescription: Dated.\nvalid_until: 2025-01-01\n---\n\n[c](../context.md)\n",
        "old",
    )
    await store.kb_write(
        "projects/alt/decisions/2026-07-succ.md",
        f"---\ntype: decision\ndescription: Successor.\nsupersedes: {old_rel}\n---\n\n[c](../context.md)\n",
        "succ",
    )
    # setdefault: an author-set valid_until is not clobbered by today
    assert read_meta(settings.brain_path / old_rel)["valid_until"] == "2025-01-01"


async def test_supersede_missing_target_errors(store: KBStore) -> None:
    new = (
        "---\ntype: decision\ndescription: d.\nsupersedes: projects/alt/decisions/nope.md\n---\n\n"
        "[ctx](../context.md)\n"
    )
    with pytest.raises(KBError, match="do not exist"):
        await store.kb_write("projects/alt/decisions/2026-07-x.md", new, "x")
    assert store.repo.is_dirty() == []  # nothing written


async def test_supersede_self_errors(store: KBStore) -> None:
    path = "projects/alt/decisions/2026-07-self.md"
    new = f"---\ntype: decision\ndescription: d.\nsupersedes: {path}\n---\n\n[ctx](../context.md)\n"
    with pytest.raises(KBError, match="supersede itself"):
        await store.kb_write(path, new, "self")


async def test_supersede_cycle_errors(store: KBStore) -> None:
    a = "projects/alt/decisions/a.md"
    b = "projects/alt/decisions/b.md"
    await store.kb_write(a, "---\ntype: decision\ndescription: A.\n---\n\n[c](../context.md)\n", "a")
    await store.kb_write(
        b, f"---\ntype: decision\ndescription: B.\nsupersedes: {a}\n---\n\n[c](../context.md)\n", "b"
    )
    cyc = f"---\ntype: decision\ndescription: A2.\nsupersedes: {b}\n---\n\n[c](../context.md)\n"
    with pytest.raises(KBError, match="cycle"):
        await store.kb_write(a, cyc, "cycle")


async def test_depth1_walks_the_supersede_edge_both_ways(store: KBStore) -> None:
    old = "projects/alt/decisions/2026-06-o.md"
    new = "projects/alt/decisions/2026-07-n.md"
    # neither concept has a BODY link to the other — only the frontmatter edge
    await store.kb_write(old, "---\ntype: decision\ndescription: old.\n---\n\nStandalone old.\n", "o")
    await store.kb_write(
        new, f"---\ntype: decision\ndescription: new.\nsupersedes: {old}\n---\n\nStandalone new.\n", "n"
    )

    r_new = await store.kb_read(new, depth=1)
    supers = [link for link in r_new["links"] if link.get("via") == "supersedes"]
    assert any(link["path"] == old and link["missing"] is False for link in supers)

    r_old = await store.kb_read(old, depth=1)
    assert any(
        link.get("via") == "supersedes" and link["path"] == new for link in r_old["links"]
    )
