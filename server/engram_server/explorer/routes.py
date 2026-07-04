"""Explorer routes: read-only, server-rendered transparency views over the brain checkout."""

from __future__ import annotations

import datetime as dt
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from anyio import to_thread
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from engram_server.explorer.access import make_guard
from engram_server.explorer.html import badge, card, esc, page
from engram_server.explorer.render import render_markdown, split_frontmatter

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request
    from starlette.responses import Response

    from engram_server.config import Settings

_LOG_DATE_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})", re.MULTILINE)
_SAFE_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# ---------------------------------------------------------------- helpers


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _rel(path: Path, brain: Path) -> str:
    return path.relative_to(brain).as_posix()


def _today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _is_expired(expires: object, today: dt.date) -> bool:
    """True when a message's ``expires`` value (date/datetime/str) is past."""
    if isinstance(expires, dt.datetime):
        return expires.date() < today
    if isinstance(expires, dt.date):
        return expires < today
    if isinstance(expires, str):
        try:
            return dt.date.fromisoformat(expires[:10]) < today
        except ValueError:
            return False
    return False


def _inbox(project_dir: Path) -> list[tuple[Path, dict]]:
    """Unarchived message files as (path, frontmatter), excluding index.md."""
    msg_dir = project_dir / "messages"
    if not msg_dir.is_dir():
        return []
    out: list[tuple[Path, dict]] = []
    for f in sorted(msg_dir.glob("*.md")):  # non-recursive: archive/ excluded
        if f.name == "index.md":
            continue
        meta, _body = split_frontmatter(_read(f))
        out.append((f, meta))
    return out


def _unread_count(project_dir: Path) -> int:
    return sum(1 for _, meta in _inbox(project_dir) if meta.get("status") == "unread")


def _last_log_date(project_dir: Path) -> str:
    log = project_dir / "log.md"
    if not log.is_file():
        return ""
    m = _LOG_DATE_RE.search(_read(log))
    return m.group(1) if m else ""


def _log_entries(log_path: Path, limit: int = 5) -> list[str]:
    """First ``limit`` '## ' blocks of log.md (newest first by convention)."""
    if not log_path.is_file():
        return []
    parts = re.split(r"(?m)^## ", _read(log_path))
    return ["## " + p.strip() for p in parts[1 : limit + 1]]


def _tree_html(dir_path: Path, brain: Path) -> str:
    """Nested link list (dirs + .md files) for the self/ and library/ trees."""
    items: list[str] = []
    entries = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    for entry in entries:
        if entry.name.startswith("."):
            continue
        rel = _rel(entry, brain)
        if entry.is_dir():
            items.append(
                f'<li><a href="/brain/f/{esc(rel)}">{esc(entry.name)}/</a>'
                f"{_tree_html(entry, brain)}</li>"
            )
        elif entry.suffix == ".md":
            items.append(f'<li><a href="/brain/f/{esc(rel)}">{esc(entry.name)}</a></li>')
    if not items:
        return ""
    return '<ul class="tree">' + "".join(items) + "</ul>"


def _meta_badges(meta: dict, today: dt.date) -> str:
    """Badge row for a concept's frontmatter (type/status/priority/expiry...)."""
    out: list[str] = []
    if meta.get("type"):
        out.append(badge(str(meta["type"])))
    status = str(meta.get("status") or "")
    if status:
        out.append(badge(status, status))
    conf = str(meta.get("confidence") or "")
    if conf:
        out.append(badge(conf, "superseded" if conf == "superseded" else ""))
    if meta.get("project"):
        out.append(badge(str(meta["project"])))
    if meta.get("to"):
        out.append(badge(f"to: {meta['to']}"))
    priority = str(meta.get("priority") or "")
    if priority:
        out.append(badge(f"priority: {priority}", "priority-high" if priority == "high" else ""))
    expires = meta.get("expires")
    if expires:
        out.append(badge(f"expires: {expires}"))
        if _is_expired(expires, today):
            out.append(badge("expired", "superseded"))
    if not out:
        return ""
    return '<div class="badges">' + "".join(out) + "</div>"


def _crumbs_for(rel: str) -> list[tuple[str, str]]:
    crumbs = [("Brain", "/brain")]
    acc = ""
    for part in [p for p in rel.split("/") if p]:
        acc = f"{acc}/{part}" if acc else part
        crumbs.append((part, f"/brain/f/{acc}"))
    return crumbs


