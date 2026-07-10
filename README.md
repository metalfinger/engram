# ENGRAM 🧠

> The durable trace your AI sessions leave behind. A cross-session knowledge ecosystem:
> OKF-formatted knowledge base in git + 28 `kb_*` MCP tools + a Claude skill + a live web
> explorer — so any session on any surface (Claude Code, claude.ai, mobile, ChatGPT) picks
> up exactly where the last one left off, and sessions can see and talk to each other.

## The pieces

1. **`brain`** — a private git repo ([metalfinger/brain](https://github.com/metalfinger/brain)) holding an OKF v0.1 bundle: one markdown file per concept, YAML frontmatter, per-project `context.md` / `log.md` / `messages/`. The only stateful thing in the system. Engine philosophy: **rules over schema** — three anchors per project, every other shape is invented per project and auto-indexed.
2. **`engram_server`** (`server/`) — FastMCP server (uv-managed, 480+ tests) on port 9210, tunneled as:
   - `engram.metalfinger.xyz/mcp` — 28 `kb_*` tools + OAuth (GitHub allowlist)
   - `brain.metalfinger.xyz` — explorer (Cloudflare Access): projects, decisions, artifacts gallery, graph, threads, workspace, **live office**
   The server owns one git checkout; a single write lock serializes every mutation (pull-rebase → mutate → commit → push). Sessions never touch git.
3. **`skills/engram/SKILL.md`** — the protocol: lazy loading, write-immediately concepts, session messages, close checklist, **token-thrift rules**, long-poll collaboration. Three synced copies (repo, brain, `~/.claude/skills`); self-updates on other PCs via the `kb_load` server manifest.
4. **`hooks/`** — auto-presence for Claude Code: hooks spool plain JSON heartbeats (SessionStart / UserPromptSubmit / PostToolUse / Stop / SessionEnd); the server ingests through its own lock. Pin a repo to a project with a one-line `.engram-project` file. See `hooks/README.md`.

## What it does (v1.x, all live)

- **Knowledge of record** — decisions, specs, runbooks, people, written as the work happens; hybrid semantic search (fastembed + Qdrant, RRF fusion); nightly self-healing reconcile + morning briefing.
- **Artifacts** — documents built from knowledge with provenance manifests, staleness tracking, revocable public share links, HTML served verbatim.
- **Cross-session collaboration** — threads/rooms (`kb_thread_post` with **server-side long-poll** `wait_for_reply`), presence roster, handoffs, advisory claims + `base_hash` optimistic concurrency. Agents can ask Hiren directly (`@hiren:` + wait) — he replies from the office in the browser.
- **The Live Office** (`/brain/office`) — a fixed pixel-art office floor hand-baked from LimeZu "Modern Interiors" (full license): 8 workstations, 2 conference rooms, lounge, office cat. Sessions walk in and take a desk with their project "open"; threads meet in the glass rooms; click a meeting to watch live and reply as Hiren; hover/click characters for dossiers (PC, folder, repo, GitHub link, timeline). Graduated presence: active → idle-at-desk (💤) → gone.
- **Brain Navigator** — MCP App widget (SEP-1865 / MCP Apps) mounting inside claude.ai.

## Repo layout

```
engram/
├── docs/HANDOFF.md          # original build spec (historical)
├── skills/engram/SKILL.md   # the protocol skill (3 synced copies)
├── hooks/                   # Claude Code auto-presence hooks + install guide
├── brain-skeleton/          # seed structure (historical)
└── server/                  # engram_server: tools, oauth, explorer, office, scheduler
    └── engram_server/explorer/assets/limezu/   # licensed art + baked office floor (+ bake script)
```

## Operating notes

- Restart after server changes: `scripts/start-engram.ps1` (kill PID on 9210 first); while production runs use `uv run --no-sync`.
- Server-made brain commits author as `helix-bot`; never commit tokens (deploy key + CF config live in `~/.engram/`).
- The brain documents Engram itself: read `projects/engram/` there (context, decisions, specs) for the full story.

## Next

- ~~MCP Apps wave 2~~ SHIPPED 2026-07-10: compliance pass + `kb_meetings` (reply to threads from mobile) + `kb_office` (live-office card). Phase 2: LimeZu sprites in the office widget.
- v2 (gated): social brains — federated knowledge, AI-mediated. See `projects/engram/ideas/2026-07-social-brains.md`.
