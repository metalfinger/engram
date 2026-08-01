//! The popup window: a small local-HTML surface carrying the "rich" tray
//! content (friend list, avatars, actionable notifications). It renders
//! `dist/index.html` — it never loads the remote dashboard and never
//! sees the bearer token — and talks to Rust only through the `invoke`
//! commands registered in `main.rs`.
//!
//! State reaches the popup two ways:
//! - Every IPC command that changes something returns a fresh
//!   [`PopupState`] so the caller can re-render immediately.
//! - The background poll loop calls [`push_state`] after every tick,
//!   which `eval`s the payload straight into the popup's JS
//!   (`window.__engramPush`) instead of using Tauri's event/listen
//!   bridge. That's deliberate: `listen()` from JS is implemented as a
//!   `plugin:event|listen` IPC call, which the ACL system gates unless
//!   the app ships a capabilities manifest — and this project
//!   intentionally has none (see the note in `main.rs`). `eval` is a
//!   direct Rust -> webview call that bypasses the IPC/ACL layer
//!   entirely, so it needs no such manifest.

use std::collections::HashMap;

use serde::Serialize;
use tauri::{AppHandle, Manager, Monitor, PhysicalPosition, PhysicalSize, Position, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};

use crate::api::{NotificationItem, TeamMember};
use crate::state::AppState;

pub const LABEL: &str = "popup";
const WIDTH: f64 = 360.0;
const HEIGHT: f64 = 520.0;
const MARGIN: i32 = 8;

