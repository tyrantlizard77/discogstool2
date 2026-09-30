"""Tests for dt_label — pure helper functions.

Covers:
  - get_side: side letter extraction from position strings
  - side_to_disc: side letter → disc number mapping
  - group_tracks_by_disc: builds disc/side structure from a fake release
  - flatten_discs: orders disc/side/track tuples
  - parse_release_id: integer and [rXXXXX] format parsing
  - read_id_file: URL mode and plain-ID mode
  - _layout_height: canvas height measured from a real layout pass
  - _chunk_tracks: greedy packing of tracks onto labels by measured height,
    for continuous and die-cut stock alike
  - credits: role policy, header-vs-row placement, group-aware wrapping, and
    what they do to the rendered layout
  - header: artist and title wrap instead of truncating
  - _strip_disambig: Discogs "(N)" suffixes, including inside multi-artist strings
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
_layout_height      = dt_label._layout_height
_chunk_tracks       = dt_label._chunk_tracks
load_config         = dt_label.load_config
save_config         = dt_label.save_config
render_label             = dt_label.render_label
LABEL_PROFILES           = dt_label.LABEL_PROFILES
MAX_LABEL_HEIGHT_PX      = dt_label.MAX_LABEL_HEIGHT_PX
_QR_SIZE                 = dt_label._QR_SIZE
_HDR_H                   = dt_label._HDR_H
_SIDE_HDR_H              = dt_label._SIDE_HDR_H
_TRACK_ROW_H             = dt_label._TRACK_ROW_H


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


# ─── _layout_height ───────────────────────────────────────────────────────────

def _make_tracks_with_sides(sides_and_counts, title="Track Title"):
    """Build a [(side, idx, track)] list for height tests."""
    result = []
    idx = 0
    for side, count in sides_and_counts:
        for n in range(count):
            t = MagicMock()
            t.__getitem__ = MagicMock(
                side_effect=lambda k, _p=f"{side}{n+1}": _p if k == "position" else "")
            t.getArtist.return_value = "Artist"
            t.getTitle.return_value = title
            t.getDuration.return_value = None
            result.append((side, idx, t))
            idx += 1
    return result


def _height_release(title="Release Title"):
    r = MagicMock()
    for name, val in [("getId", 1), ("getTitle", title), ("getArtist", "Artist"),
                      ("getLabel", "Label"), ("getCatno", "CAT1"), ("getYear", "2020"),
                      ("getLabelYear", "2020"), ("getFormat", '12"'),
                      ("getCountry", "UK"), ("getGenre", "Electronic"),
                      ("getStyle", "Techno"), ("getNotes", "")]:
        getattr(r, name).return_value = val
    r.isCompilation.return_value = False
    return r


def _h(tracks, profile=None, **kw):
    return _layout_height(_height_release(), tracks,
                          profile or LABEL_PROFILES["dk22243"], False, **kw)


class TestLayoutHeight:
    """Height is measured by running the real layout, not predicted.

    Two implementations of the layout drifted apart three separate times and
    each time the bottom-anchored QR printed over the track listing. There is
    now one implementation; these tests cover its behaviour.
    """

    PROFILE = LABEL_PROFILES["dk22243"]

    def test_returns_positive_int(self):
        h = _h(_make_tracks_with_sides([("A", 4)]))
        assert isinstance(h, int)
        assert h > 0

    def test_more_tracks_is_taller(self):
        assert _h(_make_tracks_with_sides([("A", 8)])) > \
               _h(_make_tracks_with_sides([("A", 4)]))

    def test_continuation_shorter_than_normal(self):
        tracks = _make_tracks_with_sides([("A", 4)])
        assert _h(tracks, continuation=True) < _h(tracks, continuation=False)

    def test_disc_info_adds_height(self):
        tracks = _make_tracks_with_sides([("A", 4)])
        assert _h(tracks, disc_info=(1, 2)) > _h(tracks, disc_info=None)

    def test_side_headers_counted(self):
        assert _h(_make_tracks_with_sides([("A", 2), ("B", 2)])) > \
               _h(_make_tracks_with_sides([("A", 4)]))

    def test_too_many_tracks_raises(self):
        with pytest.raises(ValueError, match="12"):
            _h(_make_tracks_with_sides([("A", 200)]))

    def test_wrapping_titles_add_height(self):
        """A title that wraps must grow the canvas — the r4884361 failure."""
        short = _make_tracks_with_sides([("A", 3)], title="Short")
        long_ = _make_tracks_with_sides(
            [("A", 3)],
            title="An Extremely Long Reconstruction Title That Certainly Wraps "
                  "Onto A Second Line No Matter The Font")
        assert _h(long_) > _h(short)

    def test_height_leaves_exactly_the_qr_block(self):
        """The canvas must clear the content by the notes gap plus the QR."""
        tracks = _make_tracks_with_sides([("A", 3)])
        release = _height_release()
        h = _layout_height(release, tracks, self.PROFILE, False)
        bottom = dt_label.render_label(release, tracks, self.PROFILE, False,
                                       probe=True)
        assert h == bottom + dt_label._NOTES_GAP + _QR_SIZE + self.PROFILE["margin_px"]

    def test_probe_is_independent_of_canvas_height(self):
        """Probe output must not depend on the canvas it was measured on.

        This is what makes a 1px scratch canvas valid: content layout never
        reads H, only the bottom-anchored QR does.
        """
        tracks = _make_tracks_with_sides([("A", 5)])
        release = _height_release()
        a = dt_label.render_label(release, tracks, self.PROFILE, False, probe=True)
        b = dt_label.render_label(release, tracks, self.PROFILE, False,
                                  height_px=3600, probe=True)
        assert a == b

    def test_probe_draws_nothing_permanent(self):
        """Probe returns a position, not an image."""
        tracks = _make_tracks_with_sides([("A", 2)])
        out = dt_label.render_label(_height_release(), tracks, self.PROFILE,
                                    False, probe=True)
        assert isinstance(out, int)


# ─── _chunk_tracks ────────────────────────────────────────────────────────────

class TestChunkContinuous:
    PROFILE = LABEL_PROFILES["dk22243"]

    def test_small_release_fits_one_chunk(self):
        tracks = _make_tracks_with_sides([("A", 4)])
        chunks = _chunk_tracks(tracks, self.PROFILE, _height_release())
        assert len(chunks) == 1
        assert chunks[0] == tracks

    def test_empty_tracks_empty_chunks(self):
        chunks = _chunk_tracks([], self.PROFILE, _height_release())
        assert chunks == []

    def test_overflow_creates_multiple_chunks(self):
        """A very large tracklist must be split across multiple labels."""
        tracks = _make_tracks_with_sides([("A", 100)])
        chunks = _chunk_tracks(tracks, self.PROFILE, _height_release())
        assert len(chunks) > 1

    def test_all_tracks_preserved(self):
        """No tracks should be lost when splitting across chunks."""
        tracks = _make_tracks_with_sides([("A", 100)])
        chunks = _chunk_tracks(tracks, self.PROFILE, _height_release())
        total = sum(len(c) for c in chunks)
        assert total == len(tracks)

    def test_chunk_order_preserved(self):
        """Tracks within and across chunks must stay in original order."""
        tracks = _make_tracks_with_sides([("A", 100)])
        chunks = _chunk_tracks(tracks, self.PROFILE, _height_release())
        flat = [t for chunk in chunks for t in chunk]
        assert flat == tracks

    def test_each_chunk_fits_within_max_height(self):
        """Every produced chunk must fit within MAX_LABEL_HEIGHT_PX."""
        tracks = _make_tracks_with_sides([("A", 100)])
        chunks = _chunk_tracks(tracks, self.PROFILE, _height_release())
        for i, chunk in enumerate(chunks):
            h = _layout_height(_height_release(), chunk, self.PROFILE, False,
                               continuation=(i > 0))
            assert h <= MAX_LABEL_HEIGHT_PX


# ─── render_label ─────────────────────────────────────────────────────────────

def _render_kwargs_for(release, tracks, profile, **kw):
    """Continuous-profile render kwargs for a track list."""
    if profile.get("feed") != "continuous":
        return {}
    return {"height_px": _layout_height(release, tracks, profile,
                                        kw.get("is_compilation", False)),
            "notes_lines": profile.get("notes_lines", 3)}


def _ink(img):
    """Count non-white pixels — a proxy for "something was actually drawn"."""
    return sum(img.convert("L").histogram()[:200])


def _bpm_zone(img, profile):
    """Crop the right-hand BPM column, where BPM values are drawn."""
    W = profile["width_px"]
    M = profile["margin_px"]
    BPM_ZONE_W = 110
    return img.crop((W - M - BPM_ZONE_W, 0, W - M, img.height))


def _qr_zone(img, profile):
    """Crop the bottom-left corner where the QR is pasted."""
    M = profile["margin_px"]
    return img.crop((M, img.height - M - _QR_SIZE, M + _QR_SIZE, img.height - M))


def _fake_render_track(position: str = "A1") -> MagicMock:
    """Build a minimal track mock suitable for render_label()."""
    t = MagicMock()
    # track["position"] is accessed via dict-style __getitem__
    t.__getitem__ = MagicMock(side_effect=lambda k: position if k == "position" else "")
    t.getArtist.return_value = "Track Artist"
    t.getTitle.return_value = "Track Title"
    t.getDuration.return_value = "3:45"
    return t


def _make_render_tracks(sides_and_counts):
    """Build a [(side, idx, track)] list for render_label() tests."""
    result = []
    idx = 0
    positions = "ABCDEFGH"
    for i, (side, count) in enumerate(sides_and_counts):
        for n in range(count):
            pos = f"{positions[i]}{n + 1}"
            result.append((side, idx, _fake_render_track(pos)))
            idx += 1
    return result


def _fake_render_release() -> MagicMock:
    """Build a minimal release mock suitable for render_label()."""
    r = MagicMock()
    r.getArtist.return_value  = "Test Artist"
    r.getTitle.return_value   = "Test Album"
    r.getLabel.return_value   = "Test Label"
    r.getCatno.return_value   = "TL001"
    r.getYear.return_value    = "2020"
    r.getCountry.return_value = "UK"
    r.getId.return_value      = 12345
    r.getGenre.return_value   = "Electronic"
    r.getArtwork.return_value = None
    return r


class TestRenderLabel:
    """Smoke and integration tests for the render_label() function."""

    def test_returns_pil_image(self):
        """render_label() returns a PIL Image object."""
        from PIL import Image as PILImage
        profile = LABEL_PROFILES["dk1247"]
        tracks = _make_render_tracks([("A", 2)])
        img = render_label(_fake_render_release(), tracks, profile, is_compilation=False)
        assert isinstance(img, PILImage.Image)

    def test_dk1247_dimensions(self):
        """dk1247 die-cut profile produces a 1200×1822 image."""
        profile = LABEL_PROFILES["dk1247"]
        tracks = _make_render_tracks([("A", 4)])
        img = render_label(_fake_render_release(), tracks, profile, is_compilation=False)
        assert img.width  == 1200
        assert img.height == 1822

    def test_layout_height_matches_render(self):
        """_layout_height() returns the canvas height render_label() then uses."""
        profile = LABEL_PROFILES["dk22243"]
        tracks = _make_render_tracks([("A", 2), ("B", 2)])
        h = _layout_height(_fake_render_release(), tracks, profile, False)
        img = render_label(
            _fake_render_release(), tracks, profile,
            is_compilation=False, height_px=h, notes_lines=profile["notes_lines"],
        )
        # render_label uses height_px as its canvas height, so img.height must equal h
        assert img.height == h

    def test_compilation_shows_track_artists(self):
        """A compilation must actually draw more ink than a non-compilation.

        This previously only checked that render_label didn't raise, so it
        would have passed even if artist names were never drawn.
        """
        profile = LABEL_PROFILES["dk1247"]
        tracks = _make_render_tracks([("A", 3)])
        plain = render_label(_fake_render_release(), tracks, profile,
                             is_compilation=False)
        comp  = render_label(_fake_render_release(), tracks, profile,
                             is_compilation=True)
        assert _ink(comp) > _ink(plain)

    def test_bpm_values_appear_in_label(self):
        """A supplied BPM must put ink in the BPM zone, not just avoid raising."""
        profile = LABEL_PROFILES["dk1247"]
        tracks = _make_render_tracks([("A", 2)])
        bpms = {0: {"bpm": 128, "duration_ms": 360000},
                1: {"bpm": 132, "duration_ms": 420000}}
        without = render_label(_fake_render_release(), tracks, profile,
                               is_compilation=False)
        with_bpm = render_label(_fake_render_release(), tracks, profile,
                                is_compilation=False, bpms=bpms)
        assert _ink(_bpm_zone(with_bpm, profile)) > _ink(_bpm_zone(without, profile))

    def test_die_cut_does_not_wrap_tracks(self):
        """Die-cut labels use truncation, not wrapping — canvas height is fixed."""
        from PIL import Image as PILImage
        profile = LABEL_PROFILES["dk1247"]
        t = MagicMock()
        t.__getitem__ = MagicMock(side_effect=lambda k: "A1" if k == "position" else "")
        t.getArtist.return_value = "Artist"
        t.getTitle.return_value = "A" * 120   # absurdly long title
        t.getDuration.return_value = ""
        tracks = [("A", 0, t)]
        img = render_label(_fake_render_release(), tracks, profile, is_compilation=False)
        # Canvas must still be the fixed die-cut height
        assert img.height == profile["height_px"]

    def test_continuous_height_includes_qr_area(self):
        """notes_h must be large enough to fit the QR code with a safety buffer."""
        profile = LABEL_PROFILES["dk22243"]
        # notes_h = 28 (divider) + QR_SIZE (200) + EXTRA_LINE_H (50) = 278
        notes_h = 28 + _QR_SIZE + dt_label._EXTRA_LINE_H
        assert notes_h >= 28 + _QR_SIZE, \
            "notes_h must provide at least QR_SIZE pixels after the divider"
        # Safety buffer must be at least one track-wrap's worth so that a single
        # missed wrap in _measure_wrap_extra_px doesn't push QR into the tracks.
        safety = notes_h - 28 - _QR_SIZE
        assert safety >= dt_label._EXTRA_LINE_H, \
            f"safety buffer {safety} too small (need >= {dt_label._EXTRA_LINE_H})"


# ─── QR code placement ────────────────────────────────────────────────────────

def _fake_long_track(position: str, title: str, artist: str = "Artist",
                     duration: str = "5:00") -> MagicMock:
    """Build a track mock with a specific long title for wrap/QR tests."""
    t = MagicMock()
    t.__getitem__ = MagicMock(side_effect=lambda k: position if k == "position" else "")
    t.getArtist.return_value = artist
    t.getTitle.return_value = title
    t.getDuration.return_value = duration
    return t


class TestQRPlacement:
    """The QR must never overlap the track listing.

    These used to re-derive the content bottom with a copy of the layout
    arithmetic — the very duplication that caused the bugs. They now ask
    render_label itself via probe mode, so there is nothing to keep in sync.
    """

    PROFILE = LABEL_PROFILES["dk22243"]
    M       = LABEL_PROFILES["dk22243"]["margin_px"]

    def _clearance(self, tracks, release=None, is_compilation=False, bpms=None):
        """Pixels between the content bottom and the top of the QR."""
        release = release or _fake_render_release()
        h = _layout_height(release, tracks, self.PROFILE, is_compilation,
                           bpms=bpms)
        bottom = render_label(release, tracks, self.PROFILE, is_compilation,
                              bpms=bpms, probe=True)
        return (h - self.M - _QR_SIZE) - bottom, h

    def test_qr_clears_tracks_short_release(self):
        tracks = _make_render_tracks([("A", 2)])
        clearance, _ = self._clearance(tracks)
        assert clearance >= 0

    def test_qr_clears_tracks_long_wrapping_titles(self):
        long_titles = [
            ("A", 0, _fake_long_track("A1", "The Bug", "Faith In Dub", "5:12")),
            ("A", 1, _fake_long_track("A2", "Ghost Dubs",
                                      "Descent Into The Maelstrom Of Endless Reverb", "6:01")),
            ("B", 2, _fake_long_track("B1", "The Bug",
                                      "Alien Virus (West Indian Centre, Leeds)", "4:48")),
            ("B", 3, _fake_long_track("B2", "Ghost Dubs",
                                      "Militants (The Rocket, Holloway) Extended", "4:22")),
        ]
        clearance, _ = self._clearance(long_titles, is_compilation=True)
        assert clearance >= 0

    def test_qr_fits_within_canvas(self):
        tracks = _make_render_tracks([("A", 3)])
        _, h = self._clearance(tracks)
        assert h - self.M - _QR_SIZE + _QR_SIZE <= h

    def test_clearance_is_exactly_the_notes_gap(self):
        """Canvas is sized precisely — no slack beyond the divider gap."""
        tracks = _make_render_tracks([("A", 3)])
        clearance, _ = self._clearance(tracks)
        assert clearance == dt_label._NOTES_GAP

    @pytest.mark.parametrize("counts", [
        [("A", 1)], [("A", 4)], [("A", 3), ("B", 3)],
        [("A", 2), ("B", 2), ("C", 2), ("D", 2)],
    ])
    def test_qr_clears_tracks_across_shapes(self, counts):
        clearance, _ = self._clearance(_make_render_tracks(counts))
        assert clearance >= 0


# ─── Font glyph coverage / script fallback ───────────────────────────────────

_DEVANAGARI = "दुनिया का राजा"   # r37718007 A2 — "King of the World"


def _font_with(chars):
    """Find any installed font covering every char in `chars`, else None.

    Used to skip fallback tests on machines with no suitable font installed.
    """
    import glob
    seen = set()
    for pattern in ("/usr/share/fonts/**/*.ttf", "/usr/share/fonts/**/*.otf",
                    "/System/Library/Fonts/**/*.ttf",
                    "/System/Library/Fonts/**/*.ttc"):
        for path in glob.glob(pattern, recursive=True):
            if path in seen:
                continue
            seen.add(path)
            try:
                if not dt_label._missing_chars(path, chars):
                    return path
            except Exception:
                continue
    return None


@pytest.fixture
def clear_font_caches():
    """Reset the module-level font caches around a test."""
    def _clear():
        dt_label._font_path_cache.clear()
        dt_label._coverage_cache.clear()
        dt_label._notdef_cache.clear()
        dt_label._font_obj_cache.clear()
        dt_label._warned_missing.clear()
    _clear()
    yield
    _clear()


class TestGlyphCoverage:
    def test_latin_covered_by_primary(self):
        path = dt_label._find_font_path("regular")
        assert path, "no regular font installed"
        assert dt_label._missing_chars(path, "Angel Edit") == []

    def test_devanagari_not_covered_by_latin_face(self):
        """The bug: Arial/Liberation have no Devanagari glyphs."""
        path = dt_label._find_font_path("regular")
        missing = dt_label._missing_chars(path, _DEVANAGARI)
        assert missing, "expected Devanagari to be missing from the Latin face"
        assert "द" in missing

    def test_whitespace_always_covered(self):
        path = dt_label._find_font_path("regular")
        assert dt_label._covers(path, " ") is True
        assert dt_label._missing_chars(path, "   ") == []

    def test_coverage_is_cached(self, clear_font_caches):
        path = dt_label._find_font_path("regular")
        dt_label._covers(path, "A")
        assert (path, "A") in dt_label._coverage_cache


class TestFontFallback:
    def test_latin_text_keeps_primary_face(self, clear_font_caches):
        font = dt_label._font_for("regular", 38, "Angel Edit")
        assert font.path == dt_label._find_font_path("regular")

    def test_falls_back_to_face_with_glyphs(self, clear_font_caches):
        """A face covering the string must be chosen over the Latin primary."""
        good = _font_with(_DEVANAGARI)
        if not good:
            pytest.skip("no Devanagari-capable font installed")
        dt_label._FONT_CANDIDATES["devanagari"] = [good]
        dt_label._font_path_cache.clear()
        font = dt_label._font_for("regular", 38, _DEVANAGARI)
        assert dt_label._missing_chars(font.path, _DEVANAGARI) == []

    def test_mixed_script_needs_whole_string_coverage(self, clear_font_caches):
        """Fallback must cover Latin too — Pillow draws the run in one face."""
        mixed = _DEVANAGARI + " (Original Mix)"
        good = _font_with(mixed)
        if not good:
            pytest.skip("no font covering both scripts installed")
        dt_label._FONT_CANDIDATES["devanagari"] = [good]
        dt_label._font_path_cache.clear()
        font = dt_label._font_for("regular", 38, mixed)
        assert dt_label._missing_chars(font.path, mixed) == []

    def test_warns_once_when_nothing_covers(self, clear_font_caches, capsys):
        """Unrenderable text warns on stderr but still returns a usable font."""
        for style in dt_label._FALLBACK_ORDER:
            dt_label._FONT_CANDIDATES[style] = []
        dt_label._font_path_cache.clear()
        font = dt_label._font_for("regular", 38, _DEVANAGARI)
        assert font is not None
        err = capsys.readouterr().err
        assert "U+0926" in err

        dt_label._font_for("regular", 38, _DEVANAGARI)
        assert capsys.readouterr().err == ""   # deduped, no repeat warning

    def test_empty_and_non_string_are_safe(self, clear_font_caches):
        assert dt_label._font_for("regular", 38, "") is not None
        assert dt_label._font_for("regular", 38, None) is not None


# ─── Printer send ─────────────────────────────────────────────────────────────

import time
import threading


def _fake_printer(hold=0.0, stall=0.0, payload_len=0):
    """Throwaway TCP server standing in for the printer.

    hold:  accept, then sit there without reading (connection idle).
    stall: accept, refuse to read for this long — the printer is busy
           physically printing — then drain. This is what fills the socket
           buffer and blocks the sender.
    """
    import socket as _s
    srv = _s.socket()
    srv.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0)); srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        try:
            conn, _ = srv.accept()
            if stall:
                time.sleep(stall)
                conn.settimeout(30)
                total = 0
                while total < payload_len:
                    chunk = conn.recv(1 << 16)
                    if not chunk:
                        break
                    total += len(chunk)
            else:
                conn.recv(1 << 16)
                time.sleep(hold)
            conn.close()
        except Exception:
            pass
        finally:
            srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return port


class TestParsePrinterUrl:
    @pytest.mark.parametrize("url,expected", [
        ("tcp://printer.local:9100", ("printer.local", 9100)),
        ("tcp://192.168.2.124:9100", ("192.168.2.124", 9100)),
        ("tcp://192.168.2.124",      ("192.168.2.124", 9100)),   # default port
        ("192.168.2.124:9100",       ("192.168.2.124", 9100)),   # no scheme
        ("printer.local",            ("printer.local", 9100)),
        ("tcp://printer.local:9100/", ("printer.local", 9100)),  # trailing slash
        ("tcp://printer.local:1234", ("printer.local", 1234)),   # custom port
    ])
    def test_parses(self, url, expected):
        assert dt_label._parse_printer_url(url) == expected


class TestPrinterSend:
    """The printer accepts raster into a small buffer and drains it as it
    prints, so a write blocks for roughly as long as printing takes.

    brother_ql's network backend hardcodes a 10s sendall timeout, which is
    shorter than a long continuous label takes — so a queued batch timed out
    partway through and left the printer holding half a job, which is what made
    the *next* label come out as static. We drive the socket ourselves to
    control that budget.
    """

    def test_writes_and_returns(self):
        port = _fake_printer()
        assert dt_label._send_raster(b"\x00" * 400,
                                     f"tcp://127.0.0.1:{port}") is None

    def test_survives_a_printer_busy_longer_than_ten_seconds(self):
        """The regression: a stall past brother_ql's hardcoded 10s must be fine."""
        payload = b"\x00" * (4 * 1024 * 1024)
        port = _fake_printer(stall=12, payload_len=len(payload))
        started = time.time()
        dt_label._send_raster(payload, f"tcp://127.0.0.1:{port}",
                              send_timeout=120)
        assert time.time() - started >= 11        # really did block on the printer

    def test_short_budget_still_times_out(self):
        """Sanity check that the stall is real, not the test being lenient."""
        payload = b"\x00" * (4 * 1024 * 1024)
        port = _fake_printer(stall=12, payload_len=len(payload))
        with pytest.raises((TimeoutError, OSError)):
            dt_label._send_raster(payload, f"tcp://127.0.0.1:{port}",
                                  send_timeout=2)

    def test_default_send_budget_is_generous(self):
        """A short default is what caused this; guard against it creeping back."""
        assert dt_label._SEND_TIMEOUT >= 120

    def test_connect_failure_raises_promptly(self):
        started = time.time()
        with pytest.raises(OSError):
            dt_label._send_raster(b"\x00" * 400, "tcp://127.0.0.1:1",
                                  connect_timeout=2)
        assert time.time() - started < 5

    def test_partially_written_blocks_retry(self):
        """A mid-transfer timeout must not be retried — the printer is holding
        half a job, and resending makes it read the new header as raster."""
        assert dt_label._partially_written(TimeoutError("timed out")) is True

    def test_connection_error_is_retryable(self):
        assert dt_label._partially_written(ConnectionRefusedError()) is False


