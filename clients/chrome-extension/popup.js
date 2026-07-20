const DEFAULT_ORIGIN = "https://engram.metalfinger.xyz";

async function getOrigin() {
  const { engramOrigin } = await chrome.storage.sync.get("engramOrigin");
  return engramOrigin || DEFAULT_ORIGIN;
}

async function getToken() {
  const { engramToken } = await chrome.storage.sync.get("engramToken");
  return engramToken || "";
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

function render(state) {
  const list = document.getElementById("list");
  const footer = document.getElementById("footer");
  const badge = document.getElementById("badge");
  list.innerHTML = "";
  footer.innerHTML = "";
  badge.textContent = "";

  if (!state.signedIn) {
    list.innerHTML = `<div class="signed-out">Paste your Engram extension token in Options.</div>`;
    footer.innerHTML = `<a class="button primary" id="open-options" href="#">Open Options</a>`;
    document.getElementById("open-options").addEventListener("click", (e) => {
      e.preventDefault();
      chrome.runtime.openOptionsPage();
    });
    return;
  }

  const unread = state.unread || [];
  const total = (state.counts?.dms || 0) + (state.counts?.notifications || 0);
  if (total > 0) badge.textContent = String(total);

  if (unread.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "You're all caught up.";
    list.appendChild(empty);
  } else {
    for (const item of unread) {
      list.appendChild(buildItemRow(item));
    }
  }

  footer.innerHTML = `
    <button id="mark-read" ${unread.length === 0 ? "disabled" : ""}>Mark all read</button>
    <a class="button" id="open-dashboard" href="#">Open dashboard</a>
  `;

  document.getElementById("mark-read").addEventListener("click", async (e) => {
    e.target.disabled = true;
    await chrome.runtime.sendMessage({ type: "mark-read" });
    await load();
  });

  document.getElementById("open-dashboard").addEventListener("click", async (e) => {
    e.preventDefault();
    const origin = await getOrigin();
    chrome.tabs.create({ url: `${origin}/dashboard` });
  });
}

function buildItemRow(item) {
  const row = document.createElement("div");
  row.className = "item";

  const kind = document.createElement("div");
  kind.className = "kind";
  kind.textContent = item.kind || "";

  const body = document.createElement("div");
  body.className = "body";
  body.textContent = item.body || "";

  const at = document.createElement("div");
  at.className = "at";
  at.textContent = relativeTime(item.at);

  row.append(kind, body, at);
  return row;
}

async function load() {
  const token = await getToken();
  if (!token) {
    render({ signedIn: false });
    return;
  }

  const origin = await getOrigin();
  try {
    const res = await fetch(`${origin}/dashboard/api/notifications`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const data = await res.json();
    if (!data || data.ok !== true) {
      render({ signedIn: false });
      return;
    }
    render({ signedIn: true, unread: data.unread, counts: data.counts });
  } catch {
    render({ signedIn: false });
  }
}

load();
