# HELIX KB — Build Handoff

**From:** Hiren's claude.ai design session, 2026-07-04
**To:** The Claude session building this (likely Claude Code)
**Deliverable:** A working cross-session knowledge ecosystem: OKF bundle in git + Helix MCP tools + skill.

Read this whole file before writing any code. It contains the intent, every decision already made (with rationale — do not relitigate them without new information), the exact contracts, and the build order. A companion `SKILL.md` and a `brain-skeleton.zip` (the repo starting structure) ship alongside this document.

---

## 1. What Hiren Wants (Intent — the "why" behind everything)

Hiren runs many Claude sessions across surfaces (claude.ai web, mobile app, Claude Code) and across projects (Alt Inc work + five freelance clients + metalfinger content channel). Today each session starts cold. He wants:

1. **Cross-session continuity.** Any new session can ask "which project are we working on?" / be told "load alt" and instantly have that project's state — without Hiren re-explaining anything.
2. **Lazy, granular access.** Never load the whole knowledge base. Orient from indexes, fetch individual files only as the conversation needs them, in both directions (read AND write). This is the core reason OKF was chosen: its `index.md` progressive-disclosure convention makes navigation-over-ingestion a first-class pattern.
3. **Sessions talking to each other.** Not just shared knowledge — explicit asynchronous messages. Session A leaves "verify DNS before touching Deccan CMS" and session B (possibly on a different surface, e.g. addressed `to: claude-code`) receives it on load, acts, marks it read. Claude sessions cannot talk live (they only exist while responding), so message-passing through the KB is the mechanism.
4. **OKF-centric and portable.** The knowledge base is a spec-conformant Google OKF v0.1 bundle (markdown + YAML frontmatter, path = concept ID, links = graph, index.md + log.md conventions). Any future consumer — Obsidian, Google's visualizer, other agents — reads it natively. Format outlives tooling.
5. **Web-viewable.** Browsable from any device (git host web UI now, deployed OKF visualizer later).
6. **A skill/capability layer** so Claude sessions know this protocol by default: offer to load a project at session start, know when something is a concept vs a log line vs a message, close sessions properly.
7. **Minimal maintenance.** Reuse existing infra. Only ONE stateful thing (the git repo). Everything else rebuildable.

## 2. Hiren's Existing Infra (facts, verified in conversation)

- Self-hosted stack on Proxmox: Mattermost, Plane, Outline, Qdrant (note: KB search will use **Qdrant Cloud**, not self-hosted), FastAPI, Docker, Cloudflare Tunnel, Forgejo.
- A working MCP server already in production at `https://mcp.metalfinger.xyz/mcp` (the "Survey" MCP) — FastAPI-based, reachable from claude.ai sessions via the connector. **The KB tools extend this server** (new `kb_*` namespace) or run as a sibling app on the same infra — builder's choice, same pattern.
- Claude.ai connectors active: Gmail, Google Drive/Calendar, Notion, Netlify, Webflow, Higgsfield, Survey.
- Comfortable with: FastAPI, Docker, git, Forgejo CI, Netlify.
- Projects to scaffold: `alt` (Alt Inc — primary), `hyprlocl`, `deccan-transcon`, `materia-verde`, `arete`, `fx-stuthi` (freelance), plus `metalfinger` (content channel) and `self` + `library` trees.

