//! Loopback sign-in flow.
//!
//! The dashboard hands the token back via a URL *fragment*
//! (`http://127.0.0.1:<port>/callback/<nonce>#token=<jwt>`), which never
//! reaches an HTTP server — fragments are client-side only. So we serve a
//! tiny page whose JS reads `location.hash` in the browser and POSTs the
//! token back to us on the same loopback port. The nonce binds the
//! callback to this one flow so nothing else on the machine can shove a
//! token at us.

use std::time::{Duration, Instant};

use rand::Rng;

const NONCE_LEN: usize = 32;
const FLOW_TIMEOUT: Duration = Duration::from_secs(180);
const NONCE_CHARSET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-";

#[derive(Debug)]
pub enum AuthError {
    /// Nothing arrived within the 3-minute window.
    Timeout,
    /// Couldn't bind the loopback listener.
    Bind(String),
    /// Listener died mid-flow.
    Io(String),
    /// The callback POSTed something we couldn't parse as `{"token": "..."}`.
    BadPayload(String),
}

impl std::fmt::Display for AuthError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            AuthError::Timeout => write!(f, "sign-in timed out"),
            AuthError::Bind(m) => write!(f, "couldn't start local listener: {m}"),
            AuthError::Io(m) => write!(f, "local listener error: {m}"),
            AuthError::BadPayload(m) => write!(f, "bad callback payload: {m}"),
        }
    }
}

impl std::error::Error for AuthError {}

fn gen_nonce() -> String {
    let mut rng = rand::thread_rng();
    (0..NONCE_LEN)
        .map(|_| {
            let idx = rng.gen_range(0..NONCE_CHARSET.len());
            NONCE_CHARSET[idx] as char
        })
        .collect()
}

const CALLBACK_HTML: &str = r#"<!doctype html>
<html>
  <head><meta charset="utf-8"><title>Engram — signing in</title></head>
  <body style="font-family:system-ui,sans-serif;background:#111;color:#eee;
               display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
    <div id="msg" style="text-align:center">
      <p>Finishing sign-in&hellip;</p>
    </div>
    <script>
      (function () {
        var msg = document.getElementById('msg');
        var hash = window.location.hash || '';
        var params = new URLSearchParams(hash.replace(/^#/, ''));
        var token = params.get('token');
        if (!token) {
          msg.innerHTML = '<p>No token found in callback. You can close this tab and try again.</p>';
          return;
        }
        fetch(window.location.pathname.replace('/callback/', '/token/'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token: token })
        }).then(function (r) {
          if (r.ok) {
            msg.innerHTML = '<p>Signed in &mdash; you can close this tab.</p>';
            try { window.close(); } catch (e) {}
          } else {
            msg.innerHTML = '<p>Sign-in failed to register. You can close this tab and try again.</p>';
          }
        }).catch(function () {
          msg.innerHTML = '<p>Sign-in failed to register. You can close this tab and try again.</p>';
        });
      })();
    </script>
  </body>
</html>"#;

/// Run the whole loopback flow synchronously (blocking on the listener).
/// Call this from `tokio::task::spawn_blocking`.
pub fn run_sign_in_flow_blocking(origin: &str) -> Result<String, AuthError> {
    let nonce = gen_nonce();
    let server =
        tiny_http::Server::http("127.0.0.1:0").map_err(|e| AuthError::Bind(e.to_string()))?;
    let port = server
        .server_addr()
        .to_ip()
        .map(|a| a.port())
        .ok_or_else(|| AuthError::Bind("no local port assigned".into()))?;

    let redirect = format!("http://127.0.0.1:{port}/callback/{nonce}");
    let auth_url = format!(
        "{}/dashboard/ext-auth?redirect={}",
        origin.trim_end_matches('/'),
        urlencoding::encode(&redirect)
    );

    if let Err(e) = webbrowser::open(&auth_url) {
        log::warn!("couldn't auto-open browser for sign-in: {e}");
    }

    let callback_path = format!("/callback/{nonce}");
    let token_path = format!("/token/{nonce}");
    let deadline = Instant::now() + FLOW_TIMEOUT;

    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(AuthError::Timeout);
        }
        let mut request = match server.recv_timeout(remaining) {
            Ok(Some(req)) => req,
            Ok(None) => return Err(AuthError::Timeout),
            Err(e) => return Err(AuthError::Io(e.to_string())),
        };

        let url = request.url().to_string();
        let method = request.method().clone();

        if method == tiny_http::Method::Get && url == callback_path {
            let header =
                tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"text/html; charset=utf-8"[..])
                    .expect("static header is valid");
            let response = tiny_http::Response::from_string(CALLBACK_HTML).with_header(header);
            let _ = request.respond(response);
            continue;
        }

        if method == tiny_http::Method::Post && url == token_path {
            let mut body = String::new();
            let read_result = request.as_reader().read_to_string(&mut body);
            if let Err(e) = read_result {
                let _ = request.respond(tiny_http::Response::from_string("bad request").with_status_code(400));
                return Err(AuthError::Io(e.to_string()));
            }
            let parsed: Result<serde_json::Value, _> = serde_json::from_str(&body);
            let token = parsed
                .ok()
                .and_then(|v| v.get("token").and_then(|t| t.as_str()).map(|s| s.to_string()));
            match token {
                Some(t) if !t.is_empty() => {
                    let _ = request.respond(
                        tiny_http::Response::from_string(r#"{"ok":true}"#)
                            .with_header(
                                tiny_http::Header::from_bytes(
                                    &b"Content-Type"[..],
                                    &b"application/json"[..],
                                )
                                .expect("static header is valid"),
                            ),
                    );
                    return Ok(t);
                }
                _ => {
                    let _ = request.respond(
                        tiny_http::Response::from_string("missing token").with_status_code(400),
                    );
                    return Err(AuthError::BadPayload("missing/empty token field".into()));
                }
            }
        }

        // Anything else (wrong nonce, stray probes, favicon, etc.) — ignore.
        let _ = request.respond(tiny_http::Response::from_string("not found").with_status_code(404));
    }
}
