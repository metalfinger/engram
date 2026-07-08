"""Pure-Python text search over the brain checkout — v1 backend for kb_search.

Ships under the exact v1.1 contract (path/title/description/score/matched_heading)
so a later Qdrant swap changes nothing for clients. Fully synchronous; callers
wrap in anyio.to_thread.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .errors import KBError

_TOKEN_SPLIT = re.compile(r"[^\w-]+")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

_W_TITLE = 5
_W_DESCRIPTION = 4
_W_TAGS = 3
_W_HEADINGS = 3
_BODY_CAP = 3  # per-token cap on body hits (1 point each)
_PHRASE_BONUS = 4
_MAX_LIMIT = 25


def _read_meta(text: str) -> tuple[dict[str, Any], str]:
    """Split leading YAML frontmatter from body. Tolerant: absent/bad YAML -> ({}, body)."""
    text = text.lstrip("﻿")
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    body = text[m.end() :]
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}, body
    return (meta if isinstance(meta, dict) else {}), body


def _tags_text(meta: dict[str, Any]) -> str:
    tags = meta.get("tags")
    if isinstance(tags, (list, tuple)):
        return " ".join(str(t) for t in tags).lower()
    return str(tags).lower() if tags else ""


def _matched_heading(body: str, tokens: list[str]) -> str | None:
    """Text of the nearest markdown heading at/above the first body line hit by any token."""
    lines = body.splitlines()
    hit_line: int | None = None
    for i, line in enumerate(lines):
        low = line.lower()
        if any(t in low for t in tokens):
            hit_line = i
            break
    if hit_line is None:
        return None
    for i in range(hit_line, -1, -1):
        m = _HEADING.match(lines[i])
        if m:
            return m.group(1)
    return None


def search(
    root: Path,
    query: str,
    project: str | None = None,
    type_: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Rank every concept/log .md under root against query.

    Corpus excludes .git and all index.md files; log.md files are included.
    Returns [{path, title, description, score, matched_heading}] sorted by
    score desc (ties: newer frontmatter timestamp first), at most limit (1..25).
    """
    if not query or not query.strip():
        raise KBError("Empty search query — pass one or more keywords, e.g. kb_search('dns cutover').")
    tokens = [t for t in _TOKEN_SPLIT.split(query.lower()) if t]
    if not tokens:
        raise KBError("Query has no searchable tokens — use words or numbers, not punctuation only.")
    phrase = query.strip().lower()
    limit = max(1, min(_MAX_LIMIT, limit))
    denom = len(tokens) * 9 + 4

    scored: list[tuple[float, str, dict[str, Any]]] = []
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        if ".git" in rel.parts or path.name == "index.md":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, body = _read_meta(text)
        rel_posix = rel.as_posix()

        if project is not None:
            prefix = "metalfinger/" if project == "metalfinger" else f"projects/{project}/"
            if meta.get("project") != project and not rel_posix.startswith(prefix):
                continue
        if type_ is not None and meta.get("type") != type_:
            continue

        title = str(meta.get("title") or path.stem)
        description = str(meta.get("description") or "")
        lower_title = title.lower()
        lower_desc = description.lower()
        lower_body = body.lower()
        tags = _tags_text(meta)
        headings = " ".join(m.group(1) for line in body.splitlines() if (m := _HEADING.match(line))).lower()

        raw = 0
        body_hit = False
        for t in tokens:
            if t in lower_title:
                raw += _W_TITLE
            if t in lower_desc:
                raw += _W_DESCRIPTION
            if t in tags:
                raw += _W_TAGS
            if t in headings:
                raw += _W_HEADINGS
            n = lower_body.count(t)
            if n:
                raw += min(n, _BODY_CAP)
                body_hit = True
        if phrase in lower_title or phrase in lower_body:
            raw += _PHRASE_BONUS
        if raw == 0:
            continue

        score = round(min(1.0, raw / denom), 4)
        result = {
            "path": rel_posix,
            "title": title,
            "description": description,
            "score": score,
            "matched_heading": _matched_heading(body, tokens) if body_hit else None,
        }
        scored.append((score, str(meta.get("timestamp") or ""), result))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [result for _, _, result in scored[:limit]]


