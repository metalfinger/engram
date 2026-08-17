---
name: chunk-work
description: Use when Hiren wants a piece of work split across sessions — "break this up", "run these in parallel", "prepare sessions for X", or any request too big for one session to hold. Proposes chunks grounded in the brain, prepares an isolated worktree and thread for each, and hands back one command per chunk. Also covers closing a chunk out with proof of work.
type: skill
---

# Chunking work across sessions

Hiren runs 2–3 sessions across three machines. This skill turns one piece of work into
prepared, isolated chunks — each with its own worktree, branch, thread, goal and brief —
and hands him a command per chunk. He runs them; the sessions coordinate through Engram.

**Two tools do the deterministic parts.** `kb_prepare_session` creates everything;
`kb_finish_session` closes it out. Your job is the judgement in between: what the chunks
ARE, and what each one needs to know.

## Before proposing anything: read

**Search the brain first, every time.** The chunks you propose are only as good as what
you know, and the brain holds decisions already made and constraints already paid for.

- `kb_search` for the subject — prior decisions, specs, runbooks.
- Look specifically for **constraints that have already cost him**. Real examples in his
  brain: *PRs go on the vibechk repo only, and only after he sees the exact text* (two were
  opened without asking and had to be closed); *an exploration ships as a NEW option — a
  moved default reads as a broken component*; *don't build a fourth option while a choice
  is open*. A chunk that violates one of these produces work that gets thrown away.
- `kb_realign` if you don't already know the project's current phase and open loops.

**Check `floor.working`.** Never propose a chunk over files another session is already on.
It rides in the result of any room or thread call.

## Proposing chunks

Present them for approval **before creating anything**. For each chunk:

- **name** — becomes the branch, worktree and thread id, so make it short and descriptive
- **what it does**, in one line
- **files** it expects to touch
- **refs** — the concepts it should read first
- **goal** and **exit_condition** — what done means, concretely enough to check

**Flag chunks that share refs.** Two chunks reading the same decision is a hint they
overlap, and overlap is the thing Hiren cannot check by eye. Say so explicitly rather than
leaving it for him to notice.

**Refuse to over-split.** Around five chunks is the practical ceiling. If work doesn't
decompose cleanly, say so and propose fewer — a wrong chunk is worse than no chunk,
because a worktree and a branch make it look legitimate. Splitting something because you
were asked to split it is how two sessions end up doing the same work differently.

## Preparing

On his approval, one call per chunk:

```
kb_prepare_session(project, task, repo_path, files=[...], refs=[...],
                   goal="...", exit_condition="...")
```

`repo_path` is absolute and required — the server cannot see anyone's working directory.

Hand back the commands **2–3 at a time**, in dependency order. Not all of them: multi-agent
runs cost roughly 15× a single chat and token usage explains most of the gain, so more
sessions is mostly more spend. Three sessions he can read beats five he cannot.

Tell him what each command will do before he runs it — one line each, not a wall.

## What the prepared session does

It starts cold, types `realign`, and inherits everything: project (from the pin), brief,
goal, exit condition, refs, and who else is working where. It works until it hits a real
decision, then uses `ask_human` — which reaches Hiren in whatever session he is actually
talking to, not on a web page.

## Closing a chunk

```
kb_finish_session(thread, project, repo_path, summary="...",
                  pr_title="...", pr_body="...")   # PR fields optional
```

It detects what happened: commits on the branch, and concepts written since the thread
started. **A chunk with no commits is not a failure** — that is a research chunk, and its
concepts are the proof of work.

Pass `pr_title`/`pr_body` only when a PR genuinely makes sense. They are put to Hiren
through `ask_human`; nothing is opened until he says yes, and the thread stays open until
he answers.

## Judgement calls

- **Don't chunk small work.** One session that finishes beats three that coordinate. If it
  fits in one session, say so.
- **Chunk by ownership, not by task list.** Good boundaries are "these files, that
  surface"; bad ones are "step 1, step 2" — sequential steps aren't parallel work, they're
  one chunk.
- **A chunk that needs another chunk's output isn't a chunk.** Say it's sequential and
  prepare the second one after the first lands.
- **If you're unsure whether two chunks overlap, they overlap.** Merge them.
