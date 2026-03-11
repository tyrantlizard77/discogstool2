"""Tests for client_interface.py — Discogs API wrapper and utilities.

Covers:
  - scrub_data: recursive whitespace stripping for dicts, lists, strings
  - _normalize_artwork: PIL resize+JPEG conversion via in-memory bytes
  - get_user_auth_tokens / set_user_auth_tokens: auth file I/O
  - DiscogsRelease: metadata accessors tested via duck-typed fake data
  - DiscogsTrack: per-track accessors, artist fallback to release
"""

from __future__ import annotations

import io
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

import client_interface
from client_interface import (
    DiscogsRelease,
    DiscogsTrack,
    scrub_data,
    _normalize_artwork,
    get_user_auth_tokens,
    set_user_auth_tokens,
)


# ─── scrub_data ───────────────────────────────────────────────────────────────

class TestScrubData:
    def test_string_stripped(self):
        assert scrub_data("  hello  ") == "hello"

    def test_string_no_change(self):
        assert scrub_data("hello") == "hello"

    def test_empty_string(self):
        assert scrub_data("") == ""

    def test_dict_values_stripped(self):
        result = scrub_data({"key": "  value  "})
        assert result == {"key": "value"}

    def test_nested_dict(self):
        result = scrub_data({"outer": {"inner": "  text  "}})
        assert result["outer"]["inner"] == "text"

    def test_list_items_stripped(self):
        result = scrub_data(["  a  ", "  b  "])
        assert result == ["a", "b"]

    def test_mixed_nested(self):
        data = {
            "artists": [{"name": "  Artist Name  ", "anv": "  "}],
            "title": "  Release Title  ",
            "year": 2020,
        }
        result = scrub_data(data)
        assert result["artists"][0]["name"] == "Artist Name"
        assert result["artists"][0]["anv"] == ""
        assert result["title"] == "Release Title"
        assert result["year"] == 2020  # non-string unchanged

    def test_non_string_passthrough(self):
        assert scrub_data(42) == 42
        assert scrub_data(3.14) == pytest.approx(3.14)
        assert scrub_data(None) is None
        assert scrub_data(True) is True

    def test_empty_dict(self):
        assert scrub_data({}) == {}

    def test_empty_list(self):
        assert scrub_data([]) == []


# ─── _normalize_artwork ───────────────────────────────────────────────────────

def _make_jpeg_bytes(width, height, mode="RGB"):
    """Create a minimal JPEG image as bytes."""
    img = Image.new(mode, (width, height), color=(128, 64, 32) if mode == "RGB" else 128)
    buf = io.BytesIO()
    if mode != "RGB":
        img = img.convert("RGB")
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _make_png_bytes(width, height, mode="RGB"):
    """Create a minimal PNG image as bytes."""
    img = Image.new(mode, (width, height), color=(200, 100, 50) if mode == "RGB" else 200)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestNormalizeArtwork:
    def test_returns_bytes(self):
        imgdata = _make_jpeg_bytes(100, 100)
        result = _normalize_artwork(imgdata)
        assert isinstance(result, bytes)

    def test_output_is_valid_jpeg(self):
        imgdata = _make_jpeg_bytes(100, 100)
        result = _normalize_artwork(imgdata)
        # Should parse as a JPEG
        img = Image.open(io.BytesIO(result))
        assert img.format == "JPEG"

    def test_small_image_unchanged_size(self):
        imgdata = _make_jpeg_bytes(100, 100)
        result = _normalize_artwork(imgdata)
        img = Image.open(io.BytesIO(result))
        assert img.width == 100
        assert img.height == 100

    def test_large_image_downscaled(self):
        imgdata = _make_jpeg_bytes(1600, 1200)
        result = _normalize_artwork(imgdata)
        img = Image.open(io.BytesIO(result))
        assert img.width <= 800
        assert img.height <= 800

    def test_square_large_image_fits_within_800(self):
        imgdata = _make_jpeg_bytes(2000, 2000)
        result = _normalize_artwork(imgdata)
        img = Image.open(io.BytesIO(result))
        assert img.width <= 800
        assert img.height <= 800

    def test_aspect_ratio_preserved_on_resize(self):
        # Wide image: 1600x800 → should stay at 2:1 ratio
        imgdata = _make_jpeg_bytes(1600, 800)
        result = _normalize_artwork(imgdata)
        img = Image.open(io.BytesIO(result))
        assert img.width == 800
        assert img.height == 400

    def test_png_converted_to_jpeg(self):
        imgdata = _make_png_bytes(100, 100)
        result = _normalize_artwork(imgdata)
        img = Image.open(io.BytesIO(result))
        assert img.format == "JPEG"

    def test_rgba_converted_to_rgb_jpeg(self):
        """RGBA (PNG with transparency) must be converted before JPEG save."""
        img = Image.new("RGBA", (100, 100), (100, 150, 200, 128))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        imgdata = buf.getvalue()
        # Should not raise
        result = _normalize_artwork(imgdata)
        out_img = Image.open(io.BytesIO(result))
        assert out_img.mode == "RGB"

    def test_greyscale_converted_to_rgb(self):
        imgdata = _make_jpeg_bytes(100, 100, mode="L")
        result = _normalize_artwork(imgdata)
        out_img = Image.open(io.BytesIO(result))
        # _normalize_artwork converts L→RGB
        assert out_img.mode == "RGB"

    def test_exact_800_not_resized(self):
        imgdata = _make_jpeg_bytes(800, 800)
        result = _normalize_artwork(imgdata)
        img = Image.open(io.BytesIO(result))
        assert img.width == 800
        assert img.height == 800