# ====================================================================== fusion
# Pure, offline, deterministic helpers shared by kbstore.kb_search to fuse the
# text and semantic engines (hybrid), expand one query into a few variants
# (multi-query), and pull in date-windowed concepts. Kept here so they are unit
# testable without a KBStore and so search.py owns all ranking math.

# Very small stopword list — enough to strip glue words for the keyword-only
# variant without gutting short queries. Deliberately conservative.
_STOPWORDS = frozenset(
    """a an the of for to in on at by and or not is are was were be been being do does did
    with from into about as it its this that these those we our i my you your they their he she
    why how what when where which who whom whose than then so such can could should would will
    over under between within why's whats hows""".split()
)

# Deterministic, offline synonym expansion — a tiny domain-flavored map. Each key
# token, when present, contributes its extra tokens to a single expansion variant.
# NO LLM, NO network: additions are appended, never removed, so recall only grows.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "oauth": ("auth", "authentication", "login"),
    "auth": ("authentication", "oauth"),
    "authentication": ("auth", "oauth", "login"),
    "login": ("signin", "auth"),
    "bearer": ("token",),
    "token": ("bearer", "credential"),
    "allowlist": ("whitelist", "allow"),
    "db": ("database",),
    "database": ("db", "store"),
    "config": ("configuration", "settings"),
    "repo": ("repository",),
    "docs": ("documentation", "doc"),
    "spec": ("specification", "design"),
    "vector": ("embedding", "semantic"),
    "embedding": ("vector", "embed"),
    "semantic": ("vector", "meaning"),
    "search": ("query", "retrieval", "find"),
    "widget": ("app", "ui"),
    "deploy": ("deployment", "release", "ship"),
    "cutover": ("migration", "switch"),
    "dns": ("domain", "record"),
    "test": ("tests", "testing"),
    "rename": ("move", "rename"),
    "message": ("messages", "msg"),
}

_WINDOW_FLOOR = 0.01  # floor score for a date-window hit that had no relevance signal
_ISO_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_LOG_DATE = re.compile(r"^\s*[*+\-]?\s*(\d{4}-\d{2}-\d{2})\b")


def _keyword_variant(query: str) -> str:
    """Query with stopwords removed. Empty string if nothing survives (all glue)."""
    kept = [t for t in _TOKEN_SPLIT.split(query.lower()) if t and t not in _STOPWORDS]
    return " ".join(kept)


def _synonym_variant(query: str) -> str:
    """Original query with a deterministic set of synonym tokens appended once.
    Empty string if no token has a mapping (so the caller can drop a no-op variant)."""
    tokens = [t for t in _TOKEN_SPLIT.split(query.lower()) if t]
    present = set(tokens)
    extra: list[str] = []
    seen = set(present)
    for t in tokens:
        for syn in _SYNONYMS.get(t, ()):  # deterministic: insertion order of the map
            if syn not in seen:
                extra.append(syn)
                seen.add(syn)
    if not extra:
        return ""
    return query.strip() + " " + " ".join(extra)


def query_variants(query: str) -> list[str]:
    """Expand a raw query into 1-3 deterministic variants for multi-query fusion:
    the raw query, a keyword-only (stopword-stripped) variant, and a synonym-expanded
    variant. Always returns the raw query first; drops empty/duplicate variants."""
    raw = query.strip()
    variants = [raw]
    for cand in (_keyword_variant(query), _synonym_variant(query)):
        if cand and cand.lower() != raw.lower() and cand not in variants:
            variants.append(cand)
    return variants


