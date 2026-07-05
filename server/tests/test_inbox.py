"""kb_inbox quick-capture."""

from __future__ import annotations

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


async def test_inbox_captures_untriaged_and_indexes(store: KBStore, settings: Settings) -> None:
    res = await store.kb_inbox("Try Qdrant hybrid search\nMight speed up recall a lot.")
    assert res["pushed"] is True
    assert res["path"].startswith("inbox/")
    assert res["path"].endswith(".md")

    f = settings.brain_path / res["path"]
    meta = read_meta(f)
    assert meta["type"] == "inbox"
    assert meta["status"] == "untriaged"
    assert meta["title"] == "Try Qdrant hybrid search"
    body = f.read_text(encoding="utf-8")
    assert "Might speed up recall a lot." in body  # body verbatim

    # inbox/index.md created lazily and links the item; root index chained
    inbox_idx = (settings.brain_path / "inbox/index.md").read_text(encoding="utf-8")
    assert f.name in inbox_idx
    root_idx = (settings.brain_path / "index.md").read_text(encoding="utf-8")
    assert "inbox/index.md" in root_idx


async def test_inbox_slug_collision_gets_suffix(store: KBStore, settings: Settings) -> None:
    r1 = await store.kb_inbox("Same title here")
    r2 = await store.kb_inbox("Same title here")
    assert r1["path"] != r2["path"]


async def test_inbox_rejects_empty(store: KBStore) -> None:
    with pytest.raises(KBError):
        await store.kb_inbox("   ")
