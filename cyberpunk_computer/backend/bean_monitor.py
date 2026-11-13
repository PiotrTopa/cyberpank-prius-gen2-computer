"""
BEAN monitor: the last frames (for GET /api/v1/bean/frames) and the capture
log for reverse engineering.

Fed by a store middleware with every :class:`SetBeanRxAction` (engine
thread); read by the API thread, hence the lock.

Capture log (``bean_capture`` REST command): one JSON object per line in
``<dir>/bean_<YYYYmmdd-HHMMSS>[_<name>][_partN].ndjson``, rotated at
``rotate_bytes`` (20 MB):

    {"type":"header","format":"bean-capture/1","start":"2026-11-13T18:02:11+01:00","name":"doors","part":1}
    {"type":"frame","ts":1763053331.204,"t_ms":123456,"pri":6,"dst":"FE","mes":"2C","data":"64","rsp":1,"ok":1}
    {"type":"frame","ts":...,"ok":0,"err":"crc",...}
    {"type":"mark","ts":1763053340.0,"text":"driver door opened"}
    {"type":"stats","ts":...,"st":{firmware counters}}   (every 10th stats line)

``rsp`` is the raw 2-bit RSP field (01 = ACK); never interpreted here.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

RING_FRAMES = 200
MARKS_MAX = 50
ROTATE_BYTES = 20 * 1024 * 1024


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(name or "")).strip("_")[:40]


class BeanMonitor:
    def __init__(self, directory: str = "logs/bean", ring: int = RING_FRAMES,
                 rotate_bytes: int = ROTATE_BYTES, clock=time.time) -> None:
        self.directory = directory
        self.rotate_bytes = int(rotate_bytes)
        self._clock = clock
        self._lock = threading.Lock()
        self._ring: "collections.deque" = collections.deque(maxlen=ring)
        self._seq = 0
        self.marks: "collections.deque" = collections.deque(maxlen=MARKS_MAX)
        self._fh = None
        self._name = ""
        self._start = ""
        self._part = 0
        self._bytes = 0
        self._frames = 0
        self._stats_n = 0
        self.path: Optional[str] = None
        self.error: Optional[str] = None

    # ── feed (engine thread) ────────────────────────────────────────────────
    def ingest(self, frames: List[Dict[str, Any]], stats: Optional[dict] = None) -> None:
        with self._lock:
            for f in frames:
                self._seq += 1
                rec = dict(f)
                rec["seq"] = self._seq
                self._ring.append(rec)
        if self._fh is not None:
            for f in frames:
                line = {"type": "frame", "ts": f.get("ts"), "t_ms": f.get("t_ms"),
                        "pri": f.get("pri"), "dst": f.get("dst"), "mes": f.get("mes"),
                        "data": f.get("data"), "rsp": f.get("rsp"),
                        "ok": 1 if f.get("ok") else 0}
                if not f.get("ok"):
                    line["err"] = f.get("err")
                self._write(line)
                self._frames += 1
            if stats:
                self._stats_n += 1
                if self._stats_n % 10 == 1:
                    self._write({"type": "stats", "ts": round(self._clock(), 3), "st": stats})

    # ── capture control ──────────────────────────────────────────────────────
    @property
    def capturing(self) -> bool:
        return self._fh is not None

    def start(self, name: str = "") -> bool:
        self.stop()
        self._name = _safe_name(name)
        self._start = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._clock()))
        self._part = 0
        self._frames = 0
        self._stats_n = 0
        self.error = None
        try:
            os.makedirs(self.directory, exist_ok=True)
            self._open_part()
        except OSError as exc:
            self.error = str(exc)
            self._fh = None
            logger.error("BEAN capture could not start in %s: %s", self.directory, exc)
            return False
        logger.info("BEAN capture started: %s", self.path)
        return True

    def stop(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            logger.info("BEAN capture stopped: %s (%d frames)", self.path, self._frames)
        self._fh = None

    def mark(self, text: str) -> Dict[str, Any]:
        rec = {"type": "mark", "ts": round(self._clock(), 3), "text": str(text)[:200]}
        self.marks.append(rec)
        if self._fh is not None:
            self._write(rec)
        else:
            logger.info("BEAN mark (no capture running): %s", rec["text"])
        return rec

    def status(self) -> Dict[str, Any]:
        return {"on": self.capturing, "file": self.path, "frames": self._frames,
                "bytes": self._bytes, "part": self._part, "name": self._name,
                "dir": self.directory, "error": self.error}

    def _open_part(self) -> None:
        self._part += 1
        base = "bean_%s" % self._start
        if self._name:
            base += "_" + self._name
        if self._part > 1:
            base += "_part%d" % self._part
        self.path = os.path.join(self.directory, base + ".ndjson")
        self._fh = open(self.path, "a", encoding="utf-8")
        self._bytes = 0
        start_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self._clock()))
        self._write({"type": "header", "format": "bean-capture/1", "start": start_iso,
                     "name": self._name, "part": self._part}, rotate=False)

    def _write(self, obj: Dict[str, Any], rotate: bool = True) -> None:
        if self._fh is None:
            return
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        try:
            if rotate and self._bytes + len(line) > self.rotate_bytes:
                self._fh.close()
                self._open_part()
            self._fh.write(line)
            self._fh.flush()
            self._bytes += len(line)
        except OSError as exc:
            self.error = str(exc)
            logger.error("BEAN capture write failed (%s); capture stopped", exc)
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    # ── API thread ──────────────────────────────────────────────────────────
    def frames_since(self, since: int = 0, limit: int = RING_FRAMES) -> Dict[str, Any]:
        with self._lock:
            out = [f for f in self._ring if f["seq"] > since][:max(1, int(limit))]
            last = self._seq
        return {"frames": out, "next": out[-1]["seq"] if out else max(since, 0),
                "last_seq": last, "marks": list(self.marks)[-10:]}
