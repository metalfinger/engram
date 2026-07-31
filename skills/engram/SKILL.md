---
name: engram
description: Hiren's cross-session knowledge base protocol. Trigger at the start of any work session, when Hiren names a project (engram, metalfinger — kb_projects lists the live set), asks "what are we working on", references past sessions or decisions, or says "close session". Governs how to load project context lazily via OKF, read/write concepts, pass messages between sessions, and close sessions so the next one inherits state.
type: skill
---

# Engram Protocol

Hiren's knowledge base is an OKF v0.1 bundle in the git repo `brain`. ALWAYS prefer the `kb_*` MCP tools (server name: `engram`) — they validate, index, and push for you, on every surface including Claude Code.

If the kb_* tools are missing in a Claude Code session: do NOT search the filesystem for the repo. Tell Hiren to run `/mcp` → engram → Authenticate (or start a fresh session if `engram` isn't listed — servers load at session start). Git-direct is the LAST resort, only when Hiren explicitly asks: the canonical checkout on his main PC is `C:/Users/Admin/.engram/brain` (else clone `git@github.com:metalfinger/brain.git`); same file rules apply — valid frontmatter, clear commits, push, AND update the parent index.md yourself (direct writes bypass the auto-indexer).

## Core principle: navigate, never ingest

Never try to load everything. Orient from indexes, fetch files one at a time as the conversation actually needs them. `kb_load` gives you the map (context + index tree + messages); `kb_read` gives you one territory at a time. A good session on a large brain touches ~5 files.

## Session start

1. If no project is identified yet and work is beginning, call `kb_projects()` and ask Hiren which one (or infer from what he's discussing — he often just starts talking about a client).
2. On "load X" or clear project context: `kb_load(project)` — but if this session already knows the project (resuming, or context was compacted), use `kb_load(project, lite=True)` (context + messages headline only, ~70% fewer tokens). Full load only on first genuine touch.
3. **Every session must be anchored to a project — no floating sessions.** If the repo has no `.engram-project` file at its root, resolve it BEFORE doing project work: call `kb_attach_project()` with no arguments to get the list, ASK Hiren which project this session is on (or a new name + one-line description), then call `kb_attach_project(project, description)` — it creates the project if it's new — and write the returned `pin_content` to `.engram-project` at the repo root. That pins this repo for this and every future session, so he's asked exactly once per repo. Ask rather than guess; only skip when the conversation is clearly not project work at all (a one-off question, not a work session).
4. **Surface unread messages FIRST** — they are instructions from a previous session, possibly addressed to your surface (`to: claude-code` means you, if you're in Claude Code). Act on them or ask, then `kb_mark_read`.
5. `kb_load` returns a `server` block listing the tools this server currently offers. If any tool it names is missing from your available tools, this chat is running a STALE tool list (opened before the last update) — tell Hiren to start a fresh chat to use the newer tools; his writes here are still safe. Only mention this when there's a real gap.
6. Confirm state in ONE line (current phase + top open loop). Do not recite the whole context back.

Independent/non-project questions need no loading — not every conversation is a KB session. Load only when work on a known project begins.

## During work

- **Write immediately, don't batch.** The moment something durable is settled — a decision, spec, runbook, person note — `kb_write` it as a concept. Ask Hiren only if genuinely ambiguous whether it's settled.
- **What goes where:**
  - *Concept* — anything a future session needs standalone. One file, correct `type`, relative links to related concepts.
  - *Log line* — what happened this session (history). Goes in the close-out entry, not written mid-session.
  - *Message* — an instruction the NEXT session must act on ("verify DNS Tuesday before CMS work"). `kb_leave_message`, addressed `to:` a surface if it matters.
  - *Nothing* — conversational back-and-forth, dead ends, chit-chat.
- **Editing, not rewriting.** To change part of an existing concept, use `kb_edit` (append / prepend / find_replace / replace_section / insert) — don't `kb_write` the whole file back. `kb_write` is for new concepts and whole-frontmatter changes; `kb_edit` for surgical body edits.
- **Organizing projects into folders.** `kb_move_project(project, "personal")` files a project into a real directory (`projects/personal/<id>/`) — one project lives in exactly one folder, browsable in git like any folder, and `""` moves it back to the top level. The project ID never changes, so `kb_load`, the `.engram-project` pin and the office keep working, and links across the bundle are re-expressed so they stay correct at the new depth. `kb_project_status(project, "archived")` tucks finished work out of the way (kept, not deleted). The web home and sidebar show the folder tree.
- **Moving/renaming.** `kb_move(old, new)` relocates ONE concept and rewrites every link to it bundle-wide; `kb_rename_project` for a whole project. Never move files by hand — the link rewrite is the point.
- **Superseding.** When a new concept replaces an old one, add `supersedes: <old-path>` to its frontmatter. The server stamps the old one `confidence: superseded`, writes the reverse `superseded_by` edge, and dates it `valid_until`. Don't delete the old decision — supersede it, so the reasoning trail survives (git remembers; the badge shows readers it's history).
- **Backfilling.** `kb_import` turns a ChatGPT/Claude data export into triageable notes under `inbox/imports/` (dry-run first). Use it to seed a brain from past conversations, not just grow forward.
- **Reading:** paths come from the index tree or `kb_search` (hybrid semantic+text, multi-query — phrase it however; add `since:`/`until:` for time-scoped recall). Use `kb_read(path, depth=1)` to see a concept's neighborhood (links AND backlinks AND supersession edges) before going deeper. Ask "is my brain healthy?" → `kb_doctor` for a live round-trip check; the nightly `library/reports/brain-health.md` has the standing findings.
- **Concept file format (validator enforces):** YAML frontmatter — `type` (required; one of: project, client, person, decision, spec, runbook, idea, meeting, video, snippet, reference, message — or a new type if none fit), `title`, `description`, `tags`, `timestamp` (ISO 8601 UTC), plus `status`/`project`/`confidence` where useful. Then a markdown body. Standard relative markdown links only — never wikilinks. Filenames: kebab-case; decisions as `YYYY-MM-slug.md`.
- Never write files named `index.md` or `log.md` as concepts — reserved by OKF. The server maintains indexes; in Claude Code, update the parent index.md yourself when adding a concept.

## Session close

When Hiren says "close session" or the work clearly wraps up, run the checklist:

1. Draft a log entry — date, what happened, decisions made (linked), open threads. **Show it to Hiren before** `kb_append_log(project, entry)`.
2. Update the project's `context.md` — Current Phase, Open Loops, Next Actions — via `kb_write`.
3. Ask: "anything the next session should be told directly?" → `kb_leave_message` if yes.
4. Confirm in one line what was committed.

Offer the close-out proactively; don't let sessions end with unwritten state.

## Judgment calls

- Two-way sync means the KB is live during conversation — if Hiren corrects something ("Arete invoice is actually paid"), update the concept then, not at close.
- `confidence: tentative` on anything Hiren hasn't explicitly confirmed; a later session can promote it to `settled`.
- Messages with `expires` in the past: mention briefly, archive, don't act.
- If a `kb_write` fails on conflict, re-read the file, merge intent manually, retry — never overwrite blind.
- The brain includes `self/` (Hiren's stack and preferences) and `library/` (cross-project runbooks) — search there before reinventing a procedure that likely exists.

## Structure is rules, not schema

Per project, only three anchors are fixed: `context.md` (living state), `log.md`
(journal), `messages/` (session mail) — they are what makes "load X" work
identically everywhere. EVERYTHING else is yours to shape: invent the folders
and `type:` values the work actually needs (research: `sources/`,
`experiments/`; brainstorm: `ideas/`; client: `meetings/`). New folders
auto-index and render first-class in the explorer. Don't force decisions/specs/
people/assets onto a project that doesn't need them — and when a new shape
proves itself, record it in
[organizing-projects](../../library/runbooks/organizing-projects.md) so future
sessions inherit the pattern. Restructuring is allowed too: move/split concepts
when a project outgrows its shape (git mv in Claude Code), and say so in the log.

## Artifacts — documents built from knowledge

When you build a document the user values (report, spec, brief, post), OFFER to save it:
`kb_write` to `projects/<p>/artifacts/YYYY-MM-<slug>.md` with frontmatter `type: artifact`,
`sources:` (the exact concept paths used) and `instruction:` (what it was built to be) —
the server stamps `built_from` provenance. Saved artifacts appear in the Navigator's
Artifacts tab and the explorer gallery, report staleness when their sources change, and
are REUSABLE: read them as sources, or rebuild them from their manifest (the
rebuild_artifact prompt) — saving over the same path keeps versions in git.
`kb_share_artifact` mints a PUBLIC revocable link (warn the user: anyone with the URL
reads that document; sources stay private — the server refuses to share if it detects a
secret unless `allow_secrets=true`). `kb_unshare_artifact` revokes.

**HTML artifacts:** if you built a rich HTML document in the chat side panel, save the
COMPLETE HTML verbatim as the body with `format: html` in the frontmatter — the share link
then serves the real interactive page (not a markdown rendering), and re-saving preserves
the existing share token. **Recipes:** save a reusable build brief as `type: recipe` under
`projects/<p>/recipes/`; `kb_recipes` lists them; re-run one anytime to regenerate a fresh
artifact from current knowledge.

**Widget caveats (surfaces):** the Navigator widget mounts reliably in claude.ai's browser,
less so in the desktop app (host-side iframe limits) — every tool still works as plain
conversation regardless. claude.ai caches a mounted widget's HTML per chat, so after a
server update a NEW chat (or page refresh) is needed to pick it up; the FIRST tool call
right after a server restart may error on a stale session — just retry.

## Multi-session workspace

Many sessions run at once across Hiren's PCs; make yours visible and coordinated. Full guide:
[multi-session-workspace](../../library/runbooks/multi-session-workspace.md).

- **Presence is automatic in Claude Code.** Hooks announce every session (repo, branch,
  remote, cwd, host, `.engram-project` pin) and heartbeat it on each prompt — do NOT call
  `kb_presence` for mere existence. Call it only to set a meaningful `working_on` line, flag
  `status: blocked`, or from claude.ai (no hooks there). TTL ~15 min; SessionEnd marks done.
- **See who's active.** `kb_roster(active_within_min=15)` lists live sessions; `kb_workspace()`
  gives the aggregated view (roster + open rooms + recent handoffs) — use it for "what's running
  across my workspace right now."
- **Collaborate in rooms — long-poll, never tight-poll.** Rooms are threads generalized to N
  parties. To ask another session something: `kb_thread_post(..., wait_for_reply=True,
  wait_seconds=25)` — ONE call that posts and returns the reply the moment it lands. To wait for
  further turns: `kb_thread_read(thread, since=cursor, wait_seconds=25)` in a loop. NEVER poll
  every 2-3s — each poll is a tool call that costs Hiren real tokens; the server-side wait is
  free and just as fast. Always pass the previous `cursor` so old turns aren't re-bought.
  Close with `close=True`. Share code in fenced blocks; share brain concepts/artifacts via
  `refs` on the post rather than re-pasting.
- **Ask Hiren himself from a room.** He watches open threads LIVE in `/brain/office` and can
  reply from the browser. When a collaboration is blocked on his input, post a turn starting
  `@hiren: <question>` with `wait_for_reply=True` — the office flags the room "needs Hiren"
  and his browser reply comes back as your reply turn. Use it for real decisions, not chatter.
- **Widgets (claude.ai chats).** `kb_meetings` mounts the meeting-rooms card — Hiren watches
  live transcripts and replies as himself from desktop or phone. `kb_office` mounts the
  glanceable live-office card (desks/meetings, watch-only). Call them when he asks about
  meetings / the office in a claude.ai chat; after a widget mounts, say ONE short line and stop.
- **Hand off.** `kb_handoff(from, summary, repo, branch, state, next_steps, refs, to)` passes
  unfinished work to a named session (`to="<session>"`) or parks it for whoever picks it up
  (`to=""`), so the next session resumes from the exact state.

## Contacts, DMs & context sharing (multi-user only)

These tools exist only when Engram runs multi-user (other people have accounts). In Hiren's solo brain they're inert — skip this section. Your identity everywhere is your `@handle` (ONE account; the MCP connector, the dashboard, and the Chrome notifier all resolve to it).

- **Surface social activity at session start.** In multi-user, `kb_load` returns a `social` block `{unread_dms, unread_notifications}` — if either is non-zero, say so and offer to show them, the same way you surface unread project messages. `kb_notifications()` lists unread notifications (new DMs, contact requests, share requests/grants); pass `mark_read=True` once you've shown them. `kb_messages()` with no argument lists conversations (each with the other party + unread count); `kb_messages(with_handle="@x")` reads that conversation and marks it read.
- **Contacts gate DMs (anti-spam).** `kb_contacts()` shows your contacts plus incoming/outgoing requests. `kb_add_contact("@x")` sends a request (auto-accepts if they already requested you); `kb_accept_contact("@x")` accepts an incoming one. You can only DM an accepted contact.
- **DM a contact.** `kb_dm("@x", message)` — the recipient gets a notification (and a desktop/push alert if they've set one up). Bodies are secret-scanned; a message containing an apparent key/token is refused.
- **Share knowledge access — the core feature.** Instead of pasting context, grant a contact SCOPED read access to part of your brain so THEIR Claude reads your shelf directly:
  - `kb_share_context("@x", ["projects/alt"], verbs=["read","search"], days=30)` grants directly and notifies them.
  - `kb_request_context("@owner", ["projects/alt"], reason)` asks for access; the owner approves with `kb_grant_request("@requester")` (or denies with `approve=False`).
  - `kb_shared_with_me()` lists what others have shared with you and until when.
  - `kb_guest_read("@owner", path)` reads one shared concept; `kb_guest_search("@owner", query)` searches within the shared paths. Both are STRICTLY scoped to the granted path prefixes — a grant on `projects/alt` never exposes `projects/alt-secret`, a sibling, or a linked-but-unshared neighbour. If a read is refused, the user needs a broader grant (`kb_request_context`).
- **Send a one-off.** `kb_send("@contact", path)` copies ONE concept from your brain into a contact's inbox with `adopted_from` provenance — a one-time copy, not a live grant. Requires being contacts; their Claude finds it in `inbox/imports/` next session.
- **Publish work + discover people.** Three tiers of reach: **private** (the default — absent `visibility:` means private), **contacts**, **public** (any signed-in Engram user; never the open web). `kb_publish(path, "public")` publishes ONE concept; publish a project's `context.md` to set the default its concepts inherit (a concept's own `visibility:` always wins). Publishing is one-way in practice — confirm before publishing anything sensitive; the body is secret-scanned and refused if it looks like credentials, and session mail/threads/workspace/inbox can never be published even inside a public project. `kb_public()` audits everything you've exposed.
- **Explore others.** `kb_explore()` lists people with public work; `kb_explore(handle="@x")` shows a profile + their published work; `kb_explore(query="…")` searches public work. `kb_read_public("@x", path)` reads one of their published concepts — no permission needed, that's what public means — so you can summarize it or answer the user's question from it. `kb_follow("@x")` (one-directional, no approval) puts their new work in `kb_feed()`.
- **Ask the author.** `kb_ask("@x", path, question)` asks about a specific piece of their public work; it lands in their inbox WITHOUT touching their brain. They answer with `kb_answer(ask_id, …)`; both sides see the thread in `kb_asks()`. Use it when the user wants the human's take rather than just reading the doc. For private work, `kb_request_context` asks for access instead.
- **Explore widget (claude.ai).** `kb_explore_card` mounts a card for browsing people, their public work, following, and asking — call it when the user wants to explore/discover rather than name a specific person.
- **Messages widget (claude.ai).** `kb_inbox_card` mounts a Messages card — DMs, contact requests, and notifications, glanceable and interactive in chat. Call it when the user asks to see/open their messages or inbox in a claude.ai chat; after it mounts, say one short line and let them use it.

## Token thrift — every kb_* result lands in Hiren's context and costs his plan

Engram must stay CHEAP to run. Rules, in priority order:

1. **Never tight-poll.** Waiting on another session = `wait_for_reply=True` /
   `wait_seconds` (server-side wait, one tool call). A 2-3s poll loop is the single
   most expensive mistake — dozens of tool results for zero new information.
2. **Cursor discipline.** Always pass `since=<previous cursor>` to `kb_thread_read`;
   re-reading a thread from the top re-buys every old turn.
3. **Load lazily, once.** `kb_load` once per project per session; `lite=True` when
   resuming or after compaction. Never re-load to "refresh" — read the one file you
   need (`kb_read`) instead.
4. **Don't repeat searches.** One well-phrased `kb_search` beats three narrow ones
   (it's already multi-query + hybrid). Reuse results you already have in context.
5. **Presence is free.** Hooks handle it in Claude Code — zero tool calls. Don't
   heartbeat manually.
6. **Write once, edit surgically.** `kb_edit` a section instead of `kb_write`-ing a
   whole file back (the smaller call AND the smaller result). Batch related close-out
   writes into the close checklist, not scattered mid-session writes.
7. **Read what you need.** `depth=1` only when you actually need the neighborhood;
   plain `kb_read` otherwise.

## Writing conventions

- **Link the graph.** Every concept links its related concepts — its decision,
  its spec, the concepts it supersedes. A concept with no outbound links is a
  dead end for `depth=1` navigation: a reader who lands on it cannot discover the
  rest of the story. Use standard relative markdown links, inline in natural prose.
- **Scannable logs.** A log entry is a bare `## YYYY-MM-DD` date heading (no title
  after the date) followed by `* **Title** — 3-6 bullet lines`. Split long prose
  into scannable sub-bullets rather than a paragraph wall, and merge same-date
  entries under one heading.
- **Real timestamps.** A concept's `timestamp:` is its actual write time in ISO
  8601 UTC — never a batch constant copied across a set of files.
