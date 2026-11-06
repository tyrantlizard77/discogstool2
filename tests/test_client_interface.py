"""Tests for client_interface.py — Discogs API wrapper and utilities.

Covers:
  - scrub_data: recursive whitespace stripping for dicts, lists, strings
  - _normalize_artwork: PIL resize+JPEG conversion via in-memory bytes
  - get_user_auth_tokens / set_user_auth_tokens: auth file I/O
  - DiscogsRelease: metadata accessors tested via duck-typed fake data
  - DiscogsTrack: per-track accessors, artist fallback to release
  - split_roles / resolveTrackScope / getCredits: where Discogs keeps credits
    (track extraartists, release extraartists scoped by `tracks`) resolved
    to the tracks they belong to
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
    split_roles,
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
        """getYear() always returns the raw pressing year — safe for file tagging."""
        rel = _make_release()
        assert rel.getYear() == "2022"

    def test_getYear_unaffected_by_master(self):
        """getYear() must not include master annotation even when master_id present."""
        data = _make_release_data(year=2025, master_id=12345)
        rel = _make_release(data)
        # Patch getMasterYear so no network/DB call is made
        with patch.object(rel, "getMasterYear", return_value="2004"):
            assert rel.getYear() == "2025"

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


# ─── getMasterYear / getLabelYear ────────────────────────────────────────────

class TestGetMasterYear:
    """Unit tests for DiscogsRelease.getMasterYear() using a mocked DB."""

    def _make_rel_with_master(self, master_id, cached_master_data):
        """Build a release whose master data is already in the DB cache."""
        data = _make_release_data(year=2025, master_id=master_id)
        rel = _make_release(data)

        mock_db = MagicMock()
        mock_db.get.return_value = cached_master_data

        import client_interface as ci
        with patch.object(ci, "threadlocal") as mock_tl:
            # Provide a pre-seeded threadlocal.db so no real DB is constructed.
            mock_tl.db = mock_db
            result = rel.getMasterYear()
        return result

    def test_no_master_id_returns_none(self):
        rel = _make_release()   # no master_id in data
        assert rel.getMasterYear() is None

    def test_master_id_zero_returns_none(self):
        data = _make_release_data(master_id=0)
        rel = _make_release(data)
        assert rel.getMasterYear() is None

    def test_cached_master_with_year(self):
        result = self._make_rel_with_master(
            master_id=99,
            cached_master_data={"year": 2004, "title": "Original Album"},
        )
        assert result == "2004"

    def test_cached_master_year_zero_returns_none(self):
        result = self._make_rel_with_master(
            master_id=99,
            cached_master_data={"year": 0},
        )
        assert result is None

    def test_cached_master_missing_year_returns_none(self):
        result = self._make_rel_with_master(
            master_id=99,
            cached_master_data={"title": "No Year Here"},
        )
        assert result is None


class TestGetLabelYear:
    """Unit tests for DiscogsRelease.getLabelYear()."""

    def _rel_with_master_year(self, pressing_year, master_year_str):
        data = _make_release_data(year=pressing_year)
        rel = _make_release(data)
        with patch.object(rel, "getMasterYear", return_value=master_year_str):
            return rel.getLabelYear()

    def test_repress_shows_pressing_then_original(self):
        """Later repress: pressing=2025, master=2004 → '2025 (2004)'."""
        result = self._rel_with_master_year(2025, "2004")
        assert result == "2025 (2004)"

    def test_first_pressing_shows_plain_year(self):
        """Year matches master → no annotation."""
        result = self._rel_with_master_year(2004, "2004")
        assert result == "2004"

    def test_no_master_shows_pressing_year(self):
        """No master data → plain pressing year."""
        result = self._rel_with_master_year(2004, None)
        assert result == "2004"

    def test_no_pressing_year_falls_back_to_master(self):
        """Pressing year is 0/absent but master year known → show master year."""
        data = _make_release_data(year=0)
        rel = _make_release(data)
        with patch.object(rel, "getMasterYear", return_value="1998"):
            assert rel.getLabelYear() == "1998"

    def test_no_years_at_all_returns_empty(self):
        data = _make_release_data(year=0)
        rel = _make_release(data)
        with patch.object(rel, "getMasterYear", return_value=None):
            assert rel.getLabelYear() == ""


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


# ─── Credits ──────────────────────────────────────────────────────────────────

from conftest import make_release  # noqa: E402


class TestSplitRoles:
    def test_single_role(self):
        assert split_roles("Remix") == ["Remix"]

    def test_packed_roles_split(self):
        assert split_roles("Artwork, Design") == ["Artwork", "Design"]

    def test_comma_inside_qualifier_is_not_a_separator(self):
        assert split_roles("Remix [Remixed, Reshaped], Producer") == [
            "Remix [Remixed, Reshaped]", "Producer"]

    def test_empty(self):
        assert split_roles("") == []

    def test_stray_commas_and_whitespace(self):
        assert split_roles(" Remix ,, Vocals ") == ["Remix", "Vocals"]


def _positions(*positions):
    return make_release(_make_release_data(tracklist=[
        {"title": f"T{p}", "position": p, "type_": "track", "duration": ""}
        for p in positions]))


class TestResolveTrackScope:
    """Every `tracks` shape seen across a 1000-release collection."""

    REL = staticmethod(lambda: _positions("A1", "A2", "B1", "B2", "C1", "C2", "C3", "D1"))

    @pytest.mark.parametrize("spec,expected", [
        ("A1",               {0}),
        ("A1, B1",           {0, 2}),
        ("C2 & C3",          {5, 6}),
        ("A1 to B1",         {0, 1, 2}),
        ("B",                {2, 3}),              # bare side
        ("A to B1",          {0, 1, 2}),           # side as range start
        ("C1 to D",          {4, 5, 6, 7}),        # side as range end
        ("A to B2, C2, D",   {0, 1, 2, 3, 5, 7}),
        ("All Music",        set(range(8))),
        ("All except D1",    set(range(7))),
        ("All tracks except A1, A2", set(range(2, 8))),
        ("a1, b2",           {0, 3}),              # case-insensitive
    ])
    def test_shapes(self, spec, expected):
        assert self.REL().resolveTrackScope(spec) == expected

    def test_empty_means_whole_release(self):
        assert self.REL().resolveTrackScope("") is None
        assert self.REL().resolveTrackScope("   ") is None

    def test_unresolvable_tokens_ignored_not_guessed(self):
        """'C3 outro' names no position: it contributes nothing, and a credit
        is never attached to a track it can't be tied to."""
        assert self.REL().resolveTrackScope("C2 & C3 outro") == {5}
        assert self.REL().resolveTrackScope("Z9") == set()

    def test_backwards_range_is_empty(self):
        assert self.REL().resolveTrackScope("B2 to A1") == set()


