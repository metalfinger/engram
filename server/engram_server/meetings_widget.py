"""Meeting widget — an MCP App (SEP-1865) over Engram's cross-session threads.

Hiren works in Claude Code terminals; claude.ai (desktop + phone) is his window
into the workspace. This widget completes the ``@hiren:`` + ``wait_for_reply``
loop from any claude.ai chat — especially mobile — without needing the browser
office: a rooms list of OPEN threads, a live transcript, and a reply box that
posts as him.

Server side mirrors ``navigator.py``'s structure: a ``ui://`` resource + meta
helpers + one self-contained HTML document. The MODEL-visible tool is
``kb_meetings`` (mounts the widget, useful in plain chat too); the data plane is
three APP-ONLY tools (``visibility: ["app"]`` — invisible to the model, zero
context cost, callable only from the widget's own ``tools/call`` bridge):
``meetings_state`` (poll target for the list), ``meeting_transcript`` (poll
target for an open room), ``meeting_reply`` (post as Hiren, reply-only).

The actual tool FUNCTIONS are registered in app.py (where ``store`` lives), the
same division of labor as the Navigator's kb_projects/kb_load/kb_search — this
module owns the resource, the meta shapes, the payload-building logic, and the
widget HTML; it takes ``store`` as a parameter rather than importing the module
singleton, so it stays unit-testable against the same KBStore fixture threads
already use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from engram_server.kbstore import KBStore

MEETINGS_URI = "ui://engram/meetings"
MEETINGS_MIME = "text/html;profile=mcp-app"

# Reply guard, mirroring explorer/routes.py's web thread-post endpoint exactly
# (same char cap, same reply-only rule) so the widget and the browser office
# behave identically for Hiren's replies.
_MAX_REPLY_CHARS = 4000


def meetings_resource_meta() -> dict:
    """``_meta`` for the ui:// resource — same deny-by-default CSP as the Navigator:
    this widget also reaches the server ONLY over the bridge, never a direct fetch."""
    return {
        "ui": {
            "csp": {"connectDomains": [], "resourceDomains": []},
            "prefersBorder": True,
        },
        "openai/widgetPrefersBorder": True,
    }


def meetings_tool_meta(enabled: bool) -> dict | None:
    """``_meta`` for ``kb_meetings`` — mounts the widget; model AND app visible
    (it stays a fully useful plain-JSON tool in chat when no widget host is present)."""
    return {"ui": {"resourceUri": MEETINGS_URI, "visibility": ["model", "app"]}} if enabled else None


def meetings_app_tool_meta(enabled: bool) -> dict | None:
    """``_meta`` for the app-only data-plane tools (``meetings_state``,
    ``meeting_transcript``, ``meeting_reply``): no resourceUri (they mount nothing —
    the widget is already up), ``visibility: ["app"]`` hides them from the model
    entirely. The OAuth allowlist means every caller IS Hiren, so app-only here is
    what keeps the model from ever calling ``meeting_reply`` on his behalf."""
    return {"ui": {"visibility": ["app"]}} if enabled else None


def register_meetings_widget(mcp: "FastMCP", enabled: bool) -> None:
    """Register the ui:// resource when the widget flag is on; a no-op when off."""
    if not enabled:
        return

    @mcp.resource(MEETINGS_URI, mime_type=MEETINGS_MIME, meta=meetings_resource_meta())
    def meetings_resource() -> str:
        return MEETINGS_HTML


# ---------------------------------------------------------------------------
# Payload building — thin over KBStore's existing thread methods, no new store
# state. Kept here (not in KBStore) so this module stays the single owner of
# the meetings-widget data shape, testable with the same store fixture threads
# already use.
# ---------------------------------------------------------------------------


