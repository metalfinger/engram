// Engram Notifications — popup UI. Relies on common.js (loaded first) for
// storage/auth/formatting helpers.

const KIND_BADGE = {
  dm: { emoji: "\u{1F4AC}", label: "DM" },
  room_invite: { emoji: "\u{1F6AA}", label: "Room invite" },
  question: { emoji: "❓", label: "Question" },
  answer: { emoji: "✅", label: "Answer" },
  contact_request: { emoji: "\u{1F44B}", label: "Contact request" },
  contact_accepted: { emoji: "\u{1F44B}", label: "Contact accepted" },
  room_closed: { emoji: "\u{1F512}", label: "Room closed" },
};

function badgeFor(kind) {
  return KIND_BADGE[kind] || { emoji: "\u{1F514}", label: kind || "Notification" };
}

function freshnessClass(minutesAgo) {
  const m = Number(minutesAgo);
  if (!Number.isFinite(m)) return "";
  if (m <= 15) return "fresh-green";
  if (m <= 120) return "fresh-amber";
  return "";
}

// -- row builders (DOM construction only — textContent/attributes, never
// innerHTML with server-provided strings) --------------------------------

function buildNotifRow(item) {
  const row = document.createElement("div");
  row.className = "notif-row";

  const badge = badgeFor(item.kind);
  const badgeEl = document.createElement("span");
  badgeEl.className = "badge";
  badgeEl.title = badge.label;
  badgeEl.textContent = badge.emoji;

  const main = document.createElement("div");
  main.className = "notif-main";

  const bodyEl = document.createElement("div");
  bodyEl.className = "notif-body";
  bodyEl.textContent = item.body || "";

  const metaEl = document.createElement("div");
  metaEl.className = "notif-meta";
  metaEl.textContent = relativeTime(item.at);

  main.append(bodyEl, metaEl);
  row.append(badgeEl, main);

  if (item.kind === "room_invite" && item.ref) {
    const openBtn = document.createElement("button");
    openBtn.className = "open-btn";
    openBtn.textContent = "Open";
    openBtn.addEventListener("click", () => {
      chrome.tabs.create({ url: dashboardUrl(`/dashboard/rooms/${encodeURIComponent(item.ref)}`) });
    });
    row.appendChild(openBtn);
  }

  return row;
}

function buildTeamRow(member) {
  const row = document.createElement("div");
  row.className = "team-row";

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  if (member.avatar_url && /^https:\/\//.test(member.avatar_url)) {
    const img = document.createElement("img");
    img.src = member.avatar_url;
    img.alt = "";
    avatar.appendChild(img);
  } else {
    avatar.style.background = colorForHandle(member.handle);
    avatar.textContent = initials(member.display_name || member.handle);
  }

  const info = document.createElement("div");
  info.className = "team-info";
  const nameEl = document.createElement("div");
  nameEl.className = "team-name";
  nameEl.textContent = member.display_name || member.handle || "";
  const projEl = document.createElement("div");
  projEl.className = "team-project";
  projEl.textContent = member.project ? `in ${member.project}` : "";
  info.append(nameEl, projEl);

  const fresh = document.createElement("span");
  fresh.className = `fresh-dot ${freshnessClass(member.minutes_ago)}`.trim();

  row.append(avatar, info, fresh);
  return row;
}

// -- section renderers -----------------------------------------------------

function renderHeader(teamData) {
  const dot = document.getElementById("status-dot");
  const handleLabel = document.getElementById("handle-label");
  const me = teamData?.me;
  dot.className = `dot ${me?.invisible ? "dot-gray" : "dot-green"}`;
  handleLabel.textContent = me?.handle ? `@${me.handle}` : "";
}

function wireInvisibleToggle(me) {
  const checkbox = document.getElementById("invisible");
  if (!me) {
    checkbox.checked = false;
    checkbox.disabled = true;
    return;
  }
  checkbox.disabled = false;
  checkbox.checked = !!me.invisible;
}

