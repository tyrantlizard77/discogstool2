"""Tests for dt_find — voice/text vinyl record finder.

Covers:
  - _fmt_search_args: search parameter summary formatting
  - load_config / save_config: file I/O roundtrip
  - SYSTEM_PROMPT: sanity checks (contains key guidance phrases)
  - TOOLS: Anthropic-native tool schemas
  - _tool_search_discogs: result formatting with a mocked Discogs client
  - AnthropicBackend: availability check and agent loop with mocked requests
  - create_backend: factory builds the backend from find_config
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# conftest.py already adds the project root to sys.path.
# dt_find has no .py extension; load it explicitly with SourceFileLoader.
import importlib.machinery

_DT_FIND_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dt_find"
)
_loader = importlib.machinery.SourceFileLoader("dt_find", _DT_FIND_PATH)
_spec = importlib.util.spec_from_loader("dt_find", _loader)
dt_find = importlib.util.module_from_spec(_spec)
sys.modules["dt_find"] = dt_find
_loader.exec_module(dt_find)

from dt_find import (  # noqa: E402  (after dynamic import)
    SYSTEM_PROMPT,
    TOOLS,
    AnthropicBackend,
    _fmt_search_args,
    create_backend,
    load_config,
    save_config,
)


# ─── _fmt_search_args ─────────────────────────────────────────────────────────

class TestFmtSearchArgs:
    def test_empty_dict(self):
        assert _fmt_search_args({}) == "(no parameters)"

    def test_single_param(self):
        result = _fmt_search_args({"q": "Miles Davis"})
        assert "q=" in result
        assert "Miles Davis" in result

    def test_multiple_params_all_included(self):
        result = _fmt_search_args({"artist": "Boards of Canada", "title": "Music Has The Right"})
        assert "artist=" in result
        assert "title=" in result

    def test_none_values_excluded(self):
        result = _fmt_search_args({"q": "test", "artist": None, "year": None})
        assert "artist" not in result
        assert "year" not in result
        assert "q=" in result

    def test_empty_string_values_excluded(self):
        result = _fmt_search_args({"q": "", "title": "Something"})
        # Empty string is falsy — should be excluded
        assert "q=" not in result
        assert "title=" in result

    def test_known_keys_only(self):
        # Unknown keys (not in the display list) should be silently ignored
        result = _fmt_search_args({"unknown_key": "value", "q": "search"})
        assert "unknown_key" not in result
        assert "q=" in result

    def test_all_supported_keys(self):
        args = {k: f"val-{k}" for k in ("q", "artist", "title", "label", "year", "catno", "format")}
        result = _fmt_search_args(args)
        for k in args:
            assert k in result


# ─── load_config / save_config ───────────────────────────────────────────────

class TestLoadSaveConfig:
    """find_config I/O. The parser is generic, so most of these use an
    arbitrary key; `find_anthropic_model` is the only key dt_find reads today.
    """

    def test_missing_file_returns_empty_dict(self, tmp_path):
        config_path = str(tmp_path / "find_config")
        with patch.object(dt_find.util, "userfile", return_value=config_path):
            result = load_config()
        assert result == {}

    def test_roundtrip(self, tmp_path):
        config_path = str(tmp_path / "find_config")
        original = {
            "find_anthropic_model": "claude-test-model",
            "some_other_key": "kept-verbatim",
        }
        with patch.object(dt_find.util, "userfile", return_value=config_path):
            save_config(original)
            result = load_config()
        assert result["find_anthropic_model"] == "claude-test-model"
        assert result["some_other_key"] == "kept-verbatim"

    def test_comment_lines_ignored(self, tmp_path):
        config_path = str(tmp_path / "find_config")
        with open(config_path, "w") as f:
            f.write("# This is a comment\n")
            f.write("find_anthropic_model=claude-test-model\n")
        with patch.object(dt_find.util, "userfile", return_value=config_path):
            result = load_config()
        assert "find_anthropic_model" in result
        assert "#" not in result.get("find_anthropic_model", "")

    def test_value_with_equals_sign(self, tmp_path):
        """Values containing '=' should be preserved correctly (split on first '=' only)."""
        config_path = str(tmp_path / "find_config")
        with open(config_path, "w") as f:
            f.write("some_url=http://host:8000/v1?key=val\n")
        with patch.object(dt_find.util, "userfile", return_value=config_path):
            result = load_config()
        assert result["some_url"] == "http://host:8000/v1?key=val"

    def test_saved_file_is_human_readable(self, tmp_path):
        config_path = str(tmp_path / "find_config")
        with patch.object(dt_find.util, "userfile", return_value=config_path):
            save_config({"find_anthropic_model": "claude-test-model"})
        with open(config_path) as f:
            content = f.read()
        assert "find_anthropic_model=claude-test-model" in content

    def test_empty_lines_skipped(self, tmp_path):
        config_path = str(tmp_path / "find_config")
        with open(config_path, "w") as f:
            f.write("\n\nfind_anthropic_model=claude-test-model\n\n")
        with patch.object(dt_find.util, "userfile", return_value=config_path):
            result = load_config()
        assert result == {"find_anthropic_model": "claude-test-model"}

    def test_model_key_roundtrips(self, tmp_path):
        """--model writes this key; create_backend reads it back."""
        config_path = str(tmp_path / "find_config")
        with patch.object(dt_find.util, "userfile", return_value=config_path):
            save_config({"find_anthropic_model": "claude-opus-4-6"})
            result = load_config()
        assert result["find_anthropic_model"] == "claude-opus-4-6"


# ─── SYSTEM_PROMPT sanity checks ─────────────────────────────────────────────

class TestSystemPrompt:
    def test_not_empty(self):
        assert SYSTEM_PROMPT.strip() != ""

    def test_warns_against_overconfident_selection(self):
        """The prompt must instruct the model to only call select_release when confident."""
        assert "confident" in SYSTEM_PROMPT.lower()

    def test_mentions_select_release(self):
        """The prompt must reference the select_release tool so the model knows to use it."""
        assert "select_release" in SYSTEM_PROMPT

    def test_mentions_vinyl(self):
        """The prompt should orient the model towards vinyl records."""
        assert "vinyl" in SYSTEM_PROMPT.lower()

    def test_kind_of_blue_example(self):
        """The prompt includes the canonical 'blue one = Kind of Blue' example."""
        assert "Kind of Blue" in SYSTEM_PROMPT


# ─── TOOLS sanity checks ──────────────────────────────────────────────────────

class TestTools:
    def test_has_two_tools(self):
        assert len(TOOLS) == 2

    def test_tool_names(self):
        names = {t["name"] for t in TOOLS}
        assert "search_discogs" in names
        assert "select_release" in names

    def test_uses_input_schema_not_parameters(self):
        """Anthropic format uses input_schema, not parameters (OpenAI format)."""
        for tool in TOOLS:
            assert "input_schema" in tool
            assert "parameters" not in tool

    def test_select_release_requires_fields(self):
        select = next(t for t in TOOLS if t["name"] == "select_release")
        required = select["input_schema"].get("required", [])
        assert "release_id" in required
        assert "reasoning" in required


# ─── _tool_search_discogs ────────────────────────────────────────────────────

class TestToolSearchDiscogs:
    def _make_mock_item(self, release_id, title, label="Test Label",
                        catno="CAT001", year="2000", country="UK",
                        fmt=None, uri=None):
        """Build a minimal mock Discogs search result item."""
        item = MagicMock()
        item.data = {
            "id": release_id,
            "title": title,           # Discogs format: "Artist - Album"
            "label": [label],
            "catno": catno,
            "year": year,
            "country": country,
            "format": fmt or ["Vinyl", "LP"],
            "uri": uri or f"/release/{release_id}",
        }
        return item

    def _mock_client(self, items):
        """Return a mock get_client_instance() result that yields *items* on search."""
        page = MagicMock()
        page.__iter__ = MagicMock(return_value=iter(items))
        page.__len__ = MagicMock(return_value=len(items))

        results = MagicMock()
        results.page = MagicMock(return_value=page)
        results.count = len(items)

        dc = MagicMock()
        dc.search = MagicMock(return_value=results)
        return dc

    def test_returns_results_list(self):
        items = [self._make_mock_item(1001, "Artist A - Album A")]
        dc = self._mock_client(items)
        with patch.object(dt_find.client_interface, "get_client_instance", return_value=dc):
            result = dt_find._tool_search_discogs(q="Artist A")
        assert "results" in result
        assert isinstance(result["results"], list)

    def test_artist_title_split(self):
        """Discogs returns 'Artist - Title'; we should split them."""
        items = [self._make_mock_item(42, "Miles Davis - Kind of Blue")]
        dc = self._mock_client(items)
        with patch.object(dt_find.client_interface, "get_client_instance", return_value=dc):
            result = dt_find._tool_search_discogs(q="Kind of Blue")
        r = result["results"][0]
        assert r["artist"] == "Miles Davis"
        assert r["title"] == "Kind of Blue"
        assert r["id"] == 42

    def test_no_artist_separator(self):
        """Items without ' - ' in title should have empty artist."""
        items = [self._make_mock_item(7, "Untitled Album")]
        dc = self._mock_client(items)
        with patch.object(dt_find.client_interface, "get_client_instance", return_value=dc):
            result = dt_find._tool_search_discogs(q="Untitled")
        r = result["results"][0]
        assert r["artist"] == ""
        assert r["title"] == "Untitled Album"

    def test_url_field_built_from_uri(self):
        items = [self._make_mock_item(99, "A - B", uri="/release/99")]
        dc = self._mock_client(items)
        with patch.object(dt_find.client_interface, "get_client_instance", return_value=dc):
            result = dt_find._tool_search_discogs(q="A")
        assert result["results"][0]["url"] == "https://www.discogs.com/release/99"

    def test_passes_kwargs_to_search(self):
        """Parameters like artist, title, label should be forwarded to dc.search()."""
        items = []
        dc = self._mock_client(items)
        with patch.object(dt_find.client_interface, "get_client_instance", return_value=dc):
            dt_find._tool_search_discogs(
                artist="Aphex Twin",
                title="Selected Ambient Works",
                label="Warp",
                year="1992",
                format="Vinyl",
            )
        call_kwargs = dc.search.call_args[1]
        assert call_kwargs["artist"] == "Aphex Twin"
        assert call_kwargs["release_title"] == "Selected Ambient Works"  # Discogs field name
        assert call_kwargs["label"] == "Warp"
        assert call_kwargs["year"] == "1992"
        assert call_kwargs["format"] == "Vinyl"

    def test_search_error_returns_error_dict(self):
        dc = MagicMock()
        dc.search.side_effect = Exception("Network timeout")
        with patch.object(dt_find.client_interface, "get_client_instance", return_value=dc):
            result = dt_find._tool_search_discogs(q="test")
        assert "error" in result
        assert result["results"] == []

    def test_count_field_present(self):
        items = [self._make_mock_item(i, f"A - B{i}") for i in range(3)]
        dc = self._mock_client(items)
        with patch.object(dt_find.client_interface, "get_client_instance", return_value=dc):
            result = dt_find._tool_search_discogs(q="B")
        assert "count" in result


# ─── AnthropicBackend ─────────────────────────────────────────────────────────

def _make_anthropic_response(text_blocks=None, tool_uses=None):
    """Build a minimal Anthropic API response dict."""
    content = []
    for text in (text_blocks or []):
        content.append({"type": "text", "text": text})
    for tc in (tool_uses or []):
        content.append({
            "type":  "tool_use",
            "id":    tc["id"],
            "name":  tc["name"],
            "input": tc["input"],
        })
    return {"content": content, "stop_reason": "end_turn" if not tool_uses else "tool_use"}


class TestAnthropicBackend:
    def _make_backend(self, model=None):
        return AnthropicBackend(model=model)

    def _patch_api_key(self, key="sk-ant-test"):
        return patch.object(AnthropicBackend, "_get_api_key", return_value=key)

    def _patch_requests_post(self, responses: list):
        """Patch requests.post to return *responses* (dicts) in order."""
        mock_responses = []
        for resp_data in responses:
            r = MagicMock()
            r.json.return_value = resp_data
            r.raise_for_status = MagicMock()
            mock_responses.append(r)
        return patch("requests.post", side_effect=mock_responses)

    def test_name_includes_model(self):
        b = AnthropicBackend(model="claude-test-model")
        assert "claude-test-model" in b.name

    def test_default_model(self):
        b = AnthropicBackend()
        assert "haiku" in b.name.lower()

    def test_check_available_no_key(self):
        b = self._make_backend()
        with patch.object(b, "_get_api_key", return_value=None):
            ok, msg = b.check_available()
        assert not ok
        assert "api key" in msg.lower()

    def test_check_available_with_key(self):
        b = self._make_backend()
        with patch.object(b, "_get_api_key", return_value="sk-ant-test"):
            ok, msg = b.check_available()
        assert ok
        assert msg == ""

    def test_select_release_returns_id(self):
        """AnthropicBackend returns (release_id, reasoning) on select_release tool use."""
        api_resp = _make_anthropic_response(tool_uses=[{
            "id":    "tu1",
            "name":  "select_release",
            "input": {"release_id": 55555, "reasoning": "Matched by title"},
        }])

        with self._patch_api_key():
            with self._patch_requests_post([api_resp]):
                with patch.object(dt_find, "_tool_search_discogs", return_value={"results": []}):
                    b = self._make_backend()
                    release_id, reasoning = b.run_agent("Miles Davis blue one")
        assert release_id == 55555
        assert reasoning == "Matched by title"

    def test_search_then_select(self):
        """AnthropicBackend: search first, then select on the second turn."""
        search_resp = _make_anthropic_response(tool_uses=[{
            "id":    "tu-search",
            "name":  "search_discogs",
            "input": {"q": "Kind of Blue"},
        }])
        select_resp = _make_anthropic_response(tool_uses=[{
            "id":    "tu-select",
            "name":  "select_release",
            "input": {"release_id": 777, "reasoning": "Found it"},
        }])
        search_result = {"count": 1, "results": [{"id": 777, "artist": "Miles Davis",
                                                   "title": "Kind of Blue"}]}

        with self._patch_api_key():
            with self._patch_requests_post([search_resp, select_resp]):
                with patch.object(dt_find, "_tool_search_discogs", return_value=search_result):
                    b = self._make_backend()
                    release_id, _ = b.run_agent("blue Miles Davis record")
        assert release_id == 777

    def test_text_response_returns_none(self):
        """If the model responds with plain text (no tool use), return (None, '')."""
        api_resp = _make_anthropic_response(text_blocks=["I could not find that record."])

        with self._patch_api_key():
            with self._patch_requests_post([api_resp]):
                b = self._make_backend()
                release_id, _ = b.run_agent("something obscure")
        assert release_id is None

    def test_api_error_returns_none(self):
        """HTTP errors from the Anthropic API return (None, '') gracefully."""
        with self._patch_api_key():
            with patch("requests.post", side_effect=Exception("Network error")):
                b = self._make_backend()
                release_id, _ = b.run_agent("test")
        assert release_id is None

    def test_no_api_key_returns_none(self):
        """With no API key configured, run_agent returns (None, '') immediately."""
        with patch.object(AnthropicBackend, "_get_api_key", return_value=None):
            b = self._make_backend()
            release_id, _ = b.run_agent("test")
        assert release_id is None

    def test_unknown_tool_gets_error_result(self):
        """Unknown tool names receive an error tool_result, not a crash."""
        bad_resp = _make_anthropic_response(tool_uses=[{
            "id":    "tu-bad",
            "name":  "delete_everything",
            "input": {},
        }])
        text_resp = _make_anthropic_response(text_blocks=["Sorry."])

        with self._patch_api_key():
            with self._patch_requests_post([bad_resp, text_resp]):
                b = self._make_backend()
                release_id, _ = b.run_agent("test")
        assert release_id is None

    def test_get_api_key_reads_env(self, tmp_path, monkeypatch):
        """_get_api_key falls back to ANTHROPIC_API_KEY env var."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key-123")
        # Patch userfile to a non-existent path so it skips the JSON file
        with patch.object(dt_find.util, "userfile", return_value=str(tmp_path / "nope.json")):
            b = self._make_backend()
            key = b._get_api_key()
        assert key == "env-key-123"

    def test_get_api_key_reads_beatport_auth(self, tmp_path):
        """_get_api_key reads anthropic_api_key from beatport_auth.json."""
        auth_file = tmp_path / "beatport_auth.json"
        auth_file.write_text(json.dumps({"anthropic_api_key": "json-key-456"}))
        with patch.object(dt_find.util, "userfile", return_value=str(auth_file)):
            b = self._make_backend()
            key = b._get_api_key()
        assert key == "json-key-456"


