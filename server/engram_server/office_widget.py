"""Office widget — an MCP App (SEP-1865) card onto the live ``/brain/office`` floor.

Hiren works in Claude Code terminals; claude.ai is his window into the workspace,
not a workplace. This widget gives that window a glance at the fixed office floor
(desks/meeting rooms/lounge baked from the LimeZu bake) so he can see who's around
and whether any meeting needs him, without leaving chat. Watch-only in v1 — tapping
an occupied meeting room hands the agent a suggestion to open it in the meetings
widget (`kb_meetings`); it never posts/creates/closes anything itself.

Unlike ``navigator.py`` / ``meetings_widget.py`` (which register the resource here
but leave the tool FUNCTIONS to app.py, where ``store`` lives), this module is
self-contained per its build brief: ``register_office_widget(mcp, settings, store)``
registers the ``ui://`` resource, its three floor-art sub-resources (served over
``resources/read`` — never a direct fetch, so the deny-by-default CSP holds), and
both tools in one place, closing over ``store`` directly.

Tools:
  kb_office()     model+app visible. Mounts the widget; returns a COMPACT roster
                  summary (not the render payload) so it stays useful as plain text
                  when the widget is off/unmounted.
  office_state()  app-only (invisible to the model, zero context cost) — the
                  widget's 5s poll target, the same payload ``/brain/office``
                  consumes (``office_api.office_payload``).

The widget HTML ports the procedural-fallback character renderer and status-color
logic straight out of ``explorer/office.html`` (paletteFor/STATUS_COL/drawProcBody
et al) rather than importing it at runtime — LimeZu sprites over ``resources/read``
are a phase-2 nicety, explicitly not blocked on here. Floor art (back/front bake +
manifest) loads once at boot over the bridge's ``resources/read`` and is cached in
the iframe; layout (hash-stable desk assignment, 2-most-recently-active meeting
rooms, overflow to the lounge) is recomputed client-side from each poll, mirroring
``office.html``'s ``relayout()`` without the pan/zoom camera or walk-in animation —
a card doesn't need either, the whole fixed floor always fits its bounds.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anyio import to_thread

from engram_server.explorer.office_api import office_payload

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from engram_server.config import Settings
    from engram_server.kbstore import KBStore

OFFICE_URI = "ui://engram/office"
OFFICE_MIME = "text/html;profile=mcp-app"
OFFICE_BACK_URI = "ui://engram/office/back.png"
OFFICE_FRONT_URI = "ui://engram/office/front.png"
OFFICE_MANIFEST_URI = "ui://engram/office/manifest.json"

# The committed bake — same files /brain/office/assets serves, read straight off
# disk (no HTTP hop) since this process already has the checkout locally.
_FLOOR_DIR = Path(__file__).parent / "explorer" / "assets" / "limezu" / "floor"


def office_resource_meta() -> dict:
    """``_meta`` for the ui:// resource — same deny-by-default CSP as the other two
    widgets: floor art comes in over ``resources/read``, never a direct fetch."""
    return {
        "ui": {
            "csp": {"connectDomains": [], "resourceDomains": []},
            "prefersBorder": True,
        },
        "openai/widgetPrefersBorder": True,
    }


def office_tool_meta(enabled: bool) -> dict | None:
    """``_meta`` for ``kb_office`` — mounts the widget; model AND app visible so it
    stays a fully useful plain-JSON tool in chat when no widget host is present."""
    return {"ui": {"resourceUri": OFFICE_URI, "visibility": ["model", "app"]}} if enabled else None


def office_state_tool_meta(enabled: bool) -> dict | None:
    """``_meta`` for the app-only poll target ``office_state``: no resourceUri (it
    mounts nothing — the widget is already up), ``visibility: ["app"]`` hides it
    from the model entirely (mirrors ``meetings_app_tool_meta``)."""
    return {"ui": {"visibility": ["app"]}} if enabled else None


def _needs_hiren(thread: dict[str, Any]) -> bool:
    """True when an OPEN thread's last turn is addressed to Hiren — same rule as
    the browser office and the meetings widget (``^\\s*@hiren`` on the message)."""
    last = thread.get("last_turn")
    if not last:
        return False
    return str(last.get("message") or "").strip().lower().startswith("@hiren")


def office_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Compact, conversational roster summary for ``kb_office``'s model-visible
    return — no floor geometry/art, just what a person glancing at the office would
    want to know. ``office_state`` (app-only) returns the FULL ``payload`` verbatim
    for the widget's own render; this is the cheap text-mode fallback + mount seed.
    """
    sessions = payload.get("sessions") or []
    open_threads = [t for t in (payload.get("threads") or []) if t.get("status") == "open"]
    rooms = payload.get("rooms") or []
    return {
        "now": payload.get("now"),
        "online": len(sessions),
        "sessions": [
            {
                "name": s.get("name") or s.get("session"),
                "status": s.get("status"),
                "project": s.get("project") or s.get("repo") or "",
                "room": s.get("room_label"),
                "quiet": bool(s.get("quiet")),
            }
            for s in sessions
        ],
        "rooms": [r.get("label") for r in rooms],
        "meetings_open": len(open_threads),
        "needs_hiren": any(_needs_hiren(t) for t in open_threads),
    }


