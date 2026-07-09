"""HTML shell, design tokens, and small presentational primitives.

Server-rendered, zero JavaScript: the mobile sidebar uses a checkbox+label
toggle and every collapsible is a native <details>. One warm copper accent,
hairline borders, a persistent bundle sidebar, and a 46rem reading measure —
a quiet archival reading room for the brain bundle.
"""

from __future__ import annotations

import json
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
.badge.conf-superseded { background: var(--surface-2); color: var(--muted); text-decoration: line-through; text-decoration-color: var(--amber); }
.badge.settled { background: transparent; color: var(--green); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--green) 40%, transparent); }
.badge.priority-high { background: color-mix(in srgb, var(--red) 15%, transparent); color: var(--red); }
.badge.bot { background: color-mix(in srgb, var(--violet) 16%, transparent); color: var(--violet); }
.badge.date { font-variant-numeric: tabular-nums; }
.badges { display: flex; flex-wrap: wrap; gap: .35rem; margin: .5rem 0 0; }

/* ---------- supersession callout ---------- */
.supersede-banner {
  display: flex; align-items: baseline; gap: .5rem; margin: 1.1rem 0 1.4rem;
  padding: .6rem .85rem; border-radius: 9px; font-size: .9rem; font-weight: 550;
  color: var(--amber); border: 1px solid color-mix(in srgb, var(--amber) 42%, transparent);
  border-left: 3px solid var(--amber); background: color-mix(in srgb, var(--amber) 12%, transparent);
}
.supersede-banner .ss-glyph { flex: 0 0 auto; font-size: 1rem; }
.supersede-banner .ss-text { flex: 1 1 auto; }
.supersede-banner .ss-asof { color: var(--muted); font-weight: 500; white-space: nowrap; }
.supersede-banner a.chip { background: color-mix(in srgb, var(--amber) 16%, transparent); color: var(--amber); border-color: color-mix(in srgb, var(--amber) 35%, transparent); }
.supersede-banner a.chip:hover { border-color: var(--amber); color: var(--amber); }
.supersedes-line { font-size: .85rem; color: var(--muted); margin: .9rem 0 1.2rem; display: flex; flex-wrap: wrap; align-items: center; gap: .4rem; }

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

/* ---------- threads (live cross-session chat) ---------- */
.livebadge {
  display: inline-flex; align-items: center; gap: .4rem; font-size: .74rem; font-weight: 700;
  letter-spacing: .02em; padding: .14rem .6rem; border-radius: 999px; color: var(--green);
  background: color-mix(in srgb, var(--green) 14%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--green) 40%, transparent);
}
.livebadge .pulse {
  width: .5rem; height: .5rem; border-radius: 50%; background: var(--green); flex: 0 0 auto;
  animation: livepulse 1.4s ease-in-out infinite;
}
@keyframes livepulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: .35; transform: scale(.65); } }
.thread-transcript { display: flex; flex-direction: column; gap: .7rem; margin: 1.6rem 0 0; }
.bubble {
  max-width: 82%; border: 1px solid var(--line); border-radius: 12px; padding: .55rem .85rem;
  background: var(--surface); box-shadow: var(--shadow);
}
.bubble.left { align-self: flex-start; border-bottom-left-radius: 4px; }
.bubble.right { align-self: flex-end; border-bottom-right-radius: 4px; }
.bubble .bhead { display: flex; align-items: baseline; gap: .5rem; margin-bottom: .2rem; }
.bubble .bsender { font-weight: 700; font-size: .8rem; letter-spacing: -0.01em; }
.bubble .btime { font-size: .7rem; color: var(--faint); font-variant-numeric: tabular-nums; }
.bubble .bbody { font-size: .92rem; }
.bubble .bbody > :first-child { margin-top: 0; }
.bubble .bbody > :last-child { margin-bottom: 0; }
.bubble.c0 { background: color-mix(in srgb, var(--accent) 7%, var(--surface)); }
.bubble.c0 .bsender { color: var(--accent-ink); }
.bubble.c1 { background: color-mix(in srgb, var(--blue) 8%, var(--surface)); }
.bubble.c1 .bsender { color: var(--blue); }
.bubble.c2 { background: color-mix(in srgb, var(--violet) 9%, var(--surface)); }
.bubble.c2 .bsender { color: var(--violet); }
.bubble.c3 { background: color-mix(in srgb, var(--green) 9%, var(--surface)); }
.bubble.c3 .bsender { color: var(--green); }
/* shared refs row under a message bubble */
.bubble .brefs { display: flex; flex-wrap: wrap; gap: .35rem; margin-top: .45rem; padding-top: .4rem; border-top: 1px solid var(--line); }
.bubble .brefs .rlabel { font-size: .66rem; text-transform: uppercase; letter-spacing: .08em; color: var(--faint); align-self: center; font-weight: 700; }