## 3. Decisions Already Made (with rationale — the "why not X" record)

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Format | **OKF v0.1** (spec: github.com/GoogleCloudPlatform/knowledge-catalog, okf/SPEC.md) | Freeform markdown / pure Obsidian conventions | OKF formalizes exactly the conventions needed (frontmatter, index.md, log.md, standard md links not wikilinks) so existing/future tooling consumes it with zero adaptation. Obsidian can still open the repo as a vault. |
| Store | **Git repo (monorepo `brain`)** | Notion as primary | Git gives history/diffs/portability/greppability and native Claude Code access. Notion = knowledge trapped in a product; may become a one-way export *view* later. Monorepo (not per-project repos) because cross-project markdown links must not break. |
| Remote (v1) | **GitHub, private** | Forgejo as primary | Forgejo adds nothing functionally (the server works on a local checkout; the remote is only a sync/durability point) but adds two failure modes (homelab down, mirror job). GitHub = zero maintenance + IS the offsite backup + bonus fallback transport via GitHub's official MCP. Switching to Forgejo later is one `git remote set-url`. Revisit if client-confidential material demands self-hosting. |
| Transport | **Helix MCP server, `kb_*` tools** | Git CLI inside each session; Notion MCP | In-session git = whole-repo clones for reads and no safe way to hold push credentials (never paste PATs in chat). The MCP server holds ONE local checkout; every read is one file returned, every write is one file + server-side commit/push with its own deploy key. Sessions never see git. This kills the "reading requires the entire repo" problem entirely. |
| Search | **Qdrant Cloud as disposable cache, deferred to v1.1** | Search-first design | Repo is the single source of truth; vectors carry payload `{path, project, type, tags, status, timestamp, heading}` and are fully rebuildable by a reindex script. Qdrant down → `kb_search` falls back to server-side ripgrep; nothing else affected. |
| Session memory | **Two files per project:** `context.md` (living state, rewritten) + `log.md` (append-only journal) **+ `messages/`** (explicit session-to-session mail) | Log-only | Messages ≠ logs: messages are instructions with lifecycle (unread→read→archived) and addressing (`to: any|claude-code|mobile`); logs are history. Concepts are durable; messages are ephemeral. |
| Skill delivery | **Dual:** MCP tool descriptions carry the protocol for claude.ai sessions (no skill install possible there); `SKILL.md` installed for Claude Code/Cowork. Skill file ALSO lives inside the repo at `skills/engram/SKILL.md` (self-describing KB). | — | Tool descriptions are the only behavior channel in claude.ai; the Survey MCP already proves this works. |

## 4. Architecture

```
CONSUMERS
  claude.ai / mobile ──► kb_* MCP tools          (never touches git)
  Claude Code        ──► git directly OR kb_* tools
  Obsidian           ──► opens repo as vault
  OKF visualizer     ──► static HTML render of bundle (Netlify)
        │
        ▼ MCP streamable HTTP + bearer token (configured once in connector, never in chat)
HELIX MCP SERVER  (FastAPI, Docker, Cloudflare Tunnel — existing stack)
  • local checkout of `brain` = the working surface for ALL reads/writes
  • write path: asyncio lock → pull --rebase → validate frontmatter → write → commit (author: helix-bot) → push
  • read path: plain file reads from checkout; NO cloning, NO whole-repo transfer, ever
  • on write: chunk-by-heading → embed → upsert Qdrant (v1.1)
        │
        ▼ push/pull (deploy key)
GITHUB private repo `brain`  = the OKF bundle = the ONLY stateful thing
        │
        ▼ nightly reconcile walk (catches direct git edits/renames/deletes)
QDRANT CLOUD collection `brain` (disposable, rebuildable)
```

Per-session data economics (the accessibility requirement, quantified): `kb_projects` ≈ one index; `kb_load` ≈ context.md + index tree + unread messages (~2–3K tokens on a mature project, NO concept bodies); then 1 file per `kb_read`/`kb_write`. A session on a 300-file brain typically transfers ~5 files.

## 5. Repo Structure (skeleton zip provided — generate any missing parts from this)

```
brain/
├── index.md                      # root index: links self/, projects/, metalfinger/, library/, skills/
├── skills/engram/SKILL.md      # the protocol, versioned with the knowledge it governs
├── self/
│   ├── index.md
│   ├── stack.md                  # type: reference — Hiren's tools/infra/preferences
│   └── bio.md
├── projects/
│   ├── index.md
│   └── <project>/                # alt, hyprlocl, deccan-transcon, materia-verde, arete, fx-stuthi
│       ├── index.md
│       ├── context.md            # type: project — REQUIRED. Sections: About, Current Phase, Open Loops, Next Actions
│       ├── log.md                # append-only, newest first, dated entries
│       ├── messages/
│       │   ├── index.md
│       │   └── archive/
│       ├── decisions/index.md    # one file per settled decision: YYYY-MM-slug.md
│       ├── specs/index.md
│       ├── people/index.md
│       └── assets/index.md       # type: reference — links out to designs/repos/docs
├── metalfinger/
│   ├── index.md
│   ├── log.md
│   └── videos/index.md           # type: video — per planned video (helix.md first)
└── library/
    ├── index.md
    ├── runbooks/index.md         # cross-project how-tos
    └── snippets/index.md
```

