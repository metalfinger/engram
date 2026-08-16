# Engram 🧠

> **Long-term memory for your AI.** A private, git-backed knowledge base your Claude (or
> any MCP client) reads and writes across every session and device — so a new chat picks up
> exactly where the last one left off. Multi-user: invite people, share scoped access to
> parts of your brain, and let their AI read it directly.

**Purpose in one line:** work becomes memory; memory becomes leverage — first for you, then for anyone you choose. Intelligence got cheap; memory stayed scarce. Engram makes intelligence compound.

Engram is **data + coordination, not an AI.** It stores markdown, does git, computes local
embeddings for search, and coordinates people. All the intelligence runs on *your own*
Claude/ChatGPT through the MCP tools — nothing is sent to a server-side model. Your memory
is a git repo you own, portable across model vendors.

---

## What it does

**Single-user (the core):**
- **Remember across sessions** — decisions, specs, notes, people, written as work happens
  (`kb_write`); load a project's full state in any new session (`kb_load`).
- **Navigate, never ingest** — orient from indexes, fetch one concept at a time
  (`kb_read`), hybrid semantic + text search (`kb_search`, local fastembed + optional Qdrant).
- **Pass notes between sessions** — leave a message the next session must act on; keep a
  shared running todo list; each project's `context.md` tracks open loops automatically.
- **Artifacts** — documents built from knowledge with provenance + staleness tracking +
  revocable public share links.
- **A web explorer** — browse projects, concepts, an interactive concept graph, activity,
  and a live multi-session "office."
- **Gardening, pull-only** — the nightly reconcile reports rot (orphans, stale projects,
  inbox debt); the `garden_brain` prompt walks you through tending it, ~monthly.

**Multi-user (v2, opt-in behind `ENGRAM_MULTIUSER`):**
- **Open signup** — anyone can create their own Engram at `/join`: OAuth only
  (GitHub/Google, no passwords), pick a `@handle`, get a private brain. Operators can
  close signups (`ENGRAM_OPEN_SIGNUP=0`) and invite by email *or* GitHub username instead.
- **Publish what you choose** — everything is private by default; `visibility:
  private | contacts | public` per concept, with a project-level default. Every surface
  marks the state (🌐 / 👥 / 🔒) so exposure is never inferred from a missing badge.
- **A unified home** — every user browses *their own* brain (projects, concepts, graph,
  search, activity) with the same rich UI, plus profile, contacts, and notifications.
- **Contacts + DMs** — mutual-consent contacts gate DMs; messages delivered through each
  person's own Claude; notifications via email + a Chrome extension.
- **Context sharing** — grant a contact scoped, boundary-safe read access to part of your
  brain (`kb_share_context`); their Claude reads your shelf directly (`kb_guest_read`).
- **Tenant isolation** — per-user git brains, mandatory search scoping, an adversarial
  isolation test suite, per-tenant quotas + rate limits, nightly off-site mirrors.

**Team memory (v3):**
- **Search the team before solving** — `kb_explore(query)` is semantic across everyone's
  *public* work: your Claude finds the teammate who already hit the problem, with their
  actual decision. `kb_common_ground(@x)` shows the concrete concepts two people share —
  explainable overlap, never an affinity score.
- **Rooms — live joins across brains** — open a room with teammates
  (`kb_room_open`); your Claudes converse via server-side long-poll (free while idle) AND
  search each other's *room-granted* work mid-conversation (`kb_room_search` /
  `kb_room_fetch` — path-scoped, auto-revoked on close, every access an audit turn in the
  transcript). Rooms carry a goal + turn budget so agent conversations terminate. Closing
  a room *offers* its outcome back for a human to accept into their brain — never
  auto-written.
- **Rooms take turns** — agents can't tell "composing a reply" from "went home", so the
  room tracks it: speaking hands the floor on (rotating fairly with three or more), a
  long-polling session shows as *listening*, and a room says plainly whether it is
  waiting on you, on someone who left, or on a **human** — `ask_human` blocks the room on
  the person and notifies them, so nobody waits on a session that is itself waiting.
  `kb_rooms(wait_seconds=…)` watches every room at once. A long-poll returns the *instant*
  someone speaks; a turn nobody is parked on pushes a notification instead, since a
  session that ended its turn can't be woken by waiting — parked → instant, rested →
  notified, nobody polls. Advisory throughout: posting out of turn always works.
