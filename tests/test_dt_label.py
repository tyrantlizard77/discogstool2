"""Tests for dt_label — pure helper functions.

Covers:
  - get_side: side letter extraction from position strings
  - side_to_disc: side letter → disc number mapping
  - group_tracks_by_disc: builds disc/side structure from a fake release
  - flatten_discs: orders disc/side/track tuples
  - parse_release_id: integer and [rXXXXX] format parsing
  - read_id_file: URL mode and plain-ID mode
  - _continuous_height: height calculation for continuous labels
  - _chunk_continuous: greedy packing of tracks onto labels
  - load_config / save_config: key=value dotfile I/O
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
from unittest.mock import patch, MagicMock

import pytest

# ── Load dt_label as a module (it has no .py extension) ──────────────────────

_DT_LABEL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dt_label"
)

_loader = importlib.machinery.SourceFileLoader("dt_label", _DT_LABEL_PATH)
_spec   = importlib.util.spec_from_loader("dt_label", _loader)
dt_label = importlib.util.module_from_spec(_spec)
sys.modules["dt_label"] = dt_label
_loader.exec_module(dt_label)

get_side            = dt_label.get_side
side_to_disc        = dt_label.side_to_disc
group_tracks_by_disc = dt_label.group_tracks_by_disc
flatten_discs       = dt_label.flatten_discs
parse_release_id    = dt_label.parse_release_id
read_id_file        = dt_label.read_id_file
_continuous_height  = dt_label._continuous_height
_chunk_continuous   = dt_label._chunk_continuous
load_config         = dt_label.load_config
save_config         = dt_label.save_config
LABEL_PROFILES      = dt_label.LABEL_PROFILES
MAX_LABEL_HEIGHT_PX = dt_label.MAX_LABEL_HEIGHT_PX


# ─── get_side ─────────────────────────────────────────────────────────────────

class TestGetSide:
    def test_alpha_numeric(self):
        assert get_side("A1") == "A"

    def test_side_b(self):
        assert get_side("B2") == "B"

    def test_double_letter(self):
        assert get_side("AA1") == "AA"

    def test_pure_letter(self):
        assert get_side("A") == "A"

    def test_numeric_only_returns_empty(self):
        assert get_side("1") == ""
        assert get_side("12") == ""

    def test_empty_string(self):
        assert get_side("") == ""

    def test_lowercase_uppercased(self):
        assert get_side("a1") == "A"

    def test_side_c(self):
        assert get_side("C3") == "C"

    def test_none_like_empty(self):
        # Position with leading/trailing spaces
        assert get_side("  B1  ") == "B"


# ─── side_to_disc ─────────────────────────────────────────────────────────────

class TestSideToDisc:
    def test_a_is_disc_1(self):
        assert side_to_disc("A") == 1

    def test_b_is_disc_1(self):
        assert side_to_disc("B") == 1

    def test_c_is_disc_2(self):
        assert side_to_disc("C") == 2

    def test_d_is_disc_2(self):
        assert side_to_disc("D") == 2

    def test_e_is_disc_3(self):
        assert side_to_disc("E") == 3

    def test_f_is_disc_3(self):
        assert side_to_disc("F") == 3

    def test_empty_returns_1(self):
        assert side_to_disc("") == 1

    def test_double_letter_uses_first_char(self):
        # AA → A → disc 1
        assert side_to_disc("AA") == 1

    def test_lowercase_input(self):
        assert side_to_disc("c") == 2


# ─── group_tracks_by_disc / flatten_discs ─────────────────────────────────────

def _fake_release(positions):
    """Build a minimal fake release with tracks at the given positions."""
    tracks = []
    for pos in positions:
        t = MagicMock()
        t.__getitem__ = lambda self, k, _p=pos: {"position": _p}[k]
        t.getArtist.return_value = "Artist"
        tracks.append(t)

    release = MagicMock()
    release.getTotalTracks.return_value = len(tracks)
    release.getTrack.side_effect = lambda i: tracks[i]
    return release


class TestGroupTracksByDisc:
    def test_single_side(self):
        rel = _fake_release(["A1", "A2", "A3"])
        discs = group_tracks_by_disc(rel)
        assert 1 in discs
        assert "A" in discs[1]
        assert len(discs[1]["A"]) == 3

    def test_two_sides_one_disc(self):
        rel = _fake_release(["A1", "A2", "B1", "B2"])
        discs = group_tracks_by_disc(rel)
        assert set(discs.keys()) == {1}
        assert "A" in discs[1]
        assert "B" in discs[1]

    def test_four_sides_two_discs(self):
        rel = _fake_release(["A1", "B1", "C1", "D1"])
        discs = group_tracks_by_disc(rel)
        assert 1 in discs and 2 in discs
        assert "A" in discs[1] and "B" in discs[1]
        assert "C" in discs[2] and "D" in discs[2]

    def test_numeric_positions_grouped_as_disc_1(self):
        rel = _fake_release(["1", "2", "3"])
        discs = group_tracks_by_disc(rel)
        assert 1 in discs

    def test_global_idx_preserved(self):
        rel = _fake_release(["A1", "A2", "B1"])
        discs = group_tracks_by_disc(rel)
        # Global indices should be 0, 1, 2 in order
        a_indices = [idx for idx, _ in discs[1]["A"]]
        b_indices = [idx for idx, _ in discs[1]["B"]]
        assert a_indices == [0, 1]
        assert b_indices == [2]


class TestFlattenDiscs:
    def test_single_disc_ordering(self):
        rel = _fake_release(["A1", "A2", "B1", "B2"])
        discs = group_tracks_by_disc(rel)
        flat = flatten_discs(discs)
        # Should be A tracks first, then B tracks
        sides = [entry[0] for entry in flat]
        assert sides == ["A", "A", "B", "B"]

    def test_multi_disc_ordering(self):
        rel = _fake_release(["A1", "B1", "C1", "D1"])
        discs = group_tracks_by_disc(rel)
        flat = flatten_discs(discs)
        sides = [entry[0] for entry in flat]
        assert sides == ["A", "B", "C", "D"]

    def test_entry_structure(self):
        rel = _fake_release(["A1"])
        discs = group_tracks_by_disc(rel)
        flat = flatten_discs(discs)
        assert len(flat) == 1
        side, idx, track = flat[0]
        assert side == "A"
        assert idx == 0


# ─── parse_release_id ─────────────────────────────────────────────────────────

class TestParseReleaseId:
    def test_plain_integer_string(self):
        assert parse_release_id("12345") == 12345

    def test_bracketed_format(self):
        assert parse_release_id("[r99999]") == 99999

    def test_bracketed_with_spaces(self):
        assert parse_release_id("  [r12345]  ") == 12345

    def test_large_number(self):
        assert parse_release_id("123456789") == 123456789

    def test_invalid_raises(self):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_release_id("notanumber")

    def test_float_raises(self):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_release_id("123.45")

    def test_bracketed_without_r_raises(self):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_release_id("[12345]")


# ─── read_id_file ─────────────────────────────────────────────────────────────

class TestReadIdFile:
    def _write(self, tmp_path, content):
        p = tmp_path / "ids.txt"
        p.write_text(content)
        return str(p)

    def test_plain_ids(self, tmp_path):
        path = self._write(tmp_path, "11111\n22222\n33333\n")
        result = read_id_file(path)
        ids = [r[2] for r in result]
        assert ids == [11111, 22222, 33333]

    def test_bracketed_ids(self, tmp_path):
        path = self._write(tmp_path, "[r12345]\n[r99999]\n")
        result = read_id_file(path)
        ids = [r[2] for r in result]
        assert ids == [12345, 99999]

    def test_url_mode(self, tmp_path):
        path = self._write(tmp_path,
            "https://www.discogs.com/release/12345 - Some Album\n"
            "https://www.discogs.com/release/99999 - Another\n"
        )
        result = read_id_file(path)
        ids = [r[2] for r in result]
        assert ids == [12345, 99999]

    def test_url_mode_ignores_plain_ids(self, tmp_path):
        """When URLs are present, plain ID lines are ignored."""
        path = self._write(tmp_path,
            "https://www.discogs.com/release/55555\n"
            "77777\n"  # plain ID — should be ignored in URL mode
        )
        result = read_id_file(path)
        ids = [r[2] for r in result]
        assert 55555 in ids
        assert 77777 not in ids

    def test_skips_blank_lines(self, tmp_path):
        path = self._write(tmp_path, "\n11111\n\n22222\n\n")
        result = read_id_file(path)
        assert len(result) == 2

    def test_skips_comment_lines(self, tmp_path):
        path = self._write(tmp_path, "# comment\n11111\n# another\n22222\n")
        result = read_id_file(path)
        ids = [r[2] for r in result]
        assert ids == [11111, 22222]

    def test_line_numbers_returned(self, tmp_path):
        path = self._write(tmp_path, "11111\n22222\n")
        result = read_id_file(path)
        linenos = [r[0] for r in result]
        assert linenos == [1, 2]

    def test_empty_file(self, tmp_path):
        path = self._write(tmp_path, "")
        result = read_id_file(path)
        assert result == []


# ─── load_config / save_config ────────────────────────────────────────────────

class TestConfig:
    def test_load_empty_when_no_file(self, tmp_path):
        fake_path = str(tmp_path / "label_config")
        with patch.object(dt_label.util, "userfile", return_value=fake_path):
            config = load_config()
        assert config == {}

    def test_save_and_load_roundtrip(self, tmp_path):
        fake_path = str(tmp_path / "label_config")
        with patch.object(dt_label.util, "userfile", return_value=fake_path):
            save_config({"printer": "tcp://192.168.1.50:9100", "model": "QL-1110NWB"})
            config = load_config()
        assert config["printer"] == "tcp://192.168.1.50:9100"
        assert config["model"] == "QL-1110NWB"

    def test_load_ignores_comments(self, tmp_path):
        fake_path = str(tmp_path / "label_config")
        with open(fake_path, "w") as f:
            f.write("# this is a comment\nprinter=tcp://1.2.3.4:9100\n")
        with patch.object(dt_label.util, "userfile", return_value=fake_path):
            config = load_config()
        assert "printer" in config
        assert "#" not in config

    def test_save_writes_key_value_pairs(self, tmp_path):
        fake_path = str(tmp_path / "label_config")
        with patch.object(dt_label.util, "userfile", return_value=fake_path):
            save_config({"profile": "dk1247"})
        content = open(fake_path).read()
        assert "profile=dk1247" in content

    def test_values_with_spaces_preserved(self, tmp_path):
        fake_path = str(tmp_path / "label_config")
        with patch.object(dt_label.util, "userfile", return_value=fake_path):
            save_config({"printer": "tcp://192.168.1.50:9100"})
            config = load_config()
        assert config["printer"] == "tcp://192.168.1.50:9100"


# ─── _continuous_height ───────────────────────────────────────────────────────

def _make_tracks_with_sides(sides_and_counts):
    """Build a minimal [(side, idx, track)] list for height calculation tests."""
    result = []
    idx = 0
    for side, count in sides_and_counts:
        for _ in range(count):
            result.append((side, idx, MagicMock()))
            idx += 1
    return result


class TestContinuousHeight:
    PROFILE = LABEL_PROFILES["dk22243"]

    def test_returns_positive_int(self):
        tracks = _make_tracks_with_sides([("A", 4)])
        h = _continuous_height(tracks, self.PROFILE)
        assert isinstance(h, int)
        assert h > 0

    def test_more_tracks_is_taller(self):
        h4 = _continuous_height(_make_tracks_with_sides([("A", 4)]), self.PROFILE)
        h8 = _continuous_height(_make_tracks_with_sides([("A", 8)]), self.PROFILE)
        assert h8 > h4

    def test_continuation_shorter_than_normal(self):
        tracks = _make_tracks_with_sides([("A", 4)])
        h_normal = _continuous_height(tracks, self.PROFILE, continuation=False)
        h_cont   = _continuous_height(tracks, self.PROFILE, continuation=True)
        assert h_cont < h_normal

    def test_disc_info_adds_height(self):
        tracks = _make_tracks_with_sides([("A", 4)])
        h_no_disc = _continuous_height(tracks, self.PROFILE, disc_info=None)
        h_disc    = _continuous_height(tracks, self.PROFILE, disc_info=(1, 2))
        assert h_disc > h_no_disc

    def test_side_headers_counted(self):
        """Two separate sides should be taller than one (extra side header)."""
        h1 = _continuous_height(_make_tracks_with_sides([("A", 4)]), self.PROFILE)
        h2 = _continuous_height(_make_tracks_with_sides([("A", 2), ("B", 2)]), self.PROFILE)
        assert h2 > h1

    def test_too_many_tracks_raises(self):
        """Extremely long tracklist should exceed 12" and raise ValueError."""
        profile = {
            "margin_px": 46,
            "notes_lines": 3,
        }
        tracks = _make_tracks_with_sides([("A", 200)])
        with pytest.raises(ValueError, match="12"):
            _continuous_height(tracks, profile)