def register_office_widget(
    mcp: "FastMCP",
    settings: "Settings",
    store: "KBStore",
    resolver: "Callable[[], Awaitable[KBStore]] | None" = None,
) -> None:
    """Register the office resource (+ 3 floor-art sub-resources) and the
    kb_office/office_state tools when ``settings.widget`` is on; a no-op when off —
    zero behavior change with the flag off, matching ``register_navigator``.

    ``resolver`` (M0.4) resolves the CALLER's store per request so a tenant sees
    their own office, never the operator's presence data (session names, cwds,
    repos). Omitted (tests, single-user) -> the fixed ``store``.
    """
    if not settings.widget:
        return

    async def _brain() -> "Path":
        if resolver is None:
            return store.root
        return (await resolver()).root

    @mcp.resource(OFFICE_URI, mime_type=OFFICE_MIME, meta=office_resource_meta())
    def office_resource() -> str:
        return OFFICE_HTML

    @mcp.resource(OFFICE_BACK_URI, mime_type="image/png")
    def office_back_resource() -> bytes:
        return (_FLOOR_DIR / "office_back.png").read_bytes()

    @mcp.resource(OFFICE_FRONT_URI, mime_type="image/png")
    def office_front_resource() -> bytes:
        return (_FLOOR_DIR / "office_front.png").read_bytes()

    @mcp.resource(OFFICE_MANIFEST_URI, mime_type="application/json")
    def office_manifest_resource() -> str:
        return (_FLOOR_DIR / "manifest.json").read_text(encoding="utf-8")

    @mcp.tool(meta=office_tool_meta(True))
    async def kb_office() -> dict[str, Any]:
        """Show Hiren his live office — call when he asks about the office, who's
        around, who's online, or what meetings are happening. Mounts the office
        widget: the fixed floor with every open session at its desk (colored by
        status) and the two meeting rooms, watch-only, updating live while the card
        stays open. Say one short line after it mounts and let him watch it.

        Returns {now, online, sessions: [{name, status, project, room, quiet}],
        rooms, meetings_open, needs_hiren} — a compact summary, not the render
        payload (the widget itself pulls that over the bridge via office_state).
        """
        now = dt.datetime.now(dt.timezone.utc)
        payload = await to_thread.run_sync(office_payload, await _brain(), now)
        return office_summary(payload)

    @mcp.tool(meta=office_state_tool_meta(True))
    async def office_state() -> dict[str, Any]:
        """App-only poll target for the office widget (invisible to the model, zero
        context cost) — the exact payload /brain/office itself consumes. The widget
        calls this every 5s while the card is visible and stops when hidden/torn
        down; never call this directly, use kb_office.

        Returns {now, sessions, recent, rooms, threads, activity}.
        """
        now = dt.datetime.now(dt.timezone.utc)
        return await to_thread.run_sync(office_payload, await _brain(), now)


# ---------------------------------------------------------------------------
# The widget. One self-contained HTML document, zero external requests, no build
# step. Raw string (NOT an f-string) — the CSS/JS braces are literal.
# ---------------------------------------------------------------------------

OFFICE_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Live Office</title>
<style>
:root{
  --bg:#141a12; --panel:#f6efe1; --panel-line:#cbb489; --ink:#3a2f24; --ink-soft:#8a7960;
  --accent:#cf7a52; --good:#4f9d5b; --warn:#d9603a; --amber-bg:#ffe9c2; --amber-bd:#e6b96a; --amber-tx:#7a4a12;
  --shadow:0 1px 2px rgba(60,40,20,.08);
}
@media (prefers-color-scheme: dark){
  :root{ --panel:#241f16; --panel-line:#4a3820; --ink:#ece6da; --ink-soft:#a09784;
    --amber-bg:#3a2a14; --amber-bd:#6b4d22; --amber-tx:#e2ae6e; --shadow:none; }
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
  font-size:13px;line-height:1.4;-webkit-font-smoothing:antialiased;}
#hdr{display:flex;align-items:center;gap:.5rem;padding:.5rem .65rem;
  background:linear-gradient(180deg,var(--panel),color-mix(in srgb,var(--panel) 88%, black));
  border-bottom:1px solid var(--panel-line);box-shadow:var(--shadow);}
#hdr .dot{width:.5rem;height:.5rem;border-radius:50%;background:var(--good);flex:0 0 auto;
  box-shadow:0 0 0 0 rgba(79,157,91,.6);animation:beat 2.4s infinite}
@keyframes beat{0%{box-shadow:0 0 0 0 rgba(79,157,91,.55)}70%{box-shadow:0 0 0 6px rgba(79,157,91,0)}100%{box-shadow:0 0 0 0 rgba(79,157,91,0)}}
#hdr strong{font-size:.85rem;font-weight:750;letter-spacing:-0.01em}
#counts{color:var(--ink-soft);font-size:.74rem;font-weight:600}
#needsbadge{display:inline-flex;align-items:center;gap:.3rem;font-size:.68rem;font-weight:700;color:var(--amber-tx);
  background:var(--amber-bg);border:1px solid var(--amber-bd);border-radius:999px;padding:.12rem .5rem;margin-left:auto;}