/* ---------- workspace (live mission control) ---------- */
.topnav a.nav-workspace { color: var(--accent-ink); font-weight: 700; }
.frontdoor {
  display: flex; align-items: center; gap: .7rem; margin: 1.4rem 0 0; padding: .7rem 1rem;
  border: 1px solid var(--accent-line); background: var(--accent-soft); border-radius: 12px;
  color: var(--fg); font-weight: 600; box-shadow: var(--shadow);
}
.frontdoor:hover { text-decoration: none; border-color: var(--accent); }
.frontdoor .fd-arrow { margin-left: auto; color: var(--accent-ink); font-size: 1.15rem; }
/* status dots — semantic to the workspace roster (working/blocked/idle/available/done) */
.dot.st-working { background: var(--green); }
.dot.st-blocked { background: var(--red); }
.dot.st-idle, .dot.st-available { background: var(--amber); }
.dot.st-done { background: var(--faint); }
.wcard { position: relative; }
.wcard .card-head { align-items: center; }
.wcard .wstatus { margin-left: auto; font-size: .72rem; font-weight: 600; color: var(--muted); text-transform: capitalize; }
.wcard.stale { opacity: .6; }
.wsrow { display: flex; flex-wrap: wrap; gap: .4rem; align-items: center; margin: .45rem 0 0; }
.chip.repo { font-family: ui-monospace, Consolas, monospace; }
.wcwd { margin: .4rem 0 0; font-size: .74rem; color: var(--faint); font-family: ui-monospace, Consolas, monospace; word-break: break-all; }
.handoff {
  display: block; border: 1px solid var(--line); border-radius: 9px; background: var(--surface);
  padding: .6rem .9rem; margin: .55rem 0; box-shadow: var(--shadow); color: var(--fg);
}
.handoff:hover { border-color: var(--accent-line); text-decoration: none; }
.handoff .hhead { display: flex; align-items: baseline; flex-wrap: wrap; gap: .3rem; }
.handoff .hfrom { font-weight: 700; font-size: .92rem; letter-spacing: -0.01em; }
.handoff .harrow { color: var(--faint); }
.handoff .hto { font-weight: 700; font-size: .92rem; color: var(--accent-ink); }
.handoff p { margin: .3rem 0 .4rem; font-size: .87rem; color: var(--muted); }
.handoff .hfoot { display: flex; flex-wrap: wrap; gap: .35rem; align-items: center; }

@media (max-width: 52rem) {
  .bubble { max-width: 92%; }
}
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
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } .livebadge .pulse { animation: none; } }
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


