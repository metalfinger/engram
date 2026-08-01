"""KBStore — async knowledge-base facade over the brain git checkout.

One instance owns ONE asyncio.Lock that serializes every mutation; reads are
lock-free against the checkout with a TTL-throttled pull that swallows git
failures (serve stale when GitHub is down). All git/file I/O runs in worker
threads via anyio.to_thread. Every tool-visible path is repo-relative POSIX.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import posixpath
import re
import secrets
import time
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import anyio
from anyio import to_thread

from .config import Settings
from .errors import GitError, KBError
from .frontmatter import (
    VISIBILITY_VALUES,
    Doc,
    normalize_meta,
    read_meta,
    serialize,
    split,
    validate_concept,
)
from .gitops import GitRepo
from .importers import parse_export
from .indexer import ensure_indexed
from .search import query_variants as _query_variants
from .search import rrf_fuse as _rrf_fuse
from .search import search as _run_search
from .search import union_max as _union_max
from .search import window_search as _window_search
from .semantic import SemanticIndex
from .version import server_manifest

log = logging.getLogger("engram.kbstore")

_PROJECT_ID_RE = re.compile(r"^[a-z0-9-]+$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_MD_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
# Same shape, named group — used by the project-move re-linker.
_MOVE_LINK_RE = re.compile(r"\]\((?P<target>[^)\s]+)\)")
# A relative .md link — the kind depth=1 navigation can follow (not http/mailto).
_REL_MD_LINK = re.compile(r"\]\((?!https?://|mailto:)[^)\s#]*\.md(?:#[^)\s]*)?\)")
_BULLET_RE = re.compile(
    r"^\s*[*+-]\s+\[(?P<title>[^\]]*)\]\((?P<target>[^)\s]+)\)\s*(?:[-–—]\s*(?P<desc>.*))?\s*$"
)
_LOG_HEADING_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*(?:[—–-]+\s*)?(.*)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_VALID_TO = ("any", "claude-code", "mobile", "web")
_ARCHIVE_INDEX = "# Archive\n\nRead messages land here.\n"
_LINK_CAP = 50
_SLUG_MAX = 60
_THREAD_ID_RE = re.compile(r"^[a-z0-9-]+$")
_ALNUM_RE = re.compile(r"[a-z0-9]")
_PRESENCE_STATUSES = ("working", "idle", "blocked", "available", "done")
# A claim older than this (minutes) is stale — ignored by the roster/warnings, like presence.
_CLAIM_TTL_MIN = 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _slug(value: str, fallback: str = "") -> str:
    """kebab-ish slug: lowercase, each non-alphanumeric run -> one hyphen, trimmed and
    capped. Returns ``fallback`` when nothing slug-able survives."""
    s = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")[:_SLUG_MAX].strip("-")
    return s or fallback


def _has_alnum(value: str) -> bool:
    """True when ``value`` contains at least one [a-z0-9] — the minimum for a usable id.
    Rejects empty / all-hyphen / all-punctuation ids that would slug to nothing."""
    return bool(_ALNUM_RE.search((value or "").lower()))


def _sha256(text: str) -> str:
    """Content hash of a concept's on-disk text — the token kb_read hands back and kb_write
    checks as base_hash for optimistic-concurrency (lost-update) safety."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_ts(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp (with or without a trailing 'Z') to an aware UTC
    datetime; None when absent or unparseable. Used for presence TTL/age math."""
    s = (value or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _title_case(slug: str) -> str:
    """kebab-case id -> Title Case display name ('mcp-apps' -> 'Mcp Apps')."""
    return " ".join(w[:1].upper() + w[1:] for w in slug.split("-") if w) or slug


def _read_text_retry(path: Path, attempts: int = 3, delay: float = 0.05) -> str:
    """Read that tolerates a concurrent writer's momentary lock/replace (the known
    Windows lock-free-read race): brief backoff retries on OSError, then re-raise.
    UnicodeDecodeError propagates unchanged — that's a content problem, not a race."""
    last: OSError | None = None
    for i in range(attempts):
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:  # includes PermissionError from in-flight replace
            last = exc
            time.sleep(delay * (i + 1))
    assert last is not None
    raise last


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _index_entries(index_path: Path) -> dict[str, tuple[str, str]]:
    """Parse an index.md's bullet lines into {link-target-key: (title, description)}.

    A dir bullet ``](x/index.md)`` or ``](x/)`` is keyed by both the raw target
    and the bare dirname ``x``; a file bullet is also keyed by its basename.
    """
    entries: dict[str, tuple[str, str]] = {}
    if not index_path.is_file():
        return entries
    try:
        text = _read_text_retry(index_path)
    except (OSError, UnicodeDecodeError):
        return entries
    for line in text.splitlines():
        m = _BULLET_RE.match(line)
        if not m:
            continue
        target = m.group("target").split("#", 1)[0]
        keys = {target}
        if target.endswith("/index.md"):
            keys.add(target[: -len("/index.md")])
        elif target.endswith("/"):
            keys.add(target.rstrip("/"))
        else:
            keys.add(posixpath.basename(target))
        value = (m.group("title").strip(), (m.group("desc") or "").strip())
        for key in keys:
            if key:
                entries[key] = value
    return entries


def _first_log_date(log_path: Path) -> str | None:
    """Date of the first '## ' heading in a log.md, or None."""
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("## "):
                    m = _LOG_HEADING_RE.match(line[3:].strip())
                    return m.group(1) if m else None
    except OSError:
        return None
    return None


def _parse_log_entries(text: str) -> list[dict[str, Any]]:
    """Split a log.md into [{date, title, body}] blocks, newest first (file order)."""
    entries: list[dict[str, Any]] = []
    body: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            m = _LOG_HEADING_RE.match(heading)
            date_s, title = (m.group(1), m.group(2).strip()) if m else (None, heading)
            body = []
            entries.append({"date": date_s, "title": title or heading, "body": body})
        elif body is not None:
            body.append(line)
    for entry in entries:
        entry["body"] = "\n".join(entry["body"]).strip()
    return entries


def _entry_to_bullet(entry: str) -> str:
    """Render an append_log entry as one OKF bullet ``* **<title>** — <body>``, with
    any continuation lines indented two spaces. A ``## `` entry is normalized rather
    than kept verbatim: the title comes from a ``## <date> — <title>`` heading, else
    from the first line — so the log stays a flat bullet list under bare date headings."""
    text = entry.replace("\r\n", "\n").replace("\r", "\n").strip()
    lines = text.split("\n")
    first = lines[0].strip()
    if first.startswith("## "):
        heading = first[3:].strip()
        m = _LOG_HEADING_RE.match(heading)
        title = (m.group(2).strip() or heading) if m else heading
    else:
        title = first
    body = "\n".join(lines[1:]).strip()
    bullet = f"* **{title}**"
    if body:
        body_lines = body.split("\n")
        bullet += f" — {body_lines[0].strip()}"
        for cont in body_lines[1:]:
            stripped = cont.strip()
            bullet += f"\n  {stripped}" if stripped else "\n"
    return bullet


def _insert_log_bullet(text: str, bullet: str, today: str) -> str:
    """Place ``bullet`` under today's date heading, newest bullet first. If the top
    ``## `` heading is already today's bare ISO date the bullet slots directly beneath
    it; otherwise a bare ``## <today>`` heading is created right after the H1, above the
    older days. History below is never edited."""
    lines = text.splitlines()
    bullet_lines = bullet.split("\n")
    for i, line in enumerate(lines):
        if not line.startswith("## "):
            continue
        m = _LOG_HEADING_RE.match(line[3:].strip())
        if m and m.group(1) == today:
            new_lines = lines[: i + 1] + bullet_lines + lines[i + 1 :]
        else:
            head = lines[:i]
            while head and not head[-1].strip():
                head.pop()
            new_lines = head + ["", f"## {today}"] + bullet_lines + [""] + lines[i:]
        return "\n".join(new_lines).rstrip("\n") + "\n"
    stripped = "\n".join(lines).rstrip("\n")
    block = "\n".join([f"## {today}", *bullet_lines])
    return f"{stripped}\n\n{block}\n" if stripped.strip() else f"{block}\n"


def _first_sentence(body: str, limit: int = 140) -> str:
    """First sentence of the first non-empty line, trimmed to ``limit`` chars."""
    stripped = body.strip()
    if not stripped:
        return ""
    first_line = stripped.splitlines()[0].strip()
    m = re.search(r"[.!?](?=\s|$)", first_line)
    sentence = first_line[: m.end()] if m else first_line
    return sentence[:limit].strip()


def _is_expired(expires: Any, today: date) -> bool:
    if not expires:
        return False
    try:
        return date.fromisoformat(str(expires)) < today
    except ValueError:
        return False


# ------------------------------------------------------------------ secret scan (share boundary)

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAI API key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("AWS access key id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("bearer token", re.compile(r"Bearer [A-Za-z0-9._-]{20,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "hardcoded credential",
        re.compile(r"(?i)\b(?:api[_-]?key|secret|password)\b\s*[:=]\s*['\"][^'\"]{8,}"),
    ),
)


# Subtrees that are NEVER visible beyond their owner, even inside a project marked
# public: session mail and coordination surfaces carry whatever a session happened to
# say, so they can't inherit a project-level publish decision.
_NEVER_PUBLIC_SEGMENTS = frozenset({"messages", "threads", "workspace", "inbox", ".git"})


def _as_tags(value: Any) -> list[str]:
    """Frontmatter tags as a clean list (YAML gives a list, or a comma string)."""
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    return []


def _never_public(rel: str) -> bool:
    """True when a repo-relative path lives in a never-publishable subtree."""
    return any(seg in _NEVER_PUBLIC_SEGMENTS for seg in rel.split("/") if seg)


def _scan_secrets(body: str) -> list[tuple[str, int]]:
    """Scan text for likely secrets. Returns [(kind, 1-based line number)] — never the
    matched value, so a refusal can name the KIND and location without leaking the secret."""
    hits: list[tuple[str, int]] = []
    for lineno, line in enumerate(body.splitlines(), start=1):
        for kind, rx in _SECRET_PATTERNS:
            if rx.search(line):
                hits.append((kind, lineno))
    return hits


def _secret_refusal(what: str, findings: list[tuple[str, int]], override: str) -> str:
    """Teaching refusal for a secret found in git-permanent text (thread post / handoff).
    Names the KINDS and line numbers, never the value, and points at the override flag."""
    kinds = sorted({kind for kind, _ in findings})
    lines = sorted({ln for _, ln in findings})
    return (
        f"Refusing to write this {what}: it contains what look like secrets "
        f"({', '.join(kinds)}) on line(s) {', '.join(map(str, lines))}. Threads and handoffs "
        "are private but COMMITTED TO GIT HISTORY permanently — a key here stays recoverable "
        f"forever, even after deletion. Remove the secret(s), or pass {override} to override "
        "deliberately (e.g. a placeholder)."
    )


# ------------------------------------------------------------------ surgical body edits (kb_edit)

_EDIT_OPERATIONS = ("append", "prepend", "find_replace", "replace_section", "insert_after", "insert_before")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def _replace_nth(text: str, find: str, repl: str, n: int) -> str:
    """Replace only the n-th (1-based) occurrence of ``find`` in ``text``."""
    start = 0
    for _ in range(n - 1):
        start = text.index(find, start) + len(find)
    pos = text.index(find, start)
    return text[:pos] + repl + text[pos + len(find) :]


def _replace_section(body: str, section: str, replacement: str) -> str:
    """Replace the block under the first heading matching ``section`` (heading kept; the
    lines beneath it up to the next same-or-higher heading are replaced)."""
    want = section.lstrip("#").strip()
    lines = body.split("\n")
    start: int | None = None
    level = 0
    for i, ln in enumerate(lines):
        m = _HEADING_RE.match(ln)
        if m and m.group(2).strip() == want:
            start, level = i, len(m.group(1))
            break
    if start is None:
        raise KBError(
            f"No section heading matching {section!r} in this concept — read it (kb_read) to see "
            "the exact headings, or use append/insert instead."
        )
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = _HEADING_RE.match(lines[j])
        if m and len(m.group(1)) <= level:
            end = j
            break
    block = [lines[start]]
    if replacement:
        block.append(replacement)
    tail = lines[end:]
    if tail and tail[0].strip():
        block.append("")
    return "\n".join(lines[:start] + block + tail)


def _apply_body_edit(
    operation: str,
    body: str,
    content: str,
    find: str | None,
    section: str | None,
    occurrence: int | str,
    fm_block: str,
) -> str:
    """Pure body transform for kb_edit. Operates ONLY on ``body``; ``fm_block`` is passed
    solely to tell a genuine zero-match apart from an anchor that lives in the frontmatter."""
    chunk = (content or "").replace("\r\n", "\n").replace("\r", "\n")
    piece = chunk.strip("\n")
    if operation == "append":
        if not piece:
            return body
        base = body.rstrip("\n")
        return f"{base}\n\n{piece}\n" if base else f"{piece}\n"
    if operation == "prepend":
        if not piece:
            return body
        rest = body.lstrip("\n")
        return f"{piece}\n\n{rest}" if rest else f"{piece}\n"
    if operation == "find_replace":
        if not find:
            raise KBError("find_replace needs `find` — the exact body text to replace.")
        count = body.count(find)
        if count == 0:
            if find in fm_block:
                raise KBError(
                    f"`find` text {find!r} appears only in the frontmatter, which kb_edit never "
                    "touches — use kb_write to change frontmatter."
                )
            raise KBError(
                f"`find` text {find!r} was not found in the body — read the concept (kb_read) and "
                "copy the anchor exactly; the match is literal, not fuzzy."
            )
        if str(occurrence) == "all":
            return body.replace(find, chunk)
        try:
            n = int(occurrence)
        except (TypeError, ValueError):
            raise KBError("occurrence must be a positive integer (1 = first) or 'all'.") from None
        if n < 1:
            raise KBError("occurrence must be a positive integer (1 = first) or 'all'.")
        if count < n:
            raise KBError(
                f"asked to replace occurrence #{n} of {find!r} but it appears only {count} time(s)."
            )
        return _replace_nth(body, find, chunk, n)
    if operation == "replace_section":
        if not section:
            raise KBError(
                "replace_section needs `section` — the heading whose block to replace, e.g. '## Notes'."
            )
        return _replace_section(body, section, piece)
    if operation in ("insert_after", "insert_before"):
        if not find:
            raise KBError(f"{operation} needs `find` — the existing body line to anchor to.")
        lines = body.split("\n")
        anchor = next((i for i, ln in enumerate(lines) if find in ln), None)
        if anchor is None:
            if find in fm_block:
                raise KBError(
                    f"anchor {find!r} appears only in the frontmatter, which kb_edit never touches "
                    "— use kb_write to change frontmatter."
                )
            raise KBError(
                f"anchor {find!r} was not found in the body — read the concept (kb_read) and copy "
                "an existing line exactly."
            )
        insert_at = anchor + 1 if operation == "insert_after" else anchor
        return "\n".join(lines[:insert_at] + piece.split("\n") + lines[insert_at:])
    raise KBError(f"Unknown edit operation {operation!r} — one of: {', '.join(_EDIT_OPERATIONS)}.")


# ------------------------------------------------------------------ single-concept move (kb_move)


def _link_resolves_to(target: str, from_dir: str) -> str | None:
    """Repo-relative path a markdown link target resolves to from ``from_dir``, or None
    when it is not a relative .md link (external, mailto, or non-markdown)."""
    base = target.split("#", 1)[0]
    if not base.endswith(".md") or "://" in base or base.startswith("mailto:"):
        return None
    if base.startswith("/"):
        return posixpath.normpath(base.lstrip("/"))
    return posixpath.normpath(posixpath.join(from_dir, base))


def _rebase_body_links(text: str, old_rel: str, new_rel: str) -> str:
    """Re-express a moved file's OWN relative links so they still resolve after the move."""
    old_dir = posixpath.dirname(old_rel)
    new_dir = posixpath.dirname(new_rel)
    if old_dir == new_dir:
        return text

    def repl(m: re.Match[str]) -> str:
        target = m.group(1)
        base, sep, frag = target.partition("#")
        if not base.endswith(".md") or "://" in base or base.startswith(("mailto:", "/")):
            return m.group(0)
        resolved = posixpath.normpath(posixpath.join(old_dir, base))
        new_target = posixpath.relpath(resolved, new_dir or ".").replace("\\", "/")
        return f"]({new_target}{sep}{frag})"

    return _MD_LINK_RE.sub(repl, text)


def _retarget_body_links(text: str, from_dir: str, old_rel: str, new_rel: str) -> tuple[str, int]:
    """Rewrite every relative link in ``text`` that resolved to ``old_rel`` so it now
    resolves to ``new_rel``. Returns (text, number of links rewritten)."""
    count = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal count
        target = m.group(1)
        base, sep, frag = target.partition("#")
        if _link_resolves_to(target, from_dir) != old_rel:
            return m.group(0)
        count += 1
        if base.startswith("/"):
            return f"](/{new_rel}{sep}{frag})"
        new_target = posixpath.relpath(new_rel, from_dir or ".").replace("\\", "/")
        return f"]({new_target}{sep}{frag})"

    return _MD_LINK_RE.sub(repl, text), count


