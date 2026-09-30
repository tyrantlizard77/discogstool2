// Firefox exposes `browser`, Chrome exposes `chrome`; both support promises
// for the APIs used here (Chrome does so from MV3 onwards).
const api = globalThis.browser ?? globalThis.chrome;

const RELEASE_RE = /discogs\.com\/(?:[^/]+\/)?release\/(\d+)/;
const DEFAULT_PORT = 5679;
const STORAGE_KEYS = ["port", "profile"];

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
// Only the port and the label profile persist. The three option checkboxes
// (split, hide BPM, preview) are deliberately per-print: they start cleared
// every time the popup opens, and clearOptions() resets them once a label has
// been sent. A forgotten "Preview only" or "Split by disc" silently changing
// the next print was worse than re-ticking a box.
api.storage.local.get(STORAGE_KEYS).then(saved => {
  if (saved.port)     portEl.value    = saved.port;
  if (saved.profile)  profileEl.value = saved.profile;
  checkConnectionStatus();
});

// Persist settings on change
portEl.addEventListener("change", () => {
  api.storage.local.set({ port: portEl.value });
});
profileEl.addEventListener("change", () => {
  api.storage.local.set({ profile: profileEl.value });
});
splitEl.addEventListener("change", () => {
  discsRowEl.classList.toggle("hidden", !splitEl.checked);
  if (!splitEl.checked) discsEl.value = "";
});

function clearOptions() {
  splitEl.checked   = false;
  hideBpmEl.checked = false;
  previewEl.checked = false;
  discsEl.value     = "";
  discsRowEl.classList.add("hidden");
}

// ── Detect release ID from active tab ─────────────────────────────────────────
api.tabs.query({ active: true, currentWindow: true }).then(tabs => {
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
  holdStatus = false;
  setStatus(preview ? "Generating preview…" : "Sending to printer…");

  const controller = new AbortController();
  // Preview is fast (local subprocess); real prints include a Discogs fetch,
  // optional BPM lookup, and physical printing — allow up to 2 minutes.
  const timer = setTimeout(() => controller.abort(), preview ? 30_000 : 120_000);
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
        await api.tabs.create({ url: `http://localhost:${port}${url}` });
      }
      const count = data.preview_urls.length;
      setStatus(`${count} preview${count > 1 ? "s" : ""} opened in new tab${count > 1 ? "s" : ""}.`, "ok");
      holdStatus = true;   // terminal message; don't let polling overwrite it
    } else {
      // Deliberately no status text here. The job is queued server-side and
      // its state changes within milliseconds, so anything written now goes
      // stale immediately — that is how the line ended up reading "Queued."
      // while the label was already printing. pollProgress() owns this line.
      api.runtime.sendMessage({ type: "job-queued" }).catch(() => {});
      // The label is on its way, so the per-print options have served their
      // purpose. Only a real print clears them — a preview is usually one of
      // several, and resetting between previews would mean re-ticking the same
      // boxes to look at the same label again. The guard is on `preview` and
      // not on this branch: a preview that came back without PNG URLs lands
      // here too, and that is not a print.
      if (!preview) clearOptions();
      pollProgress();      // paint real state at once rather than waiting a tick
    }
  } catch (err) {
    if (err.name === "AbortError") {
      setStatus("Request timed out — is dt_server running on port " + port + "?", "error");
    } else if (err.name === "TypeError" && err.message.includes("NetworkError")) {
      setStatus("dt_server not running on port " + port, "error");
    } else {
      setStatus(err.message, "error");
    }
    holdStatus = true;     // a request-level failure outranks job state
  } finally {
    clearTimeout(timer);
    printBtn.disabled = false;
  }
});

// ── Job progress polling ──────────────────────────────────────────────────────
// The popup polls while it is open. Firefox tears the popup down when it loses
// focus, which stops the interval automatically — the toolbar badge (driven by
// background.js) is what keeps reporting once this window is gone.

const POLL_MS = 500;

// Set when a terminal message (preview opened, request failed) should stay
// put; cleared at the start of the next print attempt.
let holdStatus = false;

const progressEl   = document.getElementById("progress");
const jobTitleEl   = document.getElementById("job-title");
const queueCountEl = document.getElementById("queue-count");
const jobErrorEl   = document.getElementById("job-error");
const stageEls     = Object.fromEntries(
  [...document.querySelectorAll("#stages li")].map(li => [li.dataset.stage, li])
);

const MARKS = { pending: "○", active: "◐", done: "●", skip: "–", error: "✕", warn: "!" };

