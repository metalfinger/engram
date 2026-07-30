"""OKF frontmatter parsing, validation, and serialization (pure — no git, no I/O beyond read_meta)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .errors import FrontmatterError

FENCE = "---"

TYPE_EXAMPLES = (
    "project, client, person, decision, spec, runbook, idea, meeting, video, snippet, reference, message"
)

_WIKILINK_RE = re.compile(r"\[\[.+?\]\]")

# Who may see a concept. ABSENT MEANS PRIVATE — visibility is never inferred, only
# declared, because publishing is one-way (public content can be cached/copied by
# anyone who saw it). 'contacts' = accepted contacts only; 'public' = any signed-in
# Engram user. A project's context.md sets the default its concepts inherit; a
# concept's own value always wins. See kbstore.effective_visibility.
VISIBILITY_VALUES = ("private", "contacts", "public")


@dataclass
class Doc:
    """A concept file split into YAML frontmatter (meta) and markdown body."""

    meta: dict
    body: str


def split(text: str) -> Doc | None:
    """Split text into Doc. None when text does not start with a --- fence;
    FrontmatterError when the YAML is invalid, not a mapping, or the closing fence is missing."""
    lines = text.split("\n")
    if lines[0].rstrip("\r") != FENCE:
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") != FENCE:
            continue
        raw = "\n".join(lines[1:i])
        try:
            meta = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise FrontmatterError(f"Frontmatter is not valid YAML: {exc}") from exc
        if meta is None:
            meta = {}
        if not isinstance(meta, dict):
            raise FrontmatterError(
                f"Frontmatter must be a YAML mapping (key: value pairs), got {type(meta).__name__}."
            )
        return Doc(meta=meta, body="\n".join(lines[i + 1 :]))
    raise FrontmatterError("Frontmatter opened with '---' but the closing '---' fence is missing.")


def normalize_meta(meta: dict) -> dict:
    """Convert YAML-parsed datetime/date objects back to canonical strings; recurse into containers."""
    return {key: _normalize_value(value) for key, value in meta.items()}


def _normalize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, dict):
        return {key: _normalize_value(v) for key, v in value.items()}
    if isinstance(value, list):
        return [_normalize_value(v) for v in value]
    return value


def serialize(doc: Doc) -> str:
    """Render Doc back to text: safe_dump'd meta between --- fences, then the body."""
    dumped = yaml.safe_dump(doc.meta, sort_keys=False, allow_unicode=True, width=1000)
    return f"{FENCE}\n{dumped}{FENCE}\n{doc.body}"


def _title_from_filename(rel_path: str) -> str:
    """Derive a title from the filename stem: kebab/snake-case -> Title Case. The H1 is NEVER consulted."""
    stem = PurePosixPath(rel_path.replace("\\", "/")).stem
    words = [w for w in re.split(r"[-_\s]+", stem) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words) or stem


def _timestamp_parseable(value: Any) -> bool:
    if isinstance(value, (datetime, date)):
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def validate_concept(
    text: str,
    *,
    rel_path: str,
    description_arg: str = "",
    now: datetime | None = None,
) -> tuple[str, dict, list[str]]:
    """Validate and normalize a concept file.

    Returns (normalized_text, meta, warnings). Raises FrontmatterError on missing
    frontmatter, missing/empty type, or missing description (with no fallback arg).
    If nothing changed semantically, the input text is returned verbatim.
    """
    warnings: list[str] = []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    changed = normalized != text

    doc = split(normalized)
    if doc is None:
        raise FrontmatterError(
            "Concept content must start with a YAML frontmatter fence, e.g.:\n"
            '---\ntype: ...\n---\n'
            "Prepend a frontmatter block (at minimum a 'type' field) and retry."
        )

    meta = normalize_meta(doc.meta)
    meta_changed = False

    ctype = meta.get("type")
    if not isinstance(ctype, str) or not ctype.strip():
        raise FrontmatterError(
            "Frontmatter requires a non-empty 'type'. The taxonomy is open — add types freely; "
            f"examples: {TYPE_EXAMPLES}."
        )

    if not meta.get("title"):
        meta["title"] = _title_from_filename(rel_path)
        meta_changed = True

    if not meta.get("description"):
        if description_arg:
            meta["description"] = description_arg
            meta_changed = True
        else:
            raise FrontmatterError(
                "Frontmatter has no 'description' and none was provided. Pass a one-line "
                "description argument (or add a 'description:' field) — it becomes the "
                "concept's index entry."
            )

    ts = meta.get("timestamp")
    if ts is None or (isinstance(ts, str) and not ts.strip()):
        moment = now if now is not None else datetime.now(timezone.utc)
        if moment.tzinfo is not None:
            moment = moment.astimezone(timezone.utc)
        meta["timestamp"] = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        meta_changed = True
    elif not _timestamp_parseable(ts):
        warnings.append(f"timestamp {ts!r} is not parseable ISO 8601; kept verbatim.")

    vis = meta.get("visibility")
    if vis is not None:
        normalized_vis = str(vis).strip().lower()
        if normalized_vis not in VISIBILITY_VALUES:
            raise FrontmatterError(
                f"visibility {vis!r} is not valid — use one of: {', '.join(VISIBILITY_VALUES)}. "
                "Omit the field entirely to keep the concept private (the default)."
            )
        if normalized_vis != vis:
            meta["visibility"] = normalized_vis
            meta_changed = True

    if ctype == "message":
        if not meta.get("status"):
            meta["status"] = "unread"
            meta_changed = True
        if not meta.get("to"):
            meta["to"] = "any"
            meta_changed = True

    if _WIKILINK_RE.search(doc.body):
        warnings.append(
            "Body contains [[wikilinks]]; OKF uses standard markdown links "
            "([Title](relative-path.md)) — wikilinks won't resolve for other consumers."
        )

    stripped = doc.body.rstrip("\n")
    body = f"{stripped}\n" if stripped else ""
    if body != doc.body:
        changed = True

    if not changed and not meta_changed:
        return text, meta, warnings
    return serialize(Doc(meta=meta, body=body)), meta, warnings


def read_meta(path: Path) -> dict:
    """Fast frontmatter-only parse: read up to the closing fence only; {} on any problem."""
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.readline().rstrip("\r\n") != FENCE:
                return {}
            block: list[str] = []
            for line in fh:
                if line.rstrip("\r\n") == FENCE:
                    meta = yaml.safe_load("".join(block))
                    return normalize_meta(meta) if isinstance(meta, dict) else {}
                block.append(line)
        return {}
    except Exception:
        return {}
