//! Tray icon + menu construction. The menu is rebuilt from scratch on
//! every state change (sign-in/out, roster refresh, unread count) rather
//! than mutated in place — Tauri's menu items are cheap enough and this
//! keeps the "what does the tray show right now" logic in one place.
//!
//! v1.1: the roster, "appear invisible" toggle, and "mark all read" moved
//! into the popup (see `popup.rs`) — this menu now only carries what
//! still makes sense as a plain right-click list: header, popup toggle,
//! dashboard link, sign in/out, launch-at-login, quit.

use tauri::menu::{CheckMenuItemBuilder, Menu, MenuBuilder, MenuItemBuilder, PredefinedMenuItem};
use tauri::{AppHandle, Wry};

use crate::state::RuntimeState;

pub const ID_OPEN_ENGRAM: &str = "open_engram";
pub const ID_OPEN_DASHBOARD: &str = "open_dashboard";
pub const ID_SIGN_IN: &str = "sign_in";
pub const ID_SIGN_OUT: &str = "sign_out";
pub const ID_LAUNCH_AT_LOGIN: &str = "launch_at_login";
pub const ID_QUIT: &str = "quit";

const TRAY_NORMAL: &[u8] = include_bytes!("../icons/tray-normal.png");
const TRAY_UNREAD: &[u8] = include_bytes!("../icons/tray-unread.png");

/// Decode an embedded PNG into a Tauri tray image at startup. Panics only
/// if the checked-in icon assets themselves are corrupt, which would also
/// fail CI immediately.
pub fn decode_png(bytes: &[u8]) -> tauri::image::Image<'static> {
    let decoded = image::load_from_memory(bytes)
        .expect("embedded tray icon PNG must decode")
        .to_rgba8();
    let (width, height) = decoded.dimensions();
    tauri::image::Image::new_owned(decoded.into_raw(), width, height)
}

pub fn normal_icon() -> tauri::image::Image<'static> {
    decode_png(TRAY_NORMAL)
}

pub fn unread_icon() -> tauri::image::Image<'static> {
    decode_png(TRAY_UNREAD)
}

pub fn build_menu(app: &AppHandle, snapshot: &RuntimeState) -> tauri::Result<Menu<Wry>> {
    let mut builder = MenuBuilder::new(app);

    let header_text = match &snapshot.handle {
        Some(handle) => format!("Engram — @{handle}"),
        None => "Engram — signed out".to_string(),
    };
    builder = builder.item(
        &MenuItemBuilder::with_id("header", header_text)
            .enabled(false)
            .build(app)?,
    );

    builder = builder.item(&PredefinedMenuItem::separator(app)?);
    builder = builder.item(&MenuItemBuilder::with_id(ID_OPEN_ENGRAM, "Open Engram").build(app)?);
    builder = builder.item(&MenuItemBuilder::with_id(ID_OPEN_DASHBOARD, "Open Dashboard").build(app)?);

    builder = builder.item(&PredefinedMenuItem::separator(app)?);
    if snapshot.signed_in() {
        builder = builder.item(&MenuItemBuilder::with_id(ID_SIGN_OUT, "Sign out").build(app)?);
    } else {
        builder = builder.item(&MenuItemBuilder::with_id(ID_SIGN_IN, "Sign in\u{2026}").build(app)?);
    }
    builder = builder.item(
        &CheckMenuItemBuilder::with_id(ID_LAUNCH_AT_LOGIN, "Launch at login")
            .checked(snapshot.autostart_enabled)
            .build(app)?,
    );

    builder = builder.item(&PredefinedMenuItem::separator(app)?);
    builder = builder.item(&MenuItemBuilder::with_id(ID_QUIT, "Quit").build(app)?);

    builder.build()
}

/// Rebuild the menu from the current snapshot and push new icon/tooltip
/// state onto the tray. Called after every state-changing event: sign
/// in/out, roster refresh, unread-count change, invisible toggle.
pub fn refresh_tray(app: &AppHandle, snapshot: &RuntimeState) -> tauri::Result<()> {
    let tray = app
        .tray_by_id("main")
        .expect("main tray icon registered at startup");

    let menu = build_menu(app, snapshot)?;
    tray.set_menu(Some(menu))?;

    let icon = if snapshot.unread_count > 0 {
        unread_icon()
    } else {
        normal_icon()
    };
    tray.set_icon(Some(icon))?;

    let tooltip = if snapshot.signed_in() {
        format!("Engram — {} unread", snapshot.unread_count)
    } else {
        "Engram — signed out".to_string()
    };
    tray.set_tooltip(Some(tooltip))?;

    Ok(())
}