#needsbadge .pulse{width:.4rem;height:.4rem;border-radius:50%;background:var(--amber-tx);animation:pulse 1.6s infinite}
@keyframes pulse{0%{opacity:.4}50%{opacity:1}100%{opacity:.4}}
#stage{position:relative;background:#0f130d;}
canvas#c{display:block;width:100%;image-rendering:pixelated;image-rendering:crisp-edges;cursor:pointer;}
#empty{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);text-align:center;color:#e7ead6;
  font-size:.8rem;font-weight:600;pointer-events:none;background:rgba(14,18,30,.72);
  border:1px solid rgba(120,130,110,.35);border-radius:12px;padding:.7rem 1rem;max-width:80%;}
#legend{display:flex;flex-wrap:wrap;gap:.55rem;padding:.4rem .65rem;background:var(--panel);
  border-top:1px solid var(--panel-line);}
.lg{display:inline-flex;align-items:center;gap:.3rem;font-size:.68rem;color:var(--ink-soft);font-weight:600}
.lg .d{width:.45rem;height:.45rem;border-radius:50%}
@media (prefers-reduced-motion: reduce){ *{ animation:none !important; transition:none !important; } }
</style></head>
<body>
<div id="hdr"><span class="dot" id="livedot"></span><strong>Live Office</strong>
  <span id="counts"></span><span id="needsbadge" hidden><span class="pulse"></span>needs Hiren</span></div>
<div id="stage"><canvas id="c"></canvas><div id="empty" hidden>No one's around right now.</div></div>
<div id="legend">
  <span class="lg"><span class="d" style="background:#4f9d5b"></span>working</span>
  <span class="lg"><span class="d" style="background:#d3a24a"></span>idle</span>
  <span class="lg"><span class="d" style="background:#d9603a"></span>blocked</span>
  <span class="lg"><span class="d" style="background:#8fbf8a"></span>available</span>
</div>
<script>
"use strict";
const $=(id)=>document.getElementById(id);
const canvas=$("c"), ctx=canvas.getContext("2d"), stage=$("stage"), emptyEl=$("empty"),
      countsEl=$("counts"), needsEl=$("needsbadge"), livedotEl=$("livedot");

// ─────────────────────────────── height ───────────────────────────────
function reportHeight(){
  const el=document.documentElement, prev=el.style.height; el.style.height="max-content";
  const h=Math.ceil(el.getBoundingClientRect().height); el.style.height=prev;
  try{ if(window.openai && typeof window.openai.notifyIntrinsicHeight==="function") window.openai.notifyIntrinsicHeight(h); }catch(e){}
  try{ window.parent.postMessage({jsonrpc:"2.0",method:"ui/notifications/size-changed",params:{width:Math.ceil(window.innerWidth),height:h}},"*"); }catch(e){}
}
let _rafH=0; function scheduleHeight(){ if(_rafH) cancelAnimationFrame(_rafH); _rafH=requestAnimationFrame(reportHeight); }
try{ new ResizeObserver(scheduleHeight).observe(document.body); }catch(e){ window.addEventListener("resize",scheduleHeight); }

// ─────────────────────────── MCP Apps bridge ──────────────────────────
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
  // Host recycling this iframe — stop all polling/animation so a torn-down
  // widget never keeps ticking server-side, then ack so it can proceed.
  if(m.method==="ui/resource-teardown"){
    stopAll();
    if(m.id!==undefined) window.parent.postMessage({jsonrpc:"2.0",id:m.id,result:{}},"*");
    return;
  }
},{passive:true});
// The python SDK wraps list/dict tool returns in a single-key {"result": ...}
// envelope for structured content — unwrap it EVERYWHERE or every shape check
// silently fails on claude.ai.
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
  if(window.openai && window.openai.callTool){ return normalize(await window.openai.callTool(name,args||{})); }
  return normalize(await rpcReq("tools/call",{name,arguments:args||{}}));
}
// re-engage the agent to open a meeting in the meetings widget. content MUST be
// an ARRAY of blocks — a single object silently fails to wake Claude.
async function askAgent(text){
  try{ await rpcReq("ui/message",{role:"user",content:[{type:"text",text:text}]},6000); return true; }catch(e){}
  try{ if(window.openai && typeof window.openai.sendFollowUpMessage==="function"){ window.openai.sendFollowUpMessage({prompt:text}); return true; } }catch(e){}
  return false;
}
// resources/read -> {contents:[{uri,mimeType,text?|blob?}]} — the floor art path,
// never a direct fetch (the resource CSP is deny-by-default).
async function readResource(uri){
  try{ const r=await rpcReq("resources/read",{uri},8000); const c=(r&&r.contents&&r.contents[0])||null; return c; }
  catch(e){ return null; }
}

// ─────────────────────────── small helpers (ported from office.html) ──────────
function hash32(s){s=String(s);let h=2166136261>>>0;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619);}return h>>>0;}
function clamp(v,a,b){return v<a?a:v>b?b:v;}
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function norm(s){return String(s||"").toLowerCase().replace(/[^a-z0-9]/g,"");}
function trunc(s,n){s=String(s||"");return s.length>n?s.slice(0,n-1)+"…":s;}
function hsl(h,s,l){h=(h%360+360)%360;s/=100;l/=100;const c=(1-Math.abs(2*l-1))*s,x=c*(1-Math.abs((h/60)%2-1)),m=l-c/2;let r=0,g=0,b=0;
  if(h<60){r=c;g=x;}else if(h<120){r=x;g=c;}else if(h<180){g=c;b=x;}else if(h<240){g=x;b=c;}else if(h<300){r=x;b=c;}else{r=c;b=x;}
  const to=v=>("0"+Math.round((v+m)*255).toString(16)).slice(-2); return "#"+to(r)+to(g)+to(b); }
