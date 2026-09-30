/**
 * background.js — Background script for the Discogs Label Printer.
 *
 * Shared verbatim by the Firefox (MV2 background page) and Chrome (MV3 service
 * worker) builds; only manifest.json differs between them. The two runtimes
 * are reconciled at the top of this file:
 *
 *   - namespace: Firefox exposes `browser`, Chrome exposes `chrome`
 *   - toolbar:   MV2 `browserAction` vs MV3 `action`
 *   - menus:     Firefox `menus` vs Chrome `contextMenus`
 *   - lifetime:  an MV2 background page persists, so setInterval survives; an
 *                MV3 service worker is torn down after ~30s idle, so a long
 *                poll cannot. See pollUntilIdle() for how each is handled.
 *
 * Responsibilities:
 *  1. Enable/disable the browser action (toolbar button) based on whether the
 *     active tab is a Discogs release page (URL matches RELEASE_URL).
 *  2. Register a right-click context menu item ("Print Label") on Discogs
 *     release links, so labels can be printed without opening the popup.
 *  3. Send print requests to dt_server via POST http://localhost:{port}/print.
 *
 * Communication with dt_server:
 *  - All requests go to http://localhost:{port}/ where port defaults to 5679
 *    and is persisted in storage.local under the key "port".
 *  - The context menu handler reads the stored profile and sends the same JSON
 *    body as the popup's print button. The popup's option checkboxes (split,
 *    hide BPM, preview) are per-print and not persisted, so there is nothing
 *    here to inherit: a context-menu click is always a plain print.
 */

// Show/enable the browser action only on Discogs release pages.
// URL pattern: discogs.com/release/DIGITS or discogs.com/*/release/DIGITS

// ── Cross-browser API shim ────────────────────────────────────────────────────

const api    = globalThis.browser ?? globalThis.chrome;
const action = api.action ?? api.browserAction;   // MV3 ?? MV2
const menus  = api.menus  ?? api.contextMenus;    // Firefox ?? Chrome

// An MV3 service worker has no window and is terminated when idle; an MV2
// background page lives as long as the browser does.
const IS_SERVICE_WORKER =
  typeof ServiceWorkerGlobalScope !== "undefined" &&
  globalThis instanceof ServiceWorkerGlobalScope;

const RELEASE_URL = /discogs\.com\/(?:[^/]+\/)?release\/(\d+)/;
const DISCOGS_URL  = /discogs\.com/;
const DEFAULT_PORT = 5679;
const STORAGE_KEYS = ["port", "profile"];

function updateAction(tabId, url) {
  if (url && DISCOGS_URL.test(url)) {
    action.enable(tabId);
  } else {
    action.disable(tabId);
  }
}

api.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.url !== undefined) {
    updateAction(tabId, changeInfo.url);
  }
});

api.tabs.onActivated.addListener(async ({ tabId }) => {
  const tab = await api.tabs.get(tabId);
  updateAction(tabId, tab.url);
});

// Disable on all tabs at startup; they'll re-enable via onActivated.
api.tabs.query({}).then(tabs => {
  for (const tab of tabs) {
    updateAction(tab.id, tab.url);
  }
});

// ── Context menu: right-click a Discogs release link to print ─────────────────

menus.create({
  id: "print-label",
  title: "Print Label",
  contexts: ["link"],
  targetUrlPatterns: [
    "*://*.discogs.com/*/release/*",
    "*://*.discogs.com/release/*",
  ],
});

menus.onClicked.addListener(async (info) => {
  if (info.menuItemId !== "print-label") return;

  const m = RELEASE_URL.exec(info.linkUrl);
  if (!m) return;
  const releaseId = m[1];

  const stored  = await api.storage.local.get(STORAGE_KEYS);
  const port    = stored.port    || DEFAULT_PORT;
  const profile = stored.profile || "dk1247";

  // Print jobs are queued server-side and return immediately, so this only
  // has to wait for the enqueue.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10_000);
  try {
    const resp = await fetch(`http://localhost:${port}/print`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        release_id: releaseId, profile, preview: false, split: false,
        discs: null, hide_bpm: false,
      }),
      signal: controller.signal,
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) {
      throw new Error(data.message || `Server error ${resp.status}`);
    }
    // Don't claim it printed — it's only queued at this point. The badge
    // and the popup's stage list report what actually happens next.
    pollUntilIdle();
  } catch (err) {
    const msg = err.name === "AbortError"
      ? `Request timed out — is dt_server running on port ${port}?`
      : err.message || String(err);
    console.error("Print Label context menu error:", err);
    notify("Print failed", msg, true);
  } finally {
    clearTimeout(timer);
  }
});

