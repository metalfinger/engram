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

import logging
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


@dataclass
class Chunk:
    heading: str
    text: str


@dataclass
class _FakeControls:
    """Test seam: unit tests set .client and .embedder to fakes so no network/model
    download happens. Production leaves both None and the lazy factories run."""

    client: Any = None
    embedder: Any = None
    dim: int | None = None
    calls: list[tuple[str, Any]] = field(default_factory=list)


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
        """(Re)index one concept file: delete its prior points, then upsert fresh ones.
        Failure-soft — returns False on any backend error."""
        if posixpath.basename(path) in _SKIP_NAMES:
            return True
        if not self.ensure_collection():
            return False
        try:
            chunks = self.chunk_file(path, text)
            self._delete_path(path)
            if not chunks:
                return True
            vectors = self._embed([c.text for c in chunks])
            qm = self._models()
            points = [
                qm.PointStruct(
                    id=self._point_id(path, c.heading, i),
                    vector=vectors[i],
                    payload=self._payload(path, text, c),
                )
                for i, c in enumerate(chunks)
            ]
            self._client().upsert(collection_name=self.collection, points=points)
            log.info("semantic: upserted %d chunks for %s", len(points), path)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("semantic: upsert_file(%s) failed: %s", path, exc)
            return False

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
            hits = self._client().search(
                collection_name=self.collection,
                query_vector=qvec,
                query_filter=query_filter,
                limit=max(1, limit) * 4,
                with_payload=True,
            )
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
