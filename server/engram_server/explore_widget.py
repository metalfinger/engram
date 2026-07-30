"""Explore widget — an MCP App (SEP-1865) card for Engram's public social surface:
discover people, browse their PUBLIC work, follow them, and ask a question about a
specific piece of their work. The v2 "social brains" direction (``projects/engram/
ideas/2026-07-social-brains.md`` in the brain) makes cross-brain discovery a first-class
loop; this widget is the human-facing front end for that — a place Hiren can find
people, skim what they've shared, and start a conversation anchored to a concept.

Server side mirrors ``social_widget.py``'s division of labor exactly: this module owns
the ``ui://`` resource, its ``_meta`` shapes, and the widget HTML; the tool FUNCTIONS
(the model-visible launcher ``kb_explore_card`` and the app-only data plane) are wired
in app.py, where the store/social backend lives — this module takes no store dependency
at all, it only registers the resource.

Data contract the widget's JS calls over the bridge (all provided elsewhere, coded
against exactly these shapes):
  explore_state()                -> {me, people:[{handle, display_name, avatar_url, bio,
                                      followers, is_following, public_projects}],
                                      feed:[{handle, display_name, avatar_url, path,
                                      title, description, project, updated}]}
  explore_profile(handle)        -> {profile:{handle, display_name, avatar_url, bio,
                                      followers, following, is_following},
                                      public_work:[{path, title, description, type,
                                      project, updated}]}
  explore_concept(handle, path)  -> {handle, path, title, type, project, text}
  explore_follow(handle, follow) -> {handle, is_following}
  explore_ask(handle, path, question) -> {ok, ask_id} | {error}
``explore_state``/``explore_profile``/``explore_concept``/``explore_follow``/
``explore_ask`` are app-only (``visibility: ["app"]`` — invisible to the model, zero
context cost, callable only from the widget's own bridge). The model-visible launcher
is ``kb_explore_card`` (mounts this widget); its tool result seeds the initial render,
same as ``kb_inbox_card``/``social_state`` in the social widget — the launcher itself
returns only a COMPACT summary, never the full render shape, so the widget's own data
plane (``explore_state``) is what actually feeds the UI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

EXPLORE_URI = "ui://engram/explore"
EXPLORE_MIME = "text/html;profile=mcp-app"


def explore_resource_meta() -> dict:
    """``_meta`` for the ui:// resource — same deny-by-default CSP as the other
    widgets: this widget reaches the server ONLY over the bridge, never a direct fetch."""
    return {
        "ui": {
            "csp": {"connectDomains": [], "resourceDomains": []},
            "prefersBorder": True,
        },
        "openai/widgetPrefersBorder": True,
    }


def explore_tool_meta(enabled: bool) -> dict | None:
    """``_meta`` for ``kb_explore_card`` — mounts the widget; model AND app visible
    (mirrors ``social_tool_meta``) so it stays a useful plain-JSON tool in chat
    when no widget host is present."""
    return {"ui": {"resourceUri": EXPLORE_URI, "visibility": ["model", "app"]}} if enabled else None


def explore_app_tool_meta(enabled: bool) -> dict | None:
    """``_meta`` for the app-only data-plane tools (``explore_state``,
    ``explore_profile``, ``explore_concept``, ``explore_follow``, ``explore_ask``):
    no resourceUri (they mount nothing — the widget is already up), ``visibility:
    ["app"]`` hides them from the model entirely (mirrors ``social_app_tool_meta``)."""
    return {"ui": {"visibility": ["app"]}} if enabled else None


def register_explore_widget(mcp: "FastMCP", enabled: bool) -> None:
    """Register the ui:// resource when the widget flag is on; a no-op when off —
    zero behavior change with the flag off, matching ``register_social_widget``."""
    if not enabled:
        return

    @mcp.resource(EXPLORE_URI, mime_type=EXPLORE_MIME, meta=explore_resource_meta())
    def explore_resource() -> str:
        return EXPLORE_HTML


# ---------------------------------------------------------------------------
# The widget. One self-contained HTML document, zero external requests, no build
# step. Raw string (NOT an f-string) — the CSS/JS braces are literal.
# ---------------------------------------------------------------------------

EXPLORE_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Explore</title>
<style>
:root {
  --bg:#faf7f2; --surface:#ffffff; --surface-2:#f4efe6; --inset:#f7f2ea;
  --fg:#221f1a; --muted:#7a7266; --faint:#a89e8e;
  --line:#eae1d4; --line-2:#e0d6c4;
  --accent:#b5622b; --accent-ink:#9c4f1f; --accent-fg:#ffffff;
  --accent-soft:#f1e3d3; --accent-line:#e3c9ad;
  --red:#b23a26; --green:#3f7d43; --shadow:0 1px 2px rgba(60,40,20,.05);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#16130d; --surface:#1e1a13; --surface-2:#241f16; --inset:#211c14;
    --fg:#ece6da; --muted:#a09784; --faint:#6d6453;
    --line:#2c261b; --line-2:#39321f;
    --accent:#d98a4e; --accent-ink:#e29a63; --accent-fg:#1a1409;
    --accent-soft:rgba(217,138,78,.13); --accent-line:#4a3820;
    --red:#e08a72; --green:#7fbb80; --shadow:none;
  }
}
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; }
body {
  margin:0; background:var(--bg); color:var(--fg);
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
  font-size:14px; line-height:1.5; letter-spacing:-0.003em; -webkit-font-smoothing:antialiased;
}
button { font:inherit; }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px; border-radius:3px; }
.wrap { padding:10px 12px 12px; }
@keyframes fade { from { opacity:0; transform:translateY(3px); } to { opacity:1; transform:none; } }
#view { animation:fade .15s ease; }
@media (prefers-reduced-motion: reduce) { #view { animation:none; } * { transition:none !important; } }

.eyebrow { font-size:.66rem; font-weight:700; letter-spacing:.13em; text-transform:uppercase; color:var(--accent-ink); margin:0 0 .3rem; }
h1 { font-size:1.2rem; line-height:1.15; letter-spacing:-0.02em; margin:0 0 .2rem; font-weight:700; }
.headrow { display:flex; align-items:center; gap:.5rem; }
.headrow h1 { flex:1 1 auto; min-width:0; margin:0; }

/* avatars */
.avatar { border-radius:50%; object-fit:cover; flex:0 0 auto; display:inline-block; background:var(--surface-2); }
.avatar-sm { width:2rem; height:2rem; }
.avatar-md { width:2.6rem; height:2.6rem; }
.avatar-fallback { display:grid; place-items:center; font-weight:700; color:var(--accent-ink); background:var(--accent-soft);
                    border:1px solid var(--accent-line); }
.avatar-sm.avatar-fallback { font-size:.85rem; }
.avatar-md.avatar-fallback { font-size:1.05rem; }

/* feed strip */
.section-label { font-size:.66rem; font-weight:700; letter-spacing:.08em; text-transform:uppercase; color:var(--faint);
                  margin:.9rem 0 .35rem; }
.feed-list { display:flex; flex-direction:column; gap:.35rem; margin:.3rem 0; }
.feed-item { display:block; width:100%; text-align:left; cursor:pointer; padding:.5rem .65rem; background:var(--surface);
             color:var(--fg); border:1px solid var(--line); border-radius:11px; }
.feed-title { font-size:.85rem; font-weight:650; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.feed-meta { font-size:.72rem; color:var(--muted); margin-top:.1rem; }

/* people list */
.people-list { display:flex; flex-direction:column; gap:.5rem; margin:.4rem 0; }
.person-row { display:flex; align-items:center; gap:.6rem; padding:.6rem .7rem; background:var(--surface);
              border:1px solid var(--line); border-radius:13px; box-shadow:var(--shadow); cursor:pointer; }
.person-row:active { transform:scale(.99); }
.person-main { min-width:0; flex:1 1 auto; display:flex; flex-direction:column; gap:.05rem; }
.person-name { font-size:.9rem; font-weight:650; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.person-handle { font-size:.72rem; color:var(--faint); }
.person-bio { font-size:.78rem; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin-top:.1rem; }
.person-meta { font-size:.68rem; color:var(--faint); margin-top:.1rem; }
.follow-btn { flex:0 0 auto; background:var(--accent); color:var(--accent-fg); border:1px solid var(--accent); border-radius:8px;
              font-weight:600; font-size:.76rem; padding:.32rem .7rem; cursor:pointer; }
.follow-btn.following { background:var(--surface); color:var(--muted); border-color:var(--line-2); }
.follow-btn:hover { filter:brightness(1.06); }
.follow-btn:disabled { opacity:.6; cursor:default; }

.empty { color:var(--faint); font-size:.85rem; font-style:italic; padding:1.2rem .2rem; text-align:center; }
.errbox { display:flex; align-items:center; gap:.7rem; color:var(--red); font-size:.85rem; padding:1rem 0; }
.retry { background:var(--accent); color:var(--accent-fg); border:0; border-radius:7px; font-weight:600; font-size:.78rem; padding:.35rem .8rem; cursor:pointer; }

/* skeletons */
.skel { position:relative; overflow:hidden; background:var(--surface-2); border-radius:12px; height:4rem; margin-bottom:.5rem; }
.skel::after { content:""; position:absolute; inset:0; transform:translateX(-100%);
               background:linear-gradient(90deg,transparent,color-mix(in srgb,var(--surface) 60%,transparent),transparent); animation:shimmer 1.2s infinite; }
@keyframes shimmer { 100% { transform:translateX(100%); } }

/* profile view */
.phead { display:flex; align-items:flex-start; gap:.6rem; margin:.1rem 0 .6rem; }
.back { border:1px solid var(--line-2); background:var(--surface); color:var(--muted);
        font-size:.8rem; font-weight:600; padding:.35rem .6rem; border-radius:8px; cursor:pointer; flex:0 0 auto; }
.back:hover { border-color:var(--accent-line); color:var(--accent-ink); }
.phead-main { min-width:0; flex:1 1 auto; display:flex; align-items:flex-start; gap:.6rem; }
.phead-text { min-width:0; flex:1 1 auto; }
.phead h1 { font-size:1.05rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.psub { font-size:.75rem; color:var(--faint); }
.pbio { font-size:.82rem; color:var(--muted); margin:.35rem 0 0; }
.pfollow-row { display:flex; align-items:center; gap:.6rem; margin-top:.5rem; }
.pfollowers { font-size:.76rem; color:var(--faint); }

.work-list { display:flex; flex-direction:column; gap:.4rem; margin:.3rem 0; }
.work-item { display:block; width:100%; text-align:left; cursor:pointer; padding:.55rem .65rem; background:var(--surface);
             color:var(--fg); border:1px solid var(--line); border-radius:11px; }
.work-title { font-size:.86rem; font-weight:650; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.work-desc { font-size:.78rem; color:var(--muted); margin-top:.15rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.chips { display:flex; gap:.35rem; margin-top:.3rem; flex-wrap:wrap; }
.chip { font-size:.66rem; font-weight:600; color:var(--accent-ink); background:var(--accent-soft); border:1px solid var(--accent-line);
        border-radius:999px; padding:.1rem .55rem; }

/* concept view */
.chead { margin:.1rem 0 .5rem; }
.ctext { font-size:.88rem; color:var(--fg); margin-top:.7rem; }
.ctext p { margin:0 0 .7rem; white-space:pre-wrap; word-break:break-word; }
.ctext p:last-child { margin-bottom:0; }

.composer { display:flex; flex-direction:column; gap:.4rem; margin-top:1rem; padding-top:.7rem; border-top:1px solid var(--line); }
.cq { font-size:.78rem; font-weight:650; color:var(--muted); }
.tinput { width:100%; padding:.6rem .75rem; font-size:.88rem; color:var(--fg); background:var(--surface);
          border:1px solid var(--line-2); border-radius:11px; outline:none; resize:vertical; min-height:3.4rem; font-family:inherit; }
.tinput:focus { border-color:var(--accent-line); box-shadow:0 0 0 3px var(--accent-soft); }
.tinput:disabled { opacity:.55; }
.askrow { display:flex; align-items:center; gap:.6rem; }
.askbtn { flex:0 0 auto; background:var(--accent); color:var(--accent-fg); border:1px solid var(--accent); border-radius:9px;
          font-weight:600; font-size:.82rem; padding:.4rem .9rem; cursor:pointer; }
.askbtn:hover { filter:brightness(1.06); }
.askbtn:disabled { opacity:.55; cursor:default; }
.askmsg { font-size:.78rem; margin:0; }
.askmsg.ok { color:var(--green); }
.askmsg.err { color:var(--red); }
</style></head>
<body>
<div class="wrap"><div id="view"><div class="skel"></div></div></div>
<script>
"use strict";
const $=(id)=>document.getElementById(id);
const view=$("view");

let state={ view:"people", data:null, dataStatus:null,
            profileHandle:null, profileData:null, profileStatus:null,
            conceptHandle:null, conceptPath:null, conceptData:null, conceptStatus:null,
            returnTo:{view:"people"} };
let booted=false;

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
  // Host recycling this iframe — nothing to flush, just ack so it can proceed
  // and stop all polling so a torn-down widget never keeps ticking server-side.
  if(m.method==="ui/resource-teardown"){
    stopAllPolling();
    if(m.id!==undefined) window.parent.postMessage({jsonrpc:"2.0",id:m.id,result:{}},"*");
    return;
  }
},{passive:true});
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

// ─────────────────────────── helpers ──────────────────────────
// esc() is the ONLY thing allowed to put user data (display names, handles, bios,
// titles, descriptions, project names, avatar URLs) into the DOM via innerHTML —
// every string below funnels through it. Concept body TEXT is different: it's a
// full markdown document, so it's rendered via createElement/textContent instead
// (see renderConceptText), never innerHTML, no matter how it's escaped.
function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function relTime(iso){ if(!iso) return ""; const t=Date.parse(iso); if(isNaN(t)) return "";
  const secs=(Date.now()-t)/1000; if(secs<45) return "just now"; const m=Math.floor(secs/60);
  if(m<60) return m+"m ago"; const h=Math.floor(m/60); if(h<24) return h+"h ago"; return Math.floor(h/24)+"d ago"; }
function avatarHtml(displayName, handle, avatarUrl, size){
  const label=String(displayName||handle||"?").trim();
  const letter=esc((label.charAt(0)||"?").toUpperCase());
  if(avatarUrl){
    return '<img class="avatar avatar-'+size+'" src="'+esc(avatarUrl)+'" alt="" referrerpolicy="no-referrer" '
      +'onerror="this.outerHTML=\'<span class=&quot;avatar avatar-'+size+' avatar-fallback&quot;>'+letter+'</span>\'">';
  }
  return '<span class="avatar avatar-'+size+' avatar-fallback">'+letter+'</span>';
}
function findPerson(handle){
  const people=(state.data&&state.data.people)||[];
  return people.find(p=>p.handle===handle)||null;
}

// ─────────────────────────── PEOPLE view ──────────────────────────
async function loadState(showSkeleton){
  // The widget's own data plane is the app-only explore_state() (full shape). The
  // model-visible kb_explore_card launcher returns only a COMPACT summary (to keep
  // the model's context cheap), so never fetch render data from it.
  if(showSkeleton){ state.dataStatus="loading"; renderPeople(); }
  try{
    const d=await callTool("explore_state",{});
    if(d && d.error) throw new Error(d.error);
    state.data=(d && typeof d==="object")?d:{}; state.dataStatus="ok";
  }catch(e){ if(!state.data) state.dataStatus="error"; }
  if(state.view==="people") renderPeople();
}
async function pollState(){
  if(document.hidden || state.view!=="people") return;
  try{
    const d=await callTool("explore_state",{});
    if(d && !d.error){ state.data=d; state.dataStatus="ok"; if(state.view==="people") renderPeople(); }
  }catch(e){ /* keep showing the last good state; a single missed poll is invisible */ }
}
function feedRow(f){
  return '<button class="feed-item" data-feed="'+esc(f.handle)+'|'+esc(f.path)+'">'
    +'<div class="feed-title">'+esc(f.title||f.path)+'</div>'
    +'<div class="feed-meta">@'+esc(f.handle)+(f.project?' · '+esc(f.project):'')+(f.updated?' · '+esc(relTime(f.updated)):'')+'</div>'
  +'</button>';
}
function personRow(p){
  const following=!!p.is_following;
  const n=Number(p.public_projects||0);
  return '<div class="person-row" data-person="'+esc(p.handle)+'">'
    +avatarHtml(p.display_name,p.handle,p.avatar_url,"sm")
    +'<span class="person-main"><span class="person-name">'+esc(p.display_name||p.handle)+'</span>'
      +'<span class="person-handle">@'+esc(p.handle)+'</span>'
      +(p.bio?'<span class="person-bio">'+esc(p.bio)+'</span>':'')
      +'<span class="person-meta">'+n+' public project'+(n===1?'':'s')+'</span></span>'
    +'<button class="follow-btn'+(following?' following':'')+'" data-follow="'+esc(p.handle)+'">'+(following?'Following':'Follow')+'</button>'
  +'</div>';
}
function renderPeople(){
  state.view="people"; startPolling();
  let body;
  if(state.dataStatus==="loading"){ body='<div class="skel"></div><div class="skel"></div>'; }
  else if(state.dataStatus==="error"){ body='<div class="errbox"><span>Couldn\'t load Explore.</span><button class="retry" id="retry-people">Retry</button></div>'; }
  else {
    const d=state.data||{};
    const feed=Array.isArray(d.feed)?d.feed:[];
    const people=Array.isArray(d.people)?d.people:[];
    let sec="";
    if(feed.length){
      sec+='<div class="section-label">From people you follow</div><div class="feed-list">'+feed.map(feedRow).join("")+'</div>';
    }
    sec+='<div class="section-label">Discover</div>';
    sec+=people.length ? '<div class="people-list">'+people.map(personRow).join("")+'</div>'
      : '<div class="empty">No one to discover yet.</div>';
    body=sec;
  }
  view.innerHTML='<p class="eyebrow">Explore</p><div class="headrow"><h1>Discover</h1></div>'+body;
  const rt=$("retry-people"); if(rt) rt.onclick=()=>loadState(true);
  scheduleHeight();
}
async function toggleFollow(handle){
  const btn=document.querySelector('[data-follow="'+CSS.escape(handle)+'"]');
  const wasFollowing=btn?btn.classList.contains("following"):false;
  if(btn){ btn.disabled=true; }
  try{
    const r=await callTool("explore_follow",{handle, follow:!wasFollowing});
    if(r && r.error) throw new Error(r.error);
    const isFollowing=!!(r && r.is_following);
    const p=findPerson(handle); if(p) p.is_following=isFollowing;
    if(state.profileData && state.profileData.profile && state.profileData.profile.handle===handle){
      state.profileData.profile.is_following=isFollowing;
      if(typeof state.profileData.profile.followers==="number"){
        state.profileData.profile.followers=Math.max(0, state.profileData.profile.followers+(isFollowing&&!wasFollowing?1:(!isFollowing&&wasFollowing?-1:0)));
      }
    }
    if(state.view==="people") renderPeople(); else if(state.view==="profile") renderProfile(false);
  }catch(e){ if(btn) btn.disabled=false; }
}

// ─────────────────────────── PROFILE view ──────────────────────────
async function openProfile(handle, returnTo){
  state.view="profile"; state.profileHandle=handle; state.profileData=null; state.profileStatus="loading";
  state.returnTo=returnTo||{view:"people"};
  renderProfile(true);
  try{
    const d=await callTool("explore_profile",{handle});
    if(state.view!=="profile" || state.profileHandle!==handle) return;
    if(d && d.error) throw new Error(d.error);
    state.profileData=d; state.profileStatus="ok";
  }catch(e){ state.profileStatus="error"; }
  if(state.view==="profile" && state.profileHandle===handle) renderProfile(false);
}
function workRow(w, handle){
  return '<button class="work-item" data-work="'+esc(handle)+'|'+esc(w.path)+'">'
    +'<div class="work-title">'+esc(w.title||w.path)+'</div>'
    +(w.description?'<div class="work-desc">'+esc(w.description)+'</div>':'')
    +'<div class="chips">'+(w.type?'<span class="chip">'+esc(w.type)+'</span>':'')+(w.project?'<span class="chip">'+esc(w.project)+'</span>':'')+'</div>'
  +'</button>';
}
function renderProfile(skeleton){
  state.view="profile"; stopAllPolling();
  const backLabel="‹ Explore";
  if(skeleton || state.profileStatus==="loading"){
    view.innerHTML='<div class="phead"><button class="back" id="profile-back">'+backLabel+'</button></div><div class="skel"></div><div class="skel"></div>';
    const b=$("profile-back"); if(b) b.onclick=backFromProfile;
    scheduleHeight(); return;
  }
  if(state.profileStatus==="error"){
    view.innerHTML='<div class="phead"><button class="back" id="profile-back">'+backLabel+'</button></div>'
      +'<div class="errbox"><span>Couldn\'t load this profile.</span><button class="retry" id="retry-profile">Retry</button></div>';
    $("profile-back").onclick=backFromProfile;
    $("retry-profile").onclick=()=>openProfile(state.profileHandle, state.returnTo);
    scheduleHeight(); return;
  }
  const d=state.profileData||{};
  const p=d.profile||{handle:state.profileHandle};
  const work=Array.isArray(d.public_work)?d.public_work:[];
  const following=!!p.is_following;
  const followers=Number(p.followers||0);
  view.innerHTML='<div class="phead"><button class="back" id="profile-back">'+backLabel+'</button></div>'
    +'<div class="phead-main">'+avatarHtml(p.display_name,p.handle,p.avatar_url,"md")
    +'<div class="phead-text"><h1>'+esc(p.display_name||p.handle)+'</h1><div class="psub">@'+esc(p.handle)+'</div>'
    +(p.bio?'<div class="pbio">'+esc(p.bio)+'</div>':'')
    +'<div class="pfollow-row"><button class="follow-btn'+(following?' following':'')+'" data-follow="'+esc(p.handle)+'">'+(following?'Following':'Follow')+'</button>'
    +'<span class="pfollowers">'+followers+' follower'+(followers===1?'':'s')+'</span></div></div></div>'
    +'<div class="section-label">Public work</div>'
    +(work.length?'<div class="work-list">'+work.map(w=>workRow(w,p.handle)).join("")+'</div>':'<div class="empty">Nothing public yet.</div>');
  $("profile-back").onclick=backFromProfile;
  scheduleHeight();
}
function backFromProfile(){ renderPeople(); }

// ─────────────────────────── CONCEPT view ──────────────────────────
async function openConcept(handle, path, returnTo){
  state.view="concept"; state.conceptHandle=handle; state.conceptPath=path; state.conceptData=null; state.conceptStatus="loading";
  state.askStatus=null;
  state.returnTo=returnTo||state.returnTo||{view:"people"};
  renderConcept(true);
  try{
    const d=await callTool("explore_concept",{handle, path});
    if(state.view!=="concept" || state.conceptHandle!==handle || state.conceptPath!==path) return;
    if(d && d.error) throw new Error(d.error);
    state.conceptData=d; state.conceptStatus="ok";
  }catch(e){ state.conceptStatus="error"; }
  if(state.view==="concept" && state.conceptHandle===handle && state.conceptPath===path) renderConcept(false);
}
function renderConceptText(text){
  // Concept text is a full markdown body from someone else's brain — attacker-
  // influenced. Render it with createElement/textContent, never innerHTML, no
  // matter how "safe" the string looks after esc().
  const container=document.createElement("div"); container.className="ctext";
  const paras=String(text||"").split(/\n{2,}/);
  for(const para of paras){
    if(!para.trim()) continue;
    const p=document.createElement("p"); p.textContent=para;
    container.appendChild(p);
  }
  if(!container.children.length){ const p=document.createElement("p"); p.textContent="(empty)"; container.appendChild(p); }
  return container;
}
function backFromConcept(){
  const to=state.returnTo||{view:"people"};
  if(to.view==="profile" && to.handle){ openProfile(to.handle, {view:"people"}); }
  else { renderPeople(); }
}
function renderConcept(skeleton){
  state.view="concept"; stopAllPolling();
  const backLabel=(state.returnTo&&state.returnTo.view==="profile")?"‹ Profile":"‹ Explore";
  if(skeleton || state.conceptStatus==="loading"){
    view.innerHTML='<div class="chead"><button class="back" id="concept-back">'+backLabel+'</button></div><div class="skel"></div><div class="skel"></div>';
    $("concept-back").onclick=backFromConcept;
    scheduleHeight(); return;
  }
  if(state.conceptStatus==="error"){
    view.innerHTML='<div class="chead"><button class="back" id="concept-back">'+backLabel+'</button></div>'
      +'<div class="errbox"><span>Couldn\'t load this.</span><button class="retry" id="retry-concept">Retry</button></div>';
    $("concept-back").onclick=backFromConcept;
    $("retry-concept").onclick=()=>openConcept(state.conceptHandle, state.conceptPath, state.returnTo);
    scheduleHeight(); return;
  }
  const d=state.conceptData||{};
  view.innerHTML='<div class="chead"><button class="back" id="concept-back">'+backLabel+'</button></div>'
    +'<h1>'+esc(d.title||d.path||"")+'</h1>'
    +'<div class="chips">'+(d.type?'<span class="chip">'+esc(d.type)+'</span>':'')+(d.project?'<span class="chip">'+esc(d.project)+'</span>':'')+'</div>'
    +'<div id="ctext-holder"></div>'
    +'<div class="composer"><div class="cq">Ask @'+esc(d.handle||state.conceptHandle||"")+' about this</div>'
    +'<textarea id="askinput" class="tinput" rows="2" placeholder="Ask a question…" aria-label="Question"></textarea>'
    +'<div class="askrow"><button class="askbtn" id="askbtn">Ask</button><p class="askmsg" id="askmsg" hidden></p></div></div>';
  $("concept-back").onclick=backFromConcept;
  const holder=$("ctext-holder"); if(holder) holder.appendChild(renderConceptText(d.text));
  $("askbtn").onclick=sendAsk;
  scheduleHeight();
}
async function sendAsk(){
  const inp=$("askinput"), btn=$("askbtn"), msg=$("askmsg");
  if(!inp) return;
  const question=inp.value.trim();
  if(!question) return;
  btn.disabled=true; inp.disabled=true; if(msg){ msg.hidden=true; msg.className="askmsg"; }
  try{
    const r=await callTool("explore_ask",{handle:state.conceptHandle, path:state.conceptPath, question});
    if(r && r.error) throw new Error(r.error);
    inp.value="";
    if(msg){ msg.hidden=false; msg.className="askmsg ok"; msg.textContent="Question sent to @"+state.conceptHandle+"."; }
  }catch(e){
    if(msg){ msg.hidden=false; msg.className="askmsg err"; msg.textContent="Couldn't send — try again."; }
  }
  btn.disabled=false; inp.disabled=false;
  scheduleHeight();
}

// ─────────────────────────── polling control ──────────────────────────
let stateTimer=0;
function stopAllPolling(){ if(stateTimer){ clearInterval(stateTimer); stateTimer=0; } }
function startPolling(){
  stopAllPolling();
  if(document.hidden || state.view!=="people") return;
  stateTimer=setInterval(pollState,8000);
}
document.addEventListener("visibilitychange",()=>{ if(document.hidden) stopAllPolling(); else startPolling(); });

// ─────────────────── seeding & reconcile ─────────────────
function seedFrom(data){
  if(!data) return;
  booted=true;
  if(Array.isArray(data.people) || Array.isArray(data.feed) || data.me){
    state.data=data; state.dataStatus="ok";
    if(state.view==="people") renderPeople();
    return;
  }
  loadState(true);
}

// ─────────────────── delegated clicks ─────────────────
document.addEventListener("click",(e)=>{
  const fw=e.target.closest("[data-follow]"); if(fw){ toggleFollow(fw.getAttribute("data-follow")); return; }
  const pr=e.target.closest("[data-person]"); if(pr){ openProfile(pr.getAttribute("data-person"), {view:"people"}); return; }
  const fd=e.target.closest("[data-feed]"); if(fd){
    const parts=fd.getAttribute("data-feed").split("|"); openConcept(parts[0], parts.slice(1).join("|"), {view:"people"}); return;
  }
  const wk=e.target.closest("[data-work]"); if(wk){
    const parts=wk.getAttribute("data-work").split("|");
    openConcept(parts[0], parts.slice(1).join("|"), {view:"profile", handle:state.profileHandle}); return;
  }
});

// ─────────────────────────── boot ──────────────────────────
(async function init(){
  try{ if(window.openai && window.openai.toolOutput){ seedFrom(normalize(window.openai.toolOutput)); } }catch(e){}
  try{ await rpcReq("ui/initialize",{appCapabilities:{availableDisplayModes:["inline","fullscreen"]},appInfo:{name:"engram-explore",version:"1.0.0"},protocolVersion:"2026-01-26"});
       rpcNote("ui/notifications/initialized",{}); }catch(e){}
  if(!booted) await loadState(true);
  reportHeight();
})();
</script></body></html>
"""