function renderTeam(data) {
  const list = document.getElementById("team-list");
  list.innerHTML = "";

  if (!data) {
    wireInvisibleToggle(null);
    const empty = document.createElement("div");
    empty.className = "empty-note";
    empty.textContent = "Team presence is coming soon.";
    list.appendChild(empty);
    return;
  }

  wireInvisibleToggle(data.me);
  const team = Array.isArray(data.team) ? data.team : [];
  if (team.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-note";
    empty.textContent = "No one else is online right now.";
    list.appendChild(empty);
    return;
  }
  for (const member of team) {
    list.appendChild(buildTeamRow(member));
  }
}

function renderNotifications(data) {
  const list = document.getElementById("notif-list");
  list.innerHTML = "";
  const unread = Array.isArray(data?.unread) ? data.unread : [];
  document.getElementById("mark-read").disabled = unread.length === 0;

  if (unread.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-note";
    empty.textContent = "You're all caught up.";
    list.appendChild(empty);
    return;
  }
  for (const item of unread) {
    list.appendChild(buildNotifRow(item));
  }
}

function showSignedOut() {
  document.getElementById("signed-out").hidden = false;
  document.getElementById("signed-in").hidden = true;
  document.getElementById("sign-out-btn").hidden = true;
  document.getElementById("status-dot").className = "dot";
  document.getElementById("handle-label").textContent = "";
}

function showSignedIn() {
  document.getElementById("signed-out").hidden = true;
  document.getElementById("signed-in").hidden = false;
  document.getElementById("sign-out-btn").hidden = false;
}

// -- main load/refresh -------------------------------------------------------

async function refreshAll() {
  const token = await getToken();
  if (!token) {
    showSignedOut();
    return;
  }
  showSignedIn();

  const origin = await getOrigin();
  const headers = authHeaders(token);

  const [notifRes, teamRes] = await Promise.all([
    fetchOk(`${origin}/dashboard/api/notifications`, { headers }),
    fetchOk(`${origin}/dashboard/api/team`, { headers }),
  ]);

  if (!notifRes.ok && notifRes.status === 401) {
    await signOut();
    showSignedOut();
    return;
  }

  renderNotifications(notifRes.ok ? notifRes.data : { unread: [], counts: {} });
  // /dashboard/api/team may not be live yet — renderTeam(null) shows a
  // friendly empty-state rather than an error in that case.
  renderTeam(teamRes.ok ? teamRes.data : null);
  renderHeader(teamRes.ok ? teamRes.data : null);
}

document.getElementById("signin").addEventListener("click", async () => {
  const btn = document.getElementById("signin");
  const status = document.getElementById("signin-status");
  btn.disabled = true;
  status.textContent = "";
  try {
    const origin = await getOrigin();
    await signIn(origin);
    await refreshAll();
    chrome.runtime.sendMessage({ type: "poll-now" });
  } catch (err) {
    status.textContent = err.message || "Sign-in was cancelled or failed.";
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("sign-out-btn").addEventListener("click", async () => {
  await signOut();
  showSignedOut();
});

document.getElementById("open-dashboard-link").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.tabs.create({ url: dashboardUrl() });
});

document.getElementById("mark-read").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "mark-read" });
  await refreshAll();
});

document.getElementById("invisible").addEventListener("change", async (e) => {
  const checked = e.target.checked;
  const token = await getToken();
  const origin = await getOrigin();
  const { ok } = await fetchOk(`${origin}/dashboard/api/presence`, {
    method: "POST",
    headers: { ...authHeaders(token), "Content-Type": "application/json" },
    body: JSON.stringify({ invisible: checked }),
  });
  if (!ok) {
    e.target.checked = !checked; // endpoint not live yet (or a transient failure) — revert
    return;
  }
  document.getElementById("status-dot").className = `dot ${checked ? "dot-gray" : "dot-green"}`;
});

refreshAll();
