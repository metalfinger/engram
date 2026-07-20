# Off-site backup + restore (M0.8)

Single machine = single point of failure for every user's brain. A nightly job
mirrors every user's bare repo (`users_root/<handle>/brain.git`) to ONE private
off-site git remote, each user parked on its own branch: `users/<handle>`. One
remote, one deploy key, no per-user repo creation. The operator's own brain
(Hiren's) is **not** part of this — it lives outside `users_root` and already
pushes to its own GitHub remote on every write.

Implementation: `server/engram_server/backup.py` (`mirror_all(settings)`),
scheduled nightly at `settings.backup_at` (default `04:30`) alongside the
existing reconcile/briefing jobs.

## Setup (one-time)

1. Create a private GitHub repo to hold the mirror, e.g. `metalfinger/brains-mirror`.
2. Add the **existing** deploy key (`~/.engram/id_engram.pub`, same one used
   for the operator brain) to that repo's Deploy Keys with **write** access.
   No new key needed — `backup.py` reuses `settings.deploy_key_path`.
3. Set `ENGRAM_BACKUP_REMOTE` in `.env`:
   ```
   ENGRAM_BACKUP_REMOTE=git@github.com:metalfinger/brains-mirror.git
   ```
   Leave it empty (default) to keep the mirror disabled.
4. Restart the server (`scripts/start-engram.ps1`). The scheduler picks up the
   nightly mirror at the next `backup_at` tick — no further action needed.

## What the nightly job does

For every `users_root/<handle>/brain.git`:

```
git -C users_root/<handle>/brain.git push --force <ENGRAM_BACKUP_REMOTE> main:refs/heads/users/<handle>
```

- Force-push is intentional: the mirror is a copy, never a source of truth.
- One user's failure (corrupt bare, transient network error) never blocks the
  others — each user succeeds or fails independently; failures are logged
  with the git error, truncated.
- Idempotent and cheap: an unchanged bare simply re-pushes the same SHA.

## Restore procedure

Say `alice`'s bare on the operator PC is gone or corrupted. The mirror has her
latest state on `users/alice`.

**1. Clone the mirror locally (or work from a machine that already has it):**

```
git clone git@github.com:metalfinger/brains-mirror.git brains-mirror
```

**2. Rebuild alice's bare from her branch in the mirror.** Create (or reuse) an
empty bare at the exact path the server expects, then push her branch into it
as `main` (or whatever `ENGRAM_BRAIN_BRANCH` is configured to):

```
git init --bare --initial-branch main "$HOME/.engram/users/alice/brain.git"
git -C brains-mirror push "$HOME/.engram/users/alice/brain.git" users/alice:refs/heads/main
```

(On Windows/PowerShell, quote the path the same way; `git init --bare` and
`git push` behave identically.)

**3. Let the server re-clone the checkout.** Delete any stale checkout so
`ensure_user_brain` recreates it fresh from the now-restored bare (the bare is
always the source of truth for the checkout, per `provisioning.py`):

```
rm -rf "$HOME/.engram/users/alice/brain"
```

The next tool call or scheduler tick for `alice` calls `ensure_user_brain`,
which sees the bare already has commits and just re-clones the checkout — no
skeleton reseed, no data loss.

**4. Verify:**

```
git -C "$HOME/.engram/users/alice/brain.git" log -1 --oneline
git -C "$HOME/.engram/users/alice/brain" log -1 --oneline
```

Both should show the same HEAD, matching what was last mirrored.

### Restoring ALL users at once

Repeat step 2 for every `users/<handle>` branch in the mirror — list them with:

```
git -C brains-mirror branch -r --list 'origin/users/*'
```

There is no bulk restore tool by design: a full-machine rebuild is rare enough
that a per-user loop over this runbook is safer than an untested script.
