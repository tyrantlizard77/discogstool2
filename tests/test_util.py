"""Tests for util.py — filesystem helpers, collection CSV parsing."""

from __future__ import annotations

import csv
import os
import tempfile

import pytest

import util
from util import (
    CollectionInfo,
    file_extension,
    get_audio_files,
    parse_collection_xml,
    userfile,
)


# ─── userfile ─────────────────────────────────────────────────────────────────

class TestUserfile:
    def test_returns_string(self):
        result = userfile("test.txt")
        assert isinstance(result, str)

    def test_contains_filename(self):
        result = userfile("myfile.db")
        assert result.endswith("myfile.db")

    def test_under_discogstool_dir(self):
        result = userfile("x")
        assert ".discogstool" in result

    def test_different_names_differ(self):
        assert userfile("a.db") != userfile("b.db")

    def test_same_name_consistent(self):
        assert userfile("foo") == userfile("foo")


# ─── file_extension ───────────────────────────────────────────────────────────

class TestFileExtension:
    def test_mp3(self):
        assert file_extension("track.mp3") == "mp3"

    def test_uppercase_lowercased(self):
        assert file_extension("Track.MP3") == "mp3"

    def test_m4a(self):
        assert file_extension("file.m4a") == "m4a"

    def test_wav(self):
        assert file_extension("something.wav") == "wav"

    def test_dotfile(self):
        # os.path.splitext treats ".hidden" as a name with no extension
        assert file_extension(".hidden") == ""

    def test_path_with_directory(self):
        assert file_extension("/path/to/track.aiff") == "aiff"

    def test_double_extension(self):
        # Only the last extension is returned
        assert file_extension("archive.tar.gz") == "gz"


# ─── get_audio_files ──────────────────────────────────────────────────────────

class TestGetAudioFiles:
    def test_returns_empty_for_empty_dir(self, tmp_path):
        assert get_audio_files(str(tmp_path)) == []

    def test_finds_mp3(self, tmp_path):
        (tmp_path / "track.mp3").write_bytes(b"fake mp3")
        result = get_audio_files(str(tmp_path))
        assert len(result) == 1
        assert result[0].endswith("track.mp3")

    def test_finds_m4a(self, tmp_path):
        (tmp_path / "track.m4a").write_bytes(b"fake m4a")
        result = get_audio_files(str(tmp_path))
        assert len(result) == 1

    def test_finds_aiff(self, tmp_path):
        (tmp_path / "track.aiff").write_bytes(b"fake aiff")
        result = get_audio_files(str(tmp_path))
        assert len(result) == 1

    def test_ignores_non_audio(self, tmp_path):
        (tmp_path / "readme.txt").write_bytes(b"text")
        (tmp_path / "image.png").write_bytes(b"img")
        assert get_audio_files(str(tmp_path)) == []

    def test_ignores_hidden_files(self, tmp_path):
        (tmp_path / ".hidden.mp3").write_bytes(b"hidden")
        assert get_audio_files(str(tmp_path)) == []

    def test_recursive(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.mp3").write_bytes(b"deep")
        result = get_audio_files(str(tmp_path))
        assert len(result) == 1
        assert "deep.mp3" in result[0]

    def test_multiple_files(self, tmp_path):
        for name in ("a.mp3", "b.m4a", "c.aac"):
            (tmp_path / name).write_bytes(b"audio")
        result = get_audio_files(str(tmp_path))
        assert len(result) == 3

    def test_returns_absolute_paths(self, tmp_path):
        (tmp_path / "t.mp3").write_bytes(b"x")
        result = get_audio_files(str(tmp_path))
        assert os.path.isabs(result[0])


# ─── CollectionInfo ───────────────────────────────────────────────────────────

class TestCollectionInfo:
    def _make(self):
        return CollectionInfo(
            releaseid=12345,
            collection="Vinyl",
            date="2023-01-15",
            mcond="M",
            scond="VG+",
            notes="Great copy",
        )

    def test_attributes(self):
        ci = self._make()
        assert ci.releaseid == 12345
        assert ci.collection == "Vinyl"
        assert ci.date == "2023-01-15"
        assert ci.mcond == "M"
        assert ci.scond == "VG+"
        assert ci.notes == "Great copy"


# ─── parse_collection_xml ─────────────────────────────────────────────────────

def _write_collection_csv(path, rows):
    """Write a Discogs-style collection export CSV."""
    header = [
        "Catalog#", "Artist", "Title", "Label", "Format",
        "Rating", "Released", "release_id",
        "CollectionFolder", "Date Added",
        "Collection Media Condition", "Collection Sleeve Condition",
        "Collection Notes",
    ]
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)


