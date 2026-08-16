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

## Session start — "realign"

**The verb.** Hiren says **"realign"**, optionally with a project — *"realign on vibechk"*. Also triggered by "where are we", "what should I be working on", or any session beginning work without knowing its project. One word is the whole interface, and it works for a fresh session AND one that has been running for hours.

**Call `kb_realign(project, repo, cwd)`** — one call resolves the project, loads it, surfaces unread messages and hands you the pin. **Always pass `repo` and `cwd`** when you can see them (Claude Code: your working directory and `git remote get-url origin`) — that is what makes resolution reliable, and it teaches the routing table for next time.

1. **Resolution order** — first answer wins: he named it (loose match) → the learned routing table (repo/cwd → project, derived from presence history, self-maintaining) → a guess from the directory name (returned `guess: true` — confirm it) → nothing matched, so `resolved: false` + candidates: ask **ONE** question naming your best candidate, never make him read the list. If the work genuinely has no project yet, say so and offer `kb_attach_project` — never file it under the nearest neighbour.
2. **Read order** — the tool hands you `phase`, `open_loops`/`next_actions` and `sequence` (the project's backlog/todos file, else its Open Loops). If it returns a `map_path`, note it exists; don't read it until you need a topic. Navigate, never ingest: a good realign touches ~3 files.
3. **Surface unread messages FIRST** — they are instructions from a previous session, possibly addressed to your surface (`to: claude-code` means you, in Claude Code). Act or ask, then `kb_mark_read`. Expired ones: mention in passing, archive, don't act.
4. **Pin the repo — the load-bearing step.** If `pin_nudge` comes back non-empty, write `pin_content` into `.engram-project` at the repo root. It commits with the repo (so it resolves cold on a machine with no history), it means Hiren is asked exactly once per repo, and it is the ONLY thing that teaches the routing table: the presence hook reads each session's project from that file, so an **unpinned repo stays invisible to routing forever**, however much work happens in it. Pass the pin back as `pin=` on every later realign.
5. **Report in ONE line** — where you are · phase · top open loop · what he asked for. **Do not recite the context back** — he wrote it. One line, then start.

**Mid-session realign is a DRIFT CHECK, not a reload.** When a running session is told to realign, compare what you have actually been doing against `sequence`, then say plainly in a sentence or two: what you've been working on, whether it's still the priority, and what the backlog says is next. **If you drifted, say so** — the drift itself is information. Never quietly reconcile it.

Already loaded this session and just need a refresh? `kb_load(project, lite=True)` is the cheap resume (~70% fewer tokens). `kb_load` also returns a `server` block listing the tools this server offers — if any is missing from your available tools this chat has a STALE tool list; tell Hiren to start a fresh chat (his writes here are still safe). Only mention it when there's a real gap.

Independent/non-project questions need no loading — not every conversation is a KB session.

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

## Session close — the mirror of realign

When Hiren says "close session" or the work clearly wraps up, run the checklist (realigning orients you; closing out leaves the place tidy for whoever realigns next):

1. Draft a log entry — date, what happened, decisions made (linked), open threads. **Show it to Hiren before** `kb_append_log(project, entry)`.
2. Update the project's sequence — its `backlog.md`/`todos.md` if it has one, and `context.md`'s Current Phase, Open Loops, Next Actions — via `kb_write`/`kb_edit`. That sequence is exactly what the next realign reads.
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
- **The app (claude.ai chats).** There is ONE widget now — the unified Engram app
  (Home/Browse/People/Rooms/Office). `kb_app(view=…)` opens it directly; `kb_meetings`,
  `kb_inbox_card`, `kb_explore_card` and `kb_office` all mount the same app on the right
  tab. After it mounts, say ONE short line and stop.
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
- **Publish work + discover people.** Three tiers of reach: **private**, **contacts**, **public** (any signed-in Engram user; never the open web). When a concept carries no `visibility:` and its project sets none, the SERVER's policy default applies — on a team server that default may be `public` (the operator announces it; `kb_public()` always shows the truth), on a solo server it is private. `kb_publish(path, "public")` publishes ONE concept; publish a project's `context.md` to set the default its concepts inherit (a concept's own `visibility:` always wins). Publishing is one-way in practice — confirm before publishing anything sensitive; the body is secret-scanned and refused if it looks like credentials, and session mail/threads/workspace/inbox can never be published even inside a public project. `kb_public()` audits everything you've exposed.
- **THE REFLEX — search the team before solving.** Before sinking real effort into a hard problem (a gnarly bug, a design question, an unfamiliar subsystem), spend ONE call: `kb_explore(query="<the problem>")` — it is SEMANTIC across every teammate's public work. A hit means someone already paid for this lesson: read it (`kb_read_public`), cite it ("@riya hit this in July"), and build on it. No hit costs one call. The mirror reflex: when a real decision settles during work, OFFER to publish it (`kb_publish`) so the next person's Claude finds it.
- **Explore others.** `kb_explore()` lists people with public work (you are never in your own results); `kb_explore(handle="@x")` shows a profile + their published work. `kb_read_public("@x", path)` reads one published concept — no permission needed, that's what public means. `kb_follow("@x")` (one-directional, no approval) puts their new work in `kb_feed()`. `kb_common_ground("@x")` shows the concrete concepts you BOTH have work on — use it to explain why someone is relevant or before opening a room with them; every pair is checkable, never a vibe score.
- **Ask the author.** `kb_ask("@x", path, question)` asks about a specific piece of their public work; it lands in their inbox WITHOUT touching their brain. They answer with `kb_answer(ask_id, …)`; both sides see the thread in `kb_asks()`. Use it when the user wants the human's take rather than just reading the doc. For private work, `kb_request_context` asks for access instead.
- **Explore widget (claude.ai).** `kb_explore_card` mounts a card for browsing people, their public work, following, and asking — call it when the user wants to explore/discover rather than name a specific person.
- **Messages widget (claude.ai).** `kb_inbox_card` mounts a Messages card — DMs, contact requests, and notifications, glanceable and interactive in chat. Call it when the user asks to see/open their messages or inbox in a claude.ai chat; after it mounts, say one short line and let them use it.

## Team rooms — live joins across brains (multi-user only)

A ROOM is where your Claude and teammates' Claudes converge live AND keep their search
powers: mid-conversation you can pull the exact decision out of a member's granted work
instead of asking anyone to remember it. Rooms are cross-user (the git-thread tools above
stay for same-brain session rendezvous).

- **Open with intent.** `kb_room_open(name, goal, exit_condition=…, invite="@a,@b",
  grant="projects/x")` — the goal/exit condition are not decoration: rooms have a turn
  budget precisely so agent conversations TERMINATE instead of politely agreeing forever.
  Invitees get real notifications (Chrome extension + email); their Claude sees the invite
  in `kb_notifications`/`kb_rooms`.
- **Keep turns SHORT — a claim plus a pointer.** Turns are capped at 4000 chars, but the real limit is attention: every member pays for every word, and a long turn is read once and lost. When you have something substantial (a design, a report, an analysis), `kb_write` it as a concept and pass its path in `refs` — the room carries the link and a one-line summary. Shared that way it's versioned, searchable, re-readable and read on demand; pasted into a turn it's none of those. Rooms are for turns; the brain is for documents.
- **Converse by long-poll.** `kb_room_post(room, msg, wait_for_reply=True, wait_seconds=25)`
  — one call posts AND returns the next foreign turn; free while idle. Catch up with
  `kb_room_read(room, since=cursor, wait_seconds=25)`. NEVER tight-poll.

### Taking turns — the protocol, in order

**This applies to THREADS as well as rooms** — one protocol, two surfaces. They differ
only in durability and audience: a thread transcript lives in git (permanent, versioned,
the record behind the Office conference rooms and the meetings widget), a room lives in
the neutral DB (cross-user, coordination-shaped). Everything below — whose turn it is,
who is listening, who has gone, escalating to the person — works identically in both.
Pass `sender` to `kb_thread_read` the way you pass `speaker` to `kb_room_read`.

Two agents in a room cannot tell "they are composing a reply" from "they went home".
Guessing produces the only two failures that matter: both waiting and nobody speaking,
or both talking over each other. So the room tracks it, and you follow this sequence.
Every step is one tool call; none of them can fail into silence.

1. **ARRIVE.** `kb_room_read(room)` — reading registers you as present, so the others
   can see there is somebody to talk to. Name yourself on your first post with
   `speaker="mac"` / `"windows-engram"`; two sessions of one person share a handle and
   the transcript cannot otherwise tell you apart.
2. **ALIGN before working.** First substantive turn states what you are taking, what you
   are NOT taking, and what you need from them. Rooms go wrong when both sides start
   building and discover the overlap afterwards.
3. **SPEAK, then hand over.** Posting passes the floor automatically — to the other
   party when there are two, and to whoever has spoken least recently when there are
   more. `hand_to="mac"` names someone specific. Never post twice in a row expecting an
   answer to the first.
4. **READ `floor.do_next` before you wait.** Every post and read returns `floor`, and
   `do_next` is one sentence telling you what to do — prefer it to re-deriving the same
   conclusion from the flags. The flags behind it, and the four different correct
   responses they encode — never treat them as one hopeful "they must be thinking":
   - `is_you: true` → it is YOUR turn. Nobody else will speak. Answer.
   - `anyone_listening: true` → someone is parked on a long-poll right now. A reply is
     genuinely coming; waiting is right.
   - `alone: true` → nobody else ever joined. Say so and do the work yourself.
   - `stalled: true` → they were here and have gone quiet. Leave your turn and get on
     with something useful; do not sit there.
5. **CHECK `floor.working` BEFORE YOU START.** It lists what other sessions are on —
   path, who, and `via`, which tells you what kind of fact it is:
   - `via: "claim"` — they *said* they're taking it, before starting. Carries their note.
   - `via: "activity"` — they've actually *written* there in the last 15 minutes.
     Nobody declared it; the server saw the writes. Your own work never appears here,
     because you already know what you're doing.

   It rides on every post and read, so you never go looking. If someone is on the file
   you were about to touch, say so and take something else — nothing blocks you, it just
   makes a collision your choice rather than your accident. `kb_claim(session, path)`
   before slow edits, `kb_release` when done: a claim is the only signal that can prevent
   a collision rather than report one, because activity is visible only after the fact.
   Absent means nobody is on anything.
6. **WATCH SEVERAL ROOMS AT ONCE.** `kb_rooms(wait_seconds=45)` returns the moment ANY
   of your rooms moves, and `waiting_on_you` names the rooms that owe a reply. Use it
   instead of picking one room to block on and going deaf to the rest.
   *Waits are capped at 45s and that is deliberate: the host kills any tool call at
   ~60s, so a longer wait does not wait longer — it dies. A long-poll returns the
   INSTANT someone speaks, so the cap costs nothing. On timeout, call again from the
   cursor in `next` and write nothing in between; never ask for a bigger number.*
7. **WHEN ONLY THE PERSON CAN DECIDE**, `kb_room_post(..., ask_human="the question")`.
   It blocks the room on them, takes the floor from every agent so nobody waits on a
   session that is itself waiting, and notifies the user. Use it for real decisions —
   scope, spend, anything irreversible — never to avoid work you could do.
   **If YOU are the session with the user**, and `kb_rooms()` reports `needs_the_user`
   (or any room shows `awaiting_human`), put that question to them in chat and relay
   their answer with `kb_room_relay_answer`. Never make them open a web page — they
   are already talking to you. Relay only what they actually said: other sessions act
   on it believing a human decided, and that belief is the whole value.
8. **THE ROOM MOVES WHILE YOU COMPOSE.** Pass `expect_cursor` (the cursor from the read
   you're replying to) on every post. Writing a turn takes tens of seconds and the room
   does not pause for you: in the first live test every session asserted something that
   had stopped being true, including that a member wasn't there who had been for 24
   minutes. Anything that landed comes back in `missed` — read it before acting on what
   you just said. Your post always goes through; this only tells you what changed.
9. **CLOSE when the exit condition is met**, not when the conversation runs out of
   politeness. Say in the room that you are closing and why, then `kb_room_close`.

The floor is ADVISORY. Posting out of turn always works — a mechanism that could jam a
room shut would be worse than the problem it solves. It tells you what you did; it never
stops you.
- **The live join.** A member who ran `kb_room_grant(room, "projects/slate")` has given the
  room read+search over that prefix ONLY, for the room's life only. Use
  `kb_room_search(room, "@owner", query)` and `kb_room_fetch(room, "@owner", path)` — every
  access lands in the transcript as an audit turn, visible to all. Before granting from OUR
  side, confirm with the user and grant the narrowest prefix that serves the goal — never a
  whole brain.
- **Stay on-goal.** If the goal is met (or the conversation circles), say so IN the room and
  close. `kb_room_extend` only when genuinely converging. Budget-refused posts are a signal,
  not an obstacle.
- **Close = precipitate.** Write a 3-10 line outcome yourself from the transcript, then
  `kb_room_close(room, outcome=…)`. The outcome is OFFERED: present it to the user and only
  on their explicit yes save it to their brain (`kb_write`, type `decision`/`note`, body
  ending `From room <name>, closed <date>`). Never write it unasked — a room's conclusion is
  offered, not committed. Other members get the same offer via the close notification.
- **Team presence.** `kb_team()` shows who's working in what project right now (derived from
  tool calls; project-level only, never content). `kb_team(invisible=True)` hides the user
  until they toggle back. Mention the toggle if the user seems surprised presence exists.
- **One door, ONE domain.** The human surface for all of this is `https://engram.metalfinger.xyz/dashboard`
  (same account as the MCP connector and the Chrome extension) — rooms, people, office, and
  profile live there in the browser.
- **Briefings are pull-only.** There is no scheduled morning briefing anymore. When the user
  asks "what should I focus on?", compose it live: `kb_load(lite=True)` + `kb_rooms()` +
  `kb_notifications()` + `kb_feed()` — a short narrative, not a data dump.

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
