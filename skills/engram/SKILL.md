---
name: engram
description: Hiren's cross-session knowledge base protocol. Trigger at the start of any work session, when Hiren names a project (engram, metalfinger — kb_projects lists the live set), asks "what are we working on", references past sessions or decisions, or says "close session". Governs how to load project context lazily via OKF, read/write concepts, pass messages between sessions, and close sessions so the next one inherits state.
type: skill
---

# Engram Protocol

Hiren's knowledge base is an OKF v0.1 bundle in the git repo `brain`. Access it through the `kb_*` MCP tools (claude.ai sessions) or directly via git checkout (Claude Code — same file rules apply, commit with clear messages and push).

## Core principle: navigate, never ingest

Never try to load everything. Orient from indexes, fetch files one at a time as the conversation actually needs them. `kb_load` gives you the map (context + index tree + messages); `kb_read` gives you one territory at a time. A good session on a large brain touches ~5 files.

## Session start

1. If no project is identified yet and work is beginning, call `kb_projects()` and ask Hiren which one (or infer from what he's discussing — he often just starts talking about a client).
2. On "load X" or clear project context: `kb_load(project)`.
3. **Surface unread messages FIRST** — they are instructions from a previous session, possibly addressed to your surface (`to: claude-code` means you, if you're in Claude Code). Act on them or ask, then `kb_mark_read`.
4. Confirm state in ONE line (current phase + top open loop). Do not recite the whole context back.

Independent/non-project questions need no loading — not every conversation is a KB session. Load only when work on a known project begins.

## During work

- **Write immediately, don't batch.** The moment something durable is settled — a decision, spec, runbook, person note — `kb_write` it as a concept. Ask Hiren only if genuinely ambiguous whether it's settled.
- **What goes where:**
  - *Concept* — anything a future session needs standalone. One file, correct `type`, relative links to related concepts.
  - *Log line* — what happened this session (history). Goes in the close-out entry, not written mid-session.
  - *Message* — an instruction the NEXT session must act on ("verify DNS Tuesday before CMS work"). `kb_leave_message`, addressed `to:` a surface if it matters.
  - *Nothing* — conversational back-and-forth, dead ends, chit-chat.
- **Reading:** paths come from the index tree or `kb_search`. Use `kb_read(path, depth=1)` when you need to see a concept's neighborhood before deciding to go deeper.
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