function notify(title, message, isError = false) {
  api.notifications.create({
    type:    "basic",
    iconUrl: isError ? "icons/icon-48.png" : "icons/icon-48.png",
    title,
    message,
  });
}

// ── Toolbar badge ─────────────────────────────────────────────────────────────
// Shows queue depth while work is outstanding, and a failure count afterwards.
// Polling only runs while something is active so an idle browser stays quiet.

const BADGE_POLL_MS = 1000;
let badgeTimer = null;
let lastFailures = 0;

async function refreshBadge() {
  const stored = await api.storage.local.get(["port"]);
  const port   = stored.port || DEFAULT_PORT;

  let data;
  try {
    const resp = await fetch(`http://localhost:${port}/progress`,
                             { signal: AbortSignal.timeout(2000) });
    data = await resp.json();
  } catch {
    // Server gone: clear the badge and stop polling until the next print.
    setBadge("", "#666");
    stopPolling();
    return;
  }

  if (data.depth > 0) {
    setBadge(String(data.depth), "#d6844a");
    return true;                       // keep polling
  }

  if (data.failures > 0) {
    setBadge(String(data.failures), "#e06060");
    // Notify once per new failure rather than on every poll.
    if (data.failures > lastFailures) {
      const failed = (data.recent || []).find(j => j.state === "error");
      notify("Print failed", failed?.error || "A print job failed.", true);
    }
    lastFailures = data.failures;
    stopPolling();
    return false;
  }

  lastFailures = 0;
  setBadge("", "#666");
  stopPolling();
  return false;
}

function setBadge(text, colour) {
  action.setBadgeText({ text });
  if (text) {
    action.setBadgeBackgroundColor({ color: colour });
  }
}

// Keeping the badge current means polling, and the two runtimes allow very
// different things here.
//
// MV2 background page: persists, so a 1s setInterval simply works.
//
// MV3 service worker: torn down after ~30s idle, taking any interval with it.
// chrome.alarms survives teardown but its floor is 60s — far too coarse for a
// queue that drains in seconds. So the badge is refreshed at the moments that
// actually matter (a job is queued, the popup opens, the worker wakes) with a
// 1-minute alarm as a backstop. Between those it can lag; the popup polls
// properly at 500ms whenever it is open, which is when anyone is watching.

const BADGE_ALARM = "dt-badge-poll";

function pollUntilIdle() {
  refreshBadge();

  if (IS_SERVICE_WORKER) {
    // periodInMinutes below 1 is silently clamped by Chrome.
    api.alarms?.create(BADGE_ALARM, { periodInMinutes: 1 });
    return;
  }
  if (badgeTimer === null) {
    badgeTimer = setInterval(refreshBadge, BADGE_POLL_MS);
  }
}

function stopPolling() {
  if (IS_SERVICE_WORKER) {
    api.alarms?.clear(BADGE_ALARM);
    return;
  }
  if (badgeTimer !== null) {
    clearInterval(badgeTimer);
    badgeTimer = null;
  }
}

api.alarms?.onAlarm.addListener((alarm) => {
  if (alarm.name === BADGE_ALARM) refreshBadge();
});

// The popup posts its own print requests, so listen for those too and start
// the badge poll on its behalf.
api.runtime.onMessage.addListener((msg) => {
  if (msg?.type === "job-queued")   pollUntilIdle();
  if (msg?.type === "popup-opened") refreshBadge();
});

// Pick up anything already running when the browser starts.
refreshBadge();