# ─── _chunk_continuous ────────────────────────────────────────────────────────

class TestChunkContinuous:
    PROFILE = LABEL_PROFILES["dk22243"]

    def test_small_release_fits_one_chunk(self):
        tracks = _make_tracks_with_sides([("A", 4)])
        chunks = _chunk_continuous(tracks, self.PROFILE)
        assert len(chunks) == 1
        assert chunks[0] == tracks

    def test_empty_tracks_empty_chunks(self):
        chunks = _chunk_continuous([], self.PROFILE)
        assert chunks == []

    def test_overflow_creates_multiple_chunks(self):
        """A very large tracklist must be split across multiple labels."""
        tracks = _make_tracks_with_sides([("A", 100)])
        chunks = _chunk_continuous(tracks, self.PROFILE)
        assert len(chunks) > 1

    def test_all_tracks_preserved(self):
        """No tracks should be lost when splitting across chunks."""
        tracks = _make_tracks_with_sides([("A", 100)])
        chunks = _chunk_continuous(tracks, self.PROFILE)
        total = sum(len(c) for c in chunks)
        assert total == len(tracks)

    def test_chunk_order_preserved(self):
        """Tracks within and across chunks must stay in original order."""
        tracks = _make_tracks_with_sides([("A", 100)])
        chunks = _chunk_continuous(tracks, self.PROFILE)
        flat = [t for chunk in chunks for t in chunk]
        assert flat == tracks

    def test_each_chunk_fits_within_max_height(self):
        """Every produced chunk must fit within MAX_LABEL_HEIGHT_PX."""
        tracks = _make_tracks_with_sides([("A", 100)])
        chunks = _chunk_continuous(tracks, self.PROFILE)
        for i, chunk in enumerate(chunks):
            h = _continuous_height(chunk, self.PROFILE, continuation=(i > 0))
            assert h <= MAX_LABEL_HEIGHT_PX
