"""Artifact system at the KBStore layer: built_from provenance, staleness,
kb_artifacts listing, and share/unshare token lifecycle."""

from __future__ import annotations

import pytest

from engram_server.config import Settings
from engram_server.errors import KBError
from engram_server.frontmatter import read_meta
from engram_server.kbstore import KBStore

ARTIFACT = (
    "---\ntype: artifact\ntitle: Weekly Status\ndescription: The weekly status report.\n"
    "sources:\n  - projects/alt/context.md\n---\n\nEverything is on track.\n"
)


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


async def test_write_artifact_autofills_built_from(store: KBStore, settings: Settings) -> None:
    before = store.repo.head_sha()
    res = await store.kb_write("projects/alt/artifacts/2026-07-weekly.md", ARTIFACT, "save artifact")
    # built_from is stamped with HEAD as it stood before this write's commit.
    meta = read_meta(settings.brain_path / "projects/alt/artifacts/2026-07-weekly.md")
    assert meta["built_from"] == before
    assert res["sha"] != before  # the write itself advanced HEAD


async def test_artifact_with_sources_not_warned_linkless(store: KBStore) -> None:
    res = await store.kb_write("projects/alt/artifacts/2026-07-a.md", ARTIFACT, "save")
    # a non-empty sources: list stands in for body links — no dead-end nag.
    assert not any("links to nothing" in w for w in res["warnings"])


async def test_artifact_without_sources_still_warned(store: KBStore) -> None:
    linkless = "---\ntype: artifact\ndescription: No sources here.\n---\n\nBody, no links.\n"
    res = await store.kb_write("projects/alt/artifacts/2026-07-b.md", linkless, "save")
    assert any("links to nothing" in w for w in res["warnings"])


async def test_kb_artifacts_lists_with_provenance(store: KBStore) -> None:
    await store.kb_write("projects/alt/artifacts/2026-07-weekly.md", ARTIFACT, "save")
    arts = await store.kb_artifacts()
    a = next(x for x in arts if x["path"].endswith("2026-07-weekly.md"))
    assert a["project"] == "alt"
    assert a["title"] == "Weekly Status"
    assert a["sources"] == ["projects/alt/context.md"]
    assert a["built_from"]
    assert a["shared"] is False
    assert a["share_url"] is None
    # freshly built against current HEAD, nothing touched the source since → not stale
    assert a["stale"] is False


async def test_kb_artifacts_stale_after_source_changes(store: KBStore, settings: Settings) -> None:
    await store.kb_write("projects/alt/artifacts/2026-07-weekly.md", ARTIFACT, "save")
    a0 = next(x for x in await store.kb_artifacts() if x["path"].endswith("2026-07-weekly.md"))
    assert a0["stale"] is False

    ctx = (settings.brain_path / "projects/alt/context.md").read_text(encoding="utf-8")
    await store.kb_write("projects/alt/context.md", ctx + "\nA fresh edit.\n", "touch context")

    a1 = next(x for x in await store.kb_artifacts() if x["path"].endswith("2026-07-weekly.md"))
    assert a1["stale"] is True  # a source moved since the artifact was built


async def test_kb_artifacts_project_filter(store: KBStore) -> None:
    await store.kb_write("projects/alt/artifacts/2026-07-weekly.md", ARTIFACT, "save")
    assert [a["project"] for a in await store.kb_artifacts(project="alt")] == ["alt"]
    assert await store.kb_artifacts(project="hyprlocl") == []


async def test_share_is_idempotent_and_unshare_revokes(
    store: KBStore, settings: Settings
) -> None:
    path = "projects/alt/artifacts/2026-07-weekly.md"
    await store.kb_write(path, ARTIFACT, "save")

    r1 = await store.kb_share_artifact(path)
    assert "/share/" in r1["share_url"]
    token = read_meta(settings.brain_path / path)["share"]
    assert len(str(token)) >= 20
    assert r1["share_url"].endswith(str(token))

    # idempotent: same link, NO new commit
    sha_before = store.repo.head_sha()
    r2 = await store.kb_share_artifact(path)
    assert r2["share_url"] == r1["share_url"]
    assert store.repo.head_sha() == sha_before

    # kb_artifacts reflects the shared state
    a = next(x for x in await store.kb_artifacts() if x["path"] == path)
    assert a["shared"] is True and a["share_url"] == r1["share_url"]

    # unshare removes the token; unsharing again is a no-op (no new commit)
    await store.kb_unshare_artifact(path)
    assert "share" not in read_meta(settings.brain_path / path)
    sha_after_unshare = store.repo.head_sha()
    await store.kb_unshare_artifact(path)
    assert store.repo.head_sha() == sha_after_unshare


async def test_share_rejects_non_artifact(store: KBStore) -> None:
    await store.kb_write(
        "projects/alt/decisions/2026-07-x.md",
        "---\ntype: decision\ndescription: A decision.\n---\n\n[ctx](../context.md)\n",
        "save",
    )
    with pytest.raises(KBError, match="artifact"):
        await store.kb_share_artifact("projects/alt/decisions/2026-07-x.md")


async def test_share_unknown_path_teaches(store: KBStore) -> None:
    with pytest.raises(KBError, match="kb_artifacts"):
        await store.kb_share_artifact("projects/alt/artifacts/nope.md")