# ─── get_user_auth_tokens / set_user_auth_tokens ─────────────────────────────

class TestAuthTokens:
    def test_get_returns_none_when_no_file(self, tmp_path):
        fake_path = str(tmp_path / "discogs_auth")
        with patch.object(client_interface, "discogs_auth", fake_path):
            token, secret = get_user_auth_tokens()
        assert token is None
        assert secret is None

    def test_set_and_get_roundtrip(self, tmp_path):
        fake_path = str(tmp_path / "discogs_auth")
        with patch.object(client_interface, "discogs_auth", fake_path):
            set_user_auth_tokens("mytoken", "mysecret")
            token, secret = get_user_auth_tokens()
        assert token == "mytoken"
        assert secret == "mysecret"

    def test_get_returns_strings(self, tmp_path):
        fake_path = str(tmp_path / "discogs_auth")
        with patch.object(client_interface, "discogs_auth", fake_path):
            set_user_auth_tokens("tok123", "sec456")
            token, secret = get_user_auth_tokens()
        assert isinstance(token, str)
        assert isinstance(secret, str)

    def test_malformed_file_returns_none_and_removes_file(self, tmp_path):
        fake_path = str(tmp_path / "discogs_auth")
        # Write a file without the '|' separator
        with open(fake_path, "w") as f:
            f.write("badcontent")
        with patch.object(client_interface, "discogs_auth", fake_path):
            token, secret = get_user_auth_tokens()
        assert token is None
        assert secret is None
        # File should be removed
        assert not os.path.exists(fake_path)


# ─── DiscogsRelease ───────────────────────────────────────────────────────────

def _make_release_data(**overrides):
    """Build a minimal release data dict for testing without real API calls."""
    data = {
        "title": "Test Album",
        "year": 2022,
        "country": "US",
        "artists": [{"name": "Test Artist", "anv": ""}],
        "labels": [{"name": "Test Label", "catno": "TL-001"}],
        "styles": ["Techno", "Acid"],
        "genres": ["Electronic"],
        "tracklist": [
            {"title": "Track One", "position": "A1", "type_": "track",
             "duration": "5:30"},
            {"title": "Track Two", "position": "A2", "type_": "track",
             "duration": "6:00"},
            {"title": "Track Three", "position": "B1", "type_": "track",
             "duration": "4:45"},
        ],
    }
    data.update(overrides)
    return data


def _make_release(data=None):
    """Build a DiscogsRelease bypassing __init__ (no API call needed)."""
    if data is None:
        data = _make_release_data()
    rel = DiscogsRelease.__new__(DiscogsRelease)
    rel.rid = 99999
    rel.data = data
    rel.data["tracklist"] = [
        t for t in data["tracklist"]
        if t["type_"] == "track" and t["title"] != ""
    ]
    rel.totaltracks = len(rel.data["tracklist"])
    rel.imgdata = None
    return rel