# ─── create_backend factory ───────────────────────────────────────────────────

class TestCreateBackend:
    def test_default_model(self):
        b = create_backend({})
        assert isinstance(b, AnthropicBackend)
        assert AnthropicBackend.DEFAULT_MODEL in b.name

    def test_find_anthropic_model_override(self):
        b = create_backend({"find_anthropic_model": "claude-opus-4-6"})
        assert "claude-opus-4-6" in b.name

    def test_blank_model_falls_back_to_default(self):
        b = create_backend({"find_anthropic_model": ""})
        assert AnthropicBackend.DEFAULT_MODEL in b.name

    def test_stale_local_llm_keys_ignored(self):
        """find_config files written before the local backend was removed still
        carry backend/llm_url/llm_model. They must not affect anything."""
        b = create_backend({
            "backend":   "local",
            "llm_url":   "http://wintermute.local:8000/v1",
            "llm_model": "some-local-model",
        })
        assert isinstance(b, AnthropicBackend)
        assert AnthropicBackend.DEFAULT_MODEL in b.name


# ─── _dispatch_tool ───────────────────────────────────────────────────────────

class TestDispatchTool:
    """The single tool implementation.

    The OpenAI and Anthropic backends previously each carried their own copy of
    this dispatch — 38 identical lines — so a change to tool behaviour had to be
    made twice or the two would diverge.
    """

    def test_select_release_returns_selection(self):
        payload, selection = dt_find._dispatch_tool(
            "select_release", {"release_id": 12345, "reasoning": "matches"})
        assert payload == {"status": "ok"}
        assert selection == (12345, "matches")

    def test_select_release_coerces_string_id(self):
        _, selection = dt_find._dispatch_tool(
            "select_release", {"release_id": "12345"})
        assert selection == (12345, "")

    def test_select_release_rejects_malformed_id(self):
        """A bad id must be reported to the model, not raise mid-conversation."""
        payload, selection = dt_find._dispatch_tool(
            "select_release", {"release_id": None})
        assert selection is None
        assert "Invalid release_id" in payload["error"]

    def test_select_release_rejects_non_numeric_id(self):
        payload, selection = dt_find._dispatch_tool(
            "select_release", {"release_id": "not-a-number"})
        assert selection is None
        assert "error" in payload

    def test_unknown_tool_reports_error(self):
        payload, selection = dt_find._dispatch_tool("nope", {})
        assert selection is None
        assert payload["error"] == "Unknown tool: nope"

    def test_search_returns_results_and_no_selection(self, capsys):
        fake = {"results": [{"id": 1}, {"id": 2}], "count": 7}
        with patch.object(dt_find, "_tool_search_discogs", return_value=fake):
            payload, selection = dt_find._dispatch_tool(
                "search_discogs", {"query": "blue miles davis"})
        assert payload is fake
        assert selection is None
        out = capsys.readouterr().out
        assert "Searching" in out
        assert "2 result(s)" in out and "7 total" in out

    def test_search_arguments_forwarded(self):
        with patch.object(dt_find, "_tool_search_discogs",
                          return_value={"results": [], "count": 0}) as mock:
            dt_find._dispatch_tool("search_discogs",
                                   {"artist": "Aphex", "year": "1994"})
        mock.assert_called_once_with(artist="Aphex", year="1994")
