"""KBStore — async knowledge-base facade over the brain git checkout.

One instance owns ONE asyncio.Lock that serializes every mutation; reads are
lock-free against the checkout with a TTL-throttled pull that swallows git
failures (serve stale when GitHub is down). All git/file I/O runs in worker
threads via anyio.to_thread. Every tool-visible path is repo-relative POSIX.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
import secrets
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
from .semantic import SemanticIndex

log = logging.getLogger("engram.kbstore")

_PROJECT_ID_RE = re.compile(r"^[a-z0-9-]+$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_MD_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
        context_md = _read_text_retry(ctx_path) if ctx_path.is_file() else None

        if proot_rel == "metalfinger":
            parent_entries = _index_entries(self.root / "index.md")
        else:
            parent_entries = _index_entries(self.root / "projects" / "index.md")
        title, description = parent_entries.get(pid, (None, None))
        index_tree = self._tree_node(proot, proot_rel, title, description)

        log_path = proot / "log.md"
        recent_log = (
            _parse_log_entries(_read_text_retry(log_path))[:3]
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
            content = _read_text_retry(abs_path)
        except UnicodeDecodeError as exc:
            raise KBError(f"'{rel}' is not a UTF-8 text file — kb_read serves text only.") from exc
        result: dict[str, Any] = {"path": rel, "content": content, "meta": read_meta(abs_path)}
        if depth == 1:
            result["links"] = self._neighbor_links(rel, content)
            result["backlinks"] = self._backlinks(rel)
        return result

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
    ) -> list[dict[str, Any]]:
        """Rank concept/log files against query. Prefers the semantic (vector) engine when
        Qdrant is configured; on any failure or empty config falls back to the text scorer.
        Each result carries an additive `engine` field ('semantic' | 'text').

        Returns [{path, title, description, score, matched_heading, engine}]."""
        await self._refresh()
        if self.semantic is not None:
            index = self.semantic
            results = await to_thread.run_sync(
                lambda: index.search(query, project=project, type=type, limit=limit)
            )
            # None = backend failure; [] = empty/unpopulated collection. Either way the
            # text scorer is the better answer, so only serve semantic when it has hits.
            if results:
                for r in results:
                    r["engine"] = "semantic"
                return results
            log.info("semantic: no hits/unavailable, falling back to text engine")
        text_results = await to_thread.run_sync(
            lambda: _run_search(self.root, query, project=project, type_=type, limit=limit)
        )
        for r in text_results:
            r["engine"] = "text"
        return text_results

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
            sources = meta.get("sources")
            has_sources = isinstance(sources, list) and len(sources) > 0
            if (
                abs_path.name != "context.md"
                and not _REL_MD_LINK.search(normalized)
                and not (is_artifact and has_sources)
            ):
                warnings.append(
                    "This concept links to nothing — future sessions cannot navigate to "
                    "related knowledge from it (kb_read depth=1 will be empty). Add "
                    "relative markdown links to the concepts it relates to."
                )
            state["warnings"] = warnings
            state["meta"] = meta
            created = not abs_path.exists()
            state["created"] = created
            if not created and _read_text_retry(abs_path) == normalized:
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
        return {
            "path": rel,
            "created": state["created"] and not state["no_change"],
            "no_change": state["no_change"],
            "sha": sha,
            "pushed": pushed,
            "warnings": state["warnings"],
            "indexes_updated": state["indexes"],
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

    async def kb_share_artifact(self, path: str) -> dict[str, Any]:
        """Mint (or return the existing) public share token for a type: artifact concept.
        Idempotent: an already-shared artifact returns its token with no new commit.
        Returns {path, share_url, sha, pushed}."""
        abs_path, rel = self._resolve(path)
        name = posixpath.basename(rel)
        state: dict[str, Any] = {}

        def _mutate() -> list[str]:
            meta, doc = self._load_artifact(abs_path, rel)
            existing = meta.get("share")
            if existing:
                state["token"] = str(existing)
                return []  # already shared — idempotent, no commit
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
