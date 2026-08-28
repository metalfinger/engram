"""Consolidation candidates — inbox captures that look like one topic.

The server CLUSTERS (arithmetic) and points; a session OFFERS; the human
decides; the client writes. Same rule as room precipitates, and the reason this
stays inside the no-server-LLM decision.

As with staleness, the load-bearing half is the RESET: a cluster offered once is
never offered again, or the report shows the same suggestion every night and
stops being read.
"""

from __future__ import annotations

import pytest

from engram_server.config import Settings
from engram_server.kbstore import KBStore
from engram_server.reconcile import (
    _fresh_clusters,
    _inbox_clusters,
    _load_offers,
    _render_clusters,
    _signature,
)


class _FakeIndex:
    """A stand-in semantic index: returns every OTHER path whose canned topic
    matches, so clustering can be tested without embeddings in CI."""

    def __init__(self, topics: dict[str, str]):
        self.topics = topics

    def search(self, query: str, limit: int = 6):
        want = query.strip().lower()
        return [
            {"path": path, "score": 0.9}
            for path, topic in self.topics.items()
            if topic.lower() in want or want in topic.lower()
        ][:limit]


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


def _capture(root, name: str, topic: str) -> str:
    rel = f"inbox/{name}.md"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\ntype: inbox\ntitle: {topic}\ndescription: {topic}\n---\n\n{topic}\n",
        encoding="utf-8", newline="\n",
    )
    return rel


def test_three_related_captures_cluster(store):
    root = store.root
    rels = [_capture(root, f"c{i}", "invoice chasing") for i in range(3)]
    idx = _FakeIndex({r: "invoice chasing" for r in rels})
    clusters = _inbox_clusters(root, idx, threshold=0.5)
    assert len(clusters) == 1
    assert sorted(clusters[0]) == sorted(rels)


def test_two_is_noise_not_a_cluster(store):
    """Two similar captures are a coincidence often enough that offering them
    would train the reader to ignore the section."""
    root = store.root
    rels = [_capture(root, f"p{i}", "parking receipts") for i in range(2)]
    idx = _FakeIndex({r: "parking receipts" for r in rels})
    assert _inbox_clusters(root, idx, threshold=0.5) == []


def test_unrelated_captures_do_not_cluster(store):
    root = store.root
    topics = {"a": "invoice", "b": "sprite art", "c": "dentist"}
    mapping = {_capture(root, k, v): v for k, v in topics.items()}
    assert _inbox_clusters(root, _FakeIndex(mapping), threshold=0.5) == []


def test_a_chain_is_one_cluster(store):
    """A resembles B, B resembles C, A and C never match directly — connected
    components is what makes this a cluster rather than a pile of pairs."""
    root = store.root
    a = _capture(root, "a", "invoice chasing")
    b = _capture(root, "b", "invoice chasing")
    c = _capture(root, "c", "invoice chasing")
    idx = _FakeIndex({a: "invoice chasing", b: "invoice chasing", c: "invoice chasing"})
    clusters = _inbox_clusters(root, idx, threshold=0.5)
    assert len(clusters) == 1 and len(clusters[0]) == 3


def test_archived_captures_are_left_alone(store):
    """Accepting a distillation moves sources to inbox/archive/ — they must not
    come straight back as a fresh cluster."""
    root = store.root
    live = [_capture(root, f"l{i}", "topic one") for i in range(3)]
    arch = root / "inbox" / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (arch / f"old{i}.md").write_text(
            "---\ntype: inbox\ntitle: topic one\ndescription: topic one\n---\n\nx\n",
            encoding="utf-8", newline="\n")
    idx = _FakeIndex({r: "topic one" for r in live})
    clusters = _inbox_clusters(root, idx, threshold=0.5)
    assert len(clusters) == 1
    assert all("/archive/" not in m for m in clusters[0])


def test_a_cluster_is_offered_exactly_once(store):
    """THE RESET. Declining must mean declined — otherwise the same suggestion
    appears nightly and the section stops being read."""
    root = store.root
    rels = [_capture(root, f"o{i}", "one topic") for i in range(3)]
    idx = _FakeIndex({r: "one topic" for r in rels})

    first = _fresh_clusters(root, idx, threshold=0.5)
    assert len(first) == 1, "offered on the first pass"
    assert _signature(first[0]) in _load_offers(root)

    second = _fresh_clusters(root, idx, threshold=0.5)
    assert second == [], "never offered twice"


def test_a_grown_cluster_is_a_new_offer(store):
    """A different membership is a different suggestion — the signature is the
    sorted paths, so adding a fourth capture re-offers legitimately."""
    root = store.root
    rels = [_capture(root, f"g{i}", "growing topic") for i in range(3)]
    idx = _FakeIndex({r: "growing topic" for r in rels})
    assert len(_fresh_clusters(root, idx, threshold=0.5)) == 1

    rels.append(_capture(root, "g3", "growing topic"))
    idx = _FakeIndex({r: "growing topic" for r in rels})
    grown = _fresh_clusters(root, idx, threshold=0.5)
    assert len(grown) == 1 and len(grown[0]) == 4


def test_the_offers_file_lives_outside_the_checkout(store):
    """An untracked file inside the brain would trip the clean-checkout guard and
    block every write — and this is server-local state, not knowledge."""
    root = store.root
    rels = [_capture(root, f"x{i}", "outside test") for i in range(3)]
    _fresh_clusters(root, _FakeIndex({r: "outside test" for r in rels}), threshold=0.5)
    assert not list(root.rglob(".engram-consolidation-offers.json"))
    assert (root.parent / ".engram-consolidation-offers.json").is_file()


def test_the_section_renders_members_and_the_promise(store):
    body = _render_clusters([["inbox/a.md", "inbox/b.md", "inbox/c.md"]])
    assert "Consolidation candidates (1)" in body
    assert "inbox/a.md" in body
    assert "your yes" in body and "archive" in body
    assert "None" in _render_clusters([])
