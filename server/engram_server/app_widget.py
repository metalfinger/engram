"""The Unified Engram App — one MCP App (SEP-1865) widget replacing five: the
Navigator, the Explore card, the Messages card, the Meetings card, and the
Office card. One ``ui://`` resource, one persistent tab bar (Home · Browse ·
People · Rooms · Office), one bridge, one CSS block.

This module owns ONLY the resource + its HTML. The tool FUNCTIONS this widget
calls (kb_projects, kb_load, kb_search, kb_read, kb_artifacts, kb_inbox,
kb_common_ground, social_state/social_conversation/social_send/social_accept/
social_mark_read, explore_state/explore_profile/explore_concept/explore_follow/
explore_ask, office_state, team_state, rooms_state, room_transcript,
room_reply) are wired elsewhere (app.py + teamwork store) — this module takes
no store dependency at all, mirroring how explore_widget.py/social_widget.py
divide labor from app.py.

IA, five tabs, lazy per-tab data (each tab fetches its own data on first open
via callTool and caches it in JS; the launcher's tool result only seeds which
tab opens and, opportunistically, a same-shaped payload):
  HOME    kb_projects grouped by folder, quick capture (kb_inbox), a "needs
          you" strip (unread messages + explore_state asks when present).
  BROWSE  kb_search doorways -> kb_read reader; a project concept tree via
          kb_load (opened from a Home card); kb_artifacts list. One shared
          markdown renderer + reader chrome across all three.
  PEOPLE  explore_state directory with avatars, follow/ask/profile/concept
          drill-down; a "working now" presence strip from team_state; a
          common-ground panel on profile via kb_common_ground.
  ROOMS   the merged comms hub: live rooms_state/room_transcript/room_reply,
          social_state DMs (social_conversation/social_send/social_accept),
          and notifications (room_invite entries jump into a room).
  OFFICE  lazy-loaded canvas port of the Live Office floor (office_state +
          resources/read floor art) — nothing loads until first opened.

Every callTool failure degrades to an inline message, never a blank tab (the
rule this widget is built to prove out for the merge). Deliberately DROPPED
relative to the widgets it replaces, to hold the 125,000-char HTML budget and
because the team-lead's brief doesn't ask for them here: the Navigator's
basket-to-artifact builder, save-as-recipe, recipes listing, and per-project
folder-filing UI. Browse still surfaces kb_artifacts (per the brief) — just
not the basket that built them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from engram_server.config import Settings

APP_URI = "ui://engram/app"
APP_MIME = "text/html;profile=mcp-app"


def app_resource_meta() -> dict:
    """``_meta`` for the ui:// resource — same deny-by-default CSP as every other
    Engram widget: this widget reaches the server ONLY over the MCP Apps bridge,
    never a direct fetch."""
    return {
        "ui": {
            "csp": {"connectDomains": [], "resourceDomains": []},
            "prefersBorder": True,
        },
        "openai/widgetPrefersBorder": True,
    }


def app_launcher_meta(enabled: bool) -> dict | None:
    """``_meta`` for the model-visible launcher tool that mounts this widget:
    ``visibility: ["model","app"]`` so it stays a useful plain-JSON tool in chat
    when no widget host is present. ``None`` disables it (no widget mounts)."""
    return {"ui": {"resourceUri": APP_URI, "visibility": ["model", "app"]}} if enabled else None


def app_only_meta(enabled: bool) -> dict | None:
    """``_meta`` for the app-only data-plane tools this widget's own bridge calls
    (team_state, rooms_state, room_transcript, room_reply, social_*, explore_*,
    office_state, ...): no resourceUri (they mount nothing — the widget is
    already up), ``visibility: ["app"]`` hides them from the model entirely."""
    return {"ui": {"visibility": ["app"]}} if enabled else None


def build_app_html(settings: "Settings") -> str:
    """Render the widget HTML, stamping the explorer host into the one external
    link it ever needs (an HTML artifact's rendered-page link). ``APP_HTML`` is a
    template carrying the ``__EXPLORER_URL__`` sentinel — the concrete host is
    injected only at serve time."""
    return APP_HTML.replace("__EXPLORER_URL__", settings.explorer_url.rstrip("/"))


def register_app_widget(mcp: "FastMCP", settings: "Settings") -> None:
    """Register the ui://engram/app resource when the widget flag is on; a no-op
    when off. Tools are NOT registered here — app.py wires kb_app (or whichever
    launcher name it picks) and the app-only data plane, pointing their meta at
    ``APP_URI`` via the two helpers above."""
    if not settings.widget:
        return

    html = build_app_html(settings)

    @mcp.resource(APP_URI, mime_type=APP_MIME, meta=app_resource_meta())
    def app_resource() -> str:
        return html


# ---------------------------------------------------------------------------
# The widget. One self-contained HTML document, zero external requests, no build
# step. Raw string (NOT an f-string) — the CSS/JS braces are literal.
# ---------------------------------------------------------------------------

APP_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Engram</title>
<style>
:root {
  --bg:#faf7f2; --surface:#ffffff; --surface-2:#f4efe6; --inset:#f7f2ea;
  --fg:#221f1a; --muted:#7a7266; --faint:#a89e8e;
  --line:#eae1d4; --line-2:#e0d6c4;
  --accent:#b5622b; --accent-ink:#9c4f1f; --accent-fg:#ffffff;
  --accent-soft:#f1e3d3; --accent-line:#e3c9ad;
  --green:#47804f; --blue:#3d6ba0; --violet:#7a58ab; --red:#b23a26; --amber:#97701f;
  --amber-bg:#ffe9c2; --amber-bd:#e6b96a; --amber-tx:#7a4a12;
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
    --amber-bg:#3a2a14; --amber-bd:#6b4d22; --amber-tx:#e2ae6e;
    --code-bg:#241f16; --shadow:none;
  }
}
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; }
body { margin:0; background:var(--bg); color:var(--fg);
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
  font-size:14px; line-height:1.5; letter-spacing:-0.003em; -webkit-font-smoothing:antialiased; position:relative; }
a { color:var(--accent-ink); text-decoration:none; } a:hover { text-decoration:underline; }
button { font:inherit; } :focus-visible { outline:2px solid var(--accent); outline-offset:2px; border-radius:3px; }
.wrap { padding:10px 12px 12px; }
@keyframes fade { from { opacity:0; transform:translateY(3px); } to { opacity:1; transform:none; } }
#view { animation:fade .15s ease; }
@media (prefers-reduced-motion: reduce) { #view { animation:none; } * { transition:none !important; } }

.eyebrow { font-size:.66rem; font-weight:700; letter-spacing:.13em; text-transform:uppercase; color:var(--accent-ink); margin:0 0 .3rem; }
.section-label { font-size:.66rem; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:var(--faint); margin:1rem 0 .35rem; }
h1 { font-size:1.25rem; line-height:1.15; letter-spacing:-0.02em; margin:0 0 .2rem; font-weight:700; }
.lede { color:var(--muted); font-size:.85rem; margin:.15rem 0 0; }
.headrow { display:flex; align-items:center; gap:.5rem; }
.headrow h1 { flex:1 1 auto; min-width:0; margin:0; }

/* tab bar */
.tabs { display:flex; gap:2px; border-bottom:1px solid var(--line); padding:0 6px; background:color-mix(in srgb,var(--bg) 90%,transparent); position:sticky; top:0; z-index:5; }
.tab { appearance:none; border:0; background:none; font-size:.78rem; font-weight:600; letter-spacing:.02em;
       color:var(--muted); padding:.5rem .7rem; cursor:pointer; border-bottom:2px solid transparent; margin-bottom:-1px; display:inline-flex; align-items:center; gap:.3rem; }
.tab:hover { color:var(--fg); } .tab.active { color:var(--accent-ink); border-bottom-color:var(--accent); }
.tbadge { background:var(--accent); color:var(--accent-fg); font-size:.6rem; font-weight:700; min-width:1.05rem; height:1.05rem; padding:0 .3rem; border-radius:999px; display:none; place-items:center; }

/* buttons */
.back, .refresh, .retry, .act, .arch { font-size:.75rem; font-weight:600; padding:.32rem .68rem; border-radius:7px; cursor:pointer; border:1px solid var(--line-2); background:var(--surface); color:var(--fg); }
.back:hover, .refresh:hover, .arch:hover { border-color:var(--accent-line); color:var(--accent-ink); }
.act, .retry { background:var(--accent); color:var(--accent-fg); border-color:var(--accent); }
.act:hover, .retry:hover { filter:brightness(1.06); }
.pillrow { display:flex; gap:.35rem; margin:.5rem 0 .7rem; }
.pill { appearance:none; border:1px solid var(--line-2); background:var(--surface); color:var(--muted); font-size:.74rem; font-weight:600; padding:.28rem .65rem; border-radius:999px; cursor:pointer; }
.pill:hover { border-color:var(--accent-line); color:var(--accent-ink); }
.pill.on { background:var(--accent-soft); color:var(--accent-ink); border-color:var(--accent-line); }

/* badges / chips */
.badge, .chip { display:inline-flex; align-items:center; gap:.3rem; font-size:.68rem; font-weight:600; padding:.1rem .5rem; border-radius:999px; white-space:nowrap; line-height:1.5; }
.badge { background:var(--surface-2); color:var(--muted); }
.badge.unread { background:var(--accent); color:var(--accent-fg); }
.badge.vis-public { background:var(--accent); color:var(--accent-fg); }
.badge.vis-contacts { background:var(--accent-soft); color:var(--accent-ink); }
.badge.vis-private { background:var(--surface-2); color:var(--muted); }
.chip { background:var(--inset); color:var(--muted); border:1px solid var(--line); }
.chip.proj { color:var(--accent-ink); border-color:var(--accent-line); background:var(--accent-soft); }
.chip .k { color:var(--faint); text-transform:uppercase; letter-spacing:.05em; font-size:.9em; }
.chips { display:flex; flex-wrap:wrap; gap:.35rem; margin:.5rem 0 0; }
.when { font-size:.7rem; color:var(--faint); font-variant-numeric:tabular-nums; }
.dot { width:.5rem; height:.5rem; border-radius:50%; background:var(--faint); flex:0 0 auto; }
.dot.s-green { background:var(--green); } .dot.s-blue { background:var(--blue); }
.dot.s-accent { background:var(--accent); } .dot.s-amber { background:var(--amber); } .dot.s-red { background:var(--red); }
.stamp { flex:0 0 auto; width:1.4rem; height:1.4rem; border-radius:7px; display:grid; place-items:center;
         background:var(--accent-soft); border:1px solid var(--accent-line); font-size:.85rem; }

/* avatars — the ONE avatar helper, shared by People + Rooms */
.avatar { border-radius:50%; object-fit:cover; flex:0 0 auto; display:inline-block; background:var(--surface-2); }
.avatar-sm { width:1.9rem; height:1.9rem; } .avatar-md { width:2.5rem; height:2.5rem; }
.avatar-fallback { display:grid; place-items:center; font-weight:700; color:var(--accent-ink); background:var(--accent-soft); border:1px solid var(--accent-line); }
.avatar-sm.avatar-fallback { font-size:.8rem; } .avatar-md.avatar-fallback { font-size:1rem; }

/* quick capture */
.qcap { display:flex; gap:.5rem; margin:0 0 .9rem; }
.qc-in, .sinput, .taxt { flex:1 1 auto; min-width:0; padding:.45rem .7rem; font-size:.86rem; color:var(--fg); background:var(--surface); border:1px solid var(--line-2); border-radius:8px; outline:none; }
.qc-in:focus, .sinput:focus, .taxt:focus { border-color:var(--accent-line); box-shadow:0 0 0 3px var(--accent-soft); }
.qc-go { flex:0 0 auto; background:var(--accent); color:var(--accent-fg); font-weight:650; font-size:.8rem; border:1px solid var(--accent); border-radius:8px; padding:.42rem .85rem; cursor:pointer; }
.qc-in:disabled, .qc-go:disabled { opacity:.55; cursor:default; }

/* project cards (Home) */
.cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(13rem,1fr)); gap:.6rem; margin:.7rem 0 .3rem; }
.card { display:flex; flex-direction:column; gap:.3rem; padding:.7rem .8rem; text-align:left; cursor:pointer; width:100%;
        background:var(--surface); color:var(--fg); border:1px solid var(--line); border-radius:10px; box-shadow:var(--shadow); }
.card:hover { border-color:var(--accent-line); }
.card-head { display:flex; align-items:center; gap:.4rem; } .card-head h3 { margin:0; font-size:.93rem; font-weight:650; }
.card p { margin:0; font-size:.8rem; color:var(--muted); line-height:1.4; }
.card-foot { margin-top:auto; padding-top:.15rem; display:flex; flex-wrap:wrap; gap:.35rem; align-items:center; }
.folder-label { margin:1rem 0 .1rem; }
.needsrow { display:flex; align-items:center; justify-content:space-between; gap:.5rem; padding:.5rem .65rem; background:var(--surface); border:1px solid var(--line); border-radius:10px; margin:.4rem 0; }

/* concept tree (Browse) */
.tree { margin:.5rem 0 0; }
.tnode > summary { display:flex; align-items:center; gap:.35rem; padding:.28rem .35rem; border-radius:6px; cursor:pointer; font-size:.83rem; font-weight:600; list-style:none; }
.tnode > summary::-webkit-details-marker { display:none; }
.tnode > summary::before { content:"\203a"; color:var(--faint); font-weight:400; width:.7rem; display:inline-block; }
.tnode[open] > summary::before { transform:rotate(90deg); display:inline-block; }
.tnode > summary:hover { background:var(--surface-2); }
.tnode .kids { margin:.05rem 0 .3rem .85rem; padding-left:.5rem; border-left:1px solid var(--line); }
.frow { display:flex; align-items:baseline; gap:.4rem; padding:.28rem .4rem; border-radius:6px; cursor:pointer; }
.frow:hover { background:var(--surface-2); }
.frow .glyph { color:var(--faint); font-size:.85rem; } .frow .ft { font-size:.83rem; font-weight:550; }
.frow .fd { font-size:.75rem; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.ctx { border:1px solid var(--line); border-left:3px solid var(--accent-line); border-radius:0 8px 8px 0; background:var(--inset); padding:.5rem .8rem; margin:.7rem 0 0; max-height:13rem; overflow:hidden; }

/* reader + markdown */
.props { display:flex; flex-wrap:wrap; gap:.35rem; margin:.6rem 0 .9rem; }
.md { font-size:.87rem; } .md > :first-child { margin-top:0; }
.md h1,.md h2,.md h3,.md h4 { line-height:1.2; letter-spacing:-0.01em; }
.md h1 { font-size:1.15rem; margin:1rem 0 .5rem; font-weight:700; } .md h2 { font-size:1.02rem; margin:.9rem 0 .4rem; font-weight:650; }
.md h3 { font-size:.93rem; margin:.8rem 0 .35rem; font-weight:650; } .md p,.md ul,.md ol { margin:.6rem 0; } .md li { margin:.2rem 0; }
.md code { font-family:ui-monospace,"Cascadia Code",Consolas,monospace; font-size:.86em; background:var(--code-bg); padding:.1rem .35rem; border-radius:5px; }
.md pre { background:var(--code-bg); padding:.7rem .85rem; border-radius:9px; overflow-x:auto; border:1px solid var(--line); }
.md pre code { background:none; padding:0; border:0; }
.md blockquote { margin:.9rem 0; padding:.25rem .85rem; border-left:3px solid var(--accent-line); color:var(--muted); background:var(--inset); border-radius:0 6px 6px 0; }
.md hr { border:none; border-top:1px solid var(--line); margin:1.2rem 0; }
.md a.mdlink { border-bottom:1px dotted var(--accent-line); } .md strong { font-weight:650; }

/* search + results */
.searchbar { display:flex; gap:.5rem; margin:.3rem 0 .3rem; }
.result { display:block; width:100%; text-align:left; border:1px solid var(--line); border-radius:10px; background:var(--surface);
          padding:.65rem .8rem; margin:.5rem 0; box-shadow:var(--shadow); cursor:pointer; }
.result:hover { border-color:var(--accent-line); }
.result .rtop { display:flex; align-items:flex-start; gap:.5rem; } .result h3 { margin:0; font-size:.93rem; font-weight:650; flex:1 1 auto; min-width:0; }
.result .rpath { font-size:.7rem; color:var(--faint); font-family:ui-monospace,Consolas,monospace; margin:.15rem 0; }
.result p { margin:.3rem 0 .3rem; font-size:.82rem; color:var(--muted); }
.scorebar { display:flex; align-items:center; gap:.5rem; margin-top:.35rem; }
.scorebar .track { flex:0 0 6.5rem; height:.3rem; border-radius:999px; background:var(--surface-2); overflow:hidden; }
.scorebar .fill { height:100%; background:var(--accent); border-radius:999px; }
.scorebar .pct { font-size:.68rem; color:var(--faint); font-variant-numeric:tabular-nums; }

/* artifacts */
.artrow { border:1px solid var(--line); border-radius:10px; background:var(--surface); box-shadow:var(--shadow); margin:.5rem 0; padding:.55rem .65rem; cursor:pointer; }
.artrow:hover { border-color:var(--accent-line); }
.artrow .arthead { display:flex; align-items:center; gap:.4rem; } .artrow .artt { font-size:.9rem; font-weight:650; }
.artrow .artchips { display:flex; flex-wrap:wrap; gap:.35rem; margin-top:.35rem; }
.chip.shared { color:var(--accent-ink); border-color:var(--accent-line); background:var(--accent-soft); }
.chip.stale { background:color-mix(in srgb,var(--amber) 16%,transparent); color:var(--amber); border-color:transparent; }

/* people directory + presence strip */
.teamstrip { display:flex; gap:.5rem; overflow-x:auto; padding:.2rem .1rem .6rem; margin-bottom:.3rem; }
.teamcard { display:flex; align-items:center; gap:.4rem; flex:0 0 auto; background:var(--surface); border:1px solid var(--line); border-radius:999px; padding:.25rem .65rem .25rem .3rem; font-size:.76rem; }
.freshdot { width:.45rem; height:.45rem; border-radius:50%; background:var(--faint); }
.freshdot.fresh { background:var(--green); } .freshdot.warm { background:var(--amber); }
.people-list, .work-list, .convos, .requests, .notifs, .rooms { display:flex; flex-direction:column; gap:.45rem; margin:.35rem 0; }
.person-row, .work-item, .convo, .room, .req-row { display:flex; align-items:center; gap:.6rem; padding:.6rem .7rem; background:var(--surface);
              color:var(--fg); border:1px solid var(--line); border-radius:12px; box-shadow:var(--shadow); cursor:pointer; text-align:left; width:100%; }
.person-main, .convo-main, .room-main { min-width:0; flex:1 1 auto; display:flex; flex-direction:column; gap:.05rem; }
.person-name, .convo-name, .room-name, .work-title { font-size:.88rem; font-weight:650; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.person-handle, .room-goal { font-size:.72rem; color:var(--faint); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.person-bio, .convo-preview { font-size:.78rem; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin-top:.1rem; }
.convo.unread { border-color:var(--accent-line); background:color-mix(in srgb, var(--accent-soft) 40%, var(--surface)); }
.unread-dot { width:.5rem; height:.5rem; border-radius:50%; background:var(--accent); flex:0 0 auto; }
.follow-btn, .accept-btn { flex:0 0 auto; background:var(--accent); color:var(--accent-fg); border:1px solid var(--accent); border-radius:8px;
              font-weight:600; font-size:.75rem; padding:.32rem .65rem; cursor:pointer; }
.follow-btn.following { background:var(--surface); color:var(--muted); border-color:var(--line-2); }
.follow-btn:disabled, .accept-btn:disabled { opacity:.6; cursor:default; }
.membercluster { display:flex; } .membercluster .avatar { margin-left:-.5rem; border:2px solid var(--surface); }
.membercluster .avatar:first-child { margin-left:0; }
.budgetmeter { flex:0 0 4rem; height:.3rem; border-radius:999px; background:var(--surface-2); overflow:hidden; }
.budgetmeter .fill { height:100%; background:var(--accent); }
.gauge-row { display:flex; align-items:center; gap:.4rem; margin-top:.2rem; }
.phead { display:flex; align-items:flex-start; gap:.6rem; margin:.1rem 0 .6rem; }
.phead-main { min-width:0; flex:1 1 auto; display:flex; align-items:flex-start; gap:.6rem; }
.pfollow-row { display:flex; align-items:center; gap:.6rem; margin-top:.5rem; }
.commonground { margin-top:.7rem; }
.cg-row { font-size:.78rem; color:var(--muted); padding:.35rem 0; border-top:1px solid var(--line); }
.cg-row b { color:var(--fg); }
.ctext p { margin:0 0 .7rem; white-space:pre-wrap; word-break:break-word; }

/* chat + transcript (shared by DMs and Rooms) */
.log { display:flex; flex-direction:column; gap:.15rem; min-height:5rem; }
.row { display:flex; align-items:flex-end; gap:.4rem; margin:.25rem 0; } .row.me { justify-content:flex-end; }
.row.sys { justify-content:center; }
.bubble { max-width:78%; padding:.45rem .65rem .4rem; border-radius:13px; font-size:.86rem; line-height:1.4; background:var(--surface-2); border:1px solid var(--line); }
.row.me .bubble { background:var(--accent-soft); border-color:var(--accent-line); border-bottom-right-radius:4px; }
.row.sys .bubble { background:none; border:0; color:var(--faint); font-size:.75rem; font-style:italic; text-align:center; }
.row.audit .bubble { background:none; border:0; color:var(--faint); font-size:.72rem; font-style:italic; }
.bubble .who { font-size:.65rem; font-weight:700; color:var(--muted); margin-bottom:.15rem; }
.bubble .msg { white-space:pre-wrap; word-break:break-word; }
.bubble .tm { font-size:.62rem; color:var(--faint); margin-top:.2rem; text-align:right; font-variant-numeric:tabular-nums; }
.bubble.pending { opacity:.6; } .bubble.failed { border-color:var(--red); }
.composer { position:sticky; bottom:0; display:flex; gap:.5rem; align-items:flex-end; background:var(--bg); padding:.5rem 0 .1rem; margin-top:.4rem; border-top:1px solid var(--line); }
.composer textarea { flex:1 1 auto; min-width:0; padding:.55rem .7rem; font-size:.88rem; color:var(--fg); background:var(--surface);
          border:1px solid var(--line-2); border-radius:12px; outline:none; resize:none; max-height:6rem; font-family:inherit; }
.composer textarea:focus { border-color:var(--accent-line); box-shadow:0 0 0 3px var(--accent-soft); }
.csend { flex:0 0 auto; width:2.5rem; height:2.5rem; border-radius:50%; background:var(--accent); color:var(--accent-fg);
         border:1px solid var(--accent); font-size:1rem; cursor:pointer; display:grid; place-items:center; }
.csend:disabled { opacity:.5; cursor:default; }
.cerr { color:var(--red); font-size:.76rem; margin:.2rem 0 0; }
.needs-banner { display:flex; align-items:center; gap:.5rem; background:var(--amber-bg); border:1px solid var(--amber-bd); color:var(--amber-tx);
  font-size:.8rem; font-weight:600; border-radius:9px; padding:.5rem .7rem; margin:0 0 .6rem; }

/* empty/error/skeleton states */
.empty { color:var(--faint); font-size:.82rem; font-style:italic; padding:1.1rem .2rem; }
.errbox { display:flex; align-items:center; gap:.7rem; color:var(--red); font-size:.82rem; padding:.9rem 0; }
.skel { position:relative; overflow:hidden; background:var(--surface-2); border-radius:10px; height:3.8rem; margin-bottom:.5rem; }
.skel::after { content:""; position:absolute; inset:0; transform:translateX(-100%);
               background:linear-gradient(90deg,transparent,color-mix(in srgb,var(--surface) 60%,transparent),transparent); animation:shimmer 1.2s infinite; }
@keyframes shimmer { 100% { transform:translateX(100%); } }
.skgrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(13rem,1fr)); gap:.6rem; margin:.7rem 0; }
.skel-card { height:5rem; }

/* Office tab — scoped palette (its own dark floor look, never bleeds into other tabs) */
#office-tab { --o-bg:#141a12; --o-panel:#f6efe1; --o-line:#cbb489; --o-ink:#3a2f24; --o-inks:#8a7960; --o-good:#4f9d5b;
  --o-amber-bg:#ffe9c2; --o-amber-bd:#e6b96a; --o-amber-tx:#7a4a12; margin:-10px -12px 0; }
@media (prefers-color-scheme: dark) { #office-tab { --o-panel:#241f16; --o-line:#4a3820; --o-ink:#ece6da; --o-inks:#a09784;
  --o-amber-bg:#3a2a14; --o-amber-bd:#6b4d22; --o-amber-tx:#e2ae6e; } }
#office-tab .ohdr { display:flex; align-items:center; gap:.5rem; padding:.5rem .65rem; background:var(--o-panel); border-bottom:1px solid var(--o-line); color:var(--o-ink); }
#office-tab .odot { width:.5rem; height:.5rem; border-radius:50%; background:var(--o-good); flex:0 0 auto; }
#office-tab .ocounts { color:var(--o-inks); font-size:.74rem; font-weight:600; }
#office-tab .oneeds { display:inline-flex; align-items:center; gap:.3rem; font-size:.68rem; font-weight:700; color:var(--o-amber-tx);
  background:var(--o-amber-bg); border:1px solid var(--o-amber-bd); border-radius:999px; padding:.12rem .5rem; margin-left:auto; }
#office-tab .pulse { width:.4rem; height:.4rem; border-radius:50%; background:var(--o-amber-tx); animation:pulse 1.6s infinite; }
@keyframes pulse { 0%{opacity:.4} 50%{opacity:1} 100%{opacity:.4} }
#office-tab #ostage { position:relative; background:#0f130d; } #office-tab canvas { display:block; width:100%; image-rendering:pixelated; cursor:pointer; }
#office-tab #oempty { position:absolute; left:50%; top:50%; transform:translate(-50%,-50%); text-align:center; color:#e7ead6;
  font-size:.8rem; font-weight:600; pointer-events:none; background:rgba(14,18,30,.72); border:1px solid rgba(120,130,110,.35); border-radius:12px; padding:.7rem 1rem; }
#office-tab .olegend { display:flex; flex-wrap:wrap; gap:.5rem; padding:.4rem .65rem .6rem; background:var(--o-panel); font-size:.68rem; color:var(--o-inks); font-weight:600; }
#office-tab .olegend .d { display:inline-block; width:.4rem; height:.4rem; border-radius:50%; margin-right:.25rem; }
#office-tab .ocredit { padding:.3rem .65rem; font-size:.64rem; color:var(--o-inks); background:var(--o-panel); text-align:right; }
</style></head>
<body>
<nav class="tabs" id="tabs">
  <button class="tab" data-tab="home">Home</button>
  <button class="tab" data-tab="browse">Browse</button>
  <button class="tab" data-tab="people">People</button>
  <button class="tab" data-tab="rooms">Rooms<span class="tbadge" id="rm-badge"></span></button>
  <button class="tab" data-tab="office">Office</button>
  <a class="tab" id="graphlink" style="margin-left:auto" href="__EXPLORER_URL__/brain/graph" target="_blank" rel="noopener" title="Open the brain graph in a new tab">Graph &#8599;</a>
</nav>
<div class="wrap"><div id="view"><div class="skgrid"><div class="skel skel-card"></div><div class="skel skel-card"></div><div class="skel skel-card"></div></div></div></div>
<script>
"use strict";
const $=(id)=>document.getElementById(id);
const view=$("view");

// ─────────────────────── unified in-widget state ───────────────────────
let state={
  view:"home",
  projects:null, projectsStatus:null, asks:null,
  browseSub:"search", browseReturn:"search",
  load:null, projectId:null,
  searchQuery:"", searchResults:null, searchStatus:null,
  artifacts:null, artifactsStatus:null,
  readPath:null, read:null,
  team:null, exploreData:null, exploreStatus:null,
  profileHandle:null, profileData:null, profileStatus:null, commonGround:null,
  conceptHandle:null, conceptPath:null, conceptData:null, conceptStatus:null, peopleReturn:{view:"people"},
  roomsSub:"list", roomsData:null, roomsStatus:null, socialData:null, socialStatus:null,
  openRoom:null, roomTx:null, openDM:null, dmConv:null,
  seenKeysRoom:new Set(), seenContentRoom:new Set(), seenKeysDM:new Set(), seenContentDM:new Set(),
  officeBooted:false
};
let booted=false, busy=false;

// ─────────────────────────────── height ───────────────────────────────
function reportHeight(){
  const el=document.documentElement, prev=el.style.height; el.style.height="max-content";
  const h=Math.ceil(el.getBoundingClientRect().height); el.style.height=prev;
  try{ if(window.openai && typeof window.openai.notifyIntrinsicHeight==="function") window.openai.notifyIntrinsicHeight(h); }catch(e){}
  try{ window.parent.postMessage({jsonrpc:"2.0",method:"ui/notifications/size-changed",params:{width:Math.ceil(window.innerWidth),height:h}},"*"); }catch(e){}
}
let _rafH=0; function scheduleHeight(){ if(_rafH) cancelAnimationFrame(_rafH); _rafH=requestAnimationFrame(reportHeight); }
try{ new ResizeObserver(scheduleHeight).observe(document.body); }catch(e){ window.addEventListener("resize",scheduleHeight); }

// ─────────────────────────── MCP Apps bridge (the ONE bridge) ──────────────────────────
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
    if(d) seedFrom(d);
    return;
  }
  // Host recycling this iframe — stop everything ticking server-side, then ack.
  if(m.method==="ui/resource-teardown"){
    stopAllPolling(); stopOfficeAnim();
    if(m.id!==undefined) window.parent.postMessage({jsonrpc:"2.0",id:m.id,result:{}},"*");
    return;
  }
},{passive:true});
// The python SDK wraps list/dict tool returns in a single-key {"result": ...}
// envelope for structured content — unwrap it EVERYWHERE or every shape check fails on claude.ai.
function unwrap(v){ if(v && typeof v==="object" && !Array.isArray(v) && "result" in v && Object.keys(v).length===1) return v.result; return v; }
function rpcReq(method,params,timeout){ return new Promise((resolve,reject)=>{ const id=nextId++; pending.set(id,{resolve,reject});
  window.parent.postMessage({jsonrpc:"2.0",id,method,params},"*");
  setTimeout(()=>{ if(pending.has(id)){ pending.delete(id); reject(new Error("host timeout")); } }, timeout||120000); }); }
function rpcNote(method,params){ window.parent.postMessage({jsonrpc:"2.0",method,params},"*"); }
function parseText(content){ try{ const t=Array.isArray(content)?(content.find(c=>c.type==="text")||{}).text:null; return t?JSON.parse(t):null; }catch(e){ return null; } }
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
// silently fails to wake Claude (hard-won from the Navigator/Survey widgets).
async function askAgent(text){
  try{ await rpcReq("ui/message",{role:"user",content:[{type:"text",text:text}]},6000); return true; }catch(e){}
  try{ if(window.openai && typeof window.openai.sendFollowUpMessage==="function"){ window.openai.sendFollowUpMessage({prompt:text}); return true; } }catch(e){}
  return false;
}
async function readResource(uri){
  try{ const r=await rpcReq("resources/read",{uri},8000); return (r&&r.contents&&r.contents[0])||null; }catch(e){ return null; }
}

// ─────────────────────────── shared helpers ──────────────────────────
// esc() is the ONLY thing allowed to put user data into the DOM via innerHTML.
function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function relTime(d){ if(!d) return ""; const t=Date.parse((""+d).length<=10?d+"T00:00:00Z":d); if(isNaN(t)) return "";
  const secs=(Date.now()-t)/1000; if(secs<45) return "just now"; const days=Math.floor(secs/86400);
  if(days<=0){ const m=Math.floor(secs/60); if(m<60) return m+"m ago"; return Math.floor(secs/3600)+"h ago"; }
  if(days===1) return "yesterday"; if(days<7) return days+"d ago"; if(days<30) return Math.floor(days/7)+"w ago"; return Math.floor(days/30)+"mo ago"; }
function dirOf(p){ const i=(p||"").lastIndexOf("/"); return i<0?"":p.slice(0,i); }
function resolveRel(baseDir, rel){ rel=(rel||"").split("#")[0];
  const stack = rel.startsWith("/") ? [] : (baseDir?baseDir.split("/").filter(Boolean):[]);
  for(const seg of rel.split("/")){ if(seg===""||seg===".") continue; if(seg==="..") stack.pop(); else stack.push(seg); }
  return stack.join("/"); }
function stripFrontmatter(src){ src=(src||"").replace(/\r\n/g,"\n");
  if(src.startsWith("---\n")){ const end=src.indexOf("\n---",3); if(end!==-1){ const nl=src.indexOf("\n",end+1); return src.slice(nl===-1?src.length:nl+1); } }
  return src; }
const TYPE_GLYPHS={decision:"⚖",spec:"📐",runbook:"🛠",person:"👤",client:"🏢",message:"✉",reference:"🔖",project:"📁",idea:"💡",meeting:"🗓",note:"📝",artifact:"📄"};
function glyph(t){ return TYPE_GLYPHS[String(t||"").trim().toLowerCase()]||"◆"; }
function statusDot(s){ s=String(s||"").toLowerCase();
  if(["active","published"].includes(s)) return "s-green"; if(s==="unread") return "s-accent";
  if(["tentative","draft","idle"].includes(s)) return "s-amber"; if(["blocked"].includes(s)) return "s-red"; return ""; }
function skelRows(n){ let s=""; for(let i=0;i<(n||4);i++) s+='<div class="skel"></div>'; return s; }
function skelCards(n){ let s=""; for(let i=0;i<(n||6);i++) s+='<div class="skel skel-card"></div>'; return '<div class="skgrid">'+s+'</div>'; }
function errBlock(msg, retryKey){ return '<div class="errbox"><span>'+esc(msg)+'</span><button class="retry" data-retry="'+esc(retryKey)+'">Retry</button></div>'; }
// the ONE avatar helper, shared by People and Rooms(DMs)
function avatarHtml(displayName, handle, avatarUrl, size){
  const label=String(displayName||handle||"?").trim();
  const letter=esc((label.charAt(0)||"?").toUpperCase());
  if(avatarUrl){
    return '<img class="avatar avatar-'+size+'" src="'+esc(avatarUrl)+'" alt="" referrerpolicy="no-referrer" '
      +'onerror="this.outerHTML=\'<span class=&quot;avatar avatar-'+size+' avatar-fallback&quot;>'+letter+'</span>\'">';
  }
  return '<span class="avatar avatar-'+size+' avatar-fallback">'+letter+'</span>';
}

// ─────────────────── small markdown renderer (shared by Browse's reader + tree ctx) ───────────────────
function mdInline(s){
  return s.split(/(`[^`]+`)/g).map(p=>{
    if(p.length>1 && p[0]==="`" && p[p.length-1]==="`") return "<code>"+p.slice(1,-1)+"</code>";
    p=p.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g,(m,txt,url)=>linkHtml(txt,url));
    p=p.replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>").replace(/__([^_]+)__/g,"<strong>$1</strong>");
    p=p.replace(/(^|[^*\w])\*(?!\s)([^*]+?)\*(?!\w)/g,"$1<em>$2</em>").replace(/(^|[^_\w])_(?!\s)([^_]+?)_(?!\w)/g,"$1<em>$2</em>");
    return p;
  }).join("");
}
function linkHtml(txt,url){
  if(/^https?:/i.test(url)) return '<a href="'+url+'" target="_blank" rel="noopener">'+txt+'</a>';
  const bare=url.split("#")[0];
  if(bare && !/^[a-z]+:/i.test(bare) && /\.md$/i.test(bare)) return '<a href="#" class="mdlink" data-rel="'+url+'">'+txt+'</a>';
  return txt;
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
function buildList(items){
  let out="", stack=[];
  for(const it of items){
    while(stack.length && it.indent < stack[stack.length-1].indent){ out+=(stack.pop().ordered?"</ol>":"</ul>"); }
    if(!stack.length || it.indent > stack[stack.length-1].indent){ out+=(it.ordered?"<ol>":"<ul>"); stack.push({indent:it.indent,ordered:it.ordered}); }
    out+="<li>"+mdInline(it.text)+"</li>";
  }
  while(stack.length){ out+=(stack.pop().ordered?"</ol>":"</ul>"); }
  return out;
}

// ═══════════════════════════════ HOME ═══════════════════════════════
function unreadCount(){ return (state.projects||[]).reduce((n,p)=>n+(p.unread_messages|0),0); }
function roomsBadgeCount(){
  const dmUnread=((state.socialData&&state.socialData.counts&&state.socialData.counts.dms)|0);
  const roomNeeds=((state.roomsData&&state.roomsData.rooms)||[]).reduce((n,r)=>n+((r.unread|0)>0?1:0),0);
  return dmUnread+roomNeeds;
}
function updateTabBadges(){
  const el=$("rm-badge"); if(el){ const n=roomsBadgeCount(); el.textContent=n>0?n:""; el.style.display=n>0?"inline-grid":"none"; }
}
function setActiveTab(){ document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("active", t.dataset.tab===state.view)); }
function show(html){ view.innerHTML=html; setActiveTab(); updateTabBadges(); scheduleHeight(); }

async function loadHome(force){
  if(state.projects && !force){ renderHome(); return; }
  state.projectsStatus="loading"; renderHome();
  try{
    const d=await callTool("kb_projects",{});
    if(d && d.error) throw new Error(d.error);
    state.projects=Array.isArray(d)?d:[]; state.projectsStatus="ok";
  }catch(e){ state.projectsStatus="error"; }
  renderHome();
  loadAsksBestEffort();
}
async function loadAsksBestEffort(){
  // kb_asks -> {to_answer:[{path, question,...}]} — the questions WAITING ON this user.
  try{ const d=await callTool("kb_asks",{}); const open=(d&&Array.isArray(d.to_answer))?d.to_answer.filter(a=>a.status!=="answered"):[];
    if(open.length){ state.asks=open.map(a=>({title:(a.question||"").slice(0,80)||a.path, path:a.path})); if(state.view==="home") renderHome(); } }
  catch(e){ /* best-effort — single-user servers have no asks */ }
}
function visBadge(v){
  const s=(v||"private"); const label = s==="public" ? "🌐 public" : (s==="contacts" ? "👥 contacts" : "🔒 private");
  return '<span class="badge vis-'+esc(s)+'">'+esc(label)+'</span>';
}
function projCard(p){
  const unread=(p.unread_messages|0);
  return '<button class="card" data-proj="'+esc(p.id)+'">'
    +'<div class="card-head"><span class="dot '+statusDot(p.status)+'"></span><h3>'+esc(p.title||p.id)+'</h3></div>'
    +(p.description?'<p>'+esc(p.description)+'</p>':'')
    +'<div class="card-foot">'+visBadge(p.visibility)+(unread>0?'<span class="badge unread">'+unread+' unread</span>':'')
      +(p.last_session?'<span class="when">'+esc(relTime(p.last_session))+'</span>':'')+'</div></button>';
}
function renderHome(){
  state.view="home"; stopAllPolling();
  const ps=state.projects||[];
  let body;
  if(state.projectsStatus==="loading"){ body=skelCards(6); }
  else if(state.projectsStatus==="error"){ body=errBlock("Could not load projects.","home"); }
  else if(!ps.length){ body='<div class="empty">No projects found.</div>'; }
  else{
    const folders={}; ps.forEach(p=>{ const f=p.folder||""; (folders[f]=folders[f]||[]).push(p); });
    const named=Object.keys(folders).filter(f=>f).sort();
    const chunks=[];
    named.forEach(f=>chunks.push('<p class="eyebrow folder-label">📁 '+esc(f)+'</p><div class="cards">'+folders[f].map(projCard).join("")+'</div>'));
    if(folders[""]){ if(named.length) chunks.push('<p class="eyebrow folder-label">Not in a folder</p>');
      chunks.push('<div class="cards">'+folders[""].map(projCard).join("")+'</div>'); }
    body=chunks.join("");
  }
  const needy=ps.filter(p=>(p.unread_messages|0)>0);
  let needs="";
  if(needy.length || (state.asks&&state.asks.length)){
    needs+='<p class="section-label">Needs you</p>';
    needy.forEach(p=>{ needs+='<div class="needsrow"><span>'+esc(p.title||p.id)+'</span><span class="badge unread">'+(p.unread_messages|0)+' unread</span></div>'; });
    (state.asks||[]).forEach(a=>{ needs+='<div class="needsrow"><span>'+esc(a.title||a.path||"An open ask")+'</span><span class="badge">ask</span></div>'; });
  }
  show('<div class="qcap"><input id="qc-input" class="qc-in" type="text" placeholder="Quick capture &mdash; remember anything&hellip;" autocomplete="off" aria-label="Quick capture to inbox">'
        +'<button class="qc-go" id="qc-go">Capture</button></div>'
      +'<p class="eyebrow">Engram</p><h1>brain</h1><p class="lede">Pick a project to browse its context and concepts.</p>'
      +needs+body);
  const qi=$("qc-input"), qg=$("qc-go");
  if(qg) qg.onclick=quickCapture;
  if(qi) qi.onkeydown=(e)=>{ if(e.key==="Enter"){ e.preventDefault(); quickCapture(); } };
}
async function quickCapture(){
  const inp=$("qc-input"); if(!inp) return; const text=inp.value.trim(); if(!text) return;
  const go=$("qc-go"); inp.disabled=true; if(go) go.disabled=true;
  try{ const r=await callTool("kb_inbox",{text:text}); if(r && r.error) throw new Error(r.error); inp.value=""; }
  catch(e){ /* leave the text in place so nothing is lost */ }
  inp.disabled=false; if(go) go.disabled=false;
}

// ═══════════════════════════════ BROWSE ═══════════════════════════════
function treeHtml(node, depth){
  const files=(node.files||[]).map(f=>
    '<div class="frow" data-open-path="'+esc(f.path)+'"><span class="glyph">◦</span><span class="ft">'+esc(f.title||f.name)+'</span>'
      +(f.description?'<span class="fd">&mdash; '+esc(f.description)+'</span>':'')+'</div>').join("");
  const dirs=(node.dirs||[]).map(d=>
    '<details class="tnode"'+(depth<1?" open":"")+'><summary>'+esc(d.title||d.name)+'</summary><div class="kids">'+treeHtml(d,depth+1)+'</div></details>').join("");
  return (dirs+files) || '<div class="empty">Empty.</div>';
}
function browsePills(){
  return '<div class="pillrow">'+["search","artifacts"].map(k=>
    '<button class="pill'+(state.browseSub===k?" on":"")+'" data-browsesub="'+k+'">'+(k==="search"?"Search":"Artifacts")+'</button>').join("")+'</div>';
}
function renderBrowse(){
  state.view="browse"; stopAllPolling();
  if(state.browseSub==="project") return renderProjectTree();
  if(state.browseSub==="reader") return renderReader();
  if(state.browseSub==="artifacts") return renderArtifacts();
  return renderSearch();
}
async function openProject(id){
  if(busy) return; busy=true; state.projectId=id; state.browseReturn="project"; state.browseSub="project";
  show(skelRows(5));
  try{ const d=await callTool("kb_load",{project:id}); if(d && d.index_tree){ state.load=d; renderProjectTree(); } else show(errBlock("Could not load project.","project")); }
  catch(e){ show(errBlock("Could not load project.","project")); } finally{ busy=false; }
}
function renderProjectTree(){
  state.view="browse"; state.browseSub="project"; setActiveTab();
  const L=state.load; if(!L){ state.browseSub="search"; return renderSearch(); }
  const tree=L.index_tree||{files:[],dirs:[]}; const title=tree.title||L.project;
  let ctx=""; if(L.context_md){ const body=stripFrontmatter(L.context_md).split("\n").slice(0,30).join("\n"); ctx='<div class="ctx"><div class="md">'+renderMarkdown(body)+'</div></div>'; }
  show('<div class="headrow"><button class="back" data-nav="browse-search">&lsaquo; Browse</button>'
        +'<span class="stamp">'+glyph("project")+'</span><h1 style="font-size:1.05rem">'+esc(title)+'</h1></div>'
      +ctx+'<p class="section-label">Concepts</p><div class="tree">'+treeHtml(tree,0)+'</div>');
}
async function openFile(path){
  if(busy) return; busy=true; state.readPath=path; show(skelRows(6));
  try{ const d=await callTool("kb_read",{path:path}); if(d && d.content!=null){ state.read=d; state.browseSub="reader"; renderReader(); } else show(errBlock("Could not read "+esc(path)+".","read")); }
  catch(e){ show(errBlock("Could not read file.","read")); } finally{ busy=false; }
}
function renderReader(){
  state.view="browse"; state.browseSub="reader"; setActiveTab();
  const R=state.read; if(!R){ state.browseSub=state.browseReturn||"search"; return renderBrowse(); }
  const meta=R.meta||{}; const path=R.path||state.readPath||""; const name=path.split("/").pop();
  let props="";
  if(meta.type) props+='<span class="chip"><span class="k">type</span> '+esc(meta.type)+'</span>';
  if(meta.status) props+='<span class="chip"><span class="dot '+statusDot(meta.status)+'"></span> '+esc(meta.status)+'</span>';
  const tags=Array.isArray(meta.tags)?meta.tags:(meta.tags?[meta.tags]:[]);
  props+=tags.map(t=>'<span class="chip">#'+esc(t)+'</span>').join("");
  const backTo=state.browseReturn==="project"?"browse-project":(state.browseReturn==="artifacts"?"browse-artifacts":"browse-search");
  show('<div class="headrow"><button class="back" data-nav="'+backTo+'">&lsaquo; back</button></div>'
      +'<div class="headrow"><span class="stamp">'+glyph(meta.type)+'</span><div><h1>'+esc(meta.title||name)+'</h1>'
        +(meta.description?'<p class="lede">'+esc(meta.description)+'</p>':'')+'</div></div>'
      +(props?'<div class="props">'+props+'</div>':'')
      +'<div class="md">'+renderMarkdown(R.content||"")+'</div>');
}
let _searchTimer=0;
function onSearchInput(v){ state.searchQuery=v; if(_searchTimer) clearTimeout(_searchTimer); _searchTimer=setTimeout(runSearch, 300); }
async function runSearch(){
  const q=(state.searchQuery||"").trim();
  if(!q){ state.searchResults=null; state.searchStatus=null; renderSearchBody(); return; }
  state.searchStatus="loading"; renderSearchBody();
  try{
    const d=await callTool("kb_search",{query:q, limit:12});
    if(d && d.error) throw new Error(d.error);
    state.searchResults=Array.isArray(d)?d:[]; state.searchStatus="ok";
  }catch(e){ state.searchStatus="error"; }
  renderSearchBody();
}
function renderSearch(){
  state.view="browse"; state.browseSub="search"; setActiveTab();
  show('<p class="eyebrow">Browse</p>'+browsePills()
    +'<div class="searchbar"><input id="sq" class="sinput" type="search" placeholder="Search the brain&hellip;" value="'+esc(state.searchQuery||"")+'" autocomplete="off"></div>'
    +'<div id="sresults"></div>');
  const sq=$("sq"); sq.oninput=()=>onSearchInput(sq.value); sq.onkeydown=(e)=>{ if(e.key==="Enter"){ if(_searchTimer) clearTimeout(_searchTimer); runSearch(); } };
  renderSearchBody(); try{ sq.focus(); }catch(e){}
}
function renderSearchBody(){
  const host=$("sresults"); if(!host) return;
  if(state.searchStatus==="loading"){ host.innerHTML=skelRows(4); scheduleHeight(); return; }
  if(state.searchStatus==="error"){ host.innerHTML=errBlock("Search failed.","search"); scheduleHeight(); return; }
  if(!state.searchResults){ host.innerHTML='<div class="empty">Type to search titles, descriptions, tags, and body.</div>'; scheduleHeight(); return; }
  if(!state.searchResults.length){ host.innerHTML='<div class="empty">No matches.</div>'; scheduleHeight(); return; }
  const maxScore=Math.max(1, ...state.searchResults.map(r=>+r.score||0));
  host.innerHTML=state.searchResults.map(r=>{
    const nm=(r.path||"").split("/").pop();
    const pct=Math.round(Math.max(0,Math.min(1,(+r.score||0)/maxScore))*100);
    return '<button class="result" data-open-path="'+esc(r.path)+'">'
      +'<div class="rtop"><h3>'+esc(r.title||nm)+'</h3>'+(r.engine?'<span class="chip">'+esc(r.engine)+'</span>':'')+'</div>'
      +'<div class="rpath">'+esc(r.path||"")+'</div>'
      +(r.description?'<p>'+esc(r.description)+'</p>':'')
      +'<div class="scorebar"><div class="track"><div class="fill" style="width:'+pct+'%"></div></div><span class="pct">'+pct+'%</span></div></button>';
  }).join("");
  scheduleHeight();
}
async function loadArtifacts(force){
  if(state.artifacts && !force){ renderArtifacts(); return; }
  state.artifactsStatus="loading"; renderArtifacts();
  try{ const d=await callTool("kb_artifacts",{}); if(d && d.error) throw new Error(d.error); state.artifacts=Array.isArray(d)?d:[]; state.artifactsStatus="ok"; }
  catch(e){ state.artifactsStatus="error"; }
  renderArtifacts();
}
function artifactRow(a){
  const stale=(a.stale===true)?'<span class="chip stale">sources changed</span>':"";
  const shared=a.shared?'<span class="chip shared">🔗 shared</span>':"";
  return '<div class="artrow" data-open-path="'+esc(a.path)+'"><div class="arthead"><span class="stamp">'+glyph("artifact")+'</span>'
    +'<span class="artt">'+esc(a.title||a.path.split("/").pop())+'</span></div>'
    +'<div class="artchips">'+(a.project?'<span class="chip proj">'+esc(a.project)+'</span>':'')+stale+shared
    +(a.timestamp?'<span class="when">'+esc(relTime(a.timestamp))+'</span>':'')+'</div></div>';
}
function renderArtifacts(){
  state.view="browse"; state.browseSub="artifacts"; setActiveTab();
  let body;
  if(state.artifactsStatus==="loading") body=skelRows(3);
  else if(state.artifactsStatus==="error") body=errBlock("Couldn't load artifacts.","artifacts");
  else { const rows=state.artifacts||[]; body = rows.length ? rows.map(artifactRow).join("") : '<div class="empty">No artifacts yet.</div>'; }
  show('<p class="eyebrow">Browse</p>'+browsePills()+'<div class="headrow"><p class="section-label" style="margin:0">Artifacts</p><button class="refresh" id="art-refresh">&#8635; Refresh</button></div>'+body);
  const rf=$("art-refresh"); if(rf) rf.onclick=()=>loadArtifacts(true);
}
function setBrowseSub(k){ state.browseSub=k; state.browseReturn=k;
  if(k==="artifacts"){ if(!state.artifacts && state.artifactsStatus!=="loading") loadArtifacts(false); else renderArtifacts(); }
  else renderSearch();
}

// ═══════════════════════════════ PEOPLE ═══════════════════════════════
async function loadTeam(){
  try{ const d=await callTool("team_state",{}); if(d && Array.isArray(d.team)){ state.team=d.team; if(state.view==="people") renderTeamStrip(); } }
  catch(e){ /* hide gracefully — no strip */ }
}
function renderTeamStrip(){
  const host=$("teamstrip"); if(!host) return;
  const team=state.team||[];
  if(!team.length){ host.innerHTML=""; host.hidden=true; scheduleHeight(); return; }
  host.hidden=false;
  host.innerHTML=team.map(t=>{
    const mins=Number(t.minutes_ago||0); const fresh = mins<=15 ? "fresh" : (mins<=120 ? "warm" : "");
    return '<span class="teamcard">'+avatarHtml(t.display_name,t.handle,t.avatar_url,"sm")
      +'<span>'+esc(t.display_name||t.handle)+(t.project?' in '+esc(t.project):'')+'</span><span class="freshdot '+fresh+'"></span></span>';
  }).join("");
  scheduleHeight();
}
async function loadExplore(showSkeleton){
  if(showSkeleton){ state.exploreStatus="loading"; renderPeople(); }
  try{ const d=await callTool("explore_state",{}); if(d && d.error) throw new Error(d.error); state.exploreData=(d&&typeof d==="object")?d:{}; state.exploreStatus="ok"; }
  catch(e){ if(!state.exploreData) state.exploreStatus="error"; }
  if(state.view==="people" && state.peopleSub!=="profile" && state.peopleSub!=="concept") renderPeople();
}
function personRow(p){
  const following=!!p.is_following; const n=Number(p.public_projects||0);
  return '<div class="person-row" data-person="'+esc(p.handle)+'">'+avatarHtml(p.display_name,p.handle,p.avatar_url,"sm")
    +'<span class="person-main"><span class="person-name">'+esc(p.display_name||p.handle)+'</span>'
    +'<span class="person-handle">@'+esc(p.handle)+'</span>'+(p.bio?'<span class="person-bio">'+esc(p.bio)+'</span>':'')+'</span>'
    +'<button class="follow-btn'+(following?" following":"")+'" data-follow="'+esc(p.handle)+'">'+(following?"Following":"Follow")+'</button></div>';
}
function renderPeople(){
  state.view="people"; state.peopleSub="people"; stopAllPolling();
  let body;
  if(state.exploreStatus==="loading"){ body=skelRows(3); }
  else if(state.exploreStatus==="error"){ body=errBlock("Couldn't load people.","people"); }
  else {
    const d=state.exploreData||{}; const people=Array.isArray(d.people)?d.people:[];
    body = people.length ? '<div class="people-list">'+people.map(personRow).join("")+'</div>' : '<div class="empty">No one to discover yet.</div>';
  }
  show('<p class="eyebrow">People</p><h1>Discover</h1><div class="teamstrip" id="teamstrip" hidden></div>'+body);
  renderTeamStrip(); startPolling();
  if(!state.team) loadTeam();
}
async function toggleFollow(handle){
  const btn=document.querySelector('[data-follow="'+CSS.escape(handle)+'"]'); const was=btn?btn.classList.contains("following"):false;
  if(btn) btn.disabled=true;
  try{
    const r=await callTool("explore_follow",{handle, follow:!was});
    if(r && r.error) throw new Error(r.error);
    const isFollowing=!!(r && r.is_following);
    const people=(state.exploreData&&state.exploreData.people)||[]; const p=people.find(x=>x.handle===handle); if(p) p.is_following=isFollowing;
    if(state.profileData && state.profileData.profile && state.profileData.profile.handle===handle) state.profileData.profile.is_following=isFollowing;
    if(state.peopleSub==="profile") renderProfile(false); else renderPeople();
  }catch(e){ if(btn) btn.disabled=false; }
}
async function openProfile(handle){
  state.peopleSub="profile"; state.profileHandle=handle; state.profileData=null; state.profileStatus="loading"; state.commonGround=null;
  renderProfile(true);
  try{ const d=await callTool("explore_profile",{handle}); if(state.profileHandle!==handle) return;
    if(d && d.error) throw new Error(d.error); state.profileData=d; state.profileStatus="ok"; }
  catch(e){ state.profileStatus="error"; }
  if(state.profileHandle===handle) renderProfile(false);
  loadCommonGround(handle);
}
async function loadCommonGround(handle){
  try{ const d=await callTool("kb_common_ground",{handle}); if(state.profileHandle!==handle) return;
    if(d && Array.isArray(d.pairs) && d.pairs.length){ state.commonGround=d.pairs; if(state.peopleSub==="profile") renderProfile(false); } }
  catch(e){ /* hide on error/empty */ }
}
function workRow(w, handle){
  return '<button class="work-item" data-work="'+esc(handle)+'|'+esc(w.path)+'"><span class="work-title">'+esc(w.title||w.path)+'</span>'
    +(w.description?'<div class="convo-preview">'+esc(w.description)+'</div>':'')
    +'<div class="chips">'+(w.type?'<span class="chip">'+esc(w.type)+'</span>':'')+(w.project?'<span class="chip proj">'+esc(w.project)+'</span>':'')+'</div></button>';
}
function renderProfile(skeleton){
  state.view="people"; state.peopleSub="profile"; stopAllPolling(); setActiveTab();
  if(skeleton || state.profileStatus==="loading"){ show('<button class="back" id="p-back">&lsaquo; People</button>'+skelRows(2)); $("p-back").onclick=()=>renderPeople(); return; }
  if(state.profileStatus==="error"){ show('<button class="back" id="p-back">&lsaquo; People</button>'+errBlock("Couldn't load this profile.","profile")); $("p-back").onclick=()=>renderPeople(); return; }
  const d=state.profileData||{}; const p=d.profile||{handle:state.profileHandle}; const work=Array.isArray(d.public_work)?d.public_work:[];
  const following=!!p.is_following; const followers=Number(p.followers||0);
  let cg="";
  if(state.commonGround && state.commonGround.length){
    cg='<div class="commonground"><p class="section-label">You both have work on</p>'+state.commonGround.map(pr=>
      '<div class="cg-row"><b>'+esc((pr.mine&&pr.mine.title)||"")+'</b> &harr; '+esc((pr.theirs&&pr.theirs.title)||"")+'</div>').join("")+'</div>';
  }
  show('<button class="back" id="p-back">&lsaquo; People</button>'
    +'<div class="phead-main">'+avatarHtml(p.display_name,p.handle,p.avatar_url,"md")
    +'<div><h1>'+esc(p.display_name||p.handle)+'</h1><div class="person-handle">@'+esc(p.handle)+'</div>'
    +(p.bio?'<p class="lede">'+esc(p.bio)+'</p>':'')
    +'<div class="pfollow-row"><button class="follow-btn'+(following?" following":"")+'" data-follow="'+esc(p.handle)+'">'+(following?"Following":"Follow")+'</button>'
    +'<span class="person-handle">'+followers+' follower'+(followers===1?"":"s")+'</span></div></div></div>'
    +cg+'<p class="section-label">Public work</p>'
    +(work.length?'<div class="work-list">'+work.map(w=>workRow(w,p.handle)).join("")+'</div>':'<div class="empty">Nothing public yet.</div>'));
  $("p-back").onclick=()=>renderPeople();
}
async function openConcept(handle, path){
  state.peopleSub="concept"; state.conceptHandle=handle; state.conceptPath=path; state.conceptData=null; state.conceptStatus="loading";
  renderConcept(true);
  try{ const d=await callTool("explore_concept",{handle, path}); if(state.conceptHandle!==handle || state.conceptPath!==path) return;
    if(d && d.error) throw new Error(d.error); state.conceptData=d; state.conceptStatus="ok"; }
  catch(e){ state.conceptStatus="error"; }
  if(state.conceptHandle===handle && state.conceptPath===path) renderConcept(false);
}
function renderConceptText(text){
  const container=document.createElement("div"); container.className="ctext";
  const paras=String(text||"").split(/\n{2,}/);
  for(const para of paras){ if(!para.trim()) continue; const p=document.createElement("p"); p.textContent=para; container.appendChild(p); }
  if(!container.children.length){ const p=document.createElement("p"); p.textContent="(empty)"; container.appendChild(p); }
  return container;
}
function renderConcept(skeleton){
  state.view="people"; state.peopleSub="concept"; stopAllPolling(); setActiveTab();
  if(skeleton || state.conceptStatus==="loading"){ show('<button class="back" id="c-back">&lsaquo; Back</button>'+skelRows(2)); $("c-back").onclick=()=>openProfile(state.profileHandle||state.conceptHandle); return; }
  if(state.conceptStatus==="error"){ show('<button class="back" id="c-back">&lsaquo; Back</button>'+errBlock("Couldn't load this.","concept")); $("c-back").onclick=()=>openProfile(state.profileHandle||state.conceptHandle); return; }
  const d=state.conceptData||{};
  show('<button class="back" id="c-back">&lsaquo; Back</button><h1>'+esc(d.title||d.path||"")+'</h1>'
    +'<div class="chips">'+(d.type?'<span class="chip">'+esc(d.type)+'</span>':'')+(d.project?'<span class="chip proj">'+esc(d.project)+'</span>':'')+'</div>'
    +'<div id="ctext-holder"></div>'
    +'<div class="composer" style="position:static"><textarea id="askinput" class="taxt" rows="2" placeholder="Ask @'+esc(d.handle||state.conceptHandle||"")+' a question&hellip;"></textarea>'
    +'<button class="act" id="askbtn">Ask</button></div><p class="cerr" id="askmsg" hidden></p>');
  $("c-back").onclick=()=>openProfile(state.profileHandle||state.conceptHandle);
  const holder=$("ctext-holder"); if(holder) holder.appendChild(renderConceptText(d.text));
  $("askbtn").onclick=sendAsk;
}
async function sendAsk(){
  const inp=$("askinput"), btn=$("askbtn"), msg=$("askmsg"); if(!inp) return;
  const question=inp.value.trim(); if(!question) return;
  btn.disabled=true; inp.disabled=true; if(msg) msg.hidden=true;
  try{
    const r=await callTool("explore_ask",{handle:state.conceptHandle, path:state.conceptPath, question});
    if(r && r.error) throw new Error(r.error);
    inp.value=""; if(msg){ msg.hidden=false; msg.style.color="var(--green)"; msg.textContent="Question sent to @"+state.conceptHandle+"."; }
  }catch(e){ if(msg){ msg.hidden=false; msg.style.color="var(--red)"; msg.textContent="Couldn't send &mdash; try again."; } }
  btn.disabled=false; inp.disabled=false; scheduleHeight();
}

// ═══════════════════════════════ ROOMS ═══════════════════════════════
async function loadRoomsState(showSkeleton){
  if(showSkeleton){ state.roomsStatus="loading"; renderRoomsList(); }
  try{ const d=await callTool("rooms_state",{}); if(d && d.error) throw new Error(d.error); state.roomsData=(d&&typeof d==="object")?d:{me:"",rooms:[]}; state.roomsStatus="ok"; }
  catch(e){ if(!state.roomsData) state.roomsStatus="error"; }
  if(state.roomsSub==="list" && state.view==="rooms") renderRoomsList();
  updateTabBadges();
}
async function loadSocialState(showSkeleton){
  if(showSkeleton){ state.socialStatus="loading"; if(state.roomsSub==="list") renderRoomsList(); }
  try{ const d=await callTool("social_state",{}); if(d && d.error) throw new Error(d.error); state.socialData=(d&&typeof d==="object")?d:{}; state.socialStatus="ok"; }
  catch(e){ if(!state.socialData) state.socialStatus="error"; }
  if(state.roomsSub==="list" && state.view==="rooms") renderRoomsList();
  updateTabBadges();
}
function memberCluster(handles){
  return '<div class="membercluster">'+(handles||[]).slice(0,4).map(h=>avatarHtml(h,h,null,"sm")).join("")+'</div>';
}
function roomRow(r){
  const budget=r.turn_budget?Math.min(100,Math.round(100*(r.messages_used||0)/r.turn_budget)):0;
  const lt=r.last_turn;
  return '<button class="room" data-open-room="'+esc(r.name||r.id)+'">'
    +memberCluster(r.member_handles)
    +'<span class="room-main"><span class="room-name">'+esc(r.name||r.id)+'</span>'
    +(r.goal?'<span class="room-goal">'+esc(r.goal)+'</span>':'')
    +(lt?'<span class="convo-preview"><b>'+esc(lt.author)+':</b> '+esc(lt.body||"")+'</span>':'')
    +(r.turn_budget?'<div class="gauge-row"><div class="budgetmeter"><div class="fill" style="width:'+budget+'%"></div></div><span class="when">'+(r.messages_used||0)+'/'+r.turn_budget+'</span></div>':'')
    +'</span>'+((r.unread|0)>0?'<span class="badge unread">'+r.unread+'</span>':'')+'</button>';
}
function convoRow(c){
  const unread=Number(c.unread||0)>0;
  return '<button class="convo'+(unread?" unread":"")+'" data-open-dm="'+esc(c.with)+'">'+avatarHtml(c.display_name,c.with,c.avatar_url,"sm")
    +'<span class="convo-main"><span class="convo-name">'+esc(c.display_name||c.with)+(c.at?'<span class="when">'+esc(relTime(c.at))+'</span>':'')+'</span>'
    +'<span class="convo-preview">'+esc(c.last||"")+'</span></span>'+(unread?'<span class="unread-dot"></span>':'')+'</button>';
}
function requestRow(handle){ return '<div class="req-row"><span class="person-handle" style="flex:1 1 auto">@'+esc(handle)+'</span><button class="accept-btn" data-accept="'+esc(handle)+'">Accept</button></div>'; }
function notifRow(n){
  const openBtn=(n.kind==="room_invite" && n.ref) ? '<button class="act" data-open-room-invite="'+esc(n.ref)+'">Open room</button>' : "";
  return '<div class="req-row"><span class="convo-preview" style="flex:1 1 auto">'+esc(n.body||"")+'</span>'+openBtn+(n.at?'<span class="when">'+esc(relTime(n.at))+'</span>':'')+'</div>';
}
function renderRoomsList(){
  state.view="rooms"; state.roomsSub="list"; setActiveTab(); startPolling();
  const rooms=(state.roomsData&&state.roomsData.rooms)||[];
  const sd=state.socialData||{}; const convos=Array.isArray(sd.conversations)?sd.conversations:[];
  const incoming=Array.isArray(sd.incoming)?sd.incoming:[]; const notifs=Array.isArray(sd.notifications)?sd.notifications:[];
  let body="";
  body+='<p class="section-label">Live rooms</p>';
  if(state.roomsStatus==="loading") body+=skelRows(2);
  else if(state.roomsStatus==="error") body+=errBlock("Couldn't load rooms.","rooms");
  else body += rooms.length ? '<div class="rooms">'+rooms.map(roomRow).join("")+'</div>' : '<div class="empty">No live rooms right now.</div>';
  body+='<p class="section-label">Direct messages</p>';
  if(state.socialStatus==="loading") body+=skelRows(2);
  else if(state.socialStatus==="error") body+=errBlock("Couldn't load messages.","social");
  else { body += convos.length ? '<div class="convos">'+convos.map(convoRow).join("")+'</div>' : '<div class="empty">No conversations yet.</div>';
    if(incoming.length) body+='<div class="requests" style="margin-top:.4rem">'+incoming.map(r=>requestRow(r.handle)).join("")+'</div>'; }
  if(notifs.length){
    body+='<div class="headrow"><p class="section-label" style="margin:1rem 0 0">Notifications</p><button class="refresh" id="rm-markread">Mark all read</button></div>'
      +'<div class="notifs">'+notifs.map(notifRow).join("")+'</div>';
  }
  show('<p class="eyebrow">Rooms</p><h1>Communications</h1>'+body);
  const mr=$("rm-markread"); if(mr) mr.onclick=markAllRead;
}
async function markAllRead(){
  const btn=$("rm-markread"); if(btn) btn.disabled=true;
  try{ await callTool("social_mark_read",{}); }catch(e){ /* best-effort */ }
  loadSocialState(false);
}
function turnKey(t){ return (t.id!=null?"i"+t.id:"")+"|"+(t.author||"")+"|"+(t.created||""); }
function contentKeyRoom(t){ return (t.author||"")+"|"+String(t.body||""); }
function roomBubble(t){
  const kind=t.kind||"message"; const me=(t.author===((state.roomsData&&state.roomsData.me)||"__none__"));
  if(kind==="system") return '<div class="row sys"><div class="bubble">'+esc(t.body||"")+'</div></div>';
  if(kind==="guest_read") return '<div class="row audit"><div class="bubble">'+esc(t.author||"guest")+" read this &mdash; "+esc(t.body||"")+'</div></div>';
  return '<div class="row'+(me?" me":"")+'">'+(me?"":avatarHtml(t.author,t.author,null,"sm"))
    +'<div class="bubble"><div class="who">'+esc(t.author||"")+'</div><div class="msg">'+esc(t.body||"")+'</div>'
    +(t.created?'<div class="tm">'+esc(relTime(t.created))+'</div>':'')+'</div></div>';
}
function appendRoomTurns(turns){
  const log=$("rlog"); if(!log) return;
  for(const t of (turns||[])){
    const key=turnKey(t); if(state.seenKeysRoom.has(key)) continue;
    if(state.seenContentRoom.has(contentKeyRoom(t))){ state.seenKeysRoom.add(key); continue; }
    state.seenKeysRoom.add(key); state.seenContentRoom.add(contentKeyRoom(t));
    log.insertAdjacentHTML("beforeend", roomBubble(t));
  }
  log.scrollTop=log.scrollHeight;
}
async function openRoomTx(name){
  state.roomsSub="room"; state.openRoom=name; state.roomTx=null; state.seenKeysRoom=new Set(); state.seenContentRoom=new Set();
  renderRoomTx(true);
  await loadRoomTx(true);
}
async function loadRoomTx(initial){
  if(state.roomsSub!=="room") return;
  try{
    const d=await callTool("room_transcript",{room:state.openRoom});
    if(state.roomsSub!=="room" || state.openRoom==null) return;
    if(d && d.error){ if(initial) showRoomTxError(); return; }
    state.roomTx=d.room||state.roomTx;
    if(initial) renderRoomTx(false);
    appendRoomTurns(d.turns);
    updateRoomComposerState();
  }catch(e){ if(initial) showRoomTxError(); }
}
function showRoomTxError(){ const log=$("rlog"); if(log) log.innerHTML=errBlock("Couldn't load this room.","roomtx"); }
function updateRoomComposerState(){
  const closed = state.roomTx && state.roomTx.status && state.roomTx.status!=="open";
  const inp=$("rinput"), snd=$("rsend"); if(!inp||!snd) return;
  inp.disabled=!!closed; snd.disabled=!!closed; inp.placeholder = closed ? "This room is closed" : "Message the room…";
}
function renderRoomTx(skeleton){
  state.view="rooms"; state.roomsSub="room"; stopAllPolling(); setActiveTab();
  const r=state.roomTx||{name:state.openRoom, goal:""};
  show('<button class="back" id="rt-back">&lsaquo; Rooms</button><h1>'+esc(r.name||state.openRoom)+'</h1>'
    +(r.goal?'<p class="lede">'+esc(r.goal)+'</p>':'')
    +(skeleton?skelRows(2):'<div class="log" id="rlog"></div>')
    +'<div class="composer"><textarea id="rinput" class="taxt" rows="1" placeholder="Message the room…"></textarea><button class="csend" id="rsend">&#10148;</button></div><p class="cerr" id="rerr" hidden></p>');
  $("rt-back").onclick=()=>{ state.roomsSub="list"; renderRoomsList(); };
  const inp=$("rinput"), snd=$("rsend");
  inp.onkeydown=(e)=>{ if(e.key==="Enter" && !e.shiftKey){ e.preventDefault(); sendRoomReply(); } };
  snd.onclick=sendRoomReply; startPolling();
}
async function sendRoomReply(){
  const inp=$("rinput"), snd=$("rsend"), err=$("rerr"), log=$("rlog"); if(!inp || !state.openRoom) return;
  const text=inp.value.trim(); if(!text) return;
  inp.value=""; if(err) err.hidden=true; inp.disabled=true; snd.disabled=true;
  const me=(state.roomsData&&state.roomsData.me)||"me";
  const optimistic={author:me, body:text, created:new Date().toISOString()};
  const tempKey="pending|"+contentKeyRoom(optimistic);
  if(log){ log.insertAdjacentHTML("beforeend", '<div class="row me" data-pending="'+esc(tempKey)+'"><div class="bubble pending"><div class="msg">'+esc(text)+'</div></div></div>'); log.scrollTop=log.scrollHeight; }
  state.seenContentRoom.add(contentKeyRoom(optimistic));
  try{
    const r=await callTool("room_reply",{room:state.openRoom, message:text});
    if(r && r.error) throw new Error(r.error);
    inp.disabled=false; snd.disabled=false; loadRoomTx(false);
  }catch(e){
    inp.value=text; inp.disabled=false; snd.disabled=false;
    if(err){ err.hidden=false; err.textContent="Couldn't send &mdash; try again."; }
  }
  scheduleHeight();
}
function partyInfoDM(handle){
  const convos=(state.socialData&&state.socialData.conversations)||[]; const c=convos.find(x=>x.with===handle);
  if(c) return {display_name:c.display_name, avatar_url:c.avatar_url}; return {display_name:handle, avatar_url:null};
}
function contentKeyDM(m){ return (m.mine?"me":(m.from||""))+"|"+String(m.body||""); }
function dmBubble(m){
  const mine=!!m.mine; const info=mine?((state.socialData&&state.socialData.me)||{}):partyInfoDM(state.openDM);
  return '<div class="row'+(mine?" me":"")+'">'+(mine?"":avatarHtml(info.display_name,state.openDM,info.avatar_url,"sm"))
    +'<div class="bubble"><div class="msg">'+esc(m.body||"")+'</div>'+(m.at?'<div class="tm">'+esc(relTime(m.at))+'</div>':'')+'</div></div>';
}
async function openDM(handle){
  state.roomsSub="dm"; state.openDM=handle; state.dmConv=null; state.seenKeysDM=new Set(); state.seenContentDM=new Set();
  renderDM(true); await loadDM(true);
}
async function loadDM(initial){
  if(state.roomsSub!=="dm") return;
  try{
    const d=await callTool("social_conversation",{with_handle:state.openDM});
    if(state.roomsSub!=="dm" || state.openDM==null) return;
    if(d && d.error){ if(initial) showDMError(); return; }
    state.dmConv=d; if(initial) renderDM(false);
    const log=$("dlog"); if(log){
      for(const m of (d.messages||[])){
        const key=(m.mine?"me":(m.from||""))+"|"+String(m.body||"")+"|"+(m.at||"");
        if(state.seenKeysDM.has(key)) continue;
        if(m.mine && state.seenContentDM.has(contentKeyDM(m))){ state.seenKeysDM.add(key); continue; }
        state.seenKeysDM.add(key); state.seenContentDM.add(contentKeyDM(m)); log.insertAdjacentHTML("beforeend", dmBubble(m));
      }
      log.scrollTop=log.scrollHeight;
      if(initial && !log.children.length) log.innerHTML='<div class="empty">Say hello.</div>';
    }
  }catch(e){ if(initial) showDMError(); }
}
function showDMError(){ const log=$("dlog"); if(log) log.innerHTML=errBlock("Couldn't load this conversation.","dm"); }
function renderDM(skeleton){
  state.view="rooms"; state.roomsSub="dm"; stopAllPolling(); setActiveTab();
  const info=partyInfoDM(state.openDM);
  show('<button class="back" id="dm-back">&lsaquo; Rooms</button>'
    +'<div class="phead-main">'+avatarHtml(info.display_name,state.openDM,info.avatar_url,"md")+'<div><h1>'+esc(info.display_name||state.openDM)+'</h1><div class="person-handle">@'+esc(state.openDM)+'</div></div></div>'
    +(skeleton?skelRows(2):'<div class="log" id="dlog"></div>')
    +'<div class="composer"><textarea id="dinput" class="taxt" rows="1" placeholder="Message…"></textarea><button class="csend" id="dsend">&#10148;</button></div><p class="cerr" id="derr" hidden></p>');
  $("dm-back").onclick=()=>{ state.roomsSub="list"; renderRoomsList(); };
  const inp=$("dinput"), snd=$("dsend");
  inp.onkeydown=(e)=>{ if(e.key==="Enter" && !e.shiftKey){ e.preventDefault(); sendDM(); } };
  snd.onclick=sendDM; startPolling();
}
async function sendDM(){
  const inp=$("dinput"), snd=$("dsend"), err=$("derr"), log=$("dlog"); if(!inp || !state.openDM) return;
  const text=inp.value.trim(); if(!text) return;
  inp.value=""; if(err) err.hidden=true; inp.disabled=true; snd.disabled=true;
  const optimistic={body:text, at:new Date().toISOString(), mine:true};
  if(log){ log.insertAdjacentHTML("beforeend", '<div class="row me"><div class="bubble pending"><div class="msg">'+esc(text)+'</div></div></div>'); log.scrollTop=log.scrollHeight; }
  state.seenContentDM.add(contentKeyDM(optimistic));
  try{ const r=await callTool("social_send",{to:state.openDM, message:text}); if(r && r.error) throw new Error(r.error);
    inp.disabled=false; snd.disabled=false; loadDM(false); }
  catch(e){ inp.value=text; inp.disabled=false; snd.disabled=false; if(err){ err.hidden=false; err.textContent="Couldn't send &mdash; try again."; } }
  scheduleHeight();
}
async function acceptRequest(handle){
  const btn=document.querySelector('[data-accept="'+CSS.escape(handle)+'"]'); if(btn){ btn.disabled=true; }
  try{ const r=await callTool("social_accept",{handle}); if(r && r.error) throw new Error(r.error); }
  catch(e){ if(btn) btn.disabled=false; return; }
  loadSocialState(false);
}

// ═══════════════════════════════ OFFICE (lazy) ═══════════════════════════════
function hash32(s){s=String(s);let h=2166136261>>>0;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619);}return h>>>0;}
function clamp(v,a,b){return v<a?a:v>b?b:v;}
function norm(s){return String(s||"").toLowerCase().replace(/[^a-z0-9]/g,"");}
function trunc(s,n){s=String(s||"");return s.length>n?s.slice(0,n-1)+"…":s;}
function hsl(h,s,l){h=(h%360+360)%360;s/=100;l/=100;const c=(1-Math.abs(2*l-1))*s,x=c*(1-Math.abs((h/60)%2-1)),m=l-c/2;let r=0,g=0,b=0;
  if(h<60){r=c;g=x;}else if(h<120){r=x;g=c;}else if(h<180){g=c;b=x;}else if(h<240){g=x;b=c;}else if(h<300){r=x;b=c;}else{r=c;b=x;}
  const to=v=>("0"+Math.round((v+m)*255).toString(16)).slice(-2); return "#"+to(r)+to(g)+to(b); }
const OFC_OL="#241a12";
const OFC_SKINS=[["#f6d0a8","#e0b184"],["#e8b58c","#cf9468"],["#cd925f","#ad7248"],["#a86c44","#875436"],["#8a5636","#6e4128"]];
const OFC_HAIRHUE=[28,18,42,300,215,265];
function paletteFor(host,session){
  const hh=hash32((host||"?")+"|"+(session||"?")); const hue=hh%360;
  const sk=OFC_SKINS[(hh>>>7)%OFC_SKINS.length]; const hhue=OFC_HAIRHUE[(hh>>>4)%OFC_HAIRHUE.length];
  return {shirt:hsl(hue,50,56),shirtDark:hsl(hue,50,43),skin:sk[0],skinDark:sk[1],hair:hsl(hhue,44,27),hairHi:hsl(hhue,44,37),hairChoice:(hh>>>3)%3};
}
const OFC_STATUS_COL={working:"#4f9d5b",blocked:"#d9603a",idle:"#d3a24a",available:"#8fbf8a",done:"#9a8a72",unknown:"#9c8f78"};
function ofcStatusColor(s){return OFC_STATUS_COL[s]||OFC_STATUS_COL.unknown;}
let ofcCanvas=null, ofcCtx=null, ofcStage=null, ofcEmpty=null, ofcCounts=null, ofcNeeds=null, ofcDot=null;
function ofcRoundRectFill(x,y,w,h,r,fill,stroke){ r=Math.min(r,h/2,w/2); ofcCtx.beginPath(); ofcCtx.moveTo(x+r,y); ofcCtx.arcTo(x+w,y,x+w,y+h,r); ofcCtx.arcTo(x+w,y+h,x,y+h,r);
  ofcCtx.arcTo(x,y+h,x,y,r); ofcCtx.arcTo(x,y,x+w,y,r); ofcCtx.closePath(); if(fill){ofcCtx.fillStyle=fill;ofcCtx.fill();} if(stroke){ofcCtx.strokeStyle=stroke;ofcCtx.lineWidth=1;ofcCtx.stroke();} }
let MF={"world":{"w":544,"h":352},"wallH":48,
 "desks":[{"desk":{"x":35,"y":73,"w":32,"h":48},"seat":{"x":51,"y":115},"screen":{"x":43,"y":81,"w":16,"h":12}},
  {"desk":{"x":115,"y":73,"w":32,"h":48},"seat":{"x":131,"y":115},"screen":{"x":123,"y":81,"w":16,"h":12}},
  {"desk":{"x":195,"y":73,"w":32,"h":48},"seat":{"x":211,"y":115},"screen":{"x":203,"y":81,"w":16,"h":12}},
  {"desk":{"x":275,"y":73,"w":32,"h":48},"seat":{"x":291,"y":115},"screen":{"x":283,"y":81,"w":16,"h":12}},
  {"desk":{"x":35,"y":156,"w":32,"h":48},"seat":{"x":51,"y":198},"screen":{"x":43,"y":164,"w":16,"h":12}},
  {"desk":{"x":115,"y":156,"w":32,"h":48},"seat":{"x":131,"y":198},"screen":{"x":123,"y":164,"w":16,"h":12}},
  {"desk":{"x":195,"y":156,"w":32,"h":48},"seat":{"x":211,"y":198},"screen":{"x":203,"y":164,"w":16,"h":12}},
  {"desk":{"x":275,"y":156,"w":32,"h":48},"seat":{"x":291,"y":198},"screen":{"x":283,"y":164,"w":16,"h":12}}],
 "confRooms":[{"rect":{"x":356,"y":48,"w":172,"h":144},"table":{"x":411,"y":101,"w":59,"h":39},"center":{"x":440,"y":120},
   "seats":[{"x":398,"y":120},{"x":482,"y":120},{"x":398,"y":146},{"x":482,"y":146},{"x":440,"y":98},{"x":440,"y":162}],"sign":{"x":360,"y":52}},
  {"rect":{"x":356,"y":192,"w":172,"h":144},"table":{"x":411,"y":245,"w":59,"h":39},"center":{"x":440,"y":264},
   "seats":[{"x":398,"y":264},{"x":482,"y":264},{"x":398,"y":290},{"x":482,"y":290},{"x":440,"y":242},{"x":440,"y":306}],"sign":{"x":360,"y":196}}],
 "lounge":{"sofa":{"x":32,"y":275,"w":32,"h":32},"seats":[{"x":46,"y":297},{"x":66,"y":297},{"x":118,"y":322},{"x":140,"y":322}]},
 "door":{"x":184,"y":344},"coffee":{"x":278.4,"y":270.4},"aquarium":{"x":243.2,"y":265.6},"board":{"x":115.2,"y":8.8,"w":48,"h":32}};
const OFC_FLOOR={back:null,front:null}; const OFC_DESK_SEAT_RAISE=24;
let ofcZ=1;
function ofcResize(){
  if(!ofcStage||!ofcCanvas) return;
  const worldW=MF.world.w, worldH=MF.world.h; const cssW=Math.max(240, Math.round(ofcStage.clientWidth||320)); const cssH=Math.round(cssW*worldH/worldW);
  const dpr=Math.min(2, window.devicePixelRatio||1);
  ofcCanvas.width=Math.round(cssW*dpr); ofcCanvas.height=Math.round(cssH*dpr); ofcCanvas.style.height=cssH+"px"; ofcZ=ofcCanvas.width/worldW;
}
function osx(wx){ return wx*ofcZ; } function osy(wy){ return wy*ofcZ; }
function opx(wx,wy,ww,wh,col){ const x=Math.round(osx(wx)),y=Math.round(osy(wy)),x2=Math.round(osx(wx+ww)),y2=Math.round(osy(wy+wh));
  ofcCtx.fillStyle=col; ofcCtx.fillRect(x,y,Math.max(1,x2-x),Math.max(1,y2-y)); }
function ofcFontPx(n){ return Math.max(8, n*ofcZ); }
function drawShadowOfc(x,y){ ofcCtx.fillStyle="rgba(24,16,6,.24)"; ofcCtx.beginPath(); ofcCtx.ellipse(osx(x),osy(y+10),8*ofcZ,3*ofcZ,0,0,6.283); ofcCtx.fill(); }
function drawBodyOfc(ent, x, feet, tSec){
  const col=paletteFor(ent.host||ent.name, ent.session||ent.name); const status=ent.status||"unknown";
  const seed=(hash32(ent.session||ent.name||"?")%1000)/1000*Math.PI*2; const local=tSec+seed;
  let lean=0, slump=0, headX=0;
  if(status==="idle"||status==="available") lean=-1.6;
  if(status==="blocked"){ slump=1.2; headX=Math.sin(local*3.2)*1.3; }
  const breathe=Math.sin(local*1.8)*0.6; const headTop=feet-26+lean+slump-breathe, torsoTop=headTop+10;
  opx(x-8,torsoTop-1,15,14,OFC_OL); opx(x-7,torsoTop,13,12,col.shirt); opx(x+1,torsoTop,6,12,col.shirtDark);
  opx(x-6+headX,headTop-1,12,12,OFC_OL); opx(x-5+headX,headTop,10,10,col.skin); opx(x+2+headX,headTop,3,10,col.skinDark);
  opx(x-6+headX,headTop-2,12,4,OFC_OL); opx(x-5+headX,headTop-1,10,3,col.hair);
  ofcCtx.fillStyle=ofcStatusColor(status);
  if(status==="blocked"){ ofcCtx.font="bold "+ofcFontPx(7)+"px system-ui"; ofcCtx.textAlign="center"; ofcCtx.fillStyle="#d9603a"; ofcCtx.fillText("!",osx(x+7),osy(headTop-8)); }
  return {headTop};
}
function ofcNamePill(name,status,x,headTop){
  const label=trunc(name||"?",14); const fs=ofcFontPx(5); ofcCtx.font="600 "+fs+"px system-ui";
  const tw=ofcCtx.measureText(label).width; const dotR=Math.max(2,2*ofcZ), padX=Math.max(3,2.4*ofcZ);
  const w=tw+dotR*2+padX*3, h=fs+6; const cx=osx(x); let bx=clamp(cx-w/2, 2, ofcCanvas.width-w-2); const by=Math.max(2, osy(headTop)-h-3);
  ofcRoundRectFill(bx,by,w,h,h/2,"rgba(28,24,18,.86)","rgba(255,255,255,.14)");
  ofcCtx.fillStyle=ofcStatusColor(status); ofcCtx.beginPath(); ofcCtx.arc(bx+padX+dotR,by+h/2,dotR,0,6.283); ofcCtx.fill();
  ofcCtx.fillStyle="#f5efe4"; ofcCtx.textAlign="left"; ofcCtx.textBaseline="middle"; ofcCtx.fillText(label, bx+padX*2+dotR*2, by+h/2+0.5);
}
function drawFlatBack(){ opx(0,0,MF.world.w,MF.world.h,"#c9b98e"); for(const c of MF.confRooms) opx(c.rect.x,c.rect.y,c.rect.w,c.rect.h,"#cfc7ad");
  if(MF.lounge&&MF.lounge.sofa){ const s=MF.lounge.sofa; opx(s.x,s.y,s.w,s.h,"#b5794a"); } }
function drawFlatFront(){ for(const d of MF.desks) opx(d.desk.x,d.desk.y,d.desk.w,d.desk.h,"#a4763f"); for(const c of MF.confRooms) opx(c.table.x,c.table.y,c.table.w,c.table.h,"#9a6b3a"); }
function drawFloorLayerOfc(img, fallbackFn){ ofcCtx.imageSmoothingEnabled=false; if(img){ try{ ofcCtx.drawImage(img,0,0,ofcCanvas.width,ofcCanvas.height); return; }catch(e){} } fallbackFn(); }
function findByName(sessions, name){ const key=norm(name); return sessions.find(s=>norm(s.name)===key || norm(s.session)===key) || null; }
function computeLayout(payload){
  const sessions=(payload.sessions||[]).filter(s=>s.status!=="done");
  const openThreads=(payload.threads||[]).filter(t=>t.status==="open");
  const lastMs=t=>{ const a=Date.parse(t.last_activity||""); if(!isNaN(a)) return a; const b=t.last_turn&&Date.parse(t.last_turn.timestamp||""); return isNaN(b)?0:b; };
  const sorted=[...openThreads].sort((a,b)=>lastMs(b)-lastMs(a));
  const nRooms=MF.confRooms.length; const active=sorted.slice(0,nRooms);
  const N=MF.desks.length, taken=new Set(), deskOf=new Map();
  for(const s of sessions){ if(!s.session) continue; let idx=hash32(s.session)%N, probes=0;
    while(taken.has(idx)&&probes<N){ idx=(idx+1)%N; probes++; } if(probes<N){ deskOf.set(s.session, idx); taken.add(idx); } }
  const desks=MF.desks.map(d=>({def:d, session:null})); const overflow=[];
  for(const s of sessions){ const idx=s.session!=null?deskOf.get(s.session):undefined; if(idx!=null) desks[idx].session=s; else overflow.push(s); }
  const loungeSeats=(MF.lounge&&MF.lounge.seats)||[];
  const lounge=overflow.map((s,i)=>({session:s, seat: loungeSeats.length? loungeSeats[i%loungeSeats.length] : null}));
  const confRooms=MF.confRooms.map((c,i)=>{ const thread=active[i]||null;
    const seated=[]; if(thread){ (thread.participants||[]).forEach((p,pi)=>{ const seat=c.seats[pi]||c.seats[0]; seated.push({name:p, session:findByName(sessions,p), seat}); }); }
    return {def:c, thread, seated}; });
  return { onlineCount:sessions.length, desks, lounge, confRooms };
}
let ofcPendingNameplates=[];
function drawConfRoomsOfc(layout,tSec,tMs){
  for(const cr of layout.confRooms){
    const c=cr.def;
    if(!cr.thread){ ofcCtx.save(); ofcCtx.globalAlpha=0.42; opx(c.rect.x,c.rect.y,c.rect.w,c.rect.h,"#0f131c"); ofcCtx.restore(); continue; }
    for(const p of cr.seated){
      drawShadowOfc(p.seat.x,p.seat.y);
      const body=p.session ? {session:p.session.session,host:p.session.host,name:p.session.name,status:p.session.status} : {session:"guest:"+p.name,host:p.name,name:p.name,status:"working"};
      const info=drawBodyOfc(body, p.seat.x, p.seat.y, tSec);
      ofcPendingNameplates.push({x:p.seat.x, headTop:info.headTop, name:p.name, status:body.status});
    }
  }
}
function ofcRender(tsMs){
  if(!ofcCanvas||!ofcCanvas.width) return;
  const tSec=(tsMs||0)/1000; ofcPendingNameplates=[];
  ofcCtx.clearRect(0,0,ofcCanvas.width,ofcCanvas.height);
  drawFloorLayerOfc(OFC_FLOOR.back, drawFlatBack);
  const layout=OFC_CUR.layout;
  if(layout){
    for(const d of layout.desks){ if(!d.session) continue; const seat=d.def.seat, y=seat.y-OFC_DESK_SEAT_RAISE;
      drawShadowOfc(seat.x, y); const info=drawBodyOfc({session:d.session.session,host:d.session.host,name:d.session.name,status:d.session.status}, seat.x, y, tSec);
      ofcPendingNameplates.push({x:seat.x, headTop:info.headTop, name:d.session.name||d.session.session, status:d.session.status}); }
    for(const l of layout.lounge){ if(!l.seat) continue; drawShadowOfc(l.seat.x, l.seat.y);
      const info=drawBodyOfc({session:l.session.session,host:l.session.host,name:l.session.name,status:l.session.status}, l.seat.x, l.seat.y, tSec);
      ofcPendingNameplates.push({x:l.seat.x, headTop:info.headTop, name:l.session.name||l.session.session, status:l.session.status}); }
  }
  drawFloorLayerOfc(OFC_FLOOR.front, drawFlatFront);
  if(layout) drawConfRoomsOfc(layout, tSec, tsMs||0);
  for(const n of ofcPendingNameplates) ofcNamePill(n.name, n.status, n.x, n.headTop);
  const anyMeeting = layout && layout.confRooms.some(c=>c.thread);
  if(ofcEmpty) ofcEmpty.hidden = !(layout && layout.onlineCount===0 && !anyMeeting);
}
const OFC_CUR={payload:null, layout:null};
function ofcUpdateHeader(d){
  if(!d||!ofcCounts) return;
  const online = d.online!=null ? d.online : (Array.isArray(d.sessions) ? d.sessions.filter(s=>s.status!=="done").length : null);
  const openThreads = Array.isArray(d.threads) ? d.threads.filter(t=>t.status==="open") : null;
  const meetings = d.meetings_open!=null ? d.meetings_open : (openThreads?openThreads.length:null);
  const needsH = d.needs_hiren!=null ? d.needs_hiren : (openThreads? openThreads.some(t=>t.last_turn && /^\s*@hiren\b/i.test(t.last_turn.message||"")) : false);
  if(online!=null) ofcCounts.textContent = online+" online"+(meetings!=null? " · "+meetings+" meeting"+(meetings===1?"":"s"):"");
  if(ofcNeeds) ofcNeeds.hidden = !needsH;
  if(ofcDot) ofcDot.style.background = (online===0) ? "#9a8a72" : "#4f9d5b";
}
let ofcPollTimer=0, ofcRafId=0;
function stopOfficeAnim(){ if(ofcPollTimer){ clearInterval(ofcPollTimer); ofcPollTimer=0; } if(ofcRafId){ cancelAnimationFrame(ofcRafId); ofcRafId=0; } }
function ofcFrame(ts){ ofcRender(ts); ofcRafId=requestAnimationFrame(ofcFrame); }
async function ofcRefresh(){
  try{ const data=await callTool("office_state",{}); if(data && !data.error){ OFC_CUR.payload=data; OFC_CUR.layout=computeLayout(data); ofcUpdateHeader(data); } }
  catch(e){ /* keep showing the last good frame */ }
}
async function ofcLoadFloorAssets(){
  try{ const c=await readResource("ui://engram/office/manifest.json"); if(c && c.text){ const m=JSON.parse(c.text); if(m && m.desks) MF=m; } }catch(e){}
  const loadImg=async(uri)=>{ const c=await readResource(uri); if(!c || !c.blob) return null;
    return await new Promise(res=>{ const img=new Image(); img.onload=()=>res(img); img.onerror=()=>res(null); img.src="data:"+(c.mimeType||"image/png")+";base64,"+c.blob; }); };
  const [back,front]=await Promise.all([loadImg("ui://engram/office/back.png"), loadImg("ui://engram/office/front.png")]);
  OFC_FLOOR.back=back; OFC_FLOOR.front=front;
}
async function renderOffice(){
  state.view="office"; stopAllPolling(); setActiveTab();
  show('<div id="office-tab"><div class="ohdr"><span class="odot" id="odot"></span><strong>Live Office</strong><span class="ocounts" id="ocounts"></span>'
    +'<span class="oneeds" id="oneeds" hidden><span class="pulse"></span>needs Hiren</span></div>'
    +'<div id="ostage"><canvas id="oc"></canvas><div id="oempty" hidden>No one\'s around right now.</div></div>'
    +'<div class="olegend"><span><span class="d" style="background:#4f9d5b"></span>working</span><span><span class="d" style="background:#d3a24a"></span>idle</span>'
    +'<span><span class="d" style="background:#d9603a"></span>blocked</span></div>'
    +'<div class="ocredit">Art: LimeZu</div></div>');
  ofcCanvas=$("oc"); ofcCtx=ofcCanvas.getContext("2d"); ofcStage=$("ostage"); ofcEmpty=$("oempty"); ofcCounts=$("ocounts"); ofcNeeds=$("oneeds"); ofcDot=$("odot");
  if(!state.officeBooted){ state.officeBooted=true; await ofcLoadFloorAssets(); }
  ofcResize(); if(!ofcRafId) ofcRafId=requestAnimationFrame(ofcFrame);
  await ofcRefresh();
  if(!document.hidden) ofcPollTimer=setInterval(ofcRefresh,5000);
}

// ═══════════════════════════════ polling + teardown ═══════════════════════════════
let pollTimers=[];
function stopAllPolling(){ pollTimers.forEach(t=>clearInterval(t)); pollTimers=[]; if(ofcPollTimer){ clearInterval(ofcPollTimer); ofcPollTimer=0; } }
function startPolling(){
  stopAllPolling(); if(document.hidden) return;
  if(state.view==="people") pollTimers.push(setInterval(loadTeam,20000));
  else if(state.view==="rooms"){
    if(state.roomsSub==="list"){ pollTimers.push(setInterval(()=>loadRoomsState(false),5000)); pollTimers.push(setInterval(()=>loadSocialState(false),5000)); }
    else if(state.roomsSub==="room" && state.roomTx && (state.roomTx.status||"open")==="open"){ pollTimers.push(setInterval(()=>loadRoomTx(false),5000)); }
    else if(state.roomsSub==="dm"){ pollTimers.push(setInterval(()=>loadDM(false),3000)); }
  } else if(state.view==="office" && ofcCanvas && !document.hidden){ ofcPollTimer=setInterval(ofcRefresh,5000); }
}
document.addEventListener("visibilitychange",()=>{ if(document.hidden){ stopAllPolling(); if(ofcRafId){ cancelAnimationFrame(ofcRafId); ofcRafId=0; } }
  else { startPolling(); if(state.view==="office" && ofcCanvas && !ofcRafId) ofcRafId=requestAnimationFrame(ofcFrame); } });

// ═══════════════════════════════ tab router ═══════════════════════════════
function goTab(t){
  if(t==="home"){ if(state.projects) renderHome(); else loadHome(); }
  else if(t==="browse"){ state.browseSub=state.browseSub==="reader"||state.browseSub==="project"?state.browseSub:"search"; renderBrowse(); if(state.browseSub==="artifacts" && !state.artifacts) loadArtifacts(false); }
  else if(t==="people"){ if(state.exploreData) renderPeople(); else loadExplore(true); }
  else if(t==="rooms"){ state.roomsSub="list"; renderRoomsList(); if(!state.roomsData) loadRoomsState(true); if(!state.socialData) loadSocialState(true); }
  else if(t==="office"){ renderOffice(); }
}
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>goTab(t.dataset.tab));

// ─────────────────── delegated clicks (survive re-renders) ─────────────────
document.addEventListener("click",(e)=>{
  const xa=e.target.closest('a[target="_blank"]'); if(xa && xa.href){ e.preventDefault(); viewLink(xa.href); return; }
  const op=e.target.closest("[data-open-path]"); if(op){ e.preventDefault(); openFile(op.getAttribute("data-open-path")); return; }
  const ml=e.target.closest("a.mdlink"); if(ml){ e.preventDefault(); const t=resolveRel(dirOf(state.readPath||""), ml.getAttribute("data-rel")); if(t) openFile(t); return; }
  const pc=e.target.closest("[data-proj]"); if(pc){ openProject(pc.getAttribute("data-proj")); return; }
  const bs=e.target.closest("[data-browsesub]"); if(bs){ setBrowseSub(bs.getAttribute("data-browsesub")); return; }
  const nav=e.target.closest("[data-nav]"); if(nav){ const w=nav.getAttribute("data-nav");
    if(w==="browse-search") setBrowseSub("search"); else if(w==="browse-artifacts") setBrowseSub("artifacts"); else if(w==="browse-project") renderProjectTree(); return; }
  const rt=e.target.closest("[data-retry]"); if(rt){ const w=rt.getAttribute("data-retry");
    if(w==="home") loadHome(); else if(w==="search") runSearch(); else if(w==="artifacts") loadArtifacts(true);
    else if(w==="project") openProject(state.projectId); else if(w==="read") openFile(state.readPath);
    else if(w==="people") loadExplore(true); else if(w==="rooms") loadRoomsState(true); else if(w==="social") loadSocialState(true);
    else if(w==="roomtx") loadRoomTx(true); else if(w==="dm") loadDM(true); return; }
  const fw=e.target.closest("[data-follow]"); if(fw){ toggleFollow(fw.getAttribute("data-follow")); return; }
  const pr=e.target.closest("[data-person]"); if(pr){ openProfile(pr.getAttribute("data-person")); return; }
  const wk=e.target.closest("[data-work]"); if(wk){ const parts=wk.getAttribute("data-work").split("|"); openConcept(parts[0], parts.slice(1).join("|")); return; }
  const ar=e.target.closest("[data-accept]"); if(ar){ acceptRequest(ar.getAttribute("data-accept")); return; }
  const rr=e.target.closest("[data-open-room]"); if(rr){ openRoomTx(rr.getAttribute("data-open-room")); return; }
  const ri=e.target.closest("[data-open-room-invite]"); if(ri){ openRoomTx(ri.getAttribute("data-open-room-invite")); return; }
  const dm=e.target.closest("[data-open-dm]"); if(dm){ openDM(dm.getAttribute("data-open-dm")); return; }
  const at=e.target.closest("[data-open-path]"); if(at){ return; }
});
async function viewLink(url){
  let w=null; try{ w=window.open(url, "_blank", "noopener"); }catch(e){ w=null; }
  if(w) return;
  await askAgent("Please show me this link so I can tap it: "+url);
}

// ─────────────────── seeding & reconcile ─────────────────
function seedFrom(data){
  if(!data) return; booted=true;
  let v = (data && typeof data.view==="string") ? data.view : null;
  if(!v){
    if(Array.isArray(data.people) || Array.isArray(data.feed)){ v="people"; state.exploreData=data; state.exploreStatus="ok"; }
    else if(Array.isArray(data.rooms)){ v="rooms"; state.roomsData=data; state.roomsStatus="ok"; }
    else if(Array.isArray(data.conversations)){ v="rooms"; state.socialData=data; state.socialStatus="ok"; }
    else if(Array.isArray(data) && data.length && data[0] && data[0].id!==undefined){ v="home"; state.projects=data; state.projectsStatus="ok"; }
    else v="home";
  }
  goTab(v);
}

// ─────────────────────────── boot ──────────────────────────
(async function init(){
  try{ if(window.openai && window.openai.toolOutput){ seedFrom(normalize(window.openai.toolOutput)); } }catch(e){}
  // ui/initialize handshake. NOTE: params MUST carry appInfo (the client-info
  // field named that way) — the other spelling silently breaks tools/call on claude.ai.
  try{ await rpcReq("ui/initialize",{appCapabilities:{availableDisplayModes:["inline","fullscreen"]},appInfo:{name:"engram-app",version:"1.0.0"},protocolVersion:"2026-01-26"});
       rpcNote("ui/notifications/initialized",{}); }catch(e){}
  if(!booted) goTab("home");
  reportHeight();
})();
</script></body></html>
"""