**Frontmatter grammar (validator must enforce):** required `type`; auto-fill if missing: `title`, `description`, `timestamp` (ISO 8601). Optional per spec: `tags`, `resource`. Custom (spec permits extra fields): `status` (active|done|archived|idea | for messages: unread|read|archived), `project` (denormalized for search filtering), `confidence` (settled|tentative|superseded), and message-only: `to` (any|claude-code|mobile), `priority`, `expires`.

**Type taxonomy (open — add freely):** project, client, person, decision, spec, runbook, idea, meeting, video, snippet, reference, message.

**index.md files** follow OKF exactly: no frontmatter; headings grouping `* [Title](relative-path.md) - description` entries. `kb_write` must auto-append a new concept to its parent index.md.

**Message file example:**

```yaml
---
type: message
title: Verify DNS before CMS work
description: Wix→Netlify change propagates Tuesday.
timestamp: 2026-07-04T21:00:00Z
to: any
status: unread
priority: high
expires: 2026-07-10
---
Check dig for materiaverde.com before publishing CMS changes.
If still on Wix nameservers, stop and flag Hiren.
```

## 6. MCP Tool Contracts — descriptions are THE behavior layer for claude.ai; write them verbatim-quality

**`kb_projects()`** → `[{id, title, description, status, last_session, unread_messages}]`
Description must say: *"List all projects in Hiren's knowledge base. Call this when the user asks what they're working on, mentions choosing a project, or at the start of a work session before any project is identified. Cheap — reads only index files."*

**`kb_load(project)`** → `{context_md, index_tree, recent_log: last 3 entries, unread_messages: full bodies, active_concepts: frontmatter only}`
Description: *"Load a project's working context. Call when the user names a project to work on ('load alt', 'let's do hyprlocl work'). Returns state + navigation indexes + unread inter-session messages — NOT concept bodies; fetch those individually with kb_read as needed. Surface unread messages to the user FIRST, then confirm project state in one line. After acting on a message call kb_mark_read."*

**`kb_read(path, depth=0)`** → concept file; `depth=1` adds frontmatter+description of every linked concept (one hop).
Description: *"Read one concept file from the KB. Use paths discovered via kb_load's index tree or kb_search. Use depth=1 when you need to know what a concept's neighbors are before deciding to read them."*

**`kb_write(path, content, message)`** → commit sha. Validates/auto-fills frontmatter; updates parent index.md; rejects reserved names (index.md, log.md) as concept writes.
Description: *"Create or update a concept. Call IMMEDIATELY when something durable is settled in conversation — a decision, spec, runbook, person note — don't batch to session end. Content must be OKF: YAML frontmatter with type, then markdown body. Link related concepts with relative markdown links."*

**`kb_append_log(project, entry)`** → ok. Prepends dated entry; never edits history.
**`kb_leave_message(project, title, body, to="any", priority="normal", expires=null)`** → path.
**`kb_mark_read(message_path)`** → moves to `messages/archive/`, flips status.
**`kb_search(query, project=null, type=null, limit=8)`** → `[{path, title, description, score, matched_heading}]` (v1.1; v1 may ship ripgrep-only under the same contract).

Session-close behavior lives in descriptions too: when the user says "close session" or work clearly wraps, Claude drafts a log entry (what happened, decisions, links to new concepts) and shows it BEFORE calling kb_append_log; updates context.md Open Loops/Next Actions via kb_write; offers kb_leave_message for anything the next session must know.

## 7. Server Implementation Notes

