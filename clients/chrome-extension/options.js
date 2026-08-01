// Engram Notifications — options page. Relies on common.js (loaded first)
// for storage/auth helpers.

const originInput = document.getElementById("origin");
const pollSelect = document.getElementById("poll-interval");
const status = document.getElementById("status");
const signinBtn = document.getElementById("signin");
const signoutBtn = document.getElementById("signout");

function setStatus(text, kind) {
  status.textContent = text;
  status.className = kind || "";
}

async function refreshSignedInState() {
  const token = await getToken();
  signinBtn.textContent = token ? "Re-sign in" : "Sign in with Engram";
  signoutBtn.hidden = !token;
}

async function load() {
  originInput.value = await getOrigin();
  pollSelect.value = String(await getPollMinutes());
  await refreshSignedInState();
}

document.getElementById("save-origin").addEventListener("click", async () => {
  const origin = normalizeOrigin(originInput.value);
  if (!origin) {
    setStatus("Enter a valid https:// origin (no path), e.g. https://engram.example.com", "err");
    return;
  }
  if (!(await ensureOriginPermission(origin))) {
    setStatus(
      "Permission denied — Chrome needs access to this origin to poll it. Try again, or load " +
        "the extension unpacked with the origin added to host_permissions.",
      "err",
    );
    return;
  }
  await chrome.storage.local.set({ engramOrigin: origin });
  originInput.value = origin;
  setStatus("Saved. Sign in again if you switched to a different Engram instance.", "ok");
});

pollSelect.addEventListener("change", async () => {
  await setPollMinutes(Number(pollSelect.value));
  setStatus("Poll interval updated.", "ok");
});

signinBtn.addEventListener("click", async () => {
  setStatus("Opening Engram sign-in…", "");
  try {
    const origin = normalizeOrigin(originInput.value) || DEFAULT_ORIGIN;
    await signIn(origin);
    await refreshSignedInState();
    setStatus("Signed in — you're connected.", "ok");
    chrome.runtime.sendMessage({ type: "poll-now" });
  } catch (err) {
    setStatus(err.message || "Sign-in was cancelled or failed.", "err");
  }
});

signoutBtn.addEventListener("click", async () => {
  await signOut();
  await refreshSignedInState();
  setStatus("Signed out.", "ok");
});

load();
