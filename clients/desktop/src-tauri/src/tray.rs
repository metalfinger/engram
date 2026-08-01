//! Tray icon + menu construction. The menu is rebuilt from scratch on
//! every state change (sign-in/out, roster refresh, unread count) rather
//! than mutated in place — Tauri's menu items are cheap enough and this
//! keeps the "what does the tray show right now" logic in one place.

use tauri::menu::{CheckMenuItemBuilder, Menu, MenuBuilder, MenuItemBuilder, PredefinedMenuItem};
use tauri::{AppHandle, Wry};

use crate::state::RuntimeState;

pub const ID_INVISIBLE: &str = "invisible";
pub const ID_OPEN_DASHBOARD: &str = "open_dashboard";
pub const ID_OPEN_ROOMS: &str = "open_rooms";
pub const ID_MARK_ALL_READ: &str = "mark_all_read";
pub const ID_SIGN_IN: &str = "sign_in";
pub const ID_SIGN_OUT: &str = "sign_out";
pub const ID_LAUNCH_AT_LOGIN: &str = "launch_at_login";
pub const ID_QUIT: &str = "quit";

const TRAY_NORMAL: &[u8] = include_bytes!("../icons/tray-normal.png");
const TRAY_UNREAD: &[u8] = include_bytes!("../icons/tray-unread.png");

const MAX_ROSTER_ROWS: usize = 6;

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

fn fmt_minutes_ago(m: f64) -> String {
    let mins = m.round() as i64;
    if mins <= 0 {
        "just now".to_string()
    } else if mins == 1 {
        "1m".to_string()
    } else {
        format!("{mins}m")
    }
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

    if snapshot.signed_in() {
        builder = builder.item(&PredefinedMenuItem::separator(app)?);
        if snapshot.team.is_empty() {
            builder = builder.item(
                &MenuItemBuilder::with_id("roster_empty", "Nobody active right now")
                    .enabled(false)
                    .build(app)?,
            );
        } else {
            for (i, member) in snapshot.team.iter().take(MAX_ROSTER_ROWS).enumerate() {
                let dot = if member.minutes_ago <= 15.0 { "\u{25CF}" } else { "\u{25CB}" };
                let name = member.display_name.clone().unwrap_or_else(|| member.handle.clone());
                let project = member.project.clone().unwrap_or_else(|| "—".to_string());
                let label = format!("{dot} {name} — {project} ({})", fmt_minutes_ago(member.minutes_ago));
                builder = builder.item(
                    &MenuItemBuilder::with_id(format!("roster_{i}"), label)
                        .enabled(false)
                        .build(app)?,
                );
            }
        }

        builder = builder.item(&PredefinedMenuItem::separator(app)?);
        builder = builder.item(
            &CheckMenuItemBuilder::with_id(ID_INVISIBLE, "Appear invisible")
                .checked(snapshot.invisible)
                .build(app)?,
        );
    }

    builder = builder.item(&PredefinedMenuItem::separator(app)?);
    builder = builder.item(&MenuItemBuilder::with_id(ID_OPEN_DASHBOARD, "Open Dashboard").build(app)?);
    builder = builder.item(&MenuItemBuilder::with_id(ID_OPEN_ROOMS, "Open Rooms").build(app)?);
    if snapshot.signed_in() {
        let mark_read_label = if snapshot.unread_count > 0 {
            format!("Mark all read ({})", snapshot.unread_count)
        } else {
            "Mark all read".to_string()
        };
        builder = builder.item(
            &MenuItemBuilder::with_id(ID_MARK_ALL_READ, mark_read_label)
                .enabled(snapshot.unread_count > 0)
                .build(app)?,
        );
    }

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
