"""Search isolation (M0.5) — tenant scoping in SemanticIndex.

All tenants share ONE Qdrant collection; the mandatory ``user_id`` filter on every
read/mutation is the only thing standing between them. These tests run fully
offline (fake Qdrant client + fake embedder, no live Qdrant) and exercise the
real contract end-to-end: two tenants' SemanticIndex instances pointed at the
SAME fake collection must never see or delete each other's points, and the
"hiren" tenant (production's pre-multi-tenant data) must keep working against
points that predate the ``user_id`` field entirely.

The fake client here is deliberately more capable than test_semantic.py's
FakeQdrant: it recursively evaluates nested ``Filter(should=[...])`` conditions
and ``IsEmptyCondition``, because the tenant filter for "hiren" is exactly that
shape (see semantic.py's ``_tenant_condition``). NOTE: test_semantic.py's own
FakeQdrant._match does NOT support nested Filter/IsEmptyCondition and its tests
will fail once every call carries the tenant condition — see the risk note in
the handoff, this is a pre-existing fake that needs the same recursive matcher.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from types import SimpleNamespace
from typing import Any

from engram_server.config import Settings
from engram_server.semantic import _NAMESPACE, SemanticIndex, _FakeControls


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
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _condition_matches(cond: Any, payload: dict[str, Any]) -> bool:
    """Evaluate one condition against a payload — FieldCondition, IsEmptyCondition,
    or a nested Filter (must/should), recursively. Mirrors real Qdrant semantics
    closely enough for these offline tests."""
    is_empty = getattr(cond, "is_empty", None)
    if is_empty is not None:
        key = is_empty.key
        val = payload.get(key)
        return val is None or val == "" or val == []
    match = getattr(cond, "match", None)
    if match is not None:
        return payload.get(cond.key) == match.value
    # nested Filter
    if hasattr(cond, "must") or hasattr(cond, "should"):
        return _filter_matches(cond, payload)
    raise TypeError(f"unsupported fake condition: {cond!r}")


def _filter_matches(filt: Any, payload: dict[str, Any]) -> bool:
    must = filt.must or []
    should = filt.should or []
    if must and not all(_condition_matches(c, payload) for c in must):
        return False
    if should and not any(_condition_matches(c, payload) for c in should):
        return False
    return True


class FakeQdrant:
    """In-memory Qdrant stand-in shared across tenants, like the real cloud collection.
    Records the last filter passed to search/scroll/delete so tests can inspect
    the exact structure SemanticIndex built (spec items c/d/e)."""

    def __init__(self) -> None:
        self.points: dict[str, dict] = {}
        self.created: list[str] = []
        self.last_search_filter: Any = None
        self.last_scroll_filter: Any = None
        self.last_delete_selector: Any = None

    def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self.points

    def create_collection(self, collection_name: str, vectors_config) -> None:
        self.points[collection_name] = {}
        self.created.append(collection_name)

    def create_payload_index(self, collection_name, field_name, field_schema=None):
        return None

    def upsert(self, collection_name: str, points) -> None:
        for p in points:
            self.points[collection_name][p.id] = (p.vector, p.payload)

    def delete(self, collection_name: str, points_selector) -> None:
        if hasattr(points_selector, "points"):  # PointIdsList
            for pid in points_selector.points:
                self.points[collection_name].pop(str(pid), None)
            return
        self.last_delete_selector = points_selector
        drop = [
            pid
            for pid, (_v, pl) in self.points[collection_name].items()
            if _filter_matches(points_selector, pl)
        ]
        for pid in drop:
            del self.points[collection_name][pid]

    def scroll(self, collection_name, scroll_filter=None, with_vectors=True, with_payload=True, limit=10000):
        self.last_scroll_filter = scroll_filter
        recs = []
        for pid, (vec, pl) in self.points[collection_name].items():
            if scroll_filter is not None and not _filter_matches(scroll_filter, pl):
                continue
            recs.append(SimpleNamespace(id=pid, vector=list(vec), payload=pl))
            if len(recs) >= limit:
                break
        return recs, None

    def query_points(self, collection_name, query, query_filter=None, limit=10, with_payload=True):
        self.last_search_filter = query_filter
        out = []
        for _pid, (vec, pl) in self.points[collection_name].items():
            if query_filter is not None and not _filter_matches(query_filter, pl):
                continue
            out.append(SimpleNamespace(score=_cosine(query, vec), payload=pl))
        out.sort(key=lambda h: h.score, reverse=True)
        return SimpleNamespace(points=out[:limit])


def _index_for(tenant: str, client: FakeQdrant | None = None) -> SemanticIndex:
    settings = Settings(
        _env_file=None, qdrant_url="http://fake", qdrant_collection="test-col", tenant_id=tenant
    )
    return SemanticIndex(
        settings, controls=_FakeControls(client=client or FakeQdrant(), embedder=FakeEmbedder())
    )


_DOC = (
    "---\ntype: idea\ntitle: Shared Path\ndescription: d\n---\n\n## Body\n\nsome shared content here\n"
)


def _legacy_point_id(path: str, heading: str, idx: int) -> str:
    """The pre-multi-tenancy point id format (no tenant folded in) — computed
    independently here so the test proves "hiren" still matches it exactly."""
    return str(uuid.uuid5(_NAMESPACE, f"{path}#{heading}#{idx}"))


# ------------------------------------------------------------------ (a) payload stamping


def test_payload_carries_tenant_from_settings() -> None:
    idx = _index_for("acme")
    chunk = idx.chunk_file("p/x.md", _DOC)[0]
    payload = idx._payload("p/x.md", _DOC, chunk)
    assert payload["user_id"] == "acme"


def test_payload_carries_hiren_by_default() -> None:
    idx = _index_for("hiren")
    chunk = idx.chunk_file("p/x.md", _DOC)[0]
    payload = idx._payload("p/x.md", _DOC, chunk)
    assert payload["user_id"] == "hiren"


# ------------------------------------------------------------------ (b) point id namespacing


def test_point_ids_differ_across_tenants_for_same_path() -> None:
    acme = _index_for("acme")._point_id("p/x.md", "Body", 0)
    bob = _index_for("bob")._point_id("p/x.md", "Body", 0)
    hiren = _index_for("hiren")._point_id("p/x.md", "Body", 0)
    assert len({acme, bob, hiren}) == 3


def test_hiren_point_id_matches_legacy_format_exactly() -> None:
    hiren_id = _index_for("hiren")._point_id("p/x.md", "Body", 0)
    assert hiren_id == _legacy_point_id("p/x.md", "Body", 0)


def test_non_hiren_point_id_is_not_the_legacy_format() -> None:
    acme_id = _index_for("acme")._point_id("p/x.md", "Body", 0)
    assert acme_id != _legacy_point_id("p/x.md", "Body", 0)


# ------------------------------------------------------------------ (c)/(d) filter structure


def test_tenant_condition_for_non_hiren_is_exact_match() -> None:
    idx = _index_for("acme")
    cond = idx._tenant_condition()
    assert cond.key == "user_id"
    assert cond.match.value == "acme"


def test_tenant_condition_for_hiren_is_should_of_match_or_empty() -> None:
    idx = _index_for("hiren")
    cond = idx._tenant_condition()
    assert cond.should is not None and len(cond.should) == 2
    kinds = {("match" if getattr(c, "match", None) else "empty") for c in cond.should}
    assert kinds == {"match", "empty"}
    match_cond = next(c for c in cond.should if getattr(c, "match", None))
    assert match_cond.key == "user_id" and match_cond.match.value == "hiren"
    empty_cond = next(c for c in cond.should if getattr(c, "is_empty", None))
    assert empty_cond.is_empty.key == "user_id"


def test_search_sends_tenant_filter_for_non_hiren() -> None:
    idx = _index_for("acme")
    idx.upsert_file("p/x.md", _DOC)
    idx.search("shared content")
    filt = idx._ctl.client.last_search_filter
    assert filt is not None
    assert any(getattr(c, "key", None) == "user_id" and c.match.value == "acme" for c in filt.must)


def test_search_matches_missing_user_id_points_for_hiren() -> None:
    """Points indexed before multi-tenancy existed have no user_id key at all —
    hiren's search must still surface them (the should=[match, is_empty] contract)."""
    idx = _index_for("hiren")
    idx.ensure_collection()
    # Simulate a pre-existing production point with no user_id field whatsoever.
    legacy_vec = list(idx._embed(["some shared content here"]))[0]
    idx._ctl.client.points["test-col"]["legacy-point"] = (
        legacy_vec,
        {"path": "p/legacy.md", "title": "Legacy", "description": "d", "heading": "Body"},
    )
    results = idx.search("shared content here")
    assert results is not None
    assert any(r["path"] == "p/legacy.md" for r in results)


