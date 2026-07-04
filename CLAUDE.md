# Engram — Project Instructions

Cross-session knowledge ecosystem for Hiren: OKF v0.1 bundle (`brain`) in git + `kb_*` MCP tools + the `engram` Claude skill.

## Read first
1. `docs/HANDOFF.md` — the master build spec. Intent, every decision already made WITH rationale (do not relitigate without new information), tool contracts (descriptions are the behavior layer for claude.ai), server notes, build order + acceptance criteria.
2. `skills/engram/SKILL.md` — the protocol the skill teaches.

## Naming note
The handoff was written before this project was named. "Helix KB" / "helix-kb" in `docs/HANDOFF.md` = **Engram**. The skill is named `engram` (already renamed everywhere except historical prose in the handoff). Engram is deliberately EXTERNAL to the helix repo (open-source dashboard/memory) — boundary: brain = decisions/specs/people/context/messages; helix-memory = tasks/deadlines/world-state.

## Already done (skip these handoff steps)
- Skeleton extracted → `brain-skeleton/brain/` (65 files, committed)
- Skill renamed helix-kb → engram, indexes updated
- This repo initialized on `main`, initial commit `5587b89`

## Open decisions — confirm with Hiren before building
1. GitHub org/name for the private `brain` repo (suggest `metalfinger/brain`)
2. Server shape: extend the existing Survey FastAPI MCP app at `mcp.metalfinger.xyz`, or sibling service built in `server/` here (same infra/auth pattern either way)
3. Embedding model for v1.1 (deferrable)

## Next actions (v1, handoff §9–10)
1. Create private GitHub repo `brain`, push `brain-skeleton/brain/` contents as its root
2. Build the 7 v1 tools against a server-side checkout: `kb_projects`, `kb_load`, `kb_read`, `kb_write`, `kb_append_log`, `kb_leave_message`, `kb_mark_read` (+ ripgrep `kb_search` stub)
3. Deploy (Docker + Cloudflare Tunnel, bearer token — same pattern as Survey), generate deploy key server-side
4. Add connector in Claude settings; install `skills/engram/` into `~/.claude/skills/`
5. Populate `projects/alt/context.md` (Hiren dictates), then run the v1 acceptance test from a fresh claude.ai mobile session
6. Log the build in `metalfinger/log.md` — it's channel material

## Roadmap after v1.x (Hiren, 2026-07-04)
Build rich MCP Apps (SEP-1865 widgets) for Engram like the Survey MCP's in `D:\Projects\LLM-Communication` (avatar/gallery/runner widgets are the reference pattern). Phase step 1: a study pass over the Survey implementation (widget.py + *_widget.py + tool `meta=` wiring, plus its hard-won rails in tool descriptions) to extract the reusable pattern and lessons. Then run a dedicated brainstorm on which widgets earn their keep (candidates to seed it: project switchboard card, message inbox card, session-close checklist card, brain graph view). Don't start this before v1 acceptance + v1.1 search are done.

## Conventions
- Python server: FastAPI + FastMCP style, matches Hiren's existing stack
- Git author for server commits: `helix-bot <helix@metalfinger.xyz>` (keep — infra-level name)
- Commit prefixes: `feat:`, `fix:`, `docs:`, `build:`, `test:`, `chore:`
- Never put tokens/keys in this repo; deploy key lives server-side only
