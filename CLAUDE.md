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

## v1.1 (shipped 2026-07-05)
Semantic `kb_search` live: fastembed local embeddings + Qdrant Cloud (`ENGRAM_QDRANT_*` in `.env`, collection `engram-brain`); text scorer = automatic fallback; results carry `engine`. In-process scheduler (lifespan-hooked): nightly reconcile 03:30 (index self-repair, orphan/dead-knowledge → `library/reports/brain-health.md`, full reindex) + 08:00 briefing artifact. `kb_inbox` capture (+ widget box). Interactive graph `/brain/graph`. Live-view share pages. Recipes UX (beta listing via kb_search until a kb_recipes tool exists). Real logging at last. 248 tests. Field notes: qdrant-client ≥1.12 uses `query_points` (`.search()` removed); Qdrant Cloud needs keyword payload indexes on filtered fields. While production runs, use `uv run --no-sync` (venv exe lock).

## Best-of-field wave (shipped 2026-07-08)
Three verified waves adopting the strongest ideas from the competitive scan (Basic Memory, LLM-wiki-v2, agentmemory — full research in the brain's `projects/engram/research/`). New tools: `kb_edit` (surgical ops), `kb_move` (single-concept move + link rewrite), `kb_import` (ChatGPT/Claude export backfill → inbox/imports/), `kb_doctor` (round-trip self-test; also `python -m engram_server.doctor`). New grammar: `supersedes:` frontmatter (auto reverse-edge + confidence:superseded + valid_until, surfaced in explorer/graph/widget). Search overhaul: hybrid RRF fusion + multi-query expansion (measured recall@1 0.81→0.875) + time-window (`since`/`until`) + incremental embed fingerprints. Guards: share secret-scan (allow_secrets override), artifact centroid rebuild-guard, opt-in cross-encoder rerank (`ENGRAM_RERANK_ENABLED`). 338 tests. Spec: `projects/engram/specs/best-of-field-wave.md`.

## Cross-session collaboration + workspace (shipped 2026-07-09)
The "100x workspace" wave — sessions coordinate in real time. **Threads** (`kb_thread_post`/`kb_thread_read`/`kb_threads`): two+ sessions rendezvous by a unique room name (no project needed), post turns, poll ~2-3s (instant same-server reads), `close=True` ends it; live auto-refreshing viewer at `/brain/threads`. **Workspace** (`kb_presence`/`kb_roster`/`kb_handoff`/`kb_workspace`): sessions announce repo/branch/repo_remote/cwd/project/host/status; live mission-control at `/brain/workspace` (roster + rooms + handoffs + claims, auto-refresh). **Collision safety** (`kb_claim`/`kb_release`/`kb_claims` + `base_hash` on kb_write + `hash` on kb_read): advisory claims warn on foreign edits; base_hash is optimistic-concurrency that rejects a lost-update write. Secret-scan on thread posts/handoffs; reconcile prunes stale presence >24h; search excludes threads/+workspace/ ephemera; morning briefing surfaces workspace state. 28 tools. Specs: `projects/engram/specs/workspace-coordination.md`; runbooks: `multi-session-workspace.md` + `cross-session-threads.md`.

## Live Office (rebuilt 2026-07-10 — fixed floor)
`/brain/office` is a FIXED office floor (no dynamic campus): 8 workstations + 2 glass conference rooms + lounge, hand-baked from LimeZu "Modern Interiors" (FULL license purchased 2026-07-10; credit "Art: LimeZu"; assets + bake script under `server/engram_server/explorer/assets/limezu/`, floor layers + seat manifest under `.../limezu/floor/`). Sessions walk in the door, take a hash-stable desk with their project "open" (pill + status-coloured monitor); threads claim the 2 meeting rooms (extras queue); overflow sits in the lounge; office cat naps on the sofa. Hover a character = mini-card; click = dossier (PC/folder/repo/GitHub link/project/first-seen + git-derived timeline). Meeting rooms are Hiren's live thread surface (watch + reply as "hiren" via guarded web endpoints; agents ask him with `@hiren:` + wait_for_reply). Presence is graduated: <=15 min = active, 15m-2h = idle at desk (`quiet:true`, 💤), >2h = recent[] (no character). Heartbeats fire on SessionStart/UserPromptSubmit/PostToolUse/Stop/SessionEnd hooks. 482 tests. KEY LESSON baked into the assets dir: compose rooms OFFLINE from the pack's Theme_Sorter_Black_Shadow_Singles (5,253 pre-cut objects) into back/front layers + JSON manifest — never assemble furniture tile-by-tile at runtime.

## Auto-presence (shipped 2026-07-09)
Sessions now announce themselves to the roster automatically — no `kb_presence` call. **Spool pattern** (the only lock-safe way): Claude Code hooks (`hooks/` in this repo, installed to `~/.claude/hooks/` + wired into settings.json SessionStart/UserPromptSubmit/SessionEnd) do a bare JSON file write to `~/.engram/presence-spool/<session>.json` (git-detected repo/branch/remote/host, status working→done on SessionEnd) — they NEVER touch the checkout. The server ingests the spool on a ~30s scheduler tick (`engram_server/presence_spool.py` → `kbstore._presence_write_batch`), upserting through its single write lock in ONE batched commit, deduped/throttled (re-commit only on a meaningful field change OR record >`presence_refresh_minutes` stale). No second git writer ever exists. Config (`ENGRAM_` prefix): `presence_spool_dir`, `presence_ingest_seconds=30`, `presence_refresh_minutes=5`. 440 tests. Decision + build: `decisions/2026-07-auto-presence-spool.md`; per-PC install: `hooks/README.md`.

## v2 direction (documented, not started)
Social brains — federated knowledge, AI-mediated (public shelves, guest MCP, cross-brain adoption, agent-to-agent Q&A). The vision + reserved conventions live in the brain: `projects/engram/ideas/2026-07-social-brains.md`. Do not start before the personal-tightening backlog below is done and Hiren says go.

## MCP Apps wave 2 (shipped 2026-07-10)
Spec-compliance pass (ext-apps 2026-01-26; explicit visibility + teardown ack + shape-pin tests) + `kb_meetings` (live thread transcripts + reply-as-Hiren from any claude.ai chat incl. mobile; app-only data tools = zero context cost) + `kb_office` (glanceable office card, floor art via resources/read). Spec: `projects/engram/specs/mcp-apps-wave-2.md` in the brain. 565 tests, 30 model-visible tools (+4 app-only).

## What's next
1. v1.2 remainder — skill polish from real-session friction (kb_inbox already shipped).
2. Fresh claude.ai MOBILE session run-through (last acceptance nicety).
3. Recipes v2 — real `kb_recipes` tool; scheduled recipe rebuilds when headless LLM runs exist.
4. Netlify OKF visualizer — largely superseded by `/brain/graph`; decide keep-or-drop.
5. Code repo: `github.com/metalfinger/engram` (private).

## Roadmap after v1.x (Hiren, 2026-07-04)
Build rich MCP Apps (SEP-1865 widgets) for Engram like the Survey MCP's in `D:\Projects\LLM-Communication` (avatar/gallery/runner widgets are the reference pattern). Phase step 1: a study pass over the Survey implementation (widget.py + *_widget.py + tool `meta=` wiring, plus its hard-won rails in tool descriptions) to extract the reusable pattern and lessons. Then run a dedicated brainstorm on which widgets earn their keep (candidates to seed it: project switchboard card, message inbox card, session-close checklist card, brain graph view). Don't start this before v1 acceptance + v1.1 search are done.

## Conventions
- Python server: official `mcp` SDK FastMCP style; run tests from `server/` with `uv run pytest tests/ -q`
- Git author for server-made brain commits: `helix-bot <helix@metalfinger.xyz>`; direct/manual brain edits use Hiren's identity
- Commit prefixes: `feat:`, `fix:`, `docs:`, `build:`, `test:`, `chore:`
- Never put tokens/keys in this repo (`.env` and `.mcp.json` are gitignored; deploy key + CF token live in `~/.engram/`)
- After changing server code: restart via `scripts/start-engram.ps1` (kill PID on 9210 first); the three SKILL.md copies must stay byte-identical
