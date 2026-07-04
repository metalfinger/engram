# ENGRAM 🧠

> The durable trace your AI sessions leave behind. A cross-session knowledge ecosystem: OKF-formatted knowledge base in git + MCP tools + a Claude skill, so any session on any surface (claude.ai, mobile, Claude Code) picks up exactly where the last one left off.

## What is Engram?

Three parts:

1. **`brain`** — a private git repo holding an [OKF v0.1](https://github.com/GoogleCloudPlatform/knowledge-catalog) bundle: one markdown file per concept, YAML frontmatter, `index.md` navigation, per-project `context.md` / `log.md` / `messages/`. The ONLY stateful thing in the system. (`brain-skeleton/` here is the starting structure — it gets pushed to its own private repo.)
2. **`kb_*` MCP tools** — served from the existing FastAPI MCP server at `mcp.metalfinger.xyz`. The server holds one local checkout of `brain`; every read returns one file, every write is one file + server-side commit/push. Sessions never touch git.
3. **`skills/engram/SKILL.md`** — the protocol: lazy loading (navigate, never ingest), write-immediately concepts, session-to-session messages, and a session-close checklist.

## Why not inside Helix?

[Helix](../helix) is the open-source session dashboard + local memory server. Engram is the knowledge-of-record layer — private data, cloud transport, every surface. Boundary:

- **Engram (brain)** = project context, decisions, specs, people, cross-session messages
- **Helix memory** = local operational layer: tasks, deadlines, world-state briefing, TUI telemetry

## Repo layout

```
engram/
├── docs/HANDOFF.md        # original build spec (historical): intent, decisions + rationale, contracts
├── skills/engram/SKILL.md # the protocol skill (synced: here, inside brain, ~/.claude/skills)
├── brain-skeleton/brain/  # the skeleton that seeded the private `brain` repo (historical)
└── server/                # engram_server — SHIPPED: 8 kb_* tools + OAuth allowlist + explorer, 152 tests
```

## Status

- **v1 — LIVE (2026-07-04).** `github.com/metalfinger/brain` + all 8 tools + explorer, deployed on the `my-pc` Cloudflare tunnel: `engram.metalfinger.xyz` (MCP, GitHub OAuth allowlisted) + `brain.metalfinger.xyz` (explorer, Cloudflare Access; `/brain/setup` onboards any MCP client). Cross-surface message loop proven. Engine philosophy: rules over schema — three anchors per project, every other shape LLM-invented, auto-indexed, first-class in the UI.
- **v1.1 (next)** — Qdrant Cloud embeddings behind the same `kb_search` contract, nightly reconcile, read/write race hardening.
- **v1.2** — skill polish, `kb_inbox` quick-capture.
- **v1.3** — OKF visualizer on Netlify (bundle audited conformant).
- **Later** — MCP App widgets (study the Survey implementation first; see CLAUDE.md roadmap).
