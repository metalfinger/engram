"""Pure-Python text search over the brain checkout — v1 backend for kb_search.

Ships under the exact v1.1 contract (path/title/description/score/matched_heading)
so a later Qdrant swap changes nothing for clients. Fully synchronous; callers
wrap in anyio.to_thread.
"""

from __future__ import annotations

import re
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
