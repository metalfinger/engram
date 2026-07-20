// Engram Notifications — background service worker (MV3).
//
// MV3 service workers are ephemeral: they can be killed and restarted between
// alarm ticks, so all state that must survive a restart lives in
// chrome.storage (never in a module-level variable), and polling is driven by
// chrome.alarms rather than setInterval (which would die with the worker).

const DEFAULT_ORIGIN = "https://engram.metalfinger.xyz";
const ALARM_NAME = "engram-poll";
const POLL_PERIOD_MINUTES = 1;

async function getOrigin() {
  const { engramOrigin } = await chrome.storage.sync.get("engramOrigin");
  return engramOrigin || DEFAULT_ORIGIN;
}

async function getToken() {
  const { engramToken } = await chrome.storage.sync.get("engramToken");
  return engramToken || "";
}

function authHeaders(token) {
  return { Authorization: `Bearer ${token}` };
}

function ensureAlarm() {
  chrome.alarms.get(ALARM_NAME, (alarm) => {
    if (!alarm) {
      chrome.alarms.create(ALARM_NAME, { periodInMinutes: POLL_PERIOD_MINUTES });
    }
  });
}

chrome.runtime.onInstalled.addListener(() => {
  ensureAlarm();
  poll();
});

chrome.runtime.onStartup.addListener(() => {
  ensureAlarm();
  poll();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) {
    poll();
  }
});

async function poll() {
  try {
    const token = await getToken();
    if (!token) {
      // No token saved yet — nothing to poll with. Don't fetch at all.
      await chrome.storage.local.set({ signedIn: false });
      await chrome.action.setBadgeText({ text: "" });
      return;
    }

    const origin = await getOrigin();
    const res = await fetch(`${origin}/dashboard/api/notifications`, {
      headers: authHeaders(token),
    });

    let data;
    try {
      data = await res.json();
    } catch {
      // Non-JSON response (proxy error page, offline, etc). Stay silent, try
      // again on the next alarm tick.
      return;
    }

    if (!data || data.ok !== true) {
      // Covers the documented 401 {"ok": false, "error": "not signed in"}
      // shape and any other non-ok response.
      await chrome.storage.local.set({ signedIn: false });
      await chrome.action.setBadgeText({ text: "" });
      return;
    }

    await handleUnread(data);
  } catch {
    // Network failure. Never throw out of the alarm handler — just retry
    // next tick.
  }
}

async function handleUnread(data) {
  const unread = Array.isArray(data.unread) ? data.unread : [];
  const counts = data.counts || {};

  const { lastSeenMaxId } = await chrome.storage.local.get("lastSeenMaxId");
  const currentMaxId = unread.reduce((max, item) => Math.max(max, item.id), 0);

  if (lastSeenMaxId === undefined) {
    // First run: there is no prior baseline, so anything currently unread is
    // backfill, not "new". Record the baseline silently and never notify.
    await chrome.storage.local.set({ lastSeenMaxId: currentMaxId });
  } else {
    const freshItems = unread
      .filter((item) => item.id > lastSeenMaxId)
      .sort((a, b) => a.id - b.id);

    for (const item of freshItems) {
      chrome.notifications.create(`engram-${item.id}`, {
        type: "basic",
        iconUrl: "icons/icon128.png",
        title: notificationTitle(item.kind),
        message: item.body || "",
        contextMessage: "Engram",
      });
    }

    if (currentMaxId > lastSeenMaxId) {
      await chrome.storage.local.set({ lastSeenMaxId: currentMaxId });
    }
  }

  await chrome.storage.local.set({
    signedIn: true,
    lastUnread: unread,
    lastCounts: counts,
  });

  const total = (counts.dms || 0) + (counts.notifications || 0);
  await chrome.action.setBadgeText({ text: total > 0 ? String(total) : "" });
  await chrome.action.setBadgeBackgroundColor({ color: "#d64545" });
}

function notificationTitle(kind) {
  if (kind === "dm") return "New Engram DM";
  if (kind === "notification") return "Engram notification";
  return "Engram";
}

async function markRead() {
  const token = await getToken();
  if (!token) return;
  const origin = await getOrigin();
  try {
    await fetch(`${origin}/dashboard/api/notifications/read`, {
      method: "POST",
      headers: authHeaders(token),
    });
  } catch {
    // Best-effort; the next poll will reconcile state either way.
  }
}

chrome.notifications.onClicked.addListener(async (notificationId) => {
  const origin = await getOrigin();
  chrome.tabs.create({ url: `${origin}/dashboard` });
  chrome.notifications.clear(notificationId);
  await markRead();
  await chrome.action.setBadgeText({ text: "" });
});

// Allow the popup's "Mark all read" button to reuse this logic instead of
// duplicating the fetch + badge update.
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "mark-read") {
    markRead().then(async () => {
      await chrome.action.setBadgeText({ text: "" });
      sendResponse();
    });
    return true; // keep the message channel open for the async response
  }
  return false;
});
