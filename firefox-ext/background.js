/**
 * background.js — Extension background page for the Discogs Label Printer.
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
 *    and is persisted in browser.storage.local under the key "port".
 *  - The context menu handler reads the stored profile/split/preview settings
 *    and sends the same JSON body as the popup's print button.
 */

// Show/enable the browser action only on Discogs release pages.
// URL pattern: discogs.com/release/DIGITS or discogs.com/*/release/DIGITS

const RELEASE_URL = /discogs\.com\/(?:[^/]+\/)?release\/(\d+)/;
const DISCOGS_URL  = /discogs\.com/;
const DEFAULT_PORT = 5679;
const STORAGE_KEYS = ["port", "profile", "split", "preview"];

function updateAction(tabId, url) {
  if (url && DISCOGS_URL.test(url)) {
    browser.browserAction.enable(tabId);
  } else {
    browser.browserAction.disable(tabId);
  }
}

browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.url !== undefined) {
    updateAction(tabId, changeInfo.url);
  }
});

browser.tabs.onActivated.addListener(async ({ tabId }) => {
  const tab = await browser.tabs.get(tabId);
  updateAction(tabId, tab.url);
});

// Disable on all tabs at startup; they'll re-enable via onActivated.
browser.tabs.query({}).then(tabs => {
  for (const tab of tabs) {
    updateAction(tab.id, tab.url);
  }
});

// ── Context menu: right-click a Discogs release link to print ─────────────────

browser.menus.create({
  id: "print-label",
  title: "Print Label",
  contexts: ["link"],
  targetUrlPatterns: [
    "*://*.discogs.com/*/release/*",
    "*://*.discogs.com/release/*",
  ],
});

browser.menus.onClicked.addListener(async (info) => {
  if (info.menuItemId !== "print-label") return;

  const m = RELEASE_URL.exec(info.linkUrl);
  if (!m) return;
  const releaseId = m[1];

  const stored  = await browser.storage.local.get(STORAGE_KEYS);
  const port    = stored.port    || DEFAULT_PORT;
  const profile = stored.profile || "dk1247";
  const split   = stored.split   || false;
  const preview = stored.preview || false;

  // For print jobs the server responds immediately (job is queued); no timeout
  // needed. For preview the server blocks until dt_label finishes, so keep the
  // 30-second guard.
  const controller = new AbortController();
  const timer = preview ? setTimeout(() => controller.abort(), 30_000) : null;
  try {
    const resp = await fetch(`http://localhost:${port}/print`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ release_id: releaseId, profile, preview, split, discs: null }),
      signal: controller.signal,
    });
    const data = await resp.json();
    if (preview && data.preview_urls?.length) {
      for (const url of data.preview_urls) {
        browser.tabs.create({ url: `http://localhost:${port}${url}` });
      }
    }
  } catch (err) {
    console.error("Print Label context menu error:", err);
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
});
