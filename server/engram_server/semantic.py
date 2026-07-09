"""Semantic search over the brain bundle — Qdrant vector store + local ONNX embeddings.

Every public method is failure-soft: any Qdrant/network/model error is caught,
logged, and surfaced as ``None``/``False`` so a tool NEVER crashes because the
vector backend is down. ``kbstore.kb_search`` treats that as "fall back to the
text scorer". The embedder (fastembed) is imported and constructed lazily — the
first call downloads the ONNX model, which must be non-fatal.

Chunking: each concept file becomes an intro chunk (title + description + tags +
any body preamble) plus one chunk per ``##`` section body, hard-capped at
~1500 chars. Point ids are ``uuid5(path#heading#idx)`` so re-indexing a file
upserts deterministically; ``upsert_file`` also deletes the file's prior points
first, so shrinking a document never leaves orphan chunks behind.
"""

from __future__ import annotations

import hashlib
import logging
import math
import posixpath
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .frontmatter import normalize_meta, split

log = logging.getLogger("engram.semantic")

_CHUNK_CAP = 1500
# Fixed namespace so uuid5(path#heading#idx) is stable across processes/reindexes.
_NAMESPACE = uuid.UUID("6f9d1e6a-2b1e-5c8a-9f3d-b8c2a1d0f9e7")
_SKIP_NAMES = {"index.md"}
# Coordination ephemera (threads/, workspace/) are transient signalling, not
# knowledge — kept out of the vector corpus so the nightly full reindex never
# re-adds presence/handoff/thread files after the write-path already skipped them.
_EPHEMERAL_ROOTS = ("threads", "workspace")


@dataclass
class Chunk:
    heading: str
    text: str


@dataclass
class _FakeControls:
    """Test seam: unit tests set .client, .embedder, and .reranker to fakes so no
    network/model download happens. Production leaves them None and the lazy factories run."""

    client: Any = None
    embedder: Any = None
    reranker: Any = None
    dim: int | None = None
    calls: list[tuple[str, Any]] = field(default_factory=list)


def _sha(text: str) -> str:
    """Content fingerprint of one chunk's embeddable text — the incremental key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 when either is degenerate."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _centroid(vectors: list[list[float]]) -> list[float] | None:
    """Element-wise mean of equal-length vectors, or None when empty/ragged."""
    vectors = [v for v in vectors if v]
    if not vectors:
        return None
    dim = len(vectors[0])
    if any(len(v) != dim for v in vectors):
        return None
    return [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]


def _sigmoid(x: float) -> float:
    """Numerically-stable logistic squashing of a raw cross-encoder score into (0, 1)."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _project_of(path: str) -> str | None:
    parts = path.split("/")
    if parts[0] == "metalfinger":
        return "metalfinger"
    if parts[0] == "projects" and len(parts) >= 2:
        return parts[1]
    return None


def _tags_list(meta: dict[str, Any]) -> list[str]:
    tags = meta.get("tags")
    if isinstance(tags, (list, tuple)):
        return [str(t) for t in tags]
    if tags:
        return [str(tags)]
    return []


def _split_sections(body: str) -> list[Chunk]:
    """Preamble (before the first ``##``) + one chunk per ``##`` section, each hard-capped."""
    lines = body.splitlines()
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in lines:
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        else:
            sections[-1][1].append(line)
    out: list[Chunk] = []
    for heading, buf in sections:
        text = "\n".join(buf).strip()
        if not text:
            continue
        for piece in _cap(text):
            out.append(Chunk(heading=heading, text=piece))
    return out


def _cap(text: str, cap: int = _CHUNK_CAP) -> list[str]:
    """Split ``text`` into <=cap-char pieces on whitespace boundaries."""
    if len(text) <= cap:
        return [text]
    pieces: list[str] = []
    remaining = text
    while len(remaining) > cap:
        cut = remaining.rfind(" ", 0, cap)
        if cut <= 0:
            cut = cap
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    return [p for p in pieces if p]


