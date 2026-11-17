"""
SQLite persistence of the fuelling log (``fuel_log.db`` next to the metrics
database). One table of fills, one key/value table (the cumulative estimator
counter). WAL journal: the car cuts power at any moment.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,                 -- fill time, epoch s (may be back-dated)
    fuel TEXT NOT NULL CHECK (fuel IN ('lpg', 'petrol')),
    litres REAL NOT NULL,
    price_per_l REAL,                 -- optional, currency per litre (PLN)
    total REAL,                       -- optional, amount paid (PLN)
    full INTEGER NOT NULL DEFAULT 1,
    odometer_km REAL NOT NULL,        -- the BACKEND's odometer (vehicle.odometer_km)
    odometer_source TEXT NOT NULL,    -- live | given | metrics_speed
    est_total_l REAL,                 -- cumulative estimator counter at the fill (None if back-dated)
    car_json TEXT,                    -- what the car knew when the entry was made
    note TEXT,
    created_at REAL NOT NULL,
    edited_at REAL,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS fills_live ON fills (deleted, odometer_km);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

COLUMNS = ("id", "ts", "fuel", "litres", "price_per_l", "total", "full", "odometer_km",
           "odometer_source", "est_total_l", "car_json", "note", "created_at", "edited_at")
EDITABLE = ("ts", "fuel", "litres", "price_per_l", "total", "full", "odometer_km",
            "odometer_source", "note")


class FuelLogStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row(r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(zip(COLUMNS, r))
        d["full"] = bool(d["full"])
        car = d.pop("car_json")
        d["car"] = json.loads(car) if car else None
        return d

    def entries(self) -> List[Dict[str, Any]]:
        """Every live (not undone) fill, in insertion order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT %s FROM fills WHERE deleted = 0 ORDER BY id" % ", ".join(COLUMNS)).fetchall()
        return [self._row(r) for r in rows]

    def get(self, fid: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self._conn.execute("SELECT %s FROM fills WHERE id = ?" % ", ".join(COLUMNS),
                                   (fid,)).fetchone()
        return self._row(r) if r else None

    def add(self, entry: Dict[str, Any]) -> int:
        e = dict(entry)
        car = e.pop("car", None)
        e["car_json"] = json.dumps(car, separators=(",", ":")) if car is not None else None
        e.setdefault("created_at", time.time())
        cols = [c for c in COLUMNS if c != "id" and c in e]
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO fills (%s) VALUES (%s)" % (", ".join(cols), ", ".join("?" * len(cols))),
                [int(e[c]) if c == "full" else e[c] for c in cols])
            self._conn.commit()
            return int(cur.lastrowid)

    def last_id(self) -> Optional[int]:
        """The most recently ENTERED live fill (what undo / edit act on)."""
        with self._lock:
            r = self._conn.execute("SELECT MAX(id) FROM fills WHERE deleted = 0").fetchone()
        return int(r[0]) if r and r[0] is not None else None

    def soft_delete(self, fid: int) -> bool:
        with self._lock:
            cur = self._conn.execute("UPDATE fills SET deleted = 1, edited_at = ? WHERE id = ? AND deleted = 0",
                                     (time.time(), fid))
            self._conn.commit()
            return cur.rowcount == 1

    def update(self, fid: int, fields: Dict[str, Any]) -> bool:
        f = {k: v for k, v in fields.items() if k in EDITABLE}
        if not f:
            return False
        sets = ", ".join("%s = ?" % k for k in f) + ", edited_at = ?"
        vals = [int(v) if k == "full" else v for k, v in f.items()] + [time.time(), fid]
        with self._lock:
            cur = self._conn.execute("UPDATE fills SET %s WHERE id = ? AND deleted = 0" % sets, vals)
            self._conn.commit()
            return cur.rowcount == 1

    def kv_get(self, k: str) -> Optional[str]:
        with self._lock:
            r = self._conn.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
        return r[0] if r else None

    def kv_set(self, k: str, v: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                               (k, v))
            self._conn.commit()
