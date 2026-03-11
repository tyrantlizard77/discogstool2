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

  try {
    const resp = await fetch(`http://localhost:${port}/print`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ release_id: releaseId, profile, preview, split, discs: null }),
    });
    const data = await resp.json();
    if (preview && data.preview_urls?.length) {
      for (const url of data.preview_urls) {
        browser.tabs.create({ url: `http://localhost:${port}${url}` });
      }
    }
  } catch (err) {
    console.error("Print Label context menu error:", err);
  }
});