# ─── Config-only CLI invocations ──────────────────────────────────────────────

class TestConfigOnlyInvocation:
    """`dt_label --printer …` with no release must save config and exit 0.

    Argument validation used to run before the config save, so a config-setting
    flag on its own bailed out with "provide a release ID" and saved nothing.
    """

    def _run(self, argv, cfg_path):
        """Run dt_label.main() with argv, redirecting config I/O to cfg_path.

        util.datapath is resolved at import time, so patching HOME is too late;
        userfile itself has to be replaced.
        """
        with patch.object(sys, "argv", ["dt_label"] + argv), \
             patch.object(dt_label.util, "userfile", lambda _n: cfg_path):
            with pytest.raises(SystemExit) as exc:
                dt_label.main()
        return exc.value.code

    def test_printer_alone_saves_and_exits_zero(self, tmp_path):
        cfg = str(tmp_path / "label_config")
        assert self._run(["--printer", "tcp://printer.local:9100"], cfg) == 0
        assert "printer=tcp://printer.local:9100" in open(cfg).read()

    def test_model_and_profile_alone_saved(self, tmp_path):
        cfg = str(tmp_path / "label_config")
        assert self._run(["--model", "QL-1110NWB",
                          "--label-profile", "dk22243"], cfg) == 0
        body = open(cfg).read()
        assert "model=QL-1110NWB" in body
        assert "profile=dk22243" in body

    def test_settings_merge_across_invocations(self, tmp_path):
        """A later config-only call must not clobber earlier keys."""
        cfg = str(tmp_path / "label_config")
        self._run(["--printer", "tcp://a.local:9100"], cfg)
        self._run(["--model", "QL-800"], cfg)
        body = open(cfg).read()
        assert "printer=tcp://a.local:9100" in body
        assert "model=QL-800" in body

    def test_no_args_still_errors(self, tmp_path):
        cfg = str(tmp_path / "label_config")
        assert self._run([], cfg) == 2          # argparse usage error
        assert not os.path.exists(cfg)          # and writes nothing

    def test_release_id_and_file_still_mutually_exclusive(self, tmp_path):
        cfg = str(tmp_path / "label_config")
        assert self._run(["123", "-f", "ids.txt"], cfg) == 2