def _project_summaries(brain: Path) -> list[dict]:
    """Card data for projects/* plus the metalfinger pseudo-project."""
    out: list[dict] = []
    projects_dir = brain / "projects"
    if projects_dir.is_dir():
        for pdir in sorted(projects_dir.iterdir()):
            if not pdir.is_dir() or pdir.name.startswith("."):
                continue
            meta: dict = {}
            ctx = pdir / "context.md"
            if ctx.is_file():
                meta, _ = split_frontmatter(_read(ctx))
            out.append(
                {
                    "id": pdir.name,
                    "title": str(meta.get("title") or pdir.name),
                    "description": str(meta.get("description") or ""),
                    "status": str(meta.get("status") or ""),
                    "unread": _unread_count(pdir),
                    "last_log": _last_log_date(pdir),
                }
            )
    mf = brain / "metalfinger"
    if mf.is_dir():
        title, desc = "metalfinger", ""
        idx = mf / "index.md"
        if idx.is_file():
            first = next((ln for ln in _read(idx).splitlines() if ln.strip()), "")
            line = first.lstrip("#").strip()
            if "—" in line:
                title, _sep, desc = (s.strip() for s in line.partition("—"))
            elif line:
                title = line
        out.append(
            {
                "id": "metalfinger",
                "title": title,
                "description": desc,
                "status": "active",
                "unread": _unread_count(mf),
                "last_log": _last_log_date(mf),
            }
        )
    return out


