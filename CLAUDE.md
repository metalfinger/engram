# Engram — Project Instructions

Cross-session knowledge ecosystem for Hiren: OKF v0.1 bundle (`brain`) in git + `kb_*` MCP tools + the `engram` Claude skill + private web explorer. **v1 is LIVE (2026-07-04).**

## Current state — v1 deployed and accepted in the field

- Brain repo: `github.com/metalfinger/brain` (private). Server checkout + deploy key at `C:\Users\Admin\.engram\`.
- Server: `server/` here (package `engram_server`, uv-managed, 152 tests). Runs natively on this PC, port 9210, autostarts via HKCU Run; tunneled by the existing `my-pc` cloudflared as:
  - `https://engram.metalfinger.xyz` — /mcp + OAuth (GitHub sign-in, allowlist `metalfinger`)
  - `https://brain.metalfinger.xyz` — explorer (Cloudflare Access, email OTP) with `/brain/setup` onboarding for claude.ai / Claude Code / ChatGPT / Codex / any MCP client
- Engine philosophy (settled): **rules over schema** — per project only `context.md` + `log.md` + `messages/` are anchors; every other folder/type is invented per project, auto-indexed, rendered first-class by the explorer. `kb_write` warns on link-less concepts; logs are bare-ISO-date bullets.
- Cross-surface message loop proven (claude.ai session archived a Claude Code message, git receipts).
- The brain documents Engram itself: `projects/engram/` (context, 10 decisions with rationale, v1 architecture spec, key references) — **written entirely through the kb_* tools**. Read THAT for system knowledge before this file's history.

## Read for background (historical)
1. `docs/HANDOFF.md` — the original build spec ("Helix KB" = Engram; "bearer token" was superseded by the OAuth proxy reality). Do not treat its build order as pending — it shipped.
2. `skills/engram/SKILL.md` — the protocol (synced copies: brain repo + `~/.claude/skills/engram/`; keep all three identical).

## Boundary
Engram is deliberately EXTERNAL to the helix repo: brain = decisions/specs/context/messages (knowledge of record); helix-memory = tasks/deadlines/world-state (operational).

## Brain Navigator (shipped 2026-07-05)
The flagship MCP App widget (`engram_server/navigator.py`, flag `ENGRAM_WIDGET=1` in `.env`): Home/Browse/Search/Inbox/Artifacts views + select-to-artifact basket, mounting live on claude.ai. Before ANY future widget work read the field notes in the brain: `projects/engram/specs/brain-navigator.md` (SDK `{result:...}` envelope, per-session widget HTML caching, appInfo/jsonrpc rails).

## Artifact system (shipped 2026-07-05)
Artifacts = `type: artifact` concepts under `projects/<p>/artifacts/` with provenance manifests (`sources`, `instruction`, server-stamped `built_from`), computed staleness, revocable public `/share/<token>` links (Cloudflare Access edge-bypass app covers `/share/*` — created via API), explorer gallery at `/brain/artifacts`, Navigator Artifacts tab, and four MCP prompts (`daily_briefing`, `close_session`, `build_artifact`, `rebuild_artifact` — the manifest is a recipe; saving over the same path = git-versioned living documents). Spec + field notes: `projects/engram/specs/artifact-system.md` in the brain. 206 tests. Field-tested through the live connector (first shared artifact: the Engram one-pager).

## What's next
1. **v1.1** — Qdrant Cloud embeddings + real semantic `kb_search` (same contract as the shipped text scorer) + nightly reconcile walk; harden lock-free reads vs concurrent writes (known Windows race, low probability).
2. **v1.2** — skill polish from real-session friction; `kb_inbox` quick-capture.
3. **v1.3** — OKF visualizer on Netlify (bundle verified conformant; two nits already fixed).
4. Remaining acceptance nicety: a fresh claude.ai MOBILE session run-through.
5. Open loop: push this code repo to GitHub (undecided — see `projects/engram/context.md` in the brain).

## Roadmap after v1.x (Hiren, 2026-07-04)
Build rich MCP Apps (SEP-1865 widgets) for Engram like the Survey MCP's in `D:\Projects\LLM-Communication` (avatar/gallery/runner widgets are the reference pattern). Phase step 1: a study pass over the Survey implementation (widget.py + *_widget.py + tool `meta=` wiring, plus its hard-won rails in tool descriptions) to extract the reusable pattern and lessons. Then run a dedicated brainstorm on which widgets earn their keep (candidates to seed it: project switchboard card, message inbox card, session-close checklist card, brain graph view). Don't start this before v1 acceptance + v1.1 search are done.

## Conventions
- Python server: official `mcp` SDK FastMCP style; run tests from `server/` with `uv run pytest tests/ -q`
- Git author for server-made brain commits: `helix-bot <helix@metalfinger.xyz>`; direct/manual brain edits use Hiren's identity
- Commit prefixes: `feat:`, `fix:`, `docs:`, `build:`, `test:`, `chore:`
- Never put tokens/keys in this repo (`.env` and `.mcp.json` are gitignored; deploy key + CF token live in `~/.engram/`)
- After changing server code: restart via `scripts/start-engram.ps1` (kill PID on 9210 first); the three SKILL.md copies must stay byte-identical
