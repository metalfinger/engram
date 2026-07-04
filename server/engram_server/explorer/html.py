"""HTML building blocks for the explorer. Server-rendered, zero JavaScript."""

from __future__ import annotations

from collections.abc import Sequence
from html import escape as _escape

CSS = """\
:root {
  --bg: #fafaf8; --fg: #1c1c1a; --muted: #6b6b66; --line: #e2e2dd;
  --card: #ffffff; --accent: #2456a6; --code-bg: #f0f0ec;
  --badge-bg: #ececea; --badge-fg: #454540;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16181d; --fg: #e6e6e2; --muted: #9a9a94; --line: #2c2f36;
    --card: #1e2128; --accent: #7aa5e8; --code-bg: #23262e;
    --badge-bg: #2a2d35; --badge-fg: #c2c2bc;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); line-height: 1.6;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif; }
main { max-width: 52rem; margin: 0 auto; padding: 1rem 1.25rem 4rem; }
.topnav { position: sticky; top: 0; z-index: 10; display: flex; gap: 1rem;
  align-items: baseline; padding: .6rem 1.25rem; background: var(--card);
  border-bottom: 1px solid var(--line); }
.topnav .brand { font-weight: 700; margin-right: .5rem; }
.topnav a { color: var(--fg); text-decoration: none; font-weight: 500; }
.topnav a:hover { color: var(--accent); }
a { color: var(--accent); }
h1, h2, h3 { line-height: 1.25; }
.crumbs { margin: 1rem 0; font-size: .9rem; color: var(--muted); }
.crumbs a { color: var(--muted); text-decoration: none; }
.crumbs a:hover { color: var(--accent); }
.crumbs .sep { margin: 0 .2rem; }
.badge { display: inline-block; padding: .1rem .55rem; border-radius: 999px;
  font-size: .75rem; font-weight: 600; background: var(--badge-bg);
  color: var(--badge-fg); margin: 0 .3rem .2rem 0; white-space: nowrap; }
.badge.unread { background: #b3261e; color: #fff; }
.badge.active { background: #1e6f43; color: #fff; }
.badge.done { background: #3555a4; color: #fff; }
.badge.archived { background: var(--badge-bg); color: var(--muted); }
.badge.superseded { background: #8a6d1f; color: #fff; }
.badge.priority-high { background: #b3261e; color: #fff; }
.badge.bot { background: #5b3f9e; color: #fff; }
.badges { margin: .3rem 0 .8rem; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(15rem, 1fr));
  gap: .9rem; margin: 1rem 0 2rem; }
.card { display: block; padding: .9rem 1rem; border: 1px solid var(--line);
  border-radius: .6rem; background: var(--card); color: var(--fg); text-decoration: none; }
.card:hover { border-color: var(--accent); }
.card h3 { margin: 0 0 .3rem; font-size: 1.05rem; }
.card p { margin: 0 0 .5rem; font-size: .88rem; color: var(--muted); }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: .9rem; }
th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid var(--line);
  vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
tr.bot-row td { background: rgba(122, 101, 226, .08); }
pre, code { font-family: ui-monospace, "Cascadia Code", Consolas, monospace; font-size: .88em; }
pre { background: var(--code-bg); padding: .8rem 1rem; border-radius: .5rem;
  overflow-x: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
code { background: var(--code-bg); padding: .1rem .3rem; border-radius: .3rem; }
pre code { background: none; padding: 0; }
blockquote { margin: 1rem 0; padding: .1rem 1rem; border-left: 3px solid var(--line);
  color: var(--muted); }
details { margin: 1rem 0; }
summary { cursor: pointer; font-weight: 600; }
hr { border: none; border-top: 1px solid var(--line); margin: 2rem 0; }
img { max-width: 100%; }
.msg { border: 1px solid var(--line); border-radius: .6rem; padding: .8rem 1rem;
  margin: .8rem 0; background: var(--card); }
.msg h3 { margin: .1rem 0 .3rem; font-size: 1rem; }
.msg p { margin: .2rem 0 .5rem; color: var(--muted); font-size: .9rem; }
.meta { color: var(--muted); font-size: .85rem; }
ul.tree { list-style: none; padding-left: 1.1rem; }
ul.tree li { margin: .15rem 0; }
@media (max-width: 40rem) {
  .topnav { gap: .7rem; padding: .6rem .9rem; overflow-x: auto; }
  main { padding: .8rem .9rem 3rem; }
}
"""


def esc(s: object) -> str:
    """HTML-escape any value (including attribute quotes)."""
    return _escape(str(s), quote=True)


def badge(label: str, cls: str = "") -> str:
    """A pill badge; ``cls`` picks a color variant (unread, active, done, ...)."""
    classes = f"badge {cls}".strip()
    return f'<span class="{esc(classes)}">{esc(label)}</span>'


def card(href: str, title: str, desc: str, badges_html: str = "") -> str:
    """A clickable grid card. ``badges_html`` is pre-built (trusted) HTML."""
    return (
        f'<a class="card" href="{esc(href)}">'
        f"<h3>{esc(title)}</h3>"
        f"<p>{esc(desc)}</p>"
        f'<div class="badges">{badges_html}</div>'
        "</a>"
    )


def page(title: str, body_html: str, crumbs: Sequence[tuple[str, str]] = ()) -> str:
    """Full HTML document: sticky top nav, breadcrumbs, then ``body_html`` (trusted)."""
    crumb_html = ""
    if crumbs:
        links = [f'<a href="{esc(href)}">{esc(label)}</a>' for label, href in crumbs]
        crumb_html = '<nav class="crumbs">' + '<span class="sep">/</span>'.join(links) + "</nav>"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} — Engram Brain</title>
<style>{CSS}</style>
</head>
<body>
<nav class="topnav">
  <span class="brand">Engram</span>
  <a href="/brain">Brain</a>
  <a href="/brain/system">System</a>
  <a href="/brain/activity">Activity</a>
</nav>
<main>
{crumb_html}
{body_html}
</main>
</body>
</html>
"""
