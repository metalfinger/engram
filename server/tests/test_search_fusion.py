"""Wave-2 search: pure fusion/expansion/window helpers + kb_search orchestration.

The pure helpers (query_variants, union_max, rrf_fuse, window_search) are exercised
directly; the hybrid/multi-query/time-window behavior of kb_search is exercised end to
end against a real brain checkout with a planted fake semantic engine.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from engram_server.config import Settings
from engram_server.kbstore import KBStore
from engram_server.search import query_variants, rrf_fuse, union_max, window_search


# ------------------------------------------------------------------ pure helpers


def test_query_variants_raw_keyword_synonym() -> None:
    variants = query_variants("why did we pick oauth")
    assert variants[0] == "why did we pick oauth"  # raw always first
    # keyword-only variant strips stopwords (why/did/we)
    assert any(v == "pick oauth" for v in variants)
    # synonym variant expands oauth -> auth/authentication/login (appended, deterministic)
    assert any("authentication" in v for v in variants)


def test_query_variants_dedupes_and_stays_small() -> None:
    # a single content word with no synonyms yields just the raw query (no empty/dupes)
    assert query_variants("cavorite") == ["cavorite"]
    assert len(query_variants("why did we pick oauth token")) <= 3


def test_union_max_keeps_best_score_per_path() -> None:
    a = [{"path": "x", "score": 0.2}, {"path": "y", "score": 0.9}]
    b = [{"path": "x", "score": 0.7}]
    merged = {r["path"]: r["score"] for r in union_max([a, b])}
    assert merged == {"x": 0.7, "y": 0.9}


def test_rrf_fuse_rewards_agreement() -> None:
    # 'shared' sits at rank 1 in each list; 'top_a'/'top_b' each lead only one list.
    list_a = [{"path": "top_a", "score": 0.9}, {"path": "shared", "score": 0.5}]
    list_b = [{"path": "top_b", "score": 0.9}, {"path": "shared", "score": 0.4}]
    fused = rrf_fuse([list_a, list_b])
    assert fused[0]["path"] == "shared"  # present in both -> outranks either leader
    scores = {r["path"]: r["score"] for r in fused}
    assert scores["shared"] > scores["top_a"] and scores["shared"] > scores["top_b"]


def test_window_search_bounds_and_failure_soft(tmp_path) -> None:
    (tmp_path / "projects/alt/decisions").mkdir(parents=True)
    (tmp_path / "projects/alt/decisions/june.md").write_text(
        "---\ntype: decision\ntitle: June\ndescription: d\ntimestamp: 2026-06-15T00:00:00Z\n---\n\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / "projects/alt/decisions/aug.md").write_text(
        "---\ntype: decision\ntitle: Aug\ndescription: d\ntimestamp: 2026-08-20T00:00:00Z\n---\n\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / "projects/alt/decisions/undated.md").write_text(
        "---\ntype: decision\ntitle: Undated\ndescription: d\n---\n\nbody\n", encoding="utf-8"
    )
    got = {r["path"] for r in window_search(tmp_path, since="2026-06-01", until="2026-06-30")}
    assert got == {"projects/alt/decisions/june.md"}  # aug out of window, undated skipped
    # an unparseable bound disables the pass rather than matching everything
    assert window_search(tmp_path, since="not-a-date") == []


# ------------------------------------------------------------------ kb_search orchestration


class FakeSem:
    """Stand-in semantic engine: returns fixed rows (optionally None to simulate down)."""

    def __init__(self, rows) -> None:
        self.rows = rows

    def search(self, query, project=None, type=None, limit=8):  # noqa: A002
        if self.rows is None:
            return None
        return [dict(r) for r in self.rows]


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


def _row(path, score):
    return {"path": path, "title": path, "description": "d", "score": score, "matched_heading": None}


async def test_hybrid_fusion_outranks_either_engine_alone(store: KBStore) -> None:
    # Text relevance: B (3 'zebra' hits) beats A (1 hit).
    await store.kb_write(
        "projects/zeb/decisions/a.md",
        "---\ntype: decision\ndescription: d\n---\n\nzebra shared [x](b.md)\n",
        "add",
    )
    await store.kb_write(
        "projects/zeb/decisions/b.md",
        "---\ntype: decision\ndescription: d\n---\n\nzebra zebra zebra textonly [x](a.md)\n",
        "add",
    )
    a_path = "projects/zeb/decisions/a.md"
    b_path = "projects/zeb/decisions/b.md"

    # Text engine alone ranks B first.
    store.semantic = None
    text_only = await store.kb_search("zebra", project="zeb", expand=False)
    assert text_only[0]["path"] == b_path
    assert all(r["engine"] == "text" for r in text_only)

    # Semantic returns A (and a sem-only C). Hybrid RRF puts A (in BOTH lists) on top.
    store.semantic = FakeSem([_row(a_path, 0.9), _row("projects/zeb/c.md", 0.8)])
    fused = await store.kb_search("zebra", project="zeb", expand=False)
    assert all(r["engine"] == "hybrid" for r in fused)
    assert fused[0]["path"] == a_path  # fusion flipped the text-only order
    scores = {r["path"]: r["score"] for r in fused}
    assert scores[a_path] > scores[b_path]
    assert "projects/zeb/c.md" in scores  # sem-only hit carried through


async def test_multi_query_finds_synonym_worded_doc(store: KBStore) -> None:
    # Body says 'authentication', never 'oauth'. Only the synonym variant can reach it.
    await store.kb_write(
        "projects/synzz/decisions/d.md",
        "---\ntype: decision\ndescription: The sign-in pick.\n---\n\n"
        "We chose authentication with an allowlist. [x](e.md)\n",
        "add",
    )
    target = "projects/synzz/decisions/d.md"
    # expand=False: literal 'oauth' matches nothing in scope.
    assert await store.kb_search("oauth", project="synzz", expand=False) == []
    # expand=True: synonym variant (oauth -> auth/authentication) reaches the doc.
    hits = await store.kb_search("oauth", project="synzz", expand=True)
    assert any(r["path"] == target for r in hits)


async def test_time_window_unions_in_window_doc_regardless_of_score(store: KBStore) -> None:
    await store.kb_write(
        "projects/winz/decisions/june.md",
        "---\ntype: decision\ndescription: June call.\ntimestamp: 2026-06-15T00:00:00Z\n---\n\n"
        "quorum sizing notes [x](aug.md)\n",
        "add",
    )
    await store.kb_write(
        "projects/winz/decisions/aug.md",
        "---\ntype: decision\ndescription: Aug call.\ntimestamp: 2026-08-20T00:00:00Z\n---\n\n"
        "quorum sizing notes [x](june.md)\n",
        "add",
    )
    # A query that matches neither doc's text — only the window pass can surface them.
    res = await store.kb_search(
        "nonexistentzzz", project="winz", since="2026-06-01", until="2026-06-30"
    )
    june = [r for r in res if r["path"] == "projects/winz/decisions/june.md"]
    assert june and june[0]["window"] is True
    assert not any(r["path"] == "projects/winz/decisions/aug.md" for r in res)  # out of window


async def test_time_window_unparseable_date_does_not_crash(store: KBStore) -> None:
    await store.kb_write(
        "projects/winz/decisions/x.md",
        "---\ntype: decision\ndescription: d\n---\n\nalpha beta [x](y.md)\n",
        "add",
    )
    # Bad bound is failure-soft: normal (non-window) results, no exception.
    res = await store.kb_search("alpha", project="winz", since="garbage")
    assert res and all("window" not in r for r in res)


async def test_expand_false_is_literal_single_query(store: KBStore) -> None:
    # 'oauth' is NOT a substring of 'authentication', so only the synonym-expanded
    # variant can reach this doc — expand=False must stay literal and miss it.
    await store.kb_write(
        "projects/litz/decisions/d.md",
        "---\ntype: decision\ndescription: d\n---\n\nWe use authentication here. [x](e.md)\n",
        "add",
    )
    assert await store.kb_search("oauth", project="litz", expand=False) == []
    assert await store.kb_search("oauth", project="litz", expand=True)


async def test_empty_query_raises(store: KBStore) -> None:
    from engram_server.errors import KBError

    with pytest.raises(KBError):
        await store.kb_search("   ")
