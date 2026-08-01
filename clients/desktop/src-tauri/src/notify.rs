//! Turns a fetched notifications list into native OS notifications,
//! honoring the "don't spam on first poll" backfill rule and per-kind
//! title/emoji.

use tauri::AppHandle;
use tauri_plugin_notification::NotificationExt;

use crate::api::NotificationItem;
use crate::state::RuntimeState;

fn title_for_kind(kind: &str) -> &'static str {
    match kind {
        "dm" => "\u{1F4AC} DM",
        "room_invite" => "\u{1F6AA} Room invite",
        "room_closed" => "\u{1F512} Room closed",
        "question" => "\u{2753} Question",
        "answer" => "\u{2705} Answer",
        "contact_request" => "\u{1F44B} Contact request",
        "contact_accepted" => "\u{1F44B} Contact accepted",
        _ => "Engram",
    }
}

/// Given the freshly-fetched unread list, decide what (if anything) to
/// notify, and update `seen_ids`/`baseline_captured` in place.
///
/// First successful poll after sign-in (or app start with a stored
/// token): record every id currently unread as the baseline and notify
/// nothing — otherwise every restart would replay old notifications.
/// Every poll after that: notify only ids not already in `seen_ids`.
pub fn notify_new(app: &AppHandle, rt: &mut RuntimeState, unread: &[NotificationItem]) {
    if !rt.baseline_captured {
        for item in unread {
            rt.seen_ids.insert(item.id.to_string());
        }
        rt.baseline_captured = true;
        return;
    }

    for item in unread {
        if rt.seen_ids.contains(&item.id.to_string()) {
            continue;
        }
        rt.seen_ids.insert(item.id.to_string());
        fire(app, item);
    }
}

fn fire(app: &AppHandle, item: &NotificationItem) {
    let title = title_for_kind(&item.kind);
    let result = app
        .notification()
        .builder()
        .title(title)
        .body(&item.body)
        .show();
    if let Err(e) = result {
        log::warn!("failed to show notification for {}: {e}", item.id);
    }
}
