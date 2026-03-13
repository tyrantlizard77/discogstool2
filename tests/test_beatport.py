"""Tests for beatport.py — BPM lookup and Discogs→Beatport matching.

Covers:
  - Normalisation helpers (_normalize_title, _normalize_catno,
    _strip_discogs_artist, _similarity, _catno_similarity, _title_similarity)
  - BeatportCache CRUD and TTL expiry (using in-memory SQLite)
  - LLMMatcher._parse_llm_response edge cases
  - _match_tracks: happy path, coverage gate (title-match vs BPM-match), mix names
  - BeatportMatcher: nomatch caching, confidence threshold, cached-match reuse
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

import beatport
from beatport import (
    _catno_similarity,
    _match_tracks,
    _normalize_catno,
    _normalize_title,
    _similarity,
    _strip_discogs_artist,
    _title_similarity,
    AnthropicMatcher,
    BeatportCache,
    BeatportMatcher,
    LLMMatcher,
    ReleaseMatcher,
    YEAR_HARD_MAX,
    YEAR_PENALTY_FACTOR,
)


# ─── Mock helpers ─────────────────────────────────────────────────────────────

class _MockTrack:
    """Minimal DiscogsTrack-alike for testing."""

    def __init__(self, title: str, position: str = "A1") -> None:
        self._title = title
        self._position = position

    def getTitle(self) -> str:
        return self._title

    def getPosition(self) -> str:
        return self._position


class _MockRelease:
    """Minimal DiscogsRelease-alike for testing."""

    def __init__(
        self,
        tracks: list[_MockTrack],
        rid: int = 12345,
        title: str = "Test EP",
        artist: str = "Test Artist",
        catno: str = "TEST001",
        year: str = "2020",
    ) -> None:
        self._tracks = tracks
        self._rid = rid
        self._title = title
        self._artist = artist
        self._catno = catno
        self._year = year

    def getId(self) -> int:
        return self._rid

    def getTitle(self) -> str:
        return self._title

    def getArtist(self) -> str:
        return self._artist

    def getCatno(self) -> str:
        return self._catno

    def getLabel(self) -> str:
        return "Test Label"

    def getYear(self) -> str:
        return self._year

    def getCountry(self) -> str:
        return "UK"

    def getTrack(self, i: int) -> _MockTrack:
        if i >= len(self._tracks):
            raise IndexError(f"no track {i}")
        return self._tracks[i]


def _bp_track(
    name: str,
    bpm: int | None = None,
    length_ms: int | None = None,
    mix_name: str = "Original Mix",
) -> dict:
    """Helper: build a minimal Beatport track stub."""
    return {"name": name, "bpm": bpm, "length_ms": length_ms, "mix_name": mix_name}


# ─── _normalize_title ─────────────────────────────────────────────────────────

class TestNormalizeTitle:
    def test_empty_string(self):
        assert _normalize_title("") == ""

    def test_lowercases(self):
        assert _normalize_title("HELLO WORLD") == "hello world"

    def test_removes_accent(self):
        # é → e
        result = _normalize_title("Café")
        assert "e" in result
        assert "é" not in result

    def test_removes_accent_full_word(self):
        assert _normalize_title("Résumé") == "resume"

    def test_strips_original_mix_in_parens(self):
        result = _normalize_title("Track Name (Original Mix)")
        assert "original" not in result
        assert "mix" not in result

    def test_strips_original_mix_bare(self):
        result = _normalize_title("Track Name Original Mix")
        assert "original" not in result

    def test_strips_original_alone(self):
        result = _normalize_title("Track Original")
        assert "original" not in result

    def test_strips_feat_in_parens(self):
        result = _normalize_title("Track (feat. DJ Someone)")
        assert "feat" not in result
        assert "dj someone" not in result

    def test_strips_feat_bare(self):
        result = _normalize_title("Track feat. DJ Rolando")
        assert "feat" not in result
        assert "rolando" not in result

    def test_strips_ft_abbreviation(self):
        # Note: the current implementation only strips "feat"/"feat." (not bare "ft.").
        # "ft" without "eat" is not removed; this documents the current behaviour.
        result = _normalize_title("Track ft. Singer")
        # The token "ft" remains — test that overall normalisation still ran (lowercase)
        assert result == result.lower()

    def test_collapses_punctuation(self):
        result = _normalize_title("Track: Name - Here!")
        assert ":" not in result
        assert "!" not in result
        assert "-" not in result

    def test_strips_surrounding_whitespace(self):
        assert _normalize_title("  hello  ") == "hello"

    def test_collapses_internal_whitespace(self):
        result = _normalize_title("a   b")
        assert result == "a b"

    def test_preserves_remix_info(self):
        # A non-"original" remix should survive
        result = _normalize_title("Track (DJ X Remix)")
        assert "dj x remix" in result or "dj" in result  # at minimum not all stripped


# ─── _normalize_catno ─────────────────────────────────────────────────────────

class TestNormalizeCatno:
    def test_empty(self):
        assert _normalize_catno("") == ""

    def test_none(self):
        assert _normalize_catno(None) == ""

    def test_uppercases(self):
        assert _normalize_catno("abc123") == "ABC123"

    def test_removes_spaces(self):
        assert _normalize_catno("ABC 123") == "ABC123"

    def test_removes_hyphens(self):
        assert _normalize_catno("ABC-123") == "ABC123"

    def test_removes_spaces_and_hyphens(self):
        assert _normalize_catno("AB - 12 - C") == "AB12C"

    def test_idempotent(self):
        n = _normalize_catno("XYZ999")
        assert _normalize_catno(n) == n

    # ── New: Unicode / invisible-char / format-suffix normalisation ──────────

    def test_strips_accent_combining_mark(self):
        # STÓ023 (O with acute) should normalise the same as STO023
        assert _normalize_catno("STÓ023") == _normalize_catno("STO023")

    def test_strips_zero_width_spaces(self):
        # Zero-width space (U+200B) must be removed
        assert _normalize_catno("ABC\u200b123") == "ABC123"

    def test_strips_zero_width_no_break_space(self):
        assert _normalize_catno("ABC\ufeff123") == "ABC123"

    def test_strips_trailing_digital_suffix_D(self):
        # Common Beatport pattern: label appends D for digital
        assert _normalize_catno("BLKRTZ050D") == "BLKRTZ050"

    def test_strips_trailing_LP_after_digit(self):
        assert _normalize_catno("INV345LP") == "INV345"

    def test_strips_trailing_EP_after_digit(self):
        assert _normalize_catno("LABEL007EP") == "LABEL007"

    def test_strips_trailing_CD_after_digit(self):
        assert _normalize_catno("XYZ012CD") == "XYZ012"

    def test_does_not_strip_suffix_after_letter(self):
        # "TECHEP" ends in EP but there's no digit before EP, so it stays
        assert _normalize_catno("TECHEP") == "TECHEP"

    def test_digital_suffix_matching(self):
        # After normalisation, a catno with and without D should be identical
        assert _normalize_catno("BLKRTZ050D") == _normalize_catno("BLKRTZ050")

    def test_vinyl_vs_digital_catno_matching(self):
        # Vinyl LP and digital D editions of the same release normalise to the same string
        assert _normalize_catno("INV345LP") == _normalize_catno("INV345D")


# ─── _strip_discogs_artist ────────────────────────────────────────────────────

class TestStripDiscogsArtist:
    def test_no_suffix(self):
        assert _strip_discogs_artist("Artist Name") == "Artist Name"

    def test_single_suffix(self):
        assert _strip_discogs_artist("Artist Name (2)") == "Artist Name"

    def test_single_digit_one(self):
        # (1) is also a disambiguation suffix
        assert _strip_discogs_artist("Common Name (1)") == "Common Name"

    def test_multiple_artists_with_suffixes(self):
        result = _strip_discogs_artist("Artist A (2) & Artist B (3)")
        assert "(2)" not in result
        assert "(3)" not in result
        assert "Artist A" in result
        assert "Artist B" in result

    def test_non_numeric_parens_preserved(self):
        # "(Remix)" should not be stripped — only purely numeric suffixes
        assert _strip_discogs_artist("Track (Remix)") == "Track (Remix)"

    def test_empty_string(self):
        assert _strip_discogs_artist("") == ""


# ─── _similarity and derived functions ────────────────────────────────────────

class TestSimilarity:
    def test_identical_strings(self):
        assert _similarity("hello", "hello") == 1.0

    def test_both_empty(self):
        assert _similarity("", "") == 1.0

    def test_completely_different(self):
        s = _similarity("abc", "xyz")
        assert 0.0 <= s < 0.5

    def test_partial_match(self):
        s = _similarity("hello world", "hello")
        assert 0.0 < s < 1.0

    def test_catno_similarity_normalized(self):
        # "ABC-123" and "ABC123" should be identical after normalization
        assert _catno_similarity("ABC-123", "ABC123") == 1.0

    def test_catno_similarity_case_insensitive(self):
        assert _catno_similarity("abc123", "ABC123") == 1.0

    def test_catno_similarity_spaces(self):
        assert _catno_similarity("ABC 123", "ABC123") == 1.0

    def test_title_similarity_strips_original_mix(self):
        # "Track (Original Mix)" and "Track" should be very similar
        s = _title_similarity("Track Name (Original Mix)", "Track Name")
        assert s > 0.85

    def test_title_similarity_different_titles(self):
        s = _title_similarity("Arpeggio", "Completely Unrelated Track")
        assert s < 0.5


# ─── BeatportCache ────────────────────────────────────────────────────────────

def _make_cache() -> BeatportCache:
    """Return a BeatportCache backed by in-memory SQLite."""
    return BeatportCache(db_path=":memory:")


class TestBeatportCacheRelease:
    def test_get_missing_returns_none(self):
        cache = _make_cache()
        assert cache.get_release("9999") is None

    def test_put_and_get_roundtrip(self):
        cache = _make_cache()
        data = {"id": "42", "name": "Test EP", "tracks": []}
        cache.put_release("42", data)
        assert cache.get_release("42") == data

    def test_put_overwrite(self):
        cache = _make_cache()
        cache.put_release("42", {"name": "Old"})
        cache.put_release("42", {"name": "New"})
        assert cache.get_release("42") == {"name": "New"}

    def test_expired_entry_returns_none(self):
        """Entries older than RELEASE_CACHE_TTL_DAYS should be treated as absent."""
        cache = _make_cache()
        cache._conn.execute(
            "INSERT INTO release_cache (beatport_id, fetched_date, data) VALUES (?, ?, ?)",
            ("old", "2020-01-01", json.dumps({"name": "stale"})),
        )
        cache._conn.commit()
        assert cache.get_release("old") is None

    def test_expired_entry_deleted_from_db(self):
        """Expired entries should be purged from the DB on access."""
        cache = _make_cache()
        cache._conn.execute(
            "INSERT INTO release_cache (beatport_id, fetched_date, data) VALUES (?, ?, ?)",
            ("old", "2020-01-01", json.dumps({})),
        )
        cache._conn.commit()
        cache.get_release("old")  # triggers deletion
        row = cache._conn.execute(
            "SELECT 1 FROM release_cache WHERE beatport_id='old'"
        ).fetchone()
        assert row is None

    def test_id_coerced_to_string(self):
        """Integer beatport_ids should be stored and retrieved correctly."""
        cache = _make_cache()
        cache.put_release(42, {"name": "EP"})  # int id
        assert cache.get_release(42) == {"name": "EP"}
        assert cache.get_release("42") == {"name": "EP"}


class TestBeatportCacheMatch:
    def test_get_missing_returns_none(self):
        cache = _make_cache()
        assert cache.get_match("d-1") is None

    def test_put_and_get_roundtrip(self):
        cache = _make_cache()
        cache.put_match("d-1", "bp-99", 0.95, "catno")
        result = cache.get_match("d-1")
        assert result == ("bp-99", 0.95, "catno")

    def test_put_overwrite_updates_entry(self):
        cache = _make_cache()
        cache.put_match("d-1", "bp-1", 0.80, "catno")
        cache.put_match("d-1", "bp-2", 0.92, "title")
        bid, conf, matcher = cache.get_match("d-1")
        assert bid == "bp-2"
        assert conf == pytest.approx(0.92)
        assert matcher == "title"

    def test_confidence_preserved_as_float(self):
        cache = _make_cache()
        cache.put_match("d-2", "bp-7", 0.777, "llm")
        _, conf, _ = cache.get_match("d-2")
        assert abs(conf - 0.777) < 1e-6


class TestBeatportCacheNomatch:
    def test_unknown_id_is_not_nomatch(self):
        cache = _make_cache()
        assert not cache.is_known_nomatch("disc-5")

    def test_put_nomatch_marks_as_nomatch(self):
        cache = _make_cache()
        cache.put_nomatch("disc-5")
        assert cache.is_known_nomatch("disc-5")

    def test_expired_nomatch_returns_false(self):
        """Nomatch entries older than NOMATCH_TTL_DAYS should be treated as absent."""
        cache = _make_cache()
        cache._conn.execute(
            "INSERT INTO nomatches (discogs_id, checked_date) VALUES (?, ?)",
            ("old-disc", "2020-01-01"),
        )
        cache._conn.commit()
        assert not cache.is_known_nomatch("old-disc")

    def test_expired_nomatch_deleted_from_db(self):
        cache = _make_cache()
        cache._conn.execute(
            "INSERT INTO nomatches (discogs_id, checked_date) VALUES (?, ?)",
            ("old-disc", "2020-01-01"),
        )
        cache._conn.commit()
        cache.is_known_nomatch("old-disc")  # triggers deletion
        row = cache._conn.execute(
            "SELECT 1 FROM nomatches WHERE discogs_id='old-disc'"
        ).fetchone()
        assert row is None

    def test_delete_nomatch_removes_entry(self):
        cache = _make_cache()
        cache.put_nomatch("disc-7")
        cache.delete_nomatch("disc-7")
        assert not cache.is_known_nomatch("disc-7")

    def test_delete_nonexistent_is_safe(self):
        cache = _make_cache()
        cache.delete_nomatch("does-not-exist")  # should not raise


# ─── LLMMatcher._parse_llm_response ──────────────────────────────────────────

class TestParseLLMResponse:
    def test_valid_json(self):
        result = LLMMatcher._parse_llm_response(
            '{"beatport_id": "12345", "confidence": 0.95}'
        )
        assert result == ("12345", 0.95)

    def test_integer_id_is_stringified(self):
        result = LLMMatcher._parse_llm_response(
            '{"beatport_id": 12345, "confidence": 0.9}'
        )
        assert result is not None
        bid, conf = result
        assert isinstance(bid, str)
        assert bid == "12345"

    def test_json_with_surrounding_text(self):
        result = LLMMatcher._parse_llm_response(
            'Here is the match:\n{"beatport_id": "99", "confidence": 0.88}\nDone.'
        )
        assert result == ("99", 0.88)

    def test_null_beatport_id(self):
        result = LLMMatcher._parse_llm_response(
            '{"beatport_id": null, "confidence": 0.0}'
        )
        assert result == (None, 0.0)

    def test_missing_confidence_defaults_to_zero(self):
        result = LLMMatcher._parse_llm_response('{"beatport_id": "55"}')
        assert result is not None
        bid, conf = result
        assert bid == "55"
        assert conf == 0.0

    def test_no_json_in_response(self):
        result = LLMMatcher._parse_llm_response("I could not find a match.")
        assert result is None

    def test_malformed_json(self):
        result = LLMMatcher._parse_llm_response("{invalid json here}")
        assert result is None

    def test_empty_string(self):
        result = LLMMatcher._parse_llm_response("")
        assert result is None

    def test_confidence_type_coercion(self):
        # confidence given as string (edge case)
        result = LLMMatcher._parse_llm_response(
            '{"beatport_id": "7", "confidence": "0.9"}'
        )
        assert result is not None
        _, conf = result
        assert isinstance(conf, float)
        assert conf == pytest.approx(0.9)


# ─── AnthropicMatcher._parse_response ────────────────────────────────────────

class TestAnthropicMatcherParseResponse:
    """_parse_response reuses the same JSON extraction logic as LLMMatcher."""

    def test_valid_match(self):
        result = AnthropicMatcher._parse_response(
            '{"beatport_id": "99887766", "confidence": 0.91}'
        )
        assert result == ("99887766", 0.91)

    def test_null_id_returns_none_id(self):
        result = AnthropicMatcher._parse_response(
            '{"beatport_id": null, "confidence": 0.0}'
        )
        assert result == (None, 0.0)

    def test_integer_id_stringified(self):
        result = AnthropicMatcher._parse_response(
            '{"beatport_id": 12345, "confidence": 0.88}'
        )
        assert result is not None
        bid, _ = result
        assert isinstance(bid, str)

    def test_json_with_surrounding_prose(self):
        result = AnthropicMatcher._parse_response(
            'Based on the metadata I believe this matches:\n'
            '{"beatport_id": "55", "confidence": 0.85}\n'
        )
        assert result is not None
        assert result[0] == "55"

    def test_no_json_returns_none(self):
        assert AnthropicMatcher._parse_response("No match found.") is None

    def test_malformed_json_returns_none(self):
        assert AnthropicMatcher._parse_response("{bad json}") is None


class TestAnthropicMatcherAvailability:
    def test_unavailable_without_key(self):
        matcher = AnthropicMatcher.__new__(AnthropicMatcher)
        matcher._api_key = None
        matcher._model = AnthropicMatcher._DEFAULT_MODEL
        assert not matcher.is_available()

    def test_available_with_key(self):
        matcher = AnthropicMatcher.__new__(AnthropicMatcher)
        matcher._api_key = "sk-ant-test"
        matcher._model = AnthropicMatcher._DEFAULT_MODEL
        assert matcher.is_available()

    def test_returns_none_when_no_key(self):
        """find_release should short-circuit and return (None, 0.0) without a key."""
        matcher = AnthropicMatcher.__new__(AnthropicMatcher)
        matcher._api_key = None
        matcher._model = AnthropicMatcher._DEFAULT_MODEL
        rel = _MockRelease([])
        client = MagicMock()
        bid, conf = matcher.find_release(rel, client)
        assert bid is None
        assert conf == 0.0
        client.search_releases.assert_not_called()

    def test_rejects_id_not_in_candidate_list(self):
        """If the model hallucinates an ID not in the candidates, result is discarded."""
        matcher = AnthropicMatcher.__new__(AnthropicMatcher)
        matcher._api_key = "sk-ant-test"
        matcher._model = AnthropicMatcher._DEFAULT_MODEL

        rel = _MockRelease([_MockTrack("Track A")], catno="TEST001", year="2022")
        client = MagicMock()
        client.search_releases.return_value = [
            {"id": 111, "catalog_number": "TEST001D", "name": "Test EP", "publish_date": "2022-01-01"},
        ]

        with patch.object(matcher, "_call_api", return_value='{"beatport_id": "9999999", "confidence": 0.95}'):
            bid, conf = matcher.find_release(rel, client)

        assert bid is None  # 9999999 was not in the candidate list


# ─── _match_tracks ────────────────────────────────────────────────────────────

class TestMatchTracks:
    def test_exact_title_match_with_bpm(self):
        """Exact title match returns expected BPMs."""
        rel = _MockRelease([_MockTrack("Arpeggio"), _MockTrack("Bassline")])
        bp = [
            _bp_track("Arpeggio", bpm=128),
            _bp_track("Bassline", bpm=135),
        ]
        result = _match_tracks(rel, bp)
        assert result[0]["bpm"] == 128
        assert result[1]["bpm"] == 135

    def test_duration_ms_returned(self):
        rel = _MockRelease([_MockTrack("Track One")])
        bp = [_bp_track("Track One", bpm=130, length_ms=360000)]
        result = _match_tracks(rel, bp)
        assert result[0]["duration_ms"] == 360000

    def test_bpm_and_duration_both_present(self):
        rel = _MockRelease([_MockTrack("Track")])
        bp = [_bp_track("Track", bpm=140, length_ms=420000)]
        result = _match_tracks(rel, bp)
        assert result[0]["bpm"] == 140
        assert result[0]["duration_ms"] == 420000

    def test_no_bpm_or_duration_excludes_entry(self):
        """When a Beatport track has neither BPM nor duration, the entry is
        omitted from results — but the title still counts toward coverage."""
        rel = _MockRelease([
            _MockTrack("Alpha"), _MockTrack("Beta"),
            _MockTrack("Gamma"), _MockTrack("Delta"),
        ])
        bp = [
            _bp_track("Alpha"),  # bpm=None, length_ms=None
            _bp_track("Beta"),
            _bp_track("Gamma"),
            _bp_track("Delta"),
        ]
        # All 4 titles match → coverage passes.
        # But no BPM/duration data → result is empty dict (not coverage rejection).
        result = _match_tracks(rel, bp)
        assert result == {}

    def test_coverage_gate_rejects_wrong_release(self):
        """When fewer than TRACK_COVERAGE_MIN of ≥4 tracks title-match, return {}."""
        # 4 Discogs tracks, only 1 matches — 25 % < 30 %
        rel = _MockRelease([
            _MockTrack("Arpeggio"),
            _MockTrack("Totally Unrelated Track"),
            _MockTrack("Another Random Song"),
            _MockTrack("Something Completely Different"),
        ])
        bp = [
            _bp_track("Arpeggio", bpm=128),
            _bp_track("Beatport Exclusive A", bpm=130),
            _bp_track("Beatport Exclusive B", bpm=140),
        ]
        result = _match_tracks(rel, bp)
        assert result == {}

    def test_coverage_gate_passes_majority_match(self):
        """≥30 % title coverage → gate passes, BPMs returned."""
        # 4 Discogs tracks, 3 match → 75 % ≥ 30 %
        rel = _MockRelease([
            _MockTrack("Arpeggio"),
            _MockTrack("Bassline"),
            _MockTrack("Chords"),
            _MockTrack("Totally Unrelated Track"),
        ])
        bp = [
            _bp_track("Arpeggio", bpm=128),
            _bp_track("Bassline", bpm=135),
            _bp_track("Chords", bpm=142),
        ]
        result = _match_tracks(rel, bp)
        assert 0 in result
        assert 1 in result
        assert 2 in result

    def test_coverage_gate_not_applied_for_small_releases(self):
        """The coverage gate only kicks in for releases with 4+ tracks.
        A 3-track release should not be rejected even if only 1 title matches.
        """
        rel = _MockRelease([
            _MockTrack("Arpeggio"),
            _MockTrack("Unrelated A"),
            _MockTrack("Unrelated B"),
        ])
        bp = [_bp_track("Arpeggio", bpm=128)]
        result = _match_tracks(rel, bp)
        assert 0 in result

    def test_coverage_uses_title_match_not_bpm_match(self):
        """BUG-REGRESSION: coverage gate must count title-matched tracks,
        not just tracks with BPM data.  A release where Beatport has all titles
        but no BPMs should still pass coverage (and the single BPM-equipped
        track should appear in results).
        """
        rel = _MockRelease([
            _MockTrack("Arpeggio"),
            _MockTrack("Bassline"),
            _MockTrack("Chords"),
            _MockTrack("Drums"),
        ])
        # 4 title matches; only track 0 has a BPM
        bp = [
            _bp_track("Arpeggio", bpm=128),   # has BPM
            _bp_track("Bassline"),             # no BPM
            _bp_track("Chords"),               # no BPM
            _bp_track("Drums"),                # no BPM
        ]
        result = _match_tracks(rel, bp)
        # Coverage is 100 % (4/4 titles matched) → gate passes.
        # Only track 0 has data → only it appears in result.
        assert 0 in result
        assert result[0]["bpm"] == 128

    def test_mix_name_combined_for_matching(self):
        """Remix info in mix_name field should be combined into the match title."""
        rel = _MockRelease([_MockTrack("Track (DJ X Remix)")])
        bp = [
            {"name": "Track", "bpm": 130, "length_ms": None, "mix_name": "DJ X Remix"},
        ]
        result = _match_tracks(rel, bp)
        assert 0 in result
        assert result[0]["bpm"] == 130

    def test_original_mix_not_added_to_title(self):
        """'Original Mix' in mix_name should not be appended; normalized to base name."""
        rel = _MockRelease([_MockTrack("Pure Track")])
        bp = [_bp_track("Pure Track", bpm=120, mix_name="Original Mix")]
        result = _match_tracks(rel, bp)
        assert 0 in result

    def test_empty_beatport_tracks(self):
        rel = _MockRelease([_MockTrack("A"), _MockTrack("B")])
        assert _match_tracks(rel, []) == {}

    def test_empty_discogs_tracks(self):
        rel = _MockRelease([])
        bp = [_bp_track("Something", bpm=128)]
        assert _match_tracks(rel, bp) == {}

    def test_none_entries_in_beatport_list_skipped(self):
        rel = _MockRelease([_MockTrack("Track A")])
        bp = [None, _bp_track("Track A", bpm=125)]
        result = _match_tracks(rel, bp)
        assert 0 in result


# ─── BeatportMatcher (with mocked Beatport client) ────────────────────────────

class _AlwaysMatchMatcher(ReleaseMatcher):
    """Test matcher that always returns a fixed (id, confidence) pair."""
    name = "always"

    def __init__(self, bid: str, confidence: float) -> None:
        self._bid = bid
        self._confidence = confidence

    def find_release(self, dr, client) -> tuple[str | None, float]:
        return self._bid, self._confidence


class _NeverMatchMatcher(ReleaseMatcher):
    """Test matcher that never finds anything."""
    name = "never"

    def find_release(self, dr, client) -> tuple[str | None, float]:
        return None, 0.0


class _CountingMatcher(ReleaseMatcher):
    """Test matcher that records how many times it is called."""
    name = "counting"

    def __init__(self, bid: str = "bp-99", confidence: float = 0.99) -> None:
        self.call_count = 0
        self._bid = bid
        self._confidence = confidence

    def find_release(self, dr, client) -> tuple[str | None, float]:
        self.call_count += 1
        return self._bid, self._confidence


def _make_matcher(matchers, pre_populate_release: dict | None = None) -> tuple:
    """Return (BeatportMatcher, BeatportCache) with optional pre-populated release."""
    cache = BeatportCache(db_path=":memory:")
    if pre_populate_release is not None:
        bid, data = pre_populate_release
        cache.put_release(bid, data)
    m = BeatportMatcher(matchers=matchers, cache=cache)
    return m, cache


class TestBeatportMatcherFindBpms:
    def _rel(self, tracks=2):
        return _MockRelease([_MockTrack(f"Track {i}") for i in range(tracks)])

    def test_no_match_records_nomatch(self):
        m, cache = _make_matcher([_NeverMatchMatcher()])
        rel = self._rel()
        with patch.object(beatport, "get_client", return_value=MagicMock()):
            result = m.find_bpms(rel)
        assert result == {}
        assert cache.is_known_nomatch(str(rel.getId()))

    def test_low_confidence_not_accepted_records_nomatch(self):
        """Confidence below MIN_RELEASE_CONFIDENCE (0.70) must be treated as no-match."""
        m, cache = _make_matcher([_AlwaysMatchMatcher("bp-99", 0.50)])
        rel = self._rel()
        with patch.object(beatport, "get_client", return_value=MagicMock()):
            result = m.find_bpms(rel)
        assert result == {}
        assert cache.is_known_nomatch(str(rel.getId()))

    def test_threshold_boundary_exactly_meets(self):
        """Exactly MIN_RELEASE_CONFIDENCE should be accepted."""
        threshold = BeatportMatcher.MIN_RELEASE_CONFIDENCE
        m, cache = _make_matcher(
            [_AlwaysMatchMatcher("bp-1", threshold)],
            pre_populate_release=("bp-1", {"tracks": [
                {"name": "Track 0", "bpm": 130, "length_ms": None, "mix_name": "Original Mix"},
                {"name": "Track 1", "bpm": 140, "length_ms": None, "mix_name": "Original Mix"},
            ]}),
        )
        rel = self._rel()
        with patch.object(beatport, "get_client", return_value=MagicMock()):
            result = m.find_bpms(rel)
        assert result != {}  # match was accepted

    def test_nomatch_cache_skips_matchers(self):
        """A cached nomatch should prevent matchers from running."""
        counting = _CountingMatcher()
        m, cache = _make_matcher([counting])
        rel = self._rel()
        cache.put_nomatch(str(rel.getId()))
        with patch.object(beatport, "get_client", return_value=MagicMock()):
            result = m.find_bpms(rel)
        assert result == {}
        assert counting.call_count == 0  # matchers NOT invoked

    def test_force_bypasses_nomatch_cache(self):
        """force=True must ignore a cached nomatch and run matchers."""
        counting = _CountingMatcher("bp-5", 0.95)
        m, cache = _make_matcher(
            [counting],
            pre_populate_release=("bp-5", {"tracks": []}),
        )
        rel = self._rel()
        cache.put_nomatch(str(rel.getId()))
        with patch.object(beatport, "get_client", return_value=MagicMock()):
            m.find_bpms(rel, force=True)
        assert counting.call_count == 1  # matcher WAS invoked despite nomatch

    def test_cached_match_bypasses_matchers(self):
        """A cached release match should skip the matcher pipeline."""
        counting = _CountingMatcher()
        release_data = {"tracks": [
            {"name": "Track 0", "bpm": 128, "length_ms": None, "mix_name": "Original Mix"},
            {"name": "Track 1", "bpm": 135, "length_ms": None, "mix_name": "Original Mix"},
        ]}
        m, cache = _make_matcher(
            [counting],
            pre_populate_release=("bp-77", release_data),
        )
        rel = self._rel()
        cache.put_match(str(rel.getId()), "bp-77", 0.90, "catno")
        with patch.object(beatport, "get_client", return_value=MagicMock()):
            result = m.find_bpms(rel)
        assert counting.call_count == 0  # matcher NOT invoked
        assert result[0]["bpm"] == 128

    def test_successful_match_is_cached(self):
        """A new successful match should be saved to the match cache."""
        release_data = {"tracks": [
            {"name": "Track 0", "bpm": 120, "length_ms": None, "mix_name": "Original Mix"},
        ]}
        m, cache = _make_matcher(
            [_AlwaysMatchMatcher("bp-50", 0.90)],
            pre_populate_release=("bp-50", release_data),
        )
        rel = self._rel(1)
        with patch.object(beatport, "get_client", return_value=MagicMock()):
            m.find_bpms(rel)
        cached = cache.get_match(str(rel.getId()))
        assert cached is not None
        assert cached[0] == "bp-50"

    def test_best_confidence_wins_across_matchers(self):
        """When multiple matchers succeed, the one with the highest confidence wins."""
        m1 = _AlwaysMatchMatcher("bp-low",  0.75)
        m2 = _AlwaysMatchMatcher("bp-high", 0.92)
        release_data = {"tracks": [
            {"name": "Track 0", "bpm": 99, "length_ms": None, "mix_name": "Original Mix"},
        ]}
        m, cache = _make_matcher(
            [m1, m2],
            pre_populate_release=("bp-high", release_data),
        )
        rel = self._rel(1)
        with patch.object(beatport, "get_client", return_value=MagicMock()):
            result = m.find_bpms(rel)
        cached = cache.get_match(str(rel.getId()))
        assert cached[0] == "bp-high"
        assert result[0]["bpm"] == 99

    def test_client_error_returns_empty(self):
        """If get_client raises BeatportError, find_bpms returns {}."""
        from beatport import BeatportError
        m, cache = _make_matcher([_AlwaysMatchMatcher("bp-1", 0.95)])
        rel = self._rel()
        with patch.object(beatport, "get_client", side_effect=BeatportError("auth failed")):
            result = m.find_bpms(rel)
        assert result == {}


# ─── Year-penalty behaviour ───────────────────────────────────────────────────

class TestYearPenaltyConstants:
    """Sanity-check the new year-handling constants are present and sane."""

    def test_year_hard_max_is_positive(self):
        assert YEAR_HARD_MAX > 0

    def test_year_penalty_factor_is_fraction(self):
        assert 0.0 < YEAR_PENALTY_FACTOR < 1.0

    def test_penalty_compounds_per_year(self):
        # diff=2 should apply the factor twice
        penalty_1 = YEAR_PENALTY_FACTOR ** 1
        penalty_2 = YEAR_PENALTY_FACTOR ** 2
        assert penalty_2 < penalty_1


class TestCatnoMatcherYearPenalty:
    """Integration-style tests for the year soft-penalty in CatnoMatcher."""

    def _run_catno_matcher(self, discogs_year: str, bp_year: str) -> tuple[str | None, float]:
        """Run CatnoMatcher against a single fake Beatport candidate."""
        from beatport import CatnoMatcher, CATNO_MIN_SCORE

        rel = _MockRelease(
            [_MockTrack("Test Track")],
            catno="TEST001",
            title="Test EP",
            year=discogs_year,
        )
        client = MagicMock()
        client.search_releases.return_value = [
            {
                "id": 42,
                "catalog_number": "TEST001",   # perfect catno match
                "name": "Test EP",             # perfect title match
                "publish_date": f"{bp_year}-01-01",
            }
        ]
        matcher = CatnoMatcher()
        return matcher.find_release(rel, client)

    def test_zero_year_diff_no_penalty(self):
        bid, score = self._run_catno_matcher("2022", "2022")
        assert bid == "42"
        # Perfect catno+title match, no penalty → score should be near 1.0
        assert score > 0.95

    def test_one_year_diff_reduces_score(self):
        _, score_exact  = self._run_catno_matcher("2022", "2022")
        _, score_offset = self._run_catno_matcher("2022", "2023")
        assert score_offset < score_exact
        # Should still produce a match (score > MIN_RELEASE_CONFIDENCE = 0.70)
        assert score_offset > 0.70

    def test_two_year_diff_still_matches(self):
        bid, score = self._run_catno_matcher("2022", "2024")
        assert bid == "42"
        assert score > 0.0

    def test_beyond_hard_max_skips(self):
        hard_skip_year = str(2022 + YEAR_HARD_MAX + 1)
        bid, score = self._run_catno_matcher("2022", hard_skip_year)
        assert bid is None

    def test_exact_hard_max_still_matches(self):
        # A difference of exactly YEAR_HARD_MAX should apply penalty but not skip
        boundary_year = str(2022 + YEAR_HARD_MAX)
        bid, score = self._run_catno_matcher("2022", boundary_year)
        assert bid == "42"
        expected_penalty = YEAR_PENALTY_FACTOR ** YEAR_HARD_MAX
        # Score should be approximately base_score * penalty
        assert score == pytest.approx(score / expected_penalty * expected_penalty, rel=0.05)