def _rewrite_frontmatter_refs(text: str, old_rel: str, new_rel: str) -> str:
    """Rewrite frontmatter sources/supersedes/superseded_by entries that point at ``old_rel``."""
    try:
        doc = split(text)
    except KBError:
        return text
    if doc is None:
        return text
    meta = normalize_meta(doc.meta)
    changed = False
    for key in ("sources", "supersedes", "superseded_by"):
        if key not in meta:
            continue
        val = meta[key]
        if isinstance(val, list):
            new_val = [new_rel if str(x) == old_rel else x for x in val]
            if new_val != val:
                meta[key] = new_val
                changed = True
        elif str(val) == old_rel:
            meta[key] = new_rel
            changed = True
    if not changed:
        return text
    return serialize(Doc(meta=meta, body=doc.body))


def _remove_index_bullet(root: Path, rel: str) -> str | None:
    """Drop ``rel``'s bullet from its parent index.md. Returns the index rel path if changed."""
    parent = posixpath.dirname(rel)
    base = posixpath.basename(rel)
    index_rel = f"{parent}/index.md" if parent else "index.md"
    index_path = root / index_rel
    if not index_path.is_file():
        return None
    text = _read_text_retry(index_path)
    kept: list[str] = []
    removed = False
    for line in text.splitlines():
        m = _BULLET_RE.match(line)
        if m:
            target = m.group("target").split("#", 1)[0]
            if posixpath.basename(target) == base and (target == base or target.endswith(f"/{base}")):
                removed = True
                continue
        kept.append(line)
    if not removed:
        return None
    _write_text(index_path, "\n".join(kept).rstrip("\n") + "\n")
    return index_rel


