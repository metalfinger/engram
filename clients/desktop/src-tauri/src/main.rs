// Prevent an extra console window on Windows release builds. Debug builds
// keep the console so `log`/eprintln! output is visible while developing.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod api;
mod auth;
mod avatar;
mod notify;
mod popup;
mod state;
mod tray;

use std::time::Duration;

use tauri::menu::MenuEvent;
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager};
use tauri_plugin_autostart::ManagerExt as AutostartManagerExt;
use tauri_plugin_opener::OpenerExt;

use api::{ApiClient, ApiError};
use state::{AppState, Config};

fn origin_of(app: &AppHandle) -> String {
    app.state::<AppState>().origin()
}

/// Open a URL in the user's default browser. Only ever called with
/// URLs we built ourselves from a trusted origin — see `open_url` (the
/// IPC command below) for the whitelist that guards paths coming from
/// the popup's webview.
fn open_external(app: &AppHandle, url: &str) {
    if let Err(e) = app.opener().open_url(url, None::<&str>) {
        log::warn!("failed to open {url}: {e}");
    }
}

/// Clear the stored token and repaint the tray + popup as signed-out.
/// Used for an explicit "Sign out", a 401 from the API, or the popup's
/// own `sign_out` command.
fn handle_sign_out(app: &AppHandle) {
    let state = app.state::<AppState>();
    state.clear_token();
    let snapshot = state.runtime.lock().unwrap().clone();
    if let Err(e) = tray::refresh_tray(app, &snapshot) {
        log::warn!("failed to refresh tray after sign-out: {e}");
    }
    popup::push_state(app);
}

/// One fetch cycle: notifications + roster. Fires native notifications
/// for anything new, resolves any unseen avatar urls, then repaints the
/// tray and pushes a fresh snapshot to the popup (if it's around to see
/// it — `push_state` is a no-op otherwise).
async fn poll_once(app: &AppHandle) {
    let state = app.state::<AppState>();
    let (origin, token) = {
        let cfg = state.config.lock().unwrap();
        (cfg.origin.clone(), cfg.token.clone())
    };
    let Some(token) = token else {
        return;
    };
    let client = ApiClient::new(&origin, &token);

    let notifications = client.fetch_notifications(0, 0).await;
    let team = client.fetch_team().await;

    if matches!(notifications, Err(ApiError::Unauthorized)) || matches!(team, Err(ApiError::Unauthorized)) {
        handle_sign_out(app);
        return;
    }

    {
        let mut rt = state.runtime.lock().unwrap();
        match notifications {
            Ok(resp) => {
                rt.unread_count = resp.unread.len() as u64;
                notify::notify_new(app, &mut rt, &resp.unread);
                rt.notifications = resp.unread;
            }
            Err(e) => log::warn!("notifications poll failed: {e}"),
        }
        match team {
            Ok(resp) => {
                rt.team = resp.team;
                if let Some(me) = resp.me {
                    rt.handle = Some(me.handle);
                    rt.invisible = me.invisible;
                }
            }
            Err(e) => log::warn!("team poll failed: {e}"),
        }
    }

    // Resolve any avatar urls the team roster just handed us into cached
    // data uris — all network work happens here, once per poll, never
    // from the popup's webview.
    let avatar_urls: Vec<String> = {
        let rt = state.runtime.lock().unwrap();
        rt.team.iter().filter_map(|m| m.avatar_url.clone()).collect()
    };
    // SSRF hardening (see avatar.rs's module doc): each url gets its own
    // client, built only after the hostname resolves to addresses that
    // pass the public-address check, and pinned to exactly those
    // addresses — no redirects, no second/independent DNS lookup.
    for url in avatar_urls {
        avatar::resolve(&state.avatar_cache, &url).await;
    }

    let snapshot = state.runtime.lock().unwrap().clone();
    if let Err(e) = tray::refresh_tray(app, &snapshot) {
        log::warn!("failed to refresh tray: {e}");
    }
    popup::push_state(app);
}

fn spawn_poll_loop(app: AppHandle, poll_seconds: u64) {
    tauri::async_runtime::spawn(async move {
        // First cycle is instant (baseline). After that, LIVE: each request
        // parks server-side (?wait=50&since=<max id>) and returns the moment
        // something new lands — push latency, no tight polling. The team
        // roster doesn't need push; it refreshes when a cycle ends and 60s+
        // have passed. poll_seconds still paces the signed-out retry.
        let mut last_full = std::time::Instant::now();
        poll_once(&app).await;
        loop {
            let (signed_in, since) = {
                let state = app.state::<AppState>();
                let has_token = state.config.lock().unwrap().token.is_some();
                let rt = state.runtime.lock().unwrap();
                let max_id = rt.notifications.iter().map(|n| n.id).max().unwrap_or(0);
                (has_token, max_id)
            };
            if !signed_in {
                tokio::time::sleep(Duration::from_secs(poll_seconds.max(5))).await;
                poll_once(&app).await;
                continue;
            }
            live_cycle(&app, since).await;
            if last_full.elapsed() >= Duration::from_secs(60) {
                last_full = std::time::Instant::now();
                poll_once(&app).await; // team roster + avatars refresh
            }
            tokio::time::sleep(Duration::from_secs(1)).await; // breather, not a poll
        }
    });
}