def share_page(title: str, body_html: str, description: str = "") -> str:
    """A standalone, UNGUARDED public page for a shared artifact.

    Deliberately minimal: same visual language (shared CSS) but NO topbar, NO sidebar,
    NO search, NO breadcrumbs — nothing that reveals the bundle's structure or offers
    navigation into the private knowledge base. Only the artifact body and a neutral
    footer. ``body_html`` is trusted (server-built). ``description`` feeds OG/link
    previews so shared links look polished in chat apps and socials.
    """
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:type" content="article">
<meta name="twitter:card" content="summary">
<meta name="description" content="{esc(description)}">
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
    head_extra: str = "",
) -> str:
    """Full HTML document: sticky topbar, persistent sidebar, then ``body_html``.

    ``sidebar_html`` and ``body_html`` are trusted (server-built). The mobile
    sidebar toggle and all collapsibles are pure CSS — zero JavaScript.
    ``head_extra`` injects trusted markup into ``<head>`` (e.g. the live-thread
    ``<meta http-equiv="refresh">`` — the single dynamic mechanism, no JS).
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
{head_extra}<style>{CSS}</style>
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
      <a href="/brain/workspace" class="nav-workspace">Workspace</a>
      <a href="/brain/threads">Threads</a>
      <a href="/brain/graph">Graph</a>
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


# ---------------------------------------------------------------- interactive graph

# Full-bleed graph-only styling, layered on top of the shared CSS tokens so the
# canvas reads the same light/dark copper palette (colors are pulled at runtime
# from the CSS custom properties on :root).
GRAPH_CSS = """\
.graphwrap {
  position: fixed; left: 0; right: 0; top: 3rem; bottom: 0; overflow: hidden; background: var(--bg);
}
.graphwrap canvas { width: 100%; height: 100%; display: block; touch-action: none; cursor: grab; }
.gtip {
  position: absolute; pointer-events: none; display: none; z-index: 6; max-width: 17rem;
  background: var(--surface); border: 1px solid var(--line-2); border-radius: 8px;
  padding: .4rem .6rem; font-size: .82rem; color: var(--fg); box-shadow: 0 4px 18px rgba(0,0,0,.2);
}
.gtip b { display: block; font-weight: 650; letter-spacing: -0.01em; }
.gtip span { color: var(--muted); font-size: .68rem; text-transform: uppercase; letter-spacing: .07em; }
.glegend {
  position: absolute; left: 1rem; bottom: 1rem; z-index: 5; max-width: 62%;
  display: flex; flex-wrap: wrap; gap: .55rem; font-size: .76rem; color: var(--muted);
  background: color-mix(in srgb, var(--surface) 90%, transparent);
  border: 1px solid var(--line); border-radius: 10px; padding: .5rem .7rem; box-shadow: var(--shadow);
}
.glegend .gl { display: inline-flex; align-items: center; gap: .35rem; }
.glegend .gl i { width: .7rem; height: .7rem; border-radius: 50%; display: inline-block; flex: 0 0 auto; }
.glegend .gl i.dash { width: 1rem; height: 0; border-radius: 0; border-top: 2px dashed var(--amber); background: none !important; align-self: center; }
.ghint {
  position: absolute; right: 1rem; top: 1rem; z-index: 5; font-size: .72rem; color: var(--faint);
  background: color-mix(in srgb, var(--surface) 85%, transparent);
  border: 1px solid var(--line); padding: .3rem .55rem; border-radius: 8px;
}
@media (max-width: 52rem) { .ghint { display: none; } .glegend { max-width: 90%; } }
"""

# Vanilla, zero-dependency force-directed layout. __DATA__ / __GLYPHS__ are
# substituted by graph_page(); no external requests, no libraries.
GRAPH_JS = """\
(function(){
  var DATA = __DATA__, GLYPHS = __GLYPHS__;
  var canvas = document.getElementById('graph');
  if(!canvas) return;
  var ctx = canvas.getContext('2d');
  var tip = document.getElementById('gtip');
  var cs = getComputedStyle(document.documentElement);
  function cssVar(n){ return (cs.getPropertyValue(n)||'').trim() || '#888888'; }
  var TYPEVAR = {decision:'--accent',spec:'--blue',reference:'--violet',note:'--amber',person:'--green',client:'--green',message:'--red',runbook:'--accent-ink',video:'--violet',idea:'--amber',meeting:'--blue',snippet:'--muted',project:'--accent',artifact:'--accent-ink','live-view':'--green'};
  function colorFor(t){ return cssVar(TYPEVAR[t] || '--muted'); }
  function esc(s){ return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];}); }

  var nodes = DATA.nodes.map(function(n){ return {id:n.id,title:n.title,type:n.type,superseded:!!n.superseded,x:0,y:0,vx:0,vy:0,deg:0}; });
  var byId = {}; nodes.forEach(function(n){ byId[n.id]=n; });
  var edges = DATA.edges.filter(function(e){ return byId[e.source]&&byId[e.target]; })
                        .map(function(e){ return {s:byId[e.source],t:byId[e.target],kind:e.kind||""}; });
  edges.forEach(function(e){ e.s.deg++; e.t.deg++; });
  var nbr = {}; nodes.forEach(function(n){ nbr[n.id]=new Set(); });
  edges.forEach(function(e){ nbr[e.s.id].add(e.t.id); nbr[e.t.id].add(e.s.id); });

  var W=0,H=0,dpr=Math.min(window.devicePixelRatio||1,2);
  function resize(){ W=canvas.clientWidth; H=canvas.clientHeight; canvas.width=Math.max(1,W*dpr); canvas.height=Math.max(1,H*dpr); ctx.setTransform(dpr,0,0,dpr,0,0); }
  resize();
  var N = nodes.length || 1;
  nodes.forEach(function(n,i){ var a=i/N*Math.PI*2, rr=Math.min(W,H)*0.30+40; n.x=Math.cos(a)*rr*(0.5+Math.random()*0.4); n.y=Math.sin(a)*rr*(0.5+Math.random()*0.4); });

  var view={x:W/2,y:H/2,k:1}, alpha=1, raf=null, hoverId=null;
  function radius(n){ return 5+Math.sqrt(n.deg)*3.2; }

  function step(){
    var i,j;
    for(i=0;i<nodes.length;i++){ var g=nodes[i]; g.vx+=(0-g.x)*0.0022*alpha; g.vy+=(0-g.y)*0.0022*alpha; }
    for(i=0;i<nodes.length;i++){
      for(j=i+1;j<nodes.length;j++){
        var a=nodes[i], b=nodes[j], dx=a.x-b.x, dy=a.y-b.y, d2=dx*dx+dy*dy; if(d2<0.01)d2=0.01;
        var d=Math.sqrt(d2), rep=1600*alpha/d2, fx=dx/d*rep, fy=dy/d*rep;
        a.vx+=fx; a.vy+=fy; b.vx-=fx; b.vy-=fy;
      }
    }
    var L=72;
    for(var k=0;k<edges.length;k++){
      var e=edges[k], ex=e.t.x-e.s.x, ey=e.t.y-e.s.y, ed=Math.sqrt(ex*ex+ey*ey)||0.01;
      var f=(ed-L)*0.035*alpha, ax=ex/ed*f, ay=ey/ed*f;
      e.s.vx+=ax; e.s.vy+=ay; e.t.vx-=ax; e.t.vy-=ay;
    }
    for(i=0;i<nodes.length;i++){ var m=nodes[i]; m.x+=m.vx*0.5; m.y+=m.vy*0.5; m.vx*=0.86; m.vy*=0.86; }
    alpha*=0.985;
  }

  function toScreen(n){ return {x:n.x*view.k+view.x, y:n.y*view.k+view.y}; }

  function draw(){
    ctx.clearRect(0,0,W,H); ctx.lineWidth=1;
    for(var k=0;k<edges.length;k++){
      var e=edges[k], a=toScreen(e.s), b=toScreen(e.t), act=hoverId&&(e.s.id===hoverId||e.t.id===hoverId), sup=e.kind==="supersedes";
      ctx.setLineDash(sup?[5,4]:[]);
      ctx.lineWidth = sup?1.4:1;
      ctx.strokeStyle = act ? cssVar('--accent') : (sup ? cssVar('--amber') : cssVar('--line-2'));
      ctx.globalAlpha = hoverId ? (act?0.95:0.12) : (sup?0.8:0.5);
      ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.lineTo(b.x,b.y); ctx.stroke();
      if(sup){ // arrowhead at the superseded (target) end, offset by its radius
        var dx=b.x-a.x, dy=b.y-a.y, dd=Math.sqrt(dx*dx+dy*dy)||1, ux=dx/dd, uy=dy/dd, tr=radius(e.t)+2, tx=b.x-ux*tr, ty=b.y-uy*tr, ah=6;
        ctx.setLineDash([]); ctx.beginPath(); ctx.moveTo(tx,ty);
        ctx.lineTo(tx-ux*ah-uy*ah*0.6, ty-uy*ah+ux*ah*0.6); ctx.lineTo(tx-ux*ah+uy*ah*0.6, ty-uy*ah-ux*ah*0.6);
        ctx.closePath(); ctx.fillStyle=act?cssVar('--accent'):cssVar('--amber'); ctx.fill();
      }
    }
    ctx.globalAlpha=1; ctx.lineWidth=1; ctx.setLineDash([]);
    for(var i=0;i<nodes.length;i++){
      var n=nodes[i], p=toScreen(n), r=radius(n), near=hoverId&&(n.id===hoverId||nbr[hoverId].has(n.id));
      ctx.globalAlpha = (hoverId && !near) ? 0.25 : (n.superseded ? 0.45 : 1);
      ctx.beginPath(); ctx.arc(p.x,p.y,r,0,Math.PI*2);
      ctx.fillStyle=colorFor(n.type); ctx.fill();
      ctx.lineWidth = (n.id===hoverId)?2.5:1;
      if(n.superseded){ ctx.setLineDash([2,2]); ctx.strokeStyle=cssVar('--amber'); } else { ctx.setLineDash([]); ctx.strokeStyle=cssVar('--surface'); }
      ctx.stroke(); ctx.setLineDash([]);
      if(view.k>0.55 || near){
        ctx.globalAlpha=(hoverId && !near)?0.15:0.92;
        ctx.fillStyle=cssVar('--fg'); ctx.font='11px system-ui,-apple-system,sans-serif'; ctx.textAlign='center';
        var t=n.title.length>24?n.title.slice(0,23)+'\\u2026':n.title;
        ctx.fillText(t, p.x, p.y+r+11);
      }
    }
    ctx.globalAlpha=1;
  }

  function tick(){ step(); draw(); raf=(alpha>0.03)?requestAnimationFrame(tick):null; }
  function pick(sx,sy){ var best=null,bd=1e9; for(var i=0;i<nodes.length;i++){ var n=nodes[i],p=toScreen(n),r=radius(n)+5,dx=sx-p.x,dy=sy-p.y,d=dx*dx+dy*dy; if(d<r*r && d<bd){bd=d;best=n;} } return best; }

  var dragging=false, moved=0, lastX=0, lastY=0;
  canvas.addEventListener('pointerdown', function(e){ dragging=true; moved=0; lastX=e.clientX; lastY=e.clientY; try{canvas.setPointerCapture(e.pointerId);}catch(_){} });
  canvas.addEventListener('pointermove', function(e){
    var rect=canvas.getBoundingClientRect(), sx=e.clientX-rect.left, sy=e.clientY-rect.top;
    if(dragging){ var dx=e.clientX-lastX, dy=e.clientY-lastY; moved+=Math.abs(dx)+Math.abs(dy); view.x+=dx; view.y+=dy; lastX=e.clientX; lastY=e.clientY; if(tip)tip.style.display='none'; if(!raf)draw(); return; }
    var hit=pick(sx,sy), nh=hit?hit.id:null;
    if(nh!==hoverId){ hoverId=nh; if(!raf)draw(); }
    if(hit && tip){ tip.style.display='block'; tip.style.left=(sx+14)+'px'; tip.style.top=(sy+12)+'px'; tip.innerHTML='<b>'+esc(hit.title)+'</b>'+(hit.type?'<span>'+esc(hit.type)+'</span>':''); canvas.style.cursor='pointer'; }
    else { if(tip)tip.style.display='none'; canvas.style.cursor='grab'; }
  });
  function endDrag(e){ if(!dragging)return; dragging=false; if(moved<5){ var rect=canvas.getBoundingClientRect(), hit=pick(e.clientX-rect.left,e.clientY-rect.top); if(hit){ window.location.href='/brain/f/'+hit.id; } } }
  canvas.addEventListener('pointerup', endDrag);
  canvas.addEventListener('pointerleave', function(){ if(tip)tip.style.display='none'; if(hoverId){hoverId=null; if(!raf)draw();} });
  canvas.addEventListener('wheel', function(e){ e.preventDefault(); var rect=canvas.getBoundingClientRect(), sx=e.clientX-rect.left, sy=e.clientY-rect.top, f=Math.exp(-e.deltaY*0.0012), k2=Math.max(0.2,Math.min(4,view.k*f)), wx=(sx-view.x)/view.k, wy=(sy-view.y)/view.k; view.k=k2; view.x=sx-wx*view.k; view.y=sy-wy*view.k; if(!raf)draw(); }, {passive:false});
  window.addEventListener('resize', function(){ resize(); if(!raf)draw(); });

  var legend=document.getElementById('glegend');
  if(legend){
    var types={}; nodes.forEach(function(n){ types[n.type||'other']=true; });
    var html = Object.keys(types).sort().map(function(t){ var g=GLYPHS[t]?GLYPHS[t]+' ':''; return '<span class="gl"><i style="background:'+colorFor(t)+'"></i>'+esc(g+t)+'</span>'; }).join('');
    if(edges.some(function(e){ return e.kind==="supersedes"; })){ html += '<span class="gl"><i class="dash" style="background:'+cssVar('--amber')+'"></i>⚠ supersedes →</span>'; }
    legend.innerHTML = html;
  }
  tick();
})();
"""


def graph_page(data: dict, glyphs: dict) -> str:
    """A full-bleed interactive concept-graph page (its own minimal shell).

    ``data`` is ``{"nodes": [...], "edges": [...]}``; ``glyphs`` maps a type to its
    Unicode legend glyph. The force-directed layout is inline vanilla JS with the
    node/edge data embedded directly — no fetch, no libraries, no external requests.
    """
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    gly = json.dumps(glyphs, ensure_ascii=False).replace("</", "<\\/")
    script = GRAPH_JS.replace("__DATA__", blob).replace("__GLYPHS__", gly)
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Graph — brain</title>\n"
        "<style>" + CSS + GRAPH_CSS + "</style>\n</head>\n<body>\n"
        '<header class="topbar"><div class="topbar-inner">'
        '<a class="wordmark" href="/brain"><span class="mark">◗</span>brain</a>'
        '<span class="search" aria-hidden="true"></span>'
        '<nav class="topnav">'
        '<a href="/brain">Overview</a>'
        '<a href="/brain/workspace" class="nav-workspace">Workspace</a>'
        '<a href="/brain/threads">Threads</a>'
        '<a href="/brain/graph" class="active" aria-current="page">Graph</a>'
        '<a href="/brain/system">System</a>'
        '<a href="/brain/activity">Activity</a>'
        "</nav></div></header>\n"
        '<div class="graphwrap">'
        '<canvas id="graph" aria-label="Concept graph"></canvas>'
        '<div id="gtip" class="gtip"></div>'
        '<div id="glegend" class="glegend"></div>'
        '<div class="ghint">drag to pan · scroll to zoom · click a node to open</div>'
        "</div>\n"
        "<script>" + script + "</script>\n</body>\n</html>\n"
    )
