// Engram Notifications — background service worker (MV3).
//
// MV3 service workers are ephemeral: they can be killed and restarted between
// alarm ticks, so all state that must survive a restart lives in
// chrome.storage (never in a module-level variable), and polling is driven by
// chrome.alarms rather than setInterval (which would die with the worker).

importScripts("common.js");

const ALARM_NAME = "engram-poll";

function ensureAlarm(periodInMinutes) {
  chrome.alarms.create(ALARM_NAME, { periodInMinutes });
}

async function resetAlarm() {
  ensureAlarm(await getPollMinutes());
}

chrome.runtime.onInstalled.addListener(async (details) => {
  if (details.reason === "update") {
    await migrateFromSync();
  }
  await resetAlarm();
  poll();
});

chrome.runtime.onStartup.addListener(async () => {
  await resetAlarm();
  poll();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) {
    poll();
  }
});

// The options page writes a new interval straight to storage — pick it up
// immediately instead of waiting for the next browser restart.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.pollMinutes) {
    resetAlarm();
  }
});

// One-time upgrade path from the 1.x extension, which stored the (manually
// pasted) token in chrome.storage.sync. Best-effort: a fresh sign-in via the
// popup/options always works too, so failures here are silently ignored.
async function migrateFromSync() {
  try {
    const { engramOrigin, engramToken } = await chrome.storage.sync.get([
      "engramOrigin",
      "engramToken",
    ]);
    if (!engramToken) return;
    const { engramToken: existingLocal } = await chrome.storage.local.get("engramToken");
    if (!existingLocal) {
      await chrome.storage.local.set({
        engramOrigin: engramOrigin || DEFAULT_ORIGIN,
        engramToken,
      });
    }
    await chrome.storage.sync.remove(["engramOrigin", "engramToken"]);
  } catch {
    // best-effort migration only
  }
}

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
    const { ok, status, data } = await fetchOk(`${origin}/dashboard/api/notifications`, {
      headers: authHeaders(token),
    });

    if (!ok) {
      // Covers the documented 401 {"ok": false, "error": "not signed in"}
      // shape, a rejected/expired token, and any offline/proxy-error case.
      if (status === 401) {
        await clearToken();
      }
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

// kind -> title shown on the native Chrome notification. Mirrors the badges
// used in the popup so the same vocabulary appears everywhere.
const KIND_META = {
  dm: { emoji: "\u{1F4AC}", title: "New Engram DM" },
  room_invite: { emoji: "\u{1F6AA}", title: "Room invite" },
  question: { emoji: "❓", title: "New question" },
  answer: { emoji: "✅", title: "Answered" },
  contact_request: { emoji: "\u{1F44B}", title: "Contact request" },
  contact_accepted: { emoji: "\u{1F44B}", title: "Contact accepted" },
  room_closed: { emoji: "\u{1F512}", title: "Room closed" },
};

function metaFor(kind) {
  return KIND_META[kind] || { emoji: "\u{1F514}", title: "Engram" };
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
      const meta = metaFor(item.kind);
      chrome.notifications.create(encodeNotificationId(item), {
        type: "basic",
        iconUrl: "icons/icon128.png",
        title: `${meta.emoji} ${meta.title}`,
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

// Notification ids carry enough to route a click without a second lookup:
// "engram:<id>:<kind>:<url-encoded ref>" (ref is empty for kinds without one).
// encodeURIComponent guarantees the ref segment never contains a literal ":",
// so a plain split(":") on click is safe.
function encodeNotificationId(item) {
  return `engram:${item.id}:${item.kind || ""}:${encodeURIComponent(item.ref || "")}`;
}

function decodeNotificationId(notificationId) {
  const parts = String(notificationId).split(":");
  if (parts[0] !== "engram") return null;
  const [, id, kind, refEncoded] = parts;
  let ref = "";
  try {
    ref = decodeURIComponent(refEncoded || "");
  } catch {
    ref = "";
  }
  return { id, kind: kind || "", ref };
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
  const decoded = decodeNotificationId(notificationId);
  const url =
    decoded && decoded.kind === "room_invite" && decoded.ref
      ? dashboardUrl(`/dashboard/rooms/${encodeURIComponent(decoded.ref)}`)
      : dashboardUrl();
  chrome.tabs.create({ url });
  chrome.notifications.clear(notificationId);
  await markRead();
  await chrome.action.setBadgeText({ text: "" });
});

// Let the popup reuse this logic instead of duplicating the fetch + badge
// update, and let either page force an immediate poll right after sign-in.
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "mark-read") {
    markRead().then(async () => {
      await chrome.action.setBadgeText({ text: "" });
      sendResponse();
    });
    return true; // keep the message channel open for the async response
  }
  if (message?.type === "poll-now") {
    poll().then(() => sendResponse());
    return true;
  }
  return false;
});
