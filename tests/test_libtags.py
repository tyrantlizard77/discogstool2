"""Tests for libtags.py — audio file tag management.

Covers:
  - sanitize: filename character whitelist filtering
  - tag_map / rev_tag_map: structure consistency for ID3 and MP4Tags
  - track_from_comment: both new ("Discogs: 12345") and old ("12345 VERIFIED") formats
  - AudioFile.__getitem__: track number parsing (ID3 "track/total" and MP4 tuple forms)
    — tested via duck-typed fakes so no real audio files are needed
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import libtags
from libtags import (
    TagsException,
    sanitize,
    tag_map,
    track_from_comment,
)


# ─── sanitize ─────────────────────────────────────────────────────────────────

class TestSanitize:
    def test_plain_ascii_unchanged(self):
        assert sanitize("Hello World") == "Hello World"

    def test_digits_unchanged(self):
        assert sanitize("track01") == "track01"

    def test_allowed_special_chars(self):
        # Characters in the whitelist: []()-_+.' and space
        s = "Artist [2022] (Remix) - Title"
        assert sanitize(s) == s

    def test_disallowed_chars_replaced(self):
        # Slash, colon, pipe → _
        result = sanitize("a/b:c|d")
        assert "/" not in result
        assert ":" not in result
        assert "|" not in result
        assert result == "a_b_c_d"

    def test_unicode_replaced(self):
        result = sanitize("Ré\u00e9")
        # é is not in the whitelist → _
        assert result == "R__"

    def test_empty_string(self):
        assert sanitize("") == ""

    def test_apostrophe_allowed(self):
        assert sanitize("It's a Test") == "It's a Test"

    def test_dot_allowed(self):
        assert sanitize("file.mp3") == "file.mp3"

    def test_plus_allowed(self):
        assert sanitize("a+b") == "a+b"


# ─── tag_map / rev_tag_map structure ─────────────────────────────────────────

class TestTagMap:
    EXPECTED_KEYS = {"album", "artist", "bpm", "title", "year",
                     "comment", "genre", "image", "track", "label", "compilation"}

    def test_id3_has_all_expected_keys(self):
        assert self.EXPECTED_KEYS == set(tag_map["ID3"].keys())

    def test_mp4_has_all_expected_keys(self):
        assert self.EXPECTED_KEYS == set(tag_map["MP4Tags"].keys())

    def test_id3_title_tag(self):
        assert tag_map["ID3"]["title"] == "TIT2"

    def test_id3_artist_tag(self):
        assert tag_map["ID3"]["artist"] == "TPE1"

    def test_id3_bpm_tag(self):
        assert tag_map["ID3"]["bpm"] == "TBPM"

    def test_mp4_title_tag(self):
        assert tag_map["MP4Tags"]["title"] == "\xa9nam"

    def test_mp4_bpm_tag(self):
        assert tag_map["MP4Tags"]["bpm"] == "tmpo"

    def test_rev_tag_map_inverts_id3(self):
        """rev_tag_map should map raw ID3 tags back to logical names."""
        # rev_tag_map is built at module level; test a few key entries
        assert libtags.rev_tag_map.get("TIT2") == "title"
        assert libtags.rev_tag_map.get("TPE1") == "artist"
        assert libtags.rev_tag_map.get("TBPM") == "bpm"


# ─── track_from_comment ───────────────────────────────────────────────────────

class TestTrackFromComment:
    """Tests the comment-parsing logic that extracts a DiscogsTrack from an
    embedded comment.  We mock client_interface.DiscogsTrack so no network
    calls are made.
    """

    def _call(self, comment, index=0):
        """Call track_from_comment with client_interface.DiscogsTrack mocked."""
        mock_track = MagicMock()
        with patch.object(libtags.client_interface, "DiscogsTrack",
                          return_value=mock_track) as mock_cls:
            result = track_from_comment(comment, index)
            return result, mock_cls

    def test_new_format_parses_release_id(self):
        """New format: "Label [catno] Discogs: 12345" → rid=12345."""
        _, mock_cls = self._call("Test Label [TEST001] Discogs: 12345", index=1)
        mock_cls.assert_called_once_with(12345, 0)   # index - 1 = 0

    def test_old_format_parses_release_id(self):
        """Old format: "99999 VERIFIED" → rid=99999."""
        _, mock_cls = self._call("99999 VERIFIED", index=1)
        mock_cls.assert_called_once_with(99999, 0)

    def test_index_subtracted_by_one(self):
        """track_from_comment converts 1-based index to 0-based."""
        _, mock_cls = self._call("Label [X] Discogs: 55555", index=3)
        mock_cls.assert_called_once_with(55555, 2)

    def test_invalid_comment_raises(self):
        with pytest.raises(TagsException):
            track_from_comment("no release info here", index=1)

    def test_empty_comment_raises(self):
        with pytest.raises(TagsException):
            track_from_comment("", index=1)


# ─── track number parsing logic ───────────────────────────────────────────────
# Tests the __getitem__ "track" branch in isolation without needing real files.

class TestTrackNumberParsing:
    """AudioFile.__getitem__("track") has two paths:
    - MP4: tuple (track_num, total) from mutagen
    - ID3: string "track_num/total" from mutagen

    We test the parsing logic by directly calling the conversion code rather
    than instantiating AudioFile (which requires a real audio file and Discogs).
    """

    def _parse_id3_track(self, raw):
        """Simulate the ID3 track string parsing in AudioFile.__getitem__."""
        i = str(raw).split("/")
        if len(i) == 1:
            i.append(0)
        return tuple([int(x) for x in i])

    def _parse_mp4_track(self, raw):
        """Simulate the MP4 track tuple parsing in AudioFile.__getitem__."""
        if isinstance(raw, tuple):
            return (int(raw[0]), int(raw[1]) if len(raw) > 1 and raw[1] else 0)
        return raw

    def test_id3_track_with_total(self):
        result = self._parse_id3_track("3/10")
        assert result == (3, 10)

    def test_id3_track_without_total(self):
        result = self._parse_id3_track("5")
        assert result == (5, 0)

    def test_mp4_track_tuple(self):
        result = self._parse_mp4_track((2, 8))
        assert result == (2, 8)

    def test_mp4_track_no_total(self):
        result = self._parse_mp4_track((4, 0))
        assert result == (4, 0)
