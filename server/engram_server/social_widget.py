"""Social widget — an MCP App (SEP-1865) card for Engram's DMs, contacts, and
notifications. The v2 "social brains" direction (``projects/engram/ideas/2026-07-social-
brains.md`` in the brain) puts agent-to-agent and cross-brain messaging on the roadmap;
this widget is the human-facing front end for that inbox — a place Hiren can see who's
reaching out, accept a contact request, and reply, without leaving chat.

Server side mirrors ``meetings_widget.py``'s division of labor exactly: this module owns
the ``ui://`` resource, its ``_meta`` shapes, and the widget HTML; the tool FUNCTIONS
(the model-visible launcher ``kb_inbox_card`` and the app-only data plane) are wired in
app.py, where the store/social backend lives — this module takes no store dependency at
all, it only registers the resource.

Data contract the widget's JS calls over the bridge (all provided elsewhere, coded
against exactly these shapes):
  social_state()                     -> {me, contacts, incoming, outgoing,
                                          conversations, notifications, counts}
  social_conversation(with_handle)   -> {with, messages:[{from, body, at, mine}]}
                                         (marks that conversation read server-side)
  social_send(to, message)           -> {sent, to, at}
  social_accept(handle)              -> {status, handle}
  social_mark_read()                 -> {ok}
``social_state``/``social_conversation``/``social_send``/``social_accept``/
``social_mark_read`` are app-only (``visibility: ["app"]`` — invisible to the model,
zero context cost, callable only from the widget's own bridge). The model-visible
launcher is ``kb_inbox_card`` (mounts this widget); its tool result seeds the initial
render, same as ``kb_meetings``/``meetings_state`` in the meetings widget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

SOCIAL_URI = "ui://engram/messages"
SOCIAL_MIME = "text/html;profile=mcp-app"


def social_resource_meta() -> dict:
    """``_meta`` for the ui:// resource — same deny-by-default CSP as the other
    widgets: this widget reaches the server ONLY over the bridge, never a direct fetch."""
    return {
        "ui": {
            "csp": {"connectDomains": [], "resourceDomains": []},
            "prefersBorder": True,
        },
        "openai/widgetPrefersBorder": True,
    }


def social_tool_meta(enabled: bool) -> dict | None:
    """``_meta`` for ``kb_inbox_card`` — mounts the widget; model AND app visible
    (mirrors ``meetings_tool_meta``) so it stays a useful plain-JSON tool in chat
    when no widget host is present."""
    return {"ui": {"resourceUri": SOCIAL_URI, "visibility": ["model", "app"]}} if enabled else None


def social_app_tool_meta(enabled: bool) -> dict | None:
    """``_meta`` for the app-only data-plane tools (``social_state``,
    ``social_conversation``, ``social_send``, ``social_accept``, ``social_mark_read``):
    no resourceUri (they mount nothing — the widget is already up), ``visibility:
    ["app"]`` hides them from the model entirely (mirrors ``meetings_app_tool_meta``)."""
    return {"ui": {"visibility": ["app"]}} if enabled else None


def register_social_widget(mcp: "FastMCP", enabled: bool) -> None:
    """Register the ui:// resource when the widget flag is on; a no-op when off —
    zero behavior change with the flag off, matching ``register_meetings_widget``."""
    if not enabled:
        return

    @mcp.resource(SOCIAL_URI, mime_type=SOCIAL_MIME, meta=social_resource_meta())
    def social_resource() -> str:
        return SOCIAL_HTML


# ---------------------------------------------------------------------------
# The widget. One self-contained HTML document, zero external requests, no build
# step. Raw string (NOT an f-string) — the CSS/JS braces are literal.
# ---------------------------------------------------------------------------

SOCIAL_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Messages</title>
<style>
:root {
  --bg:#faf7f2; --surface:#ffffff; --surface-2:#f4efe6; --inset:#f7f2ea;
  --fg:#221f1a; --muted:#7a7266; --faint:#a89e8e;
  --line:#eae1d4; --line-2:#e0d6c4;
  --accent:#b5622b; --accent-ink:#9c4f1f; --accent-fg:#ffffff;
  --accent-soft:#f1e3d3; --accent-line:#e3c9ad;
  --amber-bg:#ffe9c2; --amber-bd:#e6b96a; --amber-tx:#7a4a12;
  --red:#b23a26; --shadow:0 1px 2px rgba(60,40,20,.05);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#16130d; --surface:#1e1a13; --surface-2:#241f16; --inset:#211c14;
    --fg:#ece6da; --muted:#a09784; --faint:#6d6453;
    --line:#2c261b; --line-2:#39321f;
    --accent:#d98a4e; --accent-ink:#e29a63; --accent-fg:#1a1409;
    --accent-soft:rgba(217,138,78,.13); --accent-line:#4a3820;
    --amber-bg:#3a2a14; --amber-bd:#6b4d22; --amber-tx:#e2ae6e;
    --red:#e08a72; --shadow:none;
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
h1 { font-size:1.2rem; line-height:1.15; letter-spacing:-0.02em; margin:0 0 .2rem; font-weight:700; display:flex; align-items:center; gap:.4rem; }
.badge { font-size:.68rem; font-weight:700; color:var(--accent-fg); background:var(--accent); border-radius:999px; padding:.05rem .5rem; }
.headrow { display:flex; align-items:center; gap:.5rem; }
.headrow h1 { flex:1 1 auto; min-width:0; }
.markread { border:1px solid var(--line-2); background:var(--surface); color:var(--muted); font-size:.72rem; font-weight:600;
            padding:.28rem .55rem; border-radius:8px; cursor:pointer; flex:0 0 auto; }
.markread:hover { border-color:var(--accent-line); color:var(--accent-ink); }

/* avatars */
.avatar { border-radius:50%; object-fit:cover; flex:0 0 auto; display:inline-block; background:var(--surface-2); }
.avatar-sm { width:2rem; height:2rem; }
.avatar-md { width:2.4rem; height:2.4rem; }
.avatar-fallback { display:grid; place-items:center; font-weight:700; color:var(--accent-ink); background:var(--accent-soft);
                    border:1px solid var(--accent-line); }
.avatar-sm.avatar-fallback { font-size:.85rem; }
.avatar-md.avatar-fallback { font-size:1rem; }

/* conversation rail — big tap targets, mobile-first */
.convos { display:flex; flex-direction:column; gap:.45rem; margin:.6rem 0; }
.convo { display:flex; align-items:center; gap:.55rem; padding:.6rem .7rem; text-align:left; cursor:pointer;
         width:100%; background:var(--surface); color:var(--fg); border:1px solid var(--line); border-radius:12px;
         box-shadow:var(--shadow); position:relative; }
.convo:active { transform:scale(.99); }
.convo.unread { border-color:var(--accent-line); background:color-mix(in srgb, var(--accent-soft) 45%, var(--surface)); }
.convo-main { min-width:0; flex:1 1 auto; display:flex; flex-direction:column; gap:.1rem; }
.convo-top { display:flex; align-items:baseline; gap:.4rem; }
.convo-name { font-size:.92rem; font-weight:650; flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.convo-when { font-size:.68rem; color:var(--faint); flex:0 0 auto; font-variant-numeric:tabular-nums; }
.convo-preview { font-size:.8rem; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.convo.unread .convo-preview { color:var(--fg); }
.unread-dot { width:.5rem; height:.5rem; border-radius:50%; background:var(--accent); flex:0 0 auto; margin-left:.3rem; }

.section-label { font-size:.66rem; font-weight:700; letter-spacing:.08em; text-transform:uppercase; color:var(--faint);
                  margin:.9rem 0 .35rem; }
.requests { display:flex; flex-direction:column; gap:.4rem; }
.req-row { display:flex; align-items:center; gap:.5rem; padding:.5rem .65rem; background:var(--surface); border:1px solid var(--line);
           border-radius:11px; }
.req-handle { flex:1 1 auto; min-width:0; font-size:.85rem; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.accept-btn { flex:0 0 auto; background:var(--accent); color:var(--accent-fg); border:0; border-radius:7px; font-weight:600;
              font-size:.76rem; padding:.32rem .7rem; cursor:pointer; }
.accept-btn:hover { filter:brightness(1.06); }
.accept-btn:disabled { opacity:.6; cursor:default; }
.pending-line { font-size:.76rem; color:var(--faint); margin:.5rem 0 0; }

.notifs { display:flex; flex-direction:column; gap:.3rem; }
.notif-row { display:flex; align-items:baseline; gap:.5rem; padding:.4rem .1rem; border-bottom:1px solid var(--line); font-size:.8rem; }
.notif-row:last-child { border-bottom:0; }
.notif-body { flex:1 1 auto; min-width:0; color:var(--fg); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.notif-when { flex:0 0 auto; color:var(--faint); font-size:.68rem; font-variant-numeric:tabular-nums; }

.empty { color:var(--faint); font-size:.85rem; font-style:italic; padding:1.2rem .2rem; text-align:center; }
.errbox { display:flex; align-items:center; gap:.7rem; color:var(--red); font-size:.85rem; padding:1rem 0; }
.retry { background:var(--accent); color:var(--accent-fg); border:0; border-radius:7px; font-weight:600; font-size:.78rem; padding:.35rem .8rem; cursor:pointer; }

/* skeletons */
.skel { position:relative; overflow:hidden; background:var(--surface-2); border-radius:12px; height:4rem; margin-bottom:.5rem; }
.skel::after { content:""; position:absolute; inset:0; transform:translateX(-100%);
               background:linear-gradient(90deg,transparent,color-mix(in srgb,var(--surface) 60%,transparent),transparent); animation:shimmer 1.2s infinite; }
@keyframes shimmer { 100% { transform:translateX(100%); } }

/* chat view */
.chead { display:flex; align-items:flex-start; gap:.6rem; margin:.1rem 0 .5rem; }
.back { border:1px solid var(--line-2); background:var(--surface); color:var(--muted);
        font-size:.8rem; font-weight:600; padding:.35rem .6rem; border-radius:8px; cursor:pointer; flex:0 0 auto; }
.back:hover { border-color:var(--accent-line); color:var(--accent-ink); }
.chead-main { min-width:0; flex:1 1 auto; display:flex; align-items:center; gap:.55rem; }
.chead-text { min-width:0; flex:1 1 auto; }
.chead h1 { font-size:1.05rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin:0; }
.csub { font-size:.75rem; color:var(--muted); }

.log { display:flex; flex-direction:column; gap:.15rem; min-height:6rem; }
.row { display:flex; align-items:flex-end; gap:.4rem; margin:.25rem 0; }
.row.me { justify-content:flex-end; }
.bubble { max-width:76%; padding:.45rem .65rem .4rem; border-radius:13px; font-size:.86rem; line-height:1.4;
          background:var(--surface-2); border:1px solid var(--line); }
.row.me .bubble { background:var(--accent-soft); border-color:var(--accent-line); border-bottom-right-radius:4px; }
.row:not(.me) .bubble { border-bottom-left-radius:4px; }
.bubble .msg { white-space:pre-wrap; word-break:break-word; }
.bubble .tm { font-size:.62rem; color:var(--faint); margin-top:.2rem; text-align:right; font-variant-numeric:tabular-nums; }
.bubble.pending { opacity:.6; }
.bubble.failed { border-color:var(--red); }

/* composer — keyboard-safe, generous tap target */
.composer { position:sticky; bottom:0; display:flex; gap:.5rem; align-items:flex-end; background:var(--bg);
            padding:.5rem 0 .1rem; margin-top:.4rem; border-top:1px solid var(--line); }
.minput { flex:1 1 auto; min-width:0; padding:.6rem .75rem; font-size:.9rem; color:var(--fg); background:var(--surface);
          border:1px solid var(--line-2); border-radius:12px; outline:none; resize:none; max-height:6.5rem; font-family:inherit; }
.minput:focus { border-color:var(--accent-line); box-shadow:0 0 0 3px var(--accent-soft); }
.minput:disabled { opacity:.55; }
.msend { flex:0 0 auto; width:2.6rem; height:2.6rem; border-radius:50%; background:var(--accent); color:var(--accent-fg);
         border:1px solid var(--accent); font-size:1rem; cursor:pointer; display:grid; place-items:center; }
.msend:hover { filter:brightness(1.06); }
.msend:disabled { opacity:.5; cursor:default; }
.merr { color:var(--red); font-size:.76rem; margin:.2rem 0 0; }
</style></head>
<body>
<div class="wrap"><div id="view"><div class="skel"></div></div></div>
<script>
"use strict";
const $=(id)=>document.getElementById(id);
const view=$("view");

let state={ view:"list", data:null, dataStatus:null, openWith:null, conv:null,
            seenKeys:new Set(), seenContent:new Set() };
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
// esc() is the ONLY thing allowed to put user data (display names, handles,
// message bodies, avatar URLs) into the DOM — every string below funnels through
// it before touching innerHTML. Avatars go in as an <img src="..."> attribute
// (browser-sandboxed, never executes), still escaped so a crafted URL can't
// break out of the attribute; nothing user-controlled is ever assigned raw.
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
function partyInfo(handle){
  const convos=(state.data&&state.data.conversations)||[];
  const c=convos.find(x=>x.with===handle);
  if(c) return {display_name:c.display_name, avatar_url:c.avatar_url};
  const contacts=(state.data&&state.data.contacts)||[];
  const k=contacts.find(x=>x.handle===handle);
  if(k) return {display_name:k.display_name, avatar_url:k.avatar_url};
  return {display_name:handle, avatar_url:null};
}

// ─────────────────────────── LIST view ──────────────────────────
async function loadState(showSkeleton){
  // The widget's own data plane is the app-only social_state() (full shape). The
  // model-visible kb_inbox_card launcher returns only a COMPACT summary (to keep the
  // model's context cheap), so never fetch render data from it — mirrors how the
  // office widget polls office_state(), not kb_office.
  if(showSkeleton){ state.dataStatus="loading"; renderList(); }
  try{
    const d=await callTool("social_state",{});
    if(d && d.error) throw new Error(d.error);
    state.data=(d && typeof d==="object")?d:{}; state.dataStatus="ok";
  }catch(e){ if(!state.data) state.dataStatus="error"; }
  if(state.view==="list") renderList();
}
async function pollState(){
  if(document.hidden) return;
  try{
    const d=await callTool("social_state",{});
    if(d && !d.error){ state.data=d; state.dataStatus="ok"; if(state.view==="list") renderList(); }
  }catch(e){ /* keep showing the last good state; a single missed poll is invisible */ }
}
async function markAllRead(){
  const btn=$("markread"); if(btn) btn.disabled=true;
  try{ await callTool("social_mark_read",{}); }catch(e){ /* best-effort */ }
  await pollState();
}
async function acceptRequest(handle){
  const btn=document.querySelector('[data-accept="'+CSS.escape(handle)+'"]');
  if(btn){ btn.disabled=true; btn.textContent="Adding…"; }
  try{
    const r=await callTool("social_accept",{handle});
    if(r && r.error) throw new Error(r.error);
  }catch(e){ if(btn){ btn.disabled=false; btn.textContent="Accept"; } return; }
  await pollState();
}
function convoRow(c){
  const unread=Number(c.unread||0)>0;
  return '<button class="convo'+(unread?" unread":"")+'" data-open="'+esc(c.with)+'">'
    +avatarHtml(c.display_name,c.with,c.avatar_url,"sm")
    +'<span class="convo-main"><span class="convo-top"><span class="convo-name">'+esc(c.display_name||c.with)+'</span>'
      +(c.at?'<span class="convo-when">'+esc(relTime(c.at))+'</span>':'')+'</span>'
      +'<span class="convo-preview">'+esc(c.last||"")+'</span></span>'
    +(unread?'<span class="unread-dot" aria-label="'+esc(String(c.unread))+' unread"></span>':'')
  +'</button>';
}
function requestRow(handle){
  return '<div class="req-row"><span class="req-handle">@'+esc(handle)+'</span>'
    +'<button class="accept-btn" data-accept="'+esc(handle)+'">Accept</button></div>';
}
function notifRow(n){
  return '<div class="notif-row"><span class="notif-body">'+esc(n.body||"")+'</span>'
    +(n.at?'<span class="notif-when">'+esc(relTime(n.at))+'</span>':'')+'</div>';
}
function renderList(){
  state.view="list"; state.openWith=null; startPolling();
  let body;
  if(state.dataStatus==="loading"){ body='<div class="skel"></div><div class="skel"></div>'; }
  else if(state.dataStatus==="error"){ body='<div class="errbox"><span>Couldn\'t load messages.</span><button class="retry" id="retry-list">Retry</button></div>'; }
  else {
    const d=state.data||{};
    const convos=Array.isArray(d.conversations)?d.conversations:[];
    const incoming=Array.isArray(d.incoming)?d.incoming:[];
    const outgoing=Array.isArray(d.outgoing)?d.outgoing:[];
    const notifs=Array.isArray(d.notifications)?d.notifications:[];
    let sec=convos.length ? '<div class="convos">'+convos.map(convoRow).join("")+'</div>'
      : '<div class="empty">No conversations yet.</div>';
    if(incoming.length){
      sec+='<div class="section-label">Requests</div><div class="requests">'+incoming.map(r=>requestRow(r.handle)).join("")+'</div>';
    }
    if(outgoing.length){
      sec+='<div class="pending-line">Waiting on '+outgoing.map(r=>"@"+esc(r.handle)).join(", ")+'</div>';
    }
    if(notifs.length){
      sec+='<div class="section-label">Notifications</div><div class="notifs">'+notifs.map(notifRow).join("")+'</div>';
    }
    body=sec;
  }
  const counts=(state.data&&state.data.counts)||{};
  const hasNotifs=Number(counts.notifications||0)>0;
  view.innerHTML='<p class="eyebrow">Messages</p>'
    +'<div class="headrow"><h1>Inbox'+(counts.dms?' <span class="badge">'+esc(String(counts.dms))+'</span>':'')+'</h1>'
    +(hasNotifs?'<button class="markread" id="markread">Mark all read</button>':'')+'</div>'
    +body;
  const rt=$("retry-list"); if(rt) rt.onclick=()=>loadState(true);
  const mr=$("markread"); if(mr) mr.onclick=markAllRead;
  scheduleHeight();
}

// ─────────────────────────── CHAT view ──────────────────────────
function msgKey(m){ return (m.mine?"me":(m.from||""))+"|"+String(m.body||"")+"|"+(m.at||""); }
function contentKey(m){ return (m.mine?"me":(m.from||""))+"|"+String(m.body||""); }
function bubbleHtml(m, opts){
  opts=opts||{};
  const mine=!!m.mine;
  const info=mine ? ((state.data&&state.data.me)||{}) : partyInfo(state.openWith);
  const avatar=avatarHtml(info.display_name, mine?info.handle:state.openWith, info.avatar_url, "sm");
  return '<div class="row'+(mine?" me":"")+'"'+(opts.pendingKey?' data-pending="'+esc(opts.pendingKey)+'"':'')+'>'
    +(mine?'':avatar)
    +'<div class="bubble'+(opts.cls?" "+opts.cls:"")+'">'
      +'<div class="msg">'+esc(m.body||"")+'</div>'
      +(m.at?'<div class="tm">'+esc(relTime(m.at))+'</div>':'')
    +'</div>'
    +(mine?avatar:'')
  +'</div>';
}
function appendMessages(msgs){
  const log=$("log"); if(!log) return;
  const atBottom=log.scrollHeight-log.scrollTop-log.clientHeight<60;
  let added=false;
  for(const m of (msgs||[])){
    const key=msgKey(m);
    if(state.seenKeys.has(key)) continue;
    // an optimistic "me" bubble we already rendered locally — the confirmed
    // message just arrived; drop the duplicate rather than showing it twice.
    if(m.mine && state.seenContent.has(contentKey(m))){ state.seenKeys.add(key); continue; }
    state.seenKeys.add(key); state.seenContent.add(contentKey(m));
    log.insertAdjacentHTML("beforeend", bubbleHtml(m));
    added=true;
  }
  if(added && atBottom) log.scrollTop=log.scrollHeight;
  return added;
}
async function openConversation(handle){
  state.view="chat"; state.openWith=handle; state.conv=null;
  state.seenKeys=new Set(); state.seenContent=new Set();
  renderChat(true);
  try{ await rpcReq("ui/request-display-mode",{mode:"fullscreen"},4000); }catch(e){ /* best-effort — inline is fine */ }
  await loadConversation(true);
}
function closeConversation(){ state.openWith=null; renderList(); }
async function loadConversation(initial){
  if(state.view!=="chat") return;
  try{
    const d=await callTool("social_conversation",{with_handle:state.openWith});
    if(state.view!=="chat" || state.openWith==null) return;
    if(d && d.error){ if(initial) showChatError("Couldn't load this conversation."); return; }
    state.conv=d;
    if(initial) renderChat(false);
    appendMessages(d && d.messages);
    if(initial && !(d && d.messages && d.messages.length)){
      const log=$("log"); if(log && !log.children.length) log.innerHTML='<div class="empty">Say hello.</div>';
    }
    // this conversation was just read server-side — clear the cached unread
    // dot immediately rather than waiting on the next poll tick.
    const row=(state.data&&state.data.conversations||[]).find(c=>c.with===state.openWith);
    if(row) row.unread=0;
  }catch(e){ if(initial) showChatError("Couldn't load this conversation."); }
}
async function pollConversation(){
  if(state.view!=="chat" || document.hidden) return;
  await loadConversation(false);
}
function showChatError(msg){
  const log=$("log"); if(log) log.innerHTML='<div class="errbox"><span>'+esc(msg)+'</span><button class="retry" id="retry-chat">Retry</button></div>';
  const rt=$("retry-chat"); if(rt) rt.onclick=()=>loadConversation(true);
}
function renderChat(skeleton){
  const info=partyInfo(state.openWith);
  view.innerHTML='<div class="chead"><button class="back" id="chat-back">‹ Messages</button>'
      +'<div class="chead-main">'+avatarHtml(info.display_name,state.openWith,info.avatar_url,"md")
      +'<div class="chead-text"><h1>'+esc(info.display_name||state.openWith)+'</h1>'
      +'<div class="csub">@'+esc(state.openWith)+'</div></div></div></div>'
    +(skeleton?'<div class="skel"></div>':'<div class="log" id="log"></div>')
    +'<div class="composer"><textarea id="minput" class="minput" rows="1" placeholder="Message…" aria-label="Message"></textarea>'
    +'<button class="msend" id="msend" aria-label="Send">➤</button></div>'
    +'<div class="merr" id="merr" hidden></div>';
  $("chat-back").onclick=closeConversation;
  const inp=$("minput"), snd=$("msend");
  const autoGrow=()=>{ inp.style.height="auto"; inp.style.height=Math.min(inp.scrollHeight,104)+"px"; scheduleHeight(); };
  inp.oninput=autoGrow;
  inp.onkeydown=(e)=>{ if(e.key==="Enter" && !e.shiftKey){ e.preventDefault(); sendMessage(); } };
  snd.onclick=sendMessage;
  startPolling();
  scheduleHeight();
}
async function sendMessage(){
  const inp=$("minput"), snd=$("msend"), errEl=$("merr"), log=$("log");
  if(!inp || !state.openWith) return;
  const text=inp.value.trim(); if(!text) return;
  inp.value=""; inp.style.height="auto"; if(errEl) errEl.hidden=true;
  inp.disabled=true; snd.disabled=true;
  const optimistic={body:text, at:new Date().toISOString(), mine:true};
  const tempKey="pending|"+contentKey(optimistic)+"|"+optimistic.at;
  if(log){ log.insertAdjacentHTML("beforeend", bubbleHtml(optimistic,{cls:"pending", pendingKey:tempKey}));
    log.scrollTop=log.scrollHeight; }
  state.seenContent.add(contentKey(optimistic));
  try{
    const r=await callTool("social_send",{to:state.openWith, message:text});
    if(r && r.error) throw new Error(r.error);
    inp.disabled=false; snd.disabled=false; scheduleHeight();
    const row=log && log.querySelector('[data-pending="'+CSS.escape(tempKey)+'"] .bubble');
    if(row) row.classList.remove("pending");
    // pull the confirmed message in on the next poll tick (or immediately)
    loadConversation(false);
  }catch(e){
    // restore the text so Hiren can retry, mark the bubble failed, surface the error
    inp.value=text; inp.disabled=false; snd.disabled=false;
    const row=log && log.querySelector('[data-pending="'+CSS.escape(tempKey)+'"] .bubble');
    if(row) row.classList.add("failed");
    if(errEl){ errEl.hidden=false; errEl.textContent="Couldn't send — try again."; }
    try{ inp.focus(); }catch(err){}
  }
  scheduleHeight();
}

// ─────────────────────────── polling control ──────────────────────────
let stateTimer=0, convTimer=0;
function stopAllPolling(){ if(stateTimer){ clearInterval(stateTimer); stateTimer=0; } if(convTimer){ clearInterval(convTimer); convTimer=0; } }
function startPolling(){
  stopAllPolling();
  if(document.hidden) return;
  stateTimer=setInterval(pollState,5000);
  if(state.view==="chat") convTimer=setInterval(pollConversation,2000);
}
document.addEventListener("visibilitychange",()=>{ if(document.hidden) stopAllPolling(); else startPolling(); });

// ─────────────────── seeding & reconcile ─────────────────
function seedFrom(data){
  if(!data) return;
  booted=true;
  if(Array.isArray(data.conversations) || Array.isArray(data.contacts) || data.counts){
    state.data=data;
    if(state.view!=="chat") renderList();
    return;
  }
  if(data.messages!==undefined && data.with!==undefined){
    // a social_conversation-shaped push (unlikely as the mount seed, but handled)
    state.view="chat"; state.openWith=data.with; state.conv=data;
    state.seenKeys=new Set(); state.seenContent=new Set();
    renderChat(false); appendMessages(data.messages); return;
  }
  loadState(true);
}

// ─────────────────── delegated clicks ─────────────────
document.addEventListener("click",(e)=>{
  const ac=e.target.closest("[data-accept]"); if(ac){ acceptRequest(ac.getAttribute("data-accept")); return; }
  const op=e.target.closest("[data-open]"); if(op){ openConversation(op.getAttribute("data-open")); return; }
});

// ─────────────────────────── boot ──────────────────────────
(async function init(){
  try{ if(window.openai && window.openai.toolOutput){ seedFrom(normalize(window.openai.toolOutput)); } }catch(e){}
  try{ await rpcReq("ui/initialize",{appCapabilities:{availableDisplayModes:["inline","fullscreen"]},appInfo:{name:"engram-social",version:"1.0.0"},protocolVersion:"2026-01-26"});
       rpcNote("ui/notifications/initialized",{}); }catch(e){}
  if(!booted) await loadState(true);
  reportHeight();
})();
</script></body></html>
"""