async def meetings_payload(store: "KBStore") -> dict[str, Any]:
    """Compact room list for OPEN threads only: {threads: [{thread, topic, status,
    participants, turn_count, last_activity, last_turn, needs_hiren}]}.

    ``last_turn`` is the most recent turn's {sender, message, timestamp} (or None
    for a thread with no turns, which shouldn't occur — kb_thread_post always
    writes one on creation). ``needs_hiren`` is true when that last turn starts
    with '@hiren' — the poll-cheap building block for the widget's amber banner.
    """
    rows = await store.kb_threads()
    out: list[dict[str, Any]] = []
    for t in rows:
        if t.get("status") != "open":
            continue
        read = await store.kb_thread_read(t["thread"])
        turns = read.get("turns") or []
        last = turns[-1] if turns else None
        last_turn = (
            {
                "sender": last.get("sender"),
                "message": last.get("message"),
                "timestamp": last.get("timestamp"),
            }
            if last
            else None
        )
        needs_hiren = bool(last and str(last.get("message") or "").strip().lower().startswith("@hiren"))
        out.append(
            {
                "thread": t["thread"],
                "topic": t.get("topic", ""),
                "status": t.get("status", "open"),
                "participants": t.get("participants", []),
                "turn_count": t.get("turn_count", 0),
                "last_activity": t.get("last_activity", ""),
                "last_turn": last_turn,
                "needs_hiren": needs_hiren,
            }
        )
    return {"threads": out}


async def meeting_reply(store: "KBStore", thread: str, message: str) -> dict[str, Any]:
    """Post `message` into `thread` as Hiren — reply-only, mirroring the web office's
    guard exactly: the thread must already exist and be open, sender is ALWAYS
    'hiren' regardless of caller (the OAuth allowlist means every caller IS him),
    never creates or reopens a thread. Raises KBError on any guard failure or on
    kb_thread_post's own secret-scan refusal."""
    from engram_server.errors import KBError

    text = str(message or "")
    if not text.strip():
        raise KBError("Empty message — nothing to send.")
    if len(text) > _MAX_REPLY_CHARS:
        raise KBError(f"message too long (max {_MAX_REPLY_CHARS} chars)")
    existing = await store.kb_thread_read(thread)
    status = existing.get("status")
    if status == "none":
        raise KBError(f"unknown thread {thread!r}")
    if status == "closed":
        raise KBError(f"thread {thread!r} is closed")
    return await store.kb_thread_post(thread, "hiren", text)


# ---------------------------------------------------------------------------
# The widget. One self-contained HTML document, zero external requests, no build
# step. Raw string (NOT an f-string) — the CSS/JS braces are literal.
# ---------------------------------------------------------------------------

MEETINGS_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Meetings</title>
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
h1 { font-size:1.2rem; line-height:1.15; letter-spacing:-0.02em; margin:0 0 .2rem; font-weight:700; }

/* room cards — big tap targets, mobile-first */
.rooms { display:flex; flex-direction:column; gap:.55rem; margin:.7rem 0; }
.room { display:flex; flex-direction:column; gap:.4rem; padding:.75rem .85rem; text-align:left; cursor:pointer;
        width:100%; background:var(--surface); color:var(--fg); border:1px solid var(--line); border-radius:12px;
        box-shadow:var(--shadow); }
