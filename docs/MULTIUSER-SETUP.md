# Engram multi-user — operator setup (v2 M1)

Turning on multi-user. Everything below is `.env` config on the server PC; the code
is already deployed. **The hard rule stands: do not send an invite until steps 3
(backups) and the isolation harness are green.**

## 0. Prerequisites (already true)
- Single-user Engram runs on this PC, port 9210, tunneled by cloudflared as
  `engram.metalfinger.xyz` (MCP + OAuth) and `brain.metalfinger.xyz` (explorer).
- Full test suite green, including `tests/test_isolation_harness.py` (the isolation gate).

## 1. Flip the flag + session secret (`~/.engram/.env`)
```
ENGRAM_MULTIUSER=1
ENGRAM_DASHBOARD_SESSION_SECRET=<paste 32+ char secret>
```
Generate the secret:
```
python -c "import secrets;print(secrets.token_urlsafe(32))"
```
The server **refuses to start** in multiuser without OAuth configured (below) and
without a ≥32-char session secret — both are fail-closed guards, not warnings.

## 2. OAuth apps (both IdPs, so non-developers can sign in with Google)
The MCP connector uses one IdP (`ENGRAM_OAUTH_PROVIDER`, default `github`). The
**dashboard** offers every IdP whose creds are present, so add both:
```
ENGRAM_GITHUB_CLIENT_ID=...
ENGRAM_GITHUB_CLIENT_SECRET=...
ENGRAM_GOOGLE_CLIENT_ID=...
ENGRAM_GOOGLE_CLIENT_SECRET=...
```
Register these **redirect URIs** in each OAuth app:
- `https://engram.metalfinger.xyz/oauth/callback` — MCP connector sign-in (existing).
- `https://engram.metalfinger.xyz/dashboard/callback` — dashboard/onboarding sign-in (**new**).

You (the operator) are always allowed via `ENGRAM_OWNER_SUBJECTS` (default
`github:metalfinger`) and map to your existing brain — nothing about your setup changes.

## 3. Off-site backups (REQUIRED before any invite)
Create a **private** GitHub repo `brains-mirror`, add the existing Engram deploy key
to it with write access, then:
```
ENGRAM_BACKUP_REMOTE=git@github.com:metalfinger/brains-mirror.git
```
The nightly job (04:30, `ENGRAM_BACKUP_AT`) force-pushes every tenant bare to
`users/<handle>` branches. Restore procedure: `docs/BACKUP-RESTORE.md`.

## 4. Invite email (optional — invites work without it)
```
ENGRAM_CF_EMAIL_API_TOKEN=...        # empty = mailer off: dashboard shows a copy-link instead
ENGRAM_CF_EMAIL_ACCOUNT_ID=...
ENGRAM_INVITE_FROM_EMAIL=no-reply@metalfinger.xyz
```
> **Verify before relying on auto-email:** Cloudflare's Email Sending REST shape has
> changed over time. The endpoint lives in one constant (`mailer.CF_EMAIL_ENDPOINT`).
> With email off (or misconfigured), invite creation still works — the dashboard shows
> the `join` link to copy. Test with one real invite to yourself before trusting it.

## 5. Restart + smoke test
```
# kill the PID on 9210, then:
powershell -File scripts/start-engram.ps1
```
Then:
1. Open `https://engram.metalfinger.xyz/` — the public homepage renders.
2. Open `https://engram.metalfinger.xyz/dashboard` → sign in (GitHub) → you land on
   your dashboard as owner, with the Members + Invite panel.
3. Invite yourself at a second email → open the `/join?token=...` link in a private
   window → sign in with Google → claim a test handle → confirm a brain is
   provisioned under `~/.engram/users/<handle>/` → connect a fresh Claude session as
   that user and confirm it sees ONLY that brain.
4. Delete the test account's dir + row when done (no self-serve delete yet).

## What each surface is
- `/` — public homepage (marketing + install steps). No auth.
- `/dashboard` — signed-cookie session (separate from MCP OAuth and CF Access). Owner
  sees admin (members + invites); members see their handle + connect instructions.
- `/join?token=` — invite acceptance → sign in → claim handle → account + brain.
- The **explorer** (`brain.metalfinger.xyz`) stays yours, behind Cloudflare Access —
  it is NOT multi-tenant and is not exposed to other users.

## Guardrails already enforced
- Every kb_* tool resolves the caller's own store (M0.4); cross-tenant access is
  blocked and covered by `tests/test_isolation_harness.py`.
- Per-tenant quota (`ENGRAM_TENANT_QUOTA_MB`, default 200) + rate limit
  (`ENGRAM_TENANT_RATE_PER_MIN`, default 120); owner exempt.
- Reserved + Windows device-name handles rejected before provisioning.
