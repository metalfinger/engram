"""HTML shell, design tokens, and small presentational primitives.

Server-rendered, zero JavaScript: the mobile sidebar uses a checkbox+label
toggle and every collapsible is a native <details>. One warm copper accent,
hairline borders, a persistent bundle sidebar, and a 46rem reading measure —
a quiet archival reading room for the brain bundle.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape as _escape

CSS = """\
:root {
  --bg: #faf7f2; --surface: #ffffff; --surface-2: #f4efe6; --inset: #f7f2ea;
  --fg: #221f1a; --muted: #7a7266; --faint: #a89e8e;
  --line: #eae1d4; --line-2: #e0d6c4;
  --accent: #b5622b; --accent-ink: #9c4f1f; --accent-fg: #ffffff;
  --accent-soft: #f1e3d3; --accent-line: #e3c9ad;
  --green: #47804f; --blue: #3d6ba0; --violet: #7a58ab; --red: #b23a26; --amber: #97701f;
  --code-bg: #f4efe6; --shadow: 0 1px 2px rgba(60,40,20,.05);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16130d; --surface: #1e1a13; --surface-2: #241f16; --inset: #211c14;
    --fg: #ece6da; --muted: #a09784; --faint: #6d6453;
    --line: #2c261b; --line-2: #39321f;
    --accent: #d98a4e; --accent-ink: #e29a63; --accent-fg: #1a1409;
    --accent-soft: #33261722; --accent-line: #4a3820;
    --green: #6fb37f; --blue: #7ba3d6; --violet: #ad8fd8; --red: #e08a72; --amber: #cdac66;
    --code-bg: #241f16; --shadow: none;
  }
  :root { --accent-soft: rgba(217,138,78,.13); }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  font-size: 16px; line-height: 1.65; letter-spacing: -0.003em;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--accent-ink); text-decoration: none; }
a:hover { text-decoration: underline; text-underline-offset: 2px; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }

/* ---------- topbar ---------- */
.topbar {
  position: sticky; top: 0; z-index: 20; background: color-mix(in srgb, var(--bg) 88%, transparent);
  backdrop-filter: saturate(1.4) blur(8px); border-bottom: 1px solid var(--line);
}
.topbar-inner {
  max-width: 78rem; margin: 0 auto; padding: .55rem 1.5rem;
  display: flex; align-items: center; gap: 1rem;
}
.wordmark {
  font-weight: 700; font-size: 1.05rem; letter-spacing: -0.02em; color: var(--fg);
  display: inline-flex; align-items: center; gap: .45rem;
}
.wordmark:hover { text-decoration: none; color: var(--accent-ink); }
.wordmark .mark {
  width: 1.35rem; height: 1.35rem; border-radius: 6px; display: inline-grid; place-items: center;
  background: var(--accent-soft); color: var(--accent-ink); font-size: .85rem; border: 1px solid var(--accent-line);
}
.search { flex: 1 1 auto; max-width: 30rem; margin: 0 auto; }
.search input {
  width: 100%; padding: .42rem .8rem; font: inherit; font-size: .9rem; color: var(--fg);
  background: var(--surface); border: 1px solid var(--line-2); border-radius: 8px; outline: none;
}
.search input::placeholder { color: var(--faint); }
.search input:focus { border-color: var(--accent-line); box-shadow: 0 0 0 3px var(--accent-soft); }
.topnav { display: flex; gap: 1rem; align-items: center; flex: 0 0 auto; }
.topnav a { color: var(--muted); font-weight: 500; font-size: .9rem; }
.topnav a:hover { color: var(--accent-ink); text-decoration: none; }
.burger {
  display: none; cursor: pointer; font-size: 1.3rem; line-height: 1; color: var(--muted);
  padding: .1rem .3rem; user-select: none;
}
.navcb { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }

/* ---------- layout ---------- */
.layout { max-width: 78rem; margin: 0 auto; display: flex; gap: 2.5rem; padding: 0 1.5rem; }
.sidebar {
  flex: 0 0 15rem; align-self: flex-start; position: sticky; top: 3.3rem;
  max-height: calc(100vh - 3.6rem); overflow-y: auto; padding: 1.6rem 0 2rem;
}
main { flex: 1 1 auto; min-width: 0; max-width: 46rem; padding: 1.6rem 0 6rem; }

