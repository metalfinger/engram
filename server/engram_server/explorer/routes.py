"""Explorer routes: read-only, server-rendered transparency views over the brain checkout.

Every route sits behind the Cloudflare Access guard, renders through the shared
page shell (sticky topbar + persistent sidebar), and never leaves the checkout
root (path-traversal and .git guards preserved verbatim from v1).
"""

from __future__ import annotations

import datetime as dt
import posixpath
import re
import secrets
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from anyio import to_thread
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from engram_server.errors import KBError
from engram_server.explorer.access import make_guard
from engram_server.explorer.format import (
    TYPE_GLYPHS,
    humanize_time,
    is_expired,
    properties_panel,
    score_bar,
    stamp,
)
from engram_server.explorer.html import (
    badge,
    button,
    card,
    chip,
    codebox,
    esc,
    graph_page,
    page,
    share_page,
)
from engram_server.explorer.render import (
    render_markdown,
    render_markdown_public,
    split_frontmatter,
)
from engram_server.explorer.setup import render_setup_script
from engram_server.search import search as run_search

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request
    from starlette.responses import Response

    from engram_server.config import Settings

_LOG_DATE_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})", re.MULTILINE)
_SAFE_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_MONTH_PREFIX_RE = re.compile(r"^(\d{4}-\d{2})")

# The always-present OKF top-level files, in reading order (sidebar + project page).
_PROJECT_TOP_FILES = (
    ("Context", "context.md"),
    ("Log", "log.md"),
    ("Messages", "messages/index.md"),
)
# Ordering PREFERENCE only — project folders may take any shape; these known dirs
# sort first (in this order), everything else follows alphabetically.
_KNOWN_SECTION_ORDER = ("decisions", "specs", "assets", "people")


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


def _crumbs_for(rel: str, leaf_title: str | None = None) -> list[tuple[str, str]]:
    """Breadcrumbs for a repo-relative path. ``leaf_title`` humanizes the last
    crumb (frontmatter title instead of the raw ``slug.md``)."""
    crumbs = [("brain", "/brain")]
    parts = [p for p in rel.split("/") if p]
    acc = ""
    for i, part in enumerate(parts):
        acc = f"{acc}/{part}" if acc else part
        label = leaf_title if (leaf_title and i == len(parts) - 1) else part
        crumbs.append((label, f"/brain/f/{acc}"))
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


def _concept_month(f: Path, meta: dict) -> str:
    """A YYYY-MM label from a ``YYYY-MM-slug.md`` filename, else the timestamp."""
    m = _MONTH_PREFIX_RE.match(f.name)
    if m:
        return m.group(1)
    ts = str(meta.get("timestamp") or "")
    return ts[:7] if _MONTH_PREFIX_RE.match(ts) else ""


def _concept_card(f: Path, brain: Path) -> str:
    """A card for one concept file: type-stamp + title + description + badges
    (confidence colored settled=green / tentative=amber, and a YYYY-MM date)."""
    meta, _ = split_frontmatter(_read(f))
    rel = _rel(f, brain)
    title = str(meta.get("title") or f.stem)
    desc = str(meta.get("description") or "")
    head_stamp = stamp(meta.get("type"), small=True) if meta.get("type") else ""
    foot: list[str] = []
    conf = meta.get("confidence")
    if conf:
        foot.append(badge(str(conf), str(conf)))
    month = _concept_month(f, meta)
    if month:
        foot.append(badge(month, "date"))
    foot_html = f'<div class="card-foot">{"".join(foot)}</div>' if foot else ""
    desc_html = f"<p>{esc(desc)}</p>" if desc else ""
    return (
        f'<a class="card" href="/brain/f/{esc(rel)}" title="{esc(rel)}">'
        f'<div class="card-head">{head_stamp}<h3>{esc(title)}</h3></div>'
        f"{desc_html}{foot_html}</a>"
    )


def _doc_cards(d: Path, brain: Path) -> str:
    """Cards for the concept .md files directly in ``d`` (index.md excluded)."""
    if not d.is_dir():
        return ""
    return "".join(
        _concept_card(f, brain) for f in sorted(d.glob("*.md")) if f.name != "index.md"
    )


def _count_concepts(folder: Path) -> int:
    """Concept .md files directly in ``folder`` (index.md excluded)."""
    if not folder.is_dir():
        return 0
    return sum(1 for f in folder.glob("*.md") if f.name != "index.md")