const OL="#241a12";
const SKINS=[["#f6d0a8","#e0b184"],["#e8b58c","#cf9468"],["#cd925f","#ad7248"],["#a86c44","#875436"],["#8a5636","#6e4128"]];
const HAIRHUE=[28,18,42,300,215,265];
function paletteFor(host,session){
  const hh=hash32((host||"?")+"|"+(session||"?")); const hue=hh%360;
  const sk=SKINS[(hh>>>7)%SKINS.length]; const hhue=HAIRHUE[(hh>>>4)%HAIRHUE.length];
  return {shirt:hsl(hue,50,56),shirtDark:hsl(hue,50,43),shirtHi:hsl(hue,54,67),
    skin:sk[0],skinDark:sk[1],hair:hsl(hhue,44,27),hairHi:hsl(hhue,44,37),hairChoice:(hh>>>3)%3};
}
const STATUS_COL={working:"#4f9d5b",blocked:"#d9603a",idle:"#d3a24a",available:"#8fbf8a",done:"#9a8a72",unknown:"#9c8f78"};
function statusColor(s){return STATUS_COL[s]||STATUS_COL.unknown;}
function roundRect(x,y,w,h,r){r=Math.min(r,h/2,w/2);ctx.beginPath();ctx.moveTo(x+r,y);ctx.arcTo(x+w,y,x+w,y+h,r);ctx.arcTo(x+w,y+h,x,y+h,r);ctx.arcTo(x,y+h,x,y,r);ctx.arcTo(x,y,x+w,y,r);ctx.closePath();}
function roundRectFill(x,y,w,h,r,fill,stroke){ roundRect(x,y,w,h,r); if(fill){ctx.fillStyle=fill;ctx.fill();} if(stroke){ctx.strokeStyle=stroke;ctx.lineWidth=1;ctx.stroke();} }

// ─────────────────────────── manifest + floor art ──────────────────────────
// Inlined default = the committed 544x352 bake (mirrors office.html's MF_DEFAULT)
// so the widget NEVER renders blank even if resources/read fails or the host
// doesn't support it yet — a fresh fetch (below) overrides it when it lands.
let MF={"world":{"w":544,"h":352},"wallH":48,
 "desks":[
  {"desk":{"x":35,"y":73,"w":32,"h":48},"seat":{"x":51,"y":115},"screen":{"x":43,"y":81,"w":16,"h":12}},
  {"desk":{"x":115,"y":73,"w":32,"h":48},"seat":{"x":131,"y":115},"screen":{"x":123,"y":81,"w":16,"h":12}},
  {"desk":{"x":195,"y":73,"w":32,"h":48},"seat":{"x":211,"y":115},"screen":{"x":203,"y":81,"w":16,"h":12}},
  {"desk":{"x":275,"y":73,"w":32,"h":48},"seat":{"x":291,"y":115},"screen":{"x":283,"y":81,"w":16,"h":12}},
  {"desk":{"x":35,"y":156,"w":32,"h":48},"seat":{"x":51,"y":198},"screen":{"x":43,"y":164,"w":16,"h":12}},
  {"desk":{"x":115,"y":156,"w":32,"h":48},"seat":{"x":131,"y":198},"screen":{"x":123,"y":164,"w":16,"h":12}},
  {"desk":{"x":195,"y":156,"w":32,"h":48},"seat":{"x":211,"y":198},"screen":{"x":203,"y":164,"w":16,"h":12}},
  {"desk":{"x":275,"y":156,"w":32,"h":48},"seat":{"x":291,"y":198},"screen":{"x":283,"y":164,"w":16,"h":12}}],
 "confRooms":[
  {"rect":{"x":356,"y":48,"w":172,"h":144},"table":{"x":411,"y":101,"w":59,"h":39},"center":{"x":440,"y":120},
   "seats":[{"x":398,"y":120},{"x":482,"y":120},{"x":398,"y":146},{"x":482,"y":146},{"x":440,"y":98},{"x":440,"y":162}],"sign":{"x":360,"y":52}},
  {"rect":{"x":356,"y":192,"w":172,"h":144},"table":{"x":411,"y":245,"w":59,"h":39},"center":{"x":440,"y":264},
   "seats":[{"x":398,"y":264},{"x":482,"y":264},{"x":398,"y":290},{"x":482,"y":290},{"x":440,"y":242},{"x":440,"y":306}],"sign":{"x":360,"y":196}}],
 "lounge":{"sofa":{"x":32,"y":275,"w":32,"h":32},"seats":[{"x":46,"y":297},{"x":66,"y":297},{"x":118,"y":322},{"x":140,"y":322}]},
 "door":{"x":184,"y":344},"coffee":{"x":278.4,"y":270.4},"aquarium":{"x":243.2,"y":265.6},"board":{"x":115.2,"y":8.8,"w":48,"h":32}};
