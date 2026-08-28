//! Local Claude-session registry reader — the tray's per-machine truth.
//!
//! `~/.claude/sessions/<pid>.json` is one file per live Claude Code process
//! (sessionId, /rename name, cwd, status, statusUpdatedAt). The tray reads it
//! to answer the two questions Hiren wants at icon level: HOW MANY sessions
//! are running here, and does ANY need attention.
//!
//! Every rule in this file is adopted from two days of the helix TUI building
//! the same thing and shipping the bugs first (their words: adopt rather than
//! rediscover):
//!
//! - Files OUTLIVE processes and pids get REUSED, so liveness must check that
//!   the pid is a live *claude* process — plain does-the-pid-exist admitted
//!   phantoms.
//! - "waiting" = needs attention (the badge trigger). "shell" means the human
//!   dropped to a terminal on purpose — it demands nothing and does nothing,
//!   and treating it as busy was a shipped bug.
//! - `statusUpdatedAt` is a TRANSITION marker, not a heartbeat: an hour-old
//!   "busy" is still busy, but past ~6h it is a session killed mid-turn that
//!   never wrote idle — display it as idle rather than lie.
//! - Two files can claim one sessionId (stale file + restarted session);
//!   newest `updatedAt` wins.

use std::collections::HashMap;
use std::path::PathBuf;
use std::process::Command;

use serde::{Deserialize, Serialize};

const TRUST_BUSY_MS: f64 = 6.0 * 3600.0 * 1000.0;

#[derive(Debug, Clone, Deserialize)]
struct RegistryFile {
    #[serde(rename = "sessionId")]
    session_id: Option<String>,
    name: Option<String>,
    cwd: Option<String>,
    status: Option<String>,
    #[serde(rename = "statusUpdatedAt")]
    status_updated_at: Option<f64>,
    #[serde(rename = "updatedAt")]
    updated_at: Option<f64>,
    pid: Option<u32>,
}

/// One live session, as the popup renders it.
#[derive(Debug, Clone, Serialize, Default, PartialEq)]
pub struct SessionRow {
    pub name: String,
    pub cwd: String,
    /// busy | waiting | idle | shell — the registry vocabulary, post trust-cap.
    pub status: String,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct SessionsSummary {
    pub total: u64,
    pub waiting: u64,
    pub rows: Vec<SessionRow>,
}

fn registry_dir() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(std::env::temp_dir)
        .join(".claude")
        .join("sessions")
}

/// pid -> lowercase process name, one subprocess per refresh tick. A tick is
/// ~3s and the listing is ~50ms; per-file checks would multiply that by the
/// session count for no benefit.
fn live_processes() -> HashMap<u32, String> {
    let mut out = HashMap::new();

    #[cfg(target_os = "windows")]
    {
        if let Ok(res) = Command::new("tasklist").args(["/FO", "CSV", "/NH"]).output() {
            for line in String::from_utf8_lossy(&res.stdout).lines() {
                let mut cols = line.split("\",\"");
                let name = cols.next().unwrap_or("").trim_start_matches('"');
                let pid = cols.next().unwrap_or("").trim_matches('"');
                if let Ok(pid) = pid.parse::<u32>() {
                    out.insert(pid, name.to_lowercase());
                }
            }
        }
    }

    #[cfg(not(target_os = "windows"))]
    {
        if let Ok(res) = Command::new("ps").args(["-axo", "pid=,comm="]).output() {
            for line in String::from_utf8_lossy(&res.stdout).lines() {
                let trimmed = line.trim();
                if let Some((pid, comm)) = trimmed.split_once(char::is_whitespace) {
                    if let Ok(pid) = pid.trim().parse::<u32>() {
                        out.insert(pid, comm.trim().to_lowercase());
                    }
                }
            }
        }
    }

    out
}

fn now_ms() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as f64)
        .unwrap_or(0.0)
}

/// Read the registry and reduce it to what the tray needs. Failure-soft
/// end to end: no registry dir, unreadable files, no process listing — all
/// yield an empty summary, and the tray simply shows no session segment.
pub fn read() -> SessionsSummary {
    let dir = registry_dir();
    let entries = match std::fs::read_dir(&dir) {
        Ok(e) => e,
        Err(_) => return SessionsSummary::default(),
    };
    let procs = live_processes();

    // sessionId -> best file (newest updatedAt wins on duplicates).
    let mut best: HashMap<String, RegistryFile> = HashMap::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("json") {
            continue;
        }
        let Ok(raw) = std::fs::read_to_string(&path) else { continue };
        let Ok(file) = serde_json::from_str::<RegistryFile>(&raw) else { continue };
        let Some(sid) = file.session_id.clone() else { continue };

        // Liveness gate, the phantom rule: the pid must be a live process whose
        // name says claude. A dead pid, a reused pid now running something
        // else, or no pid at all — the file is a leftover, not a session.
        let live = file
            .pid
            .and_then(|pid| procs.get(&pid))
            .map(|name| name.contains("claude"))
            .unwrap_or(false);
        if !live {
            continue;
        }

        match best.get(&sid) {
            Some(prev) if prev.updated_at.unwrap_or(0.0) >= file.updated_at.unwrap_or(0.0) => {}
            _ => {
                best.insert(sid, file);
            }
        }
    }

    let now = now_ms();
    let mut rows: Vec<SessionRow> = best
        .values()
        .map(|f| {
            let mut status = f.status.clone().unwrap_or_default();
            let updated = f.status_updated_at.unwrap_or(0.0);
            if status == "busy" && updated > 0.0 && now - updated > TRUST_BUSY_MS {
                status = "idle".to_string();
            }
            SessionRow {
                name: f.name.clone().unwrap_or_else(|| "unnamed".to_string()),
                cwd: f.cwd.clone().unwrap_or_default(),
                status,
            }
        })
        .collect();
    // Waiting first (they are the point), then by name — a stable order so the
    // popup does not reshuffle under the cursor every tick.
    rows.sort_by(|a, b| {
        (b.status == "waiting")
            .cmp(&(a.status == "waiting"))
            .then_with(|| a.name.cmp(&b.name))
    });

    let waiting = rows.iter().filter(|r| r.status == "waiting").count() as u64;
    SessionsSummary {
        total: rows.len() as u64,
        waiting,
        rows,
    }
}
