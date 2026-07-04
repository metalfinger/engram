"""KBStore — async knowledge-base facade over the brain git checkout.

One instance owns ONE asyncio.Lock that serializes every mutation; reads are
lock-free against the checkout with a TTL-throttled pull that swallows git
failures (serve stale when GitHub is down). All git/file I/O runs in worker
threads via anyio.to_thread. Every tool-visible path is repo-relative POSIX.
"""

from __future__ import annotations

import asyncio
import posixpath
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from anyio import to_thread

from .config import Settings
from .errors import GitError, KBError
from .frontmatter import Doc, normalize_meta, read_meta, serialize, split, validate_concept
from .gitops import GitRepo
from .indexer import ensure_indexed
from .search import search as _run_search

_PROJECT_ID_RE = re.compile(r"^[a-z0-9-]+$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_MD_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
_BULLET_RE = re.compile(
    r"^\s*[*+-]\s+\[(?P<title>[^\]]*)\]\((?P<target>[^)\s]+)\)\s*(?:[-–—]\s*(?P<desc>.*))?\s*$"
)
_LOG_HEADING_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*(?:[—–-]+\s*)?(.*)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_VALID_TO = ("any", "claude-code", "mobile", "web")
_ARCHIVE_INDEX = "# Archive\n\nRead messages land here.\n"
_LINK_CAP = 50
_SLUG_MAX = 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
        text = index_path.read_text(encoding="utf-8")
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

    async def start(self) -> None:
        """Clone the brain if missing and enforce local git config. Idempotent."""
        await to_thread.run_sync(self.repo.ensure_clone)

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
        """'metalfinger' -> 'metalfinger'; anything else -> 'projects/<id>' (validated)."""
        pid = (project or "").strip()
        if pid == "metalfinger":
            rel = "metalfinger"
        elif _PROJECT_ID_RE.fullmatch(pid):
            rel = f"projects/{pid}"
        else:
            raise KBError(
                f"Invalid project id {project!r} — ids are lowercase letters/digits/hyphens. "
                "Call kb_projects to list valid projects."
            )
        if not (self.root / rel).is_dir():
            raise KBError(
                f"Unknown project {project!r}. Call kb_projects to list available projects."
            )
        return rel

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
            rel = "metalfinger" if pid == "metalfinger" else f"projects/{pid}"
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
                    "last_session": _first_log_date(pdir / "log.md"),
                    "unread_messages": self._count_unread(pdir / "messages"),
                }
            )
        return out

    def _project_ids(self) -> list[str]:
        pdir = self.root / "projects"
        ids = (
            sorted(d.name for d in pdir.iterdir() if d.is_dir() and not d.name.startswith("."))
            if pdir.is_dir()
            else []
        )
        return ids + ["metalfinger"]

    def _count_unread(self, messages_dir: Path) -> int:
        if not messages_dir.is_dir():
            return 0
        return sum(
            1
            for f in messages_dir.glob("*.md")
            if f.name != "index.md" and str(read_meta(f).get("status") or "") == "unread"
        )

    async def kb_load(self, project: str) -> dict[str, Any]:
        """Load a project's working context: {project, context_md, index_tree, recent_log,
        unread_messages, active_concepts}. No concept bodies — fetch those with kb_read."""
        await self._refresh()
        return await to_thread.run_sync(lambda: self._load_sync(project))

    def _load_sync(self, project: str) -> dict[str, Any]:
        proot_rel = self._project_rel(project)
        proot = self.root / proot_rel
        pid = proot_rel.split("/")[-1]

        ctx_path = proot / "context.md"
        context_md = ctx_path.read_text(encoding="utf-8") if ctx_path.is_file() else None

        if proot_rel == "metalfinger":
            parent_entries = _index_entries(self.root / "index.md")
        else:
            parent_entries = _index_entries(self.root / "projects" / "index.md")
        title, description = parent_entries.get(pid, (None, None))
        index_tree = self._tree_node(proot, proot_rel, title, description)

        log_path = proot / "log.md"
        recent_log = (
            _parse_log_entries(log_path.read_text(encoding="utf-8"))[:3]
            if log_path.is_file()
            else []
        )

        return {
            "project": pid,
            "context_md": context_md,
            "index_tree": index_tree,
            "recent_log": recent_log,
            "unread_messages": self._unread_messages(proot_rel),
            "active_concepts": self._active_concepts(proot, proot_rel),
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
                doc = split(f.read_text(encoding="utf-8"))
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
        """Read one file: {path, content, meta}. depth=1 adds links — the frontmatter of
        every relative .md link target (or {missing: true}), capped at 50."""
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
            content = abs_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise KBError(f"'{rel}' is not a UTF-8 text file — kb_read serves text only.") from exc
        result: dict[str, Any] = {"path": rel, "content": content, "meta": read_meta(abs_path)}
        if depth == 1:
            result["links"] = self._neighbor_links(rel, content)
        return result

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
    ) -> list[dict[str, Any]]:
        """Rank concept/log files against query: [{path, title, description, score, matched_heading}]."""
        await self._refresh()
        return await to_thread.run_sync(
            lambda: _run_search(self.root, query, project=project, type_=type, limit=limit)
        )

    # ------------------------------------------------------------------ writes

    async def kb_write(
        self, path: str, content: str, message: str, description: str = ""
    ) -> dict[str, Any]:
        """Create or update one concept file; on create the parent index chain is updated.
        Returns {path, created, no_change, sha, pushed, warnings, indexes_updated}."""
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
            state["warnings"] = warnings
            created = not abs_path.exists()
            state["created"] = created
            if not created and abs_path.read_text(encoding="utf-8") == normalized:
                state["no_change"] = True
                return []
            _write_text(abs_path, normalized)
            paths = [rel]
            if created:
                state["indexes"] = ensure_indexed(
                    self.root, rel, str(meta.get("title") or ""), str(meta.get("description") or "")
                )
                paths.extend(state["indexes"])
            return paths

        sha, pushed = await self._locked_commit(_mutate, f"kb: {message.strip()}")
        return {
            "path": rel,
            "created": state["created"] and not state["no_change"],
            "no_change": state["no_change"],
            "sha": sha,
            "pushed": pushed,
            "warnings": state["warnings"],
            "indexes_updated": state["indexes"],
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
                new_text = _insert_log_bullet(log_path.read_text(encoding="utf-8"), bullet, today)
            else:
                new_text = f"# Log — {pid}\n\n## {today}\n{bullet}\n"
            _write_text(log_path, new_text)
            return [rel]

        sha, pushed = await self._locked_commit(_mutate, f"log: {pid} {today}")
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
            doc = split(abs_path.read_text(encoding="utf-8"))
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
        return {"archived_path": state["archived"], "sha": sha, "pushed": pushed}

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