const FLOOR={back:null,front:null};
const DESK_SEAT_RAISE=24;   // lift a desk sitter so head/shoulders clear the tall baked desk

async function loadFloorAssets(){
  try{
    const c=await readResource("ui://engram/office/manifest.json");
    if(c && c.text){ const m=JSON.parse(c.text); if(m && m.desks) MF=m; }
  }catch(e){}
  const loadImg=async(uri)=>{
    const c=await readResource(uri); if(!c || !c.blob) return null;
    return await new Promise(res=>{ const img=new Image(); img.onload=()=>res(img); img.onerror=()=>res(null);
      img.src="data:"+(c.mimeType||"image/png")+";base64,"+c.blob; });
  };
  const [back,front]=await Promise.all([loadImg("ui://engram/office/back.png"), loadImg("ui://engram/office/front.png")]);
  FLOOR.back=back; FLOOR.front=front;
}

// ─────────────────────────── canvas sizing ──────────────────────────
let Z=1;
function resizeCanvas(){
  const worldW=MF.world.w, worldH=MF.world.h;
  const cssW=Math.max(240, Math.round(stage.clientWidth||canvas.clientWidth||320));
  const cssH=Math.round(cssW*worldH/worldW);
  const dpr=Math.min(2, window.devicePixelRatio||1);
  canvas.width=Math.round(cssW*dpr); canvas.height=Math.round(cssH*dpr);
  canvas.style.height=cssH+"px";
  Z=canvas.width/worldW;
}
try{ new ResizeObserver(()=>{ resizeCanvas(); render(performance.now()); scheduleHeight(); }).observe(stage); }
catch(e){ window.addEventListener("resize",()=>{ resizeCanvas(); render(performance.now()); }); }

function sx(wx){ return wx*Z; } function sy(wy){ return wy*Z; }
function px(wx,wy,ww,wh,col){ const x=Math.round(sx(wx)),y=Math.round(sy(wy)),x2=Math.round(sx(wx+ww)),y2=Math.round(sy(wy+wh));
  ctx.fillStyle=col; ctx.fillRect(x,y,Math.max(1,x2-x),Math.max(1,y2-y)); }
function fontPx(n){ return Math.max(8, n*Z); }
function label3(wx,wy,glyph,size,color,bubble){
  const x=sx(wx),y=sy(wy),fs=fontPx(size);
  if(bubble){ ctx.fillStyle="#fffaf0"; ctx.strokeStyle="rgba(120,60,30,.35)"; ctx.lineWidth=1;
    ctx.beginPath(); ctx.arc(x,y,fs*0.78,0,6.283); ctx.fill(); ctx.stroke(); }
  ctx.fillStyle=color; ctx.font="bold "+fs+"px system-ui"; ctx.textAlign="center"; ctx.textBaseline="middle"; ctx.fillText(glyph,x,y);
}

// ─────────────────────────── procedural body (ported from office.html's drawProcBody) ──
function drawShadow(x,y){
  ctx.fillStyle="rgba(24,16,6,.24)";
  ctx.beginPath(); ctx.ellipse(sx(x),sy(y+10),8*Z,3*Z,0,0,6.283); ctx.fill();
}
function drawBody(ent, x, feet, tSec){
  const col=paletteFor(ent.host||ent.name, ent.session||ent.name);
  const status=ent.status||"unknown";
  const seed=(hash32(ent.session||ent.name||"?")%1000)/1000*Math.PI*2;
  const local=tSec+seed;
  let lean=0, slump=0, headX=0;
  if(status==="idle"||status==="available") lean=-1.6;
  if(status==="blocked"){ slump=1.2; headX=Math.sin(local*3.2)*1.3; }
  const breathe=Math.sin(local*1.8)*0.6;
  const headTop=feet-26+lean+slump-breathe, torsoTop=headTop+10;
  px(x-8,torsoTop+2,3,15,"#6f4d2c"); px(x+6,torsoTop+2,3,15,"#6f4d2c");
  px(x-8,torsoTop+1,17,2,"#7d5836");
  if(status==="idle"||status==="available") px(x-8,torsoTop,17,2,"#87613b");
  px(x-7,torsoTop-1,15,14,OL); px(x-6,torsoTop,13,12,col.shirt); px(x+1,torsoTop,6,12,col.shirtDark); px(x-6,torsoTop,13,2,col.shirtHi);
  const typing=status==="working";
  let lhY=torsoTop+3, rhY=torsoTop+3;
  if(typing){ const beat=Math.floor(tSec*1000/110)%2; lhY=torsoTop+5+(beat?-1:1); rhY=torsoTop+5+(beat?1:-1); }
  px(x-8,torsoTop+1,3,typing?5:7,OL); px(x+6,torsoTop+1,3,typing?5:7,OL);
  px(x-7,torsoTop+1,2,typing?4:6,col.shirtDark); px(x+6,torsoTop+1,2,typing?4:6,col.shirtDark);
  px(x-8,lhY,3,3,OL); px(x+6,rhY,3,3,OL);
  px(x-7,lhY,2,2,col.skin); px(x+6,rhY,2,2,col.skin);
  if(status==="idle"||status==="available"){ px(x+5,torsoTop-2,4,4,OL); px(x+5,torsoTop-1,3,3,"#e8ddc8"); px(x+5,torsoTop-2,3,1,"#b98a55"); }
  px(x-6+headX,headTop-1,12,12,OL); px(x-5+headX,headTop,10,10,col.skin); px(x+2+headX,headTop,3,10,col.skinDark);
  px(x-6+headX,headTop-2,12,4,OL); px(x-5+headX,headTop-1,10,3,col.hair); px(x-5+headX,headTop-1,10,1,col.hairHi);
  if(col.hairChoice===0){ px(x-5+headX,headTop-1,2,6,col.hair); px(x+3+headX,headTop-1,2,6,col.hair); }
  else if(col.hairChoice===1){ px(x-5+headX,headTop-2,10,3,col.hair); px(x-5+headX,headTop-2,10,1,col.hairHi); }
  else { px(x+2+headX,headTop-1,3,3,col.hair); }
  const blink=Math.sin(local*1.6)>0.965;
  if(!blink){ const ex=x-3+headX; px(ex,headTop+4,1,2,"#2a2018"); px(ex+4,headTop+4,1,2,"#2a2018"); }
  else px(x-3+headX,headTop+5,5,1,"#2a2018");
  if(status==="blocked") label3(x+7,headTop-8,"!",7,"#d9603a",true);
  else if(typing) label3(x+8,headTop-7,"⌨",6.5,"#5a4a34",false);
  return {headTop};
}
function drawNamePill(name,status,x,headTop){
  const label=trunc(name||"?",14);
  const fs=fontPx(5);
  ctx.font="600 "+fs+"px system-ui";
  const tw=ctx.measureText(label).width;
  const dotR=Math.max(2,2*Z), padX=Math.max(3,2.4*Z);
  const w=tw+dotR*2+padX*3, h=fs+6;
  const cx=sx(x); let bx=clamp(cx-w/2, 2, canvas.width-w-2);
  const by=Math.max(2, sy(headTop)-h-3);
  roundRectFill(bx,by,w,h,h/2,"rgba(28,24,18,.86)","rgba(255,255,255,.14)");
  ctx.fillStyle=statusColor(status); ctx.beginPath(); ctx.arc(bx+padX+dotR,by+h/2,dotR,0,6.283); ctx.fill();
  ctx.fillStyle="#f5efe4"; ctx.textAlign="left"; ctx.textBaseline="middle";
  ctx.fillText(label, bx+padX*2+dotR*2, by+h/2+0.5);
}