/* ---------- sidebar nav ---------- */
.nav-section { margin-bottom: 1.4rem; }
.nav-label {
  font-size: .68rem; font-weight: 700; letter-spacing: .12em; text-transform: uppercase;
  color: var(--faint); padding: 0 .2rem; margin: 0 0 .45rem;
}
.nav-link {
  display: flex; align-items: center; gap: .4rem; padding: .26rem .5rem; border-radius: 6px;
  color: var(--muted); font-size: .875rem; line-height: 1.35;
}
.nav-link:hover { background: var(--surface-2); color: var(--fg); text-decoration: none; }
.nav-link.active { background: var(--accent-soft); color: var(--accent-ink); font-weight: 600; }
.nav-link .lead { color: var(--faint); font-size: .78rem; width: .9rem; text-align: center; flex: 0 0 auto; }
.nav-proj > summary {
  display: flex; align-items: center; gap: .4rem; padding: .26rem .5rem; border-radius: 6px;
  cursor: pointer; color: var(--fg); font-size: .875rem; font-weight: 600; list-style: none;
}
.nav-proj > summary::-webkit-details-marker { display: none; }
.nav-proj > summary::before {
  content: "›"; color: var(--faint); font-weight: 400; transition: transform .12s ease;
  display: inline-block; width: .7rem;
}
.nav-proj[open] > summary::before { transform: rotate(90deg); }
.nav-proj > summary:hover { background: var(--surface-2); }
.nav-sub { margin: .1rem 0 .3rem .85rem; padding-left: .55rem; border-left: 1px solid var(--line); }
.nav-count {
  margin-left: auto; background: var(--accent); color: var(--accent-fg); font-size: .68rem;
  font-weight: 700; min-width: 1.15rem; height: 1.15rem; padding: 0 .35rem; border-radius: 999px;
  display: inline-grid; place-items: center;
}

/* ---------- breadcrumbs ---------- */
.crumbs { font-size: .8rem; color: var(--muted); margin: 0 0 1.4rem; display: flex; flex-wrap: wrap; gap: .1rem; }
.crumbs a { color: var(--muted); }
.crumbs a:hover { color: var(--accent-ink); }
.crumbs .sep { color: var(--faint); margin: 0 .35rem; }

/* ---------- page head + type stamp ---------- */
.page-head { display: flex; align-items: flex-start; gap: .85rem; margin: 0 0 .4rem; }
.stamp {
  flex: 0 0 auto; width: 2.35rem; height: 2.35rem; border-radius: 9px; display: grid; place-items: center;
  background: var(--accent-soft); border: 1px solid var(--accent-line); font-size: 1.25rem; margin-top: .15rem;
}
.eyebrow {
  font-size: .7rem; font-weight: 700; letter-spacing: .13em; text-transform: uppercase;
  color: var(--accent-ink); margin: 0 0 .4rem;
}
h1 { font-size: 1.95rem; line-height: 1.15; letter-spacing: -0.02em; margin: 0 0 .1rem; font-weight: 700; }
.page-head h1 { margin: 0; }
h2 { font-size: 1.3rem; line-height: 1.25; letter-spacing: -0.015em; margin: 2.2rem 0 .8rem; font-weight: 650; }
h3 { font-size: 1.05rem; margin: 1.6rem 0 .5rem; font-weight: 650; }
h4 { font-size: .92rem; margin: 1.3rem 0 .4rem; font-weight: 650; color: var(--muted); }

/* ---------- properties panel ---------- */
.props {
  border: 1px solid var(--line); border-radius: 10px; background: var(--surface);
  padding: .35rem .2rem; margin: 1.1rem 0 1.8rem; box-shadow: var(--shadow);
}
.prop { display: flex; gap: .8rem; padding: .3rem .85rem; align-items: baseline; }
.prop + .prop { border-top: 1px solid var(--line); }
.prop-k {
  flex: 0 0 6.5rem; font-size: .72rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
  color: var(--faint); padding-top: .12rem;
}
.prop-v { flex: 1 1 auto; min-width: 0; display: flex; flex-wrap: wrap; gap: .35rem; align-items: center; font-size: .9rem; }
.prop-v.plain { color: var(--fg); }
.statusv { display: inline-flex; align-items: center; gap: .4rem; text-transform: capitalize; }
.dot { width: .55rem; height: .55rem; border-radius: 50%; background: var(--faint); flex: 0 0 auto; }
.dot.active, .dot.settled, .dot.paid, .dot.published { background: var(--green); }
.dot.done, .dot.read { background: var(--blue); }
.dot.unread { background: var(--accent); }
.dot.tentative, .dot.superseded, .dot.archived, .dot.draft { background: var(--amber); }
.dot.blocked, .dot.expired { background: var(--red); }