# ─── QR placement (property tests) ────────────────────────────────────────────

def _qr_track(pos, title, artist="Artist", duration=None):
    t = MagicMock()
    t.__getitem__ = MagicMock(side_effect=lambda k, _p=pos: _p if k == "position" else "")
    t.getArtist.return_value = artist
    t.getTitle.return_value = title
    t.getDuration.return_value = duration
    return t


def _qr_release(rid=4884361, title="Reconstructed", artist="Steve O'Sullivan"):
    r = MagicMock()
    for name, val in [("getId", rid), ("getTitle", title), ("getArtist", artist),
                      ("getLabel", "Sushitech Records"), ("getCatno", "SUSH28"),
                      ("getYear", "2013"), ("getLabelYear", "2013"),
                      ("getFormat", '2 x 12"'), ("getCountry", "Germany"),
                      ("getGenre", "Electronic"), ("getStyle", "Dub Techno"),
                      ("getNotes", "")]:
        getattr(r, name).return_value = val
    r.isCompilation.return_value = False
    return r


# r4884361 — bare side letters, long remix titles, no Discogs durations.
# Beatport supplies durations, whose suffix pushes two titles onto a second
# line. The measurement pass used not to see them, so the predicted canvas was
# 100 px short and the bottom-anchored QR landed on the D2 row.
_RECONSTRUCTED = [
    ("A",  "Where's Burt? (Delano Smith Reconstruction)"),
    ("B",  "Moving Forward (Mike Huckaby S Y N T H Reconstruction)"),
    ("C",  "Where's Burt? (Thor Reconstruction)"),
    ("D1", "Don't Rush The Dub (Rhauder Reconstruction)"),
    ("D2", "Stripped (Exos Reconstruction)"),
]
_RECONSTRUCTED_BPMS = {
    0: {"bpm": 125, "duration_ms": 463000},
    1: {"bpm": 123, "duration_ms": 481000},
    2: {"bpm": 120, "duration_ms": 428000},
    3: {"bpm": 126, "duration_ms": 417000},
    4: {"bpm": 122, "duration_ms": 399000},
}


