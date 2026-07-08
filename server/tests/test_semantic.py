"""Semantic index unit tests — fully offline via a fake Qdrant client + fake embedder.

No live Qdrant, no model download: the fake embedder is a deterministic bag-of-words
hash and the fake client is an in-memory point store that honours real qdrant_client
Filter/FieldCondition objects. Also covers kb_search's semantic->text fallback contract.
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from engram_server.config import Settings
from engram_server.kbstore import KBStore
from engram_server.semantic import SemanticIndex, _FakeControls


# ------------------------------------------------------------------ fakes


class FakeEmbedder:
    DIM = 16

    def embed(self, texts):
        for t in texts:
            vec = [0.0] * self.DIM
            for tok in re.findall(r"\w+", t.lower()):
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.DIM
                vec[h] += 1.0
            yield vec


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class FakeQdrant:
    """In-memory stand-in exercising the exact call surface SemanticIndex uses."""

    def __init__(self) -> None:
        self.points: dict[str, dict] = {}  # collection -> {id: (vector, payload)}
        self.created: list[str] = []

    def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self.points

    def create_collection(self, collection_name: str, vectors_config) -> None:
        self.points[collection_name] = {}
        self.created.append(collection_name)

    def upsert(self, collection_name: str, points) -> None:
        for p in points:
            self.points[collection_name][p.id] = (p.vector, p.payload)

    def create_payload_index(self, collection_name, field_name, field_schema=None):
        return None  # cloud requires keyword indexes for filtered fields; no-op in the fake

    def delete(self, collection_name: str, points_selector) -> None:
        # Two selector shapes: a Filter (delete-by-path) or a PointIdsList (delete the
        # removed chunk ids during an incremental upsert).
        if hasattr(points_selector, "points"):  # PointIdsList
            for pid in points_selector.points:
                self.points[collection_name].pop(str(pid), None)
            return
        cond = points_selector.must[0]
        key, val = cond.key, cond.match.value
        drop = [pid for pid, (_v, pl) in self.points[collection_name].items() if pl.get(key) == val]
        for pid in drop:
            del self.points[collection_name][pid]

    def scroll(self, collection_name, scroll_filter=None, with_vectors=True, with_payload=True, limit=10000):
        recs = []
        for pid, (vec, pl) in self.points[collection_name].items():
            if scroll_filter is not None and not self._match(scroll_filter, pl):
                continue
            recs.append(SimpleNamespace(id=pid, vector=list(vec), payload=pl))
            if len(recs) >= limit:
                break
        return recs, None

    # Mirror the REAL qdrant-client >= 1.12 API: query_points (returns .points).
    # There is deliberately NO .search() here — the old fake had one and it hid a
    # removed-API crash in production.
    def query_points(self, collection_name, query, query_filter=None, limit=10, with_payload=True):
        out = []
        for _pid, (vec, pl) in self.points[collection_name].items():
            if query_filter is not None and not self._match(query_filter, pl):
                continue
            out.append(SimpleNamespace(score=_cosine(query, vec), payload=pl))
        out.sort(key=lambda h: h.score, reverse=True)
        return SimpleNamespace(points=out[:limit])

    @staticmethod
    def _match(query_filter, payload) -> bool:
        return all(payload.get(c.key) == c.match.value for c in query_filter.must)


def _index() -> SemanticIndex:
    settings = Settings(_env_file=None, qdrant_url="http://fake", qdrant_collection="test-col")
    return SemanticIndex(settings, controls=_FakeControls(client=FakeQdrant(), embedder=FakeEmbedder()))


_DOC = (
    "---\ntype: decision\ntitle: Qdrant Chosen\ndescription: Vector store pick.\n"
    "tags: [search, vectors]\ntimestamp: 2026-07-01T00:00:00Z\nproject: alt\n---\n\n"
    "Intro paragraph about the choice.\n\n"
    "## Rationale\n\nQdrant Cloud is managed and cheap.\n\n"
    "## Alternatives\n\nWe looked at pgvector and weaviate.\n"
)


# ------------------------------------------------------------------ chunking


def test_chunk_file_splits_by_heading() -> None:
    idx = _index()
    chunks = idx.chunk_file("projects/alt/decisions/2026-07-qdrant.md", _DOC)
    headings = [c.heading for c in chunks]
    # intro chunk keyed by title, then one per ## section
    assert headings[0] == "Qdrant Chosen"
    assert "Rationale" in headings
    assert "Alternatives" in headings
    # intro folds title + description + tags + preamble
    intro = chunks[0].text
    assert "Qdrant Chosen" in intro and "Vector store pick." in intro
    assert "search" in intro and "Intro paragraph" in intro


def test_point_ids_deterministic_and_unique() -> None:
    idx = _index()
    a = idx._point_id("p/x.md", "Rationale", 1)
    b = idx._point_id("p/x.md", "Rationale", 1)
    c = idx._point_id("p/x.md", "Rationale", 2)
    assert a == b  # deterministic
    assert a != c  # index disambiguates


def test_long_section_is_capped_into_multiple_chunks() -> None:
    idx = _index()
    big = "word " * 800  # ~4000 chars, one ## section
    doc = f"---\ntype: idea\ntitle: Big\ndescription: d\n---\n\n## Section\n\n{big}\n"
    chunks = [c for c in idx.chunk_file("p/big.md", doc) if c.heading == "Section"]
    assert len(chunks) >= 2
    assert all(len(c.text) <= 1500 for c in chunks)


# ------------------------------------------------------------------ payload + roundtrip


def test_payload_shape() -> None:
    idx = _index()
    chunk = idx.chunk_file("projects/alt/decisions/2026-07-qdrant.md", _DOC)[0]
    payload = idx._payload("projects/alt/decisions/2026-07-qdrant.md", _DOC, chunk)
    assert payload["path"] == "projects/alt/decisions/2026-07-qdrant.md"
    assert payload["project"] == "alt"
    assert payload["type"] == "decision"
    assert payload["title"] == "Qdrant Chosen"
    assert payload["tags"] == ["search", "vectors"]
    assert payload["heading"] == "Qdrant Chosen"
    assert payload["timestamp"] == "2026-07-01T00:00:00Z"


def test_upsert_then_search_roundtrip_and_dedup() -> None:
    idx = _index()
    assert idx.upsert_file("projects/alt/decisions/2026-07-qdrant.md", _DOC) is True
    results = idx.search("qdrant vectors")
    assert results is not None
    # deduped to one row per path even though several chunks matched
    paths = [r["path"] for r in results]
    assert paths.count("projects/alt/decisions/2026-07-qdrant.md") == 1
    top = results[0]
    assert top["title"] == "Qdrant Chosen"
    assert set(top) == {"path", "title", "description", "score", "matched_heading"}


def test_upsert_replaces_prior_points_no_orphans() -> None:
    idx = _index()
    idx.upsert_file("p/x.md", "---\ntype: idea\ntitle: X\ndescription: d\n---\n\n## A\n\nalpha\n\n## B\n\nbeta\n")
    client = idx._ctl.client
    first_count = len(client.points["test-col"])
    # rewrite with fewer sections
    idx.upsert_file("p/x.md", "---\ntype: idea\ntitle: X\ndescription: d\n---\n\n## A\n\nalpha only\n")
    second_count = len(client.points["test-col"])
    assert second_count < first_count  # stale B chunk removed
    assert all(pl["path"] == "p/x.md" for _v, pl in client.points["test-col"].values())


def test_delete_file_removes_points() -> None:
    idx = _index()
    idx.upsert_file("p/x.md", _DOC)
    assert idx._ctl.client.points["test-col"]
    assert idx.delete_file("p/x.md") is True
    assert not idx._ctl.client.points["test-col"]


# ------------------------------------------------------------------ incremental embed fingerprints


class CountingEmbedder(FakeEmbedder):
    """FakeEmbedder that records how many texts it embedded — the incremental metric."""

    def __init__(self) -> None:
        self.embedded = 0

    def embed(self, texts):
        texts = list(texts)
        self.embedded += len(texts)
        return super().embed(texts)


def _counting_index() -> tuple[SemanticIndex, CountingEmbedder]:
    emb = CountingEmbedder()
    settings = Settings(_env_file=None, qdrant_url="http://fake", qdrant_collection="test-col")
    idx = SemanticIndex(settings, controls=_FakeControls(client=FakeQdrant(), embedder=emb))
    return idx, emb


_SECS = "---\ntype: idea\ntitle: X\ndescription: d\n---\n\n## A\n\nalpha\n\n## B\n\nbeta\n"


def test_incremental_unchanged_file_embeds_zero() -> None:
    idx, emb = _counting_index()
    idx.upsert_file("p/x.md", _DOC)
    emb.embedded = 0  # ignore the seed embed + dimension probe
    assert idx.upsert_file("p/x.md", _DOC) is True  # identical content
    assert emb.embedded == 0


def test_incremental_changed_section_reembeds_only_that_chunk() -> None:
    idx, emb = _counting_index()
    idx.upsert_file("p/x.md", _SECS)
    emb.embedded = 0
    changed = "---\ntype: idea\ntitle: X\ndescription: d\n---\n\n## A\n\nalpha\n\n## B\n\nbeta rewritten\n"
    idx.upsert_file("p/x.md", changed)
    assert emb.embedded == 1  # only the B section's chunk re-embedded


def test_incremental_removed_section_deletes_its_point() -> None:
    idx, _emb = _counting_index()
    idx.upsert_file("p/x.md", _SECS)
    before = len(idx._ctl.client.points["test-col"])
    idx.upsert_file("p/x.md", "---\ntype: idea\ntitle: X\ndescription: d\n---\n\n## A\n\nalpha\n")
    pts = idx._ctl.client.points["test-col"]
    assert len(pts) == before - 1
    assert "B" not in {pl["heading"] for _v, pl in pts.values()}  # no orphan


def test_incremental_frontmatter_change_refreshes_payload_without_reembedding_body() -> None:
    idx, emb = _counting_index()
    idx.upsert_file("p/x.md", "---\ntype: idea\ntitle: Old\ndescription: d\n---\n\n## A\n\nalpha\n")
    emb.embedded = 0
    idx.upsert_file("p/x.md", "---\ntype: idea\ntitle: New\ndescription: d\n---\n\n## A\n\nalpha\n")
    # intro chunk embeds (title lives in its text); section A's body is unchanged so it
    # reuses its vector — but its payload title must still be refreshed (no stale "Old").
    assert emb.embedded == 1
    titles = {pl["title"] for _v, pl in idx._ctl.client.points["test-col"].values()}
    assert titles == {"New"}


def test_incremental_error_falls_back_to_full_reembed() -> None:
    idx, emb = _counting_index()
    idx.upsert_file("p/x.md", _DOC)  # seed with chunk_sha payloads

    def _boom(*a, **k):
        raise RuntimeError("scroll boom")

    idx._ctl.client.scroll = _boom  # break the incremental path only
    emb.embedded = 0
    assert idx.upsert_file("p/x.md", _DOC) is True  # fell back to full re-embed
    assert emb.embedded > 0  # the whole file was re-embedded


def test_search_filters_project_and_type() -> None:
    idx = _index()
    idx.upsert_file("projects/alt/specs/api.md", "---\ntype: spec\ntitle: Alt API\ndescription: d\n---\n\ngrafana dashboard\n")
    idx.upsert_file("projects/hyprlocl/specs/site.md", "---\ntype: spec\ntitle: Site\ndescription: d\n---\n\ngrafana panel\n")
    idx.upsert_file("projects/alt/decisions/d.md", "---\ntype: decision\ntitle: D\ndescription: d\n---\n\ngrafana choice\n")

    alt = {r["path"] for r in idx.search("grafana", project="alt")}
    assert alt == {"projects/alt/specs/api.md", "projects/alt/decisions/d.md"}

    specs = {r["path"] for r in idx.search("grafana", type="spec")}
    assert specs == {"projects/alt/specs/api.md", "projects/hyprlocl/specs/site.md"}


def test_search_empty_query_returns_none() -> None:
    assert _index().search("   ") is None


def test_full_reindex_walks_bundle(tmp_path: Path) -> None:
    (tmp_path / "projects/alt").mkdir(parents=True)
    (tmp_path / "projects/alt/x.md").write_text(
        "---\ntype: idea\ntitle: X\ndescription: d\n---\n\nbody\n", encoding="utf-8"
    )
    (tmp_path / "projects/alt/index.md").write_text("# alt\n\n* [X](x.md)\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/config.md").write_text("---\ntype: junk\ntitle: J\ndescription: d\n---\nx\n", encoding="utf-8")
    idx = _index()
    summary = idx.full_reindex(tmp_path)
    assert summary["indexed"] == 1  # only the concept; index.md and .git skipped
    paths = {pl["path"] for _v, pl in idx._ctl.client.points["test-col"].values()}
    assert paths == {"projects/alt/x.md"}


# ------------------------------------------------------------------ backend-down softness


def test_backend_errors_never_raise() -> None:
    class Boom:
        def collection_exists(self, *a, **k):
            raise RuntimeError("network down")

    idx = SemanticIndex(
        Settings(_env_file=None, qdrant_url="http://fake"),
        controls=_FakeControls(client=Boom(), embedder=FakeEmbedder()),
    )
    assert idx.upsert_file("p/x.md", _DOC) is False
    assert idx.delete_file("p/x.md") is False
    assert idx.search("anything") is None
    assert idx.ensure_collection() is False


# ------------------------------------------------------------------ kb_search fallback contract


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


async def test_kb_search_falls_back_to_text_when_semantic_none(store: KBStore) -> None:
    await store.kb_write(
        "projects/alt/decisions/2026-07-zzz.md",
        "---\ntype: decision\ndescription: A test.\n---\n\nUnobtanium mineral notes.\n",
        "add",
    )
    store.semantic = SimpleNamespace(search=lambda *a, **k: None)  # semantic unavailable
    results = await store.kb_search("unobtanium")
    assert results
    assert all(r["engine"] == "text" for r in results)


async def test_kb_search_falls_back_when_semantic_empty(store: KBStore) -> None:
    await store.kb_write(
        "projects/alt/decisions/2026-07-yyy.md",
        "---\ntype: decision\ndescription: A test.\n---\n\nCavorite antigravity notes.\n",
        "add",
    )
    store.semantic = SimpleNamespace(search=lambda *a, **k: [])  # populated=no / no hits
    results = await store.kb_search("cavorite")
    assert results
    assert all(r["engine"] == "text" for r in results)


async def test_kb_search_semantic_only_when_text_has_no_hits(store: KBStore) -> None:
    # Semantic returns a hit for a doc the text scorer can't reach (no file / no token
    # match), so text is empty and semantic serves ALONE — engine='semantic', score kept.
    # (When text ALSO has hits the two are fused into engine='hybrid'; see test_search_fusion.)
    store.semantic = SimpleNamespace(
        search=lambda *a, **k: [
            {"path": "projects/alt/x.md", "title": "X", "description": "d", "score": 0.9, "matched_heading": None}
        ]
    )
    results = await store.kb_search("zzqqxx-token-that-matches-no-file")
    assert results == [
        {"path": "projects/alt/x.md", "title": "X", "description": "d", "score": 0.9,
         "matched_heading": None, "engine": "semantic"}
    ]