def _git_log(brain: Path, timeout: float) -> list[tuple[str, str, str, str]]:
    """Last 40 commits as (sha, date, author, subject).

    SYNC subprocess — callers run it in a worker thread (asyncio subprocesses
    break under the Windows selector event loop).
    """
    proc = subprocess.run(
        [
            "git",
            "log",
            "-n",
            "40",
            "--date=iso-strict",
            "--pretty=format:%h%x1f%ad%x1f%an%x1f%s%x1e",
        ],
        cwd=str(brain),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git log exited {proc.returncode}")
    rows: list[tuple[str, str, str, str]] = []
    for record in proc.stdout.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        fields = record.split("\x1f")
        if len(fields) == 4:
            rows.append((fields[0], fields[1], fields[2], fields[3]))
    return rows


# ---------------------------------------------------------------- routes


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register all explorer GET routes, every one behind the Access guard."""
    guard = make_guard(settings)
    brain = settings.brain_path

    @mcp.custom_route("/brain", ["GET"])
    @guard
    async def brain_overview(request: Request) -> Response:
        cards: list[str] = []
        for p in _project_summaries(brain):
            badges = ""
            if p["status"]:
                badges += badge(p["status"], p["status"])
            if p["unread"]:
                badges += badge(f"{p['unread']} unread", "unread")
            if p["last_log"]:
                badges += badge(f"log {p['last_log']}")
            cards.append(card(f"/brain/p/{p['id']}", p["title"], p["description"], badges))
        body = ["<h1>Projects</h1>", '<div class="cards">' + "".join(cards) + "</div>"]
        for name, heading in (("self", "Self"), ("library", "Library")):
            d = brain / name
            if d.is_dir():
                body.append(f"<h2>{esc(heading)}</h2>")
                body.append(_tree_html(d, brain) or '<p class="meta">Empty.</p>')
        return HTMLResponse(page("Brain", "\n".join(body), [("Brain", "/brain")]))

    @mcp.custom_route("/brain/p/{project}", ["GET"])
    @guard
    async def project_view(request: Request) -> Response:
        name = request.path_params["project"]
        if not _SAFE_PROJECT_RE.match(name):
            return PlainTextResponse("Not found", status_code=404)
        pdir = brain / "metalfinger" if name == "metalfinger" else brain / "projects" / name
        if not pdir.is_dir():
            return PlainTextResponse("Not found", status_code=404)
        rel_dir = _rel(pdir, brain)
        today = _today_utc()
        parts: list[str] = [f"<h1>{esc(name)}</h1>"]

        ctx = pdir / "context.md"
        if ctx.is_file():
            meta, body_md = split_frontmatter(_read(ctx))
            parts.append(_meta_badges(meta, today))
            parts.append(render_markdown(body_md, f"{rel_dir}/context.md"))
        else:
            parts.append('<p class="meta">No context.md.</p>')

        parts.append("<h2>Recent log</h2>")
        entries = _log_entries(pdir / "log.md")
        if entries:
            for entry in entries:
                parts.append(render_markdown(entry, f"{rel_dir}/log.md"))
        else:
            parts.append('<p class="meta">No log entries.</p>')

        parts.append("<h2>Messages</h2>")
        inbox = _inbox(pdir)
        if inbox:
            for f, meta in inbox:
                b = ""
                status = str(meta.get("status") or "")
                if status:
                    b += badge(status, status)
                if meta.get("to"):
                    b += badge(f"to: {meta['to']}")
                priority = str(meta.get("priority") or "")
                if priority:
                    b += badge(
                        f"priority: {priority}", "priority-high" if priority == "high" else ""
                    )
                expires = meta.get("expires")
                if expires:
                    b += badge(f"expires: {expires}")
                    if _is_expired(expires, today):
                        b += badge("expired", "superseded")
                rel = _rel(f, brain)
                parts.append(
                    '<div class="msg">'
                    f'<h3><a href="/brain/f/{esc(rel)}">{esc(meta.get("title") or f.stem)}</a></h3>'
                    f"<p>{esc(meta.get('description') or '')}</p>{b}</div>"
                )
        else:
            parts.append('<p class="meta">Inbox empty.</p>')

        archive_dir = pdir / "messages" / "archive"
        archived = (
            [f for f in sorted(archive_dir.glob("*.md")) if f.name != "index.md"]
            if archive_dir.is_dir()
            else []
        )
        if archived:
            links = "".join(
                f'<li><a href="/brain/f/{esc(_rel(f, brain))}">{esc(f.name)}</a></li>'
                for f in archived
            )
            parts.append(
                f"<details><summary>Archive ({len(archived)})</summary><ul>{links}</ul></details>"
            )

        quick = [d for d in ("decisions", "specs", "people", "assets") if (pdir / d).is_dir()]
        if quick:
            links = " · ".join(
                f'<a href="/brain/f/{esc(rel_dir)}/{d}">{d}/</a>' for d in quick
            )
            parts.append(f"<h2>Browse</h2><p>{links}</p>")

        crumbs = [("Brain", "/brain"), (name, f"/brain/p/{name}")]
        return HTMLResponse(page(name, "\n".join(parts), crumbs))

    @mcp.custom_route("/brain/f/{path:path}", ["GET"])
    @guard
    async def file_view(request: Request) -> Response:
        rel_param = str(request.path_params.get("path", "")).replace("\\", "/").strip("/")
        segments = [p for p in rel_param.split("/") if p]
        if any(p == ".." or p.lower() == ".git" for p in segments):
            return PlainTextResponse("Not found", status_code=404)
        root = brain.resolve()
        target = (root / rel_param).resolve() if rel_param else root
        if not target.is_relative_to(root):  # belt over the segment check above
            return PlainTextResponse("Not found", status_code=404)
        # Re-check RESOLVED parts: Windows resolve() expands 8.3 short names
        # ('GIT~1' -> '.git'), so the raw-segment check above misses them.
        if any(p.lower() == ".git" for p in target.relative_to(root).parts):
            return PlainTextResponse("Not found", status_code=404)

        if target.is_dir():
            rel = "" if target == root else target.relative_to(root).as_posix()
            title = f"/{rel}" if rel else "brain"
            body_parts: list[str] = []
            idx = target / "index.md"
            if idx.is_file():
                _meta, body_md = split_frontmatter(_read(idx))
                body_parts.append(
                    render_markdown(body_md, f"{rel}/index.md" if rel else "index.md")
                )
            body_parts.append("<h2>Contents</h2>")
            items: list[str] = []
            for entry in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
                if entry.name.startswith("."):
                    continue
                erel = entry.relative_to(root).as_posix()
                label = entry.name + ("/" if entry.is_dir() else "")
                items.append(f'<li><a href="/brain/f/{esc(erel)}">{esc(label)}</a></li>')
            body_parts.append(
                "<ul>" + "".join(items) + "</ul>" if items else '<p class="meta">Empty.</p>'
            )
            return HTMLResponse(page(title, "\n".join(body_parts), _crumbs_for(rel)))

        if not target.is_file():
            return PlainTextResponse("Not found", status_code=404)
        try:
            text = _read(target)
        except (UnicodeDecodeError, OSError):
            return PlainTextResponse("Not a readable text file", status_code=404)
        if request.query_params.get("raw") == "1":
            return PlainTextResponse(text)
        if target.suffix != ".md":
            return PlainTextResponse(text)

        rel = target.relative_to(root).as_posix()
        meta, body_md = split_frontmatter(text)
        title = str(meta.get("title") or target.name)
        body_parts = [
            f"<h1>{esc(title)}</h1>",
            _meta_badges(meta, _today_utc()),
            render_markdown(body_md, rel),
        ]
        return HTMLResponse(page(title, "\n".join(body_parts), _crumbs_for(rel)))

    @mcp.custom_route("/brain/system", ["GET"])
    @guard
    async def system_view(request: Request) -> Response:
        tools = await mcp.list_tools()
        parts = [
            "<h1>System</h1>",
            f'<p class="meta">{len(tools)} MCP tools exposed at /mcp.</p>',
        ]
        for tool in tools:
            parts.append(f"<h2><code>{esc(tool.name)}</code></h2>")
            schema = tool.inputSchema or {}
            props = schema.get("properties") or {}
            required = set(schema.get("required") or [])
            if props:
                rows: list[str] = []
                for arg, spec in props.items():
                    if not isinstance(spec, dict):
                        spec = {}
                    typ = spec.get("type") or " | ".join(
                        str(opt.get("type", "?")) for opt in spec.get("anyOf", [])
                    )
                    default = spec.get("default", "")
                    rows.append(
                        "<tr>"
                        f"<td><code>{esc(arg)}</code></td>"
                        f"<td>{esc(typ)}</td>"
                        f"<td>{'yes' if arg in required else 'no'}</td>"
                        f"<td>{esc('' if default is None else default)}</td>"
                        "</tr>"
                    )
                parts.append(
                    "<table><thead><tr><th>Argument</th><th>Type</th><th>Required</th>"
                    "<th>Default</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
                )
            else:
                parts.append('<p class="meta">No arguments.</p>')
            parts.append(f"<pre>{esc(tool.description or '')}</pre>")
        skill = brain / "skills" / "engram" / "SKILL.md"
        parts.append("<h1>Protocol (SKILL.md)</h1>")
        if skill.is_file():
            _meta, body_md = split_frontmatter(_read(skill))
            parts.append(render_markdown(body_md, "skills/engram/SKILL.md"))
        else:
            parts.append('<p class="meta">skills/engram/SKILL.md not found in the checkout.</p>')
        crumbs = [("Brain", "/brain"), ("System", "/brain/system")]
        return HTMLResponse(page("System", "\n".join(parts), crumbs))

    @mcp.custom_route("/brain/activity", ["GET"])
    @guard
    async def activity_view(request: Request) -> Response:
        parts = ["<h1>Activity</h1>"]
        rows: list[tuple[str, str, str, str]] = []
        try:
            rows = await to_thread.run_sync(_git_log, brain, settings.git_timeout)
        except Exception as exc:  # noqa: BLE001 — surface, never 500
            parts.append(f'<p class="meta">git log unavailable: {esc(exc)}</p>')
        if rows:
            trs: list[str] = []
            for sha, date, author, subject in rows:
                is_bot = author == settings.git_author_name
                author_html = badge(author, "bot") if is_bot else esc(author)
                row_cls = ' class="bot-row"' if is_bot else ""
                trs.append(
                    f"<tr{row_cls}><td><code>{esc(sha)}</code></td>"
                    f"<td>{esc(date.replace('T', ' '))}</td>"
                    f"<td>{author_html}</td><td>{esc(subject)}</td></tr>"
                )
            parts.append(
                "<table><thead><tr><th>Commit</th><th>Date</th><th>Author</th><th>Subject</th>"
                "</tr></thead><tbody>" + "".join(trs) + "</tbody></table>"
            )
        crumbs = [("Brain", "/brain"), ("Activity", "/brain/activity")]
        return HTMLResponse(page("Activity", "\n".join(parts), crumbs))

    @mcp.custom_route("/", ["GET"])
    @guard
    async def root_redirect(request: Request) -> Response:
        # UX only, never security: the Access guard already ran above. This just
        # sends explorer-host visitors to /brain instead of a bare 404.
        host = request.headers.get("host", "").split(":")[0].lower()
        expected = (urlsplit(settings.explorer_url).hostname or "").lower()
        if host and host == expected:
            return RedirectResponse("/brain")
        return PlainTextResponse("Not found", status_code=404)
