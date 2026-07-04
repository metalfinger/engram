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
├── docs/HANDOFF.md        # full build spec: intent, decisions + rationale, tool contracts, build order
├── skills/engram/SKILL.md # the protocol skill (install into ~/.claude/skills; also lives inside brain)
├── brain-skeleton/brain/  # OKF bundle skeleton → becomes the private `brain` repo
└── server/                # kb_* MCP implementation (to be built)
```

## Build order (from docs/HANDOFF.md §9)

- **v1** — brain repo on GitHub (private) + 7 core tools: `kb_projects`, `kb_load`, `kb_read`, `kb_write`, `kb_append_log`, `kb_leave_message`, `kb_mark_read` (+ ripgrep `kb_search` stub). Accept: a fresh mobile session answers "which projects am I working on?" with zero pasted context.
- **v1.1** — Qdrant Cloud embeddings, real `kb_search`, nightly reconcile.
- **v1.2** — skill polish, `kb_inbox` quick-capture.
- **v1.3** — OKF visualizer on Netlify (brain.metalfinger.xyz).