/* ---------- pills / chips / badges ---------- */
.badge, .chip {
  display: inline-flex; align-items: center; gap: .3rem; font-size: .74rem; font-weight: 600;
  padding: .12rem .55rem; border-radius: 999px; white-space: nowrap; line-height: 1.5;
}
.badge { background: var(--surface-2); color: var(--muted); }
.chip { background: var(--inset); color: var(--muted); border: 1px solid var(--line); }
a.chip:hover { border-color: var(--accent-line); color: var(--accent-ink); text-decoration: none; }
.chip.tag::before { content: "#"; color: var(--faint); }
.badge.accent { background: var(--accent-soft); color: var(--accent-ink); }
.badge.unread { background: var(--accent); color: var(--accent-fg); }
.badge.active { background: transparent; color: var(--green); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--green) 40%, transparent); }
.badge.done { background: transparent; color: var(--blue); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--blue) 40%, transparent); }
.badge.superseded, .badge.archived, .badge.tentative { background: var(--surface-2); color: var(--amber); }
.badge.settled { background: transparent; color: var(--green); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--green) 40%, transparent); }
.badge.priority-high { background: color-mix(in srgb, var(--red) 15%, transparent); color: var(--red); }
.badge.bot { background: color-mix(in srgb, var(--violet) 16%, transparent); color: var(--violet); }
.badge.date { font-variant-numeric: tabular-nums; }
.badges { display: flex; flex-wrap: wrap; gap: .35rem; margin: .5rem 0 0; }

/* ---------- cards ---------- */
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(13.5rem, 1fr)); gap: .9rem; margin: 1.2rem 0 2rem; }
.card {
  display: flex; flex-direction: column; gap: .4rem; padding: 1rem 1.05rem; text-decoration: none;
  background: var(--surface); color: var(--fg); border: 1px solid var(--line); border-radius: 10px;
  box-shadow: var(--shadow); transition: border-color .12s ease, transform .12s ease;
}
.card:hover { border-color: var(--accent-line); text-decoration: none; transform: translateY(-1px); }
.card h3 { margin: 0; font-size: 1rem; font-weight: 650; letter-spacing: -0.01em; }
.card .card-head { display: flex; align-items: center; gap: .5rem; }
.card .card-head .dot { margin-top: .05rem; }
.card p { margin: 0; font-size: .85rem; color: var(--muted); line-height: 1.5; }
.card .card-foot { margin-top: auto; display: flex; flex-wrap: wrap; gap: .35rem; align-items: center; }
.card .stamp-sm {
  width: 1.6rem; height: 1.6rem; border-radius: 7px; font-size: .95rem; display: grid; place-items: center;
  background: var(--accent-soft); border: 1px solid var(--accent-line); flex: 0 0 auto;
}
.section-label {
  font-size: .72rem; font-weight: 700; letter-spacing: .12em; text-transform: uppercase;
  color: var(--faint); margin: 2.4rem 0 .3rem;
}

/* ---------- messages ---------- */
.msg {
  position: relative; border: 1px solid var(--line); border-radius: 10px; background: var(--surface);
  padding: .85rem 1rem .85rem 1.15rem; margin: .8rem 0; overflow: hidden; box-shadow: var(--shadow);
}
.msg::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; background: var(--faint); }
.msg.prio-high::before { background: var(--red); }
.msg.prio-normal::before { background: var(--accent); }
.msg.prio-low::before { background: var(--line-2); }
.msg.expired { opacity: .55; }
.msg h3 { margin: 0 0 .2rem; font-size: .98rem; font-weight: 650; }
.msg h3 a { color: var(--fg); }
.msg p { margin: .1rem 0 .5rem; color: var(--muted); font-size: .88rem; }

