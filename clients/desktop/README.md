# Engram Tray

Ambient desktop presence for [Engram](https://engram.metalfinger.xyz) — a
system-tray app (Windows/macOS/Linux) built with **Rust + Tauri v2**.

v1 was a *thin agent*: tray-only, no window, no web content. **v1.1 adds
a popup** — left-click the tray icon for a small local-HTML panel with
your avatar, the live "working now" roster with everyone's avatars, and
actionable notifications. Right-click keeps the classic menu. The
thin-agent security model is unchanged: the popup renders a page shipped
in `dist/` and talks to Rust only over `invoke` — it never loads
`engram.metalfinger.xyz` and never sees the bearer token (see "The
popup" below).

## What it does

- Polls `GET {origin}/dashboard/api/notifications` every `poll_seconds`
  (default 60s) and fires a native notification for anything new.
- Polls `GET {origin}/dashboard/api/team` on the same cadence to show
  who's active right now, and resolves each teammate's `avatar_url` into
  a cached `data:` URI (see "Avatars" below).
- Tray icon swaps to an "unread" badge variant whenever there's anything
  unread; tooltip shows the count.
- **Left-click** the tray icon to toggle the popup (see below).
  **Right-click** for the classic menu: header (`@handle` or "signed
  out"), Open Engram (same popup toggle), Open Dashboard, Sign in / Sign
  out, Launch at login, Quit. The roster, invisible toggle, and mark-all
  -read live in the popup now instead of the menu.
- Sign-in is a loopback OAuth flow: it opens your browser at the
  dashboard's GitHub/Google sign-in, and a short-lived local HTTP
  listener on `127.0.0.1` catches the token when the browser redirects
  back (see "How sign-in works" below).

## The popup

A frameless, always-on-top, ~360×520 window (label `popup`) created once
at startup, hidden, and only ever shown/hidden after that — never torn
down and recreated. Left-clicking the tray toggles it; it's positioned
near the click point, opening upward on a bottom taskbar (Windows/Linux)
or downward from a top menu bar (macOS) — inferred from which half of
the monitor the click landed in, not a per-OS branch. It hides on focus
loss or **Esc**. If the tray click doesn't carry a usable position (or
monitor lookup fails), it falls back to a fixed corner of the primary
monitor's work area.

Sections top to bottom:

1. **Me** — your avatar, `@handle`, and "working on `<project>`" if your
   own roster row has one (you might not be in `team[]` at all; the
   popup only shows what's there). An eye-icon toggles "appear
   invisible", a gear opens the config folder, and a power icon signs
   out.
2. **Working now** — friend cards (avatar, name, project, a freshness
   dot: green ≤15m / amber ≤2h / grey older, relative time). Click a
   card to open `{origin}/dashboard/u/<handle>` in your browser.
3. **Notifications** — one row per unread item, with a per-kind action
   button (room invite/closed → open the room, DM → open messages,
   question/answer → open asks) plus a single "Mark all read" in the
   section header (there's no per-row dismiss server-side).
4. A footer with Open Dashboard / Open Rooms.

### Security model: local HTML, Rust-mediated data

The popup's webview loads `dist/index.html`/`main.js`/`style.css` — it
never fetches anything from the network and never receives the bearer
token. Every piece of data it shows comes from Rust over `invoke`
(`get_state`, `refresh_now`, `set_invisible`, `mark_all_read`, `sign_in`,
`sign_out`, `open_url`, `open_config_folder`, `hide_popup`, `quit_app` —
all defined in `src-tauri/src/main.rs`). `open_url` only ever opens
`{origin}` + a path under an explicit `/dashboard` allowlist — the
webview can ask to navigate the *browser* to a dashboard page, never to
an arbitrary URL. A strict CSP (`default-src 'self'; img-src 'self'
data:; style-src 'self' 'unsafe-inline'`) is set in `tauri.conf.json`.

Updates reach the popup two ways: every mutating command returns a
fresh state snapshot for immediate re-render, and the background poll
loop pushes a snapshot into the popup's JS after every tick via
`WebviewWindow::eval` (`window.__engramPush`) — deliberately **not**
Tauri's `emit`/`listen` event bridge, which is IPC/ACL-gated the moment
an app defines a `capabilities/*.json` manifest (this project
intentionally has none, so its own custom commands stay ungated too —
see the doc comment above the commands in `main.rs`). `eval` bypasses
that layer entirely since it's a direct Rust→webview call, not an IPC
round-trip.

### Avatars

`team[]` entries carry `avatar_url`, which can be an `https://` URL or
an already-inlined `data:` URI. Rust resolves these — never the webview:
`data:image/...` values pass straight through (any other `data:` type is
rejected); `https://` values are fetched with a 5s timeout and a
300KB/`image/*`-only cap, then cached by URL (so a shared avatar is
fetched once) and handed to the popup as a `data:` URI. No avatar (or a
failed fetch) falls back to a deterministic initials circle, colored by
a hash of the handle, drawn entirely in JS.

**SSRF hardening.** `avatar_url` is *other users'* data — it comes from
the team roster, so a malicious teammate could point theirs at an
internal address and every teammate's tray would fetch it from inside
their own network. `avatar.rs` guards against that: only `https://` is
accepted (no `http://`); the hostname is resolved first, and the fetch
is refused outright if *any* resolved address is loopback, private
(10/8, 172.16/12, 192.168/16), link-local, unique-local, CGNAT, or a
couple of other non-public ranges; the actual request is then pinned via
`ClientBuilder::resolve_to_addrs` to exactly the address set just
validated (a plain client would let the real connection re-resolve DNS
independently, which is the classic rebinding bypass — the whole
validate-then-fetch check is worthless if the check and the connection
aren't guaranteed to hit the same address); and redirects are disabled
(`redirect::Policy::none()`), so a 3xx is just treated as "no avatar"
rather than followed somewhere unvetted.

## Config file

Location (created on first run if missing):

| OS      | Path                                                              |
|---------|--------------------------------------------------------------------|
| Windows | `%APPDATA%\engram-tray\config.json`                                |
| macOS   | `~/Library/Application Support/engram-tray/config.json`             |
| Linux   | `~/.config/engram-tray/config.json`                                 |

```json
{
  "origin": "https://engram.metalfinger.xyz",
  "token": null,
  "poll_seconds": 60
}
```

- `origin` — override to point at a self-hosted/staging Engram server.
- `token` — the bearer token from sign-in. Never logged; treat like a
  password. Delete it (or use "Sign out") to force a re-login.
- `poll_seconds` — polling cadence. The app reads this file at startup;
  edit it while the app is closed, or sign out/in to force a reload.

## How sign-in works (loopback OAuth)

The dashboard's `ext-auth` endpoint hands the token back in the URL
**fragment** (`#token=...`), which browsers never send to a server. So:

1. The app generates a random nonce and binds a one-shot HTTP listener
   on `127.0.0.1:<ephemeral port>`, at path `/callback/<nonce>`.
2. It opens your default browser at
   `{origin}/dashboard/ext-auth?redirect=http://127.0.0.1:<port>/callback/<nonce>`.
3. After you finish signing in there, the dashboard redirects the
   browser back to that exact loopback URL with `#token=...` appended.
   A tiny inline page (served by the app itself) reads the fragment in
   JS and `fetch`-POSTs the token to `/token/<nonce>` on the same
   listener, then shows "Signed in — you can close this tab."
4. The app receives the POST, stores the token, and shuts the listener
   down. The whole flow times out after 3 minutes; any request with the
   wrong nonce is rejected.

The token is written straight to `config.json` — never printed to logs.

## Notification behavior

- Polls every `poll_seconds`, plus immediately after sign-in.
- **First successful poll after signing in** records every currently
  unread id as a baseline and notifies nothing — otherwise every sign-in
  (or app restart with a saved token) would replay a backlog of old
  notifications as if they just happened.
- Every poll after that fires one native notification per notification
  id not already seen, titled by kind: 🚪 Room invite / 💬 DM /
  ❓ Question / ✅ Answer / 👋 Contact request or accepted / 🔒 Room
  closed.
- **Click-to-open is best-effort only.** `tauri-plugin-notification`'s
  click events are not reliably delivered on Windows without registering
  a proper AUMID/WinRT app identity (a whole separate rabbit hole for a
  v1 tray app) — clicking a Windows toast may just dismiss it. macOS and
  Linux are more consistent. Don't rely on it; opening the popup (or the
  tray menu's Open Dashboard) is always the reliable path.

## Project layout

```
clients/desktop/
├── README.md
├── dist/
│   ├── index.html            # the popup's markup
│   ├── style.css              # dark theme, fixed 360x520
│   └── main.js                 # invoke()-driven rendering, no bundler
├── scripts/gen_icons.py      # one-off generator for src-tauri/icons/*
└── src-tauri/
    ├── Cargo.toml
    ├── build.rs
    ├── tauri.conf.json         # CSP + withGlobalTauri for the popup
    ├── icons/                 # app bundle icons + embedded tray PNGs
    └── src/
        ├── main.rs            # setup: plugins, tray build, menu wiring, poll loop, IPC commands
        ├── state.rs            # Config (on-disk) + RuntimeState (in-memory) + AppState (+ avatar cache)
        ├── api.rs              # reqwest client for the dashboard's tray API
        ├── auth.rs             # loopback OAuth (tiny_http listener + nonce)
        ├── tray.rs             # tray icon + menu construction/refresh
        ├── notify.rs           # dedupe + native notifications per kind
        ├── popup.rs            # popup window lifecycle, positioning, state snapshot, eval-push
        └── avatar.rs           # avatar_url -> cached data: URI resolution
```

## Crates

Tauri v2 core: `tauri` (feature `tray-icon`), `tauri-build`,
`tauri-plugin-notification`, `tauri-plugin-autostart`,
`tauri-plugin-single-instance`, `tauri-plugin-opener`.

Everything else: `tokio` (async runtime, timers), `reqwest`
(`rustls-tls`, `json`), `serde`/`serde_json`, `dirs` (config dir),
`rand` (nonce), `tiny_http` (loopback listener), `webbrowser` (open the
sign-in URL), `urlencoding`, `image` (decode the embedded tray PNGs),
`base64` (encode fetched avatars as `data:` URIs), `chrono`, `log`.

## Building

### Windows

```powershell
# plain binary (fast inner loop)
cd clients/desktop/src-tauri
cargo build --release
# .\target\release\engram-tray.exe

# full .msi bundle
cargo install tauri-cli --version '^2'
cd clients/desktop
cargo tauri build
# -> src-tauri/target/release/bundle/msi/Engram Tray_0.1.0_x64_en-US.msi
```

### macOS (Hiren's MacBook)

```bash
# one-time toolchain setup
curl https://sh.rustup.rs -sSf | sh
cargo install tauri-cli --version '^2'

cd clients/desktop
cargo tauri build
# -> src-tauri/target/release/bundle/dmg/Engram Tray_0.1.0_aarch64.dmg
#    (or x64, depending on the Mac's chip)
```

The bundle isn't code-signed (no Apple Developer ID configured), so on
first launch Gatekeeper will refuse the plain double-click. Instead:
**right-click the `.app` (or the mounted `.dmg`'s app) → Open → Open**,
which adds a one-time exception. After that it launches normally,
including at every subsequent login if "Launch at login" is checked.

### Linux

```bash
# Debian/Ubuntu build deps for the webview + tray + bundlers
sudo apt install -y libwebkit2gtk-4.1-dev libappindicator3-dev \
  librsvg2-dev patchelf libssl-dev build-essential curl wget file

cd clients/desktop
cargo install tauri-cli --version '^2'
cargo tauri build
# -> src-tauri/target/release/bundle/appimage/engram-tray_0.1.0_amd64.AppImage
```

### CI

A GitHub Actions release workflow already exists at
`.github/workflows/desktop.yml` (built separately from this app) and
handles cutting `.msi`/`.dmg`/`.AppImage` releases across all three OSes
— you shouldn't normally need to run `cargo tauri build` by hand except
to test locally.

## Known compromises

- **Notification click-to-open on Windows** is unreliable (see above) —
  documented rather than chased down a WinRT identity rabbit hole.
- **No settings UI for `origin`/`poll_seconds`.** The popup is a real
  window now, but these two are still config-file only — the popup's
  job is presence/notifications, not app preferences.
- **Single fetch of `poll_seconds` at startup** — changing it in
  `config.json` while the app is running has no effect until restart.
- **Popup positioning assumes one monitor's worth of precision.** It
  reads the tray click's own physical position and picks the monitor
  under it via `monitor_from_point`, but doesn't convert the tray icon's
  `rect` (which is in a platform-dependent logical/physical `Position`
  enum) — multi-monitor setups with mixed DPI scaling may land the popup
  slightly off from the icon. Still clamped inside the target monitor's
  work area either way, so it never ends up off-screen.
- **No window transparency/rounded window corners.** The popup content
  has border-radius on its internal elements, but the window itself is
  an opaque rectangle (no `transparent: true`) — avoids the extra
  WebView2/compositor edge cases that come with true window transparency
  on Windows for a first pass.
- **Push updates use `eval`, not `emit`/`listen`.** See "Security model"
  above — this project ships no `capabilities/*.json`, and adding one
  would gate every custom command (not just the plugin ones), so genuine
  live-push went through `WebviewWindow::eval` instead. Functionally
  equivalent for this single-popup app; would need revisiting if a
  second webview surface is ever added.
- The tray-only PNGs (`icons/tray-normal.png` / `tray-unread.png`) and
  the full bundle icon set are placeholder art (a simple violet "node"
  mark, plain vs. with a red badge dot) generated by
  `scripts/gen_icons.py` — swap in real brand art whenever it exists by
  re-running that script against a new source mark, or replacing the
  PNGs directly.

## Live delivery (v0.3)

Notifications are PUSH-latency: instead of polling every minute, the app parks a
request on the server (`/dashboard/api/notifications?wait=50&since=<max id>`);
the server holds it until something NEWER than your cursor lands and answers
immediately (~1-2s from event to toast). A standing unread backlog parks too, so
the client never tight-loops. The team roster refreshes on its own 60s cadence.
On errors the live loop backs off 5s.

Acting on a notification consumes it: clicking a row's action button (Open room
/ Open messages / View) marks THAT notification read (`POST
/dashboard/api/notifications/read` with `{"ids":[id]}`) and then opens the
browser — the row and the badge clear without touching the rest.

Windows toasts: if toasts don't appear but the tray dot updates, check Windows
Settings -> System -> Notifications -> "Engram Tray" is On, and Do Not Disturb /
Focus Assist is off. (Field-verified: with those on, toasts pop natively.)
