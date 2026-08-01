// Engram Notifications — shared helpers used by background.js, popup.js, and
// options.js. No DOM access here: background.js runs in an MV3 service
// worker (no `document`), so anything DOM-touching lives in popup.js/options.js.
// Loaded via `importScripts("common.js")` in the service worker and via a
// plain <script src="common.js"> (before popup.js/options.js) in the pages.

const DEFAULT_ORIGIN = "https://engram.metalfinger.xyz"; // API / MCP connector origin
const DASHBOARD_ORIGIN = "https://brain.metalfinger.xyz"; // web dashboard for humans
const DEFAULT_POLL_MINUTES = 1;

async function getOrigin() {
  const { engramOrigin } = await chrome.storage.local.get("engramOrigin");
  return engramOrigin || DEFAULT_ORIGIN;
}

async function getToken() {
  const { engramToken } = await chrome.storage.local.get("engramToken");
  return engramToken || "";
}

// Auth lives in chrome.storage.local, never .sync — a notify token shouldn't
// be copied across every Chrome profile the user is signed into.
async function setSession(origin, token) {
  await chrome.storage.local.set({ engramOrigin: origin, engramToken: token });
}

async function clearToken() {
  await chrome.storage.local.remove("engramToken");
}

async function getPollMinutes() {
  const { pollMinutes } = await chrome.storage.local.get("pollMinutes");
  const n = Number(pollMinutes);
  return Number.isFinite(n) && n > 0 ? n : DEFAULT_POLL_MINUTES;
}

async function setPollMinutes(minutes) {
  await chrome.storage.local.set({ pollMinutes: minutes });
}

function authHeaders(token) {
  return { Authorization: `Bearer ${token}` };
}

function dashboardUrl(path = "/dashboard") {
  return `${DASHBOARD_ORIGIN}${path}`;
}

function normalizeOrigin(raw) {
  const trimmed = (raw || "").trim().replace(/\/+$/, "");
  let url;
  try {
    url = new URL(trimmed);
  } catch {
    return null;
  }
  if (url.protocol !== "https:") return null;
  if (url.pathname !== "/" && url.pathname !== "") return null;
  return `${url.protocol}//${url.host}`;
}

async function ensureOriginPermission(origin) {
  try {
    return await chrome.permissions.request({ origins: [`${origin}/*`] });
  } catch {
    return false;
  }
}

// Signs in via the SAME GitHub/Google OAuth as the dashboard/MCP connector,
// through chrome.identity.launchWebAuthFlow -> {origin}/dashboard/ext-auth ->
// a scope="notify" token in the redirect fragment. No token to copy/paste.
// Throws an Error with a user-facing message on any failure.
async function signIn(origin) {
  const target = normalizeOrigin(origin) || DEFAULT_ORIGIN;
  if (!(await ensureOriginPermission(target))) {
    throw new Error("Chrome needs permission to reach that origin — please allow it.");
  }
  const redirectUrl = chrome.identity.getRedirectURL();
  const authUrl = `${target}/dashboard/ext-auth?redirect=${encodeURIComponent(redirectUrl)}`;
  let finalUrl;
  try {
    finalUrl = await chrome.identity.launchWebAuthFlow({ url: authUrl, interactive: true });
  } catch {
    throw new Error("Sign-in was cancelled or failed.");
  }
  const m = finalUrl && finalUrl.match(/[#?&]token=([^&]+)/);
  if (!m) throw new Error("Sign-in didn't return a token — try again.");
  const token = decodeURIComponent(m[1]);
  await setSession(target, token);
  return token;
}

async function signOut() {
  await clearToken();
}

function relativeTime(iso) {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diffSec = Math.round((Date.now() - then) / 1000);
  if (diffSec < 60) return "just now";
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHour = Math.round(diffMin / 60);
  if (diffHour < 24) return `${diffHour}h ago`;
  const diffDay = Math.round(diffHour / 24);
  return `${diffDay}d ago`;
}

// Deterministic color + initials for a fallback avatar when avatar_url is absent.
const AVATAR_PALETTE = [
  "#3a5cd6", "#2a9d5c", "#d68c2a", "#9d3ad6", "#d64545", "#2ab3d6", "#8c5c2a", "#5c8c2a",
];

function colorForHandle(handle) {
  const s = String(handle || "");
  let hash = 0;
  for (let i = 0; i < s.length; i++) {
    hash = (hash * 31 + s.charCodeAt(i)) | 0;
  }
  return AVATAR_PALETTE[Math.abs(hash) % AVATAR_PALETTE.length];
}

function initials(handleOrName) {
  const s = String(handleOrName || "").trim();
  if (!s) return "?";
  const parts = s.split(/[\s._-]+/).filter(Boolean);
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

// Fetch helper that collapses every failure mode (network error, non-JSON
// body, 401, or an {ok:false} response) into one shape, so callers can treat
// "no data yet" (e.g. an endpoint that hasn't shipped) and "signed out"
// uniformly without a try/catch at every call site.
async function fetchOk(url, opts) {
  try {
    const res = await fetch(url, opts);
    let data;
    try {
      data = await res.json();
    } catch {
      return { ok: false, status: res.status, data: null };
    }
    if (!data || data.ok !== true) {
      return { ok: false, status: res.status, data };
    }
    return { ok: true, status: res.status, data };
  } catch {
    return { ok: false, status: 0, data: null };
  }
}