class SemanticIndex:
    """Lazy Qdrant client + lazy fastembed embedder. Injectable for tests."""

    def __init__(self, settings: Settings, controls: _FakeControls | None = None) -> None:
        self.settings = settings
        self.collection = settings.qdrant_collection
        self._ctl = controls or _FakeControls()
        self._collection_ready = False

    # ------------------------------------------------------------------ lazy deps

    def _client(self) -> Any:
        if self._ctl.client is None:
            from qdrant_client import QdrantClient

            self._ctl.client = QdrantClient(
                url=self.settings.qdrant_url,
                api_key=self.settings.qdrant_api_key or None,
                timeout=30,
            )
        return self._ctl.client

    def _embedder(self) -> Any:
        if self._ctl.embedder is None:
            from fastembed import TextEmbedding

            log.info("semantic: loading embedding model %s (first use may download)", self.settings.embed_model)
            self._ctl.embedder = TextEmbedding(model_name=self.settings.embed_model)
        return self._ctl.embedder

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._embedder().embed(texts)
        return [list(map(float, v)) for v in vectors]

    def _reranker(self) -> Any:
        if self._ctl.reranker is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            log.info(
                "semantic: loading rerank model %s (first use may download)",
                self.settings.rerank_model,
            )
            self._ctl.reranker = TextCrossEncoder(model_name=self.settings.rerank_model)
        return self._ctl.reranker

    def _dimension(self) -> int:
        if self._ctl.dim is None:
            self._ctl.dim = len(self._embed(["dimension probe"])[0])
        return self._ctl.dim

    def _models(self):
        from qdrant_client import models as qm

        return qm

    # ------------------------------------------------------------------ collection

    def ensure_collection(self) -> bool:
        """Create the collection on first use (cosine). Idempotent; failure-soft."""
        if self._collection_ready:
            return True
        try:
            client = self._client()
            qm = self._models()
            if not client.collection_exists(self.collection):
                client.create_collection(
                    collection_name=self.collection,
                    vectors_config=qm.VectorParams(
                        size=self._dimension(), distance=qm.Distance.COSINE
                    ),
                )
            # Qdrant Cloud requires a payload index for every filtered field
            # (delete-by-path on upsert; project/type filters at search time) —
            # without these, filtered requests 400. Idempotent per field.
            for field in ("path", "project", "type"):
                try:
                    client.create_payload_index(
                        collection_name=self.collection,
                        field_name=field,
                        field_schema=qm.PayloadSchemaType.KEYWORD,
                    )
                except Exception:  # noqa: BLE001 — already-exists is fine
                    pass
            self._collection_ready = True
            return True
        except Exception as exc:  # noqa: BLE001 — backend must never crash a tool
            log.warning("semantic: ensure_collection failed: %s", exc)
            return False

    # ------------------------------------------------------------------ chunking

    def chunk_file(self, path: str, text: str) -> list[Chunk]:
        """Turn a concept file into embeddable chunks (intro + per-section)."""
        doc = split(text)
        meta = normalize_meta(doc.meta) if doc is not None else {}
        body = doc.body if doc is not None else text
        title = str(meta.get("title") or posixpath.splitext(posixpath.basename(path))[0])
        description = str(meta.get("description") or "")
        tags = _tags_list(meta)

        sections = _split_sections(body)
        # Fold the first preamble piece (heading == "", i.e. text before any ##) into
        # the intro chunk; keep every ## section (and any overflow pieces) as its own chunk.
        preamble = ""
        rest: list[Chunk] = []
        for c in sections:
            if c.heading == "" and not preamble:
                preamble = c.text
            else:
                rest.append(c)
        intro_parts = [p for p in (title, description, " ".join(tags), preamble) if p]
        chunks = [Chunk(heading=title, text="\n".join(intro_parts)[:_CHUNK_CAP])]
        chunks.extend(rest)
        return [c for c in chunks if c.text.strip()]

    def _payload(self, path: str, text: str, chunk: Chunk) -> dict[str, Any]:
        doc = split(text)
        meta = normalize_meta(doc.meta) if doc is not None else {}
        return {
            "path": path,
            "project": _project_of(path),
            "type": str(meta.get("type") or ""),
            "tags": _tags_list(meta),
            "title": str(meta.get("title") or posixpath.splitext(posixpath.basename(path))[0]),
            "description": str(meta.get("description") or ""),
            "heading": chunk.heading,
            "timestamp": str(meta.get("timestamp") or ""),
        }

    @staticmethod
    def _point_id(path: str, heading: str, idx: int) -> str:
        return str(uuid.uuid5(_NAMESPACE, f"{path}#{heading}#{idx}"))

    # ------------------------------------------------------------------ writes

    def upsert_file(self, path: str, text: str) -> bool:
        """(Re)index one concept file, embedding only chunks whose content changed.

        Incremental path (BM perf item): fetch the file's existing points, compare
        each desired chunk's sha256 to the stored ``chunk_sha`` payload, and only
        embed new/changed chunks; unchanged chunks reuse their stored vector (payload
        refreshed if frontmatter shifted); removed chunks are deleted. This makes the
        common re-write (a log append, one edited section) near-free and keeps a
        nightly full_reindex cheap.

        Failure-soft on two levels: any error in the incremental path falls back to a
        full delete-and-re-embed of the file (the original behavior, always correct);
        if that also fails (backend down) it returns False. Never raises."""
        if posixpath.basename(path) in _SKIP_NAMES:
            return True
        if not self.ensure_collection():
            return False
        try:
            return self._incremental_upsert(path, text)
        except Exception as exc:  # noqa: BLE001 — incremental hiccup: re-embed the whole file
            log.warning("semantic: incremental upsert(%s) failed, full re-embed: %s", path, exc)
            try:
                return self._full_upsert(path, text)
            except Exception as exc2:  # noqa: BLE001 — backend down: give up softly
                log.warning("semantic: upsert_file(%s) failed: %s", path, exc2)
                return False

    def _existing_points(self, path: str) -> dict[str, tuple[Any, dict[str, Any]]]:
        """{point_id: (vector, payload)} for every point already stored for ``path``."""
        qm = self._models()
        records, _next = self._client().scroll(
            collection_name=self.collection,
            scroll_filter=qm.Filter(
                must=[qm.FieldCondition(key="path", match=qm.MatchValue(value=path))]
            ),
            with_vectors=True,
            with_payload=True,
            limit=10_000,
        )
        return {str(rec.id): (rec.vector, dict(rec.payload or {})) for rec in records}

    def _incremental_upsert(self, path: str, text: str) -> bool:
        chunks = self.chunk_file(path, text)
        # desired[point_id] = (chunk, chunk_sha) — the target state for this file.
        desired: dict[str, tuple[Chunk, str]] = {}
        for i, c in enumerate(chunks):
            desired[self._point_id(path, c.heading, i)] = (c, _sha(c.text))
        existing = self._existing_points(path)

        remove = [pid for pid in existing if pid not in desired]
        to_embed: list[str] = []  # point ids whose vector must be (re)computed
        reuse: list[str] = []  # point ids whose vector is unchanged
        for pid, (_c, sha) in desired.items():
            prev = existing.get(pid)
            if prev is not None and prev[1].get("chunk_sha") == sha:
                reuse.append(pid)
            else:
                to_embed.append(pid)

        qm = self._models()
        points: list[Any] = []
        if to_embed:
            vectors = self._embed([desired[pid][0].text for pid in to_embed])
            for pid, vec in zip(to_embed, vectors):
                chunk, sha = desired[pid]
                payload = self._payload(path, text, chunk)
                payload["chunk_sha"] = sha
                points.append(qm.PointStruct(id=pid, vector=vec, payload=payload))
        for pid in reuse:
            vec, old_payload = existing[pid]
            chunk, sha = desired[pid]
            payload = self._payload(path, text, chunk)
            payload["chunk_sha"] = sha
            # Only re-upsert an unchanged-vector chunk if its payload actually moved
            # (frontmatter title/description/tags edit) — otherwise it is a true no-op.
            if payload != old_payload:
                points.append(qm.PointStruct(id=pid, vector=vec, payload=payload))

        if remove:
            self._client().delete(
                collection_name=self.collection,
                points_selector=qm.PointIdsList(points=remove),
            )
        if points:
            self._client().upsert(collection_name=self.collection, points=points)
        log.info(
            "semantic: incremental %s: %d embedded, %d reused, %d removed",
            path,
            len(to_embed),
            len(reuse),
            len(remove),
        )
        return True

    def _full_upsert(self, path: str, text: str) -> bool:
        """Delete every prior point for the file and re-embed all chunks. The always-
        correct fallback; also seeds ``chunk_sha`` so the next write can go incremental."""
        chunks = self.chunk_file(path, text)
        self._delete_path(path)
        if not chunks:
            return True
        vectors = self._embed([c.text for c in chunks])
        qm = self._models()
        points = []
        for i, c in enumerate(chunks):
            payload = self._payload(path, text, c)
            payload["chunk_sha"] = _sha(c.text)
            points.append(
                qm.PointStruct(id=self._point_id(path, c.heading, i), vector=vectors[i], payload=payload)
            )
        self._client().upsert(collection_name=self.collection, points=points)
        log.info("semantic: full re-embed %d chunks for %s", len(points), path)
        return True

    def delete_file(self, path: str) -> bool:
        """Drop every point for ``path``. Failure-soft."""
        if not self.ensure_collection():
            return False
        try:
            self._delete_path(path)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("semantic: delete_file(%s) failed: %s", path, exc)
            return False

    def _delete_path(self, path: str) -> None:
        qm = self._models()
        self._client().delete(
            collection_name=self.collection,
            points_selector=qm.Filter(
                must=[qm.FieldCondition(key="path", match=qm.MatchValue(value=path))]
            ),
        )

    # ------------------------------------------------------------------ search

    def search(
        self,
        query: str,
        project: str | None = None,
        type: str | None = None,  # noqa: A002 — tool contract field name
        limit: int = 8,
    ) -> list[dict[str, Any]] | None:
        """Vector search, deduped to best chunk per path. Returns None on ANY failure
        (empty query, backend down, model load error) so the caller falls back to text."""
        if not query or not query.strip():
            return None
        if not self.ensure_collection():
            return None
        try:
            qvec = self._embed([query])[0]
            qm = self._models()
            conditions = []
            if project:
                conditions.append(
                    qm.FieldCondition(key="project", match=qm.MatchValue(value=project))
                )
            if type:
                conditions.append(qm.FieldCondition(key="type", match=qm.MatchValue(value=type)))
            query_filter = qm.Filter(must=conditions) if conditions else None
            # qdrant-client >= 1.12 removed .search(); query_points is the API.
            hits = self._client().query_points(
                collection_name=self.collection,
                query=qvec,
                query_filter=query_filter,
                limit=max(1, limit) * 4,
                with_payload=True,
            ).points
            best: dict[str, dict[str, Any]] = {}
            for hit in hits:
                payload = dict(hit.payload or {})
                path = str(payload.get("path") or "")
                if not path:
                    continue
                score = float(getattr(hit, "score", 0.0) or 0.0)
                if path not in best or score > best[path]["score"]:
                    best[path] = {
                        "path": path,
                        "title": str(payload.get("title") or ""),
                        "description": str(payload.get("description") or ""),
                        "score": round(score, 4),
                        "matched_heading": payload.get("heading") or None,
                    }
            ranked = sorted(best.values(), key=lambda r: r["score"], reverse=True)
            return ranked[: max(1, limit)]
        except Exception as exc:  # noqa: BLE001
            log.warning("semantic: search(%r) failed: %s", query, exc)
            return None

    # ------------------------------------------------------------------ rebuild-guard

    def centroid_drift(self, result_text: str, source_paths: list[str]) -> float | None:
        """Cosine of a result's embedding centroid vs its sources' centroid.

        The result centroid is the mean of ``result_text``'s embedded chunks; the
        sources' centroid is the mean of every source path's ALREADY-INDEXED chunk
        vectors (fetched from the store, so no re-embedding of source bodies). A low
        similarity means the result diverged from what it claims to be built on — a
        cheap rebuild/hallucination guard for artifacts.

        Returns the similarity in [0, 1], or None when the backend is unavailable,
        there are no sources, or no source has any indexed vectors. Failure-soft."""
        if not result_text or not result_text.strip() or not source_paths:
            return None
        if not self.ensure_collection():
            return None
        try:
            pieces = [c.text for c in _split_sections(result_text) if c.text.strip()]
            if not pieces:
                pieces = _cap(result_text.strip())
            if not pieces:
                return None
            result_centroid = _centroid(self._embed(pieces))
            if result_centroid is None:
                return None
            source_vectors: list[list[float]] = []
            for path in source_paths:
                for _pid, (vec, _payload) in self._existing_points(str(path)).items():
                    if vec:
                        source_vectors.append(list(vec))
            source_centroid = _centroid(source_vectors)
            if source_centroid is None:
                return None
            return round(_cosine(result_centroid, source_centroid), 4)
        except Exception as exc:  # noqa: BLE001 — advisory only, never crash a write
            log.warning("semantic: centroid_drift failed: %s", exc)
            return None

    # ------------------------------------------------------------------ rerank

    def rerank(self, query: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Cross-encoder rerank of already-fused results. Blends the reranker score with
        the incoming fused score (0.7*fused_norm + 0.3*sigmoid(ce)) so an absolute
        magnitude survives for abstention, stamps ``reranked: True``, and re-sorts.

        Failure-soft: fewer than two results, or ANY reranker error, returns the input
        list unchanged (no ``reranked`` flag) so a downed reranker degrades to fusion."""
        if not results or len(results) < 2:
            return results
        try:
            documents = [
                (f"{r.get('title') or ''} {r.get('description') or ''}".strip() or str(r.get("path") or ""))
                for r in results
            ]
            ce_scores = list(self._reranker().rerank(query, documents))
            if len(ce_scores) != len(results):
                return results
            fmax = max((float(r.get("score") or 0.0) for r in results), default=0.0) or 1.0
            reranked: list[dict[str, Any]] = []
            for r, ce in zip(results, ce_scores):
                fused_norm = float(r.get("score") or 0.0) / fmax
                blended = 0.7 * fused_norm + 0.3 * _sigmoid(float(ce))
                new = dict(r)
                new["score"] = round(blended, 4)
                new["reranked"] = True
                reranked.append(new)
            reranked.sort(key=lambda r: r["score"], reverse=True)
            return reranked
        except Exception as exc:  # noqa: BLE001 — rerank is optional polish, never fatal
            log.warning("semantic: rerank(%r) failed, returning fused order: %s", query, exc)
            return results

    # ------------------------------------------------------------------ reindex

    def full_reindex(self, root: Path) -> dict[str, Any]:
        """Walk every concept .md under ``root`` and upsert it. Skips index.md and .git.
        Failure-soft: returns a summary; individual file failures are counted, not raised."""
        summary = {"indexed": 0, "failed": 0, "skipped": 0}
        if not self.ensure_collection():
            summary["error"] = "collection unavailable"
            return summary
        for f in sorted(root.rglob("*.md")):
            rel = f.relative_to(root).as_posix()
            if ".git" in f.parts or f.name in _SKIP_NAMES:
                summary["skipped"] += 1
                continue
            if rel.split("/", 1)[0] in _EPHEMERAL_ROOTS:
                summary["skipped"] += 1
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                summary["failed"] += 1
                continue
            if self.upsert_file(rel, text):
                summary["indexed"] += 1
            else:
                summary["failed"] += 1
        log.info("semantic: full_reindex %s", summary)
        return summary
