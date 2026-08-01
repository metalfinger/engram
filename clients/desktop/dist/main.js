// Engram Tray popup — vanilla JS, no bundler. Everything it knows comes
// from `invoke()` calls into src-tauri/src/main.rs's commands, plus the
// occasional unsolicited push from `window.__engramPush` (see the doc
// comment on `popup::push_state` in src-tauri/src/popup.rs for why that
// exists instead of Tauri's event/listen bridge). This file never fetches
// anything itself and never sees a bearer token.
(function () {
  const { invoke } = window.__TAURI__.core;

  const el = (id) => document.getElementById(id);

  function hueFromHandle(handle) {
    let hash = 0;
    for (let i = 0; i < handle.length; i++) {
      hash = (hash * 31 + handle.charCodeAt(i)) >>> 0;
    }
    return hash % 360;
  }

  function initials(name) {
    return (name || "?").trim().slice(0, 2).toUpperCase();
  }

  function renderAvatar(container, handle, label, avatarData) {
    container.innerHTML = "";
    container.style.background = "";
    if (avatarData) {
      const img = document.createElement("img");
      img.src = avatarData;
      img.alt = label || handle || "";
      container.appendChild(img);
      return;
    }
    const hue = hueFromHandle(handle || "?");
    container.style.background = `hsl(${hue}, 55%, 38%)`;
    container.textContent = initials(label || handle || "?");
  }

  function fmtMinutes(m) {
    const mins = Math.round(m);
    if (mins <= 0) return "just now";
    if (mins === 1) return "1m ago";
    return `${mins}m ago`;
  }

  function fmtRelative(iso) {
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return "";
    const mins = Math.round((Date.now() - then) / 60000);
    if (mins <= 0) return "just now";
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    return `${Math.round(hours / 24)}d ago`;
  }

  const KIND_META = {
    dm: { emoji: "\u{1F4AC}", label: "DM" },
    room_invite: { emoji: "\u{1F6AA}", label: "Room invite" },
    room_closed: { emoji: "\u{1F512}", label: "Room closed" },
    question: { emoji: "❓", label: "Question" },
    answer: { emoji: "✅", label: "Answer" },
    contact_request: { emoji: "\u{1F44B}", label: "Contact request" },
    contact_accepted: { emoji: "\u{1F44B}", label: "Contact accepted" },
  };

  // Which notification kinds get an action button, and where it goes.
  // Mirrors the server's own path conventions — the popup only ever
  // opens `{origin}` + one of these via the whitelisted `open_url`
  // command (see main.rs's ALLOWED_PATH_PREFIXES).
  function actionFor(item) {
    switch (item.kind) {
      case "room_invite":
      case "room_closed":
        return item.ref ? { label: "Open room", path: `/dashboard/rooms/${item.ref}` } : null;
      case "dm":
        return { label: "Open messages", path: "/dashboard" };
      case "question":
      case "answer":
        return { label: "View", path: "/dashboard/asks" };
      default:
        return null;
    }
  }

  function openPath(path) {
    invoke("open_url", { path }).catch((e) => console.error("open_url failed", path, e));
  }

  function render(state) {
    if (!state) return;
    const signedIn = !!state.signed_in;
    el("signed-out-panel").hidden = signedIn;
    el("signed-in-panel").hidden = !signedIn;

    if (signedIn) {
      const me = state.me || {};
      el("me-handle").textContent = me.handle ? `@${me.handle}` : "Engram";
      el("me-status").textContent = me.project ? `working on ${me.project}` : "";
      renderAvatar(el("me-avatar"), me.handle, me.handle, me.avatar_data);
      const invisBtn = el("btn-invisible");
      invisBtn.setAttribute("aria-pressed", String(!!me.invisible));
      invisBtn.classList.toggle("active", !!me.invisible);
    } else {
      el("me-handle").textContent = "Engram Tray";
      el("me-status").textContent = "signed out";
      renderAvatar(el("me-avatar"), "?", "?", null);
    }

    // Working now.
    const teamList = el("team-list");
    teamList.innerHTML = "";
    const team = state.team || [];
    el("team-empty").hidden = team.length > 0;
    for (const member of team) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "team-card";

      const avatar = document.createElement("div");
      avatar.className = "avatar avatar-sm";
      renderAvatar(avatar, member.handle, member.display_name, member.avatar_data);

      const text = document.createElement("div");
      text.className = "team-text";
      const name = document.createElement("div");
      name.className = "team-name";
      name.textContent = member.display_name || member.handle;
      const meta = document.createElement("div");
      meta.className = "team-meta";
      meta.textContent = member.project ? `in ${member.project}` : "—";
      text.appendChild(name);
      text.appendChild(meta);

      const dot = document.createElement("span");
      dot.className =
        "freshness-dot " + (member.minutes_ago <= 15 ? "fresh" : member.minutes_ago <= 120 ? "idle" : "stale");

      const time = document.createElement("span");
      time.className = "team-time";
      time.textContent = fmtMinutes(member.minutes_ago);

      row.appendChild(avatar);
      row.appendChild(text);
      row.appendChild(dot);
      row.appendChild(time);
      row.addEventListener("click", () => openPath(`/dashboard/u/${member.handle}`));
      teamList.appendChild(row);
    }

    // Notifications.
    const notifList = el("notif-list");
    notifList.innerHTML = "";
    const notifications = state.notifications || [];
    el("notif-empty").hidden = notifications.length > 0;
    const badge = el("unread-badge");
    const unread = state.unread_count || 0;
    badge.hidden = unread <= 0;
    badge.textContent = String(unread);
    el("btn-mark-read").disabled = unread <= 0;

    for (const item of notifications) {
      const row = document.createElement("div");
      row.className = "notif-row";
      const meta = KIND_META[item.kind] || { emoji: "\u{1F514}", label: "Engram" };

      const top = document.createElement("div");
      top.className = "notif-top";
      const kind = document.createElement("span");
      kind.className = "notif-kind";
      kind.textContent = `${meta.emoji} ${meta.label}`;
      const time = document.createElement("span");
      time.className = "notif-time";
      time.textContent = fmtRelative(item.at);
      top.appendChild(kind);
      top.appendChild(time);

      const body = document.createElement("div");
      body.className = "notif-body";
      body.textContent = item.body;

      row.appendChild(top);
      row.appendChild(body);

      const action = actionFor(item);
      if (action) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "notif-action";
        btn.textContent = action.label;
        btn.addEventListener("click", () => {
          // Acting on a notification consumes it: clear THIS one, then open.
          invoke("mark_read_one", { id: Number(item.id) }).then(render)
            .catch((e) => console.error("mark_read_one failed", e));
          openPath(action.path);
        });
        row.appendChild(btn);
      }

      notifList.appendChild(row);
    }
  }

  // The unsolicited-push hook: Rust `eval`s a call to this after every
  // background poll tick so an already-open popup stays live without
  // the user doing anything.
  window.__engramPush = render;

  function refresh() {
    invoke("get_state").then(render).catch((e) => console.error("get_state failed", e));
  }

  el("btn-invisible").addEventListener("click", () => {
    const next = el("btn-invisible").getAttribute("aria-pressed") !== "true";
    invoke("set_invisible", { invisible: next })
      .then(render)
      .catch((e) => console.error("set_invisible failed", e));
  });
  el("btn-settings").addEventListener("click", () => {
    invoke("open_config_folder").catch((e) => console.error("open_config_folder failed", e));
  });
  el("btn-sign-out").addEventListener("click", () => {
    invoke("sign_out").then(render).catch((e) => console.error("sign_out failed", e));
  });
  el("btn-sign-in").addEventListener("click", () => {
    invoke("sign_in").catch((e) => console.error("sign_in failed", e));
  });
  el("btn-mark-read").addEventListener("click", () => {
    invoke("mark_all_read").then(render).catch((e) => console.error("mark_all_read failed", e));
  });
  el("btn-open-dashboard").addEventListener("click", () => openPath("/dashboard"));
  el("btn-open-rooms").addEventListener("click", () => openPath("/dashboard/rooms"));

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      invoke("hide_popup").catch(() => {});
    }
  });

  // The window is created once, hidden, at app startup and only ever
  // shown/hidden after that (see popup.rs) — `visibilitychange` fires on
  // each show, so this is the "refresh_now() when shown" hook without
  // needing a dedicated Rust->JS signal for it.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      invoke("refresh_now").then(render).catch((e) => console.error("refresh_now failed", e));
    }
  });

  refresh();
})();
