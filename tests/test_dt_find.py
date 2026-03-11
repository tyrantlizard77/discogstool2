"""Tests for dt_find — voice/text vinyl record finder.

Covers:
  - _strip_think / _extract_think: <think> tag removal (Qwen3 reasoning chains)
  - _fmt_search_args: search parameter summary formatting
  - load_config / save_config: file I/O roundtrip
  - SYSTEM_PROMPT: sanity checks (contains key guidance phrases)
  - _tool_search_discogs: result formatting with a mocked Discogs client
  - run_agent: early-exit on select_release with a mocked OpenAI client
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
    _extract_think,
    _fmt_search_args,
    _strip_think,
    load_config,
    save_config,
)


# ─── _strip_think ─────────────────────────────────────────────────────────────

class TestStripThink:
    def test_no_tags_unchanged(self):
        assert _strip_think("hello world") == "hello world"

    def test_strips_inline_block(self):
        result = _strip_think("<think>internal</think>answer")
        assert result == "answer"
        assert "<think>" not in result

    def test_strips_multiline_block(self):
        text = "<think>\nline1\nline2\n</think>\nResponse text"
        result = _strip_think(text)
        assert "line1" not in result
        assert "line2" not in result
        assert "Response text" in result

    def test_strips_multiple_blocks(self):
        text = "<think>first</think>middle<think>second</think>end"
        result = _strip_think(text)
        assert "first" not in result
        assert "second" not in result
        assert "middle" in result
        assert "end" in result

    def test_empty_think_block(self):
        result = _strip_think("<think></think>answer")
        assert result == "answer"

    def test_empty_string(self):
        assert _strip_think("") == ""

    def test_no_think_content_preserved(self):
        text = "Just a normal LLM response."
        assert _strip_think(text) == text

    def test_strips_leading_and_trailing_whitespace_after_removal(self):
        # After stripping the block, the result should be stripped of extra space
        result = _strip_think("<think>reason</think>  answer  ")
        assert result == "answer"


# ─── _extract_think ───────────────────────────────────────────────────────────

class TestExtractThink:
    def test_no_tags_returns_none(self):
        assert _extract_think("hello world") is None

    def test_extracts_content(self):
        result = _extract_think("<think>internal reasoning</think>other")
        assert result == "internal reasoning"

    def test_extracts_multiline_content(self):
        result = _extract_think("<think>line1\nline2</think>")
        assert result == "line1\nline2"

    def test_returns_first_match_only(self):
        result = _extract_think("<think>first</think>mid<think>second</think>")
        assert result == "first"

    def test_strips_whitespace_from_content(self):
        result = _extract_think("<think>  padded  </think>")
        assert result == "padded"

    def test_empty_think_block_returns_empty_string(self):
        result = _extract_think("<think></think>")
        assert result == ""


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
    def test_missing_file_returns_empty_dict(self, tmp_path):
        config_path = str(tmp_path / "find_config")
        with patch.object(dt_find.util, "userfile", return_value=config_path):
            result = load_config()
        assert result == {}

    def test_roundtrip(self, tmp_path):
        config_path = str(tmp_path / "find_config")
        original = {
            "llm_url": "http://test.example.com/v1",
            "llm_model": "test-model-7b",
        }
        with patch.object(dt_find.util, "userfile", return_value=config_path):
            save_config(original)
            result = load_config()
        assert result["llm_url"] == "http://test.example.com/v1"
        assert result["llm_model"] == "test-model-7b"

    def test_comment_lines_ignored(self, tmp_path):
        config_path = str(tmp_path / "find_config")
        with open(config_path, "w") as f:
            f.write("# This is a comment\n")
            f.write("llm_url=http://example.com\n")
        with patch.object(dt_find.util, "userfile", return_value=config_path):
            result = load_config()
        assert "llm_url" in result
        assert "#" not in result.get("llm_url", "")

    def test_value_with_equals_sign(self, tmp_path):
        """Values containing '=' should be preserved correctly (split on first '=' only)."""
        config_path = str(tmp_path / "find_config")
        with open(config_path, "w") as f:
            f.write("llm_url=http://host:8000/v1?key=val\n")
        with patch.object(dt_find.util, "userfile", return_value=config_path):
            result = load_config()
        assert result["llm_url"] == "http://host:8000/v1?key=val"

    def test_saved_file_is_human_readable(self, tmp_path):
        config_path = str(tmp_path / "find_config")
        with patch.object(dt_find.util, "userfile", return_value=config_path):
            save_config({"llm_url": "http://x.local", "llm_model": "model"})
        with open(config_path) as f:
            content = f.read()
        assert "llm_url=http://x.local" in content
        assert "llm_model=model" in content

    def test_empty_lines_skipped(self, tmp_path):
        config_path = str(tmp_path / "find_config")
        with open(config_path, "w") as f:
            f.write("\n\nllm_url=http://example.com\n\n")
        with patch.object(dt_find.util, "userfile", return_value=config_path):
            result = load_config()
        assert result == {"llm_url": "http://example.com"}


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


# ─── run_agent (mocked OpenAI client) ────────────────────────────────────────

def _make_tool_call(tc_id: str, name: str, arguments: dict):
    """Build a mock tool call object matching the openai SDK shape."""
    tc = MagicMock()
    tc.id = tc_id
    tc.function.name = name
    tc.function.arguments = json.dumps(arguments)
    return tc


def _make_response(content: str = "", tool_calls=None):
    """Build a mock openai ChatCompletion response."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or []
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