class TestParseCollectionXml:
    def test_parses_single_row(self, tmp_path):
        csv_path = str(tmp_path / "collection.csv")
        _write_collection_csv(csv_path, [
            ["ABC-001", "Artist", "Title", "Label", "LP",
             "5", "2000", "11111",
             "All", "2023-01-01", "M", "VG+", "Good copy"],
        ])
        result = parse_collection_xml(csv_path)
        assert len(result) == 1
        ci = result[0]
        assert ci.releaseid == 11111
        assert ci.mcond == "M"
        assert ci.scond == "VG+"
        assert ci.notes == "Good copy"

    def test_parses_multiple_rows(self, tmp_path):
        csv_path = str(tmp_path / "collection.csv")
        rows = [
            ["A", "Art1", "T1", "L1", "LP", "5", "2001", str(i),
             "All", "2023-01-01", "M", "VG+", "note"] for i in [100, 200, 300]
        ]
        _write_collection_csv(csv_path, rows)
        result = parse_collection_xml(csv_path)
        assert len(result) == 3
        assert result[0].releaseid == 100
        assert result[1].releaseid == 200
        assert result[2].releaseid == 300

    def test_skips_header_row(self, tmp_path):
        csv_path = str(tmp_path / "collection.csv")
        _write_collection_csv(csv_path, [
            ["cat", "art", "ttl", "lbl", "LP", "4", "2005", "99999",
             "All", "2023-06-01", "VG", "VG", ""],
        ])
        result = parse_collection_xml(csv_path)
        # The header row is skipped; only one data row
        assert len(result) == 1

    def test_empty_notes(self, tmp_path):
        csv_path = str(tmp_path / "collection.csv")
        _write_collection_csv(csv_path, [
            ["X", "A", "T", "L", "LP", "0", "2010", "42",
             "All", "2023-01-01", "G+", "G", ""],
        ])
        result = parse_collection_xml(csv_path)
        assert result[0].notes == ""

    def test_collection_folder_stored(self, tmp_path):
        csv_path = str(tmp_path / "collection.csv")
        _write_collection_csv(csv_path, [
            ["X", "A", "T", "L", "LP", "0", "2010", "42",
             "Techno", "2023-01-01", "M", "M", ""],
        ])
        result = parse_collection_xml(csv_path)
        assert result[0].collection == "Techno"


# ─── Shared config helpers ────────────────────────────────────────────────────

class TestSaveKvConfig:
    def test_roundtrip(self, tmp_path):
        p = str(tmp_path / "cfg")
        util.save_kv_config(p, {"a": "1", "b": "two"})
        assert util.load_kv_config(p) == {"a": "1", "b": "two"}

    def test_header_written_as_comment(self, tmp_path):
        p = str(tmp_path / "cfg")
        util.save_kv_config(p, {"a": "1"}, header="hello")
        body = open(p).read()
        assert body.startswith("# hello\n")
        assert util.load_kv_config(p) == {"a": "1"}   # header not parsed as data

    def test_overwrites_existing(self, tmp_path):
        p = str(tmp_path / "cfg")
        util.save_kv_config(p, {"a": "1", "b": "2"})
        util.save_kv_config(p, {"a": "9"})
        assert util.load_kv_config(p) == {"a": "9"}

    def test_values_with_equals_survive_roundtrip(self, tmp_path):
        p = str(tmp_path / "cfg")
        util.save_kv_config(p, {"url": "http://h:8000/v1?k=v"})
        assert util.load_kv_config(p)["url"] == "http://h:8000/v1?k=v"


