# Engram session lifecycle — design

**Date:** 2026-08-17
**Status:** approved in brainstorming, not yet implemented

## The problem

Hiren runs 2–3 Claude sessions across three machines. Getting one started on a piece
of work means hand-writing a briefing block, pasting it in, and hoping the session finds
the relevant prior decisions. On 2026-08-16 he did this roughly ten times in one day.
Work that finishes is documented only if someone remembers to document it — four research
subagents produced good material that survived solely because the orchestrator wrote it up
by hand.

The gap is not the agent loop. It is everything around it.

## The frame

**Claude Code owns the loop. Engram owns the session lifecycle.**

Prepare → work → coordinate → verify → close.

The loop (tool calls, context management, diff application) is mature, commoditised, and
actively iterated on by people with more resources. Rebuilding it means owning
sandboxing, permissioning, compaction and retry — the exact layer the one documented
defector from Claude Code had to rebuild and regretted. The lifecycle around it is what
nobody else can build, because it depends on Hiren's own accumulated record.

**The boundary rule: anything deterministic is a tool, anything needing judgement is a
skill.** Decomposition cannot be a tool — the
[no-server-LLM decision](../../../../CLAUDE.md) means the server has no judgement, and
chunk boundaries are pure judgement. Preparation cannot be a skill — worktree naming, pin
placement and claim scope must be identical every time, and prose protocols demonstrably
drift (three sessions read identical floor flags on 2026-08-16 and assembled three
different conclusions, which is why `do_next` now states it server-side).

## Architecture

```
  you describe work
        │
        ▼
  ┌─────────────────┐   reads brain + codebase, kb_search for relevant
  │  SKILL          │   decisions / specs / constraints already paid for
  │  chunk-work     │   proposes chunks + refs + boundaries
  └────────┬────────┘
           │  you approve or redraw
           ▼
  ┌─────────────────┐   worktree · branch · .engram-project pin
  │  kb_prepare_    │   thread(goal, exit_condition) · claims
  │  session()      │   brief concept carrying refs
  └────────┬────────┘   → returns one copy-pasteable command
           │
           ▼
     you run it  ──►  Claude Code session
                       realigns cold off the pin, reads the brief,
                       works, ask_human on a real decision
           │
           ▼
  ┌─────────────────┐   detects commits / concepts / artifacts
  │  kb_finish_     │   PR draft → ask_human (never auto-opens)
  │  session()      │   log with proof-links · close thread · release claims
  └─────────────────┘
```

No spawner, no daemon, no tmux. Hiren runs the command. That keeps him in the loop by
construction rather than by policy, and sidesteps the cross-machine problem: prepare runs
where the worktree needs to be.

## Components

### `kb_prepare_session(project, task, files=[], refs=[], goal="", exit_condition="", base="main")`

One atomic call:

1. **Worktree + branch** — `git worktree add ../<repo>-<slug> -b <slug>` off `base`.
   Isolation by construction: two chunks cannot touch each other's files even if the
   boundaries were drawn wrong.
2. **`.engram-project` pin** in the worktree, so the new session's first `realign`
   resolves cold — no asking, no dependence on the routing table.
3. **Thread** named for the chunk, carrying `goal` and `exit_condition`, so it knows what
   done means and the turn cap can quote it back.
4. **Claims** on `files`, so the chunk appears in every other session's `floor.working`
   before a keystroke is typed.
5. **Brief concept** under `projects/<project>/briefs/` — task, constraints, and `refs` as
   links. A map, not a context dump ("navigate, never ingest" applied to startup).

Returns `{worktree, branch, thread, brief_path, command, warnings}`.

**Idempotence is a requirement, not a nicety.** Preparing the same chunk twice must return
the existing worktree or refuse on a dirty branch — never silently create a second. The
ghost-speaker bug of 2026-08-16 was exactly this shape: a second identity created where one
already existed.

### `kb_finish_session(thread, pr_title="", pr_body="")`

