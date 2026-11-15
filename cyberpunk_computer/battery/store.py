"""SQLite storage of the battery module (``battery.db``).

    meta    key/value: schema_version
    rides   one row per ride: time span, source, grade, features (JSON), raw (BLOB)
    rests   one row per rest: time span, source, features (JSON), raw (BLOB)
    cache   precomputed JSON (the summary the UI reads)

``raw`` is the compact record (model.encode_record) and the source of truth;
``features`` is derived (analysis.py) and rebuilt when ``analysis_ver`` is
older than the code. Open records (``closed`` = 0) are the crash spool: they are
overwritten every minute while a ride or rest is in progress.

WAL journal + synchronous NORMAL: a power cut loses at most the last
transaction (one spool interval), never the file.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Iterator, List, Optional

from . import analysis
from .model import ANALYSIS_VERSION, SCHEMA_VERSION, decode_record, encode_record

# Richer sources replace poorer ones when their time spans overlap.
SOURCE_RANK = {"metrics1m": 1, "synth": 2, "trip": 3, "live": 4}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rides (
    id INTEGER PRIMARY KEY,
    start_ts REAL NOT NULL, end_ts REAL NOT NULL,
    source TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 1,
    grade TEXT, analysis_ver INTEGER NOT NULL DEFAULT 0,
    features TEXT, raw BLOB);
CREATE INDEX IF NOT EXISTS rides_start ON rides (start_ts);
CREATE TABLE IF NOT EXISTS rests (
    id INTEGER PRIMARY KEY,
    start_ts REAL NOT NULL, end_ts REAL NOT NULL,
    source TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 1,
    analysis_ver INTEGER NOT NULL DEFAULT 0,
    features TEXT, raw BLOB);
CREATE INDEX IF NOT EXISTS rests_start ON rests (start_ts);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, ts REAL NOT NULL, value TEXT NOT NULL);
"""


def _rank(source: Optional[str]) -> int:
    return SOURCE_RANK.get((source or "").split(":")[0], 0)


class BatteryStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._local = threading.local()
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        conn = self.conn()
        conn.executescript(_SCHEMA)
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            conn.execute("INSERT OR IGNORE INTO meta VALUES ('created_ts', ?)", (str(time.time()),))
        else:
            self._migrate(conn, int(row[0]))
        conn.commit()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=15.0)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def _migrate(self, conn: sqlite3.Connection, have: int) -> None:
        """Schema migrations, one step per version (none yet)."""
        if have > SCHEMA_VERSION:
            raise RuntimeError("battery.db schema %d is newer than this code (%d)" % (have, SCHEMA_VERSION))
        # if have < 2: conn.execute("ALTER TABLE ...") ; have = 2
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    # ── writes ───────────────────────────────────────────────────────────────

    def _put(self, table: str, rec: Optional[dict], features: dict, start: float, end: float,
             source: str, closed: bool, force: bool = False) -> Optional[int]:
        conn = self.conn()
        raw = encode_record(rec["meta"], rec["tables"]) if rec is not None else None
        grade = (features.get("quality") or {}).get("grade") if table == "rides" else None
        fjson = json.dumps(features, separators=(",", ":"))
        # the spool row of the same record (same start) is updated in place
        row = conn.execute("SELECT id, source, closed FROM %s WHERE ABS(start_ts - ?) < 1.0" % table,
                           (start,)).fetchone()
        if row is not None and (not row[2] or force or _rank(source) > _rank(row[1])):
            if table == "rides":
                conn.execute("UPDATE rides SET end_ts=?, source=?, closed=?, grade=?, analysis_ver=?, "
                             "features=?, raw=? WHERE id=?",
                             (end, source, int(closed), grade, features.get("v", 0), fjson, raw, row[0]))
            else:
                conn.execute("UPDATE rests SET end_ts=?, source=?, closed=?, analysis_ver=?, "
                             "features=?, raw=? WHERE id=?",
                             (end, source, int(closed), features.get("v", 0), fjson, raw, row[0]))
            conn.commit()
            return row[0]
        if row is not None:
            return None
        # overlapping records from another source: the richer one wins
        over = conn.execute("SELECT id, source FROM %s WHERE start_ts < ? AND end_ts > ?" % table,
                            (end, start)).fetchall()
        if any(_rank(src) > _rank(source) or (_rank(src) == _rank(source) and not force)
               for _, src in over):
            return None
        for oid, _ in over:
            conn.execute("DELETE FROM %s WHERE id=?" % table, (oid,))
        if table == "rides":
            cur = conn.execute("INSERT INTO rides (start_ts, end_ts, source, closed, grade, analysis_ver, "
                               "features, raw) VALUES (?,?,?,?,?,?,?,?)",
                               (start, end, source, int(closed), grade, features.get("v", 0), fjson, raw))
        else:
            cur = conn.execute("INSERT INTO rests (start_ts, end_ts, source, closed, analysis_ver, "
                               "features, raw) VALUES (?,?,?,?,?,?,?)",
                               (start, end, source, int(closed), features.get("v", 0), fjson, raw))
        conn.commit()
        return cur.lastrowid

    def put_ride(self, rec: dict, features: Optional[dict] = None, force: bool = False) -> Optional[int]:
        m = rec["meta"]
        features = features if features is not None else analysis.analyse_ride(rec)
        return self._put("rides", rec, features, m["start_ts"], m["end_ts"], m.get("source", "live"),
                         bool(m.get("closed", True)), force)

    def put_rest(self, rec: dict, features: Optional[dict] = None, force: bool = False) -> Optional[int]:
        m = rec["meta"]
        features = features if features is not None else analysis.analyse_rest(rec)
        return self._put("rests", rec, features, m["start_ts"], m["end_ts"], m.get("source", "live"),
                         bool(m.get("closed", True)), force)

    def put_lite_ride(self, start: float, end: float, features: dict, source: str = "metrics1m") -> Optional[int]:
        """A ride known only from aggregates (no raw record)."""
        return self._put("rides", None, features, start, end, source, True)

    # ── reads ────────────────────────────────────────────────────────────────

    @staticmethod
    def _row(row, with_raw: bool) -> Dict[str, Any]:
        out = {"id": row[0], "start_ts": row[1], "end_ts": row[2], "source": row[3],
               "closed": bool(row[4]), "analysis_ver": row[5],
               "features": json.loads(row[6]) if row[6] else {}}
        if with_raw:
            out["raw"] = row[7]
        return out

    def rides(self, since: float = 0.0, until: float = 1e12, closed_only: bool = True,
              with_raw: bool = False, limit: Optional[int] = None, newest_first: bool = False) -> List[dict]:
        return self._list("rides", since, until, closed_only, with_raw, limit, newest_first)

    def rests(self, since: float = 0.0, until: float = 1e12, closed_only: bool = False,
              with_raw: bool = False, limit: Optional[int] = None, newest_first: bool = False) -> List[dict]:
        return self._list("rests", since, until, closed_only, with_raw, limit, newest_first)

    def _list(self, table, since, until, closed_only, with_raw, limit, newest_first) -> List[dict]:
        q = ("SELECT id, start_ts, end_ts, source, closed, analysis_ver, features%s FROM %s "
             "WHERE start_ts >= ? AND start_ts < ?%s ORDER BY start_ts %s%s") % (
            ", raw" if with_raw else "", table, " AND closed=1" if closed_only else "",
            "DESC" if newest_first else "ASC", " LIMIT %d" % int(limit) if limit else "")
        return [self._row(r, with_raw) for r in self.conn().execute(q, (since, until))]

    def get(self, table: str, rid: int, with_raw: bool = True) -> Optional[dict]:
        assert table in ("rides", "rests")
        r = self.conn().execute(
            "SELECT id, start_ts, end_ts, source, closed, analysis_ver, features, raw FROM %s WHERE id=?" % table,
            (rid,)).fetchone()
        return self._row(r, with_raw) if r else None

    @staticmethod
    def record(row: dict) -> Optional[dict]:
        """Decoded raw record of a row read with ``with_raw``."""
        if not row.get("raw"):
            return None
        meta, tables = decode_record(row["raw"])
        return {"meta": meta, "tables": tables}

    # ── maintenance ──────────────────────────────────────────────────────────

    def close_open(self, keep_rest: bool = True) -> Dict[str, Any]:
        """Start-up recovery: an open ride left by a crash / power cut is closed
        as it stands (flag ``truncated``); the open rest is returned so the
        collector can continue it."""
        conn = self.conn()
        out: Dict[str, Any] = {"rides_closed": 0, "rest": None}
        for r in conn.execute("SELECT id, raw FROM rides WHERE closed=0").fetchall():
            if r[1]:
                meta, tables = decode_record(r[1])
                meta["closed"] = True
                meta["truncated"] = True
                rec = {"meta": meta, "tables": tables}
                f = analysis.analyse_ride(rec)
                conn.execute("UPDATE rides SET closed=1, grade=?, analysis_ver=?, features=?, raw=? WHERE id=?",
                             (f["quality"]["grade"], f["v"], json.dumps(f, separators=(",", ":")),
                              encode_record(meta, tables), r[0]))
            else:
                conn.execute("UPDATE rides SET closed=1 WHERE id=?", (r[0],))
            out["rides_closed"] += 1
        rows = conn.execute("SELECT id, raw FROM rests WHERE closed=0 ORDER BY start_ts").fetchall()
        for k, r in enumerate(rows):
            last = k == len(rows) - 1
            if last and keep_rest and r[1]:
                meta, tables = decode_record(r[1])
                out["rest"] = {"meta": meta, "tables": tables}
            else:
                conn.execute("UPDATE rests SET closed=1 WHERE id=?", (r[0],))
        conn.commit()
        return out

    def reanalyse(self, force: bool = False) -> Dict[str, int]:
        """Rebuild features from the raw records where the analysis is older
        than the code (or everywhere with ``force``)."""
        conn = self.conn()
        n = {"rides": 0, "rests": 0}
        cond = "" if force else " AND analysis_ver < %d" % ANALYSIS_VERSION
        for r in conn.execute("SELECT id, raw FROM rides WHERE raw IS NOT NULL AND closed=1" + cond).fetchall():
            meta, tables = decode_record(r[1])
            f = analysis.analyse_ride({"meta": meta, "tables": tables})
            conn.execute("UPDATE rides SET grade=?, analysis_ver=?, features=? WHERE id=?",
                         (f["quality"]["grade"], f["v"], json.dumps(f, separators=(",", ":")), r[0]))
            n["rides"] += 1
        for r in conn.execute("SELECT id, raw FROM rests WHERE raw IS NOT NULL" + cond).fetchall():
            meta, tables = decode_record(r[1])
            f = analysis.analyse_rest({"meta": meta, "tables": tables})
            conn.execute("UPDATE rests SET analysis_ver=?, features=? WHERE id=?",
                         (f["v"], json.dumps(f, separators=(",", ":")), r[0]))
            n["rests"] += 1
        conn.commit()
        return n

    def set_cache(self, key: str, value: Any) -> None:
        conn = self.conn()
        conn.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                     (key, time.time(), json.dumps(value, separators=(",", ":"))))
        conn.commit()

    def get_cache(self, key: str) -> Optional[Any]:
        r = self.conn().execute("SELECT value FROM cache WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else None

    def stats(self) -> Dict[str, Any]:
        conn = self.conn()
        rides = conn.execute("SELECT COUNT(*), MIN(start_ts), MAX(end_ts), "
                             "COALESCE(SUM(LENGTH(raw)),0), COALESCE(SUM(LENGTH(features)),0), "
                             "COALESCE(SUM(end_ts-start_ts),0) FROM rides").fetchone()
        rests = conn.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(raw)),0), "
                             "COALESCE(SUM(LENGTH(features)),0) FROM rests").fetchone()
        try:
            size = os.path.getsize(self.path)
        except OSError:
            size = None
        return {"rides": rides[0], "first_ts": rides[1], "last_ts": rides[2],
                "ride_raw_bytes": rides[3], "ride_feature_bytes": rides[4], "ride_seconds": rides[5],
                "rests": rests[0], "rest_raw_bytes": rests[1], "rest_feature_bytes": rests[2],
                "db_bytes": size, "schema": SCHEMA_VERSION, "analysis_ver": ANALYSIS_VERSION,
                "path": self.path}

    def backup_to(self, dest: str) -> None:
        """Consistent copy of the database (SQLite online backup)."""
        dst = sqlite3.connect(dest)
        try:
            self.conn().backup(dst)
        finally:
            dst.close()
