from __future__ import annotations

import sqlite3
import util
import pickle
import os
import datetime
import multiprocessing

db_lock = multiprocessing.Lock()


def data2blob(data: object) -> sqlite3.Binary:
    return sqlite3.Binary(pickle.dumps(data, pickle.HIGHEST_PROTOCOL))


def blob2data(blob: bytes) -> object:
    return pickle.loads(blob)  # type: ignore[no-untyped-call]


def get_ts() -> str:
    return str(datetime.date.today())


def ts_age(ts: str) -> int:
    d = datetime.date(*[int(i) for i in ts.split("-")])
    return (datetime.date.today() - d).days

class DiscogsDatabase:
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
        c = self.conn.cursor()
        c.execute("SELECT * FROM responses where key=?", (repr(key),))
        r = c.fetchone()
        if not r:
            return None
        return blob2data(r["data"])

    def delete(self, key: object) -> None:
        c = self.conn.cursor()
        c.execute("DELETE FROM responses where key=?", (repr(key),))
        self.conn.commit()

    def put(self, key: object, value: object) -> None:
        c = self.conn.cursor()
        key_str = repr(key)
        b = data2blob(value)
        ts = get_ts()
        c.execute("INSERT INTO responses VALUES (?,?,?)", (key_str, ts, b))
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
