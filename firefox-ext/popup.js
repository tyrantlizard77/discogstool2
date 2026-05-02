const RELEASE_RE = /discogs\.com\/(?:[^/]+\/)?release\/(\d+)/;
const DEFAULT_PORT = 5679;
const STORAGE_KEYS = ["port", "profile", "split", "hide_bpm", "preview"];

// ── DOM refs ──────────────────────────────────────────────────────────────────
const releaseIdEl  = document.getElementById("release-id");
const profileEl    = document.getElementById("profile");
const splitEl      = document.getElementById("split");
const discsRowEl   = document.getElementById("discs-row");
const discsEl      = document.getElementById("discs");
const hideBpmEl    = document.getElementById("hide-bpm");
const previewEl    = document.getElementById("preview-only");
const printBtn     = document.getElementById("print-btn");
const statusEl     = document.getElementById("status");
const portEl       = document.getElementById("port");

let releaseId = null;

// ── Restore saved settings ────────────────────────────────────────────────────
browser.storage.local.get(STORAGE_KEYS).then(saved => {
  if (saved.port)     portEl.value       = saved.port;
  if (saved.profile)  profileEl.value    = saved.profile;
  if (saved.split)    splitEl.checked    = saved.split;
  if (saved.hide_bpm) hideBpmEl.checked  = saved.hide_bpm;
  if (saved.preview)  previewEl.checked  = saved.preview;
  discsRowEl.classList.toggle("hidden", !splitEl.checked);
  checkConnectionStatus();
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
hideBpmEl.addEventListener("change", () => {
  browser.storage.local.set({ hide_bpm: hideBpmEl.checked });
});
previewEl.addEventListener("change", () => {
  browser.storage.local.set({ preview: previewEl.checked });
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
    releaseIdEl.textContent = "—";
    setStatus("Browse to a release page to print.");
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
  const hideBpm = hideBpmEl.checked;

  printBtn.disabled = true;
  setStatus(preview ? "Generating preview…" : "Sending to printer…");

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30_000);
  try {
    const resp = await fetch(`http://localhost:${port}/print`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ release_id: releaseId, profile, preview, split, discs, hide_bpm: hideBpm }),
      signal: controller.signal,
    });

    const data = await resp.json();

    if (!resp.ok || !data.ok) {
      throw new Error(data.message || `Server error ${resp.status}`);
    }

    if (preview && data.preview_urls?.length) {
      for (const url of data.preview_urls) {
        await browser.tabs.create({ url: `http://localhost:${port}${url}` });
      }
      const count = data.preview_urls.length;
      setStatus(`${count} preview${count > 1 ? "s" : ""} opened in new tab${count > 1 ? "s" : ""}.`, "ok");
    } else {
      setStatus("Label sent to printer.", "ok");
    }
  } catch (err) {
    if (err.name === "AbortError") {
      setStatus("Request timed out — is dt_server running on port " + port + "?", "error");
    } else if (err.name === "TypeError" && err.message.includes("NetworkError")) {
      setStatus("dt_server not running on port " + port, "error");
    } else {
      setStatus(err.message, "error");
    }
  } finally {
    clearTimeout(timer);
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

function checkConnectionStatus() {
  const port = portEl.value || DEFAULT_PORT;
  const SERVICES = [
    { key: "discogs",   elId: "conn-discogs",   label: "Discogs"   },
    { key: "beatport",  elId: "conn-beatport",  label: "Beatport"  },
    { key: "anthropic", elId: "conn-anthropic", label: "Anthropic" },
    { key: "llm",       elId: "conn-llm",       label: "Finder"    },
  ];

  fetch(`http://localhost:${port}/status`, { signal: AbortSignal.timeout(5000) })
    .then(r => r.json())
    .then(data => {
      for (const { key, elId, label } of SERVICES) {
        const el  = document.getElementById(elId);
        const svc = data[key];

        // For the LLM dot, show the configured backend name in the label
        let displayLabel = label;
        if (key === "llm" && svc && svc.backend) {
          displayLabel = svc.backend === "anthropic" ? "Finder (Claude)" : "Finder (Local)";
          el.textContent = "● " + (svc.backend === "anthropic" ? "Claude" : "Local LLM");
        }

        if (!svc || svc.status === "unavailable") {
          el.className = "conn-dot";
          el.title     = displayLabel + ": unavailable";
        } else if (svc.status === "ok") {
          el.className = "conn-dot ok";
          el.title     = displayLabel + ": connected";
        } else {
          el.className = "conn-dot error";
          el.title     = displayLabel + ": " + (svc.message || svc.status);
        }
      }
    })
    .catch(() => {
      for (const { elId, label } of SERVICES) {
        const el = document.getElementById(elId);
        el.className = "conn-dot";
        el.title     = label + ": server unreachable";
      }
    });
}