class TestResolveAnthropicKey:
    """Precedence: beatport_auth.json, then ANTHROPIC_API_KEY.

    This lookup previously existed in four places (dt_server twice, dt_find,
    beatport). These tests pin the order so the shared helper cannot drift.
    """

    def _auth(self, tmp_path, payload):
        d = tmp_path / ".discogstool"
        d.mkdir(exist_ok=True)
        (d / "beatport_auth.json").write_text(payload)
        return str(tmp_path)

    def test_reads_key_from_auth_file(self, tmp_path, monkeypatch):
        home = self._auth(tmp_path, '{"anthropic_api_key": "sk-from-file"}')
        monkeypatch.setenv("HOME", home)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert util.resolve_anthropic_key() == "sk-from-file"

    def test_auth_file_wins_over_env(self, tmp_path, monkeypatch):
        home = self._auth(tmp_path, '{"anthropic_api_key": "sk-from-file"}')
        monkeypatch.setenv("HOME", home)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
        assert util.resolve_anthropic_key() == "sk-from-file"

    def test_falls_back_to_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
        assert util.resolve_anthropic_key() == "sk-from-env"

    def test_empty_key_in_file_falls_through_to_env(self, tmp_path, monkeypatch):
        home = self._auth(tmp_path, '{"anthropic_api_key": ""}')
        monkeypatch.setenv("HOME", home)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
        assert util.resolve_anthropic_key() == "sk-from-env"

    def test_malformed_json_falls_through_to_env(self, tmp_path, monkeypatch):
        home = self._auth(tmp_path, "{not json")
        monkeypatch.setenv("HOME", home)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
        assert util.resolve_anthropic_key() == "sk-from-env"

    def test_none_when_nothing_configured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert util.resolve_anthropic_key() is None


class TestDataDirIsLazy:
    """Importing util must not touch the filesystem.

    datapath used to be a module constant computed at import, which both
    created ~/.discogstool as an import side effect and froze HOME before any
    test could redirect it.
    """

    def test_userfile_follows_home_at_call_time(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert util.userfile("x").startswith(str(tmp_path))

    def test_datadir_creates_on_demand(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        target = tmp_path / ".discogstool"
        assert not target.exists()
        util.userfile("anything")
        assert target.exists()

    def test_datadir_can_skip_creation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        util.datadir(create=False)
        assert not (tmp_path / ".discogstool").exists()

    def test_legacy_datapath_attribute_still_works(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert util.datapath == str(tmp_path / ".discogstool")

    def test_unknown_attribute_still_raises(self):
        with pytest.raises(AttributeError):
            util.no_such_attribute

    def test_import_alone_creates_nothing(self, tmp_path):
        """A fresh interpreter importing util must not create the directory."""
        import subprocess, sys as _sys, os as _os
        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        env = dict(_os.environ, HOME=str(tmp_path), PYTHONPATH=root)
        subprocess.run([_sys.executable, "-c", "import util"], env=env, check=True)
        assert not (tmp_path / ".discogstool").exists()


class TestSuiteIsolation:
    """The suite must never touch the real ~/.discogstool (see conftest.py)."""

    def test_datadir_is_not_the_real_home(self):
        from conftest import REAL_HOME, TEST_HOME
        path = util.datadir()
        assert path.startswith(TEST_HOME)
        assert not path.startswith(os.path.join(REAL_HOME, ".discogstool"))

    def test_beatport_log_handler_writes_to_the_test_home(self):
        """The leak that prompted this: BeatportMatcher() attached a handler to
        the real beatport.log on first use."""
        import logging.handlers
        import beatport
        from conftest import TEST_HOME
        beatport.BeatportMatcher(matchers=[], cache=beatport.BeatportCache(":memory:"))
        files = [h.baseFilename for h in beatport.log.handlers
                 if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert files and all(f.startswith(TEST_HOME) for f in files)
