# Engram Notifications (Chrome extension)

An ambient teammate surface for Engram: desktop notifications for DMs, room invites, questions
and answers, plus a live "who's working now" team list — all in the toolbar popup, without
keeping Claude or the dashboard open.

## What it does

- Every N minutes (via `chrome.alarms`, not `setInterval` — MV3 service workers are ephemeral
  and a timer would die with them; default 1 minute, configurable in Options), it polls the
  notifications endpoint, authenticated with a token obtained through a one-click OAuth sign-in.
- New unread items (never seen before) raise a `chrome.notifications` desktop notification and
  update the toolbar badge with the unread count. Room-invite notifications open that room when
  clicked; everything else opens the dashboard.
- The toolbar popup shows:
  - **Working now** — an avatar list of teammates currently active, with a freshness dot and an
    "Appear invisible" toggle for your own presence.
  - **Notifications** — DMs, room invites (with an inline **Open** button), questions, answers,
    and contact requests, each with a small kind badge, plus a "Mark all read" button.
  - A header with your handle, a status dot, an "Open dashboard" link, and sign-out.
- The options page holds the API origin (only needs changing if self-hosting or testing locally)
  and the poll interval, and offers the same one-click sign-in/out.

## Sign-in — no tokens, ever

Sign-in uses `chrome.identity.launchWebAuthFlow` against `{origin}/dashboard/ext-auth`, which
carries you through the exact same GitHub/Google OAuth as the dashboard and the MCP connector,
then redirects back to the extension with a scope=`notify` token in the URL fragment. That token
is stored in `chrome.storage.local` (never `.sync` — an auth token shouldn't be copied across
every Chrome profile you're signed into) and attached as `Authorization: Bearer <token>` on every
API call. There is no field anywhere in this extension to paste a token by hand. Signing out just
clears the stored token; the next click on "Sign in with Engram" gets a fresh one.

## Two hostnames, one server

- `https://engram.metalfinger.xyz` — the API/connector origin. This is the only origin the
  extension ever calls with `fetch()`.
- The web dashboard lives on the SAME origin (`/dashboard`). This is where "Open dashboard", room-invite
  **Open** buttons, and notification clicks send you (`chrome.tabs.create`, not `fetch`).

Both are declared in `host_permissions` so the extension can reach the API and open dashboard tabs
without an extra permission prompt out of the box. The origin override in Options only affects the
API origin (self-hosting/testing); the dashboard link is fixed.

## Endpoint contract it depends on

All calls below (except `ext-auth`, which is the sign-in redirect itself) send
`Authorization: Bearer <token>` — the scope=`notify` token from OAuth sign-in:

- `GET {origin}/dashboard/ext-auth?redirect=<chrome.identity redirect URL>` → sends the user
  through GitHub/Google sign-in, then 302s to `<redirect>#token=<token>`.
- `GET {origin}/dashboard/api/notifications`
  - `200 {"ok": true, "unread": [{"id": int, "kind": str, "body": str, "at": ISO string, "ref"?: str}], "counts": {"dms": int, "notifications": int}}`
  - `401 {"ok": false, "error": "..."}` — treated as "not authenticated": the stored token is
    cleared, the popup shows the signed-out state, badge clears, no notifications are raised.
- `POST {origin}/dashboard/api/notifications/read` → `{"ok": true}` — called when a notification is
  clicked, or "Mark all read" is pressed in the popup.
- `GET {origin}/dashboard/api/team` →
  `{"ok": true, "team": [{"handle", "display_name", "avatar_url", "project", "tool", "minutes_ago"}], "me"?: {"handle", "invisible"}}`
  — if this 404s (not deployed yet on the target server), the popup falls back to a friendly
  "Team presence is coming soon" empty-state instead of erroring.
- `POST {origin}/dashboard/api/presence` with body `{"invisible": true|false}` →
  `{"ok": true, "invisible": bool}` — wired to the "Appear invisible" toggle; same 404 tolerance
  (the toggle is disabled until `/api/team` has returned a `me`).

If no token is saved yet, the background worker doesn't fetch at all — it just shows the
signed-out state.

## Polling + dedupe logic (one sentence)

An alarm fires on the configured interval; the background worker fetches the notifications
endpoint (skipping the call entirely if no token is saved), tracks the highest notification `id`
it has ever seen in `chrome.storage.local`, silently records the current max as a baseline on
first run (no notification spam from backfill), and on every later poll only raises a desktop
notification for items with an `id` greater than that stored baseline before advancing it.

## MV3 gotchas worked around

- **No `setInterval`** — the service worker can be killed at any time between events, so all
  polling goes through `chrome.alarms`, and all state (`lastSeenMaxId`, cached unread list,
  signed-in flag, token) lives in `chrome.storage`, never in a module-level variable.
- **Alarm handler never throws** — every poll is wrapped in try/catch; a network failure or a
  non-JSON response is silently ignored and retried on the next tick.
- **Configurable poll interval needs the alarm re-armed live** — the options page writes
  `pollMinutes` to storage, and `background.js` listens for `chrome.storage.onChanged` to recreate
  the alarm immediately rather than waiting for the next browser restart.
- **Configurable origin needs a permission grant, not just a manifest edit** — `host_permissions`
  in the manifest is fixed at install time and only covers the default origins. The options page
  requests a custom origin via `chrome.permissions.request` (declared as
  `optional_host_permissions: ["https://*/*"]`) before saving it. If the user denies that
  permission prompt, saving is rejected with an explanation — the only way to force it through
  then is editing `host_permissions` in `manifest.json` directly and reloading the unpacked
  extension.
- **`chrome.notifications.create` requires an `iconUrl`** for the `basic` type, so a tiny local
  icon set was generated (see below) even though the toolbar icon itself is optional.
- **Notification click routing without a second lookup** — each notification's id encodes
  `engram:<id>:<kind>:<url-encoded ref>`, so clicking a room-invite notification can open that
  room directly (`chrome.notifications.onClicked` decodes the id) without a stored side-table.
- **No HTML injection from server data** — every dynamic row (notifications, team members) is
  built with `document.createElement` + `textContent`/attribute assignment, never `innerHTML` with
  a server-provided string. Avatar images are only rendered from `https://` URLs; anything else
  falls back to a colored initials circle.
- **Shared code, no build step** — `common.js` holds the storage/auth/formatting helpers used by
  all three surfaces (`background.js` via `importScripts`, `popup.js`/`options.js` via a plain
  `<script>` tag before the page's own script). Still zero external libraries, zero bundler.

## Icons

Generated (not hand-authored) via `make_icons.py`, a one-off script using PIL:

```
uv run --no-sync python make_icons.py
```

This writes `icons/icon16.png`, `icons/icon48.png`, `icons/icon128.png` — solid rounded squares
with an "E" mark. They're already committed under `icons/`; re-run the script only if you want to
regenerate them. (If PIL/uv aren't available, you can delete the `icons/` block from
`manifest.json` and drop the `iconUrl` line in `background.js`'s `notifications.create` call —
Chrome will fall back to a default icon for the toolbar, though `notifications.create` will need
some icon path to be valid.)

## Install (load unpacked)

1. Open `chrome://extensions`.
2. Toggle **Developer mode** on (top right).
3. Click **Load unpacked**.
4. Select this folder (`clients/chrome-extension/`).
5. Click the toolbar icon (or right-click it → **Options**) and click **Sign in with Engram** —
   one click through the same GitHub/Google login you already use, no tokens involved.

If you're not signed in yet, the popup shows a sign-in prompt and no notifications fire — nothing
is broken, there's just nothing to poll with yet.

## Upgrading from 1.x

The 1.x extension stored a manually-pasted token in `chrome.storage.sync`. On update, the
background worker copies that token into `chrome.storage.local` once (if present and no local
token already exists) and clears it from `sync`. If for any reason that doesn't pick it up, just
sign in again from the popup or Options — it takes a few seconds and needs no manual token.

## Changing the origin or poll interval

Open **Options**. The API origin field only needs changing for self-hosting or testing against a
different Engram instance — changing it prompts Chrome for permission to reach that host, then
you'll need to sign in again against the new origin. The poll interval takes effect immediately,
no reload required.