.room:active { transform:scale(.99); }
.room.needs { border-color:var(--amber-bd); background:color-mix(in srgb, var(--amber-bg) 35%, var(--surface)); }
.room .rtop { display:flex; align-items:center; gap:.5rem; }
.room .rtopic { font-size:.98rem; font-weight:650; flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.needs-badge { display:inline-flex; align-items:center; gap:.3rem; font-size:.68rem; font-weight:700; color:var(--amber-tx);
  background:var(--amber-bg); border:1px solid var(--amber-bd); border-radius:999px; padding:.15rem .5rem; flex:0 0 auto; }
.needs-badge .pulse { width:.4rem; height:.4rem; border-radius:50%; background:var(--amber-tx); animation:pulse 1.6s infinite; }
@keyframes pulse { 0%{opacity:.4} 50%{opacity:1} 100%{opacity:.4} }
.parts { display:flex; flex-wrap:wrap; gap:.3rem; }
.pdot { display:inline-flex; align-items:center; gap:.25rem; font-size:.72rem; color:var(--muted); }
.pdot .d { width:.45rem; height:.45rem; border-radius:50%; background:var(--faint); }
.pdot .d.hiren { background:var(--accent); }
.preview { font-size:.82rem; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.preview b { color:var(--fg); font-weight:600; }
.rwhen { font-size:.7rem; color:var(--faint); font-variant-numeric:tabular-nums; }
.empty { color:var(--faint); font-size:.85rem; font-style:italic; padding:1.4rem .2rem; text-align:center; }
.errbox { display:flex; align-items:center; gap:.7rem; color:var(--red); font-size:.85rem; padding:1rem 0; }
.retry { background:var(--accent); color:var(--accent-fg); border:0; border-radius:7px; font-weight:600; font-size:.78rem; padding:.35rem .8rem; cursor:pointer; }

/* skeletons */
.skel { position:relative; overflow:hidden; background:var(--surface-2); border-radius:12px; height:4.6rem; }
.skel::after { content:""; position:absolute; inset:0; transform:translateX(-100%);
               background:linear-gradient(90deg,transparent,color-mix(in srgb,var(--surface) 60%,transparent),transparent); animation:shimmer 1.2s infinite; }
@keyframes shimmer { 100% { transform:translateX(100%); } }

/* transcript view */
.thead { display:flex; align-items:flex-start; gap:.6rem; margin:.1rem 0 .5rem; }
.back { border:1px solid var(--line-2); background:var(--surface); color:var(--muted);
        font-size:.8rem; font-weight:600; padding:.35rem .6rem; border-radius:8px; cursor:pointer; flex:0 0 auto; }
.back:hover { border-color:var(--accent-line); color:var(--accent-ink); }
.thead .tmain { min-width:0; flex:1 1 auto; }
.thead h1 { font-size:1.05rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.thead .tsub { font-size:.75rem; color:var(--muted); }
.needs-banner { display:flex; align-items:center; gap:.5rem; background:var(--amber-bg); border:1px solid var(--amber-bd);
  color:var(--amber-tx); font-size:.8rem; font-weight:600; border-radius:9px; padding:.5rem .7rem; margin:0 0 .6rem; }

.log { display:flex; flex-direction:column; gap:.15rem; min-height:6rem; }
.row { display:flex; margin:.25rem 0; }
.row.me { justify-content:flex-end; }
.bubble { max-width:82%; padding:.45rem .65rem .4rem; border-radius:13px; font-size:.86rem; line-height:1.4;
          background:var(--surface-2); border:1px solid var(--line); }
.row.me .bubble { background:var(--accent-soft); border-color:var(--accent-line); border-bottom-right-radius:4px; }
.row:not(.me) .bubble { border-bottom-left-radius:4px; }
.bubble .who { font-size:.66rem; font-weight:700; letter-spacing:.02em; color:var(--muted); margin-bottom:.15rem; }
.row.me .bubble .who { color:var(--accent-ink); }
.bubble .msg { white-space:pre-wrap; word-break:break-word; }
.bubble .tm { font-size:.62rem; color:var(--faint); margin-top:.2rem; text-align:right; font-variant-numeric:tabular-nums; }
.bubble.pending { opacity:.6; }
.bubble.failed { border-color:var(--red); }

/* reply box — keyboard-safe, generous tap target */
.replybar { position:sticky; bottom:0; display:flex; gap:.5rem; align-items:flex-end; background:var(--bg);
            padding:.5rem 0 .1rem; margin-top:.4rem; border-top:1px solid var(--line); }
.rinput { flex:1 1 auto; min-width:0; padding:.6rem .75rem; font-size:.9rem; color:var(--fg); background:var(--surface);
          border:1px solid var(--line-2); border-radius:12px; outline:none; resize:none; max-height:6.5rem; font-family:inherit; }
.rinput:focus { border-color:var(--accent-line); box-shadow:0 0 0 3px var(--accent-soft); }
.rinput:disabled { opacity:.55; }
.rsend { flex:0 0 auto; width:2.6rem; height:2.6rem; border-radius:50%; background:var(--accent); color:var(--accent-fg);
         border:1px solid var(--accent); font-size:1rem; cursor:pointer; display:grid; place-items:center; }
.rsend:hover { filter:brightness(1.06); }
.rsend:disabled { opacity:.5; cursor:default; }
.rerr { color:var(--red); font-size:.76rem; margin:.2rem 0 0; }
</style></head>
<body>
<div class="wrap"><div id="view"><div class="skel"></div></div></div>
<script>
"use strict";
const $=(id)=>document.getElementById(id);
const view=$("view");

let state={ view:"list", threads:null, threadsStatus:null, open:null, transcript:null, cursor:null,
            seenKeys:new Set(), seenContent:new Set(), pendingText:"" };
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
function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function isHiren(name){ return String(name||"").trim().toLowerCase()==="hiren"; }
function relTime(iso){ if(!iso) return ""; const t=Date.parse(iso); if(isNaN(t)) return "";
  const secs=(Date.now()-t)/1000; if(secs<45) return "just now"; const m=Math.floor(secs/60);
  if(m<60) return m+"m ago"; const h=Math.floor(m/60); if(h<24) return h+"h ago"; return Math.floor(h/24)+"d ago"; }

// ─────────────────────────── LIST view ──────────────────────────
async function loadList(showSkeleton){
  if(showSkeleton){ state.threadsStatus="loading"; renderList(); }
  try{
    const d=await callTool("kb_meetings",{});
    if(d && d.error) throw new Error(d.error);
    state.threads=Array.isArray(d && d.threads)?d.threads:[]; state.threadsStatus="ok";
  }catch(e){ if(!state.threads) state.threadsStatus="error"; }
  if(state.view==="list") renderList();
}
async function pollList(){
  if(state.view!=="list" || document.hidden) return;
  try{
    const d=await callTool("meetings_state",{});
    if(d && !d.error){ state.threads=Array.isArray(d.threads)?d.threads:[]; state.threadsStatus="ok";
      if(state.view==="list") renderList(); }
  }catch(e){ /* keep showing the last good list; a single missed poll is invisible */ }
}
function roomCard(t){
  const needs=!!t.needs_hiren;
  const parts=(t.participants||[]).map(p=>'<span class="pdot"><span class="d'+(isHiren(p)?" hiren":"")+'"></span>'+esc(isHiren(p)?"Hiren":p)+'</span>').join("");
  const lt=t.last_turn;
  const preview=lt ? '<b>'+esc(isHiren(lt.sender)?"Hiren":lt.sender)+':</b> '+esc(lt.message||"") : "";
  return '<button class="room'+(needs?" needs":"")+'" data-open="'+esc(t.thread)+'">'
    +'<div class="rtop"><span class="rtopic">'+esc(t.topic||t.thread)+'</span>'
      +(needs?'<span class="needs-badge"><span class="pulse"></span>needs you</span>':'')
    +'</div>'
    +(parts?'<div class="parts">'+parts+'</div>':'')
    +(preview?'<div class="preview">'+preview+'</div>':'')
    +(lt&&lt.timestamp?'<div class="rwhen">'+esc(relTime(lt.timestamp))+'</div>':'')
  +'</button>';
}
function renderList(){
  state.view="list"; startPolling();
  let body;
  if(state.threadsStatus==="loading"){ body='<div class="skel"></div><div class="skel"></div>'; }
  else if(state.threadsStatus==="error"){ body='<div class="errbox"><span>Couldn\'t load meetings.</span><button class="retry" id="retry-list">Retry</button></div>'; }
  else { const rows=state.threads||[];
    body = rows.length ? '<div class="rooms">'+rows.map(roomCard).join("")+'</div>'
      : '<div class="empty">No meetings right now — agents open rooms with kb_thread_post.</div>'; }
  view.innerHTML='<p class="eyebrow">Meetings</p><h1>Live rooms</h1>'+body;
  const rt=$("retry-list"); if(rt) rt.onclick=()=>loadList(true);
  scheduleHeight();
}

// ─────────────────────────── TRANSCRIPT view ──────────────────────────
function turnKey(t){ return (t.seq!=null?"s"+t.seq:"")+"|"+(t.sender||"")+"|"+(t.timestamp||""); }
function contentKey(t){ return (t.sender||"")+"|"+String(t.message||""); }
function bubbleHtml(t, pendingCls){
  const me=isHiren(t.sender);
  return '<div class="row'+(me?" me":"")+'"><div class="bubble'+(pendingCls?" "+pendingCls:"")+'">'
    +'<div class="who">'+esc(me?"Hiren":t.sender)+'</div>'
    +'<div class="msg">'+esc(t.message||"")+'</div>'
    +(t.timestamp?'<div class="tm">'+esc(relTime(t.timestamp))+'</div>':'')
  +'</div></div>';
}
function appendTurns(turns){
  const log=$("log"); if(!log) return;
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  let added=false;
  for(const t of (turns||[])){
    const key=turnKey(t);
    if(state.seenKeys.has(key)) continue;
    // an optimistic Hiren bubble we already rendered locally — the real turn just
    // arrived; drop the duplicate rather than showing the same reply twice.
    if(isHiren(t.sender) && state.seenContent.has(contentKey(t))){ state.seenKeys.add(key); continue; }
    state.seenKeys.add(key); state.seenContent.add(contentKey(t));
    log.insertAdjacentHTML("beforeend", bubbleHtml(t));
    added=true;
  }
  if(added && atBottom) log.scrollTop=log.scrollHeight;
  return added;
}
async function openTranscript(thread){
  state.view="transcript"; state.open=thread; state.cursor=null; state.transcript=null;
  state.seenKeys=new Set(); state.seenContent=new Set();
  renderTranscript(true);
  try{ await rpcReq("ui/request-display-mode",{mode:"fullscreen"},4000); }catch(e){ /* best-effort — inline is fine */ }
  await loadTranscript(true);
}
function closeTranscript(){ state.open=null; renderList(); }
async function loadTranscript(initial){
  if(state.view!=="transcript") return;
  try{
    const d=await callTool("meeting_transcript",{thread:state.open, since:state.cursor||""});
    if(state.view!=="transcript" || state.open==null) return;
    if(d && d.error){ if(initial) showTranscriptError("Couldn't load this meeting."); return; }
    if(d && d.cursor) state.cursor=d.cursor;
    if(d){ state.transcript=d; }
    if(initial) renderTranscript(false);
    appendTurns(d && d.turns);
    if(initial && !(d && d.turns && d.turns.length)){
      const log=$("log"); if(log && !log.children.length) log.innerHTML='<div class="empty">No messages yet.</div>';
    }
    updateComposerState();
  }catch(e){ if(initial) showTranscriptError("Couldn't load this meeting."); }
}
async function pollTranscript(){
  if(state.view!=="transcript" || document.hidden) return;
  await loadTranscript(false);
}
function showTranscriptError(msg){
  const log=$("log"); if(log) log.innerHTML='<div class="errbox"><span>'+esc(msg)+'</span><button class="retry" id="retry-tx">Retry</button></div>';
  const rt=$("retry-tx"); if(rt) rt.onclick=()=>loadTranscript(true);
}
function updateComposerState(){
  const closed = state.transcript && (state.transcript.status==="closed" || state.transcript.closed_by);
  const inp=$("rinput"), snd=$("rsend");
  if(!inp||!snd) return;
  inp.disabled=!!closed; snd.disabled=!!closed;
  inp.placeholder = closed ? "This meeting has ended" : "Reply as Hiren…";
}
function renderTranscript(skeleton){
  const th=(state.threads||[]).find(x=>x.thread===state.open) || {thread:state.open, topic:state.open, participants:[]};
  const needs=!!th.needs_hiren;
  const parts=(th.participants||[]).length ? (th.participants.length+" in the room") : "";
  view.innerHTML='<div class="thead"><button class="back" id="tx-back">‹ Rooms</button>'
      +'<div class="tmain"><h1>'+esc(th.topic||th.thread)+'</h1>'
      +(parts?'<div class="tsub">'+esc(parts)+'</div>':'')+'</div></div>'
    +(needs?'<div class="needs-banner" id="needs-banner">An agent is waiting on you — reply below</div>':'')
    +(skeleton?'<div class="skel"></div>':'<div class="log" id="log"></div>')
    +'<div class="replybar"><textarea id="rinput" class="rinput" rows="1" placeholder="Reply as Hiren…" aria-label="Reply"></textarea>'
    +'<button class="rsend" id="rsend" aria-label="Send">➤</button></div>'
    +'<div class="rerr" id="rerr" hidden></div>';
  $("tx-back").onclick=closeTranscript;
  const inp=$("rinput"), snd=$("rsend");
  const autoGrow=()=>{ inp.style.height="auto"; inp.style.height=Math.min(inp.scrollHeight,104)+"px"; scheduleHeight(); };
  inp.oninput=autoGrow;
  inp.onkeydown=(e)=>{ if(e.key==="Enter" && !e.shiftKey){ e.preventDefault(); sendReply(); } };
  snd.onclick=sendReply;
  startPolling();
  scheduleHeight();
}
async function sendReply(){
  const inp=$("rinput"), snd=$("rsend"), errEl=$("rerr"), log=$("log");
  if(!inp || !state.open) return;
  const text=inp.value.trim(); if(!text) return;
  inp.value=""; inp.style.height="auto"; if(errEl) errEl.hidden=true;
  inp.disabled=true; snd.disabled=true;
  const optimistic={sender:"hiren", message:text, timestamp:new Date().toISOString()};
  const tempKey="pending|"+contentKey(optimistic)+"|"+optimistic.timestamp;
  if(log){ log.insertAdjacentHTML("beforeend", '<div class="row me" data-pending="'+esc(tempKey)+'"><div class="bubble pending">'
    +'<div class="who">Hiren</div><div class="msg">'+esc(text)+'</div></div></div>');
    log.scrollTop=log.scrollHeight; }
  state.seenContent.add(contentKey(optimistic));
  try{
    const r=await callTool("meeting_reply",{thread:state.open, message:text});
    if(r && r.error) throw new Error(r.error);
    inp.disabled=false; snd.disabled=false; scheduleHeight();
    // Hiren just replied — clear the needs-you state immediately rather than
    // waiting on the next poll: the banner here, and the row in the cached
    // list so the badge is already gone if he taps back before it refreshes.
    const banner=$("needs-banner"); if(banner) banner.remove();
    const row=(state.threads||[]).find(t=>t.thread===state.open); if(row) row.needs_hiren=false;
    // pull the confirmed turn in on the next poll tick (or immediately)
    loadTranscript(false);
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
let listTimer=0, txTimer=0;
function stopAllPolling(){ if(listTimer){ clearInterval(listTimer); listTimer=0; } if(txTimer){ clearInterval(txTimer); txTimer=0; } }
function startPolling(){
  stopAllPolling();
  if(document.hidden) return;
  if(state.view==="list") listTimer=setInterval(pollList,5000);
  else if(state.view==="transcript") txTimer=setInterval(pollTranscript,2000);
}
document.addEventListener("visibilitychange",()=>{ if(document.hidden) stopAllPolling(); else startPolling(); });

// ─────────────────── seeding & reconcile ─────────────────
function seedFrom(data){
  if(!data) return;
  booted=true;
  if(Array.isArray(data.threads)){ state.threads=data.threads; state.threadsStatus="ok"; if(state.view!=="transcript") renderList(); return; }
  if(data.turns!==undefined && data.thread!==undefined){
    // a meeting_transcript-shaped push (unlikely as the mount seed, but handled)
    state.open=data.thread; state.transcript=data; state.cursor=data.cursor||null; renderTranscript(false); appendTurns(data.turns); return; }
  loadList(true);
}

// ─────────────────── delegated clicks ─────────────────
document.addEventListener("click",(e)=>{
  const op=e.target.closest("[data-open]"); if(op){ openTranscript(op.getAttribute("data-open")); return; }
});

// ─────────────────────────── boot ──────────────────────────
(async function init(){
  try{ if(window.openai && window.openai.toolOutput){ seedFrom(normalize(window.openai.toolOutput)); } }catch(e){}
  try{ await rpcReq("ui/initialize",{appCapabilities:{availableDisplayModes:["inline","fullscreen"]},appInfo:{name:"engram-meetings",version:"1.0.0"},protocolVersion:"2026-01-26"});
       rpcNote("ui/notifications/initialized",{}); }catch(e){}
  if(!booted) await loadList(true);
  reportHeight();
})();
</script></body></html>
"""