// ─────────────────────────── flat fallback (never blank if the baked pngs fail) ──
function drawFlatBack(){
  px(0,0,MF.world.w,MF.world.h,"#c9b98e");
  for(const c of MF.confRooms) px(c.rect.x,c.rect.y,c.rect.w,c.rect.h,"#cfc7ad");
  if(MF.lounge&&MF.lounge.sofa){ const s=MF.lounge.sofa; px(s.x,s.y,s.w,s.h,"#b5794a"); }
}
function drawFlatFront(){
  for(const d of MF.desks) px(d.desk.x,d.desk.y,d.desk.w,d.desk.h,"#a4763f");
  for(const c of MF.confRooms) px(c.table.x,c.table.y,c.table.w,c.table.h,"#9a6b3a");
}
function drawFloorLayer(img, fallbackFn){
  ctx.imageSmoothingEnabled=false;
  if(img){ try{ ctx.drawImage(img,0,0,canvas.width,canvas.height); return; }catch(e){} }
  fallbackFn();
}

// ─────────────────────────── layout (hash-stable desks, 2 busiest rooms, lounge overflow) ──
function findByName(sessions, name){
  const key=norm(name);
  return sessions.find(s=>norm(s.name)===key || norm(s.session)===key || (key.length>3&&norm(s.name).includes(key))) || null;
}
function computeLayout(payload){
  const sessions=(payload.sessions||[]).filter(s=>s.status!=="done");
  const openThreads=(payload.threads||[]).filter(t=>t.status==="open");
  const lastMs=t=>{ const a=Date.parse(t.last_activity||""); if(!isNaN(a)) return a;
    const b=t.last_turn&&Date.parse(t.last_turn.timestamp||""); return isNaN(b)?0:b; };
  const sorted=[...openThreads].sort((a,b)=>lastMs(b)-lastMs(a));
  const nRooms=MF.confRooms.length;
  const active=sorted.slice(0,nRooms), queued=sorted.slice(nRooms);

  const N=MF.desks.length, taken=new Set(), deskOf=new Map();
  for(const s of sessions){
    if(!s.session) continue;
    let idx=hash32(s.session)%N, probes=0;
    while(taken.has(idx)&&probes<N){ idx=(idx+1)%N; probes++; }
    if(probes<N){ deskOf.set(s.session, idx); taken.add(idx); }
  }
  const desks=MF.desks.map(d=>({def:d, session:null}));
  const overflow=[];
  for(const s of sessions){
    const idx=s.session!=null?deskOf.get(s.session):undefined;
    if(idx!=null) desks[idx].session=s; else overflow.push(s);
  }
  const loungeSeats=(MF.lounge&&MF.lounge.seats)||[];
  const lounge=overflow.map((s,i)=>({session:s, seat: loungeSeats.length? loungeSeats[i%loungeSeats.length] : null}));

  const confRooms=MF.confRooms.map((c,i)=>{
    const thread=active[i]||null;
    const needsHiren=!!thread && !!thread.last_turn && /^\s*@hiren\b/i.test(thread.last_turn.message||"");
    const seated=[];
    if(thread){ (thread.participants||[]).forEach((p,pi)=>{ const seat=c.seats[pi]||c.seats[0];
      seated.push({name:p, session:findByName(sessions,p), seat}); }); }
    return {def:c, thread, needsHiren, seated};
  });

  return { onlineCount:sessions.length, desks, lounge, confRooms, queuedThreads:queued };
}

