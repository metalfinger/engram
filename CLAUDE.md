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

## v2 multi-user — BUILT (2026-07-20), gated OFF by `ENGRAM_MULTIUSER`
The whole social/multi-tenant vision shipped as code, dormant until the operator flips
`ENGRAM_MULTIUSER=1` (single-user is byte-identical with it off). Milestones: **M0** tenant
engine (per-user brains under `~/.engram/users/<handle>/`, `registry` store map,
`current_store()`/`current_user()` seams, Qdrant `user_id` isolation, adversarial
`test_isolation_harness.py`, quotas, nightly off-site mirror), **M1** front door (accounts
replace the allowlist, email + **GitHub-username** invites, homepage, dashboard +
magic-link onboarding — OAuth only, no passwords; owner auto-admin via `owner_subjects`),
**M2** social (contacts/DMs/notifications in the NEUTRAL `engram.db`, never in a brain;
fanout email/Chrome), **M3** context sharing (capability tokens + `kb_guest_read`/
`kb_guest_search`, boundary-safe `covers_path`, `kb_send`). Plus: Chrome notifier extension
(`clients/chrome-extension/`, OAuth sign-in), profile avatars, Messages widget
(`kb_inbox_card`), dashboard social panel. Architecture principle: Engram is DATA +
COORDINATION only — **no server-side LLM**; all intelligence runs on the user's own Claude
(`decisions/2026-07-no-server-llm.md`). Runbook: `docs/MULTIUSER-SETUP.md`. To go live the
operator must: set `ENGRAM_MULTIUSER=1` + a 32-char `ENGRAM_DASHBOARD_SESSION_SECRET`,
register `/dashboard/callback` in the GitHub+Google OAuth apps, set `ENGRAM_BACKUP_REMOTE`
(hard gate), restart. 867 tests. Future (NOT built): per-user explorer at `brain.*` (M4),
persona/"alt" distillation, headless triage (the only thing needing a cloud LLM).

## v2 social-brains vision (documented, future)
Federated knowledge, AI-mediated (public shelves, guest MCP, cross-brain adoption,
agent-to-agent Q&A). Vision + the multi-user build plan live in the brain:
`projects/engram/ideas/2026-07-social-brains.md` + `specs/v2-build-plan.md`.

## MCP Apps wave 2 (shipped 2026-07-10)
Spec-compliance pass (ext-apps 2026-01-26; explicit visibility + teardown ack + shape-pin tests) + `kb_meetings` (live thread transcripts + reply-as-Hiren from any claude.ai chat incl. mobile; app-only data tools = zero context cost) + `kb_office` (glanceable office card, floor art via resources/read). Spec: `projects/engram/specs/mcp-apps-wave-2.md` in the brain. 565 tests, 30 model-visible tools (+4 app-only).