/* ---------- timeline ---------- */
.timeline { margin: 1rem 0 0; padding-left: 1.1rem; border-left: 1px solid var(--line); }
.tl-item { position: relative; padding: 0 0 1.2rem; }
.tl-item::before {
  content: ""; position: absolute; left: -1.42rem; top: .35rem; width: .55rem; height: .55rem;
  border-radius: 50%; background: var(--surface); border: 2px solid var(--accent-line);
}
.tl-date {
  display: inline-block; font-size: .72rem; font-weight: 700; letter-spacing: .04em; color: var(--accent-ink);
  font-family: ui-monospace, "Cascadia Code", Consolas, monospace; margin-bottom: .1rem;
}
.tl-item h3 { margin: .15rem 0 .3rem; font-size: .98rem; }
.tl-body p { margin: .3rem 0; font-size: .9rem; color: var(--muted); }

/* ---------- quick nav ---------- */
.quicknav { display: flex; flex-wrap: wrap; gap: .45rem; margin: .8rem 0 0; }

/* ---------- search results ---------- */
.filters { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; margin: 0 0 1.4rem; font-size: .85rem; color: var(--muted); }
.result {
  display: block; border: 1px solid var(--line); border-radius: 10px; background: var(--surface);
  padding: .85rem 1rem; margin: .7rem 0; text-decoration: none; color: var(--fg); box-shadow: var(--shadow);
}
.result:hover { border-color: var(--accent-line); text-decoration: none; }
.result h3 { margin: 0 0 .15rem; font-size: 1rem; font-weight: 650; }
.result .rpath { font-size: .74rem; color: var(--faint); font-family: ui-monospace, Consolas, monospace; }
.result .rpath .sep { margin: 0 .25rem; }
.result p { margin: .35rem 0 .5rem; font-size: .87rem; color: var(--muted); }
.result .rheading { font-size: .78rem; color: var(--muted); }
.result .rheading b { color: var(--accent-ink); font-weight: 600; }
.scorebar { display: flex; align-items: center; gap: .5rem; margin-top: .5rem; }
.scorebar .track { flex: 0 0 8rem; height: .35rem; border-radius: 999px; background: var(--surface-2); overflow: hidden; }
.scorebar .fill { height: 100%; background: var(--accent); border-radius: 999px; }
.scorebar .pct { font-size: .72rem; color: var(--faint); font-variant-numeric: tabular-nums; }

/* ---------- tools (system) ---------- */
.tool { border: 1px solid var(--line); border-radius: 10px; background: var(--surface); padding: 1rem 1.15rem; margin: .9rem 0; box-shadow: var(--shadow); }
.tool > h3 { margin: 0; font-size: 1rem; font-family: ui-monospace, Consolas, monospace; color: var(--accent-ink); }
.tool .summary { margin: .45rem 0 .2rem; color: var(--muted); font-size: .9rem; }
.tool details { margin: .7rem 0 0; }
.tool details summary { font-size: .8rem; color: var(--muted); }
.tool pre.desc { white-space: pre-wrap; font-family: inherit; font-size: .88rem; background: var(--inset); }

/* ---------- markdown body ---------- */
.md > :first-child { margin-top: 0; }
.md p, .md ul, .md ol { margin: .7rem 0; }
.md li { margin: .25rem 0; }
.md li.task { list-style: none; margin-left: -1.2rem; padding-left: 1.7rem; position: relative; }
.md li.task input { position: absolute; left: 0; top: .28rem; accent-color: var(--accent); }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: .88rem; display: block; overflow-x: auto; }
th, td { text-align: left; padding: .45rem .7rem; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: .78rem; letter-spacing: .03em; text-transform: uppercase; }
tr.bot-row td { background: color-mix(in srgb, var(--violet) 7%, transparent); }
pre, code { font-family: ui-monospace, "Cascadia Code", Consolas, monospace; font-size: .86em; }
pre { background: var(--code-bg); padding: .85rem 1rem; border-radius: 9px; overflow-x: auto; border: 1px solid var(--line); line-height: 1.55; }
code { background: var(--code-bg); padding: .1rem .35rem; border-radius: 5px; }
pre code { background: none; padding: 0; border: 0; }
blockquote { margin: 1.1rem 0; padding: .3rem 1rem; border-left: 3px solid var(--accent-line); color: var(--muted); background: var(--inset); border-radius: 0 6px 6px 0; }
hr { border: none; border-top: 1px solid var(--line); margin: 2rem 0; }
img { max-width: 100%; height: auto; border-radius: 8px; }
.meta { color: var(--muted); font-size: .88rem; }
.empty { color: var(--faint); font-size: .88rem; font-style: italic; }