class TestRunAgent:
    def _patch_openai(self, responses: list):
        """Context manager: patch openai.OpenAI so completions return *responses* in order."""
        client_mock = MagicMock()
        client_mock.chat.completions.create.side_effect = responses

        openai_mod = MagicMock()
        openai_mod.OpenAI.return_value = client_mock
        return patch.dict("sys.modules", {"openai": openai_mod})

    def test_select_release_returns_id(self):
        """Agent should return (release_id, reasoning) when select_release is called."""
        tc = _make_tool_call("tc1", "select_release",
                             {"release_id": 123456, "reasoning": "Exact match"})
        responses = [_make_response(tool_calls=[tc])]

        with self._patch_openai(responses):
            with patch.object(dt_find, "_tool_search_discogs", return_value={"results": []}):
                release_id, reasoning = dt_find.run_agent(
                    "Miles Davis Kind of Blue",
                    llm_url="http://localhost:8000/v1",
                    llm_model="test-model",
                )
        assert release_id == 123456
        assert reasoning == "Exact match"

    def test_search_then_select(self):
        """Agent calls search_discogs first, then select_release on the next turn."""
        search_tc = _make_tool_call(
            "tc-search", "search_discogs", {"q": "Miles Davis Kind of Blue"}
        )
        select_tc = _make_tool_call(
            "tc-select", "select_release",
            {"release_id": 999, "reasoning": "Found it"},
        )
        responses = [
            _make_response(tool_calls=[search_tc]),
            _make_response(tool_calls=[select_tc]),
        ]
        search_result = {"count": 1, "results": [{"id": 999, "artist": "Miles Davis",
                                                   "title": "Kind of Blue"}]}

        with self._patch_openai(responses):
            with patch.object(dt_find, "_tool_search_discogs", return_value=search_result):
                release_id, reasoning = dt_find.run_agent(
                    "the blue Miles Davis one",
                    llm_url="http://localhost:8000/v1",
                    llm_model="test-model",
                )
        assert release_id == 999

    def test_no_tool_calls_returns_none(self):
        """If the model responds with plain text (no tool calls), return (None, '')."""
        responses = [_make_response(content="I couldn't find that record.", tool_calls=[])]
        with self._patch_openai(responses):
            release_id, reasoning = dt_find.run_agent(
                "something completely obscure",
                llm_url="http://localhost:8000/v1",
                llm_model="test-model",
            )
        assert release_id is None

    def test_think_tags_stripped_from_content(self):
        """<think>…</think> blocks in the response should not be shown to the user."""
        responses = [_make_response(
            content="<think>I need to search first</think>",
            tool_calls=[],
        )]
        with self._patch_openai(responses):
            # Should not raise; thinking block stripped silently
            release_id, _ = dt_find.run_agent(
                "test",
                llm_url="http://localhost:8000/v1",
                llm_model="test-model",
            )
        assert release_id is None

    def test_unknown_tool_gets_error_response(self):
        """An unknown tool name should receive an error message, not crash."""
        bad_tc = _make_tool_call("tc-bad", "delete_everything", {})
        text_response = _make_response(content="Sorry, I can't do that.", tool_calls=[])
        responses = [
            _make_response(tool_calls=[bad_tc]),
            text_response,
        ]
        with self._patch_openai(responses):
            release_id, _ = dt_find.run_agent(
                "test",
                llm_url="http://localhost:8000/v1",
                llm_model="test-model",
            )
        assert release_id is None

    def test_llm_exception_returns_none(self):
        """If the OpenAI client raises, run_agent should return (None, '') gracefully."""
        client_mock = MagicMock()
        client_mock.chat.completions.create.side_effect = Exception("Connection refused")
        openai_mod = MagicMock()
        openai_mod.OpenAI.return_value = client_mock

        with patch.dict("sys.modules", {"openai": openai_mod}):
            release_id, _ = dt_find.run_agent(
                "test",
                llm_url="http://localhost:8000/v1",
                llm_model="test-model",
            )
        assert release_id is None