## v3 team memory + unified app — BUILT (2026-08-01)
PRD (APPROVED, the steering doc): `projects/personal/engram/specs/v3-team-memory-and-unified-app.md`
in the brain. Core frame: **two timescales, one substrate** — async (search/read/ask about
past work) + sync (**rooms** = live joins across brains) feeding each other via precipitate.
Shipped:
- **One Door**: explorer guard = dashboard session cookie (owner-only /brain/*; teammates →
  /dashboard); CF Access verifier kept only as secret-less fallback. Human surface =
  `brain.metalfinger.xyz/dashboard`; `engram.` stays the connector origin. CF Access edge
  app deletion is the LAST deploy step (order matters — it's what protects /brain/* until
  the guard is live).
- **Rooms** (`teamwork.py` + 12 `kb_room_*`/`kb_team` tools): neutral-DB rooms with goal +
  turn_budget + hard_cap (anti-money-fire), room-scoped path grants (auto-revoke on close,
  every guest access an audit turn), server-side long-poll bus (`room_notify`/`room_wait` —
  shared by MCP tools and the dashboard reply form), close-with-precipitate (OFFERED, never
  auto-written). Invites ride the existing notification fanout.
- **Presence from tool calls**: `_touch_presence` in `current_store()` (60s throttle);
  project attribution from kb_load/kb_attach_project; invisible mode; `team[]` in
  office.json; roster in widget People tab + extension + `/dashboard/office`.
- **The save**: `visibility` stamped into Qdrant payloads (missing = never public);
  `kb_explore(query)` = semantic cross-user over public work (self excluded);
  `kb_common_ground` = explainable work-overlap pairs. SKILL.md carries the REFLEX
  (search team before solving; offer publish on decision).
- **Unified app**: `app_widget.py` (`ui://engram/app`, 88k chars) — Home/Browse/People/
  Rooms/Office; ALL launchers re-pointed with view hints; old widget resources still
  served for stale chats; app-only plane rooms_state/room_transcript/room_reply/team_state.
- **Dashboard v3**: five-tab web IA, rooms pages (reply as signed-in user), /dashboard/api/
  team + /api/presence, /dashboard/office|artifacts|setup|ops, avatar upload (data:image
  ≤100k; tenancy backstop blocks hostile schemes).
- **Extension 2.0**: OAuth via chrome.identity (token pasting REMOVED), team roster +
  invisible toggle, room-invite deep links.
- **Policy**: `ENGRAM_DEFAULT_VISIBILITY` (env; team test runs public BY CONSENT — never
  retroactive, imports always private, _never_public segments immutable). Scheduled
  morning briefing RETIRED (`briefing_at` default '') — briefing is pull-only.
- Homepage rewritten as the teammate front door (visibility copy tracks the policy knob).

## Engram Tray + live push + ONE domain/web (post-v3 field day, 2026-08-01)
Field-driven wave with Hiren testing live:
- **ONE DOMAIN**: engram.metalfinger.xyz is everything (brain.* removed from tunnel
  ingress + code; no redirects). **ONE WEB APP**: explorer HTML pages are NOT
  registered in multiuser (invariant-tested: explorer contributes only /share/*);
  single-user self-hosts keep the explorer. Dashboard = 5 tabs + sub-tabs + avatar
  account menu; form-styling layer added (explorer CSS had no form styles at all).
- **Engram Tray** (`clients/desktop/`, Tauri v2, Rust, no webview content): tray
  icon + popup (local HTML over IPC only — webview never sees token/remote), team
  roster w/ avatar pipeline (SSRF-hardened: https-only, public-IP-only, DNS pinned
  via resolve_to_addrs, no redirects), native toasts (field-verified on Windows),
  loopback OAuth (nonce path, RFC 8252 — server allowlist in _valid_ext_redirect).
  CI matrix on tag `desktop-v*` builds .msi/.dmg(universal)/.AppImage to a GitHub
  Release. Versions: 0.1 menu-only -> 0.2 popup -> 0.3 LIVE push.
- **Live notification delivery**: `pushbus.py` per-user wake bus; SocialStore.
  create_notification wakes it (single funnel); /dashboard/api/notifications
  ?wait&since parks until newer-than-cursor (backlog parks too). Tray latency
  ~1-2s for in-process writers; external scripts land at cycle end. Per-id
  mark-read (`{"ids":[..]}`) — acting on a notification consumes it.
- **Turn attribution**: room turns carry via (human vs their Claude, 🤖 chip) —
  session channel: MCP=claude, dashboard:web/app composer=human.
- Extension downloadable from the product (/downloads/engram-chrome-extension.zip).

## Room floor control (shipped 2026-08-16)
Rooms now carry **whose turn it is**, because two agents could not tell "composing a
reply" from "went home" — producing both deadlock (each waiting) and talk-over. Speaking
hands the floor on (two parties → the other; 3+ → fair rotation to the least-recently
spoken, skipping sessions gone >30 min); `kb_room_read` registers you as present and, on
long-poll, as *listening*, so a talker can see a reply is genuinely coming. `floor`
distinguishes four states agents used to conflate: `is_you` / `anyone_listening` /
`alone` (nobody ever joined) / `stalled` (they left). `ask_human=` blocks a room on the
PERSON, strips the floor from every agent, and pushes a notification — so nobody waits on
a session that is itself waiting; their reply hands the floor back to the asker.
`kb_rooms(wait_seconds=…)` long-polls ALL your rooms at once (`waiting_on_you`,
`woke_on`). Advisory throughout — posting out of turn always succeeds.
**Applied from the PARK pattern** (`projects/mcp-explorations/park-pattern-playbook.md`
— the brain had the whole recreate-from-scratch guide, so the vibechk repo was never
needed): the host hard-kills tool calls at ~60s, so every MCP-side wait is clamped to
45s and a timeout returns a `next` cursor ("call again, write nothing between") instead
of tempting a longer wait; listening freshness refreshes at poll ENTRY only. Its Durable
Object machinery is deliberately NOT ported — that exists because Workers have no shared
memory; one process + one asyncio Condition already is the rendezvous. The `delivered`
verdict IS ported: a turn nobody is parked on pushes a notification, because a session
that ended its turn cannot be woken by waiting — parked → instant, rested → notified,
nobody polls.
**The bug underneath it all:** `wait_for_reply` filtered replies by `user_id`, so two
sessions of ONE person (Hiren's actual setup) could never see each other and every wait
timed out empty. Turns now carry a per-MCP-session `speaker` key so one handle can be two
voices. Protocol (arrive → align → speak+hand over → read the floor → close) is in
SKILL.md. 39 room tests.

## What's next
1. Wave 0 gates (Hiren): `ENGRAM_BACKUP_REMOTE` (hard gate), set `ENGRAM_DEFAULT_VISIBILITY=public`, seed ~15 decisions.
2. Team onboarding (10 people at Alt Inc) — measure the §10 PRD numbers at two weeks.
3. Full `/brain/*` deletion once dashboard reaches 100% parity (office canvas, workspace, system).
4. Wave 7 co-writing (shared projects) — needs its own decision first.
5. Code repo: `github.com/metalfinger/engram` (public).

## Roadmap after v1.x (Hiren, 2026-07-04)
Build rich MCP Apps (SEP-1865 widgets) for Engram like the Survey MCP's in `D:\Projects\LLM-Communication` (avatar/gallery/runner widgets are the reference pattern). Phase step 1: a study pass over the Survey implementation (widget.py + *_widget.py + tool `meta=` wiring, plus its hard-won rails in tool descriptions) to extract the reusable pattern and lessons. Then run a dedicated brainstorm on which widgets earn their keep (candidates to seed it: project switchboard card, message inbox card, session-close checklist card, brain graph view). Don't start this before v1 acceptance + v1.1 search are done.

## Conventions
- Python server: official `mcp` SDK FastMCP style; run tests from `server/` with `uv run pytest tests/ -q`
- Git author for server-made brain commits: `helix-bot <helix@metalfinger.xyz>`; direct/manual brain edits use Hiren's identity
- Commit prefixes: `feat:`, `fix:`, `docs:`, `build:`, `test:`, `chore:`
- Never put tokens/keys in this repo (`.env` and `.mcp.json` are gitignored; deploy key + CF token live in `~/.engram/`)
- After changing server code: restart via `scripts/start-engram.ps1` (kill PID on 9210 first); the three SKILL.md copies must stay byte-identical
