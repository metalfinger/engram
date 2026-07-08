"""Centroid rebuild-guard tests — semantic.centroid_drift + the kb_write advisory wiring.

Fully offline: a bag-of-words fake embedder makes a summary that shares vocabulary with
its sources score HIGH, and unrelated text score LOW.
"""

from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace

import pytest

from engram_server.config import Settings
from engram_server.kbstore import KBStore
from engram_server.semantic import SemanticIndex, _FakeControls


# ------------------------------------------------------------------ fakes


class FakeEmbedder:
    DIM = 24

    def embed(self, texts):
        for t in texts:
            vec = [0.0] * self.DIM
            for tok in re.findall(r"\w+", t.lower()):
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.DIM
                vec[h] += 1.0
            yield vec


class FakeQdrant:
    def __init__(self) -> None:
        self.points: dict[str, dict] = {}

    def collection_exists(self, collection_name):
        return collection_name in self.points

    def create_collection(self, collection_name, vectors_config):
        self.points[collection_name] = {}

    def create_payload_index(self, collection_name, field_name, field_schema=None):
        return None

    def upsert(self, collection_name, points):
        for p in points:
            self.points[collection_name][p.id] = (p.vector, p.payload)

    def delete(self, collection_name, points_selector):
        if hasattr(points_selector, "points"):
            for pid in points_selector.points:
                self.points[collection_name].pop(str(pid), None)
            return
        cond = points_selector.must[0]
        drop = [pid for pid, (_v, pl) in self.points[collection_name].items() if pl.get(cond.key) == cond.match.value]
        for pid in drop:
            del self.points[collection_name][pid]

    def scroll(self, collection_name, scroll_filter=None, with_vectors=True, with_payload=True, limit=10000):
        recs = []
        for pid, (vec, pl) in self.points[collection_name].items():
            if scroll_filter is not None and not all(pl.get(c.key) == c.match.value for c in scroll_filter.must):
                continue
            recs.append(SimpleNamespace(id=pid, vector=list(vec), payload=pl))
        return recs, None


def _index() -> SemanticIndex:
    settings = Settings(_env_file=None, qdrant_url="http://fake", qdrant_collection="test-col")
    return SemanticIndex(settings, controls=_FakeControls(client=FakeQdrant(), embedder=FakeEmbedder()))


_SOURCE = (
    "---\ntype: decision\ntitle: Qdrant\ndescription: d\n---\n\n"
    "Qdrant vector database chosen for semantic search over the brain bundle.\n"
)


# ------------------------------------------------------------------ centroid_drift


def test_centroid_drift_high_for_grounded_summary_low_for_unrelated() -> None:
    idx = _index()
    idx.upsert_file("projects/alt/decisions/qdrant.md", _SOURCE)
    grounded = idx.centroid_drift(
        "Qdrant vector database chosen for semantic search over the brain.",
        ["projects/alt/decisions/qdrant.md"],
    )
    unrelated = idx.centroid_drift(
        "Banana pancake breakfast recipe with maple syrup.",
        ["projects/alt/decisions/qdrant.md"],
    )
    assert grounded is not None and unrelated is not None
    assert grounded > unrelated
    assert grounded > 0.5  # shares the source's vocabulary


def test_centroid_drift_none_without_sources() -> None:
    idx = _index()
    assert idx.centroid_drift("anything at all", []) is None


def test_centroid_drift_none_when_source_has_no_indexed_vectors() -> None:
    idx = _index()  # nothing indexed
    assert idx.centroid_drift("anything", ["projects/alt/decisions/missing.md"]) is None


def test_centroid_drift_none_when_backend_down() -> None:
    class Boom:
        def collection_exists(self, *a, **k):
            raise RuntimeError("down")

    idx = SemanticIndex(
        Settings(_env_file=None, qdrant_url="http://fake"),
        controls=_FakeControls(client=Boom(), embedder=FakeEmbedder()),
    )
    assert idx.centroid_drift("x", ["p/a.md"]) is None


# ------------------------------------------------------------------ kb_write wiring


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


def test_artifact_drift_warning_none_when_semantic_off(store: KBStore) -> None:
    store.semantic = None
    assert store._artifact_drift_warning("body", ["projects/alt/context.md"]) is None


def test_artifact_drift_warning_fires_below_threshold(store: KBStore) -> None:
    store.semantic = SimpleNamespace(centroid_drift=lambda body, srcs: 0.20)
    warning = store._artifact_drift_warning("body", ["projects/alt/context.md"])
    assert warning is not None and "diverged from its sources" in warning
    # Above threshold -> no warning.
    store.semantic = SimpleNamespace(centroid_drift=lambda body, srcs: 0.90)
    assert store._artifact_drift_warning("body", ["projects/alt/context.md"]) is None
    # None similarity -> no warning.
    store.semantic = SimpleNamespace(centroid_drift=lambda body, srcs: None)
    assert store._artifact_drift_warning("body", ["projects/alt/context.md"]) is None


async def test_kb_write_artifact_appends_drift_warning(store: KBStore) -> None:
    store.semantic = SimpleNamespace(centroid_drift=lambda body, srcs: 0.15)
    result = await store.kb_write(
        "projects/alt/artifacts/2026-07-summary.md",
        "---\ntype: artifact\ntitle: Summary\ndescription: d\n"
        "sources: [projects/alt/context.md]\n---\n\nUngrounded prose here.\n",
        "add artifact",
    )
    assert any("diverged from its sources" in w for w in result["warnings"])