Detects what happened rather than being told, so the caller cannot misclassify the chunk:

1. **Commits** on the branch, with links. None is not a failure — that is a research chunk.
2. **Concepts written** during the session. For research or exploration, *these are the
   proof of work*.
3. **Artifacts** produced — a prototype URL, a shared page.
4. **PR draft** offered only when there are commits and a repo where a PR makes sense —
   put in front of Hiren via `ask_human`, never auto-opened. His brain records that two PRs
   opened without asking had to be closed.
5. **`kb_append_log`** with commits as proof-links, so "shipped X" is checkable in one click.
6. **Close the thread**, offering its precipitate (never auto-writing).
7. **Release claims — even if steps above failed.** A stale claim outlives the session and
   misinforms everyone.

The `exit_condition` set at prepare already describes the outcome shape ("one layout chosen
and written to the brain" vs "the reskin merged or explicitly parked"), so no chunk-type
field is needed.

### Skill: `chunk-work`

The judgement layer:

1. Read the request; `kb_search` the brain for relevant decisions, specs, and **constraints
   that have already cost time** — "don't build a fourth option", "PRs on vibechk only,
   after Hiren sees the exact text".
2. Propose chunks: what each does, which files, which refs, and **which chunks share refs**
   — a shared concept between two chunks hints they overlap, which is the thing a human
   cannot check by eye.
3. Check `floor.working` so it never proposes a chunk over live work.
4. On approval, call `kb_prepare_session` per chunk and hand over commands **2–3 at a
   time**, in dependency order.
5. **Refuse to over-split.** No more than ~5 chunks at once, and say plainly when work does
   not decompose cleanly rather than splitting it because it was asked to. A wrong chunk is
   worse than no chunk, because a worktree and a branch make it look legitimate.

## Data flow

| store | holds | why |
|---|---|---|
| brain (git) | brief, goal, exit condition, refs, log, findings | permanent, searchable; the proof for research chunks |
| SQLite | thread floor, claims, activity | ephemeral coordination; dies without loss |
| git worktree | branch, commits | isolation; the proof for code chunks |

Nothing is stored twice. Each store keeps what it is good at.

## Error handling

The rule: **the work is the product, coordination is a convenience.** Nothing here may
prevent Hiren working.

| failure | behaviour |
|---|---|
| worktree creation | **hard stop** — everything downstream assumes it |
| pin write | continue, warn: that session's realign will need the project named |
| thread / claims / brief | continue, warn naming what was not recorded |
| brain unreachable | still return a working command — an unprepared session beats none |

## Testing

Tests written as the situation that goes wrong, not the function that exists:

- prepare on a dirty repo; prepare twice; prepare with the brain unreachable
- a research chunk finishing with **zero commits** — the log must report the concepts, not
  "nothing shipped"
- finish with commits but no PR wanted; finish where `ask_human` goes unanswered
- claims released even when finish fails partway
- refs that do not exist — warn, never block

## Known limitations

- **Same-machine only.** Prepare runs where the worktree goes; it cannot prepare a chunk on
  the Mac from the Windows box. Blocked on the same missing piece as the presence spool
  uploader: something local acting as an agent on each machine.
- **No verification of correctness.** Nothing here checks whether a session's *conclusion*
  was right. On 2026-08-16 two sessions independently reported a bug that did not exist,
  using the same broken method. This design does not fix that; it is the next thing worth
  building, and it should come before autonomy increases.
- **Parallelism is capped at 2–3 by choice.** Anthropic's own figures put multi-agent runs
  at ~15× a single chat, with token usage explaining ~80% of the performance gain — more
  sessions is mostly more spend.

## Decisions taken during design

| question | answer |
|---|---|
| autonomy | works until it needs a decision, then `ask_human` |
| proof of work | commits + PR draft shown before opening; concepts for research chunks |
| decomposition | session proposes, Hiren approves or redraws |
| parallelism | 2–3 at a time |
| shape | bookend tools + judgement skill (not skill-only, not a full pipeline) |
