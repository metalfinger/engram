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

## Status-line segments, and what each one means

- `⬢ <project> · <branch>` — the pin and the worktree, always.
- `⏳ turn waiting: X` — a LIVE session on this account holds the floor in X and
  is not parked. Actionable soon, by whichever session it belongs to.
- `🚪 abandoned room: X [room]` — the floor-holder has not been seen for hours;
  the conversation went home mid-turn. Close it or revive it at leisure. Kept
  separate from ⏳ because six days of a false "your turn" teaches the reader to
  ignore the one segment that must never become ignorable.
- `❓ waiting on you: X` — a conversation is blocked on the PERSON (ask_human).
- `⚠ also here: <session>` — another session is working in this worktree.
- Names are threads unless suffixed `[room]` — two field reports in one day
  chased "empty threads" that were rooms, so the kind now travels with the name.

Everything here is ACCOUNT-level ambient signal at zero token cost; the
session-level truth is `kb_threads()` / `kb_rooms()` (`waiting_on_you`,
`your_turn`, `do_next` — computed against that session's own speaker key).

## Turn awareness (`engram-turns-hook.sh`)

UserPromptSubmit, FOREGROUND — its stdout becomes model context, which is the
whole point (the presence hook is backgrounded, so its stdout goes nowhere).
Reads only `~/.engram/status-cache.json` (maintained by the status line — no
network, sub-10ms) and prints one `[engram] your turn in thread(s): …` line when
a thread or ask is waiting. A Claude Code session that ended its turn cannot be
woken, so every user prompt doubles as the checkpoint where it finds out.
Silent on a stale (>3 min) or absent cache: saying nothing beats reporting a
turn someone already took.

## Staying current without remembering (`engram-update.sh`)

Hooks and skills are FILES ON DISK, so unlike everything served over MCP they do
not follow the server when it changes. That gap bit twice: a skill sat six weeks
stale on the Mac, and the presence hook sat a version behind for a day — the fix
deployed and simply never running there, with nothing saying so.

`engram-update.sh` closes it without a Claude session: it compares this machine's
digests against the server's, downloads only what differs, **verifies the bundle
hashes to the digest the server advertised before installing** (a stale edge
cache once served an old bundle while reporting success), and logs one line to
`~/.engram/update.log`. It writes only `engram*` files into the shared hooks
directory and the skill dirs the manifest names, and it **never touches
settings.json** — wiring a hook into the right events is a judgement call that
stays with `kb_setup_machine` and a human.

Run it by hand, or on a schedule:

```bash
# macOS / Linux — daily at 9am via cron
0 9 * * * ~/.claude/hooks/engram-update.sh

# Windows — daily via Task Scheduler
schtasks /Create /TN "Engram Update" /TR "bash %USERPROFILE%\.claude\hooks\engram-update.sh" /SC DAILY /ST 09:00
```

The status line also shows `⬆ engram update (run setup)` when digests differ, so
drift is visible even on a machine with no schedule.

## Don't digest this whole directory

`~/.claude/hooks/` is **shared** — other tools' hooks live here too. Engram owns
only the `engram*` files, and the update check compares just those. Digesting the
whole directory reports "stale" forever on any real machine.

## Config (server side, all optional, `ENGRAM_` env prefix)

- `PRESENCE_SPOOL_DIR` — spool dir (default `~/.engram/presence-spool`)
- `PRESENCE_INGEST_SECONDS` — tick cadence (default `30`)
- `PRESENCE_REFRESH_MINUTES` — heartbeat/dedupe window (default `5`)
