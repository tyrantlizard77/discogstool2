"""Tests for database.py — SQLite caching layer for Discogs API responses.

Covers:
  - data2blob / blob2data: pickle round-trip through sqlite3.Binary
  - get_ts: returns a valid ISO date string for today
  - ts_age: computes correct day delta
  - DiscogsDatabase: get/put/delete, put_posted/get_posted/get_last_posted
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

import pytest

import database
from database import (
    DiscogsDatabase,
    blob2data,
    data2blob,
    get_ts,
    ts_age,
)


# ─── data2blob / blob2data ────────────────────────────────────────────────────

class TestBlobSerialization:
    def test_roundtrip_dict(self):
        original = {"key": "value", "number": 42, "nested": {"a": [1, 2, 3]}}
        blob = data2blob(original)
        assert blob2data(blob) == original

    def test_roundtrip_list(self):
        original = [1, "two", 3.0, None, True]
        assert blob2data(data2blob(original)) == original

    def test_roundtrip_none(self):
        assert blob2data(data2blob(None)) is None

    def test_roundtrip_string(self):
        s = "hello \U0001f600"
        assert blob2data(data2blob(s)) == s

    def test_blob_is_bytes_like(self):
        """sqlite3.Binary wraps bytes; it should be accepted by sqlite3 as a BLOB."""
        blob = data2blob({"x": 1})
        # sqlite3.Binary is a memoryview or bytes subclass depending on Python version
        assert hasattr(blob, "__bytes__") or isinstance(blob, (bytes, memoryview))


# ─── get_ts ───────────────────────────────────────────────────────────────────

class TestGetTs:
    def test_returns_string(self):
        ts = get_ts()
        assert isinstance(ts, str)

    def test_is_valid_iso_date(self):
        ts = get_ts()
        # Should parse without error
        datetime.date.fromisoformat(ts)

    def test_is_today(self):
        ts = get_ts()
        assert ts == str(datetime.date.today())


# ─── ts_age ───────────────────────────────────────────────────────────────────

class TestTsAge:
    def test_today_is_zero(self):
        today = str(datetime.date.today())
        assert ts_age(today) == 0

    def test_yesterday_is_one(self):
        yesterday = str(datetime.date.today() - datetime.timedelta(days=1))
        assert ts_age(yesterday) == 1

    def test_old_date(self):
        old = "2020-01-01"
        age = ts_age(old)
        assert age > 365 * 4   # at least 4 years ago from now

    def test_one_week_ago(self):
        week_ago = str(datetime.date.today() - datetime.timedelta(days=7))
        assert ts_age(week_ago) == 7


# ─── DiscogsDatabase ─────────────────────────────────────────────────────────

def _make_db(tmp_path):
    """Return a DiscogsDatabase backed by a temp file (not ~/.discogstool)."""
    db_path = str(tmp_path / "test_discogs.db")
    with patch.object(database.util, "userfile", return_value=db_path):
        db = DiscogsDatabase()
    return db


class TestDiscogsDatabaseGetPut:
    def test_get_missing_returns_none(self, tmp_path):
        db = _make_db(tmp_path)
        assert db.get("missing-key") is None

    def test_put_and_get_roundtrip(self, tmp_path):
        db = _make_db(tmp_path)
        data = {"tracklist": [{"title": "Track A", "type_": "track"}]}
        db.put("release-1", data)
        result = db.get("release-1")
        assert result == data

    def test_put_complex_data(self, tmp_path):
        db = _make_db(tmp_path)
        data = {
            "artists": [{"name": "Artist", "anv": ""}],
            "labels": [{"name": "Label", "catno": "LAB001"}],
            "year": 2020,
            "tracklist": [],
        }
        db.put("release-complex", data)
        assert db.get("release-complex") == data

    def test_delete_removes_entry(self, tmp_path):
        db = _make_db(tmp_path)
        db.put("to-delete", {"x": 1})
        db.delete("to-delete")
        assert db.get("to-delete") is None

    def test_delete_nonexistent_is_safe(self, tmp_path):
        db = _make_db(tmp_path)
        db.delete("does-not-exist")  # should not raise

    def test_keys_are_repr_of_input(self, tmp_path):
        """Database stores repr(key) — integer and string keys differ."""
        db = _make_db(tmp_path)
        db.put("release-5", {"a": 1})
        # Different repr → different row
        assert db.get("release-5") == {"a": 1}
        assert db.get(5) is None   # int 5 → repr "5", not "release-5"


class TestDiscogsDatabasePosted:
    def test_get_posted_empty(self, tmp_path):
        db = _make_db(tmp_path)
        result = db.get_posted(99999)
        assert result == []

    def test_put_and_get_posted(self, tmp_path):
        db = _make_db(tmp_path)
        db.put_posted(
            releaseid=100,
            price=12.50,
            count=5,
            sales_hi=15.00,
            sales_lo=9.99,
            sales_avg=12.34,
            sales_mdn=12.00,
        )
        rows = db.get_posted(100)
        assert len(rows) == 1
        assert rows[0]["price"] == pytest.approx(12.50)
        assert rows[0]["count"] == 5

    def test_single_posting_retrieved(self, tmp_path):
        db = _make_db(tmp_path)
        # posted table has id as PRIMARY KEY — one row per releaseid
        db.put_posted(200, 10.0, 1, 10.0, 10.0, 10.0, 10.0)
        rows = db.get_posted(200)
        assert len(rows) == 1
        assert rows[0]["price"] == pytest.approx(10.0)

    def test_get_last_posted_no_entries(self, tmp_path):
        db = _make_db(tmp_path)
        assert db.get_last_posted(300) is None

    def test_get_last_posted_returns_row_with_max_age(self, tmp_path):
        """get_last_posted requires max_age > 0; entry inserted today has age 0 <= 5."""
        db = _make_db(tmp_path)
        db.put_posted(400, 8.00, 1, 10.0, 6.0, 8.0, 8.0)
        result = db.get_last_posted(400, max_age=5)
        assert result is not None
        assert result["price"] == pytest.approx(8.00)

    def test_get_last_posted_max_age_zero_returns_none(self, tmp_path):
        """max_age=0 (the default/disabled sentinel) → the condition is falsy → always None."""
        db = _make_db(tmp_path)
        db.put_posted(500, 5.00, 1, 5.0, 5.0, 5.0, 5.0)
        result = db.get_last_posted(500, max_age=0)
        assert result is None

    def test_get_last_posted_stale_returns_none(self, tmp_path):
        """If the most recent posting is older than max_age, return None."""
        db = _make_db(tmp_path)
        db.put_posted(600, 7.00, 1, 7.0, 7.0, 7.0, 7.0)

        # Manually backdate the entry to 10 days ago
        old_date = str(datetime.date.today() - datetime.timedelta(days=10))
        db.conn.execute("UPDATE posted SET date=? WHERE id=?", (old_date, 600))
        db.conn.commit()

        result = db.get_last_posted(600, max_age=5)
        assert result is None

    def test_get_last_posted_fresh_returns_row(self, tmp_path):
        """If the most recent posting is within max_age, return it.
        Entry inserted today has age=0; 0 <= 5, so it should be returned."""
        db = _make_db(tmp_path)
        db.put_posted(700, 9.00, 1, 9.0, 9.0, 9.0, 9.0)
        # Entry was just inserted → age is 0, which is ≤ max_age=5
        result = db.get_last_posted(700, max_age=5)
        assert result is not None
        assert result["price"] == pytest.approx(9.00)

    def test_get_last_posted_default_max_age_returns_none(self, tmp_path):
        """Default max_age=0 is the disabled sentinel — always returns None."""
        db = _make_db(tmp_path)
        db.put_posted(750, 7.00, 1, 7.0, 7.0, 7.0, 7.0)
        result = db.get_last_posted(750)  # default max_age=0
        assert result is None

    def test_isolation_between_ids(self, tmp_path):
        """Posted entries for different release IDs don't bleed into each other."""
        db = _make_db(tmp_path)
        db.put_posted(1001, 1.0, 1, 1, 1, 1, 1)
        db.put_posted(1002, 2.0, 1, 2, 2, 2, 2)
        assert db.get_posted(1001)[0]["price"] == pytest.approx(1.0)
        assert db.get_posted(1002)[0]["price"] == pytest.approx(2.0)