/// One LIVE cycle: park on the server until a notification newer than `since`
/// arrives (or ~50s lapse), then process exactly like a poll result.
async fn live_cycle(app: &AppHandle, since: i64) {
    let state = app.state::<AppState>();
    let (origin, token) = {
        let cfg = state.config.lock().unwrap();
        (cfg.origin.clone(), cfg.token.clone())
    };
    let Some(token) = token else { return };
    let client = ApiClient::new(&origin, &token);
    match client.fetch_notifications(since, 50).await {
        Err(ApiError::Unauthorized) => handle_sign_out(app),
        Err(e) => {
            log::warn!("live notifications cycle failed: {e}");
            tokio::time::sleep(Duration::from_secs(5)).await; // don't hammer on errors
        }
        Ok(resp) => {
            {
                let mut rt = state.runtime.lock().unwrap();
                rt.unread_count = resp.unread.len() as u64;
                notify::notify_new(app, &mut rt, &resp.unread);
                rt.notifications = resp.unread;
            }
            let snapshot = state.runtime.lock().unwrap().clone();
            if let Err(e) = tray::refresh_tray(app, &snapshot) {
                log::warn!("failed to refresh tray: {e}");
            }
            popup::push_state(app);
        }
    }
}

fn start_sign_in(app: AppHandle) {
    let origin = origin_of(&app);
    tauri::async_runtime::spawn(async move {
        let result = tokio::task::spawn_blocking(move || auth::run_sign_in_flow_blocking(&origin)).await;
        match result {
            Ok(Ok(token)) => {
                let state = app.state::<AppState>();
                state.set_token(token);
                // Immediate poll so the tray/popup flip to signed-in right
                // away instead of waiting out the poll interval; this is
                // also the "first poll after sign-in" that seeds the
                // notification baseline.
                poll_once(&app).await;
            }
            Ok(Err(e)) => log::warn!("sign-in failed: {e}"),
            Err(e) => log::warn!("sign-in task panicked: {e}"),
        }
    });
}

fn toggle_launch_at_login(app: AppHandle) {
    let manager = app.autolaunch();
    let currently_enabled = manager.is_enabled().unwrap_or(false);
    let result = if currently_enabled {
        manager.disable()
    } else {
        manager.enable()
    };
    if let Err(e) = result {
        log::warn!("failed to toggle launch-at-login: {e}");
        return;
    }
    let state = app.state::<AppState>();
    {
        let mut rt = state.runtime.lock().unwrap();
        rt.autostart_enabled = !currently_enabled;
    }
    let snapshot = state.runtime.lock().unwrap().clone();
    let _ = tray::refresh_tray(&app, &snapshot);
}

fn handle_menu_event(app: &AppHandle, event: MenuEvent) {
    let id = event.id().as_ref();
    match id {
        tray::ID_OPEN_ENGRAM => popup::toggle(app, None),
        tray::ID_OPEN_DASHBOARD => open_external(app, &format!("{}/dashboard", origin_of(app))),
        tray::ID_SIGN_IN => start_sign_in(app.clone()),
        tray::ID_SIGN_OUT => handle_sign_out(app),
        tray::ID_LAUNCH_AT_LOGIN => toggle_launch_at_login(app.clone()),
        tray::ID_QUIT => app.exit(0),
        _ => {}
    }
}

fn handle_tray_icon_event(app: &AppHandle, event: TrayIconEvent) {
    // Left click toggles the popup near the click point; right click
    // falls through to the native context menu (handled automatically
    // by the tray icon since `show_menu_on_left_click` is false).
    if let TrayIconEvent::Click {
        button: MouseButton::Left,
        button_state: MouseButtonState::Up,
        position,
        ..
    } = event
    {
        popup::toggle(app, Some(position));
    }
}

// ---------------------------------------------------------------------
// Popup IPC commands.
//
// This is the entire surface the popup's webview can reach — it never
// sees the bearer token and never talks to the dashboard directly. None
// of these need a `capabilities/*.json` manifest: Tauri's ACL system
// only gates plugin-provided commands (or *any* command, once the app
// defines its own ACL manifest) — see `webview/mod.rs`'s invoke handler
// in the `tauri` crate for the exact condition. These are plain
// `#[tauri::command]`s registered directly on the app, the project
// defines no capabilities, and the popup only ever loads local content,
// so `invoke` reaches every one of them unconditionally. Keep it that
// way: adding a capabilities file would silently start gating these too.
// ---------------------------------------------------------------------

#[tauri::command]
fn get_state(app: AppHandle) -> popup::PopupState {
    popup::build_state(&app.state::<AppState>())
}

#[tauri::command]
async fn refresh_now(app: AppHandle) -> popup::PopupState {
    poll_once(&app).await;
    popup::build_state(&app.state::<AppState>())
}