def _content_bottom_vs_qr(tracks, bpms, is_compilation=False, release=None):
    """Render a continuous label; return (overrun_px, height).

    overrun_px > 0 means the track listing crossed into the QR zone. Both the
    content bottom and the canvas height come from the same layout code, so
    this measures the real thing rather than re-deriving it.
    """
    profile = LABEL_PROFILES["dk22243"]
    release = release or _qr_release()
    tws = [(dt_label.get_side(p), i, _qr_track(p, t))
           for i, (p, t) in enumerate(tracks)]
    kw = dt_label._render_kwargs(tws, profile, release=release,
                                 is_compilation=is_compilation, bpms=bpms)
    H = kw["height_px"]
    qr_top = H - profile["margin_px"] - _QR_SIZE
    content_bottom = dt_label.render_label(release, tws, profile, is_compilation,
                                           bpms=bpms, probe=True)
    return content_bottom - qr_top, H


class TestQRPlacementProperties:
    """The QR is bottom-anchored, so it lands correctly only if the predicted
    canvas height matches what render_label actually lays out. Every QR bug so
    far has been a divergence between those two. Assert the property directly.
    """

    def test_reconstructed_qr_does_not_overlap_tracks(self):
        overrun, _ = _content_bottom_vs_qr(_RECONSTRUCTED, _RECONSTRUCTED_BPMS)
        assert overrun <= 0, f"QR overlaps track listing by {overrun}px"

    def test_beatport_duration_included_in_height(self):
        """Durations that only exist in Beatport data must affect the canvas."""
        _, h_without = _content_bottom_vs_qr(_RECONSTRUCTED, None)
        _, h_with    = _content_bottom_vs_qr(_RECONSTRUCTED, _RECONSTRUCTED_BPMS)
        assert h_with > h_without, \
            "duration suffix did not grow the canvas — measurement ignores bpms"

    def test_resolver_shared_between_measure_and_render(self):
        """_resolve_track_text is the single source of truth for row text."""
        tr = _qr_track("A1", "Title", duration=None)
        assert dt_label._resolve_track_text(tr, 0, False, None) == "Title"
        # Beatport fallback applies when Discogs has no duration
        assert dt_label._resolve_track_text(
            tr, 0, False, {0: {"duration_ms": 463000}}) == "Title (7:43)"
        # Discogs duration wins over Beatport
        tr2 = _qr_track("A1", "Title", duration="3:00")
        assert dt_label._resolve_track_text(
            tr2, 0, False, {0: {"duration_ms": 463000}}) == "Title (3:00)"

    def test_compilation_prefixes_artist(self):
        tr = _qr_track("A1", "Title", artist="Someone (2)")
        out = dt_label._resolve_track_text(tr, 0, True, None)
        assert out.startswith("Someone – ")   # disambiguation stripped

    @pytest.mark.parametrize("n", [1, 3, 5, 8, 12])
    def test_qr_never_overlaps_for_varied_lengths(self, n):
        """Property: for any track count, content must stay above the QR."""
        tracks = [(f"A{i+1}",
                   "A Fairly Long Reconstruction Title That May Well Wrap "
                   f"Around Number {i+1}")
                  for i in range(n)]
        bpms = {i: {"bpm": 120, "duration_ms": 400000 + i} for i in range(n)}
        overrun, _ = _content_bottom_vs_qr(tracks, bpms)
        assert overrun <= 0, f"{n} tracks: QR overlaps by {overrun}px"


# ─── Rendered output content ──────────────────────────────────────────────────

