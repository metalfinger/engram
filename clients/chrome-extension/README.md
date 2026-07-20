# Engram Notifications (Chrome extension)

A small MV3 Chrome extension that polls Engram for new DMs / notifications and raises a
desktop notification, so you hear about a new message without keeping Claude or the dashboard
open.

## What it does

- Every ~60 seconds (via `chrome.alarms`, not `setInterval` — MV3 service workers are ephemeral
  and a timer would die with them), it calls the dashboard notifications endpoint, authenticated
  with a bearer token you paste in once.
- New unread items (never seen before) raise a `chrome.notifications` desktop notification and
  update the toolbar badge with the unread count.
- Clicking a notification opens the dashboard and marks everything read.
- The toolbar popup shows the current unread list with a "Mark all read" button and an "Open
  dashboard" link. If no token is set (or it's rejected), it shows a "Paste your Engram extension
  token in Options" link instead.
- The options page holds the extension token and the Engram origin (only needs changing if
  self-hosting or testing locally).

## Endpoint contract it depends on

The extension calls exactly two endpoints on the configured Engram origin (default
`https://engram.metalfinger.xyz`), always sending `Authorization: Bearer <token>` (the token
pasted into Options — **not** a cookie; a cross-origin extension fetch can't reliably carry the
dashboard's `SameSite=Lax` session cookie, so bearer auth is the robust choice):

- `GET {origin}/dashboard/api/notifications`
  - `200 {"ok": true, "unread": [{"id": int, "kind": str, "body": str, "at": ISO string}], "counts": {"dms": int, "notifications": int}}`
  - `401 {"ok": false, "error": "..."}` — treated as "not authenticated": popup shows the
    paste-your-token state, badge clears, no notifications are raised.
- `POST {origin}/dashboard/api/notifications/read` → `{"ok": true}` — called when a notification is
  clicked, or "Mark all read" is pressed in the popup.

If no token is saved yet, the background worker doesn't fetch at all — it just shows the
signed-out state.

## Polling + dedupe logic (one sentence)

An alarm fires every minute; the background worker fetches the endpoint (skipping the call
entirely if no token is saved), tracks the highest notification `id` it has ever seen in
`chrome.storage.local`, silently records the current max as a baseline on first run (no
notification spam from backfill), and on every later poll only raises a desktop notification for
items with an `id` greater than that stored baseline before advancing it.

## MV3 gotchas worked around

- **No `setInterval`** — the service worker can be killed at any time between events, so all
  polling goes through `chrome.alarms`, and all state (`lastSeenMaxId`, cached unread list,
  signed-in flag) lives in `chrome.storage`, never in a module-level variable.
- **Alarm handler never throws** — every poll is wrapped in try/catch; a network failure or a
  non-JSON response is silently ignored and retried on the next tick.
- **Configurable origin needs a permission grant, not just a manifest edit** — `host_permissions`
  in the manifest is fixed at install time and only covers the default origin. The options page
  requests the custom origin via `chrome.permissions.request` (declared as
  `optional_host_permissions: ["https://*/*"]`) before saving it, so switching origins doesn't
  require reloading the unpacked extension in the common case. If the user denies that permission
  prompt, saving is rejected with an explanation — the only way to force it through then is editing
  `host_permissions` in `manifest.json` directly and reloading the unpacked extension. This is
  still needed under bearer-token auth — it's about letting the extension reach a cross-origin URL
  at all, independent of how the request authenticates.
- **`chrome.notifications.create` requires an `iconUrl`** for the `basic` type, so a tiny local
  icon set was generated (see below) even though the toolbar icon itself is optional.

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

Then get your extension token from the Engram dashboard and paste it in:

1. Sign in to `https://engram.metalfinger.xyz/dashboard` in any browser.
2. Copy the extension token shown there (Settings → Extension token).
3. Right-click the toolbar icon → **Options** (or `chrome://extensions` → this extension →
   **Details** → **Extension options**), paste the token, and click **Save**.

If no token is set, the popup shows a prompt to add one and no notifications fire — nothing is
broken, there's just nothing to poll with yet.

## Changing the origin

Open **Options** (see above). Enter an `https://` origin with no path (e.g.
`https://engram.example.com`) alongside your token and click **Save** — Chrome will prompt for
permission to access that origin; accept it. The next alarm tick polls the new origin.
