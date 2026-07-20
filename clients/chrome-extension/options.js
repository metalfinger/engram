const DEFAULT_ORIGIN = "https://engram.metalfinger.xyz";

const originInput = document.getElementById("origin");
const tokenInput = document.getElementById("token");
const status = document.getElementById("status");

function setStatus(text, kind) {
  status.textContent = text;
  status.className = kind || "";
}

function normalizeOrigin(raw) {
  const trimmed = raw.trim().replace(/\/+$/, "");
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

async function load() {
  const { engramOrigin, engramToken } = await chrome.storage.sync.get([
    "engramOrigin",
    "engramToken",
  ]);
  originInput.value = engramOrigin || DEFAULT_ORIGIN;
  tokenInput.value = engramToken || "";
}

document.getElementById("save").addEventListener("click", async () => {
  const token = tokenInput.value.trim();
  if (!token) {
    setStatus("Paste your extension token from the Engram dashboard.", "err");
    return;
  }

  const origin = normalizeOrigin(originInput.value);
  if (!origin) {
    setStatus("Enter a valid https:// origin (no path), e.g. https://engram.example.com", "err");
    return;
  }

  const originPattern = `${origin}/*`;
  try {
    const granted = await chrome.permissions.request({ origins: [originPattern] });
    if (!granted) {
      setStatus(
        "Permission denied — Chrome needs access to this origin to poll it. Try again, or load the extension unpacked with the origin added to host_permissions.",
        "err",
      );
      return;
    }
  } catch {
    setStatus("Could not request permission for that origin.", "err");
    return;
  }

  await chrome.storage.sync.set({ engramOrigin: origin, engramToken: token });
  originInput.value = origin;
  tokenInput.value = token;
  setStatus("Saved.", "ok");
});

load();