// ─────────────────────────── conference rooms ──────────────────────────
let pendingNameplates=[];
function drawRoomSign(c,label){
  const fs=fontPx(6.6);
  ctx.font="700 "+fs+"px system-ui";
  const tw=ctx.measureText(trunc(label,26)).width;
  const dotR=Math.max(2,2.2*Z), padX=Math.max(4,3.5*Z);
  const w=tw+dotR*2+padX*3, h=fs+7;
  const bx=sx(c.sign.x), by=sy(c.sign.y+6)-h/2;
  roundRectFill(bx,by,w,h,Math.min(h/2,9),"rgba(28,34,44,.93)","rgba(126,164,204,.5)");
  ctx.fillStyle="#8fe09c"; ctx.beginPath(); ctx.arc(bx+padX+dotR,by+h/2,dotR,0,6.283); ctx.fill();
  ctx.fillStyle="#eef4fa"; ctx.textAlign="left"; ctx.textBaseline="middle"; ctx.font="700 "+fs+"px system-ui";
  ctx.fillText(trunc(label,26), bx+padX*2+dotR*2, by+h/2+0.5);
}
function drawNeedsHirenBadge(c,t){
  const fs=fontPx(6.2); ctx.font="800 "+fs+"px system-ui";
  const label="❓ needs Hiren"; const tw=ctx.measureText(label).width;
  const bx=sx(c.rect.x+c.rect.w)-tw-16, by=sy(c.rect.y)-fs*0.9;
  const pulse=0.5+0.5*Math.sin(t*0.005);
  roundRectFill(bx-7,by-fs*0.72,tw+14,fs*1.7,fs*0.85,"rgba(230,144,42,"+(0.85+0.15*pulse).toFixed(3)+")","rgba(140,80,20,.5)");
  ctx.fillStyle="#fff6ea"; ctx.textAlign="left"; ctx.textBaseline="middle"; ctx.fillText(label,bx,by);
}
function drawConfRooms(layout,tSec,tMs){
  for(const cr of layout.confRooms){
    const c=cr.def;
    if(!cr.thread){
      ctx.save(); ctx.globalAlpha=0.42; px(c.rect.x,c.rect.y,c.rect.w,c.rect.h,"#0f131c"); ctx.restore();
      const pulse=0.5+0.3*Math.sin(tMs*0.002);
      ctx.globalAlpha=pulse; ctx.fillStyle="#c3ccd4"; ctx.font=fontPx(6)+"px system-ui";
      ctx.textAlign="center"; ctx.textBaseline="middle"; ctx.fillText("z z z", sx(c.center.x), sy(c.center.y)); ctx.globalAlpha=1;
      continue;
    }
    for(const p of cr.seated){
      drawShadow(p.seat.x,p.seat.y);
      const body=p.session ? {session:p.session.session,host:p.session.host,name:p.session.name,status:p.session.status}
                            : {session:"guest:"+p.name,host:p.name,name:p.name,status:"working"};
      const info=drawBody(body, p.seat.x, p.seat.y, tSec);
      pendingNameplates.push({x:p.seat.x, headTop:info.headTop, name:p.name, status:body.status});
    }
    drawRoomSign(c, "MEETING: "+cr.thread.thread);
    if(cr.needsHiren) drawNeedsHirenBadge(c, tMs);
  }
  if(layout.queuedThreads && layout.queuedThreads.length){
    const room=MF.confRooms[MF.confRooms.length-1];
    if(room){
      const fs=fontPx(5); ctx.font="600 "+fs+"px system-ui"; ctx.fillStyle="#c7bfa8";
      ctx.textAlign="left"; ctx.textBaseline="top";
      ctx.fillText(layout.queuedThreads.length+" more waiting…", sx(room.rect.x+4), sy(room.rect.y+room.rect.h+4));
    }
  }
}
function drawDeskScreens(layout){
  for(const d of layout.desks){
    const scr=d.def.screen; if(!scr) continue;
    const col = !d.session ? "#242832" : d.session.status==="blocked" ? "#7a2a1e" : d.session.status==="working" ? "#2f5b3a" : "#2a3d55";
    px(scr.x-0.5,scr.y-0.5,scr.w+1,scr.h+1,OL);
    px(scr.x,scr.y,scr.w,scr.h,col);
    px(scr.x,scr.y+scr.h*0.6,scr.w,Math.max(1,scr.h*0.18),"rgba(255,255,255,.10)");
  }
}

