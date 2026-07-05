"""Brain Navigator — an MCP App (SEP-1865) widget over the kb_* tools.

The server registers ``NAVIGATOR_URI`` as a ``ui://`` resource and tags the
navigation tools (kb_projects, kb_load, kb_search) with ``_meta.ui.resourceUri``.
An MCP-Apps-capable host (claude.ai, ChatGPT) mounts this single self-contained
HTML doc in a sandboxed iframe, pushes the tool result in, and relays the
widget's ``tools/call`` back to the server. The widget talks ONLY over that
postMessage bridge (plus a ``window.openai`` path for ChatGPT) — it makes zero
direct network requests, so the resource CSP is deny-by-default (empty arrays).

M1 scope: a HOME view of project cards and a BROWSE/READER view that renders the
kb_load index tree and reads concept files with a small built-in markdown
renderer. Search and Inbox tabs are present but disabled (M2).

Everything here is opt-in behind ``ENGRAM_WIDGET`` (config ``widget``): when the
flag is off the tool meta is ``None`` and the resource is never registered, so
the server behaves exactly as before.

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


def register_navigator(mcp: "FastMCP", enabled: bool) -> None:
    """Register the ui:// resource when the widget flag is on; a no-op when off.

    Factored out of app.py so a test can build a fresh FastMCP with the flag on
    and assert the resource is present, without reloading the app module (whose
    settings are read once at import via an lru_cache).
    """
    if not enabled:
        return

    @mcp.resource(
        NAVIGATOR_URI, mime_type=NAVIGATOR_MIME, meta=navigator_resource_meta()
    )
    def navigator_resource() -> str:
        return NAVIGATOR_HTML


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
}
a { color:var(--accent-ink); text-decoration:none; }
a:hover { text-decoration:underline; text-underline-offset:2px; }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px; border-radius:3px; }
.wrap { padding:12px 14px 14px; }

/* eyebrow / small-caps labels */
.eyebrow { font-size:.66rem; font-weight:700; letter-spacing:.13em; text-transform:uppercase; color:var(--accent-ink); margin:0 0 .3rem; }
.section-label { font-size:.66rem; font-weight:700; letter-spacing:.12em; text-transform:uppercase; color:var(--faint); margin:1.1rem 0 .35rem; }

/* tab bar */
.tabs { display:flex; gap:2px; border-bottom:1px solid var(--line); padding:0 6px; background:color-mix(in srgb,var(--bg) 90%,transparent); position:sticky; top:0; z-index:5; }
.tab { appearance:none; border:0; background:none; font:inherit; font-size:.78rem; font-weight:600; letter-spacing:.02em;
       color:var(--muted); padding:.5rem .7rem; cursor:pointer; border-bottom:2px solid transparent; margin-bottom:-1px; }
.tab:hover:not(:disabled) { color:var(--fg); }
.tab.active { color:var(--accent-ink); border-bottom-color:var(--accent); }
.tab:disabled { color:var(--faint); cursor:default; }
.tab .m2 { font-size:.58rem; text-transform:uppercase; letter-spacing:.08em; color:var(--faint); border:1px solid var(--line-2); border-radius:4px; padding:0 .25rem; margin-left:.3rem; vertical-align:middle; }

h1 { font-size:1.35rem; line-height:1.15; letter-spacing:-0.02em; margin:0 0 .15rem; font-weight:700; }
.lede { color:var(--muted); font-size:.85rem; margin:.15rem 0 0; }

/* project cards */
.cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(13rem,1fr)); gap:.6rem; margin:.9rem 0 .3rem; }
.card { display:flex; flex-direction:column; gap:.3rem; padding:.7rem .8rem; text-align:left; cursor:pointer;
        background:var(--surface); color:var(--fg); border:1px solid var(--line); border-radius:10px; box-shadow:var(--shadow);
        font:inherit; transition:border-color .12s ease, transform .12s ease; }
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
.chip .k { color:var(--faint); text-transform:uppercase; letter-spacing:.05em; font-size:.9em; }
.chips { display:flex; flex-wrap:wrap; gap:.35rem; margin:.5rem 0 0; }
.when { font-size:.7rem; color:var(--faint); font-variant-numeric:tabular-nums; }

/* stamp glyph */
.stamp { flex:0 0 auto; width:1.5rem; height:1.5rem; border-radius:7px; display:grid; place-items:center;
         background:var(--accent-soft); border:1px solid var(--accent-line); font-size:.9rem; }

/* browse header */
.bhead { display:flex; align-items:center; gap:.5rem; margin:.2rem 0 .1rem; }
.back { appearance:none; border:1px solid var(--line-2); background:var(--surface); color:var(--muted); font:inherit;
        font-size:.74rem; font-weight:600; padding:.2rem .5rem; border-radius:7px; cursor:pointer; }
.back:hover { border-color:var(--accent-line); color:var(--accent-ink); }
.crumbs { font-size:.72rem; color:var(--muted); margin:.1rem 0 .7rem; display:flex; flex-wrap:wrap; gap:.1rem; align-items:center; }
.crumbs button { appearance:none; border:0; background:none; font:inherit; font-size:inherit; color:var(--muted); cursor:pointer; padding:0; }
.crumbs button:hover { color:var(--accent-ink); text-decoration:underline; }
.crumbs .sep { color:var(--faint); margin:0 .3rem; }
.crumbs .cur { color:var(--fg); }

/* context summary block */
.ctx { border:1px solid var(--line); border-left:3px solid var(--accent-line); border-radius:0 8px 8px 0;
       background:var(--inset); padding:.5rem .8rem; margin:.7rem 0 0; max-height:15rem; overflow:hidden; position:relative; }
.ctx.fade::after { content:""; position:absolute; left:0; right:0; bottom:0; height:2.5rem; background:linear-gradient(transparent,var(--inset)); }

/* tree */
.tree { margin:.5rem 0 0; }
.tnode { margin:0; }
.tnode > summary { display:flex; align-items:center; gap:.35rem; padding:.28rem .35rem; border-radius:6px; cursor:pointer;
                   font-size:.83rem; font-weight:600; color:var(--fg); list-style:none; }
.tnode > summary::-webkit-details-marker { display:none; }
.tnode > summary::before { content:"›"; color:var(--faint); font-weight:400; width:.7rem; display:inline-block; transition:transform .12s ease; }
.tnode[open] > summary::before { transform:rotate(90deg); }
.tnode > summary:hover { background:var(--surface-2); }
.tnode .kids { margin:.05rem 0 .3rem .85rem; padding-left:.5rem; border-left:1px solid var(--line); }
.frow { display:flex; align-items:baseline; gap:.4rem; padding:.28rem .4rem; border-radius:6px; cursor:pointer; width:100%; text-align:left;
        appearance:none; border:0; background:none; font:inherit; color:var(--fg); }
.frow:hover { background:var(--surface-2); }
.frow .glyph { color:var(--faint); font-size:.85rem; flex:0 0 auto; }
.frow .ft { font-size:.83rem; font-weight:550; }
.frow .fd { font-size:.75rem; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

/* reader */
.props { display:flex; flex-wrap:wrap; gap:.35rem; margin:.6rem 0 .9rem; }
.statusv { display:inline-flex; align-items:center; gap:.35rem; text-transform:capitalize; }

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

.empty { color:var(--faint); font-size:.82rem; font-style:italic; padding:1.2rem 0; }
.err { color:var(--red); font-size:.82rem; padding:.8rem 0; }
.spin { color:var(--muted); font-size:.82rem; padding:1.2rem 0; }
</style></head>
<body>
<nav class="tabs" id="tabs">
  <button class="tab" id="tab-home" data-tab="home">Home</button>
  <button class="tab" id="tab-browse" data-tab="browse">Browse</button>
  <button class="tab" id="tab-search" data-tab="search" disabled>Search<span class="m2">M2</span></button>
  <button class="tab" id="tab-inbox" data-tab="inbox" disabled>Inbox<span class="m2">M2</span></button>
</nav>
<div class="wrap"><div id="view"><div class="spin">Loading…</div></div></div>
<script>
"use strict";
const $=(id)=>document.getElementById(id);
const view=$("view");

// ── unified in-widget state. widgetState persists ONLY {view, projectId, path};
// on boot we re-fetch from the tools regardless — the git repo is the truth, a
// stale restored payload is not. ──
let state={ view:"home", projects:null, load:null, projectId:null, readPath:null, read:null };
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
    const pr=m.params||{}; const d=pr.structuredContent ?? parseText(pr.content);
    if(d) seedFrom(d);   // a fresh tool call remounted us with new data — reseed
  }
},{passive:true});
function rpcReq(method,params,timeout){ return new Promise((resolve,reject)=>{ const id=nextId++; pending.set(id,{resolve,reject});
  window.parent.postMessage({jsonrpc:"2.0",id,method,params},"*");
  setTimeout(()=>{ if(pending.has(id)){ pending.delete(id); reject(new Error("host timeout")); } }, timeout||120000); }); }
function rpcNote(method,params){ window.parent.postMessage({jsonrpc:"2.0",method,params},"*"); }
function parseText(content){ try{ const t=Array.isArray(content)?(content.find(c=>c.type==="text")||{}).text:null; return t?JSON.parse(t):null; }catch(e){ return null; } }

// robust tool result -> data (handles isError / structuredContent / content-array / raw / JSON string)
function normalize(r){ if(r==null) return null;
  if(typeof r==="object"){
    if(r.isError){ const t=(r.content||[]).find(c=>c&&c.type==="text"); return {error:(t&&t.text)||"tool error"}; }
    if(r.structuredContent) return r.structuredContent;
    if(Array.isArray(r.content)){ const t=r.content.find(c=>c&&c.type==="text"); if(t){ try{ return JSON.parse(t.text); }catch(e){ return {error:t.text}; } } }
    return r; }
  if(typeof r==="string"){ try{ return JSON.parse(r); }catch(e){ return null; } }
  return null; }
async function callTool(name,args){
  // ChatGPT exposes window.openai.callTool; MCP Apps hosts relay tools/call over the bridge.
  if(window.openai && window.openai.callTool){ return normalize(await window.openai.callTool(name,args||{})); }
  return normalize(await rpcReq("tools/call",{name,arguments:args||{}}));
}
function persist(){ try{ if(window.openai && typeof window.openai.setWidgetState==="function")
  window.openai.setWidgetState({view:state.view, projectId:state.projectId, path:state.readPath}); }catch(e){} }

// ─────────────────────────── helpers ──────────────────────────
function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
const TYPE_GLYPHS={decision:"⚖",spec:"📐",runbook:"🛠",person:"👤",client:"🏢",video:"🎬",message:"✉",reference:"🔖",project:"📁",idea:"💡",meeting:"🗓",snippet:"✂",note:"📝"};
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
// POSIX path resolve, matching kbstore's normpath(join(base, rel)); anchors stripped.
function dirOf(p){ const i=(p||"").lastIndexOf("/"); return i<0?"":p.slice(0,i); }
function resolveRel(baseDir, rel){ rel=(rel||"").split("#")[0];
  const stack = rel.startsWith("/") ? [] : (baseDir?baseDir.split("/").filter(Boolean):[]);
  for(const seg of rel.split("/")){ if(seg===""||seg===".") continue; if(seg==="..") stack.pop(); else stack.push(seg); }
  return stack.join("/"); }

// strip a leading YAML frontmatter block (--- ... ---) so we render the body only
function stripFrontmatter(src){ src=(src||"").replace(/\r\n/g,"\n");
  if(src.startsWith("---\n")){ const end=src.indexOf("\n---",3); if(end!==-1){ const nl=src.indexOf("\n",end+1); return src.slice(nl===-1?src.length:nl+1); } }
  return src; }

// ─────────────────── small markdown renderer ───────────────────
// Escapes HTML first, then applies a safe subset: headings, bold/italic, inline
// + fenced code, lists (nested), links, blockquote, hr. Relative .md links become
// in-widget navigation; http(s) links open in a new tab.
function mdInline(s){
  const codes=[];
  s=s.replace(/`([^`]+)`/g,(m,c)=>{ codes.push(c); return ""+(codes.length-1)+""; });
  s=s.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g,(m,txt,url)=>linkHtml(txt,url));
  s=s.replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>").replace(/__([^_]+)__/g,"<strong>$1</strong>");
  s=s.replace(/(^|[^*\w])\*(?!\s)([^*]+?)\*(?!\w)/g,"$1<em>$2</em>").replace(/(^|[^_\w])_(?!\s)([^_]+?)_(?!\w)/g,"$1<em>$2</em>");
  s=s.replace(/(\d+)/g,(m,i)=>"<code>"+codes[+i]+"</code>");
  return s;
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
  const flushList=(items)=>items;
  while(i<lines.length){
    let line=lines[i];
    if(/^\s*$/.test(line)){ i++; continue; }
    // fenced code
    let fence=line.match(/^\s*```(.*)$/);
    if(fence){ i++; let buf=[]; while(i<lines.length && !/^\s*```\s*$/.test(lines[i])){ buf.push(lines[i]); i++; } i++;
      html+="<pre><code>"+buf.join("\n")+"</code></pre>"; continue; }
    // hr
    if(/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)){ html+="<hr>"; i++; continue; }
    // heading
    let h=line.match(/^\s*(#{1,6})\s+(.*)$/);
    if(h){ const n=h[1].length; html+="<h"+n+">"+mdInline(h[2].trim())+"</h"+n+">"; i++; continue; }
    // blockquote — note the source is already HTML-escaped, so '>' is now '&gt;'
    if(/^\s*&gt;\s?/.test(line)){ let buf=[]; while(i<lines.length && /^\s*&gt;\s?/.test(lines[i])){ buf.push(lines[i].replace(/^\s*&gt;\s?/,"")); i++; }
      html+="<blockquote>"+mdInline(buf.join(" "))+"</blockquote>"; continue; }
    // list (ordered/unordered, nested by indent)
    if(/^\s*([-*+]|\d+\.)\s+/.test(line)){ let items=[]; while(i<lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])){
        const m=lines[i].match(/^(\s*)([-*+]|\d+\.)\s+(.*)$/);
        items.push({indent:m[1].replace(/\t/g,"  ").length, ordered:/\d/.test(m[2]), text:m[3]}); i++; }
      html+=buildList(items); continue; }
    // paragraph
    let buf=[]; while(i<lines.length && !/^\s*$/.test(lines[i]) && !/^\s*(#{1,6}\s|&gt;|```|([-*+]|\d+\.)\s|([-*_])(\s*\3){2,}\s*$)/.test(lines[i])){ buf.push(lines[i]); i++; }
    html+="<p>"+mdInline(buf.join(" ").trim())+"</p>";
  }
  return html;
}
function buildList(items){ // indent-stack nesting
  let out="", stack=[]; // each: {indent, ordered}
  for(const it of items){
    while(stack.length && it.indent < stack[stack.length-1].indent){ out+=(stack.pop().ordered?"</ol>":"</ul>"); }
    if(!stack.length || it.indent > stack[stack.length-1].indent){ out+=(it.ordered?"<ol>":"<ul>"); stack.push({indent:it.indent,ordered:it.ordered}); }
    out+="<li>"+mdInline(it.text)+"</li>";
  }
  while(stack.length){ out+=(stack.pop().ordered?"</ol>":"</ul>"); }
  return out;
}

// ─────────────────────────── views ──────────────────────────
function setActiveTab(){ document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("active", t.dataset.tab===state.view || (state.view==="reader"&&t.dataset.tab==="browse"))); }
function show(html){ view.innerHTML=html; setActiveTab(); scheduleHeight(); }

function renderHome(){
  state.view="home"; persist(); setActiveTab();
  const ps=state.projects||[];
  let cards = ps.length ? ps.map(p=>{
    const unread = (p.unread_messages|0);
    return '<button class="card" data-proj="'+esc(p.id)+'">'
      +'<div class="card-head"><span class="dot '+statusDot(p.status)+'"></span><h3>'+esc(p.title||p.id)+'</h3></div>'
      +(p.description?'<p>'+esc(p.description)+'</p>':'')
      +'<div class="card-foot">'
        +(unread>0?'<span class="badge unread">'+unread+' unread</span>':'')
        +(p.last_session?'<span class="when">'+esc(relTime(p.last_session))+'</span>':'')
      +'</div></button>';
  }).join("") : '<div class="empty">No projects found.</div>';
  show('<p class="eyebrow">Knowledge Base</p><h1>brain</h1>'
      +'<p class="lede">Pick a project to browse its context, concepts, and log.</p>'
      +'<div class="cards">'+cards+'</div>');
  view.querySelectorAll("[data-proj]").forEach(b=>b.onclick=()=>openProject(b.dataset.proj));
}

function treeHtml(node, depth){
  let out="";
  const files=(node.files||[]).map(f=>
    '<button class="frow" data-path="'+esc(f.path)+'">'
      +'<span class="glyph">◦</span>'
      +'<span class="ft">'+esc(f.title||f.name)+'</span>'
      +(f.description?'<span class="fd">— '+esc(f.description)+'</span>':'')
    +'</button>').join("");
  const dirs=(node.dirs||[]).map(d=>
    '<details class="tnode"'+(depth<1?" open":"")+'>'
      +'<summary>'+esc(d.title||d.name)+'</summary>'
      +'<div class="kids">'+treeHtml(d,depth+1)+'</div>'
    +'</details>').join("");
  out=dirs+files;
  return out || '<div class="empty">Empty.</div>';
}

function renderBrowse(){
  state.view="browse"; state.readPath=null; state.read=null; persist(); setActiveTab();
  const L=state.load; if(!L){ renderHome(); return; }
  const tree=L.index_tree||{files:[],dirs:[]};
  const title=tree.title||L.project;
  let ctx="";
  if(L.context_md){ const body=stripFrontmatter(L.context_md).split("\n").slice(0,40).join("\n");
    ctx='<div class="ctx fade"><div class="md">'+renderMarkdown(body)+'</div></div>'; }
  show('<div class="bhead"><button class="back" id="tohome">‹ Home</button>'
        +'<span class="stamp">'+glyph("project")+'</span>'
        +'<div><p class="eyebrow" style="margin:0">Project</p><h1 style="font-size:1.1rem">'+esc(title)+'</h1></div></div>'
      +ctx
      +'<p class="section-label">Concepts</p><div class="tree">'+treeHtml(tree,0)+'</div>');
  $("tohome").onclick=()=>renderHome();
  view.querySelectorAll("[data-path]").forEach(b=>b.onclick=()=>openFile(b.dataset.path));
}

function renderReader(){
  state.view="reader"; persist(); setActiveTab();
  const R=state.read; if(!R){ renderBrowse(); return; }
  const meta=R.meta||{}; const path=R.path||state.readPath||"";
  const name=path.split("/").pop();
  let props="";
  if(meta.type) props+='<span class="chip"><span class="k">type</span> '+esc(meta.type)+'</span>';
  if(meta.status) props+='<span class="chip"><span class="statusv"><span class="dot '+statusDot(meta.status)+'"></span>'+esc(meta.status)+'</span></span>';
  if(meta.confidence) props+='<span class="chip"><span class="k">conf</span> '+esc(meta.confidence)+'</span>';
  const tags=Array.isArray(meta.tags)?meta.tags:(meta.tags?[meta.tags]:[]);
  props+=tags.map(t=>'<span class="chip tag">'+esc(t)+'</span>').join("");
  const crumbs='<button id="rback">‹ back</button><span class="sep">/</span>'
      +'<button id="rhome">'+esc((state.load&&(state.load.index_tree&&state.load.index_tree.title))||state.projectId||"project")+'</button>'
      +'<span class="sep">/</span><span class="cur">'+esc(meta.title||name)+'</span>';
  show('<div class="crumbs">'+crumbs+'</div>'
      +'<span class="stamp" style="float:left;margin:.1rem .5rem .2rem 0">'+glyph(meta.type)+'</span>'
      +'<h1>'+esc(meta.title||name)+'</h1>'
      +(meta.description?'<p class="lede">'+esc(meta.description)+'</p>':'')
      +'<div style="clear:both"></div>'
      +(props?'<div class="props">'+props+'</div>':'')
      +'<div class="md" id="mdbody">'+renderMarkdown(R.content||"")+'</div>');
  $("rback").onclick=()=>renderBrowse();
  $("rhome").onclick=()=>renderBrowse();
  // in-widget navigation for relative .md links (resolve against THIS file's dir)
  view.querySelectorAll("a.mdlink").forEach(a=>a.onclick=(e)=>{ e.preventDefault();
    const rel=a.getAttribute("data-rel"); const target=resolveRel(dirOf(path), rel); if(target) openFile(target); });
}

// ─────────────────────── async actions ──────────────────────
async function loadHome(){ show('<div class="spin">Loading projects…</div>');
  try{ const d=await callTool("kb_projects",{}); if(Array.isArray(d)){ state.projects=d; renderHome(); } else show('<div class="err">Could not load projects.</div>'); }
  catch(e){ show('<div class="err">Could not load projects.</div>'); } }
async function openProject(id){ if(busy) return; busy=true; state.projectId=id; show('<div class="spin">Loading '+esc(id)+'…</div>');
  try{ const d=await callTool("kb_load",{project:id}); if(d && d.index_tree){ state.load=d; renderBrowse(); } else show('<div class="err">Could not load project.</div>'); }
  catch(e){ show('<div class="err">Could not load project.</div>'); } finally{ busy=false; } }
async function openFile(path){ if(busy) return; busy=true; state.readPath=path; show('<div class="spin">Reading…</div>');
  try{ const d=await callTool("kb_read",{path:path}); if(d && d.content!=null){ state.read=d; renderReader(); } else show('<div class="err">Could not read '+esc(path)+'.</div>'); }
  catch(e){ show('<div class="err">Could not read file.</div>'); } finally{ busy=false; } }

// ─────────────────── seeding & reconcile ─────────────────
// The widget mounts because kb_projects / kb_load (/ kb_search) was called; the
// host pushes that result in. Shape it into the right view. kb_load output wins
// (has index_tree); a project array -> HOME; a search array (M2) -> HOME fallback.
function seedFrom(data){
  if(!data) return;
  booted=true;
  if(data.index_tree){ state.load=data; state.projectId=data.project||state.projectId; renderBrowse(); return; }
  if(Array.isArray(data)){
    if(data.length && data[0] && data[0].id!==undefined){ state.projects=data; renderHome(); return; }
    // search results (path/score) — the Search view is M2; fall back to Home.
    loadHome(); return;
  }
  if(data.content!==undefined){ state.read=data; state.readPath=data.path; renderReader(); return; }
  // unknown -> home
  loadHome();
}

// tabs
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{ if(t.disabled) return;
  if(t.dataset.tab==="home"){ if(state.projects) renderHome(); else loadHome(); }
  else if(t.dataset.tab==="browse"){ if(state.load) renderBrowse(); else if(state.projects) renderHome(); else loadHome(); } });

// ─────────────────────────── boot ──────────────────────────
(async function init(){
  // ChatGPT hands the tool output synchronously at mount.
  try{ if(window.openai && window.openai.toolOutput){ seedFrom(normalize(window.openai.toolOutput)); } }catch(e){}
  // ui/initialize handshake. NOTE: params MUST carry appInfo (the client-info
  // field named that way) — the other spelling silently breaks tools/call on claude.ai.
  try{ await rpcReq("ui/initialize",{appCapabilities:{availableDisplayModes:["inline"]},appInfo:{name:"engram-navigator",version:"1.0.0"},protocolVersion:"2026-01-26"});
       rpcNote("ui/notifications/initialized",{}); }catch(e){}
  // Reconcile: if a widgetState was restored and no push seeded us, re-fetch from
  // the tools (the git repo is truth; widgetState is just a pointer).
  if(!booted){
    let st=null; try{ st=window.openai && window.openai.widgetState; }catch(e){}
    if(st && st.view==="reader" && st.projectId && st.path){ await openProject(st.projectId); await openFile(st.path); }
    else if(st && st.view==="browse" && st.projectId){ await openProject(st.projectId); }
    else { await loadHome(); }
  }
  reportHeight();
})();
</script></body></html>
"""