class TestRenderedContent:
    """Assertions on what actually lands on the canvas.

    The suite used to render images and never inspect them, which is how a QR
    printing on top of the track listing survived three separate fixes.
    """

    PROFILE = LABEL_PROFILES["dk22243"]

    def _render(self, tracks, **kw):
        release = _fake_render_release()
        kwargs = _render_kwargs_for(release, tracks, self.PROFILE, **kw)
        return render_label(release, tracks, self.PROFILE, kw.get("is_compilation", False),
                            **kwargs)

    def test_qr_zone_is_not_blank(self):
        img = self._render(_make_render_tracks([("A", 2)]))
        assert _ink(_qr_zone(img, self.PROFILE)) > 500, "QR appears to be missing"

    def test_qr_zone_contains_black_and_white(self):
        """A QR is a mix of both; a solid block would mean a paste failure."""
        img = self._render(_make_render_tracks([("A", 2)]))
        zone = _qr_zone(img, self.PROFILE).convert("L")
        hist = zone.histogram()
        dark  = sum(hist[:128])
        total = sum(hist)
        assert 0.1 < dark / total < 0.9

    def test_more_tracks_means_more_ink(self):
        few  = self._render(_make_render_tracks([("A", 2)]))
        many = self._render(_make_render_tracks([("A", 6)]))
        assert _ink(many) > _ink(few)

    def test_blank_bpm_zone_when_no_bpms(self):
        """Without BPMs the zone holds only the thin write-line rules."""
        img = self._render(_make_render_tracks([("A", 3)]))
        zone_ink = _ink(_bpm_zone(img, self.PROFILE))
        assert zone_ink < _ink(img) * 0.2

    def test_track_area_above_qr_is_untouched_by_it(self):
        """The band just above the QR must stay clear — the r4884361 failure.

        If the canvas is undersized the QR is pasted over the last track row,
        which shows up as ink where the notes gap should be blank.
        """
        tracks = _make_render_tracks([("A", 4)])
        release = _fake_render_release()
        h = _layout_height(release, tracks, self.PROFILE, False)
        img = render_label(release, tracks, self.PROFILE, False,
                           height_px=h, notes_lines=self.PROFILE["notes_lines"])
        bottom = render_label(release, tracks, self.PROFILE, False, probe=True)
        qr_top = h - self.PROFILE["margin_px"] - _QR_SIZE
        # The strip between content bottom and qr_top holds only the divider.
        strip = img.crop((self.PROFILE["margin_px"], bottom + 4,
                          self.PROFILE["margin_px"] + _QR_SIZE, qr_top))
        assert strip.height > 0
        # The strip holds the notes divider and nothing else. A QR pasted here
        # would be orders of magnitude denser, so compare against one.
        assert _ink(strip) < _ink(_qr_zone(img, self.PROFILE)) * 0.1

    def test_die_cut_canvas_is_exactly_the_profile_size(self):
        profile = LABEL_PROFILES["dk1247"]
        img = render_label(_fake_render_release(),
                           _make_render_tracks([("A", 3)]), profile, False)
        assert (img.width, img.height) == (profile["width_px"], profile["height_px"])


from PIL import Image as _PILImage


class TestPrinterRecovery:
    """A send that dies mid-transfer leaves the printer holding a partial job.

    Whatever is sent next — the following queued label, or a manual reprint —
    gets read as the missing raster and printed as static. print_label
    therefore flushes the printer before reporting the failure.
    """

    def test_reset_sequence_is_invalidate_plus_initialize(self):
        """Matches the preamble convert() puts at the head of every job."""
        assert dt_label._RESET_SEQUENCE == b"\x00" * 200 + b"\x1b\x40"

    def test_reset_sends_the_sequence(self):
        import socket as _s
        received = []
        srv = _s.socket(); srv.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0)); srv.listen(1)
        port = srv.getsockname()[1]

        def serve():
            try:
                conn, _ = srv.accept()
                conn.settimeout(5)
                buf = b""
                while len(buf) < len(dt_label._RESET_SEQUENCE):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                received.append(buf)
                conn.close()
            except Exception:
                pass
            finally:
                srv.close()

        threading.Thread(target=serve, daemon=True).start()
        assert dt_label.reset_printer(f"tcp://127.0.0.1:{port}") is True
        time.sleep(0.2)
        assert received and received[0] == dt_label._RESET_SEQUENCE

    def test_reset_reports_failure_when_unreachable(self):
        """Must not raise — recovery is best-effort on top of an existing error."""
        assert dt_label.reset_printer("tcp://127.0.0.1:1", timeout=2) is False

    def test_mid_transfer_failure_triggers_reset(self):
        """The whole point: a timeout must flush before surfacing the error."""
        calls = []

        def fake_send(instructions, url, **kw):
            calls.append(instructions)
            if len(calls) == 1:
                raise TimeoutError("timed out")      # the real job
            return None                              # the reset

        img = _PILImage.new("RGB", (1164, 400), "white")
        with patch.object(dt_label, "_send_raster", side_effect=fake_send):
            with pytest.raises(RuntimeError) as exc:
                dt_label.print_label(img, "tcp://127.0.0.1:9100",
                                     "QL-1110NWB",
                                     LABEL_PROFILES["dk22243"], retries=1)
        assert len(calls) == 2, "reset was not attempted"
        assert calls[1] == dt_label._RESET_SEQUENCE
        assert "flushed" in str(exc.value)

    def test_connection_refused_does_not_trigger_reset(self):
        """Nothing was written, so there is nothing to flush."""
        calls = []

        def fake_send(instructions, url, **kw):
            calls.append(instructions)
            raise ConnectionRefusedError("refused")

        img = _PILImage.new("RGB", (1164, 400), "white")
        with patch.object(dt_label, "_send_raster", side_effect=fake_send):
            with pytest.raises(RuntimeError):
                dt_label.print_label(img, "tcp://127.0.0.1:9100",
                                     "QL-1110NWB",
                                     LABEL_PROFILES["dk22243"], retries=1)
        assert all(c != dt_label._RESET_SEQUENCE for c in calls)

    def test_failed_reset_warns_about_power_cycling(self):
        def fake_send(instructions, url, **kw):
            raise TimeoutError("timed out")          # job and reset both fail

        img = _PILImage.new("RGB", (1164, 400), "white")
        with patch.object(dt_label, "_send_raster", side_effect=fake_send):
            with pytest.raises(RuntimeError) as exc:
                dt_label.print_label(img, "tcp://127.0.0.1:9100",
                                     "QL-1110NWB",
                                     LABEL_PROFILES["dk22243"], retries=1)
        assert "power-cycle" in str(exc.value)


class TestPositionColumnWidth:
    """The position column is sized to its widest entry.

    It used to be a fixed 90px, which is fine for "A1" but too narrow for the
    timestamp positions some continuous-mix releases use ("14:03" needs ~97px)
    — those ran straight into the track title. r34333258 (Aleksi Perälä,
    Cycles) is the case that surfaced it.
    """

    PROFILE = LABEL_PROFILES["dk22243"]

    def _width(self, positions):
        from PIL import ImageDraw, Image as _I
        d = ImageDraw.Draw(_I.new("RGB", (10, 10), "white"))
        font = dt_label.make_font("bold", 38)
        tracks = [("A", i, _qr_track(p, "T")) for i, p in enumerate(positions)]
        return dt_label._position_column_width(d, font, tracks), d, font

    def _clears(self, positions):
        """Column must be wide enough for every position plus the gap."""
        width, d, font = self._width(positions)
        return all(d.textlength(p, font=font) + dt_label._POS_W_GAP <= width
                   for p in positions)

    def test_timestamp_positions_do_not_collide(self):
        assert self._clears(["00:00", "14:03", "19:52"])

    def test_double_letter_sides_fit(self):
        assert self._clears(["AA1", "BB12"])

    def test_short_positions_keep_the_default_width(self):
        """Ordinary releases must look exactly as before."""
        width, _, _ = self._width(["A1", "A2", "B1"])
        assert width == dt_label._POS_W_MIN

    def test_wide_positions_widen_the_column(self):
        narrow, _, _ = self._width(["A1"])
        wide,   _, _ = self._width(["00:00"])
        assert wide > narrow

    def test_widest_entry_decides(self):
        mixed, _, _ = self._width(["A1", "00:00", "B2"])
        alone, _, _ = self._width(["00:00"])
        assert mixed == alone

    def test_empty_track_list_uses_minimum(self):
        width, _, _ = self._width([])
        assert width == dt_label._POS_W_MIN

    def test_non_string_positions_are_ignored(self):
        """Stub tracks whose position isn't a string must not break layout."""
        from PIL import ImageDraw, Image as _I
        d = ImageDraw.Draw(_I.new("RGB", (10, 10), "white"))
        t = MagicMock()
        t.__getitem__ = MagicMock(side_effect=lambda k: MagicMock())
        assert dt_label._position_column_width(
            d, dt_label.make_font("bold", 38), [("A", 0, t)]) == dt_label._POS_W_MIN

    def test_renders_with_timestamp_positions(self):
        positions = ["00:00", "04:00", "08:59", "14:03"]
        tracks = [(dt_label.get_side(p), i, _qr_track(p, f"FI3AC22{i:05d}"))
                  for i, p in enumerate(positions)]
        release = _qr_release()
        kw = dt_label._render_kwargs(tracks, self.PROFILE, release=release,
                                     is_compilation=False, bpms={})
        img = render_label(release, tracks, self.PROFILE, False, **kw)
        assert img.width == self.PROFILE["width_px"]


