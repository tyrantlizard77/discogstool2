"""Tests for dt_bpm — per-track BPM diagnostic tool.

Covers:
  - _explain_decision: all decision branches (failed, out-of-range, low
    confidence, accepted)
  - _fmt_duration: None, zero, and non-zero durations
  - BeatportCache.get_verified_bpm_detail: missing entry, present entry
  - _run_essentia_for_track: no preview, cache hit, cache miss (fresh analysis),
    reverify bypasses cache
  - main(): no-Beatport-match path, successful run with mocked pipeline
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
from unittest.mock import MagicMock, call, patch

import pytest

# ── Stub third-party packages that aren't installed in the test environment ───
# Must happen before any project module is imported.

_es_mock = MagicMock()
sys.modules.setdefault("essentia", _es_mock)
sys.modules.setdefault("essentia.standard", _es_mock)

# client_interface.py imports discogs_client and PIL at module level
sys.modules.setdefault("discogs_client", MagicMock())
_pil_mock = MagicMock()
sys.modules.setdefault("PIL", _pil_mock)
sys.modules.setdefault("PIL.Image", _pil_mock)

# ── Load dt_bpm (no .py extension) ────────────────────────────────────────────

_DT_BPM_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dt_bpm"
)
_loader  = importlib.machinery.SourceFileLoader("dt_bpm", _DT_BPM_PATH)
_spec    = importlib.util.spec_from_loader("dt_bpm", _loader)
dt_bpm   = importlib.util.module_from_spec(_spec)
sys.modules["dt_bpm"] = dt_bpm
_loader.exec_module(dt_bpm)

# ── Re-use the in-memory cache helper from the beatport tests ─────────────────

from beatport import BeatportCache, BPM_VERIFY_MIN_CONFIDENCE


def _make_cache() -> BeatportCache:
    return BeatportCache(db_path=":memory:")


# ─── _explain_decision ────────────────────────────────────────────────────────

class TestExplainDecision:
    """All branches of the decision-explanation helper."""

    def test_analysis_failed_both_zero(self):
        result = dt_bpm._explain_decision(0.0, 0.0)
        assert "failed" in result

    def test_analysis_failed_none(self):
        result = dt_bpm._explain_decision(None, None)
        assert "failed" in result

    def test_detected_below_range(self):
        # Below BPM_VERIFY_MIN (40)
        result = dt_bpm._explain_decision(30.0, 4.0)
        assert "range" in result
        assert "rejected" in result

    def test_detected_above_range(self):
        # Above BPM_VERIFY_MAX (250)
        result = dt_bpm._explain_decision(300.0, 4.0)
        assert "range" in result
        assert "rejected" in result

    def test_low_confidence_rejected(self):
        low_conf = BPM_VERIFY_MIN_CONFIDENCE - 0.1
        result = dt_bpm._explain_decision(128.0, low_conf)
        assert "confidence" in result
        assert "rejected" in result

    def test_high_confidence_accepted(self):
        result = dt_bpm._explain_decision(128.0, BPM_VERIFY_MIN_CONFIDENCE + 1.0)
        assert "accepted" in result

    def test_at_confidence_threshold_accepted(self):
        result = dt_bpm._explain_decision(128.0, BPM_VERIFY_MIN_CONFIDENCE)
        assert "accepted" in result

    def test_just_below_confidence_threshold_rejected(self):
        result = dt_bpm._explain_decision(128.0, BPM_VERIFY_MIN_CONFIDENCE - 0.01)
        assert "rejected" in result

    def test_in_range_shows_confidence_value(self):
        result = dt_bpm._explain_decision(128.0, 4.5)
        assert "4.50" in result


# ─── _fmt_duration ────────────────────────────────────────────────────────────

class TestFmtDuration:
    def test_none(self):
        assert dt_bpm._fmt_duration(None) == "—"

    def test_zero(self):
        assert dt_bpm._fmt_duration(0) == "0:00"

    def test_one_minute_thirty(self):
        assert dt_bpm._fmt_duration(90_000) == "1:30"

    def test_six_minutes_twelve(self):
        assert dt_bpm._fmt_duration(372_000) == "6:12"

    def test_seconds_zero_padded(self):
        result = dt_bpm._fmt_duration(65_000)  # 1:05
        assert result == "1:05"

    def test_exact_minute(self):
        assert dt_bpm._fmt_duration(120_000) == "2:00"


# ─── BeatportCache.get_verified_bpm_detail ────────────────────────────────────

class TestGetVerifiedBpmDetail:
    def test_missing_returns_none(self):
        cache = _make_cache()
        assert cache.get_verified_bpm_detail("no-such-id") is None

    def test_present_returns_full_record(self):
        cache = _make_cache()
        cache.put_verified_bpm("bp-42", "http://example.com/preview.mp3", 128, 127.5, 4.1, 128)
        detail = cache.get_verified_bpm_detail("bp-42")
        assert detail is not None
        assert detail["declared_bpm"] == 128
        assert detail["detected_bpm"] == pytest.approx(127.5)
        assert detail["confidence"]   == pytest.approx(4.1)
        assert detail["chosen_bpm"]   == 128
        assert "verified_date" in detail

    def test_overridden_bpm_stored(self):
        cache = _make_cache()
        cache.put_verified_bpm("bp-7", "http://example.com/p.mp3", 130, 145.0, 5.0, 145)
        detail = cache.get_verified_bpm_detail("bp-7")
        assert detail["declared_bpm"] == 130
        assert detail["chosen_bpm"]   == 145

    def test_integer_and_string_ids_equivalent(self):
        cache = _make_cache()
        cache.put_verified_bpm("99", "http://x.com/p.mp3", 120, 120.0, 3.5, 120)
        assert cache.get_verified_bpm_detail(99) is not None   # int lookup
        assert cache.get_verified_bpm_detail("99") is not None  # str lookup

    def test_overwrite_updates_record(self):
        cache = _make_cache()
        cache.put_verified_bpm("bp-1", "http://x.com/p.mp3", 128, 127.0, 4.0, 128)
        cache.put_verified_bpm("bp-1", "http://x.com/p.mp3", 128, 140.0, 5.5, 140)
        detail = cache.get_verified_bpm_detail("bp-1")
        assert detail["detected_bpm"] == pytest.approx(140.0)
        assert detail["chosen_bpm"]   == 140


# ─── _run_essentia_for_track ──────────────────────────────────────────────────

def _entry(
    bpm: int | None = 128,
    sample_url: str | None = "http://example.com/preview.mp3",
    bp_track_id: str | None = "bp-track-1",
    duration_ms: int | None = 372_000,
) -> dict:
    return {
        "bpm":               bpm,
        "duration_ms":       duration_ms,
        "sample_url":        sample_url,
        "beatport_track_id": bp_track_id,
    }


class TestRunEssentiaForTrack:
    def test_no_sample_url_returns_no_preview(self):
        cache = _make_cache()
        result = dt_bpm._run_essentia_for_track(0, _entry(sample_url=None), cache, False)
        assert result["detected_bpm"] is None
        assert "no preview" in result["decision"]

    def test_no_track_id_returns_no_preview(self):
        cache = _make_cache()
        result = dt_bpm._run_essentia_for_track(0, _entry(bp_track_id=None), cache, False)
        assert result["detected_bpm"] is None

    def test_no_declared_bpm_still_analyzes(self):
        """bpm=None in the entry does not skip analysis — only missing sample_url/bp_track_id does."""
        cache = _make_cache()
        with patch("beatport._analyze_preview", return_value=(128.0, 4.5)):
            result = dt_bpm._run_essentia_for_track(0, _entry(bpm=None), cache, False)
        assert result["detected_bpm"] == pytest.approx(128.0)
        assert result["chosen_bpm"] == 128

    def test_cache_hit_returns_cached_data(self):
        cache = _make_cache()
        cache.put_verified_bpm("bp-track-1", "http://example.com/preview.mp3", 128, 127.5, 4.1, 128)
        result = dt_bpm._run_essentia_for_track(0, _entry(), cache, reverify=False)
        assert result["declared_bpm"] == 128
        assert result["detected_bpm"] == pytest.approx(127.5)
        assert result["confidence"]   == pytest.approx(4.1)
        assert result["chosen_bpm"]   == 128
        assert "(cached)" in result["decision"]

    def test_cache_hit_not_used_when_reverify(self):
        cache = _make_cache()
        cache.put_verified_bpm("bp-track-1", "http://example.com/preview.mp3", 128, 127.5, 4.1, 128)
        with patch("beatport._analyze_preview", return_value=(130.0, 5.0)) as mock_analyze:
            result = dt_bpm._run_essentia_for_track(0, _entry(), cache, reverify=True)
        mock_analyze.assert_called_once()
        assert "(cached)" not in result["decision"]

    def test_cache_miss_calls_analyze_and_caches(self):
        cache = _make_cache()
        with patch("beatport._analyze_preview", return_value=(127.5, 4.1)):
            result = dt_bpm._run_essentia_for_track(0, _entry(), cache, reverify=False)
        assert result["detected_bpm"] == pytest.approx(127.5)
        assert result["confidence"]   == pytest.approx(4.1)
        # Result should now be in cache
        detail = cache.get_verified_bpm_detail("bp-track-1")
        assert detail is not None
        assert detail["detected_bpm"] == pytest.approx(127.5)

    def test_high_confidence_returns_detected_bpm(self):
        """High-confidence detection → chosen_bpm is the rounded detected value."""
        cache = _make_cache()
        with patch("beatport._analyze_preview", return_value=(145.0, 5.5)):
            result = dt_bpm._run_essentia_for_track(0, _entry(bpm=130), cache, reverify=False)
        assert result["chosen_bpm"] == 145
        assert "accepted" in result["decision"]

    def test_low_confidence_returns_none_bpm(self):
        """Low-confidence detection → chosen_bpm is None regardless of declared BPM."""
        cache = _make_cache()
        with patch("beatport._analyze_preview", return_value=(128.5, 1.0)):
            result = dt_bpm._run_essentia_for_track(0, _entry(bpm=128), cache, reverify=False)
        assert result["chosen_bpm"] is None
        assert "rejected" in result["decision"]


# ─── main() ───────────────────────────────────────────────────────────────────

class _MockTrack:
    def __init__(self, title: str, position: str = "A1") -> None:
        self._title    = title
        self._position = position

    def getTitle(self)    -> str: return self._title
    def getPosition(self) -> str: return self._position


class _MockRelease:
    def __init__(self, tracks: list[_MockTrack], rid: int = 99999) -> None:
        self._tracks = tracks
        self._rid    = rid

    def getId(self)      -> int: return self._rid
    def getArtist(self)  -> str: return "Test Artist"
    def getTitle(self)   -> str: return "Test EP"
    def getLabel(self)   -> str: return "Test Label"
    def getCatno(self)   -> str: return "TEST001"
    def getYear(self)    -> str: return "2021"

    def getTrack(self, i: int) -> _MockTrack:
        if i >= len(self._tracks):
            raise IndexError
        return self._tracks[i]


class TestMain:
    """End-to-end tests for main() with all external dependencies mocked."""

    def _run(self, argv: list[str], patches: dict) -> tuple[int, str]:
        """Run main() with given sys.argv and patches, return (exit_code, stdout)."""
        import io
        stdout_capture = io.StringIO()
        exit_code = 0

        ctx_managers = [patch(k, v) for k, v in patches.items()]

        with patch("sys.argv", ["dt_bpm"] + argv):
            with patch("sys.stdout", stdout_capture):
                try:
                    for cm in ctx_managers:
                        cm.__enter__()
                    dt_bpm.main()
                except SystemExit as e:
                    exit_code = e.code or 0
                finally:
                    for cm in reversed(ctx_managers):
                        try:
                            cm.__exit__(None, None, None)
                        except Exception:
                            pass

        return exit_code, stdout_capture.getvalue()

    def test_no_beatport_match_prints_message(self, capsys):
        mock_release = _MockRelease([_MockTrack("Track One")])
        cache = _make_cache()  # no matches stored

        with (
            patch("dt_bpm.DiscogsRelease", return_value=mock_release),
            patch("dt_bpm.bp.BeatportCache", return_value=cache),
            patch("dt_bpm.bp.BeatportMatcher") as mock_matcher_cls,
        ):
            mock_matcher_cls.return_value.find_bpms.return_value = {}
            with patch("sys.argv", ["dt_bpm", "99999"]):
                with pytest.raises(SystemExit) as exc_info:
                    dt_bpm.main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "No Beatport match" in captured.out

    def test_successful_run_prints_table(self, capsys):
        mock_release = _MockRelease([
            _MockTrack("Track One", "A1"),
            _MockTrack("Track Two", "A2"),
        ])
        cache = _make_cache()
        cache.put_match("99999", "bp-release-1", 0.91, "CatnoMatcher")

        bp_tracks = [
            {"id": 1, "name": "Track One", "bpm": 128, "length_ms": 372_000,
             "sample_url": "http://example.com/1.mp3", "mix_name": "Original Mix"},
            {"id": 2, "name": "Track Two", "bpm": 132, "length_ms": 400_000,
             "sample_url": "http://example.com/2.mp3", "mix_name": "Original Mix"},
        ]
        raw_track_data = {
            0: {"bpm": 128, "duration_ms": 372_000,
                "sample_url": "http://example.com/1.mp3", "beatport_track_id": "1"},
            1: {"bpm": 132, "duration_ms": 400_000,
                "sample_url": "http://example.com/2.mp3", "beatport_track_id": "2"},
        }

        with (
            patch("dt_bpm.DiscogsRelease", return_value=mock_release),
            patch("dt_bpm.bp.BeatportCache", return_value=cache),
            patch("dt_bpm.bp.BeatportMatcher") as mock_matcher_cls,
            patch("dt_bpm.bp.get_client", return_value=MagicMock()),
            patch("dt_bpm.bp._fetch_full_release", return_value={"tracks": bp_tracks}),
            patch("dt_bpm.bp._match_tracks", return_value=raw_track_data),
            patch("beatport._analyze_preview", return_value=(127.5, 4.1)),
        ):
            mock_matcher_cls.return_value.find_bpms.return_value = {}
            with patch("sys.argv", ["dt_bpm", "99999"]):
                dt_bpm.main()

        captured = capsys.readouterr()
        assert "Track One" in captured.out
        assert "Track Two" in captured.out
        assert "128" in captured.out
        assert "132" in captured.out
        assert "Beatport match" in captured.out
        # Summary line
        assert "Beatport matches: 2" in captured.out

    def test_reverify_flag_passed_through(self, capsys):
        """--reverify should cause _analyze_preview to be called even with a cached entry."""
        mock_release = _MockRelease([_MockTrack("Track One", "A1")])
        cache = _make_cache()
        cache.put_match("99999", "bp-release-1", 0.91, "CatnoMatcher")
        # Pre-populate cache — should be ignored with --reverify
        cache.put_verified_bpm("1", "http://example.com/1.mp3", 128, 127.0, 4.0, 128)

        raw_track_data = {
            0: {"bpm": 128, "duration_ms": 372_000,
                "sample_url": "http://example.com/1.mp3", "beatport_track_id": "1"},
        }

        bp_tracks = [
            {"id": 1, "name": "Track One", "bpm": 128, "length_ms": 372_000,
             "sample_url": "http://example.com/1.mp3", "mix_name": "Original Mix"},
        ]

        with (
            patch("dt_bpm.DiscogsRelease", return_value=mock_release),
            patch("dt_bpm.bp.BeatportCache", return_value=cache),
            patch("dt_bpm.bp.BeatportMatcher") as mock_matcher_cls,
            patch("dt_bpm.bp.get_client", return_value=MagicMock()),
            patch("dt_bpm.bp._fetch_full_release", return_value={"tracks": bp_tracks}),
            patch("dt_bpm.bp._match_tracks", return_value=raw_track_data),
            patch("beatport._analyze_preview", return_value=(128.0, 4.5)) as mock_analyze,
        ):
            mock_matcher_cls.return_value.find_bpms.return_value = {}
            with patch("sys.argv", ["dt_bpm", "99999", "--reverify"]):
                dt_bpm.main()

        mock_analyze.assert_called_once()