# ------------------------------------------------------------------ (e) delete / existing_points scoping


def test_existing_points_scoped_to_tenant() -> None:
    client = FakeQdrant()
    acme = _index_for("acme", client)
    bob = _index_for("bob", client)
    acme.upsert_file("p/shared.md", _DOC)
    bob.upsert_file("p/shared.md", _DOC)
    # Same repo-relative path, but each tenant's _existing_points must only see its own.
    acme_points = acme._existing_points("p/shared.md")
    bob_points = bob._existing_points("p/shared.md")
    assert acme_points and bob_points
    assert set(acme_points) != set(bob_points)  # different point ids (b)
    for _pid, (_vec, payload) in acme_points.items():
        assert payload["user_id"] == "acme"
    for _pid, (_vec, payload) in bob_points.items():
        assert payload["user_id"] == "bob"


def test_delete_file_never_touches_another_tenants_points_at_same_path() -> None:
    client = FakeQdrant()
    acme = _index_for("acme", client)
    bob = _index_for("bob", client)
    acme.upsert_file("p/shared.md", _DOC)
    bob.upsert_file("p/shared.md", _DOC)
    assert len(client.points["test-col"]) == 4  # intro + Body chunk, per tenant

    assert acme.delete_file("p/shared.md") is True
    remaining = client.points["test-col"]
    assert len(remaining) == 2  # only bob's two chunks survive
    assert all(payload["user_id"] == "bob" for _vec, payload in remaining.values())


def test_search_never_crosses_tenants() -> None:
    client = FakeQdrant()
    acme = _index_for("acme", client)
    bob = _index_for("bob", client)
    acme.upsert_file("p/a.md", "---\ntype: idea\ntitle: A\ndescription: d\n---\n\nunique acme content\n")
    bob.upsert_file("p/b.md", "---\ntype: idea\ntitle: B\ndescription: d\n---\n\nunique bob content\n")

    acme_results = acme.search("unique")
    bob_results = bob.search("unique")
    assert {r["path"] for r in acme_results} == {"p/a.md"}
    assert {r["path"] for r in bob_results} == {"p/b.md"}


def test_ensure_collection_creates_user_id_payload_index() -> None:
    idx = _index_for("acme")
    calls: list[str] = []
    real_create = idx._ctl.client.create_payload_index

    def _spy(collection_name, field_name, field_schema=None):
        calls.append(field_name)
        return real_create(collection_name, field_name, field_schema)

    idx._ctl.client.create_payload_index = _spy
    idx.ensure_collection()
    assert "user_id" in calls
