"""SQLite cache for Discogs API responses.

Storage layout
--------------
Database file: ~/.discogstool/discogs.db

Tables:
  responses   General-purpose key/value cache.  Keys are repr() of the
              Python object passed to get()/put() (strings get extra quotes,
              so the string "release-123" is stored as "'release-123'").
              Values are pickled with the highest available protocol.
              TTL is enforced by the caller via DiscogsDatabase(max_age=N);
              stale entries are deleted on read rather than purged proactively.

  posted      Price history for Discogs marketplace listings.  Each row is a
              snapshot (date, price, sales stats) for a single release ID.
              Multiple rows per release accumulate over time; callers use
              get_last_posted() to retrieve the most recent snapshot within
              an optional age window.

Concurrency
-----------
db_lock (multiprocessing.Lock) is held during __init__ to serialise schema
creation.  Reads and writes are otherwise unsynchronised at the Python level;
WAL journal mode (set on every connection) lets SQLite handle concurrent
readers safely without exclusive locks.

Pickle protocol
---------------
pickle.HIGHEST_PROTOCOL is used for efficiency.  This means the cache file
is not portable across Python major versions, but that is acceptable for a
local developer cache that can be regenerated on demand.
"""

from __future__ import annotations

import sqlite3
import util
import pickle
import os
import datetime
import multiprocessing

db_lock = multiprocessing.Lock()


def data2blob(data: object) -> sqlite3.Binary:
    """Serialize an arbitrary Python object to a SQLite BLOB via pickle."""
    return sqlite3.Binary(pickle.dumps(data, pickle.HIGHEST_PROTOCOL))


def blob2data(blob: bytes) -> object:
    """Deserialize a pickled BLOB back to a Python object."""
    return pickle.loads(blob)  # type: ignore[no-untyped-call]


def get_ts() -> str:
    """Return today's date as an ISO-8601 string (YYYY-MM-DD)."""
    return str(datetime.date.today())


def ts_age(ts: str) -> int:
    """Return the number of days elapsed since the given ISO-8601 date string."""
    d = datetime.date(*[int(i) for i in ts.split("-")])
    return (datetime.date.today() - d).days

class DiscogsDatabase:
    """Persistent SQLite cache for Discogs API responses.

    Each instance opens (or creates) ~/.discogstool/discogs.db.  Cached
    entries older than max_age days are treated as stale: getData() in
    client_interface.py deletes and re-fetches them.

    The database is opened in WAL mode so that multiple reader processes
    (dt_process worker pool) can query concurrently without blocking each
    other or the writer.
    """

    conn: sqlite3.Connection
    max_age: int

    def __init__(self, max_age: int = 7) -> None:
        with db_lock:
            db_file = util.userfile("discogs.db")
            create_flag = not os.path.exists(db_file)
            self.conn = sqlite3.connect(db_file)
            # fetch rows as dictionaries
            self.conn.row_factory = sqlite3.Row

            # maximum data age
            self.max_age = max_age

            self.conn.execute("PRAGMA journal_mode=WAL")

            if create_flag:
                print("Creating new database.")
                c = self.conn.cursor()
                c.execute(
                    """CREATE TABLE responses (key TEXT PRIMARY KEY,
                                                    last_update TEXT,
                                                    data BLOB)"""
                )
                c.execute(
                    """CREATE TABLE posted (id INTEGER PRIMARY KEY,
                                                price REAL,
                                                count INTEGER,
                                                sales_hi REAL,
                                                sales_lo REAL,
                                                sales_avg REAL,
                                                sales_mdn REAL,
                                                date TEXT)"""
                )
                self.conn.commit()

    def get(self, key: object) -> object | None:
        """Return the cached value for key, or None if not present.

        The age of the entry is NOT checked here; callers are responsible for
        calling delete() and re-fetching when ts_age(entry) > self.max_age.
        """
        c = self.conn.cursor()
        c.execute("SELECT * FROM responses where key=?", (repr(key),))
        r = c.fetchone()
        if not r:
            return None
        return blob2data(r["data"])

    def delete(self, key: object) -> None:
        """Remove a cached entry by key (no-op if not present)."""
        c = self.conn.cursor()
        c.execute("DELETE FROM responses where key=?", (repr(key),))
        self.conn.commit()

    def put(self, key: object, value: object) -> None:
        """Insert or replace the cached value for key, stamped with today's date."""
        c = self.conn.cursor()
        key_str = repr(key)
        b = data2blob(value)
        ts = get_ts()
        c.execute("INSERT OR REPLACE INTO responses VALUES (?,?,?)", (key_str, ts, b))
        self.conn.commit()

    def get_posted(self, releaseid: int) -> list[sqlite3.Row]:
        c = self.conn.cursor()
        c.execute("SELECT * FROM posted WHERE id=? ORDER BY date DESC", (releaseid,))
        return c.fetchall()

    def get_last_posted(self, releaseid: int, max_age: int = 0) -> sqlite3.Row | None:
        results = self.get_posted(releaseid)
        if not results:
            return None
        recent = results[0]
        if max_age and ts_age(recent["date"]) <= max_age:
            return recent
        return None

    def put_posted(
        self,
        releaseid: int,
        price: float,
        count: int,
        sales_hi: float,
        sales_lo: float,
        sales_avg: float,
        sales_mdn: float,
    ) -> None:
        c = self.conn.cursor()
        ts = get_ts()
        c.execute("INSERT INTO posted VALUES (?,?,?,?,?,?,?,?)",
                (releaseid, price, count, sales_hi, sales_lo, sales_avg, sales_mdn, ts))
        self.conn.commit()
