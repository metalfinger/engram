"""Explorer routes: read-only, server-rendered transparency views over the brain checkout.

Every route sits behind the Cloudflare Access guard, renders through the shared
page shell (sticky topbar + persistent sidebar), and never leaves the checkout
root (path-traversal and .git guards preserved verbatim from v1).
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from anyio import to_thread
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from engram_server.errors import KBError
from engram_server.explorer.access import make_guard
from engram_server.explorer.format import (
    humanize_time,
    is_expired,
    properties_panel,
    score_bar,
    stamp,
)
from engram_server.explorer.html import badge, button, chip, codebox, esc, page
from engram_server.explorer.render import render_markdown, split_frontmatter
from engram_server.explorer.setup import render_setup_script
from engram_server.search import search as run_search

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request
    from starlette.responses import Response

    from engram_server.config import Settings

_LOG_DATE_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})", re.MULTILINE)
_SAFE_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Standard OKF project subfiles, in reading order, for the sidebar subtree.
_PROJECT_SUBFILES = (
    ("Context", "context.md"),
    ("Log", "log.md"),
    ("Messages", "messages/index.md"),
    ("Decisions", "decisions/index.md"),
    ("Specs", "specs/index.md"),
    ("People", "people/index.md"),
    ("Assets", "assets/index.md"),
)


# ---------------------------------------------------------------- fs helpers


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _rel(path: Path, brain: Path) -> str:
    return path.relative_to(brain).as_posix()


def _today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


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


def _session_time(date_str: str) -> str:
    """A relative-date badge for a YYYY-MM-DD session date ('today' for same day)."""
    if not date_str:
        return ""
    rel, exact = humanize_time(date_str)
    if rel == "just now":
        rel = "today"
    return f'<span class="badge" title="{esc(exact)}">{esc(rel)}</span>'


def _first_sentence(text: str | None) -> str:
    """The first sentence of a (possibly multi-line) description, trimmed."""
    text = (text or "").strip()
    if not text:
        return ""
    return re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]


# ---------------------------------------------------------------- view builders


def _crumbs_for(rel: str) -> list[tuple[str, str]]:
    crumbs = [("brain", "/brain")]
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


def _project_card(p: dict) -> str:
    dot = f'<span class="dot {esc(p["status"])}"></span>' if p["status"] else ""
    desc = f"<p>{esc(p['description'])}</p>" if p["description"] else ""
    foot: list[str] = []
    if p["unread"]:
        foot.append(badge(f"{p['unread']} unread", "unread"))
    if p["status"]:
        foot.append(badge(p["status"], p["status"]))
    if p["last_log"]:
        foot.append(_session_time(p["last_log"]))
    foot_html = f'<div class="card-foot">{"".join(foot)}</div>' if foot else ""
    return (
        f'<a class="card" href="/brain/p/{esc(p["id"])}">'
        f'<div class="card-head">{dot}<h3>{esc(p["title"])}</h3></div>'
        f"{desc}{foot_html}</a>"
    )


def _doc_cards(d: Path, brain: Path) -> str:
    """Cards for the concept .md files directly in ``d`` (index.md excluded)."""
    cards: list[str] = []
    if not d.is_dir():
        return ""
    for f in sorted(d.glob("*.md")):
        if f.name == "index.md":
            continue
        meta, _ = split_frontmatter(_read(f))
        title = str(meta.get("title") or f.stem)
        desc = str(meta.get("description") or "")
        head_stamp = stamp(meta.get("type"), small=True) if meta.get("type") else ""
        cards.append(
            f'<a class="card" href="/brain/f/{esc(_rel(f, brain))}">'
            f'<div class="card-head">{head_stamp}<h3>{esc(title)}</h3></div>'
            f"<p>{esc(desc)}</p></a>"
        )
    return "".join(cards)


def _msg_card(f: Path, meta: dict, brain: Path, today: dt.date) -> str:
    prio = str(meta.get("priority") or "normal").lower()
    expired = is_expired(meta.get("expires"), today)
    classes = f"msg prio-{esc(prio)}" + (" expired" if expired else "")
    rel = _rel(f, brain)
    title = esc(meta.get("title") or f.stem)
    desc = f"<p>{esc(meta.get('description') or '')}</p>" if meta.get("description") else ""
    chips: list[str] = []
    if meta.get("to"):
        chips.append(badge(f"to: {meta['to']}", "accent"))
    if prio and prio != "normal":
        chips.append(badge(f"priority: {prio}", "priority-high" if prio == "high" else ""))
    if meta.get("expires"):
        chips.append(badge(f"expires: {meta['expires']}", "superseded"))
    if expired:
        chips.append(badge("expired", "priority-high"))
    if meta.get("status"):
        chips.append(badge(str(meta["status"]), str(meta["status"])))
    foot = f'<div class="badges">{"".join(chips)}</div>' if chips else ""
    return (
        f'<div class="{classes}">'
        f'<h3><a href="/brain/f/{esc(rel)}">{title}</a></h3>{desc}{foot}</div>'
    )


def _timeline(entries: list[str], current_path: str) -> str:
    """Render log '## ' blocks as a dated timeline."""
    items: list[str] = []
    for entry in entries:
        first, _, rest = entry.partition("\n")
        head = first[3:].strip() if first.startswith("## ") else first.strip()
        if "—" in head:
            date, _sep, title = (s.strip() for s in head.partition("—"))
        else:
            m = _LOG_DATE_RE.match(first)
            date, title = (m.group(1) if m else ""), head
        body_html = render_markdown(rest.strip(), current_path) if rest.strip() else ""
        date_html = f'<span class="tl-date">{esc(date)}</span>' if date else ""
        items.append(
            f'<div class="tl-item">{date_html}<h3>{esc(title)}</h3>'
            f'<div class="tl-body">{body_html}</div></div>'
        )
    return '<div class="timeline">' + "".join(items) + "</div>"


def _result_card(r: dict) -> str:
    path = str(r["path"])
    seg_html = '<span class="sep">›</span>'.join(esc(s) for s in path.split("/"))
    desc = f"<p>{esc(r['description'])}</p>" if r.get("description") else ""
    heading = r.get("matched_heading")
    heading_html = (
        f'<div class="rheading">matched in <b>{esc(heading)}</b></div>' if heading else ""
    )
    return (
        f'<a class="result" href="/brain/f/{esc(path)}">'
        f'<h3>{esc(r["title"])}</h3>'
        f'<div class="rpath">{seg_html}</div>'
        f"{desc}{heading_html}{score_bar(r['score'])}</a>"
    )


# ---------------------------------------------------------------- setup / onboarding

# Cloudflare Access email allowlist for the explorer (shown on /brain/setup).
_ALLOWED_EMAILS = ("hir.012612@gmail.com", "hiren@metalfinger.xyz")


def _setup_card(glyph: str, heading: str, *inner: str, compact: bool = False) -> str:
    cls = "setup-card compact" if compact else "setup-card"
    return (
        f'<div class="{cls}">'
        f'<h2><span class="glyph" aria-hidden="true">{glyph}</span>{esc(heading)}</h2>'
        + "".join(inner)
        + "</div>"
    )


def _setup_body(
    mcp_url: str,
    explorer_url: str,
    explorer_host: str,
    gh_login: str,
    tool_names: list[str],
    allowed_emails: tuple[str, ...] | list[str],
) -> str:
    """Build the onboarding page body: one card per surface. Zero JavaScript.

    Code boxes are one-click-selectable via CSS ``user-select: all`` — nothing to
    copy-paste a script for. Pure/testable: the route feeds it live settings + the
    tool names from ``mcp.list_tools()``.
    """
    cc_cmd = f"claude mcp add --transport http --scope user engram {mcp_url}"
    codex_cmd = f"codex mcp add engram {mcp_url}"
    codex_toml = f'[mcp_servers.engram]\nurl = "{mcp_url}"'
    login_b = f"<b>{esc(gh_login)}</b>"

    cards = [
        _setup_card(
            "🌐",
            "claude.ai — web + mobile app",
            f'<p class="note">Add a custom connector, then sign in with GitHub as {login_b} — '
            "only allowlisted accounts pass.</p>",
            button("Open Connectors settings", "https://claude.ai/settings/connectors", new_tab=True),
            '<p class="note">Paste this MCP URL when prompted:</p>',
            codebox(mcp_url),
        ),
        _setup_card(
            "⌨",
            "Claude Code — any PC",
            '<p class="note">Run once, then in a session: <b>/mcp → engram → Authenticate</b> '
            "(GitHub sign-in; the token persists).</p>",
            codebox(cc_cmd),
            '<p class="note">Or run the installer — it registers the server and installs the '
            "engram skill:</p>",
            button("Download engram-setup.ps1", "/brain/setup/engram-setup.ps1"),
        ),
        _setup_card(
            "💬",
            "ChatGPT — web + apps",
            f'<p class="note">Settings → Connectors → enable <b>Developer mode</b> (Advanced settings) → '
            f"Add custom connector → paste the URL → sign in with GitHub as {login_b}.</p>",
            codebox(mcp_url),
            '<p class="note">Custom connectors need a paid plan with developer mode. The server '
            "already publishes the <code>/.well-known/oauth-protected-resource</code> discovery "
            "ChatGPT looks for.</p>",
        ),
        _setup_card(
            "📟",
            "Codex CLI — any PC",
            '<p class="note">Add the server, then authenticate when prompted (OAuth):</p>',
            codebox(codex_cmd),
            '<p class="note">On older Codex, add it to <code>~/.codex/config.toml</code> instead:</p>',
            codebox(codex_toml),
        ),
        _setup_card(
            "🔌",
            "Any MCP client",
            '<p class="note">Cursor, Gemini CLI, and other MCP clients: point them at the same URL — '
            "streamable HTTP + OAuth, only allowlisted GitHub accounts pass. The protocol travels in "
            "the tool descriptions, so every client behaves the same.</p>",
            codebox(mcp_url),
            compact=True,
        ),
        _setup_card(
            "🔖",
            "Browse — this site",
            f'<p class="note">This explorer lives at <b>{esc(explorer_host)}</b>, gated by Cloudflare '
            "Access with an email one-time PIN. Allowed:</p>",
            '<div class="quicknav">' + "".join(chip(e) for e in allowed_emails) + "</div>",
            codebox(explorer_url),
        ),
        _setup_card(
            "🛠",
            "What you get",
            f'<p class="note">{len(tool_names)} tools every connected session can call:</p>',
            '<div class="quicknav">'
            + "".join(chip(name, "/brain/system") for name in tool_names)
            + "</div>",
            '<p class="setup-more"><a href="/brain/system">Full reference &amp; protocol →</a></p>',
        ),
    ]

    return "\n".join(
        [
            '<div class="page-head">',
            stamp("runbook"),
            '<div><p class="eyebrow">Onboarding</p><h1>Connect a device</h1></div>',
            "</div>",
            '<p class="meta">One private brain, every surface. Pick your client below — '
            "Engram works with any remote-MCP client.</p>",
            f'<div class="setup-grid">{"".join(cards)}</div>',
        ]
    )


# ---------------------------------------------------------------- sidebar


def _nav_link(href: str, label: str, active: str, lead: str = "") -> str:
    cls = "nav-link active" if href == active else "nav-link"
    lead_html = f'<span class="lead">{esc(lead)}</span>' if lead else ""
    return f'<a class="{cls}" href="{esc(href)}">{lead_html}{esc(label)}</a>'


def _nav_section(label: str, inner_html: str) -> str:
    return f'<div class="nav-section"><p class="nav-label">{esc(label)}</p>{inner_html}</div>'


def _nav_files(d: Path, brain: Path, active: str) -> str:
    """Flat nav links for the concept .md files directly in ``d``."""
    links: list[str] = []
    if not d.is_dir():
        return ""
    for f in sorted(d.glob("*.md")):
        if f.name == "index.md":
            continue
        meta, _ = split_frontmatter(_read(f))
        title = str(meta.get("title") or f.stem)
        links.append(_nav_link(f"/brain/f/{_rel(f, brain)}", title, active))
    return "".join(links)


def _nav_project(pdir: Path, brain: Path, active: str) -> str:
    name = pdir.name
    rel_dir = _rel(pdir, brain)
    overview = f"/brain/p/{name}"
    unread = _unread_count(pdir)
    here = (
        active == overview
        or active == f"/brain/f/{rel_dir}"
        or active.startswith(f"/brain/f/{rel_dir}/")
    )
    open_attr = " open" if here else ""
    count = f'<span class="nav-count">{unread}</span>' if unread else ""
    children = [_nav_link(overview, "Overview", active)]
    for label, rel_name in _PROJECT_SUBFILES:
        fpath = pdir / rel_name
        if fpath.is_file():
            children.append(_nav_link(f"/brain/f/{_rel(fpath, brain)}", label, active))
    return (
        f'<details class="nav-proj"{open_attr}>'
        f"<summary>{esc(name)}{count}</summary>"
        f'<div class="nav-sub">{"".join(children)}</div>'
        "</details>"
    )


def _sidebar(brain: Path, active: str) -> str:
    """The persistent bundle tree: Projects, Self, Metalfinger, Library, Skills."""
    out: list[str] = []

    projects_dir = brain / "projects"
    proj_html: list[str] = []
    if projects_dir.is_dir():
        for pdir in sorted(projects_dir.iterdir()):
            if pdir.is_dir() and not pdir.name.startswith("."):
                proj_html.append(_nav_project(pdir, brain, active))
    if proj_html:
        out.append(_nav_section("Projects", "".join(proj_html)))

    self_html = _nav_files(brain / "self", brain, active)
    if self_html:
        out.append(_nav_section("Self", self_html))

    mf = brain / "metalfinger"
    if mf.is_dir():
        items = [_nav_link("/brain/p/metalfinger", "Overview", active)]
        if (mf / "log.md").is_file():
            items.append(_nav_link("/brain/f/metalfinger/log.md", "Log", active))
        if (mf / "videos" / "index.md").is_file():
            items.append(_nav_link("/brain/f/metalfinger/videos/index.md", "Videos", active))
        out.append(_nav_section("Metalfinger", "".join(items)))

    lib = brain / "library"
    if lib.is_dir():
        items = []
        for sub in ("runbooks", "snippets"):
            if (lib / sub / "index.md").is_file():
                items.append(
                    _nav_link(f"/brain/f/library/{sub}/index.md", sub.capitalize(), active)
                )
        if items:
            out.append(_nav_section("Library", "".join(items)))

    skills_items = [_nav_link("/brain/setup", "Setup", active)]
    if (brain / "skills" / "engram" / "SKILL.md").is_file():
        skills_items.append(
            _nav_link("/brain/f/skills/engram/SKILL.md", "Engram Protocol", active)
        )
    skills_items.append(_nav_link("/brain/system", "System & Tools", active))
    out.append(_nav_section("Skills", "".join(skills_items)))

    return "".join(out)


def _shell(brain: Path, title: str, body: str, crumbs, active: str, search_value: str = "") -> str:
    return page(
        title,
        body,
        crumbs,
        sidebar_html=_sidebar(brain, active),
        search_value=search_value,
    )


# ---------------------------------------------------------------- git


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
        summaries = _project_summaries(brain)
        projects = [p for p in summaries if p["id"] != "metalfinger"]
        total_unread = sum(p["unread"] for p in summaries)

        lede = "Hiren's cross-session knowledge base."
        idx = brain / "index.md"
        if idx.is_file():
            first = next((ln for ln in _read(idx).splitlines() if ln.strip()), "")
            line = first.lstrip("#").strip()
            if "—" in line:
                lede = line.partition("—")[2].strip() or lede

        stats = [chip(f"{len(projects)} projects")]
        if total_unread:
            stats.append(badge(f"{total_unread} unread", "unread"))
        body = [
            '<section class="masthead">',
            '<p class="eyebrow">OKF v0.1 · Knowledge base</p>',
            "<h1>brain</h1>",
            f'<p class="lede">{esc(lede)}</p>',
            f'<div class="stat-row">{"".join(stats)}</div>',
            "</section>",
        ]

        body.append('<p class="section-label">Projects</p>')
        body.append('<div class="cards">' + "".join(_project_card(p) for p in projects) + "</div>")

        for name, heading in (("self", "Self"), ("metalfinger", "Metalfinger"), ("library", "Library")):
            d = brain / name
            if not d.is_dir():
                continue
            if name == "metalfinger":
                cards = _doc_cards(d / "videos", brain)
                mf = next((p for p in summaries if p["id"] == "metalfinger"), None)
                lead_card = _project_card(mf) if mf else ""
                cards = lead_card + cards
            elif name == "library":
                cards = _doc_cards(d / "runbooks", brain) + _doc_cards(d / "snippets", brain)
            else:
                cards = _doc_cards(d, brain)
            if cards:
                body.append(f'<p class="section-label">{esc(heading)}</p>')
                body.append(f'<div class="cards">{cards}</div>')

        return HTMLResponse(
            _shell(brain, "brain", "\n".join(body), [("brain", "/brain")], "/brain")
        )

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

        meta: dict = {}
        ctx = pdir / "context.md"
        if ctx.is_file():
            meta, body_md = split_frontmatter(_read(ctx))
        title = str(meta.get("title") or name)
        parts: list[str] = [
            '<div class="page-head">',
            stamp(meta.get("type") or "project"),
            f"<div><p class=\"eyebrow\">Project</p><h1>{esc(title)}</h1></div>",
            "</div>",
        ]

        chips: list[str] = []
        if meta.get("status"):
            chips.append(badge(str(meta["status"]), str(meta["status"])))
        last = _last_log_date(pdir)
        if last:
            chips.append(f'<span class="meta">Last session</span>{_session_time(last)}')
        if chips:
            parts.append(f'<div class="badges">{"".join(chips)}</div>')

        if ctx.is_file():
            parts.append(f'<div class="md">{render_markdown(body_md, f"{rel_dir}/context.md")}</div>')
        else:
            parts.append('<p class="empty">No context.md yet.</p>')

        inbox = _inbox(pdir)
        unread = [(f, m) for f, m in inbox if m.get("status") == "unread"]
        parts.append('<p class="section-label">Inbox</p>')
        if inbox:
            for f, m in inbox:
                parts.append(_msg_card(f, m, brain, today))
        else:
            parts.append('<p class="empty">No messages waiting.</p>')

        archive_dir = pdir / "messages" / "archive"
        archived = (
            [f for f in sorted(archive_dir.glob("*.md")) if f.name != "index.md"]
            if archive_dir.is_dir()
            else []
        )
        if archived:
            links = "".join(
                f'<a class="nav-link" href="/brain/f/{esc(_rel(f, brain))}">{esc(f.name)}</a>'
                for f in archived
            )
            parts.append(
                f"<details><summary>Archive ({len(archived)})</summary>"
                f'<div class="nav-sub">{links}</div></details>'
            )

        entries = _log_entries(pdir / "log.md")
        parts.append('<p class="section-label">Recent sessions</p>')
        if entries:
            parts.append(_timeline(entries, f"{rel_dir}/log.md"))
        else:
            parts.append('<p class="empty">No sessions logged yet.</p>')

        quick = [
            (label, f"/brain/f/{rel_dir}/{sub}")
            for label, sub in (
                ("Log", "log.md"),
                ("Decisions", "decisions"),
                ("Specs", "specs"),
                ("People", "people"),
                ("Assets", "assets"),
            )
            if (pdir / sub).exists()
        ]
        if quick:
            parts.append('<p class="section-label">Browse</p>')
            parts.append(
                '<div class="quicknav">'
                + "".join(chip(label, href) for label, href in quick)
                + "</div>"
            )

        crumbs = [("brain", "/brain"), (name, f"/brain/p/{name}")]
        _ = unread  # unread count already surfaced via the sidebar badge
        return HTMLResponse(
            _shell(brain, title, "\n".join(parts), crumbs, f"/brain/p/{name}")
        )

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
            body_parts: list[str] = [f"<h1>{esc(title)}</h1>"]
            idx = target / "index.md"
            if idx.is_file():
                _meta, body_md = split_frontmatter(_read(idx))
                body_parts.append(
                    f'<div class="md">'
                    + render_markdown(body_md, f"{rel}/index.md" if rel else "index.md")
                    + "</div>"
                )
            entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            listing: list[str] = []
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                erel = entry.relative_to(root).as_posix()
                label = entry.name + ("/" if entry.is_dir() else "")
                listing.append(chip(label, f"/brain/f/{erel}"))
            listing_html = (
                f'<div class="quicknav">{"".join(listing)}</div>'
                if listing
                else '<p class="empty">Empty.</p>'
            )
            if idx.is_file():
                body_parts.append(
                    f"<details><summary>All files ({len(listing)})</summary>{listing_html}</details>"
                )
            else:
                body_parts.append('<p class="section-label">Contents</p>')
                body_parts.append(listing_html)
            active = f"/brain/f/{rel}" if rel else "/brain"
            return HTMLResponse(
                _shell(brain, title, "\n".join(body_parts), _crumbs_for(rel), active)
            )

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
        head_parts: list[str]
        if meta.get("type"):
            head_parts = [
                '<div class="page-head">',
                stamp(meta.get("type")),
                f'<div><p class="eyebrow">{esc(str(meta["type"]))}</p><h1>{esc(title)}</h1></div>',
                "</div>",
            ]
        else:
            head_parts = [f"<h1>{esc(title)}</h1>"]
        body_parts = [
            *head_parts,
            properties_panel(meta, _today_utc()),
            f'<div class="md">{render_markdown(body_md, rel)}</div>',
        ]
        return HTMLResponse(
            _shell(brain, title, "\n".join(body_parts), _crumbs_for(rel), f"/brain/f/{rel}")
        )

    @mcp.custom_route("/brain/search", ["GET"])
    @guard
    async def search_view(request: Request) -> Response:
        q = (request.query_params.get("q") or "").strip()
        project = (request.query_params.get("project") or "").strip() or None
        type_ = (request.query_params.get("type") or "").strip() or None

        parts: list[str] = ['<h1>Search</h1>']
        filters: list[str] = []
        if project:
            filters.append(f'<span class="meta">project</span>{chip(project, f"/brain/p/{esc(project)}")}')
        if type_:
            filters.append(f'<span class="meta">type</span>{badge(type_, "accent")}')
        if filters:
            parts.append(f'<div class="filters">{"".join(filters)}</div>')

        if not q:
            parts.append(
                '<p class="empty">Type a query in the search box above — it ranks titles, '
                "descriptions, tags, headings, and body across the whole bundle.</p>"
            )
        else:
            try:
                results = await to_thread.run_sync(run_search, brain, q, project, type_, 20)
            except KBError as exc:
                parts.append(f'<p class="meta">{esc(exc)}</p>')
                results = None
            if results is not None:
                if not results:
                    parts.append(f'<p class="empty">No matches for “{esc(q)}”.</p>')
                else:
                    plural = "result" if len(results) == 1 else "results"
                    parts.append(
                        f'<p class="meta">{len(results)} {plural} for “<b>{esc(q)}</b>”.</p>'
                    )
                    for r in results:
                        parts.append(_result_card(r))

        title = f"Search: {q}" if q else "Search"
        crumbs = [("brain", "/brain"), ("Search", "/brain/search")]
        return HTMLResponse(
            _shell(brain, title, "\n".join(parts), crumbs, "/brain/search", search_value=q)
        )

    @mcp.custom_route("/brain/setup", ["GET"])
    @guard
    async def setup_view(request: Request) -> Response:
        mcp_url = settings.public_url.rstrip("/") + "/mcp"
        explorer_url = settings.explorer_url.rstrip("/")
        explorer_host = urlsplit(settings.explorer_url).hostname or explorer_url
        logins = [x.strip() for x in settings.allowed_logins.split(",") if x.strip()]
        gh_login = logins[0] if logins else "your GitHub account"
        tools = await mcp.list_tools()
        body = _setup_body(
            mcp_url,
            explorer_url,
            explorer_host,
            gh_login,
            [t.name for t in tools],
            _ALLOWED_EMAILS,
        )
        crumbs = [("brain", "/brain"), ("Setup", "/brain/setup")]
        return HTMLResponse(_shell(brain, "Setup", body, crumbs, "/brain/setup"))

    @mcp.custom_route("/brain/setup/engram-setup.ps1", ["GET"])
    @guard
    async def setup_script(request: Request) -> Response:
        mcp_url = settings.public_url.rstrip("/") + "/mcp"
        return PlainTextResponse(
            render_setup_script(mcp_url),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="engram-setup.ps1"'},
        )

    @mcp.custom_route("/brain/system", ["GET"])
    @guard
    async def system_view(request: Request) -> Response:
        tools = await mcp.list_tools()
        parts = [
            "<h1>System</h1>",
            f'<p class="meta">{len(tools)} MCP tools exposed at <code>/mcp</code>. '
            "The tool descriptions are the protocol — claude.ai sessions carry no skill.</p>",
        ]
        for tool in tools:
            desc = (tool.description or "").strip()
            summary = _first_sentence(desc)
            parts.append('<div class="tool">')
            parts.append(f"<h3>{esc(tool.name)}</h3>")
            if summary:
                parts.append(f'<p class="summary">{esc(summary)}</p>')
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
            if desc:
                parts.append(
                    "<details><summary>Full description</summary>"
                    f'<pre class="desc">{esc(desc)}</pre></details>'
                )
            parts.append("</div>")

        parts.append('<p class="section-label">Protocol · SKILL.md</p>')
        skill = brain / "skills" / "engram" / "SKILL.md"
        if skill.is_file():
            _meta, body_md = split_frontmatter(_read(skill))
            parts.append(f'<div class="md">{render_markdown(body_md, "skills/engram/SKILL.md")}</div>')
        else:
            parts.append('<p class="empty">skills/engram/SKILL.md not found in the checkout.</p>')
        crumbs = [("brain", "/brain"), ("System", "/brain/system")]
        return HTMLResponse(
            _shell(brain, "System", "\n".join(parts), crumbs, "/brain/system")
        )

    @mcp.custom_route("/brain/activity", ["GET"])
    @guard
    async def activity_view(request: Request) -> Response:
        parts = ["<h1>Activity</h1>", '<p class="meta">Every commit to the brain repo, newest first.</p>']
        rows: list[tuple[str, str, str, str]] = []
        try:
            rows = await to_thread.run_sync(_git_log, brain, settings.git_timeout)
        except Exception as exc:  # noqa: BLE001 — surface, never 500
            parts.append(f'<p class="meta">git log unavailable: {esc(exc)}</p>')
        if rows:
            items: list[str] = []
            for sha, date, author, subject in rows:
                is_bot = author == settings.git_author_name
                author_html = badge(author, "bot") if is_bot else badge(author, "accent")
                rel, exact = humanize_time(date)
                items.append(
                    '<div class="tl-item">'
                    f'<span class="tl-date" title="{esc(exact)}">{esc(rel)}</span>'
                    f"<h3>{esc(subject)}</h3>"
                    f'<div class="tl-body meta"><code>{esc(sha)}</code> · {author_html}</div>'
                    "</div>"
                )
            parts.append('<div class="timeline">' + "".join(items) + "</div>")
        crumbs = [("brain", "/brain"), ("Activity", "/brain/activity")]
        return HTMLResponse(
            _shell(brain, "Activity", "\n".join(parts), crumbs, "/brain/activity")
        )

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