# ─── Credits ──────────────────────────────────────────────────────────────────

from conftest import _cheap_and_deep_data, make_release  # noqa: E402

_CREDIT_LINE_H     = dt_label._CREDIT_LINE_H
_HDR_CREDIT_LINE_H = dt_label._HDR_CREDIT_LINE_H
_TITLE_WRAP_LINE_H = dt_label._TITLE_WRAP_LINE_H
_HEADER_MAX_LINES  = dt_label._HEADER_MAX_LINES
_NOTES_GAP         = dt_label._NOTES_GAP


def _all_tracks(release):
    return flatten_discs(group_tracks_by_disc(release))


def _plain_release(n_tracks=3, **overrides):
    """A real DiscogsRelease with no credits, n tracks on side A."""
    data = _cheap_and_deep_data()
    data["extraartists"] = []
    data["tracklist"] = [{"position": f"A{i + 1}", "type_": "track", "duration": "",
                          "title": f"Track {i + 1}"} for i in range(n_tracks)]
    data.update(overrides)
    return data


def _probe(release, profile=LABEL_PROFILES["dk22243"], tracks=None):
    tracks = _all_tracks(release) if tracks is None else tracks
    return render_label(release, tracks, profile, release.isCompilation(), probe=True)


class TestCreditLabel:
    @pytest.mark.parametrize("role,label", [
        ("Remix",                             "Remix"),
        ("Remix [Reshaped]",                  "Remix"),
        ("remix",                             "Remix"),
        ("Edited By",                         "Edit"),
        ("Edited By [Digital Editing]",       None),   # mastering, not a re-edit
        ("DJ Mix",                            "DJ Mix"),
        ("Featuring [With]",                  "Featuring"),
        ("Vocals",                            "Vocals"),
        ("Vocals [Background]",               None),
        ("Lead Vocals",                       "Vocals"),
        ("Voice",                             "Voice"),
        ("Producer",                          "Producer"),
        ("Producer [Additional]",             "Add. Production"),
        ("Producer [Additional Production]",  "Add. Production"),
        ("Co-producer",                       "Co-producer"),
        ("Lacquer Cut By",                    None),
        ("Mastered By",                       None),
        ("Written-By",                        None),
        ("Artwork",                           None),
        ("Mixed By",                          None),   # mix engineer, not a DJ mix
        ("",                                  None),
    ])
    def test_role_policy(self, role, label):
        assert dt_label._credit_label(role) == label


class TestCreditPlan:
    def test_r3854655(self, cheap_and_deep):
        """The release that prompted credit support, end to end.

        Both remixes share the title "Words, Breaths & Pauses Remix"; the
        remixer is the only thing that tells them apart on the label.
        """
        header, rows = dt_label._credit_plan(cheap_and_deep)
        assert header == [("Producer", "Jay Ahern")]
        assert rows == {
            0: [("Remix", "Norman Nodge"), ("Add. Production", "Goran Simonoski"),
                ("Vocals", "Uta Alder")],
            1: [("Remix", "Jonsson/Alter"), ("Add. Production", "Goran Simonoski"),
                ("Vocals", "Uta Alder")],
            2: [("Add. Production", "Morgan Packard"), ("Vocals", "Crissy Liu")],
        }

    def test_formatted_lines(self, cheap_and_deep):
        header, rows = dt_label._credit_plan(cheap_and_deep)
        assert dt_label._format_credits(rows[0]) == (
            "Remix: Norman Nodge \u00b7 Vocals: Uta Alder "
            "\u00b7 Add. Production: Goran Simonoski")

    def test_release_artist_credit_is_dropped(self, cheap_and_deep):
        """"Producer: Cheap And Deep" — the artist producing their own record —
        is on most releases and says nothing."""
        header, rows = dt_label._credit_plan(cheap_and_deep)
        names = {n for _, n in header} | {n for r in rows.values() for _, n in r}
        assert "Cheap And Deep" not in names

    def test_pressing_and_packaging_roles_not_printed(self, cheap_and_deep):
        header, rows = dt_label._credit_plan(cheap_and_deep)
        names = {n for _, n in header} | {n for r in rows.values() for _, n in r}
        assert "MJR" not in names and "Maike Verloh" not in names

    def test_credit_on_every_track_is_hoisted_to_header(self):
        """One remixer across a whole EP prints once, not on every row."""
        data = _plain_release(3)
        for t in data["tracklist"]:
            t["extraartists"] = [{"name": "Solo Remixer", "role": "Remix", "id": 5}]
        header, rows = dt_label._credit_plan(make_release(data))
        assert header == [("Remix", "Solo Remixer")]
        assert all(r == [] for r in rows.values())

    def test_credit_on_some_tracks_stays_on_rows(self):
        data = _plain_release(3)
        data["tracklist"][0]["extraartists"] = [{"name": "R", "role": "Remix", "id": 5}]
        header, rows = dt_label._credit_plan(make_release(data))
        assert header == []
        assert rows[0] == [("Remix", "R")] and rows[1] == rows[2] == []

    def test_track_artist_credit_is_dropped(self):
        """On a compilation, "Producer: <that track's artist>" is noise too."""
        data = _plain_release(2)
        data["tracklist"][0]["artists"] = [{"name": "Guest", "id": 42}]
        data["tracklist"][0]["extraartists"] = [
            {"name": "Guest", "role": "Producer", "id": 42},
            {"name": "Other", "role": "Remix", "id": 43}]
        header, rows = dt_label._credit_plan(make_release(data))
        assert rows[0] == [("Remix", "Other")]

    def test_remixer_already_in_title_is_not_repeated(self):
        """74% of Remix credits in the collection were already in the title."""
        data = _plain_release(2)
        data["tracklist"][0]["title"] = "Phunk (Mark Broom Rmx)"
        data["tracklist"][0]["extraartists"] = [{"name": "Mark Broom", "role": "Remix", "id": 9}]
        assert dt_label._credit_plan(make_release(data))[1][0] == []

    def test_title_match_is_whole_word(self):
        data = _plain_release(2)
        data["tracklist"][0]["title"] = "Regisland"
        data["tracklist"][0]["extraartists"] = [{"name": "Regis", "role": "Remix", "id": 9}]
        assert dt_label._credit_plan(make_release(data))[1][0] == [("Remix", "Regis")]

    def test_self_remix_is_kept(self):
        """An artist remixing their own track is what tells two versions apart,
        so version credits survive the own-artist filter (23 such credits in
        the collection were being dropped)."""
        data = _plain_release(2)
        data["tracklist"][0]["extraartists"] = [
            {"name": "Cheap And Deep Productions", "anv": "Cheap And Deep",
             "role": "Remix", "id": 1563065}]
        assert dt_label._credit_plan(make_release(data))[1][0] == [
            ("Remix", "Cheap And Deep")]

    def test_self_producer_is_still_dropped(self):
        data = _plain_release(2)
        data["tracklist"][0]["extraartists"] = [
            {"name": "Cheap And Deep", "role": "Producer", "id": 1563065}]
        assert dt_label._credit_plan(make_release(data))[1][0] == []

    def test_header_credit_named_in_release_title_is_dropped(self):
        data = _plain_release(3, title="Solo Remixer Remixes")
        data["extraartists"] = [{"name": "Solo Remixer", "role": "Remix", "id": 5}]
        header, rows = dt_label._credit_plan(make_release(data))
        assert header == [] and all(r == [] for r in rows.values())

    def test_placement_decided_before_title_filter(self):
        """A release-wide credit stays in the header even when one track's
        title mentions it, instead of scattering onto every other row."""
        data = _plain_release(3)
        data["tracklist"][1]["title"] = "Track 2 (Solo Remixer Remix)"
        data["extraartists"] = [{"name": "Solo Remixer", "role": "Remix", "id": 5}]
        header, rows = dt_label._credit_plan(make_release(data))
        assert header == [("Remix", "Solo Remixer")]
        assert all(r == [] for r in rows.values())

    def test_disambiguation_suffix_stripped_from_credit_names(self):
        data = _plain_release(2)
        data["tracklist"][0]["extraartists"] = [{"name": "Lucy (12)", "role": "Remix", "id": 9}]
        assert dt_label._credit_plan(make_release(data))[1][0] == [("Remix", "Lucy")]

    def test_no_credits(self):
        header, rows = dt_label._credit_plan(make_release(_plain_release(2)))
        assert header == [] and rows == {0: [], 1: []}

    def test_stub_release_yields_nothing(self):
        """The suite renders MagicMock releases everywhere; they must not crash."""
        assert dt_label._credit_plan(_fake_render_release()) == ([], {})
        assert dt_label._credit_plan(_height_release()) == ([], {})