// ─────────────────────────── render ──────────────────────────
const CUR={payload:null, layout:null};
function render(tsMs){
  if(!canvas.width || !MF) return;
  const tSec=(tsMs||0)/1000;
  pendingNameplates=[];
  ctx.clearRect(0,0,canvas.width,canvas.height);
  drawFloorLayer(FLOOR.back, drawFlatBack);
  const layout=CUR.layout;
  if(layout){
    for(const d of layout.desks){ if(!d.session) continue;
      const seat=d.def.seat, y=seat.y-DESK_SEAT_RAISE;
      drawShadow(seat.x, y);
      const info=drawBody({session:d.session.session,host:d.session.host,name:d.session.name,status:d.session.status}, seat.x, y, tSec);
      pendingNameplates.push({x:seat.x, headTop:info.headTop, name:d.session.name||d.session.session, status:d.session.status});
    }
    for(const l of layout.lounge){ if(!l.seat) continue;
      drawShadow(l.seat.x, l.seat.y);
      const info=drawBody({session:l.session.session,host:l.session.host,name:l.session.name,status:l.session.status}, l.seat.x, l.seat.y, tSec);
      pendingNameplates.push({x:l.seat.x, headTop:info.headTop, name:l.session.name||l.session.session, status:l.session.status});
    }
  }
  drawFloorLayer(FLOOR.front, drawFlatFront);
  if(layout){ drawDeskScreens(layout); drawConfRooms(layout, tSec, tsMs||0); }
  for(const n of pendingNameplates) drawNamePill(n.name, n.status, n.x, n.headTop);
  const anyMeeting = layout && layout.confRooms.some(c=>c.thread);
  emptyEl.hidden = !(layout && layout.onlineCount===0 && !anyMeeting);
}

// ─────────────────────────── header + counts ──────────────────────────
function updateHeaderFrom(d){
  if(!d) return;
  const online = d.online!=null ? d.online : (Array.isArray(d.sessions) ? d.sessions.filter(s=>s.status!=="done").length : null);
  const openThreads = Array.isArray(d.threads) ? d.threads.filter(t=>t.status==="open") : null;
  const meetings = d.meetings_open!=null ? d.meetings_open : (openThreads?openThreads.length:null);
  const needsH = d.needs_hiren!=null ? d.needs_hiren : (openThreads? openThreads.some(t=>t.last_turn && /^\s*@hiren\b/i.test(t.last_turn.message||"")) : false);
  if(online!=null) countsEl.textContent = online+" online"+(meetings!=null? " · "+meetings+" meeting"+(meetings===1?"":"s"):"");
  needsEl.hidden = !needsH;
  livedotEl.style.background = (online===0) ? "#9a8a72" : "#4f9d5b";
}

// ─────────────────────────── polling + animation lifecycle ──────────────────────────
let pollTimer=0, rafId=0;
function stopAll(){ if(pollTimer){ clearInterval(pollTimer); pollTimer=0; } if(rafId){ cancelAnimationFrame(rafId); rafId=0; } }
function startAnim(){ if(!rafId) rafId=requestAnimationFrame(frame); }
function frame(ts){ render(ts); rafId=requestAnimationFrame(frame); }
async function refreshState(){
  try{
    const data=await callTool("office_state",{});
    if(data && !data.error){ CUR.payload=data; CUR.layout=computeLayout(data); updateHeaderFrom(data); }
  }catch(e){ /* keep showing the last good frame; a single missed poll is invisible */ }
}
function startPolling(){ if(pollTimer) clearInterval(pollTimer); if(!document.hidden) pollTimer=setInterval(refreshState,5000); }
document.addEventListener("visibilitychange", ()=>{ if(document.hidden){ stopAll(); } else { startAnim(); refreshState(); startPolling(); } });

// tap an occupied meeting room -> hand the agent a suggestion to open it in the
// meetings widget (kb_meetings). Watch-only otherwise — no post/create/close here.
canvas.addEventListener("click",(e)=>{
  const layout=CUR.layout; if(!layout) return;
  const rect=canvas.getBoundingClientRect();
  const px_=(e.clientX-rect.left)*(canvas.width/Math.max(1,rect.width));
  const py_=(e.clientY-rect.top)*(canvas.height/Math.max(1,rect.height));
  const wx=px_/Z, wy=py_/Z;
  for(const cr of layout.confRooms){
    if(!cr.thread) continue; const r=cr.def.rect;
    if(wx>=r.x && wx<=r.x+r.w && wy>=r.y && wy<=r.y+r.h){
      askAgent('Open the meeting "'+cr.thread.thread+'" — show me the transcript and let me reply as Hiren.');
      return;
    }
  }
});

// ─────────────────── seeding (kb_office's own tool-result is only the compact
// summary — not enough to render the floor; it seeds the header, then boot
// immediately fetches the full office_state payload for the actual render) ──
function seedFrom(data){ if(data) updateHeaderFrom(data); }

// ─────────────────────────── boot ──────────────────────────
(async function init(){
  try{ if(window.openai && window.openai.toolOutput){ seedFrom(normalize(window.openai.toolOutput)); } }catch(e){}
  try{ await rpcReq("ui/initialize",{appCapabilities:{availableDisplayModes:["inline"]},appInfo:{name:"engram-office",version:"1.0.0"},protocolVersion:"2026-01-26"});
       rpcNote("ui/notifications/initialized",{}); }catch(e){}
  await loadFloorAssets();
  resizeCanvas();
  startAnim();
  await refreshState();
  startPolling();
  reportHeight();
})();
</script></body></html>
"""
