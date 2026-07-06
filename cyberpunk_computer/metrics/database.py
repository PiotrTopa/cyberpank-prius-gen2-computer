"""
Tiered SQLite time-series store for long-term vehicle metrics.

Design goals (POCO F1, always-on, power may be cut at any moment):
- Zero external daemons — pure stdlib ``sqlite3``. Fits the infra-as-code system.
- Crash-safe — WAL journal survives sudden power loss (the car cuts power).
- Bounded growth — raw samples are downsampled into minute / hour / day
  rollups, so "fuel consumption over a year" is a few hundred KB, not billions
  of rows.

Tiers (each a closed-bucket aggregate of the one below it):

    metrics_raw     1 Hz samples            kept ~RAW_RETENTION_DAYS
    metrics_1min    1-minute buckets        kept ~MIN_RETENTION_DAYS
    metrics_1hour   1-hour buckets          kept ~HOUR_RETENTION_DAYS
    metrics_1day    1-day buckets           kept forever

Aggregates store ``sum`` and ``count`` (not ``avg``) so cascading rollups stay
exact; ``avg`` is derived on read. ``last`` keeps the most recent value in the
bucket for "latest known" reads.

Discrete events (ignition on/off, power-source switch, faults) go to a separate
``events`` table.

Threading: SQLite connections are not shareable across threads. Each thread
(the metrics sink writer, each API reader) calls :meth:`connect` to get its own
connection. WAL allows one writer + many concurrent readers.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Bucket periods in seconds.
MINUTE = 60
HOUR = 3600
DAY = 86400

# Retention windows (None = keep forever).
RAW_RETENTION_DAYS = 2
MIN_RETENTION_DAYS = 90
HOUR_RETENTION_DAYS = 730  # ~2 years
DAY_RETENTION_DAYS: Optional[int] = None  # keep forever


@dataclass(frozen=True)
class AggrPoint:
    """One aggregated bucket returned from a rollup tier."""
    bucket: int      # epoch seconds, aligned to the tier period (bucket start)
    min: float
    max: float
    avg: float
    last: float
    count: int


@dataclass(frozen=True)
class EventRow:
    """A discrete event record."""
    ts: float
    type: str
    detail: str


# Maps a logical resolution name to (table, period_seconds).
_TIER_BY_RES: Dict[str, Tuple[str, int]] = {
    "raw": ("metrics_raw", 1),
    "1m": ("metrics_1min", MINUTE),
    "1h": ("metrics_1hour", HOUR),
    "1d": ("metrics_1day", DAY),
}


def _align(ts: float, period: int) -> int:
    """Return the bucket-start epoch second for ``ts`` at ``period`` seconds."""
    return int(ts // period) * period


class MetricsDatabase:
    """Owns the SQLite schema and provides write / rollup / query helpers.

    A single instance is shared between threads, but every database operation
    opens or receives a per-thread connection. The constructor initialises the
    schema once using a short-lived connection.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── connection ────────────────────────────────────────────────────────

    def connect(self) -> sqlite3.Connection:
        """Open a new, WAL-configured connection owned by the calling thread."""
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")  # WAL + NORMAL = crash-safe, fast
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS metrics_raw (
                    signal TEXT NOT NULL,
                    ts     REAL NOT NULL,
                    value  REAL NOT NULL,
                    PRIMARY KEY (signal, ts)
                ) WITHOUT ROWID;

                CREATE TABLE IF NOT EXISTS metrics_1min (
                    signal TEXT    NOT NULL,
                    bucket INTEGER NOT NULL,
                    min    REAL    NOT NULL,
                    max    REAL    NOT NULL,
                    sum    REAL    NOT NULL,
                    count  INTEGER NOT NULL,
                    last   REAL    NOT NULL,
                    PRIMARY KEY (signal, bucket)
                ) WITHOUT ROWID;

                CREATE TABLE IF NOT EXISTS metrics_1hour (
                    signal TEXT    NOT NULL,
                    bucket INTEGER NOT NULL,
                    min    REAL    NOT NULL,
                    max    REAL    NOT NULL,
                    sum    REAL    NOT NULL,
                    count  INTEGER NOT NULL,
                    last   REAL    NOT NULL,
                    PRIMARY KEY (signal, bucket)
                ) WITHOUT ROWID;

                CREATE TABLE IF NOT EXISTS metrics_1day (
                    signal TEXT    NOT NULL,
                    bucket INTEGER NOT NULL,
                    min    REAL    NOT NULL,
                    max    REAL    NOT NULL,
                    sum    REAL    NOT NULL,
                    count  INTEGER NOT NULL,
                    last   REAL    NOT NULL,
                    PRIMARY KEY (signal, bucket)
                ) WITHOUT ROWID;

                CREATE TABLE IF NOT EXISTS events (
                    ts     REAL NOT NULL,
                    type   TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
                CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events (type, ts);

                CREATE TABLE IF NOT EXISTS rollup_state (
                    tier        TEXT PRIMARY KEY,
                    last_bucket INTEGER NOT NULL
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

    # ── writes (called from the sink's writer thread) ─────────────────────

    def write_samples(
        self,
        conn: sqlite3.Connection,
        samples: Sequence[Tuple[str, float, float]],
    ) -> int:
        """Insert raw ``(signal, ts, value)`` samples in one transaction.

        Duplicate ``(signal, ts)`` pairs are ignored (idempotent re-runs).
        Returns the number of rows offered (not necessarily inserted).
        """
        if not samples:
            return 0
        conn.executemany(
            "INSERT OR IGNORE INTO metrics_raw (signal, ts, value) VALUES (?, ?, ?)",
            samples,
        )
        conn.commit()
        return len(samples)

    def write_events(
        self,
        conn: sqlite3.Connection,
        events: Sequence[Tuple[float, str, str]],
    ) -> int:
        """Insert ``(ts, type, detail)`` event rows in one transaction."""
        if not events:
            return 0
        conn.executemany(
            "INSERT INTO events (ts, type, detail) VALUES (?, ?, ?)",
            events,
        )
        conn.commit()
        return len(events)

    # ── rollups ───────────────────────────────────────────────────────────

    def rollup(self, conn: sqlite3.Connection, now: Optional[float] = None) -> None:
        """Cascade closed buckets raw→1min→1hour→1day.

        Only *closed* buckets (whose period has fully elapsed) are aggregated,
        so a bucket is written exactly once and never revised.
        """
        now = now if now is not None else time.time()
        self._rollup_from_raw(conn, now)
        self._rollup_from_aggr(conn, "metrics_1min", "metrics_1hour", HOUR, "1hour", now)
        self._rollup_from_aggr(conn, "metrics_1hour", "metrics_1day", DAY, "1day", now)
        conn.commit()

    def _get_last_bucket(self, conn: sqlite3.Connection, tier: str) -> int:
        row = conn.execute(
            "SELECT last_bucket FROM rollup_state WHERE tier = ?", (tier,)
        ).fetchone()
        return int(row[0]) if row else 0

    def _set_last_bucket(self, conn: sqlite3.Connection, tier: str, bucket: int) -> None:
        conn.execute(
            "INSERT INTO rollup_state (tier, last_bucket) VALUES (?, ?) "
            "ON CONFLICT(tier) DO UPDATE SET last_bucket = excluded.last_bucket",
            (tier, bucket),
        )

    def _rollup_from_raw(self, conn: sqlite3.Connection, now: float) -> None:
        last_done = self._get_last_bucket(conn, "1min")
        newest_closed = _align(now, MINUTE) - MINUTE  # last fully-elapsed minute
        if newest_closed <= last_done:
            return
        rows = conn.execute(
            "SELECT signal, ts, value FROM metrics_raw "
            "WHERE ts >= ? AND ts < ? ORDER BY signal, ts",
            (last_done + MINUTE, newest_closed + MINUTE),
        ).fetchall()
        buckets: Dict[Tuple[str, int], List[float]] = {}
        for signal, ts, value in rows:
            key = (signal, _align(ts, MINUTE))
            buckets.setdefault(key, []).append(value)
        out = []
        for (signal, bucket), values in buckets.items():
            out.append(
                (signal, bucket, min(values), max(values),
                 float(sum(values)), len(values), values[-1])
            )
        if out:
            conn.executemany(
                "INSERT OR REPLACE INTO metrics_1min "
                "(signal, bucket, min, max, sum, count, last) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                out,
            )
        self._set_last_bucket(conn, "1min", newest_closed)

    def _rollup_from_aggr(
        self,
        conn: sqlite3.Connection,
        src: str,
        dst: str,
        period: int,
        tier: str,
        now: float,
    ) -> None:
        last_done = self._get_last_bucket(conn, tier)
        newest_closed = _align(now, period) - period
        if newest_closed <= last_done:
            return
        rows = conn.execute(
            f"SELECT signal, bucket, min, max, sum, count, last FROM {src} "
            "WHERE bucket >= ? AND bucket < ? ORDER BY signal, bucket",
            (last_done + period, newest_closed + period),
        ).fetchall()
        # Aggregate child buckets into parent buckets, preserving exact sum/count.
        agg: Dict[Tuple[str, int], Dict[str, float]] = {}
        for signal, bucket, bmin, bmax, bsum, bcount, blast in rows:
            key = (signal, _align(bucket, period))
            cur = agg.get(key)
            if cur is None:
                agg[key] = {
                    "min": bmin, "max": bmax, "sum": bsum,
                    "count": bcount, "last": blast, "last_bucket": bucket,
                }
            else:
                cur["min"] = min(cur["min"], bmin)
                cur["max"] = max(cur["max"], bmax)
                cur["sum"] += bsum
                cur["count"] += bcount
                if bucket >= cur["last_bucket"]:
                    cur["last"] = blast
                    cur["last_bucket"] = bucket
        out = [
            (signal, bucket, v["min"], v["max"], v["sum"], int(v["count"]), v["last"])
            for (signal, bucket), v in agg.items()
        ]
        if out:
            conn.executemany(
                f"INSERT OR REPLACE INTO {dst} "
                "(signal, bucket, min, max, sum, count, last) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                out,
            )
        self._set_last_bucket(conn, tier, newest_closed)

    # ── retention ─────────────────────────────────────────────────────────

    def prune(self, conn: sqlite3.Connection, now: Optional[float] = None) -> None:
        """Delete data older than each tier's retention window."""
        now = now if now is not None else time.time()
        if RAW_RETENTION_DAYS is not None:
            conn.execute(
                "DELETE FROM metrics_raw WHERE ts < ?",
                (now - RAW_RETENTION_DAYS * DAY,),
            )
        for table, days in (
            ("metrics_1min", MIN_RETENTION_DAYS),
            ("metrics_1hour", HOUR_RETENTION_DAYS),
            ("metrics_1day", DAY_RETENTION_DAYS),
        ):
            if days is not None:
                conn.execute(
                    f"DELETE FROM {table} WHERE bucket < ?",
                    (int(now - days * DAY),),
                )
        conn.commit()

    # ── reads (called from API reader threads) ────────────────────────────

    @staticmethod
    def pick_resolution(start: float, end: float) -> str:
        """Choose the highest reasonable resolution for the requested window."""
        delta = end - start
        if delta <= 10 * MINUTE:
            return "raw"
        if delta <= 2 * HOUR:
            return "1m"
        if delta <= 8 * DAY:
            return "1h"
        return "1d"

    def query_series(
        self,
        conn: sqlite3.Connection,
        signal: str,
        start: float,
        end: float,
        resolution: Optional[str] = None,
    ) -> List[AggrPoint]:
        """Return aggregated points for ``signal`` within ``[start, end]``.

        ``resolution`` is one of raw/1m/1h/1d; if omitted it is auto-picked.
        Raw rows are returned as degenerate aggregates (min=max=avg=last=value).
        """
        res = resolution or self.pick_resolution(start, end)
        table, _ = _TIER_BY_RES.get(res, _TIER_BY_RES["1m"])
        if table == "metrics_raw":
            rows = conn.execute(
                "SELECT ts, value FROM metrics_raw "
                "WHERE signal = ? AND ts >= ? AND ts <= ? ORDER BY ts",
                (signal, start, end),
            ).fetchall()
            return [
                AggrPoint(int(ts), value, value, value, value, 1)
                for ts, value in rows
            ]
        rows = conn.execute(
            f"SELECT bucket, min, max, sum, count, last FROM {table} "
            "WHERE signal = ? AND bucket >= ? AND bucket <= ? ORDER BY bucket",
            (signal, int(start), int(end)),
        ).fetchall()
        return [
            AggrPoint(int(b), bmin, bmax, (bsum / bcount if bcount else 0.0), blast, int(bcount))
            for b, bmin, bmax, bsum, bcount, blast in rows
        ]

    def query_latest(self, conn: sqlite3.Connection, signal: str) -> Optional[float]:
        """Return the most recent raw value for a signal, if any."""
        row = conn.execute(
            "SELECT value FROM metrics_raw WHERE signal = ? ORDER BY ts DESC LIMIT 1",
            (signal,),
        ).fetchone()
        return float(row[0]) if row else None

    def query_events(
        self,
        conn: sqlite3.Connection,
        start: float,
        end: float,
        type_filter: Optional[str] = None,
        limit: int = 1000,
    ) -> List[EventRow]:
        """Return discrete events within ``[start, end]``, newest first."""
        if type_filter:
            rows = conn.execute(
                "SELECT ts, type, detail FROM events "
                "WHERE ts >= ? AND ts <= ? AND type = ? ORDER BY ts DESC LIMIT ?",
                (start, end, type_filter, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, type, detail FROM events "
                "WHERE ts >= ? AND ts <= ? ORDER BY ts DESC LIMIT ?",
                (start, end, limit),
            ).fetchall()
        return [EventRow(ts, typ, detail) for ts, typ, detail in rows]

    def signals_present(self, conn: sqlite3.Connection) -> List[str]:
        """Return the distinct signal names that have any stored data."""
        rows = conn.execute(
            "SELECT DISTINCT signal FROM metrics_1min "
            "UNION SELECT DISTINCT signal FROM metrics_raw ORDER BY 1"
        ).fetchall()
        return [r[0] for r in rows]
