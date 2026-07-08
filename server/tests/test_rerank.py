"""Cross-encoder rerank tests — semantic.rerank blending + kb_search opt-in wiring.

Offline: a fake cross-encoder scores documents by a keyword so we can assert the
blend reorders and stamps `reranked`, and that a raising reranker degrades softly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from engram_server.config import Settings
from engram_server.kbstore import KBStore
from engram_server.semantic import SemanticIndex, _FakeControls, _sigmoid


class FakeReranker:
    """Cross-encoder stand-in: high score for docs containing 'winner', low otherwise."""

    def rerank(self, query, documents):
        return [4.0 if "winner" in d.lower() else -4.0 for d in documents]


class BoomReranker:
    def rerank(self, query, documents):
        raise RuntimeError("reranker model failed to load")


def _index(reranker=None) -> SemanticIndex:
    settings = Settings(_env_file=None, qdrant_url="http://fake", rerank_enabled=True)
    return SemanticIndex(settings, controls=_FakeControls(reranker=reranker or FakeReranker()))


# ------------------------------------------------------------------ semantic.rerank


def test_rerank_blends_and_reorders() -> None:
    idx = _index()
    # Equal fused scores -> the cross-encoder decides the order (fusion weight is 0.7,
    # so with a fused tie the 0.3 rerank term breaks it toward 'winner').
    fused = [
        {"path": "a.md", "title": "loser doc", "description": "", "score": 0.5},
        {"path": "b.md", "title": "the winner doc", "description": "", "score": 0.5},
    ]
    out = idx.rerank("query", fused)
    assert all(r.get("reranked") is True for r in out)
    assert out[0]["path"] == "b.md"  # cross-encoder promoted the 'winner'
    # Blended score = 0.7*fused_norm + 0.3*sigmoid(ce); winner: fused_norm=0.5/0.5=1.0.
    expected = round(0.7 * 1.0 + 0.3 * _sigmoid(4.0), 4)
    assert out[0]["score"] == expected


def test_rerank_noop_for_single_result() -> None:
    idx = _index()
    one = [{"path": "a.md", "title": "x", "description": "", "score": 0.9}]
    out = idx.rerank("query", one)
    assert out == one  # untouched, no reranked flag
    assert "reranked" not in out[0]


def test_rerank_error_returns_unreranked_passthrough() -> None:
    idx = _index(reranker=BoomReranker())
    fused = [
        {"path": "a.md", "title": "loser", "description": "", "score": 0.9},
        {"path": "b.md", "title": "winner", "description": "", "score": 0.5},
    ]
    out = idx.rerank("query", fused)
    assert out == fused  # unchanged order, no reranked flag
    assert all("reranked" not in r for r in out)


# ------------------------------------------------------------------ kb_search wiring


class FakeSem:
    """Semantic engine that stays out of fusion (search=None -> text engine) but reranks."""

    def __init__(self) -> None:
        self.rerank_calls = 0

    def search(self, query, project=None, type=None, limit=8):  # noqa: A002
        return None

    def rerank(self, query, results):
        self.rerank_calls += 1
        out = [dict(r) for r in results]
        for r in out:
            r["reranked"] = True
        out.sort(key=lambda r: r["path"], reverse=True)  # deterministic reorder
        return out


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


async def _seed_two(store: KBStore) -> None:
    await store.kb_write(
        "projects/rz/decisions/aaa.md",
        "---\ntype: decision\ndescription: d\n---\n\nquorum sizing alpha [x](bbb.md)\n",
        "add",
    )
    await store.kb_write(
        "projects/rz/decisions/bbb.md",
        "---\ntype: decision\ndescription: d\n---\n\nquorum sizing beta [x](aaa.md)\n",
        "add",
    )


async def test_kb_search_reranks_when_enabled(store: KBStore) -> None:
    await _seed_two(store)
    store.settings.rerank_enabled = True
    sem = FakeSem()
    store.semantic = sem
    results = await store.kb_search("quorum", project="rz", expand=False)
    assert sem.rerank_calls == 1
    assert results and all(r.get("reranked") is True for r in results)


async def test_kb_search_no_rerank_when_disabled(store: KBStore) -> None:
    await _seed_two(store)
    store.settings.rerank_enabled = False
    sem = FakeSem()
    store.semantic = sem
    results = await store.kb_search("quorum", project="rz", expand=False)
    assert sem.rerank_calls == 0
    assert results and all("reranked" not in r for r in results)