class TestGetCredits:
    def test_track_credits_come_first(self, cheap_and_deep):
        a1 = cheap_and_deep.getTrack(0).getCredits()
        assert a1[0] == {"role": "Remix", "name": "Norman Nodge", "artist_id": 845344}

    def test_scoped_release_credit_only_on_its_tracks(self, cheap_and_deep):
        """Uta Alder is credited on "A1, B1" — not on B2."""
        names = [[c["name"] for c in cheap_and_deep.getTrack(i).getCredits()]
                 for i in range(3)]
        assert "Uta Alder" in names[0]
        assert "Uta Alder" in names[1]
        assert "Uta Alder" not in names[2]

    def test_unscoped_release_credit_on_every_track(self, cheap_and_deep):
        for i in range(3):
            assert any(c["name"] == "Jay Ahern"
                       for c in cheap_and_deep.getTrack(i).getCredits())

    def test_packed_role_becomes_one_credit_per_role(self, cheap_and_deep):
        roles = [c["role"] for c in cheap_and_deep.getTrack(0).getCredits()
                 if c["name"] == "Maike Verloh"]
        assert roles == ["Artwork", "Design"]

    def test_anv_preferred_over_canonical_name(self, cheap_and_deep):
        names = [c["name"] for c in cheap_and_deep.getTrack(0).getCredits()]
        assert "MJR" in names and "Mark Richardson (3)" not in names

    def test_same_credit_recorded_twice_is_deduplicated(self):
        data = _make_release_data(
            extraartists=[{"name": "X", "role": "Remix", "tracks": "A1", "id": 7}])
        data["tracklist"][0]["extraartists"] = [
            {"name": "X", "role": "Remix", "tracks": "", "id": 7}]
        credits = make_release(data).getTrack(0).getCredits()
        assert credits == [{"role": "Remix", "name": "X", "artist_id": 7}]

    def test_nameless_entry_skipped(self):
        data = _make_release_data(extraartists=[{"name": "", "role": "Remix", "tracks": ""}])
        assert make_release(data).getTrack(0).getCredits() == []

    def test_no_credits_anywhere(self):
        assert make_release(_make_release_data()).getTrack(0).getCredits() == []

    def test_unlinked_artist_id_is_zero(self):
        data = _make_release_data(extraartists=[{"name": "Anon", "role": "Remix", "tracks": ""}])
        assert make_release(data).getTrack(0).getCredits()[0]["artist_id"] == 0


