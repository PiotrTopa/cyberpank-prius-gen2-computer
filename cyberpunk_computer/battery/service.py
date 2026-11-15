"""Backend glue of the battery module: ingress tap + low-priority worker.

    ingress message-log callback (engine thread, panda thread)
        -> ``tap``: a cheap filter, then ``deque.append`` (no decoding there)
    worker thread ("battery-health", nice +10)
        every 0.5 s: drain the queue -> FrameDecoder -> Collector
        every 60 s:  spool the open ride / rest into battery.db
        on a closed ride: analyse, store, rebuild the summary cache

The tap sees exactly the messages the trip recorder writes, so the live path
and the backfill share the decoder and the collector.

Clock: stream time is CLOCK_BOOTTIME (counts suspend, never steps) anchored to
the wall clock; the anchor is refreshed only while no ride is open, so a ride
never sees a clock step. Stamps written before the phone has a sane clock are
flagged (``clock_suspect``).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Any, Dict, Optional

from . import analysis, frames, summary
from .collector import Collector
from .frames import FrameDecoder
from .model import SPOOL_INTERVAL_S
from .store import BatteryStore

logger = logging.getLogger(__name__)

_MIN_SANE_WALL = 1_767_225_600.0     # 2026-01-01: an unset RTC reads far below
_QUEUE_MAX = 200_000                 # ~6 min of battery frames if the worker stalls


def _boottime() -> float:
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return time.monotonic()


class BatteryHealthService:
    def __init__(self, db_path: str, nice: int = 10) -> None:
        self.db_path = db_path
        self.store = BatteryStore(db_path)
        self._queue: deque = deque(maxlen=_QUEUE_MAX)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._nice = nice
        self._anchor = (0.0, 0.0)      # (boottime, wall)
        self._recompute = threading.Event()
        self._collector: Optional[Collector] = None
        self._decoder: Optional[FrameDecoder] = None
        self.dropped = 0
        self.status: Dict[str, Any] = {"running": False, "ride_open": False, "queue": 0,
                                       "rides_stored": 0, "last_ride_ts": None, "last_error": None,
                                       "summary_s": None, "analysis_s": None, "counts": {}}

    # ── tap (ingress threads) ────────────────────────────────────────────────

    def tap(self, msg, direction: str = "IN") -> None:
        """Message-log callback: filter and queue; never blocks, never raises."""
        try:
            data = msg.data
            if frames.wanted(msg.device_id, data):
                q = self._queue
                if len(q) >= _QUEUE_MAX:
                    self.dropped += 1
                q.append((_boottime(), msg.device_id, data))
        except Exception:        # a tap must never disturb the ingress
            pass

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="battery-health", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def request_recompute(self) -> None:
        self._recompute.set()

    # ── worker ───────────────────────────────────────────────────────────────

    def _wall(self, bt: float) -> float:
        return self._anchor[1] + (bt - self._anchor[0])

    def _reanchor(self) -> None:
        self._anchor = (_boottime(), time.time())

    def _on_ride(self, rec: dict) -> None:
        t0 = time.perf_counter()
        if rec["meta"]["start_ts"] < _MIN_SANE_WALL:
            rec["meta"]["clock_suspect"] = True
        f = analysis.analyse_ride(rec)
        self.store.put_ride(rec, f)
        self.status["analysis_s"] = round(time.perf_counter() - t0, 3)
        self.status["rides_stored"] += 1
        self.status["last_ride_ts"] = rec["meta"]["end_ts"]
        logger.info("Battery: ride stored (%.0f s, grade %s, %d block samples)",
                    rec["meta"]["end_ts"] - rec["meta"]["start_ts"], f["quality"]["grade"], f["n_blk"])
        self._recompute.set()

    def _on_rest(self, rec: dict) -> None:
        self.store.put_rest(rec)

    def _spool(self) -> None:
        snap = self._collector.snapshot()
        if snap["ride"] is not None:
            self.store.put_ride(snap["ride"], {"v": 0, "quality": {"grade": None, "flags": ["open"]}})
        if snap["rest"] is not None:
            self.store.put_rest(snap["rest"])

    def _rebuild_summary(self) -> None:
        t0 = time.perf_counter()
        s = summary.build_summary(self.store)
        self.store.set_cache("summary", s)
        self.status["summary_s"] = round(time.perf_counter() - t0, 3)

    def _run(self) -> None:
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), self._nice)
        except (AttributeError, OSError):
            pass
        st = self.status
        try:
            rec = self.store.close_open()
            n = self.store.reanalyse()
            if rec["rides_closed"] or n["rides"] or n["rests"]:
                logger.info("Battery: start-up recovery: %d open ride(s) closed, %d ride(s) / %d rest(s) re-analysed",
                            rec["rides_closed"], n["rides"], n["rests"])
            self._collector = Collector(self._on_ride, self._on_rest, source="live")
            self._decoder = FrameDecoder(self._collector)
            self._reanchor()
            if rec["rest"] is not None and time.time() >= rec["rest"]["meta"]["start_ts"]:
                self._collector.restore_rest(rec["rest"])
            self._rebuild_summary()
        except Exception as e:
            logger.exception("Battery: start-up failed; module disabled")
            st["last_error"] = repr(e)
            return
        st["running"] = True
        logger.info("Battery health collector started (db=%s)", self.db_path)
        next_spool = time.monotonic() + SPOOL_INTERVAL_S
        col, dec, q = self._collector, self._decoder, self._queue
        while not self._stop.is_set():
            try:
                if col.ride is None and not q:
                    self._reanchor()
                n = len(q)
                for _ in range(n):
                    bt, dev, data = q.popleft()
                    dec.feed(self._wall(bt), dev, data)
                col.tick(self._wall(_boottime()))
                st["queue"] = len(q)
                st["ride_open"] = col.ride is not None
                st["counts"] = dec.counts
                if time.monotonic() >= next_spool:
                    next_spool = time.monotonic() + SPOOL_INTERVAL_S
                    self._spool()
                if self._recompute.is_set():
                    self._recompute.clear()
                    self._rebuild_summary()
            except Exception as e:
                st["last_error"] = repr(e)
                logger.exception("Battery: worker error")
            self._stop.wait(0.5)
        # shutdown: keep what is open (a restart mid-ride continues as a new ride)
        try:
            col.finish()
            self._spool()
        except Exception:
            logger.exception("Battery: shutdown flush failed")
        st["running"] = False

    # ── API helpers (any thread) ─────────────────────────────────────────────

    def get_summary(self) -> Optional[dict]:
        s = self.store.get_cache("summary")
        if s is not None:
            s["live"] = {k: self.status[k] for k in ("running", "ride_open", "queue", "rides_stored",
                                                     "last_error", "summary_s", "analysis_s", "counts")}
            s["live"]["dropped"] = self.dropped
        return s


_ACTIVE: Optional[BatteryHealthService] = None


def active() -> Optional[BatteryHealthService]:
    return _ACTIVE


def set_active(svc: Optional[BatteryHealthService]) -> None:
    global _ACTIVE
    _ACTIVE = svc


def wire_battery_health(twin, db_path: str) -> Optional[BatteryHealthService]:
    """Create the service, tap the ingress and register it for the API.
    ``BACKEND_BATTERY=0`` disables the module."""
    if os.environ.get("BACKEND_BATTERY", "1").strip().lower() in ("0", "off", "false", "no"):
        logger.info("Battery health module disabled (BACKEND_BATTERY=0)")
        return None
    svc = BatteryHealthService(db_path)
    twin.ingress.add_message_log_callback(svc.tap)
    set_active(svc)
    return svc