#[derive(Debug, Clone, Serialize, Default)]
pub struct FriendCard {
    pub handle: String,
    pub display_name: String,
    pub avatar_data: Option<String>,
    pub project: Option<String>,
    pub minutes_ago: f64,
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct NotificationRow {
    pub id: String,
    pub kind: String,
    pub body: String,
    pub at: String,
    #[serde(rename = "ref")]
    pub reference: Option<String>,
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct MeInfo {
    pub handle: Option<String>,
    pub avatar_data: Option<String>,
    pub project: Option<String>,
    pub invisible: bool,
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct PopupState {
    pub signed_in: bool,
    pub origin: String,
    pub unread_count: u64,
    pub notifications: Vec<NotificationRow>,
    pub team: Vec<FriendCard>,
    pub me: MeInfo,
}

fn avatar_for(cache: &HashMap<String, String>, member: &TeamMember) -> Option<String> {
    member.avatar_url.as_ref().and_then(|u| cache.get(u).cloned())
}

fn notification_row(item: &NotificationItem) -> NotificationRow {
    NotificationRow {
        id: item.id.to_string(),
        kind: item.kind.clone(),
        body: item.body.clone(),
        at: item.at.clone(),
        reference: item.reference.clone(),
    }
}

/// Build the popup's JSON snapshot from current state. Pure/sync — all
/// avatar fetching already happened during the last poll (see
/// `avatar::resolve`, called from `main::poll_once`).
pub fn build_state(state: &AppState) -> PopupState {
    let origin = state.config.lock().unwrap().origin.clone();
    let rt = state.runtime.lock().unwrap();
    let cache = state.avatar_cache.lock().unwrap();

    let me_handle = rt.handle.clone();
    let me_row = me_handle.as_ref().and_then(|h| rt.team.iter().find(|m| &m.handle == h));

    let me = MeInfo {
        handle: me_handle.clone(),
        avatar_data: me_row.and_then(|m| avatar_for(&cache, m)),
        project: me_row.and_then(|m| m.project.clone()),
        invisible: rt.invisible,
    };

    // "Friends" excludes me — my own row is already covered by the
    // header above.
    let team = rt
        .team
        .iter()
        .filter(|m| Some(&m.handle) != me_handle.as_ref())
        .map(|m| FriendCard {
            handle: m.handle.clone(),
            display_name: m.display_name.clone().unwrap_or_else(|| m.handle.clone()),
            avatar_data: avatar_for(&cache, m),
            project: m.project.clone(),
            minutes_ago: m.minutes_ago,
        })
        .collect();

    let notifications = rt.notifications.iter().map(notification_row).collect();

    PopupState {
        signed_in: rt.signed_in(),
        origin,
        unread_count: rt.unread_count,
        notifications,
        team,
        me,
    }
}

/// Create the popup window, hidden. Called once at startup from
/// `.setup()` — never recreated; toggling only shows/hides it.
pub fn create_window(app: &AppHandle) -> tauri::Result<()> {
    let window = WebviewWindowBuilder::new(app, LABEL, WebviewUrl::App("index.html".into()))
        .title("Engram")
        .inner_size(WIDTH, HEIGHT)
        .resizable(false)
        .decorations(false)
        .always_on_top(true)
        .skip_taskbar(true)
        .visible(false)
        .shadow(true)
        .build()?;

    // Hide on focus loss, like any other tray popup.
    let hide_on_blur = window.clone();
    window.on_window_event(move |event| {
        if let WindowEvent::Focused(false) = event {
            let _ = hide_on_blur.hide();
        }
    });

    Ok(())
}

pub fn hide(app: &AppHandle) {
    if let Some(w) = app.get_webview_window(LABEL) {
        let _ = w.hide();
    }
}

/// Toggle the popup, anchoring it near `anchor` (the tray click's
/// physical position) when one is available — e.g. not when toggled
/// from a menu item, which carries no click position.
pub fn toggle(app: &AppHandle, anchor: Option<PhysicalPosition<f64>>) {
    let Some(window) = app.get_webview_window(LABEL) else {
        return;
    };
    if window.is_visible().unwrap_or(false) {
        let _ = window.hide();
        return;
    }
    position(app, &window, anchor);
    let _ = window.show();
    let _ = window.set_focus();
}

/// Place the popup near the tray icon click. Windows/Linux tray icons
/// live in a bottom taskbar (open upward); macOS's menu bar is at the
/// top (open downward) — inferred here from which half of the monitor
/// the click landed in, rather than a `cfg(target_os)` split, so a
/// single code path covers both. Falls back to a fixed bottom-right
/// corner of the primary monitor when no click position is available
/// (e.g. toggled from the tray menu) or monitor lookup fails.
fn position(app: &AppHandle, window: &WebviewWindow, anchor: Option<PhysicalPosition<f64>>) {
    let size = window.outer_size().unwrap_or(PhysicalSize::new(WIDTH as u32, HEIGHT as u32));
    let pw = size.width as i32;
    let ph = size.height as i32;

    let monitor: Option<Monitor> = anchor
        .and_then(|p| app.monitor_from_point(p.x, p.y).ok().flatten())
        .or_else(|| window.current_monitor().ok().flatten())
        .or_else(|| app.primary_monitor().ok().flatten());

    let (mx, my, mw, mh) = monitor
        .map(|m| {
            let wa = m.work_area();
            (wa.position.x, wa.position.y, wa.size.width as i32, wa.size.height as i32)
        })
        .unwrap_or((0, 0, 1920, 1080));

    let (mut x, mut y) = match anchor {
        Some(p) => {
            let ax = p.x as i32;
            let ay = p.y as i32;
            let open_upward = ay > my + mh / 2;
            let x = ax - pw / 2;
            let y = if open_upward { ay - ph - MARGIN } else { ay + MARGIN };
            (x, y)
        }
        None => (mx + mw - pw - 16, my + mh - ph - 16),
    };

    x = x.clamp(mx + MARGIN, (mx + mw - pw - MARGIN).max(mx + MARGIN));
    y = y.clamp(my + MARGIN, (my + mh - ph - MARGIN).max(my + MARGIN));

    let _ = window.set_position(Position::Physical(PhysicalPosition::new(x, y)));
}

/// Push a fresh snapshot into the popup's JS. See the module doc for
/// why this goes through `eval` rather than `emit`/`listen`.
pub fn push_state(app: &AppHandle) {
    let Some(window) = app.get_webview_window(LABEL) else {
        return;
    };
    let state = build_state(&app.state::<AppState>());
    let Ok(json) = serde_json::to_string(&state) else {
        return;
    };
    let _ = window.eval(format!("window.__engramPush && window.__engramPush({json});"));
}