def _count_sessions(pdir: Path) -> int:
    """Number of dated '## YYYY-MM-DD' session headings in the project log."""
    log = pdir / "log.md"
    if not log.is_file():
        return 0
    return len(_LOG_DATE_RE.findall(_read(log)))


def _dir_title(folder: Path) -> str:
    """Human label for a folder: its index.md H1 if present, else a Title-Cased name."""
    idx = folder / "index.md"
    if idx.is_file():
        first = next((ln for ln in _read(idx).splitlines() if ln.strip()), "")
        h1 = first.lstrip("#").strip()
        if h1:
            return h1
    return folder.name.replace("-", " ").replace("_", " ").title()


def _project_sections(pdir: Path) -> list[Path]:
    """Immediate concept subdirectories of a project, excluding messages/.

    Any folder shape is first-class: known dirs (decisions/specs/assets/people)
    keep their preferred order; everything else follows alphabetically. Emptiness
    is decided by the caller — this returns every candidate section dir.
    """
    subs = [
        d
        for d in pdir.iterdir()
        if d.is_dir() and not d.name.startswith(".") and d.name != "messages"
    ]

    def _key(d: Path) -> tuple[int, str]:
        name = d.name.lower()
        rank = _KNOWN_SECTION_ORDER.index(name) if name in _KNOWN_SECTION_ORDER else len(_KNOWN_SECTION_ORDER)
        return (rank, name)

    return sorted(subs, key=_key)