class TestGetTrackCredits:
    """Only what Discogs lists under the track — what labels print."""

    def test_track_credits_only(self, cheap_and_deep):
        assert [c["name"] for c in cheap_and_deep.getTrack(0).getTrackCredits()] == [
            "Norman Nodge"]

    def test_scoped_release_credits_excluded(self, cheap_and_deep):
        """Uta Alder is a release credit scoped to "A1, B1": getCredits()
        includes her on A1, getTrackCredits() does not."""
        a1 = cheap_and_deep.getTrack(0)
        assert any(c["name"] == "Uta Alder" for c in a1.getCredits())
        assert not any(c["name"] == "Uta Alder" for c in a1.getTrackCredits())

    def test_getCredits_starts_with_track_credits(self, cheap_and_deep):
        b2 = cheap_and_deep.getTrack(2)
        assert b2.getCredits()[:len(b2.getTrackCredits())] == b2.getTrackCredits()

    def test_no_track_credits(self):
        assert make_release(_make_release_data()).getTrack(0).getTrackCredits() == []


class TestEmptyLists:
    """r3060397 (Far – Water & Solutions) has `labels: []`, and getLabel()
    used to raise IndexError, which crashed dt_label outright."""

    def test_empty_labels(self):
        rel = make_release(_make_release_data(labels=[]))
        assert rel.getLabel() == ""
        assert rel.getCatno() == ""

    def test_empty_artists(self):
        assert make_release(_make_release_data(artists=[])).getArtist() == ""

    def test_missing_key(self):
        data = _make_release_data()
        del data["labels"]
        assert make_release(data).getLabel() == ""

    def test_untitled_release_synthesises_from_empty_catno(self):
        """getTitle() falls back to the catno for "Untitled" releases."""
        rel = make_release(_make_release_data(labels=[], title="Untitled"))
        assert rel.getTitle() == ""


