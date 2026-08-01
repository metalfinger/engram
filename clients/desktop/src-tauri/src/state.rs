//! Persisted config (JSON in the OS app-config dir) + the in-memory
//! runtime state shared across the tray, poller and menu handlers.

use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::Mutex;

use serde::{Deserialize, Serialize};

use crate::api::TeamMember;

pub const DEFAULT_ORIGIN: &str = "https://engram.metalfinger.xyz";
pub const DEFAULT_POLL_SECONDS: u64 = 60;

fn default_origin() -> String {
    DEFAULT_ORIGIN.to_string()
}

fn default_poll_seconds() -> u64 {
    DEFAULT_POLL_SECONDS
}

/// On-disk config. Never log `token`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    #[serde(default = "default_origin")]
    pub origin: String,
    #[serde(default)]
    pub token: Option<String>,
    #[serde(default = "default_poll_seconds")]
    pub poll_seconds: u64,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            origin: default_origin(),
            token: None,
            poll_seconds: default_poll_seconds(),
        }
    }
}

impl Config {
    pub fn config_path() -> PathBuf {
        let base = dirs::config_dir().unwrap_or_else(std::env::temp_dir);
        base.join("engram-tray").join("config.json")
    }

    pub fn load() -> Self {
        let path = Self::config_path();
        match std::fs::read_to_string(&path) {
            Ok(raw) => serde_json::from_str(&raw).unwrap_or_default(),
            Err(_) => Self::default(),
        }
    }

    pub fn save(&self) -> std::io::Result<()> {
        let path = Self::config_path();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let raw = serde_json::to_string_pretty(self).unwrap_or_default();
        std::fs::write(path, raw)
    }
}

/// Everything the tray menu/icon needs to render itself, kept in one
/// place so a "rebuild the menu" call is always working from a
/// consistent snapshot.
#[derive(Debug, Clone, Default)]
pub struct RuntimeState {
    pub handle: Option<String>,
    pub invisible: bool,
    pub unread_count: u64,
    pub team: Vec<TeamMember>,
    pub autostart_enabled: bool,
    /// Ids already seen since sign-in — first poll after sign-in seeds
    /// this without notifying (backfill rule); every id afterwards that
    /// isn't already in here is "new".
    pub seen_ids: HashSet<String>,
    pub baseline_captured: bool,
}

impl RuntimeState {
    pub fn signed_in(&self) -> bool {
        self.handle.is_some()
    }
}

pub struct AppState {
    pub config: Mutex<Config>,
    pub runtime: Mutex<RuntimeState>,
}

impl AppState {
    pub fn new(config: Config) -> Self {
        Self {
            config: Mutex::new(config),
            runtime: Mutex::new(RuntimeState::default()),
        }
    }

    pub fn origin(&self) -> String {
        self.config.lock().unwrap().origin.clone()
    }

    /// Store a fresh token after a successful sign-in and reset the
    /// per-session notification baseline so the next poll backfills
    /// quietly instead of firing a flood of "new" notifications.
    pub fn set_token(&self, token: String) {
        {
            let mut cfg = self.config.lock().unwrap();
            cfg.token = Some(token);
            let _ = cfg.save();
        }
        let mut rt = self.runtime.lock().unwrap();
        rt.seen_ids.clear();
        rt.baseline_captured = false;
    }

    pub fn clear_token(&self) {
        {
            let mut cfg = self.config.lock().unwrap();
            cfg.token = None;
            let _ = cfg.save();
        }
        let mut rt = self.runtime.lock().unwrap();
        *rt = RuntimeState::default();
    }
}
