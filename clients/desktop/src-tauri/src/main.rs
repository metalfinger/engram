// Prevent an extra console window on Windows release builds. Debug builds
// keep the console so `log`/eprintln! output is visible while developing.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod api;
mod auth;
mod notify;
mod state;
mod tray;

use std::time::Duration;

use tauri::menu::MenuEvent;
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager};
use tauri_plugin_autostart::ManagerExt as AutostartManagerExt;
use tauri_plugin_opener::OpenerExt;

use api::{ApiClient, ApiError};
use state::{AppState, Config};

fn origin_of(app: &AppHandle) -> String {
    app.state::<AppState>().origin()
}

fn open_url(app: &AppHandle, url: &str) {
    if let Err(e) = app.opener().open_url(url, None::<&str>) {
        log::warn!("failed to open {url}: {e}");
    }
}

/// Clear the stored token and repaint the tray as signed-out. Used both
/// for an explicit "Sign out" click and for a 401 from the API.
fn handle_sign_out(app: &AppHandle) {
    let state = app.state::<AppState>();
    state.clear_token();
    let snapshot = state.runtime.lock().unwrap().clone();
    if let Err(e) = tray::refresh_tray(app, &snapshot) {
        log::warn!("failed to refresh tray after sign-out: {e}");
    }
}

/// One fetch cycle: notifications + roster. Fires native notifications
/// for anything new, then repaints the tray with the latest snapshot.
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

    let notifications = client.fetch_notifications().await;
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

    let snapshot = state.runtime.lock().unwrap().clone();
    if let Err(e) = tray::refresh_tray(app, &snapshot) {
        log::warn!("failed to refresh tray: {e}");
    }
}

fn spawn_poll_loop(app: AppHandle, poll_seconds: u64) {
    tauri::async_runtime::spawn(async move {
        loop {
            poll_once(&app).await;
            tokio::time::sleep(Duration::from_secs(poll_seconds.max(5))).await;
        }
    });
}

fn start_sign_in(app: AppHandle) {
    let origin = origin_of(&app);
    tauri::async_runtime::spawn(async move {
        let result = tokio::task::spawn_blocking(move || auth::run_sign_in_flow_blocking(&origin)).await;
        match result {
            Ok(Ok(token)) => {
                let state = app.state::<AppState>();
                state.set_token(token);
                // Immediate poll so the tray flips to signed-in right
                // away instead of waiting out the poll interval; this
                // is also the "first poll after sign-in" that seeds the
                // notification baseline.
                poll_once(&app).await;
            }
            Ok(Err(e)) => log::warn!("sign-in failed: {e}"),
            Err(e) => log::warn!("sign-in task panicked: {e}"),
        }
    });
}

fn toggle_invisible(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        let state = app.state::<AppState>();
        let (origin, token, next) = {
            let cfg = state.config.lock().unwrap();
            let rt = state.runtime.lock().unwrap();
            (cfg.origin.clone(), cfg.token.clone(), !rt.invisible)
        };
        let Some(token) = token else { return };
        let client = ApiClient::new(&origin, &token);
        match client.set_presence(next).await {
            Ok(resp) => {
                {
                    let mut rt = state.runtime.lock().unwrap();
                    rt.invisible = resp.invisible;
                }
                let snapshot = state.runtime.lock().unwrap().clone();
                let _ = tray::refresh_tray(&app, &snapshot);
            }
            Err(ApiError::Unauthorized) => handle_sign_out(&app),
            Err(e) => log::warn!("failed to toggle invisible: {e}"),
        }
    });
}

fn mark_all_read(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        let state = app.state::<AppState>();
        let (origin, token) = {
            let cfg = state.config.lock().unwrap();
            (cfg.origin.clone(), cfg.token.clone())
        };
        let Some(token) = token else { return };
        let client = ApiClient::new(&origin, &token);
        match client.mark_all_read().await {
            Ok(()) => {
                {
                    let mut rt = state.runtime.lock().unwrap();
                    rt.unread_count = 0;
                }
                let snapshot = state.runtime.lock().unwrap().clone();
                let _ = tray::refresh_tray(&app, &snapshot);
            }
            Err(ApiError::Unauthorized) => handle_sign_out(&app),
            Err(e) => log::warn!("failed to mark all read: {e}"),
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
        tray::ID_OPEN_DASHBOARD => open_url(app, &format!("{}/dashboard", origin_of(app))),
        tray::ID_OPEN_ROOMS => open_url(app, &format!("{}/dashboard/rooms", origin_of(app))),
        tray::ID_MARK_ALL_READ => mark_all_read(app.clone()),
        tray::ID_INVISIBLE => toggle_invisible(app.clone()),
        tray::ID_SIGN_IN => start_sign_in(app.clone()),
        tray::ID_SIGN_OUT => handle_sign_out(app),
        tray::ID_LAUNCH_AT_LOGIN => toggle_launch_at_login(app.clone()),
        tray::ID_QUIT => app.exit(0),
        _ => {}
    }
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

            let initial_snapshot = app.state::<AppState>().runtime.lock().unwrap().clone();
            let initial_menu = tray::build_menu(app.handle(), &initial_snapshot)?;

            let handle_for_menu = app.handle().clone();
            TrayIconBuilder::with_id("main")
                .icon(tray::normal_icon())
                .tooltip("Engram — signed out")
                .menu(&initial_menu)
                .show_menu_on_left_click(true)
                .on_menu_event(move |_tray_app, event| handle_menu_event(&handle_for_menu, event))
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