function renderStage(li, info) {
  const state = info?.state ?? "pending";

  // Map server-side stage states onto display classes.
  let cls = "pending";
  if (state === "start" || state === "progress") cls = "active";
  else if (state === "done")        cls = "done";
  else if (state === "skip")        cls = "skip";
  else if (state === "error")       cls = "error";

  li.className = cls;
  li.querySelector(".stage-mark").textContent = MARKS[cls];

  // "2/4" style counts where the server supplied them.
  const count = li.querySelector(".stage-count");
  if (info && info.total != null && info.done != null && info.total > 0) {
    count.textContent = `${info.done}/${info.total}`;
  } else if (info && info.labels != null && state === "done") {
    count.textContent = info.labels > 1 ? `${info.labels} labels` : "";
  } else {
    count.textContent = "";
  }

}

// How long a finished job stays on screen before the panel clears itself.
// Without this the popup keeps showing the last job forever, so reopening it
// hours later looks like something is still in progress.
const RECENT_GRACE_S = 90;

const STAGE_VERB = {
  lookup:  "Looking up release",
  samples: "Downloading samples",
  bpm:     "Analysing BPM",
  render:  "Rendering label",
  print:   "Printing",
};

function describeJob(job, waiting) {
  if (job.state === "error")  return [job.error || "Failed.", "error"];
  if (job.state === "queued") {
    return [waiting > 1 ? `Queued — ${waiting} ahead.` : "Queued.", ""];
  }
  if (job.state === "done") {
    return [job.kind === "preview" ? "Preview ready." : "Printed.", "ok"];
  }

  // Running: report the furthest stage that is actually in progress.
  let text = "Working…";
  for (const name of Object.keys(STAGE_VERB)) {
    const info = job.stages?.[name];
    if (!info) continue;
    if (info.state === "start" || info.state === "progress") {
      text = STAGE_VERB[name];
      if (info.total > 0 && info.done != null) text += ` ${info.done}/${info.total}`;
      text += "…";
    }
  }
  return [text, ""];
}

function renderProgress(data) {
  const now    = Date.now() / 1000;
  const recent = data.recent?.[0] ?? null;
  const fresh  = recent && (now - (recent.finished ?? 0)) < RECENT_GRACE_S;
  const queued = data.queued ?? [];

  // Prefer the running job; if nothing is running yet, show the one about to
  // run (there is a brief window after POST where the job exists but the
  // worker has not picked it up); otherwise show a just-finished job.
  const job = data.current ?? queued[0] ?? (fresh ? recent : null);

  if (!job && !data.depth) {
    progressEl.classList.add("hidden");
    if (!holdStatus) setStatus("");
    return;
  }
  progressEl.classList.remove("hidden");

  // Jobs waiting *behind* whichever one is on screen.
  const waiting = data.current ? queued.length : Math.max(0, queued.length - 1);
  queueCountEl.textContent = waiting === 1 ? "1 queued" : `${waiting} queued`;
  queueCountEl.classList.toggle("hidden", waiting === 0);

  if (!job) return;

  jobTitleEl.textContent = job.title || `r${job.release_id}`;
  for (const [name, li] of Object.entries(stageEls)) {
    renderStage(li, job.stages?.[name]);
  }
  jobErrorEl.textContent = job.error || "";
  jobErrorEl.classList.toggle("hidden", !job.error);

  // The status line reflects live job state — never a value captured earlier.
  if (!holdStatus) {
    const [text, cls] = describeJob(job, waiting);
    setStatus(text, cls);
  }
}

async function pollProgress() {
  const port = portEl.value || DEFAULT_PORT;
  try {
    const resp = await fetch(`http://localhost:${port}/progress`,
                             { signal: AbortSignal.timeout(2000) });
    renderProgress(await resp.json());
  } catch {
    // dt_server unreachable — leave whatever was last drawn in place rather
    // than flickering the panel away on a single dropped poll.
  }
}

api.runtime.sendMessage({ type: "popup-opened" }).catch(() => {});
pollProgress();
setInterval(pollProgress, POLL_MS);

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
  ];

  fetch(`http://localhost:${port}/status`, { signal: AbortSignal.timeout(5000) })
    .then(r => r.json())
    .then(data => {
      for (const { key, elId, label } of SERVICES) {
        const el  = document.getElementById(elId);
        const svc = data[key];

        if (!svc || svc.status === "unavailable") {
          el.className = "conn-dot";
          el.title     = label + ": unavailable";
        } else if (svc.status === "ok") {
          el.className = "conn-dot ok";
          el.title     = label + ": connected";
        } else {
          el.className = "conn-dot error";
          el.title     = label + ": " + (svc.message || svc.status);
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