/* ---------- concept footer (kill dead ends) ---------- */
.concept-foot { margin-top: 3rem; padding-top: 1.5rem; border-top: 1px solid var(--line); }
.siblingnav { display: flex; gap: .8rem; margin: 0 0 1.4rem; }
.sib {
  flex: 1 1 0; min-width: 0; border: 1px solid var(--line); border-radius: 9px; padding: .55rem .8rem;
  background: var(--surface); color: var(--fg); box-shadow: var(--shadow);
}
.sib:hover { border-color: var(--accent-line); text-decoration: none; }
.sib.next { text-align: right; }
.sib .lbl { display: block; font-size: .66rem; text-transform: uppercase; letter-spacing: .09em; color: var(--faint); margin-bottom: .1rem; }
.sib .t { font-size: .88rem; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: block; }
.foot-block { margin: 1.2rem 0; }
.backlinks { display: flex; flex-wrap: wrap; gap: .4rem; }
.pathline { margin: 1.6rem 0 0; font-size: .74rem; color: var(--faint); display: flex; align-items: center; gap: .4rem; }
.pathline .k { text-transform: uppercase; letter-spacing: .08em; }
.pathline code { -webkit-user-select: all; user-select: all; background: var(--code-bg); padding: .1rem .45rem; border-radius: 4px; color: var(--muted); font-size: .95em; }
.demoted { margin-top: 2.2rem; padding-top: 1rem; border-top: 1px dashed var(--line); }
.demoted .empty { margin: .15rem 0; }

/* ---------- setup / onboarding ---------- */
.setup-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(20rem, 1fr)); gap: 1rem; margin: 1.4rem 0; }
.setup-card { border: 1px solid var(--line); border-radius: 12px; background: var(--surface); padding: 1.2rem 1.3rem; box-shadow: var(--shadow); }
.setup-card h2 { margin: 0; font-size: 1.08rem; display: flex; align-items: center; gap: .5rem; }
.setup-card h2 .glyph { font-size: 1rem; }
.setup-card .note { color: var(--muted); font-size: .87rem; margin: .35rem 0 .7rem; line-height: 1.55; }
.setup-card .note b { color: var(--fg); font-weight: 600; }
.btn {
  display: inline-flex; align-items: center; gap: .4rem; background: var(--accent); color: var(--accent-fg);
  font-weight: 600; font-size: .9rem; padding: .5rem .95rem; border-radius: 8px; border: 1px solid var(--accent);
}
.btn:hover { text-decoration: none; filter: brightness(1.06); }
.btn::after { content: "↗"; font-size: .82em; opacity: .8; }
.codebox {
  border: 1px solid var(--line-2); border-radius: 8px; margin: .55rem 0; overflow-x: auto;
  background: var(--code-bg);
}
.codebox code {
  display: block; padding: .55rem .75rem; font-size: .8rem; white-space: pre; background: none;
  border: 0; color: var(--fg); cursor: pointer; -webkit-user-select: all; user-select: all;
}
.setup-more { margin-top: .8rem; font-size: .85rem; font-weight: 600; }
.setup-card .quicknav { margin-top: .6rem; }
.setup-card.compact { background: var(--inset); }
.setup-card.compact h2 { font-size: .98rem; }
.setup-card.compact .note { margin-bottom: .35rem; }

/* ---------- masthead (home) ---------- */
.masthead { margin: .5rem 0 2rem; }
.masthead h1 { font-size: 2.4rem; }
.masthead .lede { color: var(--muted); font-size: 1.02rem; margin: .5rem 0 0; max-width: 34rem; }
.stat-row { display: flex; flex-wrap: wrap; gap: .5rem; margin: 1rem 0 0; }

