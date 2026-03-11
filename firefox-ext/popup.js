const RELEASE_RE = /discogs\.com\/(?:[^/]+\/)?release\/(\d+)/;
const DEFAULT_PORT = 5679;
const STORAGE_KEYS = ["port", "profile", "split"];

// ── DOM refs ──────────────────────────────────────────────────────────────────
const releaseIdEl  = document.getElementById("release-id");
const profileEl    = document.getElementById("profile");
const splitEl      = document.getElementById("split");
const discsRowEl   = document.getElementById("discs-row");
const discsEl      = document.getElementById("discs");
const previewEl    = document.getElementById("preview-only");
const printBtn     = document.getElementById("print-btn");
const statusEl     = document.getElementById("status");
const portEl       = document.getElementById("port");

let releaseId = null;

// ── Restore saved settings ────────────────────────────────────────────────────
browser.storage.local.get(STORAGE_KEYS).then(saved => {
  if (saved.port)    portEl.value    = saved.port;
  if (saved.profile) profileEl.value = saved.profile;
  if (saved.split)   splitEl.checked = saved.split;
  discsRowEl.classList.toggle("hidden", !splitEl.checked);
});

// Persist settings on change
portEl.addEventListener("change", () => {
  browser.storage.local.set({ port: portEl.value });
});
profileEl.addEventListener("change", () => {
  browser.storage.local.set({ profile: profileEl.value });
});
splitEl.addEventListener("change", () => {
  browser.storage.local.set({ split: splitEl.checked });
  discsRowEl.classList.toggle("hidden", !splitEl.checked);
  if (!splitEl.checked) discsEl.value = "";
});

// ── Detect release ID from active tab ─────────────────────────────────────────
browser.tabs.query({ active: true, currentWindow: true }).then(tabs => {
  const url = tabs[0]?.url ?? "";
  const m = RELEASE_RE.exec(url);
  if (m) {
    releaseId = m[1];
    releaseIdEl.textContent = "r" + releaseId;
    printBtn.disabled = false;
  } else {
    releaseIdEl.textContent = "no release";
    setStatus("Not on a Discogs release page.", "error");
  }
});

// ── Print / Preview ───────────────────────────────────────────────────────────
printBtn.addEventListener("click", async () => {
  if (!releaseId) return;

  const port    = portEl.value || DEFAULT_PORT;
  const profile = profileEl.value;
  const preview = previewEl.checked;
  const split   = splitEl.checked;
  const discs   = parseDiscs(discsEl.value);

  printBtn.disabled = true;
  setStatus(preview ? "Generating preview…" : "Sending to printer…");

  try {
    const resp = await fetch(`http://localhost:${port}/print`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ release_id: releaseId, profile, preview, split, discs }),
    });

    const data = await resp.json();

    if (!resp.ok || !data.ok) {
      throw new Error(data.message || `Server error ${resp.status}`);
    }

    if (preview && data.pngs?.length) {
      for (const png of data.pngs) {
        await browser.tabs.create({ url: "data:image/png;base64," + png });
      }
      const count = data.pngs.length;
      setStatus(`${count} preview${count > 1 ? "s" : ""} opened in new tab${count > 1 ? "s" : ""}.`, "ok");
    } else {
      setStatus("Label sent to printer.", "ok");
    }
  } catch (err) {
    if (err.name === "TypeError" && err.message.includes("NetworkError")) {
      setStatus("dt_server not running on port " + port, "error");
    } else {
      setStatus(err.message, "error");
    }
  } finally {
    printBtn.disabled = false;
  }
});

// ── Helpers ───────────────────────────────────────────────────────────────────

function parseDiscs(raw) {
  // Accept "1 3", "1,3", "1, 3" → [1, 3]. Returns null if blank.
  const nums = raw.trim().split(/[\s,]+/).map(Number).filter(n => Number.isInteger(n) && n > 0);
  return nums.length ? nums : null;
}

function setStatus(msg, cls = "") {
  statusEl.textContent = msg;
  statusEl.className = cls;
}
