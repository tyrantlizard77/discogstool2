"""Tests for the browser extensions (Firefox MV2 and Chrome MV3).

Everything except manifest.json is shared between the two builds, so these
cover three things:

  1. popup.js behaves identically under Firefox's `browser` namespace and
     Chrome's `chrome` namespace (tests/js/popup_harness.js, run under both).
  2. The two manifests stay consistent with each other and with what their
     manifest version requires — a mismatch here is invisible until the
     extension refuses to load.
  3. The shared sources parse.

Node-dependent tests skip when node isn't installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

_ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXT    = os.path.join(_ROOT, "ext")
_SHARED = os.path.join(_EXT, "shared")
_HARNESS = os.path.join(_ROOT, "tests", "js", "popup_harness.js")

_SHARED_FILES = ("popup.html", "popup.js", "background.js")

_needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                 reason="node not installed")


def _manifest(browser):
    with open(os.path.join(_EXT, browser, "manifest.json")) as f:
        return json.load(f)


# ─── Shared sources ───────────────────────────────────────────────────────────

class TestSharedSources:
    @pytest.mark.parametrize("name", _SHARED_FILES)
    def test_shared_file_exists(self, name):
        assert os.path.isfile(os.path.join(_SHARED, name))

    @pytest.mark.parametrize("name", ["popup.js", "background.js"])
    @_needs_node
    def test_shared_script_parses(self, name):
        r = subprocess.run(["node", "--check", os.path.join(_SHARED, name)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    @pytest.mark.parametrize("name", ["popup.js", "background.js"])
    def test_no_bare_browser_namespace(self, name):
        """Both builds share these files, so `browser.` must not appear in code.

        Chrome has no `browser` global; a bare reference would throw there but
        work fine in Firefox, so this is exactly the kind of thing that only
        shows up after shipping.
        """
        src = open(os.path.join(_SHARED, name), encoding="utf-8").read()
        code = "\n".join(
            line for line in src.splitlines()
            if not line.lstrip().startswith(("*", "//", "/*"))
        )
        assert "browser." not in code

    def test_no_local_llm_references(self):
        """dt_find runs on Claude only; nothing in the UI may offer a local LLM."""
        for name in _SHARED_FILES:
            src = open(os.path.join(_SHARED, name), encoding="utf-8").read()
            assert "conn-llm" not in src, name
            assert "Local LLM" not in src, name

    def test_every_element_id_popup_js_uses_exists_in_html(self):
        """popup.js and popup.html have to agree on element IDs.

        getElementById returns null rather than throwing, so a dropped element
        surfaces later as "cannot read properties of null" in whichever branch
        happens to touch it — which is how removing a footer dot from one file
        and not the other would ship unnoticed.
        """
        import re
        js   = open(os.path.join(_SHARED, "popup.js"), encoding="utf-8").read()
        html = open(os.path.join(_SHARED, "popup.html"), encoding="utf-8").read()

        html_ids = set(re.findall(r'id="([^"]+)"', html))
        js_ids   = set(re.findall(r'getElementById\(\s*"([^"]+)"\s*\)', js))
        js_ids  |= set(re.findall(r'elId:\s*"([^"]+)"', js))   # connection dots

        assert js_ids, "found no element IDs in popup.js — regex is stale"
        assert js_ids <= html_ids, f"missing from popup.html: {sorted(js_ids - html_ids)}"

    def test_api_shim_accepts_either_namespace(self):
        for name in ("popup.js", "background.js"):
            src = open(os.path.join(_SHARED, name), encoding="utf-8").read()
            assert "globalThis.browser ?? globalThis.chrome" in src


# ─── popup behaviour under both namespaces ────────────────────────────────────

class TestPopupUnderBothNamespaces:
    @pytest.mark.parametrize("namespace", ["browser", "chrome"])
    @_needs_node
    def test_status_transitions(self, namespace):
        env = dict(os.environ, EXT_NAMESPACE=namespace)
        r = subprocess.run(["node", _HARNESS], capture_output=True, text=True,
                           env=env)
        assert r.returncode == 0, r.stdout + r.stderr


# ─── Manifests ────────────────────────────────────────────────────────────────

class TestManifests:
    def test_versions_match(self):
        """A version skew between builds means one browser silently lags."""
        assert _manifest("firefox")["version"] == _manifest("chrome")["version"]

    def test_names_match(self):
        assert _manifest("firefox")["name"] == _manifest("chrome")["name"]

    def test_firefox_is_mv2(self):
        assert _manifest("firefox")["manifest_version"] == 2

    def test_chrome_is_mv3(self):
        """Chrome no longer accepts MV2."""
        assert _manifest("chrome")["manifest_version"] == 3

    def test_firefox_uses_background_scripts(self):
        bg = _manifest("firefox")["background"]
        assert "scripts" in bg and "background.js" in bg["scripts"]

    def test_chrome_uses_service_worker(self):
        assert _manifest("chrome")["background"]["service_worker"] == "background.js"

    def test_firefox_uses_browser_action(self):
        m = _manifest("firefox")
        assert "browser_action" in m and "action" not in m

    def test_chrome_uses_action(self):
        m = _manifest("chrome")
        assert "action" in m and "browser_action" not in m

    def test_chrome_declares_localhost_host_permission(self):
        """MV3 needs host_permissions to reach dt_server on localhost."""
        hosts = _manifest("chrome")["host_permissions"]
        assert any("localhost" in h for h in hosts)

    def test_chrome_declares_alarms_permission(self):
        """The service-worker badge backstop needs chrome.alarms."""
        assert "alarms" in _manifest("chrome")["permissions"]

    def test_chrome_uses_context_menus_permission(self):
        """Chrome spells it contextMenus; Firefox spells it menus."""
        assert "contextMenus" in _manifest("chrome")["permissions"]

    def test_firefox_uses_menus_permission(self):
        assert "menus" in _manifest("firefox")["permissions"]

    @pytest.mark.parametrize("browser", ["firefox", "chrome"])
    def test_popup_and_icons_referenced_exist(self, browser):
        m = _manifest(browser)
        act = m.get("action") or m.get("browser_action")
        assert os.path.isfile(os.path.join(_SHARED, act["default_popup"]))
        for path in act["default_icon"].values():
            assert os.path.isfile(os.path.join(_SHARED, path))

    @pytest.mark.parametrize("browser", ["firefox", "chrome"])
    def test_no_stray_host_permissions(self, browser):
        """The extension must only reach localhost, never arbitrary hosts."""
        for host in _manifest(browser).get("host_permissions", []):
            assert "localhost" in host or "127.0.0.1" in host
