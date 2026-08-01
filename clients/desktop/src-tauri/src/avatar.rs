//! Resolves a team member's `avatar_url` into a `data:` URI the popup's
//! webview can render without ever making a network request of its own.
//!
//! `data:` values pass through untouched. `https://` values are fetched
//! here — server side, on the poll cadence — size- and content-type
//! capped, and cached by url so an unchanged avatar is never re-fetched.
//!
//! SECURITY (SSRF): `avatar_url` is OTHER USERS' data — a malicious
//! teammate could point one at an internal address and every teammate's
//! tray would then probe that address from inside their own network. So:
//! https only, the hostname is resolved FIRST and every resolved address
//! must be public (loopback / private / link-local / unique-local / CGNAT
//! ranges are rejected), and redirects are never followed (a 3xx just
//! means "no avatar" — re-validating hops isn't worth the complexity).

use std::collections::HashMap;
use std::net::IpAddr;
use std::sync::Mutex;
use std::time::Duration;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use reqwest::header::CONTENT_TYPE;

const MAX_AVATAR_BYTES: usize = 300 * 1024;
const FETCH_TIMEOUT: Duration = Duration::from_secs(5);

/// Is this an address a teammate's avatar may legitimately live at?
/// Public unicast only — everything internal-ish is refused.
fn ip_is_public(ip: &IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => {
            let o = v4.octets();
            !(v4.is_loopback()
                || v4.is_private()
                || v4.is_link_local()
                || v4.is_unspecified()
                || v4.is_broadcast()
                || o[0] == 100 && (64..=127).contains(&o[1]) // 100.64/10 CGNAT
                || o[0] == 192 && o[1] == 0 && o[2] == 0) // 192.0.0/24 protocol assignments
        }
        IpAddr::V6(v6) => {
            let seg = v6.segments();
            !(v6.is_loopback()
                || v6.is_unspecified()
                || (seg[0] & 0xfe00) == 0xfc00 // fc00::/7 unique-local
                || (seg[0] & 0xffc0) == 0xfe80 // fe80::/10 link-local
                || v6.to_ipv4_mapped().is_some_and(|m| !ip_is_public(&IpAddr::V4(m))))
        }
    }
}

/// https-only + resolves-to-public-addresses-only. `None` = don't fetch.
async fn validated_host_port(url: &str) -> Option<()> {
    let parsed = reqwest::Url::parse(url).ok()?;
    if parsed.scheme() != "https" {
        return None;
    }
    // A literal IP in the URL is checked directly; a hostname is resolved and
    // EVERY address it maps to must be public (an attacker controls their DNS).
    let host = parsed.host_str()?;
    let port = parsed.port_or_known_default().unwrap_or(443);
    if let Ok(ip) = host.parse::<IpAddr>() {
        return ip_is_public(&ip).then_some(());
    }
    let addrs = tokio::net::lookup_host((host, port)).await.ok()?;
    let mut any = false;
    for addr in addrs {
        if !ip_is_public(&addr.ip()) {
            return None;
        }
        any = true;
    }
    any.then_some(())
}

/// Resolve `url` to a `data:` URI, reading/populating `cache` as needed.
/// Returns `None` on any failure (scheme, address class, network, status,
/// size, content-type) — callers fall back to an initials avatar in the UI.
pub async fn resolve(http: &reqwest::Client, cache: &Mutex<HashMap<String, String>>, url: &str) -> Option<String> {
    if url.starts_with("data:image/") {
        return Some(url.to_string());
    }
    if let Some(cached) = cache.lock().unwrap().get(url).cloned() {
        return Some(cached);
    }

    validated_host_port(url).await?;
    let resp = http.get(url).timeout(FETCH_TIMEOUT).send().await.ok()?;
    if !resp.status().is_success() {
        return None; // includes 3xx: the client never follows redirects
    }
    let content_type = resp
        .headers()
        .get(CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or_default()
        .to_string();
    if !content_type.starts_with("image/") {
        return None;
    }
    let bytes = resp.bytes().await.ok()?;
    if bytes.len() > MAX_AVATAR_BYTES {
        return None;
    }

    let data_uri = format!("data:{content_type};base64,{}", STANDARD.encode(&bytes));
    cache.lock().unwrap().insert(url.to_string(), data_uri.clone());
    Some(data_uri)
}
