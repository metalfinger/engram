# Engram auto-presence hooks

Drop-in Claude Code hooks that make every session announce itself to the Engram
workspace roster automatically — no `kb_presence` call needed. Set them up once
per PC.

## What they do

`engram-presence-hook.sh` (a thin backgrounded wrapper) pipes the Claude Code
hook payload to `engram_presence_hook.py`, which:

1. reads `session_id`, `hook_event_name`, `cwd` from stdin,
2. git-detects `repo` / `branch` / `repo_remote` from `cwd` (read-only; empty if not a repo),
3. writes a plain-JSON **spool** file to `~/.engram/presence-spool/<session>.json`.

That's the whole client side. It **never** touches the brain checkout or runs a
git write, so it can't race the server's commit lock. The running Engram server
ingests the spool on a ~30s tick (`engram_server/presence_spool.py`) and upserts
each record into `workspace/presence/` through its single write lock, batched and
throttled (a session re-commits only when a meaningful field changes or its record
is >5 min stale). Status is `working` on SessionStart/UserPromptSubmit, `done` on
SessionEnd.

## Why a spool (not a direct git hook)

A hook that imported the store and committed would be a second git writer racing
the server → checkout corruption. The spool keeps the client to a bare file write
and leaves the one-writer invariant intact. See
`projects/engram/decisions/2026-07-auto-presence-spool.md` in the brain.

## Install (per PC)

1. Copy both files into `~/.claude/hooks/`.
2. Add the wrapper to `~/.claude/settings.json` under **SessionStart**,
   **UserPromptSubmit**, and **SessionEnd** (append alongside any existing hooks;
   each event takes a hook group):

   ```json
   { "hooks": [ { "type": "command", "command": "bash ~/.claude/hooks/engram-presence-hook.sh" } ] }
   ```

3. That's it — the server side is already deployed. Watch yourself appear at
   `https://engram.metalfinger.xyz/brain/workspace`.

## Attaching a repo to a project (`.engram-project`)

Drop a one-line file named `.engram-project` at a repo's root containing an Engram
project id (e.g. `engram`). The hook walks up from `cwd` to the repo top, reads it,
and stamps the session's `project` — so that session shows up in that project's
**room** in the live office (`/brain/office`), and the roster knows its home project.

```
echo engram > .engram-project      # pin this repo's sessions to the "engram" project
```

Lines starting with `#` are ignored; the first non-empty line wins. This is purely a
**home-room hint** — it never restricts access. Every session can still read any
project via the `kb_*` tools; the room is just where its character sits.

## Status line (`engram-statusline.sh`)

Optional terminal chrome: project pin · branch · whose turn it is in a room ·
what's blocked on you · a warning when another session is already in this
worktree. It reads a cache and spawns a detached refresh, so it never blocks the
prompt — a status line that stalls is worse than one thirty seconds behind.

**It wraps rather than replaces.** If you already have a status line, name it in
`~/.engram/statusline.json` and its output is printed above ours, unchanged:

```json
{ "parent": "python C:/Users/Admin/.claude/statusline-helix.py" }
```

Then point `settings.json` at ours once — `bash ~/.claude/hooks/engram-statusline.sh` —
and the same entry works on every PC, because each machine's own
`statusline.json` decides what else it shows. To undo, restore the old
`statusLine.command`; nothing else changed.

Live data needs `~/.engram/upload.json` (the server's own machine has no cache
source and shows the local segments only). `kb_setup_machine` writes all of this.

## Don't digest this whole directory

`~/.claude/hooks/` is **shared** — other tools' hooks live here too. Engram owns
only the `engram*` files, and the update check compares just those. Digesting the
whole directory reports "stale" forever on any real machine.

## Config (server side, all optional, `ENGRAM_` env prefix)

- `PRESENCE_SPOOL_DIR` — spool dir (default `~/.engram/presence-spool`)
- `PRESENCE_INGEST_SECONDS` — tick cadence (default `30`)
- `PRESENCE_REFRESH_MINUTES` — heartbeat/dedupe window (default `5`)
