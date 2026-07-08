"""Brain Navigator — an MCP App (SEP-1865) widget over the kb_* tools.

The server registers ``NAVIGATOR_URI`` as a ``ui://`` resource and tags the
navigation tools (kb_projects, kb_load, kb_search) with ``_meta.ui.resourceUri``.
An MCP-Apps-capable host (claude.ai, ChatGPT) mounts this single self-contained
HTML doc in a sandboxed iframe, pushes the tool result in, and relays the
widget's ``tools/call`` back to the server. The widget talks ONLY over that
postMessage bridge (plus a ``window.openai`` path for ChatGPT) — it makes zero
direct network requests, so the resource CSP is deny-by-default (empty arrays).

Full surface:
  HOME    project cards from kb_projects.
  BROWSE  kb_load: context.md summary + collapsible concept tree.
  READER  kb_read: frontmatter chips + a small built-in markdown renderer;
          relative .md links navigate in-widget.
  SEARCH  debounced kb_search with an optional project filter, score bars.
  INBOX   unread inter-session messages aggregated across projects, with
          Archive (kb_mark_read) and Act (ui/message handoff to the agent).
  BASKET  collect concepts from any view, reorder them, and ask the agent to
          build an artifact (status report / spec / blog / summary / …) from
          exactly those paths — the differentiator.

Everything is opt-in behind ``ENGRAM_WIDGET`` (config ``widget``): when the flag
is off the tool meta is ``None`` and the resource is never registered, so the
server behaves exactly as before.

Visual language mirrors the transparency explorer (engram_server/explorer/html.py):
warm paper/ink, one copper accent, hairline borders, small-caps eyebrow labels,
and the same Unicode type-stamp glyphs — light and dark via prefers-color-scheme.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

NAVIGATOR_URI = "ui://engram/navigator"
NAVIGATOR_MIME = "text/html;profile=mcp-app"


def navigator_resource_meta() -> dict:
    """``_meta`` for the ui:// resource.

    Deny-by-default CSP (empty connect/resource domains) because the widget
    reaches the server ONLY over the MCP Apps bridge — it never fetches a URL
    itself. ``prefersBorder`` (both the MCP-Apps and OpenAI namespaces) asks the
    host for its padded, bordered card chrome so the inline card isn't full-bleed
    on ChatGPT. Height is reported at runtime (size-changed + notifyIntrinsicHeight).
    """
    return {
        "ui": {
            "csp": {"connectDomains": [], "resourceDomains": []},
            "prefersBorder": True,
        },
        "openai/widgetPrefersBorder": True,
    }


def navigator_tool_meta(enabled: bool) -> dict | None:
    """``_meta`` linking a tool to the Navigator widget; ``None`` disables it.

    When ``None`` the tool stays plain (conversational) — no widget mounts.
    """
    return {"ui": {"resourceUri": NAVIGATOR_URI}} if enabled else None


def get_navigator_html(explorer_url: str) -> str:
    """Render the widget HTML, stamping the explorer host into the Graph link.

    ``NAVIGATOR_HTML`` is a template carrying the ``__EXPLORER_URL__`` sentinel —
    kept literal in the module constant so the no-external-requests test can keep
    asserting the constant has no real URLs. The concrete host is injected ONLY at
    serve time here (server-side), so the served document never contains the
    sentinel and never ships a hard-coded domain.
    """
    return NAVIGATOR_HTML.replace("__EXPLORER_URL__", explorer_url.rstrip("/"))


def register_navigator(mcp: "FastMCP", enabled: bool) -> None:
    """Register the ui:// resource when the widget flag is on; a no-op when off.

    Factored out of app.py so a test can build a fresh FastMCP with the flag on
    and assert the resource is present, without reloading the app module (whose
    settings are read once at import via an lru_cache). The explorer URL for the
    Graph link is read from settings HERE (keeping app.py's two-arg call intact)
    and baked into the served HTML once at registration.
    """
    if not enabled:
        return

    from engram_server.config import get_settings

    html = get_navigator_html(get_settings().explorer_url)

    @mcp.resource(
        NAVIGATOR_URI, mime_type=NAVIGATOR_MIME, meta=navigator_resource_meta()
    )
    def navigator_resource() -> str:
        return html


# ---------------------------------------------------------------------------
# The widget. One self-contained HTML document, zero external requests, no build
# step. Raw string (NOT an f-string) — the CSS/JS braces are literal.
# ---------------------------------------------------------------------------

NAVIGATOR_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Brain Navigator</title>
<style>
:root {
  --bg:#faf7f2; --surface:#ffffff; --surface-2:#f4efe6; --inset:#f7f2ea;
  --fg:#221f1a; --muted:#7a7266; --faint:#a89e8e;
  --line:#eae1d4; --line-2:#e0d6c4;
  --accent:#b5622b; --accent-ink:#9c4f1f; --accent-fg:#ffffff;
  --accent-soft:#f1e3d3; --accent-line:#e3c9ad;
  --green:#47804f; --blue:#3d6ba0; --violet:#7a58ab; --red:#b23a26; --amber:#97701f;
  --code-bg:#f4efe6; --shadow:0 1px 2px rgba(60,40,20,.05);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#16130d; --surface:#1e1a13; --surface-2:#241f16; --inset:#211c14;
    --fg:#ece6da; --muted:#a09784; --faint:#6d6453;
    --line:#2c261b; --line-2:#39321f;
    --accent:#d98a4e; --accent-ink:#e29a63; --accent-fg:#1a1409;
    --accent-soft:rgba(217,138,78,.13); --accent-line:#4a3820;
    --green:#6fb37f; --blue:#7ba3d6; --violet:#ad8fd8; --red:#e08a72; --amber:#cdac66;
    --code-bg:#241f16; --shadow:none;
  }
}
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; }
body {
  margin:0; background:var(--bg); color:var(--fg);
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
  font-size:14px; line-height:1.55; letter-spacing:-0.003em; -webkit-font-smoothing:antialiased;
  position:relative;
}
a { color:var(--accent-ink); text-decoration:none; }
a:hover { text-decoration:underline; text-underline-offset:2px; }
button { font:inherit; }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px; border-radius:3px; }
.wrap { padding:12px 14px 14px; }

/* view transition (CSS only) */
@keyframes fade { from { opacity:0; transform:translateY(3px); } to { opacity:1; transform:none; } }
#view { animation:fade .16s ease; }
@media (prefers-reduced-motion: reduce) { #view { animation:none; } * { transition:none !important; } }

/* eyebrow / small-caps labels */
.eyebrow { font-size:.66rem; font-weight:700; letter-spacing:.13em; text-transform:uppercase; color:var(--accent-ink); margin:0 0 .3rem; }
.section-label { font-size:.66rem; font-weight:700; letter-spacing:.12em; text-transform:uppercase; color:var(--faint); margin:1.1rem 0 .35rem; }

/* tab bar */
.tabs { display:flex; gap:2px; border-bottom:1px solid var(--line); padding:0 6px; background:color-mix(in srgb,var(--bg) 90%,transparent); position:sticky; top:0; z-index:5; }
.tab { appearance:none; border:0; background:none; font-size:.78rem; font-weight:600; letter-spacing:.02em;
       color:var(--muted); padding:.5rem .7rem; cursor:pointer; border-bottom:2px solid transparent; margin-bottom:-1px; display:inline-flex; align-items:center; gap:.3rem; }
.tab:hover { color:var(--fg); }
.tab.active { color:var(--accent-ink); border-bottom-color:var(--accent); }
.tbadge { background:var(--accent); color:var(--accent-fg); font-size:.6rem; font-weight:700; min-width:1.05rem; height:1.05rem; padding:0 .3rem; border-radius:999px; display:none; place-items:center; }
.graphlink { margin-left:auto; align-self:center; font-size:.72rem; font-weight:600; letter-spacing:.02em; color:var(--muted); padding:.3rem .5rem; border-radius:7px; white-space:nowrap; }
.graphlink:hover { color:var(--accent-ink); text-decoration:none; background:var(--surface-2); }

/* quick capture (Home) */
.qcap { display:flex; gap:.5rem; margin:0 0 .9rem; }
.qc-in { flex:1 1 auto; min-width:0; padding:.45rem .7rem; font-size:.86rem; color:var(--fg); background:var(--surface); border:1px solid var(--line-2); border-radius:8px; outline:none; }
.qc-in:focus { border-color:var(--accent-line); box-shadow:0 0 0 3px var(--accent-soft); }
.qc-go { flex:0 0 auto; background:var(--accent); color:var(--accent-fg); font-weight:650; font-size:.8rem; border:1px solid var(--accent); border-radius:8px; padding:.42rem .85rem; cursor:pointer; }
.qc-go:hover { filter:brightness(1.06); }
.qc-in:disabled, .qc-go:disabled { opacity:.55; cursor:default; }

/* artifacts filter row + recipes */
.filterrow { display:flex; gap:.35rem; margin:.6rem 0 .2rem; }
.fbtn { appearance:none; border:1px solid var(--line-2); background:var(--surface); color:var(--muted); font-size:.74rem; font-weight:600; padding:.28rem .6rem; border-radius:999px; cursor:pointer; }
.fbtn:hover { border-color:var(--accent-line); color:var(--accent-ink); }
.fbtn.on { background:var(--accent-soft); color:var(--accent-ink); border-color:var(--accent-line); }
.chip.beta { background:color-mix(in srgb,var(--violet) 16%,transparent); color:var(--violet); border-color:transparent; text-transform:uppercase; letter-spacing:.06em; font-size:.6rem; }
.save-recipe { flex:0 0 auto; background:var(--surface); color:var(--accent-ink); font-weight:600; font-size:.82rem; border:1px solid var(--accent-line); border-radius:8px; padding:.42rem .8rem; cursor:pointer; }
.save-recipe:hover { background:var(--accent-soft); }

h1 { font-size:1.35rem; line-height:1.15; letter-spacing:-0.02em; margin:0 0 .15rem; font-weight:700; }
.lede { color:var(--muted); font-size:.85rem; margin:.15rem 0 0; }

/* project cards */
.cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(13rem,1fr)); gap:.6rem; margin:.9rem 0 .3rem; }
.card { display:flex; flex-direction:column; gap:.3rem; padding:.7rem .8rem; text-align:left; cursor:pointer;
        background:var(--surface); color:var(--fg); border:1px solid var(--line); border-radius:10px; box-shadow:var(--shadow);
        transition:border-color .12s ease, transform .12s ease; }
.card:hover { border-color:var(--accent-line); transform:translateY(-1px); }
.card-head { display:flex; align-items:center; gap:.4rem; }
.card-head h3 { margin:0; font-size:.95rem; font-weight:650; letter-spacing:-0.01em; }
.card p { margin:0; font-size:.8rem; color:var(--muted); line-height:1.45; }
.card-foot { margin-top:auto; padding-top:.15rem; display:flex; flex-wrap:wrap; gap:.35rem; align-items:center; }

/* status dot */
.dot { width:.55rem; height:.55rem; border-radius:50%; background:var(--faint); flex:0 0 auto; }
.dot.s-green { background:var(--green); } .dot.s-blue { background:var(--blue); }
.dot.s-accent { background:var(--accent); } .dot.s-amber { background:var(--amber); } .dot.s-red { background:var(--red); }

/* badges / chips */
.badge, .chip { display:inline-flex; align-items:center; gap:.3rem; font-size:.68rem; font-weight:600; padding:.1rem .5rem; border-radius:999px; white-space:nowrap; line-height:1.5; }
.badge { background:var(--surface-2); color:var(--muted); }
.badge.unread { background:var(--accent); color:var(--accent-fg); }
.chip { background:var(--inset); color:var(--muted); border:1px solid var(--line); }
.chip.tag::before { content:"#"; color:var(--faint); }
.chip.proj { color:var(--accent-ink); border-color:var(--accent-line); background:var(--accent-soft); }
.chip.prio-high { background:color-mix(in srgb,var(--red) 15%,transparent); color:var(--red); border-color:transparent; }
.chip.supersede { background:color-mix(in srgb,var(--amber) 16%,transparent); color:var(--amber); border-color:color-mix(in srgb,var(--amber) 35%,transparent); cursor:pointer; }
.chip.supersede:hover { border-color:var(--amber); }
.chip.supersedes-chip { cursor:pointer; }
.chip.dim { opacity:.5; }
.chip .k { color:var(--faint); text-transform:uppercase; letter-spacing:.05em; font-size:.9em; }
.chips { display:flex; flex-wrap:wrap; gap:.35rem; margin:.5rem 0 0; }
.when { font-size:.7rem; color:var(--faint); font-variant-numeric:tabular-nums; }

/* stamp glyph */
.stamp { flex:0 0 auto; width:1.5rem; height:1.5rem; border-radius:7px; display:grid; place-items:center;
         background:var(--accent-soft); border:1px solid var(--accent-line); font-size:.9rem; }

/* browse header + crumbs */
.bhead { display:flex; align-items:center; gap:.5rem; margin:.2rem 0 .1rem; }
.back, .refresh { border:1px solid var(--line-2); background:var(--surface); color:var(--muted);
        font-size:.74rem; font-weight:600; padding:.2rem .5rem; border-radius:7px; cursor:pointer; }
.back:hover, .refresh:hover { border-color:var(--accent-line); color:var(--accent-ink); }
.crumbs { font-size:.72rem; color:var(--muted); margin:.1rem 0 .7rem; display:flex; flex-wrap:wrap; gap:.1rem; align-items:center; }
.crumbs button { border:0; background:none; font-size:inherit; color:var(--muted); cursor:pointer; padding:0; }
.crumbs button:hover { color:var(--accent-ink); text-decoration:underline; }
.crumbs .sep { color:var(--faint); margin:0 .3rem; }
.crumbs .cur { color:var(--fg); }

/* context summary block */
.ctx { border:1px solid var(--line); border-left:3px solid var(--accent-line); border-radius:0 8px 8px 0;
       background:var(--inset); padding:.5rem .8rem; margin:.7rem 0 0; max-height:15rem; overflow:hidden; position:relative; }
.ctx.fade::after { content:""; position:absolute; left:0; right:0; bottom:0; height:2.5rem; background:linear-gradient(transparent,var(--inset)); }

/* tree */
.tree { margin:.5rem 0 0; }
.tnode > summary { display:flex; align-items:center; gap:.35rem; padding:.28rem .35rem; border-radius:6px; cursor:pointer;
                   font-size:.83rem; font-weight:600; color:var(--fg); list-style:none; }
.tnode > summary::-webkit-details-marker { display:none; }
.tnode > summary::before { content:"›"; color:var(--faint); font-weight:400; width:.7rem; display:inline-block; transition:transform .12s ease; }
.tnode[open] > summary::before { transform:rotate(90deg); }
.tnode > summary:hover { background:var(--surface-2); }
.tnode .kids { margin:.05rem 0 .3rem .85rem; padding-left:.5rem; border-left:1px solid var(--line); }
.frow { display:flex; align-items:baseline; gap:.4rem; padding:.28rem .4rem; border-radius:6px; }
.frow .open { flex:1 1 auto; display:flex; align-items:baseline; gap:.4rem; cursor:pointer; min-width:0; }
.frow:hover { background:var(--surface-2); }
.frow .glyph { color:var(--faint); font-size:.85rem; flex:0 0 auto; }
.frow .ft { font-size:.83rem; font-weight:550; }
.frow .fd { font-size:.75rem; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

/* basket toggle on rows */
.bk-toggle { flex:0 0 auto; width:1.35rem; height:1.35rem; border-radius:6px; border:1px solid var(--line-2);
             background:var(--surface); color:var(--muted); font-size:.8rem; line-height:1; cursor:pointer; display:grid; place-items:center; }
.bk-toggle:hover { border-color:var(--accent-line); color:var(--accent-ink); }
.bk-toggle.on { background:var(--accent); color:var(--accent-fg); border-color:var(--accent); }

/* reader */
.props { display:flex; flex-wrap:wrap; gap:.35rem; margin:.6rem 0 .9rem; }
.statusv { display:inline-flex; align-items:center; gap:.35rem; text-transform:capitalize; }
.rhead { display:flex; align-items:flex-start; gap:.5rem; }
.rhead .rh-main { min-width:0; flex:1 1 auto; }
.rhead.superseded .rh-main h1 { color:var(--muted); text-decoration:line-through; text-decoration-color:var(--amber); }
.supersede-note { display:flex; align-items:baseline; gap:.4rem; margin:.6rem 0 .3rem; padding:.5rem .7rem; border-radius:8px;
  font-size:.82rem; font-weight:550; color:var(--amber); border:1px solid color-mix(in srgb,var(--amber) 40%,transparent);
  border-left:3px solid var(--amber); background:color-mix(in srgb,var(--amber) 12%,transparent); }
.supersede-note .snl { color:var(--amber); font-weight:650; text-decoration:underline; cursor:pointer; }

/* markdown body */
.md { font-size:.87rem; }
.md > :first-child { margin-top:0; }
.md h1,.md h2,.md h3,.md h4 { line-height:1.2; letter-spacing:-0.01em; }
.md h1 { font-size:1.2rem; margin:1.1rem 0 .5rem; font-weight:700; }
.md h2 { font-size:1.05rem; margin:1rem 0 .45rem; font-weight:650; }
.md h3 { font-size:.95rem; margin:.9rem 0 .4rem; font-weight:650; }
.md h4 { font-size:.85rem; margin:.8rem 0 .35rem; font-weight:650; color:var(--muted); }
.md p,.md ul,.md ol { margin:.6rem 0; }
.md li { margin:.2rem 0; }
.md code { font-family:ui-monospace,"Cascadia Code",Consolas,monospace; font-size:.86em; background:var(--code-bg); padding:.1rem .35rem; border-radius:5px; }
.md pre { background:var(--code-bg); padding:.7rem .85rem; border-radius:9px; overflow-x:auto; border:1px solid var(--line); line-height:1.5; margin:.7rem 0; }
.md pre code { background:none; padding:0; border:0; }
.md blockquote { margin:.9rem 0; padding:.25rem .85rem; border-left:3px solid var(--accent-line); color:var(--muted); background:var(--inset); border-radius:0 6px 6px 0; }
.md hr { border:none; border-top:1px solid var(--line); margin:1.4rem 0; }
.md a.mdlink { border-bottom:1px dotted var(--accent-line); }
.md strong { font-weight:650; }

/* search */
.searchbar { display:flex; gap:.5rem; margin:.7rem 0 .3rem; }
.sinput { flex:1 1 auto; min-width:0; padding:.45rem .7rem; font-size:.86rem; color:var(--fg);
          background:var(--surface); border:1px solid var(--line-2); border-radius:8px; outline:none; }
.sinput:focus { border-color:var(--accent-line); box-shadow:0 0 0 3px var(--accent-soft); }
.sselect, .bksel { padding:.45rem .5rem; font-size:.8rem; color:var(--fg); background:var(--surface); border:1px solid var(--line-2); border-radius:8px; }
.result { display:block; border:1px solid var(--line); border-radius:10px; background:var(--surface);
          padding:.7rem .85rem; margin:.55rem 0; box-shadow:var(--shadow); cursor:pointer; transition:border-color .12s ease; }
.result:hover { border-color:var(--accent-line); }
.result .rtop { display:flex; align-items:flex-start; gap:.5rem; }
.result h3 { margin:0; font-size:.95rem; font-weight:650; flex:1 1 auto; min-width:0; }
.result .rpath { font-size:.7rem; color:var(--faint); font-family:ui-monospace,Consolas,monospace; margin:.15rem 0; }
.result .rpath .sep { margin:0 .2rem; }
.result p { margin:.3rem 0 .35rem; font-size:.82rem; color:var(--muted); }
.scorebar { display:flex; align-items:center; gap:.5rem; margin-top:.4rem; }
.scorebar .track { flex:0 0 7rem; height:.3rem; border-radius:999px; background:var(--surface-2); overflow:hidden; }
.scorebar .fill { height:100%; background:var(--accent); border-radius:999px; transition:width .3s ease; }
.scorebar .pct { font-size:.68rem; color:var(--faint); font-variant-numeric:tabular-nums; }

/* inbox */
.ihead { display:flex; align-items:center; justify-content:space-between; margin:.2rem 0 .6rem; }
.imsg { border:1px solid var(--line); border-radius:10px; background:var(--surface); box-shadow:var(--shadow); margin:.55rem 0; padding:.2rem .3rem; position:relative; overflow:hidden; }
.imsg::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background:var(--accent); }
.imsg.expired { opacity:.6; }
.imsg.expired::before { background:var(--faint); }
.imsg > summary { list-style:none; cursor:pointer; padding:.55rem .7rem; display:flex; flex-direction:column; gap:.35rem; }
.imsg > summary::-webkit-details-marker { display:none; }
.imsg .ititle { font-size:.9rem; font-weight:650; }
.imsg .ichips { display:flex; flex-wrap:wrap; gap:.3rem; }
.imsg .ibody { padding:.2rem .8rem .3rem; border-top:1px solid var(--line); margin-top:.1rem; }
.imsg .iacts { display:flex; gap:.5rem; padding:.2rem .7rem .6rem; }
.act, .arch { font-size:.75rem; font-weight:600; padding:.32rem .7rem; border-radius:7px; cursor:pointer; border:1px solid var(--line-2); background:var(--surface); color:var(--fg); }
.act { background:var(--accent); color:var(--accent-fg); border-color:var(--accent); }
.act:hover { filter:brightness(1.06); }
.arch:hover { border-color:var(--accent-line); color:var(--accent-ink); }

/* artifacts */
.artrow { border:1px solid var(--line); border-radius:10px; background:var(--surface); box-shadow:var(--shadow); margin:.55rem 0; padding:.5rem .6rem; }
.artrow .artmain { display:flex; flex-direction:column; gap:.35rem; cursor:pointer; }
.artrow .arthead { display:flex; align-items:center; gap:.45rem; }
.artrow .artt { font-size:.9rem; font-weight:650; }
.artrow .artchips { display:flex; flex-wrap:wrap; gap:.35rem; align-items:center; }
.artrow .artacts { display:flex; gap:.5rem; margin-top:.5rem; }
.chip.shared { color:var(--accent-ink); border-color:var(--accent-line); background:var(--accent-soft); }
.chip.stale { background:color-mix(in srgb,var(--amber) 16%,transparent); color:var(--amber); border-color:transparent; }
.sharebox { margin-top:.5rem; border:1px solid var(--line-2); border-radius:8px; background:var(--code-bg); overflow-x:auto; }
.sharebox .share-url { display:block; padding:.45rem .6rem; font-family:ui-monospace,Consolas,monospace; font-size:.74rem; white-space:pre; color:var(--fg); -webkit-user-select:all; user-select:all; }

/* basket footer bar */
.basket { position:sticky; bottom:0; z-index:6; background:color-mix(in srgb,var(--surface) 96%,var(--accent) 4%);
          border-top:1px solid var(--accent-line); padding:0; max-height:0; overflow:hidden; transition:max-height .2s ease; }
.basket.on { max-height:60vh; padding:.6rem .8rem .7rem; box-shadow:0 -3px 12px rgba(60,40,20,.08); }
.bkhead { display:flex; align-items:center; justify-content:space-between; margin-bottom:.4rem; }
.bkclear { font-size:.7rem; font-weight:600; color:var(--muted); background:none; border:0; cursor:pointer; }
.bkclear:hover { color:var(--red); }
.bkchips { display:flex; flex-direction:column; gap:.3rem; max-height:9rem; overflow-y:auto; margin-bottom:.5rem; }
.bkchip { display:flex; align-items:center; gap:.25rem; font-size:.78rem; background:var(--inset); border:1px solid var(--line); border-radius:7px; padding:.2rem .35rem; }
.bkchip .bkt { flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.bkchip button { border:0; background:none; cursor:pointer; color:var(--muted); font-size:.7rem; padding:0 .15rem; line-height:1; }
.bkchip button:hover { color:var(--accent-ink); }
.bkchip .bkx:hover { color:var(--red); }
.bkbuild { display:flex; gap:.4rem; align-items:center; flex-wrap:wrap; }
.bkcustom { flex:1 1 8rem; min-width:0; padding:.4rem .5rem; font-size:.8rem; background:var(--surface); border:1px solid var(--line-2); border-radius:8px; color:var(--fg); }
.build { flex:0 0 auto; background:var(--accent); color:var(--accent-fg); font-weight:650; font-size:.82rem; border:1px solid var(--accent); border-radius:8px; padding:.42rem .9rem; cursor:pointer; }
.build:hover { filter:brightness(1.06); }

/* toasts */
#toasts { position:absolute; top:.4rem; left:0; right:0; display:flex; flex-direction:column; align-items:center; gap:.35rem; z-index:20; pointer-events:none; }
.toast { pointer-events:auto; display:flex; align-items:center; gap:.6rem; background:var(--fg); color:var(--bg);
         font-size:.8rem; font-weight:500; padding:.4rem .75rem; border-radius:8px; box-shadow:0 3px 12px rgba(0,0,0,.25);
         opacity:0; transform:translateY(-6px); transition:opacity .2s ease, transform .2s ease; max-width:90%; }
.toast.on { opacity:1; transform:none; }
.toast-retry { background:var(--accent); color:var(--accent-fg); border:0; border-radius:6px; font-size:.72rem; font-weight:700; padding:.2rem .5rem; cursor:pointer; }

/* skeletons + states */
.skel { position:relative; overflow:hidden; background:var(--surface-2); border-radius:9px; }
.skel::after { content:""; position:absolute; inset:0; transform:translateX(-100%);
               background:linear-gradient(90deg,transparent,color-mix(in srgb,var(--surface) 60%,transparent),transparent); animation:shimmer 1.2s infinite; }
@keyframes shimmer { 100% { transform:translateX(100%); } }
.skgrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(13rem,1fr)); gap:.6rem; margin:.9rem 0; }
.skel-card { height:5.2rem; } .skel-row { height:3.1rem; margin:.55rem 0; }
.sklist { margin:.7rem 0; }
.empty { color:var(--faint); font-size:.82rem; font-style:italic; padding:1.2rem .2rem; }
.errbox { display:flex; align-items:center; gap:.7rem; color:var(--red); font-size:.82rem; padding:.9rem 0; }
.retry { background:var(--accent); color:var(--accent-fg); border:0; border-radius:7px; font-weight:600; font-size:.75rem; padding:.3rem .7rem; cursor:pointer; }
</style></head>
<body>
<nav class="tabs" id="tabs">
  <button class="tab" data-tab="home">Home</button>
  <button class="tab" data-tab="browse">Browse</button>
  <button class="tab" data-tab="search">Search</button>
  <button class="tab" data-tab="inbox">Inbox<span class="tbadge" id="ib-badge"></span></button>
  <button class="tab" data-tab="artifacts">Artifacts</button>
  <a class="graphlink" id="graphlink" href="__EXPLORER_URL__/brain/graph" target="_blank" rel="noopener" title="Open the brain graph in a new tab">Graph ↗</a>
</nav>
<div id="toasts"></div>
<div class="wrap"><div id="view"><div class="skgrid"><div class="skel skel-card"></div><div class="skel skel-card"></div><div class="skel skel-card"></div></div></div></div>
<div class="basket" id="basket"></div>
<script>
"use strict";
const $=(id)=>document.getElementById(id);
const view=$("view");

// ── unified in-widget state. widgetState persists ONLY {view, projectId, path,
// basket}; on boot we re-fetch content from the tools regardless — the git repo
// is the truth, a restored payload is just a pointer. ──
let state={
  view:"home", projects:null, load:null, projectId:null, readPath:null, read:null,
  searchQuery:"", searchProject:null, searchResults:null, searchStatus:null,
  inbox:null, inboxStatus:null,
  artifacts:null, artifactsStatus:null,
  recipes:null, recipesStatus:null, artifactFilter:"all",
  basket:[]   // [{path, title}] — ordered
};
let booted=false, busy=false;

// ─────────────────────────────── height ───────────────────────────────
// Measure TRUE content height (temporarily height:max-content so the iframe can
// SHRINK too), report via BOTH channels, debounced through rAF. No internal
// scrolling — the host sizes the card to us.
function reportHeight(){
  const el=document.documentElement, prev=el.style.height; el.style.height="max-content";
  const h=Math.ceil(el.getBoundingClientRect().height); el.style.height=prev;
  try{ if(window.openai && typeof window.openai.notifyIntrinsicHeight==="function") window.openai.notifyIntrinsicHeight(h); }catch(e){}
  try{ window.parent.postMessage({jsonrpc:"2.0",method:"ui/notifications/size-changed",params:{width:Math.ceil(window.innerWidth),height:h}},"*"); }catch(e){}
}
let _rafH=0; function scheduleHeight(){ if(_rafH) cancelAnimationFrame(_rafH); _rafH=requestAnimationFrame(reportHeight); }
try{ new ResizeObserver(scheduleHeight).observe(document.body); }catch(e){ window.addEventListener("resize",scheduleHeight); }

// ─────────────────────────── MCP Apps bridge ──────────────────────────
// JSON-RPC over postMessage. The guard drops non-JSONRPC frames — claude.ai
// injects unrelated messages (e.g. auth_token) into the same channel.
let nextId=1; const pending=new Map();
window.addEventListener("message",(e)=>{
  const m=e.data; if(!m || m.jsonrpc!=="2.0") return;
  if(m.id!==undefined && pending.has(m.id)){
    const p=pending.get(m.id); pending.delete(m.id);
    if(m.error) p.reject(new Error((m.error&&m.error.message)||"rpc error")); else p.resolve(m.result);
    return;
  }
  if(m.method==="ui/notifications/tool-result"){
    const pr=m.params||{}; const d=unwrap(pr.structuredContent ?? parseText(pr.content));
    if(d) seedFrom(d);   // a fresh tool call remounted us with new data — reseed
  }
},{passive:true});
// The python SDK wraps list/dict tool returns in a single-key {"result": ...}
// envelope for structured content (mcp func_metadata) — unwrap it EVERYWHERE,
// or every shape check (Array.isArray, d.index_tree) silently fails on claude.ai.
function unwrap(v){ if(v && typeof v==="object" && !Array.isArray(v) && "result" in v && Object.keys(v).length===1) return v.result; return v; }
function rpcReq(method,params,timeout){ return new Promise((resolve,reject)=>{ const id=nextId++; pending.set(id,{resolve,reject});
  window.parent.postMessage({jsonrpc:"2.0",id,method,params},"*");
  setTimeout(()=>{ if(pending.has(id)){ pending.delete(id); reject(new Error("host timeout")); } }, timeout||120000); }); }
function rpcNote(method,params){ window.parent.postMessage({jsonrpc:"2.0",method,params},"*"); }
function parseText(content){ try{ const t=Array.isArray(content)?(content.find(c=>c.type==="text")||{}).text:null; return t?JSON.parse(t):null; }catch(e){ return null; } }

// robust tool result -> data (handles isError / structuredContent / content-array / raw / JSON string)
function normalize(r){ if(r==null) return null;
  if(typeof r==="object"){
    if(r.isError){ const t=(r.content||[]).find(c=>c&&c.type==="text"); return {error:(t&&t.text)||"tool error"}; }
    if(r.structuredContent) return unwrap(r.structuredContent);
    if(Array.isArray(r.content)){ const t=r.content.find(c=>c&&c.type==="text"); if(t){ try{ return unwrap(JSON.parse(t.text)); }catch(e){ return {error:t.text}; } } }
    return unwrap(r); }
  if(typeof r==="string"){ try{ return unwrap(JSON.parse(r)); }catch(e){ return null; } }
  return null; }
async function callTool(name,args){
  // ChatGPT exposes window.openai.callTool; MCP Apps hosts relay tools/call over the bridge.
  if(window.openai && window.openai.callTool){ return normalize(await window.openai.callTool(name,args||{})); }
  return normalize(await rpcReq("tools/call",{name,arguments:args||{}}));
}
// re-engage the agent. content MUST be an ARRAY of blocks — a single object
// silently fails to wake Claude (hard-won from the Survey widget). Falls back to
// ChatGPT's sendFollowUpMessage.
async function askAgent(text){
  try{ await rpcReq("ui/message",{role:"user",content:[{type:"text",text:text}]},6000); return true; }catch(e){}
  try{ if(window.openai && typeof window.openai.sendFollowUpMessage==="function"){ window.openai.sendFollowUpMessage({prompt:text}); return true; } }catch(e){}
  return false;
}
function persist(){ try{ if(window.openai && typeof window.openai.setWidgetState==="function")
  window.openai.setWidgetState({view:state.view, projectId:state.projectId, path:state.readPath, basket:state.basket}); }catch(e){} }

// ─────────────────────────── helpers ──────────────────────────
function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
const TYPE_GLYPHS={decision:"⚖",spec:"📐",runbook:"🛠",person:"👤",client:"🏢",video:"🎬",message:"✉",reference:"🔖",project:"📁",idea:"💡",meeting:"🗓",snippet:"✂",note:"📝",artifact:"📄"};
function glyph(t){ return TYPE_GLYPHS[String(t||"").trim().toLowerCase()]||"◆"; }
function statusDot(s){ s=String(s||"").toLowerCase();
  if(["active","settled","paid","published"].includes(s)) return "s-green";
  if(["done","read"].includes(s)) return "s-blue";
  if(s==="unread") return "s-accent";
  if(["tentative","superseded","archived","draft"].includes(s)) return "s-amber";
  if(["blocked","expired"].includes(s)) return "s-red";
  return ""; }
function relTime(d){ if(!d) return ""; const t=Date.parse((""+d).length<=10?d+"T00:00:00Z":d); if(isNaN(t)) return esc(d);
  const days=Math.floor((Date.now()-t)/86400000);
  if(days<=0) return "today"; if(days===1) return "yesterday"; if(days<7) return days+" days ago";
  if(days<30) return Math.floor(days/7)+"w ago"; if(days<365) return Math.floor(days/30)+"mo ago"; return Math.floor(days/365)+"y ago"; }
function dirOf(p){ const i=(p||"").lastIndexOf("/"); return i<0?"":p.slice(0,i); }
// POSIX path resolve, matching kbstore's normpath(join(base, rel)); anchors stripped.
function resolveRel(baseDir, rel){ rel=(rel||"").split("#")[0];
  const stack = rel.startsWith("/") ? [] : (baseDir?baseDir.split("/").filter(Boolean):[]);
  for(const seg of rel.split("/")){ if(seg===""||seg===".") continue; if(seg==="..") stack.pop(); else stack.push(seg); }
  return stack.join("/"); }
// strip a leading YAML frontmatter block (--- ... ---) so we render the body only
function stripFrontmatter(src){ src=(src||"").replace(/\r\n/g,"\n");
  if(src.startsWith("---\n")){ const end=src.indexOf("\n---",3); if(end!==-1){ const nl=src.indexOf("\n",end+1); return src.slice(nl===-1?src.length:nl+1); } }
  return src; }

// skeleton + state helpers
function skelRows(n){ let s=""; for(let i=0;i<(n||4);i++) s+='<div class="skel skel-row"></div>'; return '<div class="sklist">'+s+'</div>'; }
function skelCards(n){ let s=""; for(let i=0;i<(n||6);i++) s+='<div class="skel skel-card"></div>'; return '<div class="skgrid">'+s+'</div>'; }
function errBlock(msg, retryKey){ return '<div class="errbox"><span>'+esc(msg)+'</span><button class="retry" data-retry="'+esc(retryKey)+'">Retry</button></div>'; }
function showToast(msg, retryFn){
  const host=$("toasts"); if(!host) return;
  const t=document.createElement("div"); t.className="toast";
  const sp=document.createElement("span"); sp.textContent=msg; t.appendChild(sp);
  if(retryFn){ const b=document.createElement("button"); b.className="toast-retry"; b.textContent="Retry"; b.onclick=()=>{ t.remove(); retryFn(); }; t.appendChild(b); }
  host.appendChild(t); requestAnimationFrame(()=>t.classList.add("on"));
  setTimeout(()=>{ t.classList.remove("on"); setTimeout(()=>t.remove(),300); }, retryFn?6000:3200);
}

// ─────────────────── small markdown renderer ───────────────────
// Escapes HTML first, then applies a safe subset: headings, bold/italic, inline
// + fenced code, lists (nested), links, blockquote, hr. Relative .md links become
// in-widget navigation; http(s) links open in a new tab.
function mdInline(s){
  // split on inline-code spans so bold/italic never reach inside code
  return s.split(/(`[^`]+`)/g).map(p=>{
    if(p.length>1 && p[0]==="`" && p[p.length-1]==="`") return "<code>"+p.slice(1,-1)+"</code>";
    p=p.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g,(m,txt,url)=>linkHtml(txt,url));
    p=p.replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>").replace(/__([^_]+)__/g,"<strong>$1</strong>");
    p=p.replace(/(^|[^*\w])\*(?!\s)([^*]+?)\*(?!\w)/g,"$1<em>$2</em>").replace(/(^|[^_\w])_(?!\s)([^_]+?)_(?!\w)/g,"$1<em>$2</em>");
    return p;
  }).join("");
}
function linkHtml(txt,url){ // txt & url arrive already HTML-escaped
  if(/^https?:/i.test(url)) return '<a href="'+url+'" target="_blank" rel="noopener">'+txt+'</a>';
  const bare=url.split("#")[0];
  if(bare && !/^[a-z]+:/i.test(bare) && /\.md$/i.test(bare)) return '<a href="#" class="mdlink" data-rel="'+url+'">'+txt+'</a>';
  return txt; // mailto/anchors/unknown schemes -> inert label (sandbox blocks them anyway)
}
function renderMarkdown(src){
  const lines=esc(stripFrontmatter(src)).split("\n");
  let html="", i=0;
  while(i<lines.length){
    let line=lines[i];
    if(/^\s*$/.test(line)){ i++; continue; }
    let fence=line.match(/^\s*```(.*)$/);
    if(fence){ i++; let buf=[]; while(i<lines.length && !/^\s*```\s*$/.test(lines[i])){ buf.push(lines[i]); i++; } i++;
      html+="<pre><code>"+buf.join("\n")+"</code></pre>"; continue; }
    if(/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)){ html+="<hr>"; i++; continue; }
    let h=line.match(/^\s*(#{1,6})\s+(.*)$/);
    if(h){ const n=h[1].length; html+="<h"+n+">"+mdInline(h[2].trim())+"</h"+n+">"; i++; continue; }
    // blockquote — the source is already HTML-escaped, so '>' is now '&gt;'
    if(/^\s*&gt;\s?/.test(line)){ let buf=[]; while(i<lines.length && /^\s*&gt;\s?/.test(lines[i])){ buf.push(lines[i].replace(/^\s*&gt;\s?/,"")); i++; }
      html+="<blockquote>"+mdInline(buf.join(" "))+"</blockquote>"; continue; }
    if(/^\s*([-*+]|\d+\.)\s+/.test(line)){ let items=[]; while(i<lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])){
        const m=lines[i].match(/^(\s*)([-*+]|\d+\.)\s+(.*)$/);
        items.push({indent:m[1].replace(/\t/g,"  ").length, ordered:/\d/.test(m[2]), text:m[3]}); i++; }
      html+=buildList(items); continue; }
    let buf=[]; while(i<lines.length && !/^\s*$/.test(lines[i]) && !/^\s*(#{1,6}\s|&gt;|```|([-*+]|\d+\.)\s|([-*_])(\s*\3){2,}\s*$)/.test(lines[i])){ buf.push(lines[i]); i++; }
    html+="<p>"+mdInline(buf.join(" ").trim())+"</p>";
  }
  return html;
}
function buildList(items){ // indent-stack nesting
  let out="", stack=[];
  for(const it of items){
    while(stack.length && it.indent < stack[stack.length-1].indent){ out+=(stack.pop().ordered?"</ol>":"</ul>"); }
    if(!stack.length || it.indent > stack[stack.length-1].indent){ out+=(it.ordered?"<ol>":"<ul>"); stack.push({indent:it.indent,ordered:it.ordered}); }
    out+="<li>"+mdInline(it.text)+"</li>";
  }
  while(stack.length){ out+=(stack.pop().ordered?"</ol>":"</ul>"); }
  return out;
}

// ─────────────────────── basket (the differentiator) ──────────────────────
const ARTIFACT_TYPES=[
  {v:"status-report", t:"Status report"},
  {v:"spec", t:"Spec document"},
  {v:"blog", t:"Blog post"},
  {v:"summary", t:"Summary"},
  {v:"handoff", t:"Handoff brief"},
  {v:"custom", t:"Custom…"}
];
// per-type craft direction — shared by BUILD (drives artifact quality) and
// SAVE-AS-RECIPE (becomes the recipe's stored `instruction`).
const CRAFT_BRIEF={
  "status-report":"a polished STATUS REPORT: title, date, at-a-glance summary box, then sections (Current state / Decisions & rationale / Open items / Next steps); use tables or status chips where the data is list-like",
  spec:"a clean SPEC DOCUMENT: numbered sections, requirement tables, architecture described precisely; professional technical-doc typography",
  blog:"an engaging BLOG POST in Hiren's metalfinger voice: strong hook, subheads, short paragraphs, a closing takeaway",
  summary:"a crisp EXECUTIVE SUMMARY: one-screen overview, key points as scannable bullets, a bottom-line verdict",
  handoff:"a HANDOFF BRIEF for the next person/session: context in one paragraph, current state, exact next actions as a checklist, gotchas called out",
};
// project owning a brain path is projects/<project>/…
function projectOf(p){ const parts=(p||"").split("/"); return (parts[0]==="projects"&&parts[1])?parts[1]:""; }
// dominant project across the basket; ties (and mixed) fall back to the first path's project
function dominantProject(){
  const counts={}; let first="";
  for(const b of state.basket){ const pj=projectOf(b.path); if(!first&&pj) first=pj; if(pj) counts[pj]=(counts[pj]||0)+1; }
  let best=first, bestN=-1;
  for(const k in counts){ if(counts[k]>bestN){ bestN=counts[k]; best=k; } }
  return best||first||"<project>";
}
function inBasket(path){ return state.basket.some(x=>x.path===path); }
function bkBtn(path,title){ const on=inBasket(path);
  return '<button class="bk-toggle'+(on?" on":"")+'" data-bk-path="'+esc(path)+'" data-bk-title="'+esc(title||"")+'" title="'+(on?"In basket":"Add to basket")+'" aria-label="Add to basket">'+(on?"✓":"＋")+'</button>'; }
function toggleBasket(path, title){
  const i=state.basket.findIndex(x=>x.path===path); const added=(i<0);
  if(added) state.basket.push({path, title:title||path.split("/").pop()}); else state.basket.splice(i,1);
  document.querySelectorAll("[data-bk-path]").forEach(el=>{ if(el.getAttribute("data-bk-path")===path){ el.classList.toggle("on",added); el.textContent=added?"✓":"＋"; el.title=added?"In basket":"Add to basket"; } });
  persist(); renderBasket();
}
function moveBasket(i,d){ const j=i+d; if(j<0||j>=state.basket.length) return; const a=state.basket; const t=a[i]; a[i]=a[j]; a[j]=t; persist(); renderBasket(); }
function clearBasket(){ state.basket=[]; document.querySelectorAll("[data-bk-path]").forEach(el=>{ el.classList.remove("on"); el.textContent="＋"; el.title="Add to basket"; }); persist(); renderBasket(); }
// the chosen type's instruction text — a custom instruction, or the craft brief.
function chosenInstruction(){
  const sel=$("bk-type"); const v=sel?sel.value:"summary";
  if(v==="custom"){ const ci=$("bk-custom"); return (ci&&ci.value.trim())||"Write a useful document"; }
  return CRAFT_BRIEF[v]||CRAFT_BRIEF.summary;
}
async function buildArtifact(){
  if(!state.basket.length) return;
  const sel=$("bk-type"); const v=sel?sel.value:"summary";
  const paths=state.basket.map((b,i)=>(i+1)+". "+b.path).join("\n");
  // The handoff prompt controls artifact QUALITY: demand a real Artifact (never
  // chat text) and give per-type craft direction — Claude renders what we ask for.
  let msg;
  const quality=" Create it as a proper ARTIFACT (side-panel document — never plain chat text). Make it genuinely well-designed: clear hierarchy, scannable structure; prefer rich formatted markdown, or a styled HTML artifact when visuals (tables, status colors, timelines) would materially help. Cite the source paths in a small footer. After presenting the artifact, OFFER to save it into the brain: kb_write to projects/<project>/artifacts/YYYY-MM-<slug>.md with frontmatter type: artifact, sources: [the exact paths above], instruction: <the instruction you were given> — the server stamps provenance; then it appears in the Artifacts tab. If you built an HTML artifact, save the experience VERBATIM: add format: html to the frontmatter and make the body the COMPLETE HTML document — its share link then serves the real interactive page.";
  if(v==="custom"){ const instr=chosenInstruction();
    msg=instr+"\n\nUse ONLY these knowledge-base concepts — kb_read each path first, use only their content."+quality+"\n"+paths;
  } else { const c=CRAFT_BRIEF[v]||CRAFT_BRIEF.summary;
    msg="From these knowledge-base concepts — kb_read each path first, use only their content — build "+c+"."+quality+"\n"+paths;
  }
  const ok=await askAgent(msg);
  showToast(ok?"Sent to Claude — building your artifact":"Couldn't reach Claude — try again", ok?null:buildArtifact);
}
// SAVE-AS-RECIPE: persist the basket (ordered sources) + chosen instruction as a
// reusable, rebuildable recipe concept. content MUST be an ARRAY of blocks (askAgent).
async function saveRecipe(){
  if(!state.basket.length) return;
  const proj=dominantProject();
  const month=new Date().toISOString().slice(0,7);   // YYYY-MM
  const paths=state.basket.map((b,i)=>(i+1)+". "+b.path).join("\n");
  const msg="Save this as a reusable recipe: kb_write to projects/"+proj+"/recipes/"+month+"-<slug>.md"
    +" with frontmatter type: recipe, sources: [the ordered paths below], instruction: "+chosenInstruction()+"."
    +" Recipes are rebuildable anytime with the rebuild_artifact prompt.\n"+paths;
  const ok=await askAgent(msg);
  showToast(ok?"Sent to Claude — saving your recipe":"Couldn't reach Claude — try again", ok?null:saveRecipe);
}
function renderBasket(){
  const bar=$("basket"); if(!bar) return;
  updateTabBadges();
  if(!state.basket.length){ bar.classList.remove("on"); bar.innerHTML=""; scheduleHeight(); return; }
  const chips=state.basket.map((b,i)=>
    '<span class="bkchip"><button class="bkup" data-mv="'+i+',-1" title="Move up" aria-label="Move up">▲</button>'
    +'<button class="bkdn" data-mv="'+i+',1" title="Move down" aria-label="Move down">▼</button>'
    +'<span class="bkt" title="'+esc(b.path)+'">'+esc(b.title||b.path.split("/").pop())+'</span>'
    +'<button class="bkx" data-rm="'+esc(b.path)+'" title="Remove" aria-label="Remove">×</button></span>').join("");
  const opts=ARTIFACT_TYPES.map(t=>'<option value="'+t.v+'">'+esc(t.t)+'</option>').join("");
  bar.innerHTML='<div class="bkhead"><span class="eyebrow" style="margin:0">Basket · '+state.basket.length+'</span>'
    +'<button class="bkclear" id="bk-clear">Clear all</button></div>'
    +'<div class="bkchips">'+chips+'</div>'
    +'<div class="bkbuild"><select id="bk-type" class="bksel" aria-label="Artifact type">'+opts+'</select>'
    +'<input id="bk-custom" class="bkcustom" placeholder="Custom instruction…" hidden>'
    +'<button class="build" id="bk-build">Build artifact</button>'
    +'<button class="save-recipe" id="bk-recipe" title="Save the sources + instruction as a reusable recipe">Save as recipe</button></div>';
  bar.classList.add("on");
  $("bk-clear").onclick=clearBasket;
  $("bk-build").onclick=buildArtifact;
  $("bk-recipe").onclick=saveRecipe;
  const sel=$("bk-type"); sel.onchange=()=>{ $("bk-custom").hidden=(sel.value!=="custom"); scheduleHeight(); };
  bar.querySelectorAll("[data-mv]").forEach(b=>b.onclick=()=>{ const p=b.getAttribute("data-mv").split(","); moveBasket(+p[0],+p[1]); });
  bar.querySelectorAll("[data-rm]").forEach(b=>b.onclick=()=>toggleBasket(b.getAttribute("data-rm"),null));
  scheduleHeight();
}

// ─────────────────────────── views ──────────────────────────
function unreadCount(){ if(state.inbox) return state.inbox.length; return (state.projects||[]).reduce((n,p)=>n+(p.unread_messages|0),0); }
function updateTabBadges(){ const n=unreadCount(); const el=$("ib-badge"); if(el){ el.textContent=n>0?n:""; el.style.display=n>0?"inline-grid":"none"; } }
function setActiveTab(){ const map={reader:"browse"}; const cur=map[state.view]||state.view;
  document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("active", t.dataset.tab===cur)); }
function show(html){ view.innerHTML=html; setActiveTab(); updateTabBadges(); scheduleHeight(); }

function renderHome(){
  state.view="home"; persist(); setActiveTab();
  const ps=state.projects||[];
  let cards = ps.length ? ps.map(p=>{
    const unread=(p.unread_messages|0);
    return '<button class="card" data-proj="'+esc(p.id)+'">'
      +'<div class="card-head"><span class="dot '+statusDot(p.status)+'"></span><h3>'+esc(p.title||p.id)+'</h3></div>'
      +(p.description?'<p>'+esc(p.description)+'</p>':'')
      +'<div class="card-foot">'
        +(unread>0?'<span class="badge unread">'+unread+' unread</span>':'')
        +(p.last_session?'<span class="when">'+esc(relTime(p.last_session))+'</span>':'')
      +'</div></button>';
  }).join("") : '<div class="empty">No projects found.</div>';
  show('<div class="qcap"><input id="qc-input" class="qc-in" type="text" placeholder="Quick capture — remember anything…" autocomplete="off" aria-label="Quick capture to inbox">'
        +'<button class="qc-go" id="qc-go">Capture</button></div>'
      +'<p class="eyebrow">Knowledge Base</p><h1>brain</h1>'
      +'<p class="lede">Pick a project to browse its context, concepts, and log.</p>'
      +'<div class="cards">'+cards+'</div>');
  const qi=$("qc-input"), qg=$("qc-go");
  if(qg) qg.onclick=quickCapture;
  if(qi) qi.onkeydown=(e)=>{ if(e.key==="Enter"){ e.preventDefault(); quickCapture(); } };
}
// QUICK CAPTURE: fire-and-forget note into the inbox via kb_inbox. Graceful when
// the tool isn't live yet (older server) — toast instead of a hard error.
async function quickCapture(){
  const inp=$("qc-input"); if(!inp) return; const text=inp.value.trim(); if(!text) return;
  const go=$("qc-go"); inp.disabled=true; if(go) go.disabled=true;
  let errMsg=null;
  try{ const r=await callTool("kb_inbox",{text:text}); if(r && r.error) errMsg=String(r.error); }
  catch(e){ errMsg=String((e&&e.message)||"tool error"); }
  inp.disabled=false; if(go) go.disabled=false;
  if(errMsg){
    if(/unknown.?tool|not.?found|no such tool|not live|unrecognized|method not found/i.test(errMsg)) showToast("Inbox tool not live yet");
    else showToast("Couldn't capture — try again", quickCapture);
    return;
  }
  inp.value=""; showToast("Captured to inbox"); try{ inp.focus(); }catch(e){}
}

function treeHtml(node, depth){
  const files=(node.files||[]).map(f=>
    '<div class="frow"><span class="open" data-open-path="'+esc(f.path)+'">'
      +'<span class="glyph">◦</span><span class="ft">'+esc(f.title||f.name)+'</span>'
      +(f.description?'<span class="fd">— '+esc(f.description)+'</span>':'')
    +'</span>'+bkBtn(f.path, f.title||f.name)+'</div>').join("");
  const dirs=(node.dirs||[]).map(d=>
    '<details class="tnode"'+(depth<1?" open":"")+'>'
      +'<summary>'+esc(d.title||d.name)+'</summary>'
      +'<div class="kids">'+treeHtml(d,depth+1)+'</div>'
    +'</details>').join("");
  return (dirs+files) || '<div class="empty">Empty.</div>';
}

function renderBrowse(){
  state.view="browse"; state.readPath=null; state.read=null; persist(); setActiveTab();
  const L=state.load; if(!L){ renderHome(); return; }
  const tree=L.index_tree||{files:[],dirs:[]};
  const title=tree.title||L.project;
  let ctx="";
  if(L.context_md){ const body=stripFrontmatter(L.context_md).split("\n").slice(0,40).join("\n");
    ctx='<div class="ctx fade"><div class="md">'+renderMarkdown(body)+'</div></div>'; }
  show('<div class="bhead"><button class="back" data-nav="home">‹ Home</button>'
        +'<span class="stamp">'+glyph("project")+'</span>'
        +'<div><p class="eyebrow" style="margin:0">Project</p><h1 style="font-size:1.1rem">'+esc(title)+'</h1></div></div>'
      +ctx
      +'<p class="section-label">Concepts</p><div class="tree">'+treeHtml(tree,0)+'</div>');
}

// supersession is legible in the reader: a "superseded by →" chip (and a "supersedes"
// chip) linking via the in-widget open flow, plus a muted, struck title + an amber
// note banner when this concept has been replaced. Fields render defensively (absent
// = nothing); each accepts a single path or a list.
function asPaths(v){ if(Array.isArray(v)) return v.map(x=>String(x)); return (v==null||v==="")?[]:[String(v)]; }
function supersedeChips(meta){
  let s="";
  asPaths(meta.superseded_by).forEach(p=>{ s+='<span class="chip supersede" data-open-path="'+esc(p)+'" title="Superseded by '+esc(p)+'">superseded by → '+esc(p.split("/").pop())+'</span>'; });
  asPaths(meta.supersedes).forEach(p=>{ s+='<span class="chip supersedes-chip" data-open-path="'+esc(p)+'" title="Supersedes '+esc(p)+'">supersedes '+esc(p.split("/").pop())+'</span>'; });
  return s;
}
function supersedeNote(meta){
  const by=asPaths(meta.superseded_by); if(!by.length) return "";
  const links=by.map(p=>'<span class="snl" data-open-path="'+esc(p)+'">'+esc(p.split("/").pop())+'</span>').join(", ");
  const asof=meta.valid_until?' <span class="when">as of '+esc(meta.valid_until)+'</span>':"";
  return '<div class="supersede-note"><span aria-hidden="true">⚠</span><span>Superseded by '+links+asof+'</span></div>';
}
function renderReader(){
  state.view="reader"; persist(); setActiveTab();
  const R=state.read; if(!R){ renderBrowse(); return; }
  const meta=R.meta||{}; const path=R.path||state.readPath||""; const name=path.split("/").pop();
  const superseded=(String(meta.confidence||"")==="superseded")||asPaths(meta.superseded_by).length>0;
  let props="";
  if(meta.type) props+='<span class="chip"><span class="k">type</span> '+esc(meta.type)+'</span>';
  if(meta.status) props+='<span class="chip"><span class="statusv"><span class="dot '+statusDot(meta.status)+'"></span>'+esc(meta.status)+'</span></span>';
  if(meta.confidence) props+='<span class="chip"><span class="k">conf</span> '+esc(meta.confidence)+'</span>';
  if(meta.valid_until) props+='<span class="chip"><span class="k">until</span> '+esc(meta.valid_until)+'</span>';
  props+=supersedeChips(meta);
  const tags=Array.isArray(meta.tags)?meta.tags:(meta.tags?[meta.tags]:[]);
  props+=tags.map(t=>'<span class="chip tag">'+esc(t)+'</span>').join("");
  const projLabel=(state.load&&state.load.index_tree&&state.load.index_tree.title)||state.projectId||"project";
  const crumbs='<button data-nav="browse">‹ back</button><span class="sep">/</span>'
      +'<button data-nav="browse">'+esc(projLabel)+'</button>'
      +'<span class="sep">/</span><span class="cur">'+esc(meta.title||name)+'</span>';
  show('<div class="crumbs">'+crumbs+'</div>'
      +'<div class="rhead'+(superseded?" superseded":"")+'"><span class="stamp">'+glyph(meta.type)+'</span>'
        +'<div class="rh-main"><h1>'+esc(meta.title||name)+'</h1>'
        +(meta.description?'<p class="lede">'+esc(meta.description)+'</p>':'')+'</div>'
        +bkBtn(path, meta.title||name)+'</div>'
      +supersedeNote(meta)
      +(props?'<div class="props">'+props+'</div>':'')
      +readerBody(meta, R.content||""));
}
// HTML artifacts are experiences, not documents: never dump their source into the
// reader — link to the rendered page (share URL serves it verbatim).
function readerBody(meta, content){
  if(String(meta.format||"")!=="html") return '<div class="md">'+renderMarkdown(content)+'</div>';
  var body=content.replace(/^---[\s\S]*?---\s*/,"");
  var open=meta.share
    ? '<p><button class="act" data-view-url="__EXPLORER_URL__/share/'+esc(meta.share)+'">Open rendered page ↗</button></p>'
    : '<p class="empty">This is an HTML artifact — Share it (Artifacts tab) to get a rendered, viewable page.</p>';
  return open+'<div class="md"><details><summary>HTML source ('+body.length+' chars)</summary><pre>'+esc(body.slice(0,20000))+'</pre></details></div>';
}

// ── SEARCH ──
let _searchTimer=0;
function onSearchInput(v){ state.searchQuery=v; if(_searchTimer) clearTimeout(_searchTimer);
  _searchTimer=setTimeout(runSearch, 300); }   // 300ms debounce
function onSearchEnter(){ if(_searchTimer) clearTimeout(_searchTimer); runSearch(); }
async function runSearch(){
  const q=(state.searchQuery||"").trim();
  if(!q){ state.searchResults=null; state.searchStatus=null; renderSearchBody(); return; }
  state.searchStatus="loading"; renderSearchBody();
  try{
    const args={query:q, limit:12}; if(state.searchProject) args.project=state.searchProject;
    const d=await callTool("kb_search", args);
    if(d && d.error) throw new Error(d.error);
    state.searchResults=Array.isArray(d)?d:[]; state.searchStatus="ok";
  }catch(e){ state.searchStatus="error"; }
  renderSearchBody();
}
function renderSearch(){
  state.view="search"; persist(); setActiveTab();
  const projOpts='<option value="">All projects</option>'+(state.projects||[]).map(p=>
    '<option value="'+esc(p.id)+'"'+(state.searchProject===p.id?" selected":"")+'>'+esc(p.title||p.id)+'</option>').join("");
  show('<p class="eyebrow">Search</p>'
    +'<div class="searchbar"><input id="sq" class="sinput" type="search" placeholder="Search the brain…" value="'+esc(state.searchQuery||"")+'" autocomplete="off">'
    +'<select id="sproj" class="sselect" aria-label="Project filter">'+projOpts+'</select></div>'
    +'<div id="sresults"></div>');
  const sq=$("sq"); sq.oninput=()=>onSearchInput(sq.value); sq.onkeydown=(e)=>{ if(e.key==="Enter") onSearchEnter(); };
  const sp=$("sproj"); sp.onchange=()=>{ state.searchProject=sp.value||null; runSearch(); };
  renderSearchBody();
  try{ sq.focus(); }catch(e){}
}
function renderSearchBody(){
  const host=$("sresults"); if(!host) return;
  if(state.searchStatus==="loading"){ host.innerHTML=skelRows(4); scheduleHeight(); return; }
  if(state.searchStatus==="error"){ host.innerHTML=errBlock("Search failed.","search"); scheduleHeight(); return; }
  if(!state.searchResults){ host.innerHTML='<div class="empty">Type to search titles, descriptions, tags, headings, and body.</div>'; scheduleHeight(); return; }
  if(!state.searchResults.length){ host.innerHTML='<div class="empty">No matches for “'+esc((state.searchQuery||"").trim())+'”.</div>'; scheduleHeight(); return; }
  const maxScore=Math.max(1, ...state.searchResults.map(r=>+r.score||0));
  host.innerHTML=state.searchResults.map(r=>{
    const parts=(r.path||"").split("/"); const nm=parts[parts.length-1];
    const crumb=parts.map(esc).join('<span class="sep">/</span>');
    const pct=Math.round(Math.max(0,Math.min(1,(+r.score||0)/maxScore))*100);
    return '<div class="result" data-open-path="'+esc(r.path)+'">'
      +'<div class="rtop"><h3>'+esc(r.title||nm)+'</h3>'+bkBtn(r.path, r.title||nm)+'</div>'
      +'<div class="rpath">'+crumb+'</div>'
      +(r.description?'<p>'+esc(r.description)+'</p>':'')
      +(r.matched_heading?'<span class="chip">§ '+esc(r.matched_heading)+'</span>':'')
      +'<div class="scorebar"><div class="track"><div class="fill" style="width:'+pct+'%"></div></div><span class="pct">'+pct+'%</span></div>'
    +'</div>';
  }).join("");
  scheduleHeight();
}

// ── INBOX ──
async function loadInbox(force){
  if(state.inbox && !force){ renderInbox(); return; }
  state.inboxStatus="loading"; renderInbox();
  try{
    let ps=state.projects; if(!Array.isArray(ps)){ ps=await callTool("kb_projects",{}); if(Array.isArray(ps)) state.projects=ps; }
    const withUnread=(state.projects||[]).filter(p=>(p.unread_messages|0)>0);
    const rows=[];
    for(const p of withUnread){
      const L=await callTool("kb_load",{project:p.id});   // only projects with unread — cheap fan-out
      if(L && Array.isArray(L.unread_messages)){
        for(const m of L.unread_messages){ rows.push(Object.assign({}, m, {project:p.id, projectTitle:p.title||p.id})); }
      }
    }
    // high priority first, then unexpired before expired
    rows.sort((a,b)=>((b.priority==="high")-(a.priority==="high")) || ((a.expired?1:0)-(b.expired?1:0)));
    state.inbox=rows; state.inboxStatus="ok"; renderInbox();
  }catch(e){ state.inboxStatus="error"; renderInbox(); }
}
function inboxRow(m){
  const chips='<span class="chip proj">'+esc(m.projectTitle||m.project)+'</span>'
    +(m.to&&m.to!=="any"?'<span class="chip"><span class="k">to</span> '+esc(m.to)+'</span>':'')
    +(m.priority&&m.priority!=="normal"?'<span class="chip prio-'+esc(m.priority)+'">'+esc(m.priority)+'</span>':'')
    +(m.expires?'<span class="chip'+(m.expired?" dim":"")+'"><span class="k">exp</span> '+esc(m.expires)+'</span>':'');
  return '<details class="imsg'+(m.expired?" expired":"")+'">'
    +'<summary><span class="ititle">'+esc(m.title||"(untitled)")+'</span><span class="ichips">'+chips+'</span></summary>'
    +'<div class="ibody md">'+renderMarkdown(m.body||"")+'</div>'
    +'<div class="iacts"><button class="act" data-act="'+esc(m.path)+'">Act on it</button>'
    +'<button class="arch" data-arch="'+esc(m.path)+'">Archive</button></div>'
    +'</details>';
}
function renderInbox(){
  state.view="inbox"; persist(); setActiveTab();
  let body;
  if(state.inboxStatus==="loading"){ body=skelRows(3); }
  else if(state.inboxStatus==="error"){ body=errBlock("Couldn't load the inbox.","inbox"); }
  else { const rows=state.inbox||[];
    body = rows.length ? rows.map(inboxRow).join("")
      : '<div class="empty">Inbox zero — no unread messages across your projects.</div>'; }
  show('<div class="ihead"><p class="eyebrow" style="margin:0">Inbox</p><button class="refresh" id="ib-refresh">⟳ Refresh</button></div>'+body);
  const rf=$("ib-refresh"); if(rf) rf.onclick=()=>loadInbox(true);
}
async function archiveMsg(path){
  const idx=(state.inbox||[]).findIndex(m=>m.path===path); if(idx<0) return;
  const removed=state.inbox[idx];
  state.inbox.splice(idx,1); renderInbox();   // optimistic removal
  try{ const r=await callTool("kb_mark_read",{message_path:path}); if(r&&r.error) throw new Error(r.error); showToast("Archived."); }
  catch(e){ state.inbox.splice(idx,0,removed); renderInbox(); showToast("Couldn't archive — try again", ()=>archiveMsg(path)); }   // rollback
}
async function actMsg(path){
  const m=(state.inbox||[]).find(x=>x.path===path); if(!m) return;
  const text="Act on this inter-session message from the brain: "+path+" — "+(m.title||"")+"\n"+(m.body||"");
  const ok=await askAgent(text);
  showToast(ok?"Handed to Claude":"Couldn't reach Claude — try again", ok?null:()=>actMsg(path));
}

// ── ARTIFACTS ──
// Saved documents (type: artifact) built from the basket, with provenance. Rows open
// in the Reader; Share mints a PUBLIC link (kb_share_artifact), Unshare revokes it.
async function loadArtifacts(force){
  if(state.artifacts && !force){ renderArtifacts(); return; }
  state.artifactsStatus="loading"; renderArtifacts();
  try{
    const d=await callTool("kb_artifacts",{});
    if(d && d.error) throw new Error(d.error);
    state.artifacts=Array.isArray(d)?d:[]; state.artifactsStatus="ok";
  }catch(e){ state.artifactsStatus="error"; }
  renderArtifacts();
}
function artifactRow(a){
  const stale=(a.stale===true)?'<span class="chip stale">sources changed</span>':"";
  const shared=a.shared?'<span class="chip shared">🔗 shared</span>':"";
  const when=a.timestamp?'<span class="when">'+esc(relTime(a.timestamp))+'</span>':"";
  // HTML artifacts open their rendered page directly (the share URL serves it verbatim)
  const view=(String(a.format||"")==="html" && a.shared && a.share_url)
    ? '<button class="act" data-view-url="'+esc(a.share_url)+'">View ↗</button>' : "";
  const acts=view+(a.shared
    ? (a._confirm
        ? '<button class="arch" data-unshare="'+esc(a.path)+'">Confirm unshare</button>'
        : '<button class="arch" data-unshare="'+esc(a.path)+'">Unshare</button>')
    : '<button class="act" data-share="'+esc(a.path)+'">Share</button>');
  const box=(a.shared && a.share_url)
    ? '<div class="sharebox"><code class="share-url">'+esc(a.share_url)+'</code></div>' : "";
  return '<div class="artrow">'
    +'<div class="artmain" data-open-path="'+esc(a.path)+'">'
      +'<div class="arthead"><span class="stamp">'+glyph("artifact")+'</span><span class="artt">'+esc(a.title||a.path.split("/").pop())+'</span></div>'
      +'<div class="artchips"><span class="chip proj">'+esc(a.project||"")+'</span>'+stale+shared+when+'</div>'
    +'</div>'
    +'<div class="artacts">'+acts+'</div>'
    +box
  +'</div>';
}
// RECIPES: best-effort listing. kb_artifacts scans artifacts/ only; recipes live
// in recipes/, so we surface them via kb_search(type:"recipe") until a dedicated
// kb_recipes tool exists — hence the "beta" chip. Tap opens the Reader; Run hands
// the recipe back to the agent to rebuild from its current sources.
async function loadRecipes(force){
  if(state.recipes && !force){ renderArtifacts(); return; }
  state.recipesStatus="loading"; renderArtifacts();
  try{
    const d=await callTool("kb_search",{query:"recipe", type:"recipe", limit:25});
    if(d && d.error) throw new Error(d.error);
    state.recipes=Array.isArray(d)?d:[]; state.recipesStatus="ok";
  }catch(e){ state.recipesStatus="error"; state.recipes=state.recipes||[]; }
  renderArtifacts();
}
function recipeRow(r){
  const nm=(r.path||"").split("/").pop();
  const proj=projectOf(r.path)||r.project||"";
  return '<div class="artrow">'
    +'<div class="artmain" data-open-path="'+esc(r.path)+'">'
      +'<div class="arthead"><span class="stamp">'+glyph("runbook")+'</span><span class="artt">'+esc(r.title||nm)+'</span></div>'
      +'<div class="artchips">'+(proj?'<span class="chip proj">'+esc(proj)+'</span>':'')+'</div>'
    +'</div>'
    +'<div class="artacts"><button class="act" data-run-recipe="'+esc(r.path)+'">Run</button></div>'
  +'</div>';
}
async function runRecipe(path){
  const proj=projectOf(path)||"<project>";
  const text="Run the recipe "+path+": kb_read it, kb_read its current sources, build per its instruction as a proper ARTIFACT, then offer to save the result to projects/"+proj+"/artifacts/.";
  const ok=await askAgent(text);
  showToast(ok?"Sent to Claude — running the recipe":"Couldn't reach Claude — try again", ok?null:()=>runRecipe(path));
}
function artifactsBody(){
  if(state.artifactsStatus==="loading") return skelRows(3);
  if(state.artifactsStatus==="error") return errBlock("Couldn't load artifacts.","artifacts");
  const rows=state.artifacts||[];
  return rows.length ? rows.map(artifactRow).join("")
    : '<div class="empty">No artifacts yet — build one from the basket.</div>';
}
function recipesBody(){
  if(state.recipesStatus==="loading") return skelRows(2);
  if(state.recipesStatus==="error") return errBlock("Couldn't load recipes.","recipes");
  const rows=state.recipes||[];
  return rows.length ? rows.map(recipeRow).join("")
    : '<div class="empty">No recipes found yet — save one from the basket.</div>';
}
function setFilter(f){ state.artifactFilter=f;
  if((f==="all"||f==="recipes") && !state.recipes && state.recipesStatus!=="loading"){ loadRecipes(false); return; }
  renderArtifacts();
}
function renderArtifacts(){
  state.view="artifacts"; persist(); setActiveTab();
  const f=state.artifactFilter||"all";
  const labels={all:"All", artifacts:"Artifacts", recipes:"Recipes"};
  const filterRow='<div class="filterrow">'+["all","artifacts","recipes"].map(k=>
    '<button class="fbtn'+(f===k?" on":"")+'" data-filter="'+k+'">'+labels[k]+'</button>').join("")+'</div>';
  let sections="";
  if(f==="all"||f==="artifacts"){ sections+='<p class="section-label">Artifacts</p>'+artifactsBody(); }
  if(f==="all"||f==="recipes"){ sections+='<p class="section-label">Recipes <span class="chip beta">beta</span></p>'+recipesBody(); }
  show('<div class="ihead"><p class="eyebrow" style="margin:0">Artifacts</p><button class="refresh" id="art-refresh">⟳ Refresh</button></div>'
    +filterRow+sections);
  const rf=$("art-refresh"); if(rf) rf.onclick=()=>{ loadArtifacts(true); if(f!=="artifacts") loadRecipes(true); };
  // lazily populate recipes the first time they're in view
  if((f==="all"||f==="recipes") && !state.recipes && state.recipesStatus!=="loading"){ loadRecipes(false); }
}
async function shareArtifact(path){
  try{
    const r=await callTool("kb_share_artifact",{path:path});
    if(r&&r.error) throw new Error(r.error);
    const a=(state.artifacts||[]).find(x=>x.path===path);
    if(a){ a.shared=true; a.share_url=r.share_url; a._confirm=false; }
    renderArtifacts();
    showToast("Public link created — anyone with the URL can read this");
  }catch(e){ showToast("Couldn't create link — try again", ()=>shareArtifact(path)); }
}
async function unshareArtifact(path){
  const a=(state.artifacts||[]).find(x=>x.path===path);
  if(a && !a._confirm){ a._confirm=true; renderArtifacts(); return; }   // inline confirm
  try{
    const r=await callTool("kb_unshare_artifact",{path:path});
    if(r&&r.error) throw new Error(r.error);
    if(a){ a.shared=false; a.share_url=null; a._confirm=false; }
    renderArtifacts();
    showToast("Link revoked — the URL no longer resolves");
  }catch(e){ if(a) a._confirm=false; showToast("Couldn't revoke — try again", ()=>unshareArtifact(path)); }
}

// ─────────────────────── async loaders ──────────────────────
async function loadHome(){ show(skelCards(6));
  try{ const d=await callTool("kb_projects",{}); if(Array.isArray(d)){ state.projects=d; renderHome(); } else show(errBlock("Could not load projects.","home")); }
  catch(e){ show(errBlock("Could not load projects.","home")); } }
async function openProject(id){ if(busy) return; busy=true; state.projectId=id; show(skelRows(5));
  try{ const d=await callTool("kb_load",{project:id}); if(d && d.index_tree){ state.load=d; renderBrowse(); } else show(errBlock("Could not load project.","project")); }
  catch(e){ show(errBlock("Could not load project.","project")); } finally{ busy=false; } }
async function openFile(path){ if(busy) return; busy=true; state.readPath=path; show(skelRows(6));
  try{ const d=await callTool("kb_read",{path:path}); if(d && d.content!=null){ state.read=d; renderReader(); } else show(errBlock("Could not read "+esc(path)+".","read")); }
  catch(e){ show(errBlock("Could not read file.","read")); } finally{ busy=false; } }

// ─────────────────── seeding & reconcile ─────────────────
// The widget mounts because a tagged tool was called; the host pushes that
// result in. Shape it into the right view.
function seedFrom(data){
  if(!data) return;
  booted=true;
  if(data.index_tree){ state.load=data; state.projectId=data.project||state.projectId; renderBrowse(); return; }
  if(Array.isArray(data)){
    if(data.length && data[0] && data[0].id!==undefined){ state.projects=data; renderHome(); return; }
    if(data.length && data[0] && data[0].path!==undefined && data[0].shared!==undefined){ state.artifacts=data; state.artifactsStatus="ok"; renderArtifacts(); return; }
    if(data.length && data[0] && data[0].path!==undefined && data[0].score!==undefined){ state.searchResults=data; state.searchStatus="ok"; renderSearch(); return; }
    loadHome(); return;   // empty/unknown array
  }
  if(data.content!==undefined){ state.read=data; state.readPath=data.path; renderReader(); return; }
  loadHome();
}

// ─────────────────── delegated clicks (survive re-renders) ─────────────────
document.addEventListener("click",(e)=>{
  // EVERY external-opening anchor (Graph link, markdown http links, anything
  // target=_blank) goes through the popup-or-hand-to-chat fallback: no button
  // in this widget may silently do nothing on a popup-blocking host.
  const xa=e.target.closest('a[target="_blank"]'); if(xa && xa.href){ e.preventDefault(); viewArtifact(xa.href); return; }
  const bk=e.target.closest("[data-bk-path]"); if(bk){ e.preventDefault(); toggleBasket(bk.getAttribute("data-bk-path"), bk.getAttribute("data-bk-title")); return; }
  const op=e.target.closest("[data-open-path]"); if(op){ e.preventDefault(); openFile(op.getAttribute("data-open-path")); return; }
  const ml=e.target.closest("a.mdlink"); if(ml){ e.preventDefault(); const t=resolveRel(dirOf(state.readPath||""), ml.getAttribute("data-rel")); if(t) openFile(t); return; }
  const pc=e.target.closest("[data-proj]"); if(pc){ openProject(pc.getAttribute("data-proj")); return; }
  const nav=e.target.closest("[data-nav]"); if(nav){ goTab(nav.getAttribute("data-nav")); return; }
  const rt=e.target.closest("[data-retry]"); if(rt){ const w=rt.getAttribute("data-retry");
    if(w==="home") loadHome(); else if(w==="search") runSearch(); else if(w==="inbox") loadInbox(true);
    else if(w==="artifacts") loadArtifacts(true); else if(w==="recipes") loadRecipes(true);
    else if(w==="project") openProject(state.projectId); else if(w==="read") openFile(state.readPath); return; }
  const ff=e.target.closest("[data-filter]"); if(ff){ setFilter(ff.getAttribute("data-filter")); return; }
  const rr=e.target.closest("[data-run-recipe]"); if(rr){ e.preventDefault(); runRecipe(rr.getAttribute("data-run-recipe")); return; }
  const ar=e.target.closest("[data-arch]"); if(ar){ archiveMsg(ar.getAttribute("data-arch")); return; }
  const ac=e.target.closest("[data-act]"); if(ac){ actMsg(ac.getAttribute("data-act")); return; }
  const sh=e.target.closest("[data-share]"); if(sh){ e.preventDefault(); shareArtifact(sh.getAttribute("data-share")); return; }
  const un=e.target.closest("[data-unshare]"); if(un){ e.preventDefault(); unshareArtifact(un.getAttribute("data-unshare")); return; }
  const vw=e.target.closest("[data-view-url]"); if(vw){ e.preventDefault(); viewArtifact(vw.getAttribute("data-view-url")); return; }
});

// Some hosts (claude.ai) swallow popup navigation from the sandboxed iframe —
// try window.open first; when blocked, hand the link to the agent so it lands
// in chat where links are always tappable.
async function viewArtifact(url){
  let w=null;
  try{ w=window.open(url, "_blank", "noopener"); }catch(e){ w=null; }
  if(w) return;
  const ok=await askAgent("Please show me this artifact link so I can tap it (just present the link, nothing else): "+url);
  showToast(ok ? "Link sent to chat — tap it there" : "Popup blocked — copy the URL below the row");
}

// tabs
function goTab(t){
  if(t==="home"){ if(state.projects) renderHome(); else loadHome(); }
  else if(t==="browse"){ if(state.load) renderBrowse(); else if(state.projects) renderHome(); else loadHome(); }
  else if(t==="search"){ renderSearch(); }
  else if(t==="inbox"){ loadInbox(false); }
  else if(t==="artifacts"){ loadArtifacts(false); }
}
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>goTab(t.dataset.tab));

// ─────────────────────────── boot ──────────────────────────
(async function init(){
  // restore persisted basket + pointer first (content is re-fetched below)
  let st=null; try{ st=window.openai && window.openai.widgetState; }catch(e){}
  if(st && Array.isArray(st.basket)) state.basket=st.basket;
  renderBasket();
  // ChatGPT hands the tool output synchronously at mount.
  try{ if(window.openai && window.openai.toolOutput){ seedFrom(normalize(window.openai.toolOutput)); } }catch(e){}
  // ui/initialize handshake. NOTE: params MUST carry appInfo (the client-info
  // field named that way) — the other spelling silently breaks tools/call on claude.ai.
  try{ await rpcReq("ui/initialize",{appCapabilities:{availableDisplayModes:["inline"]},appInfo:{name:"engram-navigator",version:"1.0.0"},protocolVersion:"2026-01-26"});
       rpcNote("ui/notifications/initialized",{}); }catch(e){}
  // Reconcile: if a push already seeded us, keep it; otherwise re-fetch from the
  // tools (the git repo is truth; widgetState is just a pointer).
  if(!booted){
    if(st && st.view==="reader" && st.projectId && st.path){ await openProject(st.projectId); await openFile(st.path); }
    else if(st && st.view==="browse" && st.projectId){ await openProject(st.projectId); }
    else if(st && st.view==="search"){ if(!state.projects){ try{ const d=await callTool("kb_projects",{}); if(Array.isArray(d)) state.projects=d; }catch(e){} } renderSearch(); }
    else if(st && st.view==="inbox"){ await loadInbox(false); }
    else if(st && st.view==="artifacts"){ await loadArtifacts(false); }
    else { await loadHome(); }
  }
  reportHeight();
})();
</script></body></html>
"""