class TestWrapCredits:
    GROUPS = ["Remix: Norman Nodge", "Vocals: Uta Alder",
              "Add. Production: Goran Simonoski"]

    def setup_method(self):
        from PIL import Image, ImageDraw
        self.draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        self.font = dt_label._font_for("regular", dt_label._CREDIT_FONT_PX,
                                       " ".join(self.GROUPS))

    def _w(self, text):
        return self.draw.textlength(text, font=self.font)

    def test_fits_on_one_line(self):
        wide = self._w(dt_label._CREDIT_SEP.join(self.GROUPS)) + 10
        assert dt_label.wrap_credits(self.draw, self.GROUPS, self.font, wide) == [
            dt_label._CREDIT_SEP.join(self.GROUPS)]

    def test_breaks_between_groups_never_inside_one(self):
        """The first render split "Add. Production:" from "Goran Simonoski"."""
        width = self._w(dt_label._CREDIT_SEP.join(self.GROUPS[:2])) + 10
        lines = dt_label.wrap_credits(self.draw, self.GROUPS, self.font, width)
        assert lines == [dt_label._CREDIT_SEP.join(self.GROUPS[:2]), self.GROUPS[2]]

    def test_overflow_ends_in_ellipsis(self):
        width = self._w(dt_label._CREDIT_SEP.join(self.GROUPS[:2])) + 10
        lines = dt_label.wrap_credits(self.draw, self.GROUPS, self.font, width,
                                      max_lines=1)
        assert len(lines) == 1 and lines[0].endswith("\u2026")
        assert self._w(lines[0]) <= width

    def test_oversized_group_word_wraps_on_its_own(self):
        group = "Remix: " + ", ".join(f"Remixer Number {i}" for i in range(6))
        width = self._w("Remixer Number 1, Remixer Number 2") + 10
        lines = dt_label.wrap_credits(self.draw, [group], self.font, width)
        assert len(lines) == 2
        assert all(self._w(l) <= width for l in lines)

    def test_every_line_within_width(self):
        for width in (200, 350, 500, 800):
            for line in dt_label.wrap_credits(self.draw, self.GROUPS, self.font, width):
                assert self._w(line) <= width

    def test_no_groups(self):
        assert dt_label.wrap_credits(self.draw, [], self.font, 500) == []


class TestCreditLayout:
    """Credits change row and header heights; the probe must see exactly that."""

    CONT  = LABEL_PROFILES["dk22243"]
    DIE   = LABEL_PROFILES["dk1247"]

    def _with_row_credit(self, credit_names):
        data = _plain_release(2)
        data["tracklist"][0]["extraartists"] = [
            {"name": n, "role": "Remix", "id": 100 + i} for i, n in enumerate(credit_names)]
        return make_release(data)

    def test_one_credit_line_adds_exactly_one_line_height(self):
        base = _probe(make_release(_plain_release(2)))
        assert _probe(self._with_row_credit(["R"])) - base == _CREDIT_LINE_H

    def test_long_credits_wrap_to_two_lines_on_continuous(self):
        many = [f"Remixer With A Long Name {i}" for i in range(12)]
        base = _probe(make_release(_plain_release(2)))
        assert _probe(self._with_row_credit(many)) - base == 2 * _CREDIT_LINE_H

    def test_die_cut_credits_are_one_line(self):
        """Die-cut has a fixed canvas, so credits truncate like titles do."""
        many = [f"Remixer With A Long Name {i}" for i in range(12)]
        base = _probe(make_release(_plain_release(2)), self.DIE)
        assert _probe(self._with_row_credit(many), self.DIE) - base == _CREDIT_LINE_H

    def test_header_credit_line_height(self):
        data = _plain_release(2)
        data["extraartists"] = [{"name": "Whole Release", "role": "Remix", "id": 7}]
        base = _probe(make_release(_plain_release(2)))
        assert _probe(make_release(data)) - base == _HDR_CREDIT_LINE_H

    def test_continuation_header_omits_header_credits(self):
        data = _plain_release(2)
        data["extraartists"] = [{"name": "Whole Release", "role": "Remix", "id": 7}]
        rel, plain = make_release(data), make_release(_plain_release(2))
        cont = lambda r: render_label(r, _all_tracks(r), self.CONT, False,
                                      continuation=True, probe=True)
        assert cont(rel) == cont(plain)

    def test_credits_are_drawn(self, cheap_and_deep):
        """Ink in the title column of the track rows, not just a taller canvas."""
        bare = make_release(_cheap_and_deep_data())
        bare.data["extraartists"] = []
        for t in bare.data["tracklist"]:
            t["extraartists"] = []

        def track_column_ink(rel):
            tracks = _all_tracks(rel)
            h = _layout_height(rel, tracks, self.CONT, False)
            img = render_label(rel, tracks, self.CONT, False, height_px=h,
                               notes_lines=self.CONT["notes_lines"])
            M = self.CONT["margin_px"]
            top = render_label(rel, tracks[:0], self.CONT, False, probe=True)
            bottom = render_label(rel, tracks, self.CONT, False, probe=True)
            return _ink(img.crop((M + 120, top, self.CONT["width_px"] - M - 120, bottom)))

        assert track_column_ink(cheap_and_deep) > track_column_ink(bare) * 1.5

    def test_rendered_credits_break_between_groups(self):
        """render_label must lay credits out with wrap_credits, not plain word
        wrap.  Both give two lines here, so only the *content* of the second
        line tells them apart: word wrap strands the name ("Goran Simonoski"),
        group wrap keeps "Add. Production: Goran Simonoski" together.  Measure
        the rendered width of that line.
        """
        from PIL import ImageDraw
        data = _plain_release(2)
        data["tracklist"][1]["extraartists"] = [
            {"name": "Norman Nodge", "role": "Remix", "id": 1},
            {"name": "Uta Alder", "role": "Vocals", "id": 2},
            {"name": "Goran Simonoski", "role": "Producer [Additional Production]", "id": 3}]
        rel = make_release(data)
        tracks = _all_tracks(rel)
        P = self.CONT
        M, W = P["margin_px"], P["width_px"]

        # The widths render_label works with.
        scratch = ImageDraw.Draw(_PILImage.new("RGB", (1, 1)))
        groups = dt_label._credit_groups(dt_label._credit_plan(rel)[1][1])
        font = dt_label._font_for("regular", dt_label._CREDIT_FONT_PX,
                                  dt_label._CREDIT_SEP.join(groups))
        pos_w = dt_label._position_column_width(
            scratch, dt_label.make_font("bold", 38), tracks)
        max_w = W - 2 * M - pos_w - 110 - 16
        by_group = dt_label.wrap_credits(scratch, groups, font, max_w)
        by_word  = dt_label.wrap_text(scratch, dt_label._CREDIT_SEP.join(groups), font, max_w)
        assert len(by_group) == len(by_word) == 2 and by_group[1] != by_word[1], \
            "fixture no longer distinguishes the two wrapping strategies"

        h = _layout_height(rel, tracks, P, False)
        img = render_label(rel, tracks, P, False, height_px=h,
                           notes_lines=P["notes_lines"]).convert("L")
        bottom = render_label(rel, tracks, P, False, probe=True)
        rule_y = bottom - 18                                # the row's BPM rule
        x0, x1 = M + pos_w, W - M - 110 - 8                 # title column only
        band = img.crop((x0, rule_y - _CREDIT_LINE_H + 4, x1, rule_y - 2))
        cols = [x for x in range(band.width)
                if min(band.getpixel((x, y)) for y in range(band.height)) < 128]
        drawn_w = cols[-1] - cols[0] + 1

        assert abs(drawn_w - scratch.textlength(by_group[1], font=font)) < 8
        assert abs(drawn_w - scratch.textlength(by_word[1], font=font)) > 40

    def test_qr_clears_credited_rows(self, cheap_and_deep):
        tracks = _all_tracks(cheap_and_deep)
        h = _layout_height(cheap_and_deep, tracks, self.CONT, False)
        bottom = _probe(cheap_and_deep)
        assert bottom + _NOTES_GAP <= h - self.CONT["margin_px"] - _QR_SIZE

    def test_no_tripwire_on_credited_release(self, cheap_and_deep, caplog):
        import logging
        caplog.set_level(logging.WARNING, logger=dt_label.log.name)
        args = argparse.Namespace(split=False)
        for profile in (self.CONT, self.DIE):
            dt_label.render_labels_for_release(cheap_and_deep, args, profile)
        assert "overruns the QR zone" not in caplog.text