#[tauri::command]
async fn set_invisible(app: AppHandle, invisible: bool) -> popup::PopupState {
    let state = app.state::<AppState>();
    let (origin, token) = {
        let cfg = state.config.lock().unwrap();
        (cfg.origin.clone(), cfg.token.clone())
    };
    if let Some(token) = token {
        let client = ApiClient::new(&origin, &token);
        match client.set_presence(invisible).await {
            Ok(resp) => {
                state.runtime.lock().unwrap().invisible = resp.invisible;
            }
            Err(ApiError::Unauthorized) => handle_sign_out(&app),
            Err(e) => log::warn!("failed to set invisible: {e}"),
        }
        let snapshot = state.runtime.lock().unwrap().clone();
        let _ = tray::refresh_tray(&app, &snapshot);
    }
    popup::build_state(&state)
}

#[tauri::command]
async fn mark_all_read(app: AppHandle) -> popup::PopupState {
    let state = app.state::<AppState>();
    let (origin, token) = {
        let cfg = state.config.lock().unwrap();
        (cfg.origin.clone(), cfg.token.clone())
    };
    if let Some(token) = token {
        let client = ApiClient::new(&origin, &token);
        match client.mark_all_read().await {
            Ok(()) => {
                let mut rt = state.runtime.lock().unwrap();
                rt.unread_count = 0;
                rt.notifications.clear();
            }
            Err(ApiError::Unauthorized) => handle_sign_out(&app),
            Err(e) => log::warn!("failed to mark all read: {e}"),
        }
        let snapshot = state.runtime.lock().unwrap().clone();
        let _ = tray::refresh_tray(&app, &snapshot);
    }
    popup::build_state(&state)
}

#[tauri::command]
fn sign_in(app: AppHandle) {
    start_sign_in(app);
}

#[tauri::command]
fn sign_out(app: AppHandle) -> popup::PopupState {
    handle_sign_out(&app);
    popup::build_state(&app.state::<AppState>())
}

/// Path prefixes the popup is allowed to open in the default browser.
/// `open_url` joins `path` onto the configured origin — this whitelist
/// is what stops the webview from ever asking us to open something
/// that isn't the Engram dashboard itself.
const ALLOWED_PATH_PREFIXES: &[&str] = &["/dashboard"];

#[tauri::command]
fn open_url(app: AppHandle, path: String) -> Result<(), String> {
    if !path.starts_with('/') || path.contains("..") || path.contains("://") {
        return Err(format!("rejected non-relative path: {path}"));
    }
    if !ALLOWED_PATH_PREFIXES.iter().any(|prefix| path.starts_with(prefix)) {
        return Err(format!("path not on the allowlist: {path}"));
    }
    let origin = origin_of(&app);
    open_external(&app, &format!("{origin}{path}"));
    Ok(())
}

#[tauri::command]
fn open_config_folder(app: AppHandle) -> Result<(), String> {
    let dir = Config::config_path().parent().map(|p| p.to_path_buf()).ok_or("no config directory")?;
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    app.opener()
        .open_path(dir.to_string_lossy().to_string(), None::<&str>)
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn hide_popup(app: AppHandle) {
    popup::hide(&app);
}

#[tauri::command]
fn quit_app(app: AppHandle) {
    app.exit(0);
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|_app, _argv, _cwd| {
            // A second launch just exits (handled by the plugin itself);
            // there is no window to focus, so nothing to do here.
        }))
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            get_state,
            refresh_now,
            set_invisible,
            mark_all_read,
            sign_in,
            sign_out,
            open_url,
            open_config_folder,
            hide_popup,
            quit_app,
        ])
        .setup(|app| {
            let config = Config::load();
            let poll_seconds = config.poll_seconds;
            let app_state = AppState::new(config);
            app.manage(app_state);

            let autostart_enabled = app.autolaunch().is_enabled().unwrap_or(false);
            {
                let state = app.state::<AppState>();
                let mut rt = state.runtime.lock().unwrap();
                rt.autostart_enabled = autostart_enabled;
            }

            // Created hidden once, up front, and never torn down — the
            // tray toggle only shows/hides it (see popup.rs).
            popup::create_window(app.handle())?;

            let initial_snapshot = app.state::<AppState>().runtime.lock().unwrap().clone();
            let initial_menu = tray::build_menu(app.handle(), &initial_snapshot)?;

            let handle_for_menu = app.handle().clone();
            let handle_for_tray = app.handle().clone();
            TrayIconBuilder::with_id("main")
                .icon(tray::normal_icon())
                .tooltip("Engram — signed out")
                .menu(&initial_menu)
                .show_menu_on_left_click(false)
                .on_menu_event(move |_tray_app, event| handle_menu_event(&handle_for_menu, event))
                .on_tray_icon_event(move |_tray, event| handle_tray_icon_event(&handle_for_tray, event))
                .build(app)?;

            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            spawn_poll_loop(app.handle().clone(), poll_seconds);

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the Engram Tray app")
        .run(|_app_handle, _event| {});
}
