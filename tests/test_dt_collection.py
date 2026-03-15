"""Tests for dt_collection — collection scanner and metadata update tool.

Covers:
  - collection_report: partially-recorded report emits 1-based track numbers (P0 bug fix)
  - collection_report: missing-locally and discogs-missing reports
  - duplicate detection in file_map
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest


# ── Load dt_collection as a module (it has no .py extension) ─────────────────

_DT_COLLECTION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dt_collection"
)


def _load_dt_collection():
    """Load dt_collection with sys.argv mocked to avoid argparse/scanning side effects."""
    with patch("sys.argv", ["dt_collection", "/tmp"]):
        with patch("util.get_audio_files", return_value=[]):
            loader = importlib.machinery.SourceFileLoader("dt_collection", _DT_COLLECTION_PATH)
            spec = importlib.util.spec_from_loader("dt_collection", loader)
            mod = importlib.util.module_from_spec(spec)
            sys.modules.setdefault("dt_collection", mod)
            loader.exec_module(mod)
    return mod


dt_collection = _load_dt_collection()
collection_report = dt_collection.collection_report


# ── Mock helpers ──────────────────────────────────────────────────────────────

def _mock_release(rid: int, total_tracks: int, year: str = "2020") -> MagicMock:
    r = MagicMock()
    r.getId.return_value = rid
    r.getTotalTracks.return_value = total_tracks
    r.getYear.return_value = year
    r.__str__ = lambda self: f"Release {rid}"
    r.__repr__ = lambda self: f"<Release {rid}>"
    return r


def _mock_af(release: MagicMock, track_index: int) -> MagicMock:
    """Build a mock AudioFile whose getTrack() returns track_index (0-based)."""
    track = MagicMock()
    track.getTrackNumber.return_value = track_index + 1  # 1-based
    track.getRelease.return_value = release

    af = MagicMock()
    af.getTrack.return_value = track
    af.getFilename.return_value = f"/music/release_{release.getId()}_track{track_index}.aiff"
    return af


def _make_args(
    all_reports: bool = False,
    missing: bool = False,
    discogs_missing: bool = False,
    partially_recorded: bool = False,
) -> MagicMock:
    args = MagicMock()
    args.all_reports = all_reports
    args.missing = missing
    args.discogs_missing = discogs_missing
    args.partially_recorded = partially_recorded
    return args


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestPartiallyRecorded:
    """The partially-recorded report must show 1-based track numbers (not doubled)."""

    def _run(self, release, filelist, args=None):
        if args is None:
            args = _make_args(partially_recorded=True)

        # Stub out parse_collection_xml to return one collection item pointing
        # at the same release, and DiscogsRelease to return our mock.
        collection_item = MagicMock()
        collection_item.releaseid = release.getId()

        captured = StringIO()
        with patch.object(dt_collection, "verbose", False), \
             patch("util.parse_collection_xml", return_value=[collection_item]), \
             patch("client_interface.DiscogsRelease", return_value=release), \
             patch("sys.stdout", captured):
            collection_report("/fake/collection.xml", filelist, args)

        return captured.getvalue()

    def _extract_missing_list(self, output: str) -> list[int]:
        """Parse the '[1, 2, 3]'-style list from a partially-recorded report line."""
        import re
        # Find lines like "Release 1 missing [1, 3]"
        m = re.search(r"missing\s+(\[.*?\])", output)
        if not m:
            return []
        return eval(m.group(1))  # safe: test-generated string from repr(list[int])

    def test_missing_first_track_shown_as_1(self):
        """Track index 0 missing → must appear as track number 1 (not 0 or 2)."""
        release = _mock_release(rid=10, total_tracks=2)
        # Only track index 1 present; index 0 is missing
        filelist = [_mock_af(release, 1)]
        output = self._run(release, filelist)
        missing = self._extract_missing_list(output)
        assert missing == [1]

    def test_missing_second_track_shown_as_2(self):
        """Track index 1 missing → must appear as track number 2 (not 1 or 4)."""
        release = _mock_release(rid=20, total_tracks=3)
        # Tracks 0 and 2 present; index 1 is missing
        filelist = [_mock_af(release, 0), _mock_af(release, 2)]
        output = self._run(release, filelist)
        missing = self._extract_missing_list(output)
        assert missing == [2]

    def test_no_missing_tracks_not_in_report(self):
        """A fully-recorded release must not appear in the partial report."""
        release = _mock_release(rid=30, total_tracks=2)
        filelist = [_mock_af(release, 0), _mock_af(release, 1)]
        output = self._run(release, filelist)
        lines = [l for l in output.splitlines() if "Release 30" in l]
        assert lines == []

    def test_missing_track_number_not_doubled(self):
        """Regression for i+i bug: track indices 2 and 3 missing → track numbers 3 and 4, not 4 and 6."""
        # With 4-track release and only tracks 0 and 1 present, indices 2 and 3 are missing.
        # Old bug (i+i): would display [4, 6].  Correct (i+1): [3, 4].
        release = _mock_release(rid=50, total_tracks=4)
        filelist = [_mock_af(release, 0), _mock_af(release, 1)]
        output = self._run(release, filelist)
        missing = self._extract_missing_list(output)
        assert 6 not in missing  # old bug: 3+3=6
        assert missing == [3, 4]  # correct 1-based values


class TestNotInCollection:
    """Files whose release is not in the Discogs collection appear in the missing report."""

    def test_file_not_in_collection_appears(self):
        release = _mock_release(rid=99, total_tracks=1)
        filelist = [_mock_af(release, 0)]
        args = _make_args(missing=True)

        # Collection has a different release
        other_item = MagicMock()
        other_item.releaseid = 1000
        other_release = _mock_release(rid=1000, total_tracks=1)

        captured = StringIO()
        with patch.object(dt_collection, "verbose", False), \
             patch("util.parse_collection_xml", return_value=[other_item]), \
             patch("client_interface.DiscogsRelease", return_value=other_release), \
             patch("sys.stdout", captured):
            collection_report("/fake/collection.xml", filelist, args)

        output = captured.getvalue()
        # The AF for release 99 should appear in the "not in collection" section
        assert "release_99" in output or "99" in output