@media (max-width: 52rem) {
  .burger { display: inline-block; }
  .layout { display: block; padding: 0 1rem; }
  .topbar-inner { padding: .55rem 1rem; gap: .6rem; }
  .search { max-width: none; }
  .topnav { gap: .7rem; }
  .sidebar {
    display: none; position: fixed; top: 3.1rem; left: 0; bottom: 0; width: 17rem; z-index: 15;
    background: var(--surface); border-right: 1px solid var(--line); padding: 1.2rem 1rem 2rem;
    max-height: none; box-shadow: 0 8px 30px rgba(0,0,0,.18);
  }
  .navcb:checked ~ .layout .sidebar { display: block; }
  main { max-width: none; padding: 1.2rem 0 5rem; }
  .masthead h1 { font-size: 1.9rem; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
"""


def esc(s: object) -> str:
    """HTML-escape any value (including attribute quotes)."""
    return _escape(str(s), quote=True)


def badge(label: str, cls: str = "") -> str:
    """A pill badge; ``cls`` picks a color variant (unread, active, done, ...)."""
    classes = f"badge {cls}".strip()
    return f'<span class="{esc(classes)}">{esc(label)}</span>'


def chip(label: str, href: str = "", cls: str = "", title: str = "") -> str:
    """A small chip, optionally a link. ``cls`` e.g. 'tag' for #-prefixed tags.

    ``title`` sets the hover tooltip — used to keep the raw repo path visible
    (and copyable) even when the chip shows a human title.
    """
    classes = f"chip {cls}".strip()
    title_attr = f' title="{esc(title)}"' if title else ""
    if href:
        return f'<a class="{esc(classes)}" href="{esc(href)}"{title_attr}>{esc(label)}</a>'
    return f'<span class="{esc(classes)}"{title_attr}>{esc(label)}</span>'


def card(href: str, title: str, desc: str, badges_html: str = "") -> str:
    """A clickable grid card. ``badges_html`` is pre-built (trusted) HTML."""
    return (
        f'<a class="card" href="{esc(href)}">'
        f"<h3>{esc(title)}</h3>"
        f"<p>{esc(desc)}</p>"
        f'<div class="card-foot">{badges_html}</div>'
        "</a>"
    )


def button(label: str, href: str, *, new_tab: bool = False) -> str:
    """A primary (copper) call-to-action link styled as a button."""
    tgt = ' target="_blank" rel="noopener"' if new_tab else ""
    return f'<a class="btn" href="{esc(href)}"{tgt}>{esc(label)}</a>'


def codebox(text: str) -> str:
    """A monospace box whose contents select in one click (CSS ``user-select: all``).

    Zero JavaScript: the reader clicks once and copies. Multi-line text (e.g. a
    TOML snippet) keeps its line breaks.
    """
    return f'<div class="codebox"><code>{esc(text)}</code></div>'


def share_page(title: str, body_html: str) -> str:
    """A standalone, UNGUARDED public page for a shared artifact.

    Deliberately minimal: same visual language (shared CSS) but NO topbar, NO sidebar,
    NO search, NO breadcrumbs — nothing that reveals the bundle's structure or offers
    navigation into the private knowledge base. Only the artifact body and a neutral
    footer. ``body_html`` is trusted (server-built).
    """
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="layout">
  <main>
{body_html}
  </main>
</div>
</body>
</html>
"""


def page(
    title: str,
    body_html: str,
    crumbs: Sequence[tuple[str, str]] = (),
    *,
    sidebar_html: str = "",
    search_value: str = "",
) -> str:
    """Full HTML document: sticky topbar, persistent sidebar, then ``body_html``.

    ``sidebar_html`` and ``body_html`` are trusted (server-built). The mobile
    sidebar toggle and all collapsibles are pure CSS — zero JavaScript.
    """
    crumb_html = ""
    if crumbs:
        links = [f'<a href="{esc(href)}">{esc(label)}</a>' for label, href in crumbs]
        crumb_html = '<nav class="crumbs">' + '<span class="sep">/</span>'.join(links) + "</nav>"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} — brain</title>
<style>{CSS}</style>
</head>
<body>
<input type="checkbox" id="navcb" class="navcb" aria-hidden="true">
<header class="topbar">
  <div class="topbar-inner">
    <label for="navcb" class="burger" aria-label="Toggle navigation">≡</label>
    <a class="wordmark" href="/brain"><span class="mark">◗</span>brain</a>
    <form class="search" role="search" action="/brain/search" method="get">
      <input type="search" name="q" value="{esc(search_value)}"
        placeholder="Search the brain…" aria-label="Search the brain" autocomplete="off">
    </form>
    <nav class="topnav">
      <a href="/brain/system">System</a>
      <a href="/brain/activity">Activity</a>
    </nav>
  </div>
</header>
<div class="layout">
  <aside class="sidebar" aria-label="Bundle navigation">{sidebar_html}</aside>
  <main>
{crumb_html}
{body_html}
  </main>
</div>
</body>
</html>
"""