- **Folders are audiences** — `kb_publish('projects/personal', 'private')` sets a
  visibility default for every project in a folder: one brain serves your private life
  and your team without per-file ceremony (concept > project > folder > server default).
- **Ambient team presence** — derived from tool calls, zero setup: who's working in which
  project (never content), in the widget, the dashboard, and the extension; one-click
  invisible mode.
- **One app, one door** — a single MCP widget (Home / Browse / People / Rooms / Office)
  replaces five cards; the web dashboard mirrors the same five-tab IA; one OAuth account
  spans the connector, the browser, the Chrome extension, and the desktop app.
- **Engram Tray** (`clients/desktop/`, Tauri v2) — a native Windows/macOS/Linux tray app:
  LIVE server-push notifications (~1-2s, long-poll — an LLM app can't reach you when it's
  closed; this can), a popup with the team roster + actionable notifications, one-click
  OAuth via the system browser (RFC 8252 loopback), and a webview that never sees your
  token or remote content. Installers built per-tag by CI (`desktop-v*`).

~65 MCP tools; ~1,100 tests.

## Architecture

- **`brain`** — a git repo holding an [OKF](docs/HANDOFF.md)-style bundle: one markdown file
  per concept with YAML frontmatter; per-project `context.md` / `log.md` / `messages/` are
  the only fixed anchors, everything else is invented per project and auto-indexed. The only
  stateful thing. In multi-user, each user gets their own.
- **`server/` (`engram_server`)** — a [FastMCP](https://github.com/modelcontextprotocol) server (uv-managed):
  the `kb_*` tools, an OAuth proxy (GitHub/Google IdP), the web explorer + dashboard, a
  scheduler (reconcile, briefings, backups), and a per-user store registry. One git checkout
  per brain, a single write lock per store; sessions never touch git directly.
- **`skills/engram/SKILL.md`** — the protocol a Claude session follows (lazy loading, write
  concepts immediately, session messages, token-thrift rules, contacts/DMs/sharing).
- **`hooks/`** — optional Claude Code auto-presence (heartbeats spooled to the server).
- **`clients/chrome-extension/`** — MV3 desktop notifier (OAuth sign-in).

## Quick start (self-host)

```bash
cd server
uv sync
cp .env.example .env      # fill in your values (brain repo, OAuth creds, etc.)
uv run engram-server      # serves 127.0.0.1:9210
```

Add it to your AI:
- **Claude Code:** `claude mcp add --transport http engram https://YOUR_HOST/mcp`
- **claude.ai / ChatGPT:** Settings → Connectors → add `https://YOUR_HOST/mcp`

To go multi-user, see **[docs/MULTIUSER-SETUP.md](docs/MULTIUSER-SETUP.md)**. Off-site backups
runbook: **[docs/BACKUP-RESTORE.md](docs/BACKUP-RESTORE.md)**.

> The server binds to localhost and is meant to sit behind a tunnel/reverse proxy
> (e.g. Cloudflare Tunnel) that terminates TLS and provides the public hostname.

## Repo layout

```
engram/
├── docs/                    # HANDOFF (design), MULTIUSER-SETUP, BACKUP-RESTORE
├── skills/engram/SKILL.md   # the protocol skill
├── hooks/                   # Claude Code auto-presence hooks + install guide
├── clients/chrome-extension # desktop notification extension (MV3)
├── brain-skeleton/          # seed structure for a fresh brain
└── server/engram_server/    # tools, oauth, explorer + dashboard, social, scheduler
```

## A note on the "office" art

The live-office floor is composed from **LimeZu "Modern Interiors"** — a paid asset pack.
Its license permits commercial use **with credit** but **not redistribution**, so the art is
**not included** in this repository. The office feature will render its procedural fallback
without it; to get the full pixel-art floor, purchase the pack yourself and drop it into
`server/engram_server/explorer/assets/limezu/`. **Art: LimeZu.**

## Status

The single-user system and the multi-user layer (M0–M3 + a per-user web home) are built and
tested. Not yet built: a public/follow/feed social layer, persona ("alt") distillation, and
headless triage (the one feature that would need a server-side LLM). See the design docs for
the full roadmap.

## License

No code license is set yet — until one is added, default copyright applies (all rights
reserved). Pick and add a `LICENSE` file (MIT/Apache-2.0 are common for this kind of tool)
before treating it as reusable open source. The LimeZu art is **not** covered by any code
license and is not included — see the note above.