def _project_section_render(
    pdir: Path, brain: Path
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Generic, any-shape section rendering for a project.

    Every immediate subdir (except messages/) that holds at least one concept .md
    becomes an inline card section titled by its index.md H1 (fallback: Title-Cased
    dir name); an empty dir becomes a demoted muted line. Returns
    ``(stat_chips, section_html, browse_chips, demoted_lines)`` — the route wraps
    the always-present log/messages/session chrome around these.
    """
    stat: list[str] = []
    section_html: list[str] = []
    browse: list[str] = []
    demoted: list[str] = []
    for d in _project_sections(pdir):
        count = _count_concepts(d)
        label = _dir_title(d)
        href = f"/brain/f/{_rel(d, brain)}"
        browse.append(chip(f"{label} · {count}", href))
        if count:
            stat.append(chip(f"{count} {d.name}", href))
            section_html.append(f'<p class="section-label">{esc(label)}</p>')
            section_html.append(f'<div class="cards">{_doc_cards(d, brain)}</div>')
        else:
            demoted.append(f'<p class="empty">No {esc(label.lower())} yet.</p>')
    return stat, section_html, browse, demoted


def _file_title(f: Path) -> str:
    """Frontmatter title of a concept file, falling back to its filename stem."""
    try:
        meta, _ = split_frontmatter(_read(f))
    except (OSError, UnicodeDecodeError):
        return f.stem
    return str(meta.get("title") or f.stem)


def _siblings(target: Path) -> list[Path]:
    """Sorted concept .md files in the same folder (index.md excluded)."""
    return [f for f in sorted(target.parent.glob("*.md")) if f.name != "index.md"]


def _backlinks(brain: Path, target_rel: str) -> list[tuple[str, str]]:
    """Every non-index .md file that links to ``target_rel``, as (rel, title).

    Full scan of the (~35-file) bundle per call — cheap and always fresh. Only
    relative markdown links are resolved; external/anchor links are ignored.
    """
    out: list[tuple[str, str]] = []
    for path in sorted(brain.rglob("*.md")):
        rel = path.relative_to(brain).as_posix()
        if ".git" in rel.split("/") or path.name == "index.md" or rel == target_rel:
            continue
        # Provenance edges count: an artifact that lists target_rel as a source
        # references it as surely as a body link does.
        srcs = split_frontmatter(_read(path))[0].get("sources") if path.is_file() else None
        if isinstance(srcs, list) and target_rel in [str(s) for s in srcs]:
            out.append((rel, _file_title(path)))
            continue
        try:
            text = _read(path)
        except (OSError, UnicodeDecodeError):
            continue
        cur_dir = posixpath.dirname(rel)
        for m in _MD_LINK_RE.finditer(text):
            raw = m.group(1).split()[0] if m.group(1).split() else ""
            link = raw.split("#", 1)[0]
            if not link or link.startswith(("http://", "https://", "mailto:", "#")):
                continue
            joined = link.lstrip("/") if link.startswith("/") else posixpath.join(cur_dir, link)
            if posixpath.normpath(joined) == target_rel:
                out.append((rel, _file_title(path)))
                break
    return out


def _concept_footer(target: Path, rel: str, brain: Path) -> str:
    """Anti-dead-end footer: prev/next siblings, 'more in folder', backlinks, path."""
    blocks: list[str] = []

    sibs = _siblings(target)
    idx = next((i for i, f in enumerate(sibs) if f == target), None)
    if idx is not None and len(sibs) > 1:
        nav: list[str] = []
        if idx > 0:
            prev = sibs[idx - 1]
            nav.append(
                f'<a class="sib prev" href="/brain/f/{esc(_rel(prev, brain))}">'
                f'<span class="lbl">← Previous</span>'
                f'<span class="t">{esc(_file_title(prev))}</span></a>'
            )
        if idx < len(sibs) - 1:
            nxt = sibs[idx + 1]
            nav.append(
                f'<a class="sib next" href="/brain/f/{esc(_rel(nxt, brain))}">'
                f'<span class="lbl">Next →</span>'
                f'<span class="t">{esc(_file_title(nxt))}</span></a>'
            )
        if nav:
            blocks.append(f'<div class="siblingnav">{"".join(nav)}</div>')

    others = [f for f in sibs if f != target]
    if others:
        chips = "".join(
            chip(_file_title(f), f"/brain/f/{_rel(f, brain)}", title=_rel(f, brain))
            for f in others
        )
        blocks.append(
            '<div class="foot-block">'
            f'<p class="section-label">More in {esc(_dir_title(target.parent))}</p>'
            f'<div class="quicknav">{chips}</div></div>'
        )

    backlinks = _backlinks(brain, rel)
    inner = (
        "".join(chip(t, f"/brain/f/{p}", title=p) for p, t in backlinks)
        if backlinks
        else '<p class="empty">Nothing links here yet.</p>'
    )
    blocks.append(
        '<div class="foot-block">'
        '<p class="section-label">Referenced by</p>'
        f'<div class="backlinks">{inner}</div></div>'
    )

    blocks.append(
        f'<p class="pathline"><span class="k">Path</span><code>{esc(rel)}</code>'
        f'<a class="chip" href="/brain/f/{esc(rel)}?raw=1" title="raw markdown">raw</a></p>'
    )
    return f'<footer class="concept-foot">{"".join(blocks)}</footer>'


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
    """Render log '## ' blocks as a dated timeline.

    Handles BOTH log shapes: the old '## YYYY-MM-DD — Title' + paragraph (one
    entry per heading) and the new bare '## YYYY-MM-DD' with '* **Title** — body'
    bullets underneath (the heading carries only the date; the bullets are the body).
    """
    items: list[str] = []
    for entry in entries:
        first, _, rest = entry.partition("\n")
        head = first[3:].strip() if first.startswith("## ") else first.strip()
        m = _LOG_DATE_RE.match(first)
        date = m.group(1) if m else ""
        if "—" in head:
            title = head.partition("—")[2].strip()
        elif date and head == date:
            title = ""  # bare-date heading (new format): the bullets are the content
        else:
            title = head
        body_html = render_markdown(rest.strip(), current_path) if rest.strip() else ""
        date_html = f'<span class="tl-date">{esc(date or head)}</span>'
        title_html = f"<h3>{esc(title)}</h3>" if title else ""
        items.append(
            f'<div class="tl-item">{date_html}{title_html}'
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


# ---------------------------------------------------------------- artifacts


def _artifact_is_stale(
    brain: Path, built_from: str | None, sources: list[str], timeout: float
) -> bool | None:
    """True when any source changed since ``built_from``; None when undecidable.

    SYNC subprocess — callers run it in a worker thread. Mirrors KBStore._artifact_stale
    so the explorer and the tools agree on staleness.
    """
    if not built_from or not sources:
        return None
    try:
        proc = subprocess.run(
            ["git", "log", "--oneline", f"{built_from}..HEAD", "--", *sources],
            cwd=str(brain),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return bool(proc.stdout.strip())


def _artifact_provenance(meta: dict, stale: bool | None, explorer_url: str) -> str:
    """Provenance panel for an artifact concept page (guarded view): built_from sha,
    staleness badge, linked sources, and share status/URL."""
    raw_sources = meta.get("sources")
    sources = [str(s) for s in raw_sources] if isinstance(raw_sources, list) else []
    built_from = meta.get("built_from")
    row: list[str] = []
    if built_from:
        short = esc(str(built_from)[:10])
        row.append(f'<span class="chip" title="{esc(built_from)}">built from <code>{short}</code></span>')
    if stale is True:
        row.append(badge("sources changed since build", "priority-high"))
    elif stale is False:
        row.append(badge("current", "active"))
    parts = [
        '<div class="foot-block">',
        '<p class="section-label">Provenance</p>',
        f'<div class="badges">{"".join(row)}</div>' if row else "",
    ]
    if sources:
        chips = "".join(
            chip(posixpath.basename(s), f"/brain/f/{s}", title=s) for s in sources
        )
        parts.append(f'<p class="meta">Built from these sources:</p><div class="quicknav">{chips}</div>')
    share = meta.get("share")
    if share:
        url = f"{explorer_url.rstrip('/')}/share/{share}"
        parts.append(
            '<p class="meta">Shared publicly — anyone with this link can read it:</p>'
            f"{codebox(url)}"
        )
    else:
        parts.append('<p class="meta">Not shared. Use kb_share_artifact to create a public link.</p>')
    parts.append("</div>")
    return "".join(parts)


def _find_shared_artifact(brain: Path, token: str) -> tuple[Path, dict, str] | None:
    """Locate the shareable file whose frontmatter ``share`` equals ``token``.

    Lookup is by full scan of projects/*/artifacts/*.md and constant-time token
    comparison — never by any path derived from caller input (traversal-proof). No
    early return, so a hit late in the scan is not distinguishable by timing. Returns
    (path, meta, body) or None; the scan skips index.md and only accepts files whose
    type is ``artifact`` or ``live-view`` (a live data-bound status wall).
    """
    projects_dir = brain / "projects"
    if not projects_dir.is_dir():
        return None
    match: tuple[Path, dict, str] | None = None
    for pdir in sorted(projects_dir.iterdir()):
        adir = pdir / "artifacts"
        if not adir.is_dir():
            continue
        for f in sorted(adir.glob("*.md")):
            if f.name == "index.md":
                continue
            try:
                meta, body = split_frontmatter(_read(f))
            except (OSError, UnicodeDecodeError):
                continue
            if str(meta.get("type") or "") not in ("artifact", "live-view"):
                continue
            share = meta.get("share")
            if isinstance(share, str) and secrets.compare_digest(share, token):
                match = (f, meta, body)
    return match


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
    # Always-present top-level files, then every concept subdir (any shape).
    for label, rel_name in _PROJECT_TOP_FILES:
        fpath = pdir / rel_name
        if fpath.is_file():
            children.append(_nav_link(f"/brain/f/{_rel(fpath, brain)}", label, active))
    for d in _project_sections(pdir):
        idx = d / "index.md"
        target = idx if idx.is_file() else d
        children.append(_nav_link(f"/brain/f/{_rel(target, brain)}", _dir_title(d), active))
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

    proot = brain / "projects"
    if proot.is_dir() and any(
        f.name != "index.md"
        for p in sorted(proot.iterdir())
        if (p / "artifacts").is_dir()
        for f in (p / "artifacts").glob("*.md")
    ):
        out.append(
            _nav_section("Artifacts", _nav_link("/brain/artifacts", "Gallery", active))
        )

    skills_items = [_nav_link("/brain/setup", "Setup", active)]
    if (brain / "skills" / "engram" / "SKILL.md").is_file():
        skills_items.append(
            _nav_link("/brain/f/skills/engram/SKILL.md", "Engram Protocol", active)
        )
    skills_items.append(_nav_link("/brain/graph", "Graph", active))
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


# ---------------------------------------------------------------- graph


def _graph_group(rel: str) -> str:
    """The cluster a concept belongs to: the project name under projects/, else
    the first path segment (self, metalfinger, library, skills, ...)."""
    parts = rel.split("/")
    if parts[0] == "projects" and len(parts) > 1:
        return parts[1]
    return parts[0] if parts else ""


def _graph_data(brain: Path) -> dict:
    """Build the concept graph: every non-index .md is a node, every relative body
    link between two nodes is an edge.

    Nodes carry ``{id, title, type, project, links_out, links_in}``; ``id`` is the
    repo-relative POSIX path. Link resolution mirrors ``_backlinks`` (external/anchor
    links ignored, .git and index.md excluded). Pure — no git, no request state.
    """
    metas: dict[str, tuple[dict, str]] = {}
    for path in sorted(brain.rglob("*.md")):
        rel = path.relative_to(brain).as_posix()
        parts = rel.split("/")
        if ".git" in parts or path.name == "index.md":
            continue
        try:
            text = _read(path)
        except (OSError, UnicodeDecodeError):
            continue
        meta, _body = split_frontmatter(text)
        metas[rel] = (meta, text)

    node_ids = set(metas)
    edges: list[dict] = []
    out_count: dict[str, int] = {}
    in_count: dict[str, int] = {}
    for rel, (_meta, text) in metas.items():
        cur_dir = posixpath.dirname(rel)
        seen: set[str] = set()
        for m in _MD_LINK_RE.finditer(text):
            raw = m.group(1).split()[0] if m.group(1).split() else ""
            link = raw.split("#", 1)[0]
            if not link or link.startswith(("http://", "https://", "mailto:", "#")):
                continue
            joined = link.lstrip("/") if link.startswith("/") else posixpath.join(cur_dir, link)
            tgt = posixpath.normpath(joined)
            if tgt == rel or tgt not in node_ids or tgt in seen:
                continue
            seen.add(tgt)
            edges.append({"source": rel, "target": tgt})
            out_count[rel] = out_count.get(rel, 0) + 1
            in_count[tgt] = in_count.get(tgt, 0) + 1
        # Provenance edges: artifact -> each source it was built from.
        srcs = _meta.get("sources")
        if isinstance(srcs, list):
            for s in srcs:
                tgt = posixpath.normpath(str(s))
                if tgt == rel or tgt not in node_ids or tgt in seen:
                    continue
                seen.add(tgt)
                edges.append({"source": rel, "target": tgt, "kind": "sources"})
                out_count[rel] = out_count.get(rel, 0) + 1
                in_count[tgt] = in_count.get(tgt, 0) + 1

    nodes: list[dict] = []
    for rel in sorted(node_ids):
        meta, _text = metas[rel]
        nodes.append(
            {
                "id": rel,
                "title": str(meta.get("title") or Path(rel).stem),
                "type": str(meta.get("type") or ""),
                "project": _graph_group(rel),
                "links_out": out_count.get(rel, 0),
                "links_in": in_count.get(rel, 0),
            }
        )
    return {"nodes": nodes, "edges": edges}


def _git_head(brain: Path, timeout: float) -> str | None:
    """The current HEAD sha (for caching), or None when git/checkout is unavailable.

    SYNC subprocess — callers run it in a worker thread.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(brain),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


# ---------------------------------------------------------------- live status wall


def _current_phase_line(pdir: Path) -> str:
    """The first content line under a project context.md's 'Current Phase' section.

    Any heading level matches ('# Current Phase' / '## Current Phase'); the section
    ends at the next heading. Returns '' when the section or its content is absent.
    """
    ctx = pdir / "context.md"
    if not ctx.is_file():
        return ""
    _meta, body = split_frontmatter(_read(ctx))
    in_phase = False
    for line in body.split("\n"):
        s = line.strip()
        if s.startswith("#"):
            if in_phase:
                break
            in_phase = s.lstrip("#").strip().lower() == "current phase"
            continue
        if in_phase and s:
            return s
    return ""


def _render_status_wall(brain: Path, today: dt.date) -> str:
    """A public, link-free live status wall: one card per project with its current
    phase, unread count, last-log date, and decision/spec counts, plus a
    'rendered just now' footer stamped with the request time.

    Contains NO ``/brain/f`` links and NO source paths — safe for the public share
    page. Rendered server-side at request time, so it is always current.
    """
    cards: list[str] = []
    for p in _project_summaries(brain):
        pid = p["id"]
        pdir = brain / "metalfinger" if pid == "metalfinger" else brain / "projects" / pid
        phase = _current_phase_line(pdir)
        n_dec = _count_concepts(pdir / "decisions")
        n_spec = _count_concepts(pdir / "specs")
        foot: list[str] = []
        if p["status"]:
            foot.append(badge(str(p["status"]), str(p["status"])))
        if p["unread"]:
            foot.append(badge(f"{p['unread']} unread", "unread"))
        if p["last_log"]:
            foot.append(f'<span class="badge date">{esc(p["last_log"])}</span>')
        if n_dec:
            foot.append(chip(f"{n_dec} decisions"))
        if n_spec:
            foot.append(chip(f"{n_spec} specs"))
        phase_html = (
            f"<p>{esc(phase)}</p>" if phase else '<p class="empty">No current phase noted.</p>'
        )
        cards.append(
            '<div class="card">'
            f'<div class="card-head"><h3>{esc(p["title"])}</h3></div>'
            f"{phase_html}"
            f'<div class="card-foot">{"".join(foot)}</div>'
            "</div>"
        )
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f'<div class="cards">{"".join(cards)}</div>'
        '<footer class="concept-foot"><p class="meta">'
        f"live — rendered just now · {esc(now)}</p></footer>"
    )


# ---------------------------------------------------------------- routes


def _unfence_html(body: str) -> str:
    """The full HTML document from an html-format artifact body: either bare
    (starts with a doctype/tag) or wrapped in a single ```html fenced block."""
    text = body.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[-1].strip().startswith("```"):
            text = "\n".join(lines[1:-1])
        else:
            text = "\n".join(lines[1:])
    return text


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register all explorer GET routes, every one behind the Access guard."""
    guard = make_guard(settings)
    brain = settings.brain_path
    _graph_cache: dict[str, dict] = {}

    async def _graph_payload() -> dict:
        """The concept graph, cached by HEAD sha (rebuilt only when the bundle moves)."""
        sha = await to_thread.run_sync(_git_head, brain, settings.git_timeout)
        if sha and sha in _graph_cache:
            return _graph_cache[sha]
        data = await to_thread.run_sync(_graph_data, brain)
        if sha:
            _graph_cache.clear()
            _graph_cache[sha] = data
        return data

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
            f'<div><p class="eyebrow">Project</p><h1>{esc(title)}</h1></div>',
            "</div>",
        ]

        # Generic section discovery — every concept subdir is first-class.
        stat_sec, section_html, browse_sec, demoted = _project_section_render(pdir, brain)
        n_sess = _count_sessions(pdir)
        n_unread = _unread_count(pdir)

        # Aggregate stat row: status · last session · one chip per non-empty section.
        stat: list[str] = []
        if meta.get("status"):
            stat.append(badge(str(meta["status"]), str(meta["status"])))
        last = _last_log_date(pdir)
        if last:
            stat.append(_session_time(last))
        stat.extend(stat_sec)
        stat.append(chip(f"{n_sess} sessions", f"/brain/f/{rel_dir}/log.md"))
        if n_unread:
            stat.append(badge(f"{n_unread} unread", "unread"))
        parts.append(f'<div class="stat-row">{"".join(stat)}</div>')

        if ctx.is_file():
            parts.append(f'<div class="md">{render_markdown(body_md, f"{rel_dir}/context.md")}</div>')
        else:
            parts.append('<p class="empty">No context.md yet.</p>')

        inbox = _inbox(pdir)
        has_unread = n_unread > 0

        # Inbox sits on top ONLY when there is something unread to act on.
        if has_unread:
            parts.append('<p class="section-label">Inbox</p>')
            parts.extend(_msg_card(f, m, brain, today) for f, m in inbox)

        entries = _log_entries(pdir / "log.md")
        parts.append('<p class="section-label">Recent sessions</p>')
        if entries:
            parts.append(_timeline(entries, f"{rel_dir}/log.md"))
        else:
            parts.append('<p class="empty">No sessions logged yet.</p>')

        # A non-empty-but-all-read inbox drops below Recent sessions.
        if inbox and not has_unread:
            parts.append('<p class="section-label">Inbox</p>')
            parts.extend(_msg_card(f, m, brain, today) for f, m in inbox)

        # Inline card sections for every non-empty concept dir (any shape).
        parts.extend(section_html)

        archive_dir = pdir / "messages" / "archive"
        archived = (
            [f for f in sorted(archive_dir.glob("*.md")) if f.name != "index.md"]
            if archive_dir.is_dir()
            else []
        )
        if archived:
            links = "".join(
                f'<a class="nav-link" href="/brain/f/{esc(_rel(f, brain))}" '
                f'title="{esc(_rel(f, brain))}">{esc(_file_title(f))}</a>'
                for f in archived
            )
            parts.append(
                f"<details><summary>Archived messages ({len(archived)})</summary>"
                f'<div class="nav-sub">{links}</div></details>'
            )

        # Browse chips generated from the actual dirs, with counts.
        browse: list[str] = [chip(f"Log · {n_sess}", f"/brain/f/{rel_dir}/log.md")]
        browse.extend(browse_sec)
        if (pdir / "messages" / "index.md").is_file():
            browse.append(chip(f"Messages · {len(inbox)}", f"/brain/f/{rel_dir}/messages/index.md"))
        parts.append('<p class="section-label">Browse</p>')
        parts.append(f'<div class="quicknav">{"".join(browse)}</div>')

        if not inbox:
            demoted.insert(0, '<p class="empty">Inbox — no new messages.</p>')
        if demoted:
            parts.append(f'<div class="demoted">{"".join(demoted)}</div>')

        crumbs = [("brain", "/brain"), (name, f"/brain/p/{name}")]
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
                if entry.is_dir():
                    label = entry.name + "/"
                elif entry.suffix == ".md" and entry.name != "index.md":
                    label = _file_title(entry)  # human title, not the slug filename
                else:
                    label = entry.name
                listing.append(chip(label, f"/brain/f/{erel}", title=erel))
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
        if (
            str(meta.get("type") or "") == "live-view"
            and str(meta.get("view") or "") == "status-wall"
        ):
            wall = await to_thread.run_sync(_render_status_wall, brain, _today_utc())
            note = (
                '<div class="props"><div class="prop"><span class="prop-v plain">'
                "This is a live view — it renders current project status at request time. "
                "Share it (its frontmatter <code>share:</code> token) for a client-facing, "
                "always-current page.</span></div></div>"
            )
            leaf = title if meta.get("title") else None
            body_parts = [*head_parts, note, wall, _concept_footer(target, rel, brain)]
            return HTMLResponse(
                _shell(brain, title, "\n".join(body_parts), _crumbs_for(rel, leaf), f"/brain/f/{rel}")
            )
        provenance = ""
        if str(meta.get("type") or "") == "artifact":
            raw_sources = meta.get("sources")
            srcs = [str(s) for s in raw_sources] if isinstance(raw_sources, list) else []
            stale = await to_thread.run_sync(
                _artifact_is_stale, brain, meta.get("built_from"), srcs, settings.git_timeout
            )
            provenance = _artifact_provenance(meta, stale, settings.explorer_url)
        if str(meta.get("format") or "") == "html":
            # Don't inject a stored HTML document into the explorer's own DOM —
            # link to the rendered page (its share URL) and show source collapsed.
            share_tok = str(meta.get("share") or "")
            if share_tok:
                open_link = (
                    f'<p><a class="btn" href="/share/{esc(share_tok)}" target="_blank" '
                    'rel="noopener">Open rendered page ↗</a></p>'
                )
            else:
                open_link = (
                    '<p class="meta">This is an HTML artifact — share it '
                    "(kb_share_artifact) to get a rendered, sendable page.</p>"
                )
            src = _unfence_html(body_md)
            body_html = (
                open_link
                + f'<details><summary>HTML source ({len(src):,} chars)</summary>'
                + f'<div class="md"><pre><code>{esc(src[:20000])}</code></pre></div></details>'
            )
        else:
            body_html = f'<div class="md">{render_markdown(body_md, rel)}</div>'
        body_parts = [
            *head_parts,
            properties_panel(meta, _today_utc()),
            provenance,
            body_html,
            _concept_footer(target, rel, brain),
        ]
        leaf = title if meta.get("title") else None
        return HTMLResponse(
            _shell(brain, title, "\n".join(body_parts), _crumbs_for(rel, leaf), f"/brain/f/{rel}")
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

    @mcp.custom_route("/brain/artifacts", ["GET"])
    @guard
    async def artifacts_gallery(request: Request) -> Response:
        def collect() -> list[dict]:
            rows: list[dict] = []
            proot = brain / "projects"
            if not proot.is_dir():
                return rows
            for pdir in sorted(proot.iterdir()):
                adir = pdir / "artifacts"
                if not adir.is_dir():
                    continue
                for f in sorted(adir.glob("*.md")):
                    if f.name == "index.md":
                        continue
                    meta, _ = split_frontmatter(_read(f))
                    srcs = [str(s) for s in (meta.get("sources") or [])]
                    is_live = str(meta.get("type") or "") == "live-view"
                    rows.append(
                        {
                            "rel": f.relative_to(brain).as_posix(),
                            "project": pdir.name,
                            "title": str(meta.get("title") or f.stem),
                            "description": str(meta.get("description") or ""),
                            "timestamp": meta.get("timestamp"),
                            "n_sources": len(srcs),
                            "stale": None if is_live else _artifact_is_stale(
                                brain, meta.get("built_from"), srcs, settings.git_timeout
                            ),
                            "shared": bool(meta.get("share")),
                            "is_live": is_live,
                        }
                    )
            rows.sort(key=lambda r: str(r.get("timestamp") or ""), reverse=True)
            return rows

        rows = await to_thread.run_sync(collect)
        body: list[str] = [
            '<section class="masthead">',
            '<p class="eyebrow">Built from the brain</p>',
            "<h1>Artifacts</h1>",
            '<p class="lede">Documents generated from knowledge-base concepts — each one '
            "knows its sources, its build point, and whether those sources have moved on.</p>",
            "</section>",
        ]
        if not rows:
            body.append(
                '<p class="meta">No artifacts yet — build one from the Navigator&rsquo;s '
                "basket in any claude.ai chat, then save it to the brain.</p>"
            )
        else:
            cards = []
            for r in rows:
                badges = [chip(r["project"])]
                if r["is_live"]:
                    badges.append(badge("live", "active"))
                if r["stale"] is True:
                    badges.append(badge("sources changed", "unread"))
                elif r["stale"] is False:
                    badges.append(badge("current", "active"))
                if r["shared"]:
                    badges.append(badge("shared", "accent"))
                if r["timestamp"]:
                    rel_t, exact = humanize_time(r["timestamp"])
                    badges.append(f'<span class="meta" title="{esc(exact)}">{esc(rel_t)}</span>')
                if not r["is_live"]:
                    badges.append(f'<span class="meta">{r["n_sources"]} sources</span>')
                cards.append(
                    card(
                        f"/brain/f/{r['rel']}",
                        r["title"],
                        r["description"],
                        "".join(badges),
                    )
                )
            body.append('<div class="cards">' + "".join(cards) + "</div>")
        crumbs = [("brain", "/brain"), ("Artifacts", "/brain/artifacts")]
        return HTMLResponse(
            _shell(brain, "Artifacts", "".join(body), crumbs, "/brain/artifacts")
        )

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

    @mcp.custom_route("/brain/graph.json", ["GET"])
    @guard
    async def graph_json(request: Request) -> Response:
        return JSONResponse(await _graph_payload())

    @mcp.custom_route("/brain/graph", ["GET"])
    @guard
    async def graph_view(request: Request) -> Response:
        data = await _graph_payload()
        return HTMLResponse(graph_page(data, dict(TYPE_GLYPHS)))

    @mcp.custom_route("/share/{token}", ["GET"])
    async def share_view(request: Request) -> Response:
        # DELIBERATELY UNGUARDED — the ONLY content route without the Cloudflare Access
        # gate. It serves ONLY the rendered body of an explicitly-shared artifact: no
        # source paths, no nav, no sidebar, no other concepts. The token is matched by a
        # full scan + constant-time compare and is NEVER used to build a filesystem path
        # (traversal is impossible); tokens shorter than 20 chars are rejected outright.
        token = str(request.path_params.get("token", ""))
        if len(token) < 20:
            return PlainTextResponse("Not found", status_code=404)
        found = await to_thread.run_sync(_find_shared_artifact, brain, token)
        if found is None:
            return PlainTextResponse("Not found", status_code=404)
        _target, meta, body_md = found
        title = str(meta.get("title") or "Shared document")
        if str(meta.get("format") or "") == "html":
            # An HTML artifact IS the page (saved verbatim from a chat side panel).
            # Serve it raw, sandboxed: unique origin, so it can never read this
            # host's cookies/storage; inline script/style still run.
            html_doc = _unfence_html(body_md)
            return HTMLResponse(
                html_doc,
                headers={
                    "Content-Security-Policy": "sandbox allow-scripts allow-popups",
                    "X-Robots-Tag": "noindex",
                },
            )
        is_wall = (
            str(meta.get("type") or "") == "live-view"
            and str(meta.get("view") or "") == "status-wall"
        )
        eyebrow = "Live status" if is_wall else "Shared document"
        head = [
            '<div class="page-head">',
            stamp(meta.get("type") or "artifact"),
            f'<div><p class="eyebrow">{eyebrow}</p><h1>{esc(title)}</h1></div>',
            "</div>",
        ]
        if is_wall:
            wall = await to_thread.run_sync(_render_status_wall, brain, _today_utc())
            body = "".join(head) + wall
        else:
            ts = meta.get("timestamp")
            if ts:
                _reltime, exact = humanize_time(ts)
                head.append(f'<p class="meta">{esc(exact)}</p>')
            body = (
                "".join(head)
                + f'<div class="md">{render_markdown_public(body_md)}</div>'
                + '<footer class="concept-foot"><p class="meta">'
                "Shared from Hiren&rsquo;s knowledge base.</p></footer>"
            )
        return HTMLResponse(share_page(title, body, str(meta.get("description") or "")))

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
