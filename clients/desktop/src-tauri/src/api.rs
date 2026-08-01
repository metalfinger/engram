//! Thin client for the Engram dashboard's tray-facing API.
//!
//! All endpoints live under `{origin}/dashboard/api/*` and are
//! bearer-token authenticated. A 401 means the stored token is no longer
//! valid and the caller should fall back to the signed-out state.

use std::time::Duration;

use serde::{Deserialize, Serialize};

const REQUEST_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Debug)]
pub enum ApiError {
    /// The server rejected the bearer token (401). Caller should sign out.
    Unauthorized,
    /// Any other transport/parse failure. Carries a short message for logs.
    Other(String),
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ApiError::Unauthorized => write!(f, "unauthorized"),
            ApiError::Other(msg) => write!(f, "{msg}"),
        }
    }
}

impl std::error::Error for ApiError {}

impl From<reqwest::Error> for ApiError {
    fn from(e: reqwest::Error) -> Self {
        ApiError::Other(e.to_string())
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct NotificationItem {
    pub id: String,
    pub kind: String,
    pub body: String,
    pub at: String,
    #[serde(rename = "ref")]
    pub reference: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize, Default)]
pub struct NotificationsResponse {
    pub ok: bool,
    #[serde(default)]
    pub unread: Vec<NotificationItem>,
    #[serde(default)]
    pub counts: serde_json::Value,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct TeamMember {
    pub handle: String,
    #[serde(default)]
    pub display_name: Option<String>,
    #[serde(default)]
    pub avatar_url: Option<String>,
    #[serde(default)]
    pub project: Option<String>,
    #[serde(default)]
    pub tool: Option<String>,
    pub minutes_ago: f64,
}

#[derive(Debug, Clone, Deserialize, Serialize, Default)]
pub struct Me {
    pub handle: String,
    #[serde(default)]
    pub invisible: bool,
}

#[derive(Debug, Clone, Deserialize, Serialize, Default)]
pub struct TeamResponse {
    pub ok: bool,
    #[serde(default)]
    pub team: Vec<TeamMember>,
    #[serde(default)]
    pub me: Option<Me>,
}

#[derive(Debug, Clone, Deserialize, Serialize, Default)]
pub struct PresenceResponse {
    pub ok: bool,
    #[serde(default)]
    pub invisible: bool,
}

#[derive(Clone)]
pub struct ApiClient {
    http: reqwest::Client,
    origin: String,
    token: String,
}

impl ApiClient {
    pub fn new(origin: &str, token: &str) -> Self {
        let http = reqwest::Client::builder()
            .timeout(REQUEST_TIMEOUT)
            .build()
            .unwrap_or_else(|_| reqwest::Client::new());
        Self {
            http,
            origin: origin.trim_end_matches('/').to_string(),
            token: token.to_string(),
        }
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.origin, path)
    }

    async fn check_status(resp: reqwest::Response) -> Result<reqwest::Response, ApiError> {
        if resp.status().as_u16() == 401 {
            return Err(ApiError::Unauthorized);
        }
        if !resp.status().is_success() {
            return Err(ApiError::Other(format!("http {}", resp.status())));
        }
        Ok(resp)
    }

    pub async fn fetch_notifications(&self) -> Result<NotificationsResponse, ApiError> {
        let resp = self
            .http
            .get(self.url("/dashboard/api/notifications"))
            .bearer_auth(&self.token)
            .send()
            .await?;
        let resp = Self::check_status(resp).await?;
        Ok(resp.json::<NotificationsResponse>().await?)
    }

    pub async fn mark_all_read(&self) -> Result<(), ApiError> {
        let resp = self
            .http
            .post(self.url("/dashboard/api/notifications/read"))
            .bearer_auth(&self.token)
            .send()
            .await?;
        Self::check_status(resp).await?;
        Ok(())
    }

    pub async fn fetch_team(&self) -> Result<TeamResponse, ApiError> {
        let resp = self
            .http
            .get(self.url("/dashboard/api/team"))
            .bearer_auth(&self.token)
            .send()
            .await?;
        let resp = Self::check_status(resp).await?;
        Ok(resp.json::<TeamResponse>().await?)
    }

    pub async fn set_presence(&self, invisible: bool) -> Result<PresenceResponse, ApiError> {
        let resp = self
            .http
            .post(self.url("/dashboard/api/presence"))
            .bearer_auth(&self.token)
            .json(&serde_json::json!({ "invisible": invisible }))
            .send()
            .await?;
        let resp = Self::check_status(resp).await?;
        Ok(resp.json::<PresenceResponse>().await?)
    }
}