class KBStore:
    """All kb_* tool behavior over one server-owned brain checkout."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root: Path = settings.brain_path
        self.repo = GitRepo(
            path=settings.brain_path,
            remote=settings.brain_remote,
            branch=settings.brain_branch,
            ssh_key=settings.deploy_key_path,
            author=(settings.git_author_name, settings.git_author_email),
            timeout=settings.git_timeout,
        )
        self._lock = asyncio.Lock()
        self._last_pull = 0.0  # time.monotonic() of the last successful-or-attempted pull
        # Semantic backend is optional: only live when enabled AND a Qdrant URL is set.
        # Everything else degrades to the text scorer. Never constructed for text-only.
        self.semantic: SemanticIndex | None = (
            SemanticIndex(settings)
            if settings.semantic_search and settings.qdrant_url
            else None
        )
        if self.semantic is not None:
            # v3: every indexed point carries effective visibility, so the vector
            # store can answer "team-visible?" without touching the filesystem.
            self.semantic.visibility_resolver = self._visibility_sync
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        """Clone the brain if missing and enforce local git config. Idempotent."""
        await to_thread.run_sync(self.repo.ensure_clone)

    # ------------------------------------------------------------------ observability + indexing

    def _log_mutation(self, tool: str, paths: list[str], sha: str, pushed: bool) -> None:
        """One structured line per kb_* mutation: tool, sha, pushed flag, touched paths."""
        log.info(
            "kb_mutation tool=%s sha=%s pushed=%s paths=%s",
            tool,
            (sha or "")[:12],
            pushed,
            ",".join(paths) if paths else "-",
        )

    def _schedule_index(
        self,
        upserts: list[str] | None = None,
        deletes: list[str] | None = None,
        reindex: bool = False,
    ) -> None:
        """Fire-and-forget semantic (re)index of touched files after a commit. No-op when
        the semantic backend is off or no event loop is running (sync test contexts).
        Errors are logged, never raised — indexing must not affect the tool's result."""
        if self.semantic is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _job() -> None:
            index = self.semantic
            assert index is not None
            try:
                if reindex:
                    await to_thread.run_sync(lambda: index.full_reindex(self.root))
                    return
                for p in deletes or []:
                    await to_thread.run_sync(lambda p=p: index.delete_file(p))
                for p in upserts or []:
                    if posixpath.basename(p) == "index.md":
                        continue
                    abs_p = self.root / p
                    if not abs_p.is_file():
                        continue
                    text = _read_text_retry(abs_p)
                    await to_thread.run_sync(lambda p=p, t=text: index.upsert_file(p, t))
            except Exception:  # noqa: BLE001 — background indexing is best-effort
                log.warning("semantic: background index job failed", exc_info=True)

        task = loop.create_task(_job())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ------------------------------------------------------------------ plumbing

    def _resolve(self, path: str) -> tuple[Path, str]:
        """Traversal-safe resolution: returns (absolute path, normalized POSIX rel)."""
        if not isinstance(path, str) or not path.strip():
            raise KBError(
                "Empty path — pass a repo-relative POSIX path like 'projects/alt/context.md'."
            )
        rel = path.strip().replace("\\", "/")
        if rel.startswith("/") or _DRIVE_RE.match(rel):
            raise KBError(
                f"Absolute paths are not allowed: {path!r}. Use repo-relative POSIX paths "
                "like 'projects/alt/context.md'."
            )
        for part in rel.split("/"):
            if part in ("", ".", ".."):
                raise KBError(
                    f"Invalid path {path!r}: empty, '.' or '..' segments are not allowed — "
                    "paths never escape the knowledge base."
                )
            if part.lower() == ".git":
                raise KBError(f"Invalid path {path!r}: '.git' is off-limits.")
        root_resolved = self.root.resolve()
        abs_path = (self.root / rel).resolve()
        if not abs_path.is_relative_to(root_resolved):
            raise KBError(f"Path {path!r} escapes the knowledge base root.")
        # Re-check the RESOLVED parts: on Windows, resolve() expands 8.3 short
        # names (e.g. 'GIT~1' -> '.git'), so the pre-resolution segment check
        # above misses them. Reject any '.git' component after canonicalization.
        if any(part.lower() == ".git" for part in abs_path.relative_to(root_resolved).parts):
            raise KBError(f"Invalid path {path!r}: '.git' is off-limits.")
        return abs_path, rel

    def _project_rel(self, project: str) -> str:
        """Resolve a project ID to its path, wherever it sits.

        Projects live either at ``projects/<id>`` or inside ONE folder level,
        ``projects/<folder>/<id>`` — folders are real directories so the bundle is
        browsable in git/Finder. The ID stays flat and unique across the brain, so
        kb_load('alt'), `.engram-project` pins and the office never have to know
        which folder a project currently lives in."""
        pid = (project or "").strip()
        if pid == "metalfinger":
            rel = "metalfinger"
            if (self.root / rel).is_dir():
                return rel
            raise KBError(f"Unknown project {project!r}. Call kb_projects to list projects.")
        if not _PROJECT_ID_RE.fullmatch(pid):
            raise KBError(
                f"Invalid project id {project!r} — ids are lowercase letters/digits/hyphens. "
                "Call kb_projects to list valid projects."
            )
        found = self._find_project_dir(pid)
        if found is None:
            raise KBError(
                f"Unknown project {project!r}. Call kb_projects to list available projects."
            )
        return found

    @staticmethod
    def _is_folder_dir(path: Path) -> bool:
        """A directory under projects/ is a FOLDER when it groups projects rather than
        being one: it has no context.md of its own but contains a directory that does.
        context.md is the OKF project anchor, so it's the honest marker."""
        if (path / "context.md").is_file():
            return False
        try:
            return any(
                c.is_dir() and not c.name.startswith(".") and (c / "context.md").is_file()
                for c in path.iterdir()
            )
        except OSError:
            return False

    def _find_project_dir(self, pid: str) -> str | None:
        """'projects/<pid>' or 'projects/<folder>/<pid>' — whichever exists."""
        pdir = self.root / "projects"
        direct = pdir / pid
        if direct.is_dir() and not self._is_folder_dir(direct):
            return f"projects/{pid}"
        if pdir.is_dir():
            for child in sorted(pdir.iterdir()):
                if child.is_dir() and not child.name.startswith(".") and (child / pid).is_dir():
                    return f"projects/{child.name}/{pid}"
        return None

    def _project_folder(self, pid: str) -> str:
        """The folder a project sits in ('' when it's at the top level)."""
        rel = self._find_project_dir(pid) or ""
        parts = rel.split("/")
        return parts[1] if len(parts) == 3 else ""

    async def kb_rename_project(self, old_id: str, new_id: str) -> dict[str, Any]:
        """Rename projects/<old_id> to projects/<new_id>, rewriting links bundle-wide.

        Returns {old, new, links_rewritten, sha, pushed}."""
        old = (old_id or "").strip()
        new = (new_id or "").strip()
        if old == "metalfinger" or new == "metalfinger":
            raise KBError("metalfinger is a fixed top-level tree and cannot be renamed.")
        if not _PROJECT_ID_RE.fullmatch(old) or not _PROJECT_ID_RE.fullmatch(new):
            raise KBError("Project ids are lowercase letters/digits/hyphens, e.g. 'mcp-explorations'.")
        if old == new:
            raise KBError("old_id and new_id are identical — nothing to rename.")
        old_dir = self.root / "projects" / old
        new_dir = self.root / "projects" / new
        state: dict[str, Any] = {"links": 0}

        def _mutate() -> list[str]:
            if not old_dir.is_dir():
                raise KBError(f"Unknown project {old!r}. Call kb_projects to list projects.")
            if new_dir.exists():
                raise KBError(f"projects/{new} already exists — pick a different new_id.")
            old_dir.rename(new_dir)
            # The bullet in projects/index.md should read the project's real name after a
            # rename, not the old title: prefer the moved context.md's frontmatter title,
            # else Title-Case the new id.
            new_title = str(read_meta(new_dir / "context.md").get("title") or "") or _title_case(new)
            touched: set[str] = {f"projects/{old}", f"projects/{new}"}
            # Rewrite link targets everywhere: 'projects/<old>/...' anywhere in the bundle,
            # '<old>/...' from projects/index.md, '../<old>/...' from sibling projects;
            # plus the denormalized 'project:' frontmatter field inside the moved tree.
            deep = re.compile(rf"(\]\([^)]*?)projects/{re.escape(old)}/")
            sibling = re.compile(rf"(\]\(\.\./){re.escape(old)}/")
            index_bullet = re.compile(
                rf"(?P<pre>[*+-]\s+)\[(?P<title>[^\]]*)\](?P<open>\(){re.escape(old)}/"
            )
            for f in self.root.rglob("*.md"):
                if ".git" in f.parts:
                    continue
                text = _read_text_retry(f)
                orig = text
                text, n1 = deep.subn(rf"\g<1>projects/{new}/", text)
                n2 = n3 = 0
                if f.parent == self.root / "projects" and f.name == "index.md":
                    # Rewrite the bullet TEXT (project title) and target together for the
                    # renamed project; leave every other project's bullet untouched.
                    def _rewrite_bullet(m: re.Match[str]) -> str:
                        return f"{m.group('pre')}[{new_title}]{m.group('open')}{new}/"

                    text, n2 = index_bullet.subn(_rewrite_bullet, text)
                elif f.is_relative_to(new_dir.parent) and not f.is_relative_to(new_dir):
                    text, n3 = sibling.subn(rf"\g<1>{new}/", text)
                if f.is_relative_to(new_dir):
                    text = re.sub(
                        rf"^project: {re.escape(old)}$", f"project: {new}", text, flags=re.M
                    )
                if text != orig:
                    state["links"] += n1 + n2 + n3
                    _write_text(f, text)
                    touched.add(f.relative_to(self.root).as_posix())
            return sorted(touched)

        sha, pushed = await self._locked_commit(_mutate, f"kb: rename project {old} -> {new}")
        self._log_mutation("kb_rename_project", [f"projects/{old}", f"projects/{new}"], sha, pushed)
        # A rename moves many concepts at once — reindex the whole bundle rather than
        # tracking each moved path; the walk skips index.md and is failure-soft.
        self._schedule_index(reindex=True)
        return {"old": old, "new": new, "links_rewritten": state["links"], "sha": sha, "pushed": pushed}

    async def _refresh(self) -> None:
        """Read-path throttled pull. Skips inside the TTL or while a write holds the lock;
        swallows GitError so reads serve stale content when the remote is unreachable."""
        if self._last_pull and (time.monotonic() - self._last_pull) < self.settings.pull_ttl:
            return
        if self._lock.locked():
            return
        async with self._lock:
            try:
                await to_thread.run_sync(self.repo.pull_rebase)
            except GitError:
                pass
            self._last_pull = time.monotonic()

    async def _locked_commit(self, mutate: Callable[[], list[str]], message: str) -> tuple[str, bool]:
        """Serialized write: pull --rebase (GitError propagates, nothing written) ->
        clean-checkout guard -> mutate -> commit+push. mutate returns the rel paths to
        stage; an empty list means nothing changed (no commit, HEAD returned)."""
        async with self._lock:

            def _work() -> tuple[str, bool]:
                self.repo.pull_rebase()
                dirty = self.repo.is_dirty()
                if dirty:
                    raise KBError(
                        "The server's brain checkout has uncommitted changes and writes are "
                        f"blocked: {', '.join(dirty)}. The checkout is server-owned and must "
                        "stay clean — investigate before writing."
                    )
                paths = mutate()
                if not paths:
                    return self.repo.head_sha(), True
                return self.repo.commit_and_push(paths, message)

            sha, pushed = await to_thread.run_sync(_work)
            self._last_pull = time.monotonic()
            return sha, pushed

    # ------------------------------------------------------------------ reads

    async def kb_projects(self) -> list[dict[str, Any]]:
        """List all projects: [{id, title, description, status, last_session, unread_messages}]."""
        await self._refresh()
        return await to_thread.run_sync(self._projects_sync)

    def _projects_sync(self) -> list[dict[str, Any]]:
        proj_entries = _index_entries(self.root / "projects" / "index.md")
        root_entries = _index_entries(self.root / "index.md")
        out: list[dict[str, Any]] = []
        for pid in self._project_ids():
            rel = "metalfinger" if pid == "metalfinger" else (self._find_project_dir(pid) or f"projects/{pid}")
            pdir = self.root / rel
            entries = root_entries if pid == "metalfinger" else proj_entries
            title, description = entries.get(pid, ("", ""))
            ctx_meta = read_meta(pdir / "context.md")
            out.append(
                {
                    "id": pid,
                    "title": title or str(ctx_meta.get("title") or pid),
                    "description": description or str(ctx_meta.get("description") or ""),
                    "status": str(ctx_meta.get("status") or "active"),
                    # Grouping labels from context.md frontmatter — the brain already
                    # speaks tags, so organizing projects needs no new grammar and no
                    # nesting (a project can belong to several groups at once).
                    # The real directory this project lives in ('' = top level).
                    "folder": (rel.split("/")[1] if rel.count("/") == 2 else ""),
                    "tags": _as_tags(ctx_meta.get("tags")),
                    # The project's DEFAULT reach (its concepts inherit unless they
                    # override). Surfaced everywhere so "what's exposed?" is answerable
                    # at a glance — silent publication is the failure mode that matters.
                    "visibility": (
                        str(ctx_meta.get("visibility") or "").strip().lower()
                        if str(ctx_meta.get("visibility") or "").strip().lower() in VISIBILITY_VALUES
                        else "private"
                    ),
                    "last_session": _first_log_date(pdir / "log.md"),
                    "unread_messages": self._count_unread(pdir / "messages"),
                }
            )
        return out

    def _project_ids(self) -> list[str]:
        """Every project id, whether it sits at the top level or inside a folder."""
        pdir = self.root / "projects"
        ids: list[str] = []
        if pdir.is_dir():
            for d in sorted(pdir.iterdir()):
                if not d.is_dir() or d.name.startswith("."):
                    continue
                if self._is_folder_dir(d):
                    ids.extend(
                        sorted(c.name for c in d.iterdir()
                               if c.is_dir() and not c.name.startswith("."))
                    )
                else:
                    ids.append(d.name)
            ids = sorted(set(ids))
        # The 'metalfinger' personal top-level tree is the operator's; only surface it
        # as a pseudo-project when it actually exists. A fresh tenant brain has no such
        # dir, so this stops a phantom empty 'metalfinger' project appearing for them.
        if (self.root / "metalfinger").is_dir():
            ids = ids + ["metalfinger"]
        return ids

    def _count_unread(self, messages_dir: Path) -> int:
        if not messages_dir.is_dir():
            return 0
        return sum(
            1
            for f in messages_dir.glob("*.md")
            if f.name != "index.md" and str(read_meta(f).get("status") or "") == "unread"
        )

    async def kb_load(self, project: str, lite: bool = False) -> dict[str, Any]:
        """Load a project's working context: {project, context_md, index_tree, recent_log,
        unread_messages, active_concepts}. No concept bodies — fetch those with kb_read.
        lite=True returns a token-thrifty resume view (context_md, last log entry, unread
        COUNT + titles only, active_concepts, server) — no index_tree, no message bodies."""
        await self._refresh()
        return await to_thread.run_sync(lambda: self._load_sync(project, lite))

    def _load_sync(self, project: str, lite: bool = False) -> dict[str, Any]:
        proot_rel = self._project_rel(project)
        proot = self.root / proot_rel
        pid = proot_rel.split("/")[-1]

        ctx_path = proot / "context.md"
        context_md = _read_text_retry(ctx_path) if ctx_path.is_file() else None

        log_path = proot / "log.md"
        log_entries = _parse_log_entries(_read_text_retry(log_path)) if log_path.is_file() else []

        # Live capability manifest so a stale chat can detect it's behind (see version.py).
        # Read fresh every load — the one channel that reaches already-open sessions whose
        # tool list is frozen. Pass the brain's SKILL.md so the manifest carries a live
        # skill fingerprint (Claude Code compares it against its local copy and self-heals).
        manifest = server_manifest(self.root / "skills" / "engram" / "SKILL.md")

        if lite:
            # Resume view for a project you already know: skip the (large) index_tree and
            # every message body. Unread messages collapse to a count + title-only stubs so
            # the session still knows something is waiting without re-buying the bodies.
            unread = self._unread_messages(proot_rel)
            unread_titles = [
                {
                    "path": m["path"],
                    "title": m["title"],
                    "to": m["to"],
                    "priority": m["priority"],
                    "expired": m["expired"],
                }
                for m in unread
            ]
            return {
                "project": pid,
                "lite": True,
                "context_md": context_md,
                "recent_log": log_entries[:1],
                "unread_count": len(unread),
                "unread_titles": unread_titles,
                "active_concepts": self._active_concepts(proot, proot_rel),
                "server": manifest,
            }

        if proot_rel == "metalfinger":
            parent_entries = _index_entries(self.root / "index.md")
        else:
            parent_entries = _index_entries(self.root / "projects" / "index.md")
        title, description = parent_entries.get(pid, (None, None))
        index_tree = self._tree_node(proot, proot_rel, title, description)

        return {
            "project": pid,
            "context_md": context_md,
            "index_tree": index_tree,
            "recent_log": log_entries[:3],
            "unread_messages": self._unread_messages(proot_rel),
            "active_concepts": self._active_concepts(proot, proot_rel),
            "server": manifest,
        }

    def _tree_node(
        self, dir_abs: Path, dir_rel: str, title: str | None, description: str | None
    ) -> dict[str, Any]:
        """Filesystem-walked navigation node, decorated from the dir's own index.md bullets.
        Excludes messages/ subtrees, every index.md/log.md, and dot entries."""
        entries = _index_entries(dir_abs / "index.md")
        dirs: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        for child in sorted(dir_abs.iterdir(), key=lambda p: p.name):
            if child.name.startswith("."):
                continue
            child_rel = f"{dir_rel}/{child.name}"
            deco = entries.get(child.name, (None, None))
            if child.is_dir():
                if child.name == "messages":
                    continue
                dirs.append(self._tree_node(child, child_rel, deco[0], deco[1]))
            else:
                if child.name in ("index.md", "log.md"):
                    continue
                files.append(
                    {"path": child_rel, "name": child.name, "title": deco[0], "description": deco[1]}
                )
        return {
            "path": dir_rel,
            "name": posixpath.basename(dir_rel),
            "title": title,
            "description": description,
            "dirs": dirs,
            "files": files,
        }

    def _unread_messages(self, proot_rel: str) -> list[dict[str, Any]]:
        """All unread messages, full bodies, with expired computed against today UTC."""
        mdir = self.root / proot_rel / "messages"
        if not mdir.is_dir():
            return []
        today = _utcnow().date()
        out: list[dict[str, Any]] = []
        for f in sorted(mdir.glob("*.md")):
            if f.name == "index.md":
                continue
            try:
                doc = split(_read_text_retry(f))
            except (OSError, UnicodeDecodeError, KBError):
                continue
            if doc is None:
                continue
            meta = normalize_meta(doc.meta)
            if str(meta.get("status") or "") != "unread":
                continue
            expires = meta.get("expires")
            out.append(
                {
                    "path": f"{proot_rel}/messages/{f.name}",
                    "title": str(meta.get("title") or f.stem),
                    "description": str(meta.get("description") or ""),
                    "to": str(meta.get("to") or "any"),
                    "priority": str(meta.get("priority") or "normal"),
                    "expires": expires,
                    "expired": _is_expired(expires, today),
                    "timestamp": meta.get("timestamp"),
                    "body": doc.body.strip(),
                }
            )
        return out

    def _active_concepts(self, proot: Path, proot_rel: str) -> list[dict[str, Any]]:
        """Frontmatter-only records for status: active concepts (context.md excluded)."""
        out: list[dict[str, Any]] = []
        for f in sorted(proot.rglob("*.md")):
            if f.name in ("index.md", "log.md", "context.md"):
                continue
            rel = f.relative_to(self.root).as_posix()
            parts = rel.split("/")
            if "messages" in parts[:-1] or any(p.startswith(".") for p in parts):
                continue
            meta = read_meta(f)
            if str(meta.get("status") or "") != "active":
                continue
            out.append({**meta, "path": rel})
        return out

    async def kb_read(self, path: str, depth: int = 0) -> dict[str, Any]:
        """Read one file: {path, content, meta, hash}. depth=1 adds links — the frontmatter of
        every relative .md link target (or {missing: true}), capped at 50. `hash` is the
        sha256 of the returned content: pass it back as kb_write's base_hash so a conflicting
        concurrent edit is rejected instead of silently lost."""
        if depth not in (0, 1):
            raise KBError("depth must be 0 or 1.")
        await self._refresh()
        return await to_thread.run_sync(lambda: self._read_sync(path, depth))

    def _read_sync(self, path: str, depth: int) -> dict[str, Any]:
        abs_path, rel = self._resolve(path)
        if abs_path.is_dir():
            raise KBError(
                f"'{rel}' is a directory — read '{rel}/index.md', or call kb_load for a "
                "navigable tree."
            )
        if not abs_path.is_file():
            raise KBError(
                f"No such file: '{rel}'. Discover paths via kb_load's index_tree or kb_search."
            )
        try:
            content = _read_text_retry(abs_path)
        except UnicodeDecodeError as exc:
            raise KBError(f"'{rel}' is not a UTF-8 text file — kb_read serves text only.") from exc
        result: dict[str, Any] = {
            "path": rel,
            "content": content,
            "meta": read_meta(abs_path),
            "hash": _sha256(content),
        }
        if depth == 1:
            links = self._neighbor_links(rel, content)
            self._add_supersede_links(rel, result["meta"], links)
            result["links"] = links
            result["backlinks"] = self._backlinks(rel)
        return result

    def _add_supersede_links(
        self, rel: str, meta: dict[str, Any], links: list[dict[str, Any]]
    ) -> None:
        """Append the frontmatter supersession edges (supersedes + superseded_by) to a
        depth=1 link set, marked via='supersedes', so the chain is walkable both ways even
        when there is no body link between the two concepts."""
        refs: list[str] = list(self._supersedes_list(meta))
        back = meta.get("superseded_by")
        if back:
            refs.extend(str(b) for b in (back if isinstance(back, list) else [back]))
        seen = {link["path"] for link in links}
        for ref in refs:
            try:
                _abs, tref = self._resolve(ref)
            except KBError:
                continue
            if tref == rel or tref in seen:
                continue
            seen.add(tref)
            neighbor = self.root / tref
            if neighbor.is_file():
                links.append(
                    {"path": tref, "missing": False, "meta": read_meta(neighbor), "via": "supersedes"}
                )
            else:
                links.append({"path": tref, "missing": True, "via": "supersedes"})
            if len(links) >= _LINK_CAP:
                break

    def _backlinks(self, rel: str) -> list[dict[str, Any]]:
        """Concepts whose bodies link TO rel, PLUS artifacts that list rel as a
        provenance source (via: "sources") — the graph walks both edge kinds."""
        base_of = posixpath.dirname
        out: list[dict[str, Any]] = []
        for f in sorted(self.root.rglob("*.md")):
            if ".git" in f.parts or f.name == "index.md":
                continue
            src = f.relative_to(self.root).as_posix()
            if src == rel:
                continue
            meta = read_meta(f)
            srcs = meta.get("sources")
            if isinstance(srcs, list) and rel in [str(s) for s in srcs]:
                out.append({"path": src, "meta": meta, "via": "sources"})
                if len(out) >= _LINK_CAP:
                    break
                continue
            try:
                text = _read_text_retry(f)
            except (OSError, UnicodeDecodeError):
                continue
            for m in _MD_LINK_RE.finditer(text):
                target = m.group(1).split("#", 1)[0]
                if not target.endswith(".md") or "://" in target:
                    continue
                if target.startswith("/"):
                    resolved = posixpath.normpath(target.lstrip("/"))
                else:
                    resolved = posixpath.normpath(posixpath.join(base_of(src), target))
                if resolved == rel:
                    out.append({"path": src, "meta": meta, "via": "link"})
                    break
            if len(out) >= _LINK_CAP:
                break
        return out

    def _neighbor_links(self, rel: str, text: str) -> list[dict[str, Any]]:
        base = posixpath.dirname(rel)
        seen: set[str] = set()
        links: list[dict[str, Any]] = []
        for m in _MD_LINK_RE.finditer(text):
            target = m.group(1).split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            if not target.endswith(".md"):
                continue
            if target.startswith("/"):
                resolved = posixpath.normpath(target.lstrip("/"))
            else:
                resolved = posixpath.normpath(posixpath.join(base, target))
            if resolved.startswith("..") or resolved == rel or resolved in seen:
                continue
            seen.add(resolved)
            neighbor = self.root / resolved
            if neighbor.is_file():
                links.append({"path": resolved, "missing": False, "meta": read_meta(neighbor)})
            else:
                links.append({"path": resolved, "missing": True})
            if len(links) >= _LINK_CAP:
                break
        return links

    async def kb_search(
        self,
        query: str,
        project: str | None = None,
        type: str | None = None,  # noqa: A002 — tool contract field name
        limit: int = 8,
        expand: bool = True,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        """Rank concept/log files against query, fusing the semantic and text engines.

        Multi-query (expand=True): the query is expanded into a few deterministic,
        offline variants (raw, keyword-only, synonym-expanded) and each engine runs on
        all of them, unioned to the best score per path. Hybrid: when BOTH engines have
        hits they are fused by reciprocal-rank fusion (engine='hybrid'); when only one
        is available its results serve alone (engine='semantic'|'text') — the original
        all-or-nothing fallback is preserved for a downed engine. Time window: when
        since/until (ISO dates) are set, every concept whose frontmatter timestamp (or
        log entry date) falls in the window is unioned in and flagged window=True, so
        'what did we decide about X back in June' surfaces those regardless of score.

        Returns [{path, title, description, score, matched_heading, engine, window?}].
        Failure-soft throughout: any engine or the window pass can fail without crashing.
        """
        if not query or not query.strip():
            raise KBError(
                "Empty search query — pass one or more keywords, e.g. kb_search('dns cutover')."
            )
        await self._refresh()
        limit = max(1, min(25, limit))
        # Candidate depth per engine — deeper than the final limit so fusion has room.
        cand = min(25, max(limit * 3, 10))
        variants = _query_variants(query) if expand else [query.strip()]

        def _text_multi() -> list[dict[str, Any]]:
            # The raw variant's KBError (empty/punctuation-only query) is the contract
            # error and must propagate; derived variants that tokenize to nothing are skipped.
            lists = [_run_search(self.root, variants[0], project=project, type_=type, limit=cand)]
            for v in variants[1:]:
                try:
                    lists.append(_run_search(self.root, v, project=project, type_=type, limit=cand))
                except KBError:
                    continue
            return _union_max(lists)

        text_ranked = await to_thread.run_sync(_text_multi)

        semantic_ranked: list[dict[str, Any]] | None = None
        if self.semantic is not None:
            index = self.semantic

            def _sem_multi() -> list[dict[str, Any]] | None:
                lists: list[list[dict[str, Any]]] = []
                any_ok = False  # at least one variant reached the backend (not None)
                for v in variants:
                    r = index.search(v, project=project, type=type, limit=cand)
                    if r is None:
                        continue
                    any_ok = True
                    lists.append(r)
                return _union_max(lists) if any_ok else None

            semantic_ranked = await to_thread.run_sync(_sem_multi)

        # None => semantic backend down for every variant; [] => reachable but no hits.
        # Only fuse when semantic actually returned hits AND text has hits; otherwise
        # fall back to whichever single engine has something (text preferred when tie-empty).
        if semantic_ranked:
            if text_ranked:
                fused = _rrf_fuse([semantic_ranked, text_ranked])
                engine = "hybrid"
            else:
                fused = semantic_ranked
                engine = "semantic"
        else:
            fused = text_ranked
            engine = "text"
        for r in fused:
            r["engine"] = engine

        if since or until:
            window_results = await to_thread.run_sync(
                lambda: _window_search(self.root, since=since, until=until, project=project, type_=type)
            )
            by_path = {r["path"]: r for r in fused}
            for wr in window_results:
                hit = by_path.get(wr["path"])
                if hit is not None:
                    hit["window"] = True  # already relevant — keep its score, just flag it
                else:
                    wr["engine"] = engine
                    wr["window"] = True
                    fused.append(wr)
                    by_path[wr["path"]] = wr
            fused.sort(key=lambda r: r["score"], reverse=True)

        # Optional cross-encoder rerank of the fused candidates (opt-in). Reranks the
        # whole candidate pool before truncating to limit; fully failure-soft — a
        # reranker error returns the un-reranked fused order unchanged.
        if self.settings.rerank_enabled and self.semantic is not None and len(fused) > 1:
            index = self.semantic
            reranked = await to_thread.run_sync(lambda: index.rerank(query, fused))
            if reranked:
                fused = reranked

        return fused[:limit]

    # ------------------------------------------------------------------ writes

    async def kb_write(
        self,
        path: str,
        content: str,
        message: str,
        description: str = "",
        base_hash: str = "",
        session: str = "",
    ) -> dict[str, Any]:
        """Create or update one concept file; on create the parent index chain is updated.
        Returns {path, created, no_change, sha, pushed, warnings, indexes_updated}.

        base_hash: for safe concurrent edits, pass kb_read's returned `hash` — the write is
        rejected (nothing overwritten) if the file changed on disk since you read it. session:
        this session's id; when set, a foreign active kb_claim on the path adds a heads-up
        warning."""
        abs_path, rel = self._resolve(path)
        name = posixpath.basename(rel)
        if not rel.endswith(".md"):
            raise KBError("kb_write writes markdown concept files — the path must end in '.md'.")
        if name == "index.md":
            raise KBError(
                "index.md files are maintained automatically — write the concept file and its "
                "parent index updates itself."
            )
        if name == "log.md":
            raise KBError(
                "log.md is append-only and server-maintained — use kb_append_log to add a "
                "session entry."
            )
        if "messages" in rel.split("/")[:-1]:
            raise KBError(
                "Files under messages/ are managed by the messaging tools — use "
                "kb_leave_message to leave a message and kb_mark_read to archive one."
            )
        if not message or not message.strip():
            raise KBError("Pass a short commit message describing the change.")

        state: dict[str, Any] = {"no_change": False, "created": False, "indexes": [], "warnings": []}

        def _mutate() -> list[str]:
            normalized, meta, warnings = validate_concept(
                content, rel_path=rel, description_arg=description
            )
            state["type"] = str(meta.get("type") or "")  # for the publish-reflex nudge
            # Artifact provenance: stamp built_from = current HEAD when absent, so the
            # saved document records exactly which bundle state it was built against.
            # Also carry a live share token forward across updates the writer didn't
            # know about — re-saving an artifact must never silently kill its public link.
            is_artifact = str(meta.get("type") or "") == "artifact"
            inherited_share = ""
            if is_artifact and not meta.get("share") and abs_path.exists():
                try:
                    inherited_share = str(read_meta(abs_path).get("share") or "")
                except Exception:  # noqa: BLE001 — unreadable old file: nothing to inherit
                    inherited_share = ""
            if is_artifact and (not meta.get("built_from") or inherited_share):
                doc = split(normalized)
                assert doc is not None  # validate_concept guaranteed a frontmatter fence
                meta = normalize_meta(doc.meta)
                if not meta.get("built_from"):
                    meta["built_from"] = self.repo.head_sha()
                if inherited_share:
                    meta["share"] = inherited_share
                normalized = serialize(Doc(meta=meta, body=doc.body))
            # An artifact's provenance lives in its `sources:` list, not body links —
            # a non-empty sources list counts as "links to something", so don't nag.
            # A `supersedes:` frontmatter edge is walkable too (depth=1 surfaces it).
            sources = meta.get("sources")
            has_sources = isinstance(sources, list) and len(sources) > 0
            has_supersedes = bool(self._supersedes_list(meta))
            if (
                abs_path.name != "context.md"
                and not _REL_MD_LINK.search(normalized)
                and not (is_artifact and has_sources)
                and not has_supersedes
            ):
                warnings.append(
                    "This concept links to nothing — future sessions cannot navigate to "
                    "related knowledge from it (kb_read depth=1 will be empty). Add "
                    "relative markdown links to the concepts it relates to."
                )
            # Structured supersession edge: validate targets, reject self/cycles, stamp
            # each superseded target's frontmatter in THIS commit, and strip a stray
            # superseded_by from the new concept. May re-serialize `normalized`/`meta`.
            today_iso = _utcnow().strftime("%Y-%m-%d")
            normalized, meta, superseded = self._apply_supersedes(rel, meta, normalized, today_iso)
            state["warnings"] = warnings
            state["meta"] = meta
            state["superseded"] = superseded
            # Stash the artifact body + sources for a post-commit rebuild-guard check
            # (embedding runs off the git lock, failure-soft, never blocks the write).
            if is_artifact and has_sources:
                drift_doc = split(normalized)
                if drift_doc is not None:
                    state["drift"] = (drift_doc.body, [str(s) for s in sources])
            created = not abs_path.exists()
            state["created"] = created
            current_text = None if created else _read_text_retry(abs_path)
            # Optimistic-concurrency backstop: if the caller passed the hash they read, refuse
            # to overwrite blind when the on-disk content no longer matches it (another session
            # edited it in between). Empty base_hash keeps today's last-writer-wins behavior.
            if base_hash:
                disk_hash = _sha256(current_text if current_text is not None else "")
                if disk_hash != base_hash:
                    raise KBError(
                        "This concept changed since you read it (another session may have "
                        "edited it). Re-read it (kb_read) and merge your change into the current "
                        "content, then retry — nothing was overwritten."
                    )
            main_unchanged = not created and current_text == normalized
            if main_unchanged and not superseded:
                state["no_change"] = True
                return []
            paths: list[str] = []
            if not main_unchanged:
                _write_text(abs_path, normalized)
                paths.append(rel)
                if created:
                    state["indexes"] = ensure_indexed(
                        self.root, rel, str(meta.get("title") or ""), str(meta.get("description") or "")
                    )
                    paths.extend(state["indexes"])
            paths.extend(superseded)
            return paths

        sha, pushed = await self._locked_commit(_mutate, f"kb: {message.strip()}")
        self._log_mutation("kb_write", [rel], sha, pushed)
        if not state["no_change"]:
            self._schedule_index(upserts=[rel])
        # Fresh knowledge only: warn when a NEW concept semantically duplicates an
        # existing one, so the brain consolidates instead of fragmenting. Runs after
        # the commit (the write itself is never blocked); failure-soft.
        if state["created"] and not state["no_change"]:
            dupe = await to_thread.run_sync(
                lambda: self._near_duplicate(rel, state.get("meta") or {})
            )
            if dupe:
                state["warnings"].append(dupe)
        # Artifact rebuild-guard: warn (never block) when a saved artifact's body has
        # drifted from the sources it claims to be built on. Runs after the commit; soft.
        if not state["no_change"] and state.get("drift"):
            body, srcs = state["drift"]
            drift_warning = await to_thread.run_sync(
                lambda: self._artifact_drift_warning(body, srcs)
            )
            if drift_warning:
                state["warnings"].append(drift_warning)
        # Cooperative collision signal: a live foreign claim on this path means another
        # session flagged it as theirs. Never blocks — just a heads-up on the result.
        if not state["no_change"]:
            claim_warn = await to_thread.run_sync(lambda: self._claim_collision_warning(rel, session))
            if claim_warn:
                state["warnings"].append(claim_warn)
        # The publish reflex, server-side (v3): a settled DECISION that lands private
        # on a team server is invisible to the one search that makes the team layer
        # worth anything. Nudge — never auto-publish; the human decides.
        if (
            not state["no_change"]
            and self.settings.multiuser
            and str(state.get("type") or "").lower() in ("decision", "spec", "runbook")
            and not _never_public(rel)
        ):
            vis = await to_thread.run_sync(lambda: self._visibility_sync(rel))
            if vis == "private":
                state["warnings"].append(
                    f"This {state.get('type')} is PRIVATE — teammates' searches can't find "
                    "it. If it's shareable, offer the user: kb_publish('" + rel + "', 'public')."
                )
        return {
            "path": rel,
            "created": state["created"] and not state["no_change"],
            "no_change": state["no_change"],
            "sha": sha,
            "pushed": pushed,
            "warnings": state["warnings"],
            "indexes_updated": state["indexes"],
            "superseded": state.get("superseded", []),
        }

    _DUPE_EXEMPT_TYPES = ("artifact", "report", "inbox", "message")

    def _near_duplicate(self, rel: str, meta: dict[str, Any]) -> str | None:
        """A teaching warning when a new concept closely matches an existing one
        (semantic engine only; None when unavailable, exempt-typed, or no match)."""
        if self.semantic is None:
            return None
        if str(meta.get("type") or "") in self._DUPE_EXEMPT_TYPES:
            return None
        query = f"{meta.get('title') or ''}. {meta.get('description') or ''}".strip(". ")
        if not query:
            return None
        try:
            hits = self.semantic.search(query, limit=3) or []
        except Exception:  # noqa: BLE001 — advisory only, never break a write
            return None
        for hit in hits:
            path = str(hit.get("path") or "")
            score = float(hit.get("score") or 0.0)
            if path and path != rel and score >= self.settings.dupe_threshold:
                return (
                    f"Possibly a duplicate of existing concept '{path}' "
                    f"(similarity {score:.2f}) — consider updating that concept "
                    "instead of keeping both, or link them explicitly."
                )
        return None

    def _artifact_drift_warning(self, body: str, sources: list[str]) -> str | None:
        """Advisory rebuild-guard warning when an artifact's body diverged from its
        sources (semantic engine only; None when unavailable or above threshold)."""
        if self.semantic is None:
            return None
        try:
            similarity = self.semantic.centroid_drift(body, sources)
        except Exception:  # noqa: BLE001 — advisory only, never break a write
            return None
        if similarity is None:
            return None
        if similarity < self.settings.artifact_drift_threshold:
            return (
                f"this artifact diverged from its sources (similarity {similarity:.2f}) — "
                "it may contain content not grounded in them; review."
            )
        return None

    # ------------------------------------------------------------------ supersession

    def _supersedes_list(self, meta: dict[str, Any]) -> list[str]:
        """Frontmatter `supersedes` as a clean list of raw path strings (scalar or list)."""
        raw = meta.get("supersedes")
        if not raw:
            return []
        values = raw if isinstance(raw, list) else [raw]
        return [s for s in (str(v).strip() for v in values) if s]

    def _apply_supersedes(
        self, rel: str, meta: dict[str, Any], normalized: str, today: str
    ) -> tuple[str, dict[str, Any], list[str]]:
        """Resolve a concept's `supersedes:` edge inside the write commit: validate the
        targets exist, reject self-supersession and cycles, strip a stray `superseded_by`
        from the new concept (the newest link in a chain is not itself superseded), and
        stamp each target's frontmatter (confidence: superseded, superseded_by: <this>,
        valid_until: <today>) without touching its body. Returns (normalized, meta,
        edited_target_rels)."""
        raw_targets = self._supersedes_list(meta)
        if not raw_targets:
            return normalized, meta, []
        resolved: list[str] = []
        missing: list[str] = []
        for target in raw_targets:
            _abs, trel = self._resolve(target)
            if trel == rel:
                raise KBError(
                    f"A concept cannot supersede itself ({rel}) — remove it from `supersedes`."
                )
            if not (self.root / trel).is_file():
                missing.append(trel)
            elif trel not in resolved:
                resolved.append(trel)
        if missing:
            raise KBError(
                "supersedes points at concept(s) that do not exist: "
                f"{', '.join(missing)}. Never write a dangling supersede — fix the path(s) "
                "(kb_search to find the real one) or drop them."
            )
        self._guard_supersede_cycle(rel, resolved)
        # Normalize the supersedes list to resolved repo-relative paths and drop any
        # superseded_by the author left on the new concept, then re-serialize.
        doc = split(normalized)
        assert doc is not None
        meta = normalize_meta(doc.meta)
        meta["supersedes"] = resolved if len(resolved) > 1 else resolved[0]
        meta.pop("superseded_by", None)
        normalized = serialize(Doc(meta=meta, body=doc.body))
        edited = [trel for trel in resolved if self._stamp_superseded(trel, rel, today)]
        return normalized, meta, edited

    def _guard_supersede_cycle(self, rel: str, targets: list[str]) -> None:
        """Raise if following the targets' own supersedes chains loops back to ``rel``."""
        seen: set[str] = set()
        stack = list(targets)
        while stack:
            cur = stack.pop()
            if cur == rel:
                raise KBError(
                    f"Supersession cycle detected: {rel} would supersede a concept that "
                    "(transitively) supersedes it back. Supersession is a chain, not a loop."
                )
            if cur in seen:
                continue
            seen.add(cur)
            for nxt in self._supersedes_list(read_meta(self.root / cur)):
                try:
                    _abs, nrel = self._resolve(nxt)
                except KBError:
                    continue
                stack.append(nrel)

    def _stamp_superseded(self, target_rel: str, by_rel: str, today: str) -> bool:
        """Stamp a superseded target's frontmatter (body untouched). Returns True if changed."""
        abs_path = self.root / target_rel
        doc = split(_read_text_retry(abs_path))
        if doc is None:
            raise KBError(f"Cannot supersede {target_rel}: it has no frontmatter fence to mark.")
        meta = normalize_meta(doc.meta)
        before = dict(meta)
        meta["confidence"] = "superseded"
        meta["superseded_by"] = by_rel
        meta.setdefault("valid_until", today)
        if meta == before:
            return False
        _write_text(abs_path, serialize(Doc(meta=meta, body=doc.body)))
        return True

    # ------------------------------------------------------------------ surgical edit + move

    async def kb_edit(
        self,
        path: str,
        operation: str,
        content: str = "",
        find: str | None = None,
        section: str | None = None,
        occurrence: int | str = 1,
        session: str = "",
    ) -> dict[str, Any]:
        """Surgically edit ONE concept's body without rewriting the whole file. Operations:
        append, prepend, find_replace, replace_section, insert_after, insert_before. Never
        touches the frontmatter fence. Returns {path, sha, pushed, operation, warnings}.
        session: when set, a foreign active kb_claim on the path adds a heads-up warning."""
        abs_path, rel = self._resolve(path)
        name = posixpath.basename(rel)
        if operation not in _EDIT_OPERATIONS:
            raise KBError(f"Unknown operation {operation!r} — one of: {', '.join(_EDIT_OPERATIONS)}.")
        if not rel.endswith(".md"):
            raise KBError("kb_edit edits markdown concept files — the path must end in '.md'.")
        if name == "index.md":
            raise KBError("index.md files are server-maintained — edit the concept, not its index.")
        if name == "log.md":
            raise KBError("log.md is append-only — use kb_append_log to add a session entry.")
        if "messages" in rel.split("/")[:-1]:
            raise KBError(
                "Files under messages/ are managed by the messaging tools — use kb_leave_message "
                "and kb_mark_read."
            )
        state: dict[str, Any] = {"warnings": [], "no_change": False}

        def _mutate() -> list[str]:
            if not abs_path.is_file():
                raise KBError(
                    f"No such concept: '{rel}'. kb_edit changes an EXISTING concept's body — "
                    "create new concepts with kb_write."
                )
            original = _read_text_retry(abs_path)
            doc = split(original)
            if doc is None:
                raise KBError(
                    f"'{rel}' has no frontmatter fence — kb_edit edits OKF concept bodies; use "
                    "kb_write to (re)establish this file."
                )
            fm_block = original[: len(original) - len(doc.body)] if doc.body else original
            new_body = _apply_body_edit(operation, doc.body, content, find, section, occurrence, fm_block)
            if new_body == doc.body:
                state["no_change"] = True
                return []
            candidate = serialize(Doc(meta=normalize_meta(doc.meta), body=new_body))
            validated, _meta, warnings = validate_concept(candidate, rel_path=rel)
            state["warnings"] = warnings
            if validated == original:
                state["no_change"] = True
                return []
            _write_text(abs_path, validated)
            return [rel]

        sha, pushed = await self._locked_commit(_mutate, f"kb: edit {rel} ({operation})")
        self._log_mutation("kb_edit", [rel], sha, pushed)
        if not state["no_change"]:
            self._schedule_index(upserts=[rel])
            claim_warn = await to_thread.run_sync(lambda: self._claim_collision_warning(rel, session))
            if claim_warn:
                state["warnings"].append(claim_warn)
        return {
            "path": rel,
            "sha": sha,
            "pushed": pushed,
            "operation": operation,
            "warnings": state["warnings"],
        }

    async def kb_move(self, old_path: str, new_path: str) -> dict[str, Any]:
        """Move a single concept old_path -> new_path, rewriting every relative markdown link
        to it across the whole bundle and both parent indexes. Returns
        {old, new, links_rewritten, sha, pushed}."""
        old_abs, old_rel = self._resolve(old_path)
        new_abs, new_rel = self._resolve(new_path)
        if old_rel == new_rel:
            raise KBError("old_path and new_path are identical — nothing to move.")
        for rel_, label in ((old_rel, "source"), (new_rel, "destination")):
            if not rel_.endswith(".md"):
                raise KBError(f"kb_move moves markdown concepts — the {label} path must end in '.md'.")
            base = posixpath.basename(rel_)
            if base in ("index.md", "log.md"):
                raise KBError(f"{base} is server-maintained and cannot be moved with kb_move.")
            if "messages" in rel_.split("/")[:-1]:
                raise KBError("Messages are managed by the messaging tools — kb_move does not move them.")
        state: dict[str, Any] = {"links": 0}

        def _mutate() -> list[str]:
            if not old_abs.is_file():
                raise KBError(
                    f"No such concept to move: '{old_rel}'. Discover paths via kb_load or kb_search."
                )
            if new_abs.exists():
                raise KBError(
                    f"'{new_rel}' already exists — pick a destination that is free, or edit it directly."
                )
            old_content = _read_text_retry(old_abs)
            # Move the file, rebasing ITS OWN relative links so they still resolve and
            # fixing any self-referential frontmatter refs.
            new_content = _rewrite_frontmatter_refs(
                _rebase_body_links(old_content, old_rel, new_rel), old_rel, new_rel
            )
            _write_text(new_abs, new_content)
            old_abs.unlink()
            touched: set[str] = {old_rel, new_rel}
            meta = read_meta(new_abs)
            removed = _remove_index_bullet(self.root, old_rel)
            if removed:
                touched.add(removed)
            touched.update(
                ensure_indexed(
                    self.root, new_rel, str(meta.get("title") or ""), str(meta.get("description") or "")
                )
            )
            # Rewrite every OTHER file: body links that resolved to old_rel + frontmatter refs.
            for f in self.root.rglob("*.md"):
                if ".git" in f.parts:
                    continue
                f_rel = f.relative_to(self.root).as_posix()
                if f_rel == old_rel:
                    continue
                text = _read_text_retry(f)
                orig = text
                if f_rel != new_rel:
                    text, n = _retarget_body_links(text, posixpath.dirname(f_rel), old_rel, new_rel)
                    state["links"] += n
                text = _rewrite_frontmatter_refs(text, old_rel, new_rel)
                if text != orig:
                    _write_text(f, text)
                    touched.add(f_rel)
            return sorted(touched)

        sha, pushed = await self._locked_commit(_mutate, f"kb: move {old_rel} -> {new_rel}")
        self._log_mutation("kb_move", [old_rel, new_rel], sha, pushed)
        self._schedule_index(upserts=[new_rel], deletes=[old_rel])
        return {
            "old": old_rel,
            "new": new_rel,
            "links_rewritten": state["links"],
            "sha": sha,
            "pushed": pushed,
        }

    async def kb_append_log(self, project: str, entry: str) -> dict[str, Any]:
        """Prepend a dated entry to the project's log.md (newest first; history never edited).
        Returns {ok, path, sha, date, pushed}."""
        if not entry or not entry.strip():
            raise KBError("Empty log entry — describe what happened this session.")
        proot_rel = self._project_rel(project)
        pid = proot_rel.split("/")[-1]
        rel = f"{proot_rel}/log.md"
        today = _utcnow().strftime("%Y-%m-%d")
        bullet = _entry_to_bullet(entry)

        def _mutate() -> list[str]:
            log_path = self.root / rel
            if log_path.is_file():
                new_text = _insert_log_bullet(_read_text_retry(log_path), bullet, today)
            else:
                new_text = f"# Log — {pid}\n\n## {today}\n{bullet}\n"
            _write_text(log_path, new_text)
            return [rel]

        sha, pushed = await self._locked_commit(_mutate, f"log: {pid} {today}")
        self._log_mutation("kb_append_log", [rel], sha, pushed)
        self._schedule_index(upserts=[rel])
        return {"ok": True, "path": rel, "sha": sha, "date": today, "pushed": pushed}

    async def kb_leave_message(
        self,
        project: str,
        title: str,
        body: str,
        to: str = "any",
        priority: str = "normal",
        expires: str | None = None,
    ) -> dict[str, Any]:
        """Leave an inter-session message under <project>/messages/ and regenerate the
        messages index. Returns {path, sha, pushed, warnings}."""
        proot_rel = self._project_rel(project)
        pid = proot_rel.split("/")[-1]
        if not title or not title.strip():
            raise KBError("Message title is required — one line the next session will scan.")
        title = title.strip()
        warnings: list[str] = []
        if to not in _VALID_TO:
            warnings.append(
                f"'to: {to}' is not a known surface ({', '.join(_VALID_TO)}); stored anyway — "
                "receiving sessions filter on it."
            )
        if expires is not None:
            valid = bool(_DATE_RE.match(str(expires)))
            if valid:
                try:
                    date.fromisoformat(str(expires))
                except ValueError:
                    valid = False
            if not valid:
                raise KBError(f"expires must be a YYYY-MM-DD date, got {expires!r}.")

        now = _utcnow()
        today = now.strftime("%Y-%m-%d")
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:_SLUG_MAX].strip("-") or "message"
        state: dict[str, Any] = {}

        def _mutate() -> list[str]:
            mdir = self.root / proot_rel / "messages"
            adir = mdir / "archive"
            name = f"{today}-{slug}.md"
            n = 2
            while (mdir / name).exists() or (adir / name).exists():
                name = f"{today}-{slug}-{n}.md"
                n += 1
            meta: dict[str, Any] = {
                "type": "message",
                "title": title,
                "description": _first_sentence(body) or title,
                "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to": to,
                "status": "unread",
                "priority": priority,
            }
            if expires is not None:
                meta["expires"] = str(expires)
            body_text = body.replace("\r\n", "\n").replace("\r", "\n").strip()
            _write_text(mdir / name, serialize(Doc(meta=meta, body=f"{body_text}\n" if body_text else "")))
            rel = f"{proot_rel}/messages/{name}"
            state["path"] = rel
            paths = [rel]
            if not (adir / "index.md").is_file():
                _write_text(adir / "index.md", _ARCHIVE_INDEX)
                paths.append(f"{proot_rel}/messages/archive/index.md")
            self._regen_messages_index(proot_rel)
            paths.append(f"{proot_rel}/messages/index.md")
            return paths

        sha, pushed = await self._locked_commit(_mutate, f"msg: {pid}: {title}")
        self._log_mutation("kb_leave_message", [state["path"]], sha, pushed)
        self._schedule_index(upserts=[state["path"]])
        return {"path": state["path"], "sha": sha, "pushed": pushed, "warnings": warnings}

    async def kb_mark_read(self, message_path: str) -> dict[str, Any]:
        """Flip a message to status: read, move it to messages/archive/, regenerate the
        messages index. Returns {archived_path, sha, pushed}."""
        abs_path, rel = self._resolve(message_path)
        parts = rel.split("/")
        name = parts[-1]
        if name == "index.md":
            raise KBError(
                "index.md is the messages index, not a message — pass the message file path "
                "from kb_load's unread_messages."
            )
        if len(parts) >= 3 and parts[-2] == "archive" and parts[-3] == "messages":
            raise KBError(f"'{rel}' is already archived — nothing to do.")
        if len(parts) < 2 or parts[-2] != "messages":
            raise KBError(
                f"'{rel}' is not under a messages/ directory — messages live at "
                "<project>/messages/<file>.md; use the path from kb_load's unread_messages."
            )
        mdir_rel = "/".join(parts[:-1])
        proot_rel = "/".join(parts[:-2])
        state: dict[str, Any] = {}

        def _mutate() -> list[str]:
            if not abs_path.is_file():
                raise KBError(
                    f"No such message: '{rel}'. Call kb_load — unread_messages carries the "
                    "current paths."
                )
            doc = split(_read_text_retry(abs_path))
            if doc is None:
                raise KBError(f"'{rel}' has no frontmatter — kb_mark_read only archives 'type: message' files.")
            meta = normalize_meta(doc.meta)
            if meta.get("type") != "message":
                raise KBError(
                    f"'{rel}' has type {meta.get('type')!r} — kb_mark_read only archives "
                    "'type: message' files."
                )
            meta["status"] = "read"
            meta["read_at"] = _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            archived_rel = f"{mdir_rel}/archive/{name}"
            _write_text(self.root / archived_rel, serialize(Doc(meta=meta, body=doc.body)))
            abs_path.unlink()
            state["archived"] = archived_rel
            paths = [rel, archived_rel]
            archive_index = self.root / mdir_rel / "archive" / "index.md"
            if not archive_index.is_file():
                _write_text(archive_index, _ARCHIVE_INDEX)
                paths.append(f"{mdir_rel}/archive/index.md")
            self._regen_messages_index(proot_rel)
            paths.append(f"{mdir_rel}/index.md")
            return paths

        sha, pushed = await self._locked_commit(_mutate, f"msg: read {name}")
        self._log_mutation("kb_mark_read", [rel, state["archived"]], sha, pushed)
        self._schedule_index(upserts=[state["archived"]], deletes=[rel])
        return {"archived_path": state["archived"], "sha": sha, "pushed": pushed}

    # ------------------------------------------------------------------ inbox

    async def kb_inbox(self, text: str) -> dict[str, Any]:
        """Quick-capture a raw thought into inbox/ at the bundle root — zero ceremony,
        untriaged. Returns {path, sha, pushed}."""
        if not text or not text.strip():
            raise KBError("Empty inbox capture — pass the thought, task, or link to remember.")
        clean = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        first_line = clean.splitlines()[0].strip()
        title = first_line[:120].strip() or "Inbox note"
        now = _utcnow()
        today = now.strftime("%Y-%m-%d")
        slug = re.sub(r"[^a-z0-9]+", "-", first_line.lower()).strip("-")[:_SLUG_MAX].strip("-") or "note"
        state: dict[str, Any] = {}

        def _mutate() -> list[str]:
            idir = self.root / "inbox"
            name = f"{today}-{slug}.md"
            n = 2
            while (idir / name).exists():
                name = f"{today}-{slug}-{n}.md"
                n += 1
            rel = f"inbox/{name}"
            meta: dict[str, Any] = {
                "type": "inbox",
                "title": title,
                "description": _first_sentence(clean) or title,
                "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "untriaged",
            }
            _write_text(self.root / rel, serialize(Doc(meta=meta, body=f"{clean}\n")))
            state["path"] = rel
            paths = [rel]
            paths.extend(
                ensure_indexed(self.root, rel, title, str(meta["description"]))
            )
            return paths

        sha, pushed = await self._locked_commit(_mutate, f"inbox: {title[:60]}")
        self._log_mutation("kb_inbox", [state["path"]], sha, pushed)
        self._schedule_index(upserts=[state["path"]])
        return {"path": state["path"], "sha": sha, "pushed": pushed}

    # ------------------------------------------------------------------ cross-session threads

    def _thread_dir(self, thread: str) -> tuple[str, str]:
        """Validate a kebab thread id and return (thread-id, 'threads/<id>' rel dir)."""
        tid = (thread or "").strip()
        if not tid or not _THREAD_ID_RE.fullmatch(tid) or not _has_alnum(tid):
            raise KBError(
                f"Invalid thread id {thread!r} — thread ids are kebab-case (lowercase "
                "letters, digits, hyphens) with at least one letter or digit, e.g. "
                "'deploy-handoff'. Call kb_threads to list the open threads."
            )
        return tid, f"threads/{tid}"

    def _read_turns(self, turns_dir: Path) -> list[dict[str, Any]]:
        """Every turn under a thread's turns/ dir, ordered by seq (failure-soft per file)."""
        out: list[dict[str, Any]] = []
        if not turns_dir.is_dir():
            return out
        for f in sorted(turns_dir.glob("*.md")):
            if f.name == "index.md":
                continue
            try:
                doc = split(_read_text_retry(f))
            except Exception:  # noqa: BLE001 — skip a bad turn, never crash a read
                continue
            if doc is None:
                continue
            meta = normalize_meta(doc.meta)
            if str(meta.get("type") or "") != "thread-turn":
                continue
            try:
                seq = int(meta.get("seq"))
            except (TypeError, ValueError):
                seq = 0
            raw_refs = meta.get("refs")
            refs = [str(r) for r in raw_refs] if isinstance(raw_refs, list) else []
            out.append(
                {
                    "seq": seq,
                    "sender": str(meta.get("sender") or ""),
                    "timestamp": str(meta.get("timestamp") or ""),
                    "message": doc.body.strip("\n"),
                    "refs": refs,
                }
            )
        out.sort(key=lambda t: (t["seq"], t["timestamp"]))
        return out

    @staticmethod
    def _render_thread(meta: dict[str, Any], turns: list[dict[str, Any]]) -> str:
        """Human-readable transcript body regenerated on each post (explorer renders it)."""
        topic = str(meta.get("topic") or "")
        status = str(meta.get("status") or "open")
        parts = meta.get("participants")
        participants = ", ".join(str(p) for p in parts) if isinstance(parts, list) else ""
        head = [f"# Thread: {topic or meta.get('title') or 'untitled'}", ""]
        if status == "closed":
            closed_by = str(meta.get("closed_by") or "")
            closed_at = str(meta.get("closed_at") or "")
            head.append(f"**Status:** closed (by {closed_by} at {closed_at})")
        else:
            head.append("**Status:** open")
        head.append(f"**Participants:** {participants or '—'}")
        head.append("")
        head.append("---")
        head.append("")
        lines = list(head)
        for t in turns:
            lines.append(f"### {t['seq']}. {t['sender']} — {t['timestamp']}")
            lines.append("")
            lines.append(t["message"] or "_(empty)_")
            refs = t.get("refs")
            if refs:
                # Rendered as code spans, not markdown links: the reconcile link sweep
                # must not read a shared path as an outbound link from the thread dir
                # (it would resolve wrong and look dangling). The turn's refs: frontmatter
                # is the machine-readable copy the dashboard consumes.
                lines.append("")
                lines.append("shared: " + ", ".join(f"`{r}`" for r in refs))
            lines.append("")
        return "\n".join(lines).rstrip("\n") + "\n"

    async def kb_thread_post(
        self,
        thread: str,
        sender: str,
        message: str,
        close: bool = False,
        topic: str = "",
        refs: list[str] | None = None,
        allow_secrets: bool = False,
        wait_for_reply: bool = False,
        wait_seconds: int = 25,
    ) -> dict[str, Any]:
        """Post one turn to a bundle-root cross-session thread (auto-creates it on first
        post). Appends a turn file, adds the sender to participants, regenerates the
        thread.md transcript, and — when close=True — marks the thread closed with this as
        the final turn. Optional `refs` are repo-relative concept/artifact paths shared into
        the room: they land in the turn's refs: frontmatter and render as a 'shared:' line in
        the transcript (a ref that doesn't exist is warned, not blocked). To share a CODE
        snippet, put a fenced code block in `message` — it renders verbatim. The message is
        scanned for secrets before it is committed (threads are private but git-permanent);
        pass allow_secrets=True to override.

        wait_for_reply=True makes this ONE call = post + await the answer: after posting, the
        server long-polls (`wait_seconds`, clamped 1..55) for the first turn from a DIFFERENT
        sender (or a close) and returns it as `reply` (the turn dict), or `reply: null` +
        `waited: true` on timeout. This is the token-cheap way to hold a two-session
        conversation — one tool call per exchange instead of a post plus a poll loop.

        Returns {thread, seq, status, participants, posted, pushed, warnings} plus
        {reply, waited?} when wait_for_reply=True."""
        tid, tdir_rel = self._thread_dir(thread)
        if not sender or not sender.strip():
            raise KBError("A thread post needs a sender — the name of the session posting (e.g. 'session-a').")
        if not message or not message.strip():
            raise KBError("Empty thread message — pass what you want the other session to read.")
        if not allow_secrets:
            findings = _scan_secrets(message)
            if findings:
                raise KBError(_secret_refusal("thread message", findings, "allow_secrets=True"))
        sender = sender.strip()
        refs_list = [str(r).strip() for r in (refs or []) if str(r).strip()]
        # Warn (never block) on a ref that doesn't resolve to an existing file — a shared
        # path the other session couldn't open is a coordination bug worth surfacing.
        warnings: list[str] = []
        for r in refs_list:
            try:
                abs_ref, _rr = self._resolve(r)
            except KBError:
                warnings.append(f"shared ref {r!r} is not a valid repo path — the other session can't open it.")
                continue
            if not abs_ref.is_file():
                warnings.append(f"shared ref {r!r} does not exist (yet) — the other session can't open it.")
        sender_slug = re.sub(r"[^a-z0-9]+", "-", sender.lower()).strip("-")[:_SLUG_MAX].strip("-") or "anon"
        clean = message.replace("\r\n", "\n").replace("\r", "\n").strip()
        now = _utcnow()
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        state: dict[str, Any] = {}

        def _mutate() -> list[str]:
            tdir = self.root / tdir_rel
            thread_path = tdir / "thread.md"
            turns_dir = tdir / "turns"
            existing = self._read_turns(turns_dir)

            if thread_path.is_file():
                tmeta = normalize_meta(split(_read_text_retry(thread_path)).meta)
                if str(tmeta.get("status") or "open") == "closed":
                    raise KBError(
                        f"Thread '{tid}' is closed (by {tmeta.get('closed_by')!r}) — no more "
                        "posts. Start a new thread with a different id to keep talking."
                    )
                created = False
            else:
                tmeta = {
                    "type": "thread",
                    "title": f"Thread: {topic.strip() or tid}",
                    "description": (topic.strip() or f"Cross-session thread {tid}")[:_SLUG_MAX * 3],
                    "topic": topic.strip(),
                    "status": "open",
                    "participants": [],
                    "created": timestamp,
                    "timestamp": timestamp,
                }
                created = True

            # Next sequence = max seen + 1, NOT len(existing): robust to a skipped/corrupt
            # turn file (which _read_turns drops) so seq numbers never collide or rewind.
            seq = (max(t["seq"] for t in existing) + 1) if existing else 0
            participants = list(tmeta.get("participants") or [])
            if sender not in participants:
                participants.append(sender)

            # Collision-safe turn filename (compact ts + sender slug); the write lock
            # already serializes posts, this only guards a same-microsecond retry.
            base = now.strftime("%Y%m%dT%H%M%S%f")
            name = f"{base}-{sender_slug}.md"
            n = 2
            while (turns_dir / name).exists():
                name = f"{base}-{sender_slug}-{n}.md"
                n += 1
            turn_meta: dict[str, Any] = {
                "type": "thread-turn",
                "title": f"Turn {seq} — {sender}",
                "description": _first_sentence(clean) or f"Turn {seq} from {sender}",
                "sender": sender,
                "timestamp": timestamp,
                "seq": seq,
            }
            # Only stamp refs: when there are some, so a plain post's turn file stays
            # byte-identical to the pre-refs shape.
            if refs_list:
                turn_meta["refs"] = refs_list
            turn_rel = f"{tdir_rel}/turns/{name}"
            _write_text(turns_dir / name, serialize(Doc(meta=turn_meta, body=f"{clean}\n")))

            all_turns = existing + [
                {"seq": seq, "sender": sender, "timestamp": timestamp, "message": clean, "refs": refs_list}
            ]
            tmeta["participants"] = participants
            tmeta["timestamp"] = timestamp
            tmeta["last_activity"] = timestamp
            if close:
                tmeta["status"] = "closed"
                tmeta["closed_by"] = sender
                tmeta["closed_at"] = timestamp
            _write_text(thread_path, serialize(Doc(meta=tmeta, body=self._render_thread(tmeta, all_turns))))

            paths = [turn_rel, f"{tdir_rel}/thread.md"]
            if created:
                paths.extend(
                    ensure_indexed(self.root, f"{tdir_rel}/thread.md", str(tmeta["title"]), str(tmeta["description"]))
                )
            # Keep the turn reachable from its parent index so the reconcile membership
            # sweep never has to repair it.
            paths.extend(ensure_indexed(self.root, turn_rel, str(turn_meta["title"]), str(turn_meta["description"])))

            state["seq"] = seq
            state["status"] = str(tmeta["status"])
            state["participants"] = participants
            return paths

        verb = "close" if close else "post"
        sha, pushed = await self._locked_commit(_mutate, f"thread: {tid} {sender} {verb}")
        self._log_mutation("kb_thread_post", [f"{tdir_rel}/thread.md"], sha, pushed)
        self._schedule_index(upserts=[f"{tdir_rel}/thread.md"])
        result: dict[str, Any] = {
            "thread": tid,
            "seq": state["seq"],
            "status": state["status"],
            "participants": state["participants"],
            "posted": timestamp,
            "pushed": pushed,
            "warnings": warnings,
        }
        # Post-and-await: hold the same call open for the other session's reply. A closed
        # thread (we just closed it, or it already was) can't receive a reply, so skip.
        if wait_for_reply and not close and state["status"] != "closed":
            w = max(1, min(int(wait_seconds or 25), 55))
            deadline = time.monotonic() + w
            cursor = timestamp  # our own post — turns after it are candidate replies
            reply: dict[str, Any] | None = None
            closed = False
            # Long-poll the read, advancing the cursor past any SAME-sender turns until a
            # DIFFERENT sender speaks (the reply) or the thread closes — each read chunk is
            # itself a server-side wait, so this is all free to the caller.
            while time.monotonic() < deadline:
                chunk = min(55, int(deadline - time.monotonic()))
                if chunk < 1:
                    break
                rr = await self.kb_thread_read(thread, since=cursor, wait_seconds=chunk)
                new_turns = rr.get("turns", [])
                if rr.get("cursor"):
                    cursor = rr["cursor"]
                diff = [t for t in new_turns if t.get("sender") != sender]
                if diff:
                    reply = diff[0]
                    result["status"] = rr["status"]  # may have flipped to closed
                    break
                if rr.get("status") == "closed":
                    closed = True
                    break
                if not new_turns:
                    break  # the read timed out with nothing new → overall timeout
            result["reply"] = reply
            if reply is None and not closed:
                result["waited"] = True
        return result

    async def kb_thread_read(
        self, thread: str, since: str | None = None, wait_seconds: int = 0
    ) -> dict[str, Any]:
        """Read a cross-session thread — poll for the other session's turns. `since` is a
        cursor (a prior read's `cursor`); only turns AFTER it are returned. An unknown
        thread returns {status: 'none', turns: []}. Returns {thread, status, topic,
        participants, turns: [{seq, sender, timestamp, message}], cursor, closed_by?}.

        `wait_seconds` (clamped 0..55) turns this into a server-side LONG-POLL: when > 0 and
        there are no new turns after `since`, the server loops (1s sleep + re-read) and
        returns the INSTANT a new turn arrives or the thread closes, or the empty result at
        timeout. This is free to the caller — one tool call covers the whole wait instead of
        N tight polls, each of which would cost the user tokens. Zero busy CPU (it sleeps),
        zero extra pulls (read is local file IO).

        Deliberately does NOT trigger a git pull: both sessions post THROUGH this same
        server to the same local checkout, so a turn is on disk the instant its post
        commits and a same-server poll sees it with zero network round-trip. This keeps a
        tight poll loop cheap (a local file read). External/other-PC posts are still picked
        up by the periodic pull that other tools drive."""
        tid, tdir_rel = self._thread_dir(thread)

        def _read() -> dict[str, Any]:
            tdir = self.root / tdir_rel
            thread_path = tdir / "thread.md"
            none = {"thread": tid, "status": "none", "topic": "", "participants": [], "turns": [], "cursor": since}
            if not thread_path.is_file():
                return none
            try:
                doc = split(_read_text_retry(thread_path))
            except Exception:  # noqa: BLE001 — a corrupt thread.md reads as absent, never crashes
                return none
            if doc is None:
                return none
            tmeta = normalize_meta(doc.meta)
            all_turns = self._read_turns(tdir / "turns")
            newest = all_turns[-1]["timestamp"] if all_turns else since
            shown = [t for t in all_turns if not since or t["timestamp"] > since]
            out: dict[str, Any] = {
                "thread": tid,
                "status": str(tmeta.get("status") or "open"),
                "topic": str(tmeta.get("topic") or ""),
                "participants": list(tmeta.get("participants") or []),
                "turns": shown,
                "cursor": newest,
            }
            if str(tmeta.get("status") or "") == "closed":
                out["closed_by"] = str(tmeta.get("closed_by") or "")
            return out

        result = await to_thread.run_sync(_read)
        wait = max(0, min(int(wait_seconds or 0), 55))
        # A new turn or a closed thread means there is nothing to wait for — return now.
        # (status 'none' keeps waiting: a joiner may read before the opener's first post.)
        if wait <= 0 or result["turns"] or result["status"] == "closed":
            return result
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            await anyio.sleep(1.0)
            result = await to_thread.run_sync(_read)
            if result["turns"] or result["status"] == "closed":
                return result
        return result  # timeout — the normal empty result

    async def kb_threads(self) -> list[dict[str, Any]]:
        """List cross-session threads (newest activity first) so a session can discover or
        join one by name. Returns [{thread, status, topic, participants, turn_count,
        last_activity}]."""
        await self._refresh()
        return await to_thread.run_sync(self._threads_sync)

    def _threads_sync(self) -> list[dict[str, Any]]:
        troot = self.root / "threads"
        out: list[dict[str, Any]] = []
        if not troot.is_dir():
            return out
        for tdir in sorted(troot.iterdir()):
            if not tdir.is_dir() or tdir.name.startswith("."):
                continue
            thread_path = tdir / "thread.md"
            if not thread_path.is_file():
                continue
            meta = read_meta(thread_path)
            turns = self._read_turns(tdir / "turns")
            out.append(
                {
                    "thread": tdir.name,
                    "status": str(meta.get("status") or "open"),
                    "topic": str(meta.get("topic") or ""),
                    "participants": list(meta.get("participants") or []),
                    "turn_count": len(turns),
                    "last_activity": str(meta.get("last_activity") or meta.get("timestamp") or ""),
                }
            )
        out.sort(key=lambda t: str(t.get("last_activity") or ""), reverse=True)
        return out

    # ------------------------------------------------------------------ workspace (presence / roster / handoff)

    async def kb_presence(
        self,
        session: str,
        name: str = "",
        status: str = "working",
        working_on: str = "",
        repo: str = "",
        branch: str = "",
        repo_remote: str = "",
        cwd: str = "",
        project: str = "",
        note: str = "",
        host: str = "",
    ) -> dict[str, Any]:
        """Upsert THIS session's presence record (one file per session, overwritten each
        call) so the roster and Hiren's dashboard can see who's working on what. Returns
        {session, updated, roster_active} where roster_active counts sessions active in the
        last 15 minutes."""
        sid = _slug(session)
        if not sid:
            raise KBError(
                "A presence record needs a session id — a short kebab-case handle for THIS "
                "session, e.g. 'pc1-claude-code' or 'mobile-hiren'."
            )
        st = (status or "working").strip().lower()
        if st not in _PRESENCE_STATUSES:
            raise KBError(
                f"Invalid status {status!r} — use one of: {', '.join(_PRESENCE_STATUSES)}."
            )
        now = _utcnow()
        updated = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        clean_note = (note or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        state: dict[str, Any] = {}

        def _mutate() -> list[str]:
            rel = f"workspace/presence/{sid}.md"
            title = f"Presence: {name.strip() or sid}"
            desc = (working_on.strip() or f"{sid} — {st}")[: _SLUG_MAX * 3]
            meta: dict[str, Any] = {
                "type": "presence",
                "title": title,
                "description": desc,
                "session": sid,
                "name": name.strip(),
                "status": st,
                "working_on": working_on.strip(),
                "repo": repo.strip(),
                "branch": branch.strip(),
                "repo_remote": repo_remote.strip(),
                "cwd": cwd.strip(),
                "project": project.strip(),
                "host": host.strip(),
                "updated": updated,
            }
            body = f"{clean_note}\n" if clean_note else ""
            _write_text(self.root / rel, serialize(Doc(meta=meta, body=body)))
            state["rel"] = rel
            # Keep the record linked from its parent index so the reconcile membership
            # sweep never has to repair it; idempotent on re-announce (already linked).
            paths = [rel]
            paths.extend(ensure_indexed(self.root, rel, title, desc))
            return paths

        sha, pushed = await self._locked_commit(_mutate, f"workspace: presence {sid} ({st})")
        self._log_mutation("kb_presence", [state["rel"]], sha, pushed)
        # Presence records churn on every heartbeat and are pure ephemera — deliberately
        # NOT fed to the semantic index (the nightly full reindex still catches them).
        roster_active = await to_thread.run_sync(lambda: len(self._presence_records_sync(15)))
        return {"session": sid, "updated": updated, "roster_active": roster_active}

    async def kb_roster(self, active_within_min: int = 15) -> list[dict[str, Any]]:
        """List sessions active within the window (default 15 min), most-recently-updated
        first — the 'who's online' board across all of Hiren's PCs and projects. Stale
        records are filtered out (not deleted); pass active_within_min<=0 to disable the
        filter and list every record. Returns [{session, name, status, working_on,
        repo, branch, repo_remote, cwd, project, host, updated, age_min}]."""
        await self._refresh()
        return await to_thread.run_sync(lambda: self._presence_records_sync(active_within_min))

    def _presence_records_sync(self, active_within_min: int | None) -> list[dict[str, Any]]:
        """Every presence record, newest-updated first, TTL-filtered. age_min is minutes
        since `updated` (None when the timestamp is missing/unparseable). A record with an
        unusable timestamp is treated as stale and excluded when a window is set. A window
        <= 0 (or None) means NO filter — every record, stale or not, is returned."""
        pdir = self.root / "workspace" / "presence"
        out: list[dict[str, Any]] = []
        if not pdir.is_dir():
            return out
        now = _utcnow()
        for f in sorted(pdir.glob("*.md")):
            if f.name == "index.md":
                continue
            meta = read_meta(f)
            if str(meta.get("type") or "") != "presence":
                continue
            updated = str(meta.get("updated") or "")
            dt = _parse_ts(updated)
            age_min = round((now - dt).total_seconds() / 60.0, 1) if dt is not None else None
            if active_within_min is not None and active_within_min > 0:
                if age_min is None or age_min > active_within_min:
                    continue
            out.append(
                {
                    "session": str(meta.get("session") or f.stem),
                    "name": str(meta.get("name") or ""),
                    "status": str(meta.get("status") or ""),
                    "working_on": str(meta.get("working_on") or ""),
                    "repo": str(meta.get("repo") or ""),
                    "branch": str(meta.get("branch") or ""),
                    "repo_remote": str(meta.get("repo_remote") or ""),
                    "cwd": str(meta.get("cwd") or ""),
                    "project": str(meta.get("project") or ""),
                    "host": str(meta.get("host") or ""),
                    "updated": updated,
                    "age_min": age_min,
                }
            )
        out.sort(key=lambda r: str(r.get("updated") or ""), reverse=True)
        return out

    def _presence_write_batch(
        self, records: list[dict[str, Any]], refresh_minutes: int, written: list[str]
    ) -> list[str]:
        """Upsert MANY presence records in ONE commit — the spool-ingest write path.

        Pure mutate: called INSIDE ``_locked_commit`` (never a second git writer). Each
        record is a spool dict keyed by ``session``; a record is written only when a
        meaningful field (status/name/working_on/repo/branch/repo_remote/cwd/project)
        differs from the on-disk record OR the on-disk ``updated`` is older than
        ``refresh_minutes`` — so a long-running session refreshes to stay inside the
        roster window without a commit per heartbeat. Reuses the same slug/status/meta
        shape as ``kb_presence``. Appends each written session slug to ``written`` and
        returns every touched rel path (records + indexes, de-duplicated, in order);
        ``[]`` means nothing changed so ``_locked_commit`` makes no commit."""
        now = _utcnow()
        updated = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        window = max(0, refresh_minutes) * 60
        paths: list[str] = []
        seen: set[str] = set()
        for rec in records:
            sid = _slug(str(rec.get("session") or ""))
            if not sid:
                continue  # unusable session id — failure-soft skip
            st = str(rec.get("status") or "working").strip().lower()
            if st in ("ended", "end", "closed"):
                st = "done"  # a session-end hook says done; presence file is kept (housekeeping prunes)
            if st not in _PRESENCE_STATUSES:
                st = "working"  # coerce rather than raise — ingest must stay failure-soft
            name = str(rec.get("name") or "").strip()
            working_on = str(rec.get("working_on") or "").strip()
            repo = str(rec.get("repo") or "").strip()
            branch = str(rec.get("branch") or "").strip()
            repo_remote = str(rec.get("repo_remote") or "").strip()
            cwd = str(rec.get("cwd") or "").strip()
            project = str(rec.get("project") or "").strip()
            host = str(rec.get("host") or "").strip()
            note = (
                str(rec.get("note") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            )

            rel = f"workspace/presence/{sid}.md"
            abs_path = self.root / rel
            if abs_path.is_file():
                old = read_meta(abs_path)
                unchanged = (
                    str(old.get("status") or "") == st
                    and str(old.get("name") or "") == name
                    and str(old.get("working_on") or "") == working_on
                    and str(old.get("repo") or "") == repo
                    and str(old.get("branch") or "") == branch
                    and str(old.get("repo_remote") or "") == repo_remote
                    and str(old.get("cwd") or "") == cwd
                    and str(old.get("project") or "") == project
                )
                old_dt = _parse_ts(str(old.get("updated") or ""))
                fresh = old_dt is not None and (now - old_dt).total_seconds() < window
                if unchanged and fresh:
                    continue  # deduped: no meaningful change and still inside the window

            title = f"Presence: {name or sid}"
            desc = (working_on or f"{sid} — {st}")[: _SLUG_MAX * 3]
            meta: dict[str, Any] = {
                "type": "presence",
                "title": title,
                "description": desc,
                "session": sid,
                "name": name,
                "status": st,
                "working_on": working_on,
                "repo": repo,
                "branch": branch,
                "repo_remote": repo_remote,
                "cwd": cwd,
                "project": project,
                "host": host,
                "updated": updated,
            }
            body = f"{note}\n" if note else ""
            _write_text(abs_path, serialize(Doc(meta=meta, body=body)))
            written.append(sid)
            for p in [rel, *ensure_indexed(self.root, rel, title, desc)]:
                if p not in seen:
                    seen.add(p)
                    paths.append(p)
        return paths

    async def kb_handoff(
        self,
        from_session: str,
        summary: str,
        repo: str = "",
        branch: str = "",
        state: str = "",
        next_steps: str = "",
        refs: list[str] | None = None,
        to: str = "any",
        room: str = "",
        allow_secrets: bool = False,
    ) -> dict[str, Any]:
        """Write a structured, append-only handoff record (repo/branch/state/next-steps/refs)
        so another session resumes exactly where this one left off. When `room` is given, ALSO
        posts a short pointer into that thread so a watching session is notified. summary and
        next_steps are scanned for secrets before the record is committed (handoffs are private
        but git-permanent); pass allow_secrets=True to override. Returns {path, sha, pushed,
        notified, room_error?}."""
        frm = (from_session or "").strip()
        if not frm or not _has_alnum(frm):
            raise KBError(
                "A handoff needs from_session — the name of the session handing off, with at "
                "least one letter or digit (e.g. 'session-a')."
            )
        if not summary or not summary.strip():
            raise KBError("Empty handoff summary — say what work is being handed off.")
        if not allow_secrets:
            findings = _scan_secrets(summary) + _scan_secrets(next_steps)
            if findings:
                raise KBError(_secret_refusal("handoff", findings, "allow_secrets=True"))
        room_id = (room or "").strip()
        if room_id and not _THREAD_ID_RE.fullmatch(room_id):
            # Validate up front so a bad room id can't leave a committed handoff whose
            # notification silently failed.
            raise KBError(
                f"Invalid room id {room_id!r} — rooms are kebab-case (lowercase letters, "
                "digits, hyphens), e.g. 'deploy-handoff'. Call kb_threads to list open rooms."
            )
        from_slug = _slug(frm, "anon")
        refs_list = [str(r).strip() for r in (refs or []) if str(r).strip()]
        to_clean = (to or "any").strip() or "any"
        summary_clean = summary.replace("\r\n", "\n").replace("\r", "\n").strip()
        now = _utcnow()
        created = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        result: dict[str, Any] = {}

        def _mutate() -> list[str]:
            hdir = self.root / "workspace" / "handoffs"
            base = now.strftime("%Y%m%dT%H%M%S%f")
            name = f"{base}-{from_slug}.md"
            n = 2
            while (hdir / name).exists():
                name = f"{base}-{from_slug}-{n}.md"
                n += 1
            rel = f"workspace/handoffs/{name}"
            title = f"Handoff from {frm}"
            desc = _first_sentence(summary_clean) or title
            meta: dict[str, Any] = {
                "type": "handoff",
                "title": title,
                "description": desc,
                "from": frm,
                "to": to_clean,
                "summary": summary_clean,
                "repo": repo.strip(),
                "branch": branch.strip(),
                "state": state.strip(),
                "next_steps": next_steps.strip(),
                "refs": refs_list,
                "created": created,
                "status": "open",
            }
            _write_text(self.root / rel, serialize(Doc(meta=meta, body=self._render_handoff(meta))))
            result["rel"] = rel
            paths = [rel]
            paths.extend(ensure_indexed(self.root, rel, title, desc))
            return paths

        sha, pushed = await self._locked_commit(_mutate, f"workspace: handoff from {from_slug}")
        self._log_mutation("kb_handoff", [result["rel"]], sha, pushed)
        self._schedule_index(upserts=[result["rel"]])

        notified = False
        room_error = ""
        if room_id:
            # Notify a watching session. The handoff already committed; a room-post failure
            # (e.g. the room is closed) must not fail the handoff — log, report, move on.
            pointer = f"Handoff from {frm}: {summary_clean}"
            if next_steps.strip():
                pointer += f"\n\n**Next:** {next_steps.strip()}"
            try:
                # allow_secrets carries through: summary/next_steps already passed the scan
                # above, so the pointer built from them won't be spuriously refused.
                await self.kb_thread_post(
                    room_id, frm, pointer, refs=[result["rel"], *refs_list], allow_secrets=True
                )
                notified = True
            except KBError as exc:
                room_error = str(exc)
                log.warning("kb_handoff: could not post pointer into room %r", room_id, exc_info=True)

        out: dict[str, Any] = {"path": result["rel"], "sha": sha, "pushed": pushed, "notified": notified}
        if room_error:
            out["room_error"] = room_error
        return out

    @staticmethod
    def _render_handoff(meta: dict[str, Any]) -> str:
        """Human-readable handoff body (explorer renders it). Refs are code spans, not
        markdown links, so the reconcile link sweep doesn't misread them from workspace/."""
        frm = str(meta.get("from") or "")
        lines = [f"# Handoff from {frm}", ""]
        lines.append(
            f"**To:** {meta.get('to') or 'any'}   "
            f"**Status:** {meta.get('status') or 'open'}   "
            f"**Created:** {meta.get('created') or ''}"
        )
        repo = str(meta.get("repo") or "")
        branch = str(meta.get("branch") or "")
        if repo or branch:
            lines.append(f"**Repo:** {repo or '—'}   **Branch:** {branch or '—'}")
        lines += ["", "## Summary", str(meta.get("summary") or "").strip() or "—"]
        state = str(meta.get("state") or "").strip()
        if state:
            lines += ["", "## State", state]
        nxt = str(meta.get("next_steps") or "").strip()
        if nxt:
            lines += ["", "## Next steps", nxt]
        refs = meta.get("refs")
        if isinstance(refs, list) and refs:
            lines += ["", "## Refs"]
            lines += [f"- `{r}`" for r in refs]
        return "\n".join(lines).rstrip("\n") + "\n"

    def _handoffs_sync(self, limit: int | None = 5) -> list[dict[str, Any]]:
        """Recent handoff records, newest first (filenames are compact-ts prefixed)."""
        hdir = self.root / "workspace" / "handoffs"
        out: list[dict[str, Any]] = []
        if not hdir.is_dir():
            return out
        for f in sorted(hdir.glob("*.md")):
            if f.name == "index.md":
                continue
            meta = read_meta(f)
            if str(meta.get("type") or "") != "handoff":
                continue
            raw_refs = meta.get("refs")
            refs = [str(r) for r in raw_refs] if isinstance(raw_refs, list) else []
            out.append(
                {
                    "path": f.relative_to(self.root).as_posix(),
                    "from": str(meta.get("from") or ""),
                    "to": str(meta.get("to") or "any"),
                    "summary": str(meta.get("summary") or ""),
                    "repo": str(meta.get("repo") or ""),
                    "branch": str(meta.get("branch") or ""),
                    "state": str(meta.get("state") or ""),
                    "next_steps": str(meta.get("next_steps") or ""),
                    "refs": refs,
                    "created": str(meta.get("created") or ""),
                    "status": str(meta.get("status") or "open"),
                }
            )
        out.sort(key=lambda h: str(h.get("created") or ""), reverse=True)
        return out[:limit] if (limit is not None and limit >= 0) else out

    async def kb_workspace(self) -> dict[str, Any]:
        """One-shot snapshot of the whole workspace: {roster, rooms, recent_handoffs}. Single
        refresh, then reads presence (active 15 min), open threads, and the last ~5 handoffs.
        recent_handoffs carries summaries (from/to/summary/status/path), NOT the full
        state/next_steps bodies — kb_read the handoff path when you need the full detail."""
        await self._refresh()

        def _snapshot() -> dict[str, Any]:
            # Handoffs are trimmed to their summary here: a workspace glance wants "who
            # handed what to whom", not the full state + next_steps prose (which can be
            # paragraphs). The path is kept so a session can kb_read the full record.
            handoffs = [
                {
                    "path": h["path"],
                    "from": h["from"],
                    "to": h["to"],
                    "summary": h["summary"],
                    "repo": h["repo"],
                    "branch": h["branch"],
                    "created": h["created"],
                    "status": h["status"],
                }
                for h in self._handoffs_sync(5)
            ]
            return {
                "roster": self._presence_records_sync(15),
                "rooms": self._threads_sync(),
                "recent_handoffs": handoffs,
            }

        return await to_thread.run_sync(_snapshot)

    # ------------------------------------------------------------------ claims (advisory locks)

    def _claim_slug(self, path: str) -> str:
        """Filename stem for a claim on ``path`` — one file per claimed path/task, upserted."""
        return _slug(path, "claim")

    async def kb_claim(self, session: str, path: str, note: str = "") -> dict[str, Any]:
        """Claim a concept/file/task so other sessions know you're working on it (advisory).
        Upserts one record per path. If a live claim by a DIFFERENT session already exists,
        still succeeds but returns already_claimed_by. Returns {path, session, claimed_at,
        already_claimed_by?}."""
        sid = _slug(session)
        if not sid:
            raise KBError(
                "A claim needs a session id — a short kebab-case handle for THIS session, "
                "e.g. 'pc1-claude-code'."
            )
        p = (path or "").strip().replace("\\", "/")
        if not p or not _has_alnum(p):
            raise KBError(
                "A claim needs a path or task with at least one letter/digit — pass the "
                "repo-relative concept path you're editing (e.g. 'projects/alt/specs/api.md') "
                "or a short task string."
            )
        slug = self._claim_slug(p)
        now = _utcnow()
        claimed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        clean_note = (note or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        state: dict[str, Any] = {}

        def _mutate() -> list[str]:
            rel = f"workspace/claims/{slug}.md"
            abs_claim = self.root / rel
            # A live foreign claim on this path? Report it (advisory) before we upsert ours.
            if abs_claim.is_file():
                prior = read_meta(abs_claim)
                if str(prior.get("type") or "") == "claim":
                    prior_sess = str(prior.get("session") or "")
                    dt = _parse_ts(str(prior.get("claimed_at") or ""))
                    age = (now - dt).total_seconds() / 60.0 if dt is not None else None
                    if age is not None and age <= _CLAIM_TTL_MIN and prior_sess and prior_sess != sid:
                        state["already_claimed_by"] = prior_sess
            title = f"Claim: {p}"
            desc = (clean_note or p)[: _SLUG_MAX * 3]
            meta: dict[str, Any] = {
                "type": "claim",
                "title": title,
                "description": desc,
                "path": p,
                "session": sid,
                "note": clean_note,
                "claimed_at": claimed_at,
            }
            body = f"{clean_note}\n" if clean_note else ""
            _write_text(abs_claim, serialize(Doc(meta=meta, body=body)))
            state["rel"] = rel
            paths = [rel]
            paths.extend(ensure_indexed(self.root, rel, title, desc))
            return paths

        sha, pushed = await self._locked_commit(_mutate, f"workspace: claim {slug}")
        self._log_mutation("kb_claim", [state["rel"]], sha, pushed)
        out: dict[str, Any] = {"path": p, "session": sid, "claimed_at": claimed_at}
        if "already_claimed_by" in state:
            out["already_claimed_by"] = state["already_claimed_by"]
        return out

    async def kb_release(self, session: str, path: str) -> dict[str, Any]:
        """Release your claim on a path (only if you own it; else a no-op with a note).
        Returns {path, released, note?}."""
        sid = _slug(session)
        if not sid:
            raise KBError("A release needs the session id that made the claim.")
        p = (path or "").strip().replace("\\", "/")
        if not p or not _has_alnum(p):
            raise KBError("A release needs the claimed path/task string.")
        slug = self._claim_slug(p)
        state: dict[str, Any] = {"released": False, "note": ""}

        def _mutate() -> list[str]:
            rel = f"workspace/claims/{slug}.md"
            abs_claim = self.root / rel
            if not abs_claim.is_file():
                state["note"] = "no active claim on that path — nothing to release"
                return []
            meta = read_meta(abs_claim)
            owner = str(meta.get("session") or "")
            if owner and owner != sid:
                state["note"] = f"claim is held by {owner}, not {sid} — left in place"
                return []
            index_rel = _remove_index_bullet(self.root, rel)
            abs_claim.unlink()
            state["released"] = True
            paths = [rel]
            if index_rel:
                paths.append(index_rel)
            return paths

        sha, pushed = await self._locked_commit(_mutate, f"workspace: release {slug}")
        if state["released"]:
            self._log_mutation("kb_release", [f"workspace/claims/{slug}.md"], sha, pushed)
        out: dict[str, Any] = {"path": p, "released": state["released"]}
        if state["note"]:
            out["note"] = state["note"]
        return out

    async def kb_claims(self) -> list[dict[str, Any]]:
        """List current claims (active first, then stale), so a session can see what other
        sessions are already working on. Returns [{path, session, note, claimed_at, age_min,
        stale}]."""
        await self._refresh()
        return await to_thread.run_sync(self._claims_sync)

    def _claims_sync(self) -> list[dict[str, Any]]:
        """Every claim record; age_min is minutes since claimed_at (None when unparseable),
        stale True past the TTL. Active (non-stale) first, then newest-claimed first."""
        cdir = self.root / "workspace" / "claims"
        out: list[dict[str, Any]] = []
        if not cdir.is_dir():
            return out
        now = _utcnow()
        for f in sorted(cdir.glob("*.md")):
            if f.name == "index.md":
                continue
            meta = read_meta(f)
            if str(meta.get("type") or "") != "claim":
                continue
            claimed_at = str(meta.get("claimed_at") or "")
            dt = _parse_ts(claimed_at)
            age_min = round((now - dt).total_seconds() / 60.0, 1) if dt is not None else None
            stale = age_min is None or age_min > _CLAIM_TTL_MIN
            out.append(
                {
                    "path": str(meta.get("path") or ""),
                    "session": str(meta.get("session") or ""),
                    "note": str(meta.get("note") or ""),
                    "claimed_at": claimed_at,
                    "age_min": age_min,
                    "stale": stale,
                }
            )
        out.sort(key=lambda c: str(c.get("claimed_at") or ""), reverse=True)
        out.sort(key=lambda c: bool(c.get("stale")))  # active (stale=False) first, stable
        return out

    def _claim_collision_warning(self, rel: str, writer: str) -> str | None:
        """Heads-up string when ``rel`` carries a live claim by a session other than
        ``writer`` (advisory — never blocks a write). None when clear. An empty writer means
        the caller didn't identify itself, so any live claim is flagged."""
        slug = self._claim_slug(rel)
        if not slug:
            return None
        f = self.root / "workspace" / "claims" / f"{slug}.md"
        if not f.is_file():
            return None
        meta = read_meta(f)
        if str(meta.get("type") or "") != "claim":
            return None
        dt = _parse_ts(str(meta.get("claimed_at") or ""))
        if dt is None:
            return None
        age = (_utcnow() - dt).total_seconds() / 60.0
        if age > _CLAIM_TTL_MIN:
            return None
        sess = str(meta.get("session") or "")
        wid = _slug(writer) if writer else ""
        if wid and sess == wid:
            return None
        return (
            f"heads up: {sess or 'another session'} claimed this path {int(round(age))}m ago — "
            "you may be colliding; coordinate in a room (kb_thread_post)."
        )

    # ------------------------------------------------------------------ importers

    async def kb_import(
        self, source: str, payload: str, dry_run: bool = True
    ) -> dict[str, Any]:
        """Backfill the brain from a ChatGPT/Claude export. dry_run (default) parses and
        returns proposals WITHOUT writing; dry_run=False files each proposal into
        inbox/imports/ as a type: imported-conversation concept (skipping paths that
        already exist), in one commit.

        Returns {source, proposed: [{path, title, timestamp, message_count, truncated}],
        imported: [paths], skipped: [paths]}."""
        src = (source or "").strip().lower()
        if src not in ("chatgpt", "claude"):
            raise KBError(
                f"Unknown import source {source!r} — supported sources are 'chatgpt' and 'claude'."
            )
        if not payload or not payload.strip():
            raise KBError(
                "Empty import payload — paste the exported conversations JSON "
                "(ChatGPT or Claude conversations.json)."
            )
        proposals = await to_thread.run_sync(lambda: parse_export(src, payload))
        proposed_summary = [
            {
                "path": p["suggested_path"],
                "title": p["title"],
                "timestamp": p["timestamp"],
                "message_count": p["message_count"],
                "truncated": p["truncated"],
            }
            for p in proposals
        ]
        if dry_run or not proposals:
            return {
                "source": src,
                "proposed": proposed_summary,
                "imported": [],
                "skipped": [],
            }

        state: dict[str, Any] = {"imported": [], "skipped": []}

        def _mutate() -> list[str]:
            paths: list[str] = []
            for p in proposals:
                _abs, rel = self._resolve(p["suggested_path"])
                if _abs.exists():
                    state["skipped"].append(rel)
                    continue
                description = f"Imported {p['source']} conversation ({p['message_count']} turns)."
                meta: dict[str, Any] = {
                    "type": "imported-conversation",
                    "title": p["title"],
                    "description": description,
                    "timestamp": p["timestamp"] or _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "source": p["source"],
                    "status": "untriaged",
                    "message_count": p["message_count"],
                }
                text = serialize(Doc(meta=meta, body=p["body"]))
                normalized, meta, _warnings = validate_concept(
                    text, rel_path=rel, description_arg=description
                )
                _write_text(_abs, normalized)
                paths.append(rel)
                paths.extend(
                    ensure_indexed(
                        self.root, rel, str(meta.get("title") or ""), str(meta.get("description") or "")
                    )
                )
                state["imported"].append(rel)
            return paths

        sha, pushed = await self._locked_commit(
            _mutate, f"kb: import {len(proposals)} conversations from {src}"
        )
        self._log_mutation("kb_import", state["imported"], sha, pushed)
        if state["imported"]:
            self._schedule_index(upserts=list(state["imported"]))
        return {
            "source": src,
            "proposed": proposed_summary,
            "imported": state["imported"],
            "skipped": state["skipped"],
            "sha": sha,
            "pushed": pushed,
        }

    # ------------------------------------------------------------------ artifacts

    async def kb_artifacts(self, project: str | None = None) -> list[dict[str, Any]]:
        """List saved artifacts (type: artifact concepts under projects/*/artifacts/) with
        provenance and staleness. Returns records sorted newest first."""
        await self._refresh()
        return await to_thread.run_sync(lambda: self._artifacts_sync(project))

    def _artifacts_sync(self, project: str | None) -> list[dict[str, Any]]:
        projects_dir = self.root / "projects"
        out: list[dict[str, Any]] = []
        if not projects_dir.is_dir():
            return out
        explorer = self.settings.explorer_url.rstrip("/")
        for pdir in sorted(projects_dir.iterdir()):
            if not pdir.is_dir() or pdir.name.startswith("."):
                continue
            if project is not None and pdir.name != project:
                continue
            adir = pdir / "artifacts"
            if not adir.is_dir():
                continue
            for f in sorted(adir.glob("*.md")):
                if f.name == "index.md":
                    continue
                meta = read_meta(f)
                raw_sources = meta.get("sources")
                sources = [str(s) for s in raw_sources] if isinstance(raw_sources, list) else []
                built_from = str(meta["built_from"]) if meta.get("built_from") else None
                token = meta.get("share")
                shared = bool(token)
                out.append(
                    {
                        "path": f.relative_to(self.root).as_posix(),
                        "project": pdir.name,
                        "title": str(meta.get("title") or f.stem),
                        "description": str(meta.get("description") or ""),
                        "timestamp": meta.get("timestamp"),
                        "format": str(meta.get("format") or "markdown"),
                        "sources": sources,
                        "built_from": built_from,
                        "stale": self._artifact_stale(built_from, sources),
                        "shared": shared,
                        "share_url": f"{explorer}/share/{token}" if shared else None,
                    }
                )
        out.sort(key=lambda a: str(a.get("timestamp") or ""), reverse=True)
        return out

    def _artifact_stale(self, built_from: str | None, sources: list[str]) -> bool | None:
        """True when any source changed since built_from; None when it can't be decided
        (no built_from/sources, an unknown sha, or git unavailable)."""
        if not built_from or not sources:
            return None
        try:
            changed = self.repo.run("log", "--oneline", f"{built_from}..HEAD", "--", *sources)
        except GitError:
            return None
        return bool(changed.strip())

    # -------------------------------------------------------------------- recipes

    async def kb_recipes(self, project: str | None = None) -> list[dict[str, Any]]:
        """List saved recipes (type: recipe concepts under projects/*/recipes/) — reusable
        build instructions (sources + instruction). Returns records sorted newest first."""
        await self._refresh()
        return await to_thread.run_sync(lambda: self._recipes_sync(project))

    def _recipes_sync(self, project: str | None) -> list[dict[str, Any]]:
        projects_dir = self.root / "projects"
        out: list[dict[str, Any]] = []
        if not projects_dir.is_dir():
            return out
        for pdir in sorted(projects_dir.iterdir()):
            if not pdir.is_dir() or pdir.name.startswith("."):
                continue
            if project is not None and pdir.name != project:
                continue
            rdir = pdir / "recipes"
            if not rdir.is_dir():
                continue
            for f in sorted(rdir.glob("*.md")):
                if f.name == "index.md":
                    continue
                meta = read_meta(f)  # failure-soft: returns {} on any unreadable/malformed file
                raw_sources = meta.get("sources")
                sources = [str(s) for s in raw_sources] if isinstance(raw_sources, list) else []
                out.append(
                    {
                        "path": f.relative_to(self.root).as_posix(),
                        "project": pdir.name,
                        "title": str(meta.get("title") or f.stem),
                        "description": str(meta.get("description") or ""),
                        "timestamp": meta.get("timestamp"),
                        "sources": sources,
                        "instruction": str(meta.get("instruction") or ""),
                    }
                )
        out.sort(key=lambda a: str(a.get("timestamp") or ""), reverse=True)
        return out

    async def kb_share_artifact(self, path: str, allow_secrets: bool = False) -> dict[str, Any]:
        """Mint (or return the existing) public share token for a type: artifact concept.
        Idempotent: an already-shared artifact returns its token with no new commit. Before
        minting, the body is scanned for likely secrets and sharing is REFUSED if any are
        found unless allow_secrets=True. Returns {path, share_url, sha, pushed}."""
        abs_path, rel = self._resolve(path)
        name = posixpath.basename(rel)
        state: dict[str, Any] = {}

        def _mutate() -> list[str]:
            meta, doc = self._load_artifact(abs_path, rel)
            existing = meta.get("share")
            if existing:
                state["token"] = str(existing)
                return []  # already shared — idempotent, no commit
            if not allow_secrets:
                findings = _scan_secrets(doc.body)
                if findings:
                    kinds = sorted({kind for kind, _ in findings})
                    lines = sorted({ln for _, ln in findings})
                    raise KBError(
                        f"Refusing to share {name}: its body contains what look like secrets "
                        f"({', '.join(kinds)}) on line(s) {', '.join(map(str, lines))}. A share "
                        "link is PUBLIC — remove the secret(s) with kb_edit/kb_write, or re-run "
                        "kb_share_artifact with allow_secrets=True to override deliberately."
                    )
            token = secrets.token_urlsafe(24)
            meta["share"] = token
            state["token"] = token
            _write_text(abs_path, serialize(Doc(meta=meta, body=doc.body)))
            return [rel]

        sha, pushed = await self._locked_commit(_mutate, f"kb: share {name}")
        explorer = self.settings.explorer_url.rstrip("/")
        return {
            "path": rel,
            "share_url": f"{explorer}/share/{state['token']}",
            "sha": sha,
            "pushed": pushed,
        }

    async def kb_unshare_artifact(self, path: str) -> dict[str, Any]:
        """Revoke a type: artifact concept's public share link (removes the share token).
        No-op when it was never shared. Returns {path, sha, pushed}."""
        abs_path, rel = self._resolve(path)
        name = posixpath.basename(rel)

        def _mutate() -> list[str]:
            meta, doc = self._load_artifact(abs_path, rel)
            if not meta.get("share"):
                return []  # nothing to revoke — no_change ok
            meta.pop("share", None)
            _write_text(abs_path, serialize(Doc(meta=meta, body=doc.body)))
            return [rel]

        sha, pushed = await self._locked_commit(_mutate, f"kb: unshare {name}")
        return {"path": rel, "sha": sha, "pushed": pushed}

    def _load_artifact(self, abs_path: Path, rel: str) -> tuple[dict[str, Any], Doc]:
        """Read + validate an existing type: artifact concept for a share mutation.
        Returns (normalized meta, Doc) or raises KBError teaching the fix."""
        if not abs_path.is_file():
            raise KBError(
                f"No such artifact: '{rel}'. Call kb_artifacts to list saved artifacts."
            )
        doc = split(_read_text_retry(abs_path))
        if doc is None:
            raise KBError(
                f"'{rel}' has no frontmatter — sharing only applies to 'type: artifact' concepts."
            )
        meta = normalize_meta(doc.meta)
        if str(meta.get("type") or "") != "artifact":
            raise KBError(
                f"'{rel}' has type {meta.get('type')!r} — only 'type: artifact' concepts can be "
                "shared. Save it as an artifact (frontmatter type: artifact) first."
            )
        return meta, doc

    # ------------------------------------------------------------------ visibility (M5)
    #
    # Who may see a concept. The rule, in priority order:
    #   1. the concept's own `visibility:` frontmatter, if declared
    #   2. else its project's context.md `visibility:` (the project default)
    #   3. else PRIVATE
    # Publishing is one-way — public content can be copied by anyone who saw it — so
    # visibility is only ever DECLARED, never inferred, and the default is always private.

    async def effective_visibility(self, path: str) -> str:
        """Resolve a concept's visibility: 'private' | 'contacts' | 'public'."""
        await self._refresh()
        _abs, rel = self._resolve(path)
        return await to_thread.run_sync(self._visibility_sync, rel)

    def _visibility_sync(self, rel: str) -> str:
        if _never_public(rel):
            return "private"
        abs_path = self.root / rel
        if abs_path.is_file():
            own = str(read_meta(abs_path).get("visibility") or "").strip().lower()
            if own in VISIBILITY_VALUES:
                return own
        # Inherit the project's default (the project root is the first 1-2 segments).
        proot = self._project_root_rel(rel)
        if proot:
            ctx = self.root / proot / "context.md"
            if ctx.is_file():
                default = str(read_meta(ctx).get("visibility") or "").strip().lower()
                if default in VISIBILITY_VALUES:
                    return default
        # Nothing marked anywhere -> the operator's policy default (v3 team test:
        # ENGRAM_DEFAULT_VISIBILITY=public for one consenting team). Never applies
        # to _never_public segments — those returned 'private' above.
        policy = str(self.settings.default_visibility or "").strip().lower()
        return policy if policy in VISIBILITY_VALUES else "private"

    def _project_root_rel(self, rel: str) -> str:
        """'projects/alt/decisions/x.md' -> 'projects/alt'; 'metalfinger/x.md' -> 'metalfinger'."""
        parts = [p for p in rel.split("/") if p]
        if not parts:
            return ""
        if parts[0] == "projects" and len(parts) > 1:
            return f"projects/{parts[1]}"
        return parts[0]

    async def kb_publish(self, path: str, visibility: str = "public") -> dict[str, Any]:
        """Set a concept's (or a project's, via its context.md) visibility.

        Publishing to 'public' secret-scans the body first and REFUSES if anything looks
        like a credential — public content is unrecallable. Returns
        {path, visibility, applies_to, sha, pushed}."""
        want = str(visibility or "").strip().lower()
        if want not in VISIBILITY_VALUES:
            raise KBError(
                f"visibility must be one of: {', '.join(VISIBILITY_VALUES)} (got {visibility!r})."
            )
        abs_path, rel = self._resolve(path)
        if _never_public(rel) and want != "private":
            raise KBError(
                f"'{rel}' can never be published — session mail, threads, workspace and inbox "
                "are private by nature, even inside a public project."
            )
        if not abs_path.is_file():
            raise KBError(f"No such concept: '{rel}'.")

        def _mutate() -> list[str]:
            doc = split(_read_text_retry(abs_path))
            if doc is None:
                raise KBError(f"'{rel}' has no frontmatter — cannot set visibility.")
            meta = normalize_meta(doc.meta)
            if want == "public":
                findings = _scan_secrets(doc.body)
                if findings:
                    raise KBError(_secret_refusal("concept", findings, "a different visibility"))
            if str(meta.get("visibility") or "").strip().lower() == want:
                return []  # already there — no_change
            if want == "private":
                meta.pop("visibility", None)  # absent == private; keep frontmatter clean
            else:
                meta["visibility"] = want
            _write_text(abs_path, serialize(Doc(meta=meta, body=doc.body)))
            return [rel]

        sha, pushed = await self._locked_commit(_mutate, f"kb: visibility {want} for {rel}")
        is_project = posixpath.basename(rel) == "context.md"
        if is_project:
            # A project-level flip changes the EFFECTIVE visibility of every concept
            # that inherits it, but only context.md's bytes changed — so re-stamp the
            # whole subtree's vector payloads now. Without this, an UNPUBLISH would
            # keep the project's concepts surfacing in cross-user search (titles/
            # descriptions) until the nightly reindex — a day-long leak window.
            proj_dir = (self.root / rel).parent
            subtree = [
                p.relative_to(self.root).as_posix()
                for p in proj_dir.rglob("*.md")
                if ".git" not in p.parts
            ]
            self._schedule_index(upserts=subtree)
        else:
            self._schedule_index(upserts=[rel])
        applies = "project (its concepts inherit this default)" if is_project else "concept"
        return {"path": rel, "visibility": want, "applies_to": applies, "sha": sha, "pushed": pushed}

    async def kb_move_project(self, project: str, folder: str = "") -> dict[str, Any]:
        """Move a project into a real folder directory (or back to the top level).

        `projects/<id>` <-> `projects/<folder>/<id>`. Folders are actual directories, so
        the bundle stays browsable in git and any file manager, and a project lives in
        exactly one place. The project ID does NOT change, so kb_load, `.engram-project`
        pins and the office keep working untouched.

        Every markdown link in the bundle is RE-EXPRESSED, not string-replaced: each
        relative target is resolved against its file's old location and rewritten relative
        to the new one. That's what keeps `../../library/...`-style links (which escape the
        project and therefore change depth) correct.

        Returns {project, folder, from, to, links_rewritten, sha, pushed}."""
        pid = (project or "").strip()
        dest_folder = (folder or "").strip().lower()
        if pid == "metalfinger":
            raise KBError("metalfinger is a fixed top-level tree and cannot be moved.")
        if dest_folder and not _PROJECT_ID_RE.fullmatch(dest_folder):
            raise KBError("Folder names are lowercase letters/digits/hyphens, e.g. 'alt-inc'.")
        old_rel = self._project_rel(pid)  # validates it exists
        new_rel = f"projects/{dest_folder}/{pid}" if dest_folder else f"projects/{pid}"
        if old_rel == new_rel:
            return {"project": pid, "folder": dest_folder, "from": old_rel, "to": new_rel,
                    "links_rewritten": 0, "sha": await to_thread.run_sync(self.repo.head_sha),
                    "pushed": True}
        if dest_folder == pid:
            raise KBError("A folder can't have the same name as the project inside it.")
        state = {"links": 0}

        def _mutate() -> list[str]:
            old_dir = self.root / old_rel
            new_dir = self.root / new_rel
            if new_dir.exists():
                raise KBError(f"{new_rel} already exists.")
            # Refuse to bury a project under something that is itself a project.
            if dest_folder:
                holder = self.root / "projects" / dest_folder
                if holder.is_dir() and (holder / "context.md").is_file():
                    raise KBError(
                        f"'{dest_folder}' is a project, not a folder — projects can't nest "
                        "inside other projects."
                    )
                holder.mkdir(parents=True, exist_ok=True)
            old_dir.rename(new_dir)
            touched_files = self._relink_after_move(old_rel, new_rel)
            state["links"] = len(touched_files)
            self._ensure_folder_indexes()
            # Every rewritten file must be staged, not just the moved tree: links live
            # in library/, self/, metalfinger/ too, and an unstaged rewrite leaves the
            # checkout dirty — which blocks EVERY later write until someone commits it.
            return ["projects", old_rel, new_rel, *touched_files]

        sha, pushed = await self._locked_commit(
            _mutate, f"kb: move {pid} -> {dest_folder or 'top level'}"
        )
        return {"project": pid, "folder": dest_folder, "from": old_rel, "to": new_rel,
                "links_rewritten": state["links"], "sha": sha, "pushed": pushed}

    def _relink_after_move(self, old_rel: str, new_rel: str) -> list[str]:
        """Rewrite every relative markdown link so it still points at the same concept.

        Resolve each target against the file's OLD directory, map it through the move,
        then re-express it relative to the file's NEW directory. Depth-correct by
        construction, which regex substitution is not.

        Returns the repo-relative paths of every file it changed, so the caller can
        stage them all."""
        touched: list[str] = []
        old_p, new_p = PurePosixPath(old_rel), PurePosixPath(new_rel)

        def _moved(path: PurePosixPath) -> PurePosixPath:
            """Map a bundle path through the move (identity outside the moved tree)."""
            if path == old_p or old_p in path.parents:
                return new_p / path.relative_to(old_p)
            return path

        for f in self.root.rglob("*.md"):
            if ".git" in f.parts:
                continue
            new_file_rel = PurePosixPath(f.relative_to(self.root).as_posix())
            # Where this file USED to live (it moved too if it's inside the tree).
            old_file_rel = new_file_rel
            if new_p == new_file_rel or new_p in new_file_rel.parents:
                old_file_rel = old_p / new_file_rel.relative_to(new_p)
            text = _read_text_retry(f)
            changed = False

            def _sub(m: "re.Match[str]") -> str:
                nonlocal changed
                target = m.group("target")
                if not target or target.startswith(("http://", "https://", "mailto:", "#", "/")):
                    return m.group(0)
                frag = ""
                if "#" in target:
                    target, frag = target.split("#", 1)
                    frag = "#" + frag
                if not target:
                    return m.group(0)
                try:
                    resolved = PurePosixPath(
                        posixpath.normpath(posixpath.join(str(old_file_rel.parent), target))
                    )
                except ValueError:
                    return m.group(0)
                if str(resolved).startswith(".."):
                    return m.group(0)  # already escapes the bundle; leave it alone
                new_target_abs = _moved(resolved)
                new_target = posixpath.relpath(str(new_target_abs), str(new_file_rel.parent))
                if new_target != target:
                    changed = True
                return f"]({new_target}{frag})"

            text2 = _MOVE_LINK_RE.sub(_sub, text)
            if changed and text2 != text:
                _write_text(f, text2)
                touched.append(str(new_file_rel))
        return touched

    def _ensure_folder_indexes(self) -> None:
        """Give every folder directory an index.md listing the projects inside it, and
        keep projects/index.md pointing at folders as well as loose projects."""
        pdir = self.root / "projects"
        if not pdir.is_dir():
            return
        for d in sorted(pdir.iterdir()):
            if not d.is_dir() or d.name.startswith(".") or not self._is_folder_dir(d):
                continue
            bullets = []
            for c in sorted(d.iterdir()):
                if not c.is_dir() or c.name.startswith("."):
                    continue
                meta = read_meta(c / "context.md")
                title = str(meta.get("title") or _title_case(c.name))
                desc = str(meta.get("description") or "")
                bullets.append(f"* [{title}]({c.name}/context.md) - {desc}")
            listing = "\n".join(bullets) if bullets else "Nothing here yet."
            _write_text(d / "index.md", f"# {_title_case(d.name)}\n\n{listing}\n")

    async def kb_tag_project(self, project: str, tags: list[str] | None = None,
                             status: str = "") -> dict[str, Any]:
        """Organize a project: set its grouping `tags` and/or `status` on context.md.

        Tags are how projects are grouped (client work, personal, archived…) — a project
        can carry several, which is why this is tags rather than folders: no nesting, no
        path churn, and one project can live in more than one group.
        Returns {project, tags, status, sha, pushed}."""
        rel = self._project_rel(project)  # validates the project exists
        ctx_rel = f"{rel}/context.md"
        abs_path = self.root / ctx_rel
        clean = [t.strip().lower() for t in (tags or []) if str(t).strip()]

        def _mutate() -> list[str]:
            doc = split(_read_text_retry(abs_path)) if abs_path.is_file() else None
            if doc is None:
                raise KBError(f"{project!r} has no context.md to organize.")
            meta = normalize_meta(doc.meta)
            if tags is not None:
                if clean:
                    meta["tags"] = clean
                else:
                    meta.pop("tags", None)
            if status:
                meta["status"] = status.strip().lower()
            _write_text(abs_path, serialize(Doc(meta=meta, body=doc.body)))
            return [ctx_rel]

        sha, pushed = await self._locked_commit(_mutate, f"kb: organize {project}")
        meta_now = read_meta(abs_path)
        return {
            "project": project, "tags": _as_tags(meta_now.get("tags")),
            "status": str(meta_now.get("status") or "active"), "sha": sha, "pushed": pushed,
        }

    async def ensure_project(self, project: str, description: str) -> dict[str, Any]:
        """Create a project's scaffold (context.md) if it doesn't exist yet. Idempotent —
        an existing project is returned untouched. Returns {project, created}."""
        pid = (project or "").strip().lower()
        if not _PROJECT_ID_RE.fullmatch(pid):
            raise KBError(
                f"Invalid project id {project!r} — use lowercase letters, digits and hyphens."
            )
        if (self.root / "projects" / pid).is_dir():
            return {"project": pid, "created": False}
        scaffold = (
            f"---\ntype: project\ntitle: {_title_case(pid)}\n"
            f"description: {description or pid}\nstatus: active\n---\n\n"
            "# About\n\n"
            f"{description or 'What this project is.'}\n\n"
            "# Current Phase\n\nJust started.\n\n"
            "# Open Loops\n\n- [ ] First step\n\n"
            "# Next Actions\n\n- [ ] Decide what to do first\n"
        )
        await self.kb_write(f"projects/{pid}/context.md", scaffold, f"feat: start {pid}")
        return {"project": pid, "created": True}

    async def kb_public(self) -> dict[str, Any]:
        """Audit: every concept currently visible beyond you, with its effective visibility.
        Returns {public: [...], contacts: [...]} of {path, title, description, type, project}."""
        await self._refresh()
        return await to_thread.run_sync(self._public_sync)

    def _public_sync(self) -> dict[str, Any]:
        out: dict[str, list[dict[str, Any]]] = {"public": [], "contacts": []}
        for abs_path in sorted(self.root.rglob("*.md")):
            rel = abs_path.relative_to(self.root).as_posix()
            if rel.startswith(".") or _never_public(rel):
                continue
            if posixpath.basename(rel) in ("index.md", "log.md"):
                continue
            vis = self._visibility_sync(rel)
            if vis not in ("public", "contacts"):
                continue
            meta = read_meta(abs_path)
            out[vis].append(
                {
                    "path": rel,
                    "title": str(meta.get("title") or posixpath.basename(rel)[:-3]),
                    "description": str(meta.get("description") or ""),
                    "type": str(meta.get("type") or ""),
                    "project": self._project_root_rel(rel).removeprefix("projects/"),
                    "updated": str(meta.get("timestamp") or ""),
                }
            )
        return out

    def _regen_messages_index(self, proot_rel: str) -> None:
        """Regenerate <project>/messages/index.md canonically (server-owned, no frontmatter)."""
        mdir = self.root / proot_rel / "messages"
        bullets: list[str] = []
        if mdir.is_dir():
            for f in sorted(mdir.glob("*.md")):
                if f.name == "index.md":
                    continue
                meta = read_meta(f)
                if str(meta.get("status") or "") != "unread":
                    continue
                title = str(meta.get("title") or f.stem)
                description = str(meta.get("description") or "")
                to = str(meta.get("to") or "any")
                priority = str(meta.get("priority") or "normal")
                bullets.append(f"* [{title}]({f.name}) - {description} (to: {to}, {priority})")
        listing = "\n".join(bullets) if bullets else "No unread messages."
        _write_text(
            mdir / "index.md",
            f"# Messages\n\n{listing}\n\nRead messages live in [archive/](archive/index.md).\n",
        )
