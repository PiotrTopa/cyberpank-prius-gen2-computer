"""
Persistence of the alert framework: settings file and the alert log.

* ``alerts_settings.json`` (atomic writes): mode per alert type, fire counts,
  and small per-function memories (service intervals done, known DTCs,
  consumption history for the range estimate). Kept out of
  ``user_settings.json`` on purpose (separate owner, separate merges).
* ``alerts_log.jsonl``: one JSON object per line, every fire (shadow and on),
  clear, acknowledge, automation action and mode change, with the inputs that
  fired it. Rotated by size (``max_bytes`` x ``keep``). A writer thread does
  the file I/O and the metrics-database ``events`` rows, so the engine thread
  never waits for the disk.

Directory: ``BACKEND_ALERTS_DIR``, else ``alerts/`` next to the metrics
database.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import queue
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

SETTINGS_FILE = "alerts_settings.json"
LOG_FILE = "alerts_log.jsonl"


class AlertSettings:
    """JSON-backed settings. Thread-safe; ``save()`` is cheap and atomic."""

    def __init__(self, directory: Optional[str]) -> None:
        self.dir = Path(directory) if directory else None
        self._lock = threading.RLock()
        self.data: Dict[str, Any] = {"modes": {}, "counts": {}, "memory": {}}
        self.load()

    @property
    def path(self) -> Optional[Path]:
        return self.dir / SETTINGS_FILE if self.dir else None

    def load(self) -> None:
        p = self.path
        if p is None or not p.exists():
            return
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.exception("alert settings %s unreadable; starting from defaults", p)
            return
        if isinstance(raw, dict):
            with self._lock:
                for k in ("modes", "counts", "memory"):
                    if isinstance(raw.get(k), dict):
                        self.data[k] = raw[k]

    def save(self) -> bool:
        p = self.path
        if p is None:
            return False
        with self._lock:
            text = json.dumps(self.data, indent=1, sort_keys=True, default=str)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".alerts_", dir=str(p.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, p)
            return True
        except OSError:
            logger.exception("alert settings could not be written to %s", p)
            return False

    # modes
    def mode(self, alert_id: str) -> Optional[str]:
        with self._lock:
            m = self.data["modes"].get(alert_id)
        return m if isinstance(m, str) else None

    def set_mode(self, alert_id: str, mode: str) -> None:
        with self._lock:
            self.data["modes"][alert_id] = mode

    # per-function memory
    def memory(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.data["memory"].get(key, default)

    def set_memory(self, key: str, value: Any) -> None:
        with self._lock:
            self.data["memory"][key] = value

    # counts
    def counts(self) -> Dict[str, int]:
        with self._lock:
            return {k: int(v) for k, v in self.data["counts"].items() if isinstance(v, (int, float))}

    def add_count(self, alert_id: str, n: int = 1) -> int:
        with self._lock:
            c = int(self.data["counts"].get(alert_id, 0)) + n
            self.data["counts"][alert_id] = c
            return c


class AlertLog:
    """In-memory recent entries + a background JSONL writer (+ metrics events)."""

    def __init__(self, directory: Optional[str], keep_recent: int = 500,
                 max_bytes: int = 4 * 1024 * 1024, keep_files: int = 3,
                 db_writer: Optional[Callable[[List[Dict[str, Any]]], None]] = None) -> None:
        self.dir = Path(directory) if directory else None
        self.recent: Deque[Dict[str, Any]] = collections.deque(maxlen=keep_recent)
        self.max_bytes = max_bytes
        self.keep_files = keep_files
        self._db_writer = db_writer
        self._q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=5000)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.dropped = 0
        self.written = 0
        self.seq = 0

    @property
    def path(self) -> Optional[Path]:
        return self.dir / LOG_FILE if self.dir else None

    def start(self) -> None:
        if self._thread is not None or self.dir is None:
            return
        self._thread = threading.Thread(target=self._run, name="alerts-log", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        if self._thread is None:
            return
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout)
        self._thread = None

    def append(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self.seq += 1
            entry = dict(entry, seq=self.seq)
            self.recent.append(entry)
        if self._thread is not None:
            try:
                self._q.put_nowait(entry)
            except queue.Full:
                self.dropped += 1
        return entry

    def recent_entries(self, limit: int = 100, alert_id: Optional[str] = None,
                       event: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self.recent)
        if alert_id:
            items = [e for e in items if e.get("id") == alert_id]
        if event:
            items = [e for e in items if e.get("event") == event]
        return items[-limit:][::-1]

    def files(self) -> List[Path]:
        p = self.path
        if p is None:
            return []
        out = [p.with_name(p.name + ".%d" % i) for i in range(self.keep_files - 1, 0, -1)]
        out.append(p)
        return [f for f in out if f.exists()]

    def iter_export(self, since_wall: Optional[float] = None) -> Iterator[str]:
        """Every logged line, oldest first (rotated files included); entries
        not yet flushed come from memory."""
        last_seq = 0
        for f in self.files():
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        if since_wall is not None:
                            try:
                                if json.loads(line).get("ts", 0) < since_wall:
                                    continue
                            except ValueError:
                                continue
                        yield line + "\n"
            except OSError:
                continue
        if self.path is None or not self.files():
            with self._lock:
                items = list(self.recent)
            for e in items:
                if e.get("seq", 0) > last_seq and (since_wall is None or e.get("ts", 0) >= since_wall):
                    yield json.dumps(e, separators=(",", ":"), default=str) + "\n"

    def _rotate(self) -> None:
        p = self.path
        assert p is not None
        for i in range(self.keep_files - 1, 0, -1):
            src = p if i == 1 else p.with_name(p.name + ".%d" % (i - 1))
            dst = p.with_name(p.name + ".%d" % i)
            if src.exists():
                os.replace(src, dst)

    def _run(self) -> None:
        assert self.dir is not None
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("alert log directory %s not writable", self.dir)
            return
        while True:
            entry = self._q.get()
            if entry is None:
                return
            batch = [entry]
            while len(batch) < 200:
                try:
                    e = self._q.get_nowait()
                except queue.Empty:
                    break
                if e is None:
                    self._write(batch)
                    return
                batch.append(e)
            self._write(batch)

    def _write(self, batch: List[Dict[str, Any]]) -> None:
        p = self.path
        try:
            size = p.stat().st_size if p.exists() else 0
            fh = open(p, "a", encoding="utf-8")
            try:
                for e in batch:
                    if size > self.max_bytes:
                        fh.close()
                        self._rotate()
                        fh = open(p, "a", encoding="utf-8")
                        size = 0
                    line = json.dumps(e, separators=(",", ":"), default=str) + "\n"
                    fh.write(line)
                    size += len(line)
            finally:
                fh.close()
            self.written += len(batch)
        except OSError:
            logger.exception("alert log write failed")
        if self._db_writer is not None:
            try:
                self._db_writer(batch)
            except Exception:  # noqa: BLE001 - the log must not die with the DB
                logger.exception("alert events -> metrics database failed")