def union_max(rankings: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Union several result lists keeping the best-scoring row per path. Used to
    collapse a single engine's multi-query runs before fusion. Sorted best-first."""
    best: dict[str, dict[str, Any]] = {}
    for results in rankings:
        for r in results:
            path = r.get("path")
            if not path:
                continue
            if path not in best or r.get("score", 0.0) > best[path].get("score", 0.0):
                best[path] = r
    return sorted(best.values(), key=lambda r: r.get("score", 0.0), reverse=True)


def rrf_fuse(
    rankings: list[list[dict[str, Any]]], k: int = 60
) -> list[dict[str, Any]]:
    """Reciprocal-rank fusion of two (or more) ranked result lists.

    RRF over BM's ``max(a,b)+0.3*min(a,b)`` because it fuses on RANK not raw score:
    the text scorer emits 0..1 saturating scores and the vector engine emits cosine
    similarities on a different scale, so score-space fusion would need per-engine
    min-max normalization to be meaningful. Rank fusion sidesteps that entirely and
    is robust to either engine's absolute magnitudes.

    Score for a path = sum over lists of 1/(k + rank) (rank 0-based within each list).
    A path present in BOTH lists therefore outranks one present in only one at a
    similar position. The returned ``score`` is the RRF score; each row carries the
    richest metadata seen (the appearance with the highest original engine score)."""
    scores: dict[str, float] = {}
    meta: dict[str, dict[str, Any]] = {}
    for results in rankings:
        for rank, r in enumerate(results):
            path = r.get("path")
            if not path:
                continue
            scores[path] = scores.get(path, 0.0) + 1.0 / (k + rank + 1)
            if path not in meta or r.get("score", 0.0) > meta[path].get("score", 0.0):
                meta[path] = r
    fused: list[dict[str, Any]] = []
    for path, s in scores.items():
        row = dict(meta[path])
        row["score"] = round(s, 6)
        fused.append(row)
    fused.sort(key=lambda r: r["score"], reverse=True)
    return fused


def _parse_date(value: Any) -> date | None:
    """Best-effort ISO date out of a frontmatter timestamp / string. None if unparseable."""
    if not value:
        return None
    m = _ISO_DATE.search(str(value))
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def window_search(
    root: Path,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    type_: str | None = None,
) -> list[dict[str, Any]]:
    """Every concept whose frontmatter ``timestamp`` (or, for log.md, any entry date)
    falls in [since, until] — inclusive, either bound optional. Returns floor-scored
    result rows (same shape as ``search``) so kb_search can union them in regardless
    of relevance. Failure-soft: an unparseable bound disables the pass (returns [])
    rather than matching everything; unreadable/undated files are skipped."""
    lo = _parse_date(since) if since else None
    hi = _parse_date(until) if until else None
    if (since and lo is None) or (until and hi is None):
        return []  # a bound was given but could not be parsed — do not guess

    def _in_window(d: date) -> bool:
        return (lo is None or d >= lo) and (hi is None or d <= hi)

    results: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        if ".git" in rel.parts or path.name == "index.md":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, body = _read_meta(text)
        rel_posix = rel.as_posix()

        if project is not None:
            prefix = "metalfinger/" if project == "metalfinger" else f"projects/{project}/"
            if meta.get("project") != project and not rel_posix.startswith(prefix):
                continue
        if type_ is not None and meta.get("type") != type_:
            continue

        matched: date | None = None
        if path.name == "log.md":
            for line in body.splitlines():
                m = _LOG_DATE.match(line)
                if not m:
                    continue
                d = _parse_date(m.group(1))
                if d and _in_window(d) and (matched is None or d > matched):
                    matched = d
        else:
            d = _parse_date(meta.get("timestamp"))
            if d and _in_window(d):
                matched = d
        if matched is None:
            continue

        results.append(
            {
                "path": rel_posix,
                "title": str(meta.get("title") or path.stem),
                "description": str(meta.get("description") or ""),
                "score": _WINDOW_FLOOR,
                "matched_heading": None,
            }
        )
    return results