class TestDiscogsRelease:
    def test_getTitle(self):
        rel = _make_release()
        assert rel.getTitle() == "Test Album"

    def test_getTitle_untitled_falls_back_to_catno(self):
        data = _make_release_data(title="Untitled")
        rel = _make_release(data)
        assert rel.getTitle() == "TL-001"

    def test_getArtist(self):
        rel = _make_release()
        assert rel.getArtist() == "Test Artist"

    def test_getArtist_anv_preferred(self):
        data = _make_release_data(
            artists=[{"name": "Real Name", "anv": "Stage Name"}]
        )
        rel = _make_release(data)
        assert rel.getArtist() == "Stage Name"

    def test_getArtist_multiple_joined(self):
        data = _make_release_data(
            artists=[
                {"name": "Artist A", "anv": ""},
                {"name": "Artist B", "anv": ""},
            ]
        )
        rel = _make_release(data)
        assert "Artist A" in rel.getArtist()
        assert "Artist B" in rel.getArtist()

    def test_getLabel(self):
        rel = _make_release()
        assert rel.getLabel() == "Test Label"

    def test_getCatno(self):
        rel = _make_release()
        assert rel.getCatno() == "TL-001"

    def test_getYear(self):
        rel = _make_release()
        assert rel.getYear() == "2022"

    def test_getCountry(self):
        rel = _make_release()
        assert rel.getCountry() == "US"

    def test_getCountry_missing_returns_empty(self):
        data = _make_release_data()
        del data["country"]
        rel = _make_release(data)
        assert rel.getCountry() == ""

    def test_getGenre_uses_styles(self):
        rel = _make_release()
        g = rel.getGenre()
        assert "Techno" in g
        assert "Acid" in g

    def test_getGenre_falls_back_to_genres(self):
        data = _make_release_data(styles=[])
        rel = _make_release(data)
        assert "Electronic" in rel.getGenre()

    def test_getGenre_empty_when_none(self):
        data = _make_release_data(styles=[], genres=[])
        rel = _make_release(data)
        assert rel.getGenre() == ""

    def test_getTotalTracks(self):
        rel = _make_release()
        assert rel.getTotalTracks() == 3

    def test_getId(self):
        rel = _make_release()
        assert rel.getId() == 99999

    def test_getTrack_returns_DiscogsTrack(self):
        rel = _make_release()
        track = rel.getTrack(0)
        assert isinstance(track, DiscogsTrack)

    def test_isCompilation_single_artist_false(self):
        rel = _make_release()
        assert rel.isCompilation() is False

    def test_isCompilation_multiple_artists_true(self):
        data = _make_release_data(
            tracklist=[
                {"title": "T1", "position": "A1", "type_": "track", "duration": "",
                 "artists": [{"name": "Artist X"}]},
                {"title": "T2", "position": "A2", "type_": "track", "duration": "",
                 "artists": [{"name": "Artist Y"}]},
            ]
        )
        rel = _make_release(data)
        assert rel.isCompilation() is True

    def test_tracklist_filters_non_tracks(self):
        """Headings (type_='heading') should be filtered out."""
        data = _make_release_data(
            tracklist=[
                {"title": "Side A", "position": "", "type_": "heading", "duration": ""},
                {"title": "Track One", "position": "A1", "type_": "track", "duration": "5:00"},
            ]
        )
        rel = _make_release(data)
        assert rel.getTotalTracks() == 1

    def test_tracklist_filters_empty_title(self):
        data = _make_release_data(
            tracklist=[
                {"title": "", "position": "A1", "type_": "track", "duration": ""},
                {"title": "Real Track", "position": "A2", "type_": "track", "duration": "4:00"},
            ]
        )
        rel = _make_release(data)
        assert rel.getTotalTracks() == 1

    def test_repr(self):
        rel = _make_release()
        assert "99999" in repr(rel)

    def test_str(self):
        rel = _make_release()
        s = str(rel)
        assert "Test Artist" in s
        assert "Test Album" in s


# ─── DiscogsTrack ─────────────────────────────────────────────────────────────

class TestDiscogsTrack:
    def _make_track(self, index=0, data=None):
        rel = _make_release(data)
        return rel.getTrack(index)

    def test_getTitle(self):
        t = self._make_track(0)
        assert t.getTitle() == "Track One"

    def test_getTitle_untitled_includes_position(self):
        data = _make_release_data(
            tracklist=[
                {"title": "Untitled", "position": "A1", "type_": "track", "duration": ""}
            ]
        )
        t = self._make_track(0, data)
        # Falls back to "<release_title> <position>"
        assert "A1" in t.getTitle()

    def test_getTrackNumber(self):
        t = self._make_track(0)
        assert t.getTrackNumber() == 1
        t2 = self._make_track(2)
        assert t2.getTrackNumber() == 3

    def test_getArtist_falls_back_to_release(self):
        """When track has no per-track artists, returns release artist."""
        t = self._make_track(0)
        assert t.getArtist() == "Test Artist"

    def test_getArtist_uses_track_artists_when_present(self):
        data = _make_release_data(
            tracklist=[
                {"title": "Collab", "position": "A1", "type_": "track", "duration": "",
                 "artists": [{"name": "DJ One"}, {"name": "MC Two"}]},
            ]
        )
        t = self._make_track(0, data)
        artist = t.getArtist()
        assert "DJ One" in artist
        assert "MC Two" in artist

    def test_getDuration(self):
        t = self._make_track(0)
        assert t.getDuration() == "5:30"

    def test_getDuration_missing_returns_empty(self):
        data = _make_release_data(
            tracklist=[
                {"title": "T", "position": "A1", "type_": "track"}   # no 'duration' key
            ]
        )
        t = self._make_track(0, data)
        assert t.getDuration() == ""

    def test_getRelease(self):
        rel = _make_release()
        t = rel.getTrack(0)
        assert t.getRelease() is rel

    def test_repr(self):
        t = self._make_track(0)
        r = repr(t)
        assert "99999" in r
        assert "0" in r

    def test_str(self):
        t = self._make_track(0)
        s = str(t)
        assert "Track One" in s
        assert "A1" in s

    def test_index_out_of_range_raises(self):
        rel = _make_release()
        with pytest.raises(client_interface.ClientException):
            rel.getTrack(100)

    def test_getitem(self):
        t = self._make_track(0)
        assert t["title"] == "Track One"
        assert t["position"] == "A1"