- Extend the existing FastAPI MCP app (same auth pattern as Survey: bearer token, Cloudflare Tunnel in front). Namespace: `kb_`.
- Git ops: shell out to git or use dulwich/pygit2 — builder's choice. Author `helix-bot <helix@metalfinger.xyz>`. Deploy key with write access to the GitHub repo, held server-side only.
- Serialize writes with one asyncio lock; `pull --rebase` before every write so Hiren's direct git/Obsidian edits never get clobbered; on conflict, fail the tool call with a clear message rather than force anything.
- Frontmatter validator: PyYAML; enforce `type`, auto-fill title (from first H1 or filename), description (empty→require in tool arg), timestamp (now, UTC).
- v1.1 embeddings: chunk by markdown heading; deterministic point IDs = hash(path#heading) so rewrites upsert cleanly; nightly full-walk reconcile cron for drift from direct git edits.
- NOT in v1: delete/move tools (do in git directly; nightly reconcile catches it), multi-user auth, cross-file transactions.

## 8. Web Viewing

1. **Now:** GitHub's web UI renders the bundle with working relative links (indexes become nav pages).
2. **v1.3:** Google's reference OKF visualizer (in the knowledge-catalog repo) renders any conformant bundle as a self-contained interactive HTML page with graph view. CI on push → build → deploy to Netlify (e.g. brain.metalfinger.xyz) behind Netlify auth. Works with zero adaptation BECAUSE the bundle is spec-conformant — this is the payoff of OKF discipline.
3. **Someday/content:** custom Three.js force-graph viewer of the bundle = a metalfinger channel video demoing the whole system ("Helix v2 — I gave my AI a memory").

## 9. Build Order + Acceptance Criteria

**v1 (one weekend): repo + core tools.**
Unzip skeleton → private GitHub repo `brain` → implement kb_projects, kb_load, kb_read, kb_write, kb_append_log, kb_leave_message, kb_mark_read (+ ripgrep-backed kb_search stub) → deploy → add connector in Claude settings.
✅ Accept when, in a FRESH claude.ai mobile session with zero context pasted: "which projects am I working on?" → correct list; "load alt" → state summary + any unread message surfaced; a decision made in chat lands as a proper OKF file with updated parent index; "close session" → log entry + context.md updated; a message left `to: claude-code` is surfaced by a subsequent Claude Code session's load.

**v1.1:** Qdrant Cloud embeddings + real kb_search + nightly reconcile. ✅ Semantic query returns the right concept from a project not currently loaded; deleting the collection + reindex script restores search fully.

**v1.2:** Finalize skills/engram/SKILL.md (draft ships with this handoff), install in Claude Code, polish tool descriptions from real-session friction. Add `kb_inbox(text)` quick-capture → `inbox/` for later triage.

**v1.3:** OKF visualizer on Netlify; optional Notion one-way export (Notion = view, never source).

## 10. First Actions for the Building Session

1. Read this file fully, then `SKILL.md`, then unzip `brain-skeleton.zip` and read its root `index.md`.
2. Confirm with Hiren: GitHub repo name/org, embedding model choice for v1.1 (defer OK), whether kb_* extends the Survey app or runs as sibling service.
3. Create the private GitHub repo, push the skeleton, generate + install the deploy key server-side.
4. Build v1 tools against the local checkout; test each with curl/MCP inspector before wiring the connector.
5. Populate `projects/alt/context.md` first (Hiren dictates), then run the v1 acceptance test from a fresh session.
6. Log the build itself as the first entries in `metalfinger/log.md` — it's channel material.

## 11. Sources / References

- OKF spec: `github.com/GoogleCloudPlatform/knowledge-catalog` → `okf/SPEC.md` (v0.1, published 2026-06-12). Reserved filenames: index.md, log.md. Only `type` is spec-required, but Google's reference parser expects type/title/description/timestamp — we match the stricter set.
- Announcement: Google Cloud blog, "How the Open Knowledge Format can improve data sharing."
- Reference visualizer + sample bundles: same repo.
- Prior art pattern: Karpathy's "LLM wiki" gist (April 2026) — OKF formalizes it.