class TestMissingFields:
    """Discogs sends null for fields it has nothing for; they must read as "".

    `country` is null on ~1% of releases and used to print as "None" on the
    label.  `year` uses 0 for "unknown" and used to come back as "0".
    """

    def test_null_country(self):
        assert make_release(_make_release_data(country=None)).getCountry() == ""

    def test_missing_country(self):
        data = _make_release_data(); del data["country"]
        assert make_release(data).getCountry() == ""

    @pytest.mark.parametrize("year", [0, None, "0"[:0]])
    def test_unknown_year_is_blank(self, year):
        assert make_release(_make_release_data(year=year)).getYear() == ""

    def test_missing_year(self):
        data = _make_release_data(); del data["year"]
        assert make_release(data).getYear() == ""

    def test_real_year_unchanged(self):
        assert make_release(_make_release_data(year=1998)).getYear() == "1998"

    def test_null_genre_lists(self):
        rel = make_release(_make_release_data(styles=None, genres=None))
        assert rel.getGenre() == ""

    def test_null_entries_inside_genre_list(self):
        rel = make_release(_make_release_data(styles=["Techno", None, ""]))
        assert rel.getGenre() == "Techno"

    def test_styles_all_blank_falls_back_to_genres(self):
        rel = make_release(_make_release_data(styles=[None], genres=["Electronic"]))
        assert rel.getGenre() == "Electronic"

    def test_null_title(self):
        assert make_release(_make_release_data(title=None)).getTitle() == ""

    def test_null_catno_skipped_not_printed(self):
        rel = make_release(_make_release_data(labels=[
            {"name": "A", "catno": None}, {"name": "B", "catno": "X1"}]))
        assert rel.getCatno() == "X1"
        assert rel.getLabel() == "A / B"

    def test_blank_entry_leaves_no_dangling_separator(self):
        """Characterization: this used to give "A /"."""
        rel = make_release(_make_release_data(labels=[
            {"name": "A", "catno": "X1"}, {"name": "B", "catno": ""}]))
        assert rel.getCatno() == "X1"

    def test_null_anv_falls_back_to_name(self):
        rel = make_release(_make_release_data(artists=[{"name": "Real", "anv": None}]))
        assert rel.getArtist() == "Real"

    def test_null_track_fields(self):
        data = _make_release_data(tracklist=[
            {"title": None, "position": None, "type_": "track", "duration": None,
             "artists": [{"name": None}, {"name": "B"}]}])
        t = make_release(data).getTrack(0)
        assert (t.getTitle(), t.getPosition(), t.getDuration(), t.getArtist()) == ("", "", "", "B")

    def test_null_role_and_position_in_credits(self):
        data = _make_release_data(extraartists=[{"name": "X", "role": None, "tracks": "A1"}])
        data["tracklist"][0]["extraartists"] = [{"name": "Y", "role": None}]
        data["tracklist"][1]["position"] = None
        rel = make_release(data)
        assert rel.getTrack(0).getCredits() == []      # no role → no credit, no crash
        assert rel.resolveTrackScope("A1") == {0}

    def test_none_never_appears_as_text(self):
        """Every accessor, on a release that is null wherever Discogs allows."""
        data = _make_release_data(country=None, year=None, styles=None, genres=None,
                                  labels=[{"name": None, "catno": None}],
                                  artists=[{"name": "A", "anv": None}])
        rel = make_release(data)
        for value in (rel.getCountry(), rel.getYear(), rel.getGenre(), rel.getLabel(),
                      rel.getCatno(), rel.getArtist(), rel.getLabelYear(), rel.getTitle()):
            assert "None" not in value


class TestGetFormats:
    def test_names_in_order(self):
        rel = make_release(_make_release_data(formats=[
            {"name": "Box Set", "qty": "1"}, {"name": "Vinyl", "qty": "3"},
            {"name": "CD", "qty": "1"}]))
        assert rel.getFormats() == ["Box Set", "Vinyl", "CD"]

    def test_missing_or_null(self):
        assert make_release(_make_release_data()).getFormats() == []
        assert make_release(_make_release_data(formats=None)).getFormats() == []
        assert make_release(_make_release_data(formats=[{"name": None}])).getFormats() == []


class TestArtistIds:
    def test_release_artist_ids(self, cheap_and_deep):
        assert cheap_and_deep.getArtistIds() == {1563065}

    def test_track_inherits_release_artists(self, cheap_and_deep):
        assert cheap_and_deep.getTrack(0).getArtistIds() == {1563065}

    def test_track_with_own_artists(self):
        data = _make_release_data()
        data["tracklist"][0]["artists"] = [{"name": "Guest", "id": 42}]
        assert make_release(data).getTrack(0).getArtistIds() == {42}

    def test_unlinked_artists_omitted(self):
        data = _make_release_data(artists=[{"name": "No Id"}])
        assert make_release(data).getArtistIds() == set()