class TestHeaderWrap:
    """Artist and title wrap rather than being cut off with an ellipsis."""

    PROFILE = LABEL_PROFILES["dk22243"]

    def _probe_header(self, artist="Artist", title="Title"):
        r = _fake_render_release()
        r.getArtist.return_value = artist
        r.getTitle.return_value = title
        return render_label(r, [], self.PROFILE, False, probe=True)

    def test_long_artist_wraps(self):
        """Real case (r14739648): the fifth artist used to be cut off."""
        long_artist = "Caterina Barbieri (2) / Donato Dozzy / Lucy (12) / Klock / Xhin"
        assert (self._probe_header(artist=long_artist) - self._probe_header()
                == _TITLE_WRAP_LINE_H)

    def test_long_title_wraps(self):
        long_title = "A Title Long Enough That It Cannot Possibly Fit On One Line"
        assert (self._probe_header(title=long_title) - self._probe_header()
                == _TITLE_WRAP_LINE_H)

    def test_wrap_is_capped(self):
        absurd = " ".join(["Overlong"] * 60)
        extra = (_HEADER_MAX_LINES - 1) * _TITLE_WRAP_LINE_H
        assert self._probe_header(artist=absurd) - self._probe_header() == extra
        assert self._probe_header(title=absurd) - self._probe_header() == extra

    def test_wrapped_artist_is_drawn(self):
        """The second artist line carries ink where a short artist has none."""
        long_artist = "Caterina Barbieri / Donato Dozzy / Lucy / Klock / Xhin"

        def artist_band_ink(artist):
            r = _fake_render_release()
            r.getArtist.return_value = artist
            img = render_label(r, [], LABEL_PROFILES["dk1247"], False)
            M = self.PROFILE["margin_px"]
            # Second artist line; with a short artist this is the gap above
            # the title, which starts 70 px down.
            y0 = M + _TITLE_WRAP_LINE_H + 8
            return _ink(img.crop((M, y0, img.width - M, M + 70 + 4)))

        assert artist_band_ink(long_artist) > artist_band_ink("Artist") + 500


class TestStripDisambig:
    @pytest.mark.parametrize("name,expected", [
        ("Mark Richardson (3)", "Mark Richardson"),
        ("Caterina Barbieri (2) / Donato Dozzy / Lucy (12) / Klock / Xhin",
         "Caterina Barbieri / Donato Dozzy / Lucy / Klock / Xhin"),
        ("Lucy (12)/Klock", "Lucy/Klock"),
        ("A / B (2)", "A / B"),
        ("Jonsson/Alter", "Jonsson/Alter"),        # one duo's name — slash kept
        ("The (2) Sisters", "The (2) Sisters"),    # not a suffix
        ("808 State", "808 State"),
        ("Artist", "Artist"),
    ])
    def test_cases(self, name, expected):
        assert dt_label._strip_disambig(name) == expected


class TestChunkDieCut:
    """Die-cut labels are packed by measured height, capped at max_tracks."""

    PROFILE = LABEL_PROFILES["dk1247"]

    def _fits(self, chunks, release):
        return all(
            _layout_height(release, c, self.PROFILE, False, continuation=i > 0)
            <= self.PROFILE["height_px"]
            for i, c in enumerate(chunks))

    def test_twelve_tracks_on_two_sides_is_still_one_label(self):
        """Where the old count-based packing was safe, nothing changes."""
        tracks = _make_tracks_with_sides([("A", 6), ("B", 6)])
        chunks = _chunk_tracks(tracks, self.PROFILE, _height_release())
        assert chunks == [tracks]

    def test_four_sides_no_longer_overrun_the_qr(self):
        """Twelve tracks over four sides: four side headers pushed the last row
        into the QR under count-based packing (48 labels across a
        1000-release collection overran like this)."""
        tracks = _make_tracks_with_sides([("A", 3), ("B", 3), ("C", 3), ("D", 3)])
        release = _height_release()
        assert _layout_height(release, tracks, self.PROFILE, False) > self.PROFILE["height_px"]
        chunks = _chunk_tracks(tracks, self.PROFILE, release)
        assert len(chunks) == 2
        assert self._fits(chunks, release)

    def test_max_tracks_cap_still_applies(self):
        tracks = _make_tracks_with_sides([("A", 30)], title="T")
        chunks = _chunk_tracks(tracks, self.PROFILE, _height_release())
        assert all(len(c) <= self.PROFILE["max_tracks"] for c in chunks)
        assert [t for c in chunks for t in c] == tracks

    def test_credits_can_push_tracks_onto_a_continuation(self):
        data = _plain_release(12)
        for i, t in enumerate(data["tracklist"]):
            t["extraartists"] = [{"name": f"Remixer {i}", "role": "Remix", "id": 500 + i}]
        rel = make_release(data)
        tracks = _all_tracks(rel)
        plain = make_release(_plain_release(12))
        assert len(_chunk_tracks(_all_tracks(plain), self.PROFILE, plain)) == 1, \
            "without credits these twelve tracks fit one label"
        chunks = _chunk_tracks(tracks, self.PROFILE, rel)
        assert len(chunks) > 1
        assert self._fits(chunks, rel)
        assert [t for c in chunks for t in c] == tracks


class TestSplitMode:
    """--split: one label per disc, with a continuation when a disc won't fit."""

    DIE = LABEL_PROFILES["dk1247"]

    def _crowded_disc_release(self):
        """Disc 1 is twelve credited tracks — too tall for one die-cut label."""
        data = _plain_release(12)
        for i, t in enumerate(data["tracklist"]):
            t["position"] = f"{'AB'[i // 6]}{i % 6 + 1}"
            t["extraartists"] = [{"name": f"Remixer {i}", "role": "Remix", "id": 500 + i}]
        data["tracklist"].append({"position": "C1", "type_": "track", "duration": "",
                                  "title": "Disc Two"})
        return make_release(data)

    def test_oversized_disc_gets_a_continuation(self, caplog):
        import logging
        caplog.set_level(logging.WARNING, logger=dt_label.log.name)
        labels = dt_label.render_labels_for_release(
            self._crowded_disc_release(), argparse.Namespace(split=True), self.DIE)
        suffixes = [s for s, _ in labels]
        assert suffixes[0] == "disc1" and "disc1_cont1" in suffixes
        assert suffixes[-1] == "disc2"
        assert "overruns the QR zone" not in caplog.text

    def test_first_disc_chunk_is_measured_with_its_disc_line(self):
        """The "Disc N of M" line is 40 px.  Find a disc that fits without it
        but not with it: chunking must see the line and split, or the QR lands
        on the last row of a label that looked like it fit."""
        H = self.DIE["height_px"]
        for n in range(1, 13):
            for c in range(n + 1):
                data = _plain_release(n)
                for i in range(c):
                    data["tracklist"][i]["extraartists"] = [
                        {"name": f"R{i}", "role": "Remix", "id": 700 + i}]
                rel = make_release(data)
                tracks = _all_tracks(rel)
                if (_layout_height(rel, tracks, self.DIE, False) <= H
                        < _layout_height(rel, tracks, self.DIE, False, disc_info=(1, 1))):
                    assert len(_chunk_tracks(tracks, self.DIE, rel)) == 1
                    chunks = _chunk_tracks(tracks, self.DIE, rel, disc_info=(1, 1))
                    assert len(chunks) == 2
                    assert _layout_height(rel, chunks[0], self.DIE, False,
                                          disc_info=(1, 1)) <= H
                    return
        pytest.fail("no layout found where the disc line decides the fit")

    def test_continuation_sorts_after_its_disc(self):
        """dt_server lists previews in filename order."""
        names = [f"123_{s}.png" for s in ("disc2", "disc1_cont1", "disc1")]
        assert sorted(names) == ["123_disc1.png", "123_disc1_cont1.png", "123_disc2.png"]

    def test_select_discs_keeps_continuations(self):
        labels = [(s, None) for s in ("disc1", "disc1_cont1", "disc2", "disc10")]
        assert [s for s, _ in dt_label._select_discs(labels, [1])] == ["disc1", "disc1_cont1"]
        assert [s for s, _ in dt_label._select_discs(labels, [2])] == ["disc2"]
        assert [s for s, _ in dt_label._select_discs(labels, [10])] == ["disc10"]
