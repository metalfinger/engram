# Engram Tray

Ambient desktop presence for [Engram](https://engram.metalfinger.xyz) — a
system-tray-only app (Windows/macOS/Linux) built with **Rust + Tauri v2**.

v1 is a *thin agent*: it never opens a window or renders any web content.
It shows native OS notifications for room invites, DMs, questions and
answers, and a tray menu with the live team roster. Every menu action
opens the real dashboard in your default browser.

## What it does

- Polls `GET {origin}/dashboard/api/notifications` every `poll_seconds`
  (default 60s) and fires a native notification for anything new.
- Polls `GET {origin}/dashboard/api/team` on the same cadence to show
  who's active right now in the tray menu.
- Tray icon swaps to an "unread" badge variant whenever there's anything
  unread; tooltip shows the count.
- Tray menu: header (`@handle` or "signed out"), up to 6 "working now"
  rows, "Appear invisible" toggle, Open Dashboard / Open Rooms, Mark all
  read, Sign in / Sign out, Launch at login, Quit.
- Sign-in is a loopback OAuth flow: it opens your browser at the
  dashboard's GitHub/Google sign-in, and a short-lived local HTTP
  listener on `127.0.0.1` catches the token when the browser redirects
  back (see "How sign-in works" below).

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
  Linux are more consistent. Don't rely on it; the tray menu ("Open
  Dashboard" / "Open Rooms") is always the reliable path.

## Project layout

```
clients/desktop/
├── README.md
├── dist/index.html          # placeholder frontend Tauri requires but never shows
├── scripts/gen_icons.py      # one-off generator for src-tauri/icons/*
└── src-tauri/
    ├── Cargo.toml
    ├── build.rs
    ├── tauri.conf.json
    ├── icons/                 # app bundle icons + embedded tray PNGs
    └── src/
        ├── main.rs            # setup: plugins, tray build, menu wiring, poll loop
        ├── state.rs            # Config (on-disk) + RuntimeState (in-memory) + AppState
        ├── api.rs              # reqwest client for the dashboard's tray API
        ├── auth.rs             # loopback OAuth (tiny_http listener + nonce)
        ├── tray.rs             # tray icon + menu construction/refresh
        └── notify.rs           # dedupe + native notifications per kind
```

## Crates

Tauri v2 core: `tauri` (feature `tray-icon`), `tauri-build`,
`tauri-plugin-notification`, `tauri-plugin-autostart`,
`tauri-plugin-single-instance`, `tauri-plugin-opener`.

Everything else: `tokio` (async runtime, timers), `reqwest`
(`rustls-tls`, `json`), `serde`/`serde_json`, `dirs` (config dir),
`rand` (nonce), `tiny_http` (loopback listener), `webbrowser` (open the
sign-in URL), `urlencoding`, `image` (decode the embedded tray PNGs),
`chrono`, `log`.

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

## Known compromises (v1)

- **Notification click-to-open on Windows** is unreliable (see above) —
  documented rather than chased down a WinRT identity rabbit hole.
- **No settings UI.** `origin`/`poll_seconds` are config-file only; there
  is deliberately no window to edit them in, per the "thin agent, no
  webview ever" brief.
- **Single fetch of `poll_seconds` at startup** — changing it in
  `config.json` while the app is running has no effect until restart.
- The tray-only PNGs (`icons/tray-normal.png` / `tray-unread.png`) and
  the full bundle icon set are placeholder art (a simple violet "node"
  mark, plain vs. with a red badge dot) generated by
  `scripts/gen_icons.py` — swap in real brand art whenever it exists by
  re-running that script against a new source mark, or replacing the
  PNGs directly.
