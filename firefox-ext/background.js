// Show/enable the browser action only on Discogs release pages.
// URL pattern: discogs.com/release/DIGITS or discogs.com/*/release/DIGITS

const RELEASE_URL = /discogs\.com\/(?:[^/]+\/)?release\/(\d+)/;

function updateAction(tabId, url) {
  if (url && RELEASE_URL.test(url)) {
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
