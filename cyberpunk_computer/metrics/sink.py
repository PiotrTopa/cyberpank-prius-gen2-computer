"""
Metrics sink — samples the Store into the tiered SQLite store on its own thread.

Why a dedicated thread with its own SQLite connection:
- The Store is single-threaded; its subscribers run on the engine loop. Doing
  disk I/O there would stall ingress. Instead the sink reads ``store.state``
  (an immutable, freely shareable snapshot) on its own cadence.
- SQLite connections are per-thread, so the writer owns exactly one connection.

Each tick (default 1 Hz) the sink:
  1. snapshots ``store.state``,
  2. appends one raw sample per known signal (batched in a transaction),
  3. diffs against the previous snapshot to emit discrete events.

Periodically it cascades rollups (raw→1min→1hour→1day) and prunes old data.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from ..state.app_state import AppState
from ..state.store import Store
from .database import MetricsDatabase
from .signals import detect_events, sample_all

logger = logging.getLogger(__name__)


class MetricsSink:
    """Background sampler that persists Store state into a :class:`MetricsDatabase`."""

    def __init__(
        self,
        store: Store,
        db: MetricsDatabase,
        sample_interval: float = 1.0,
        rollup_interval: float = 60.0,
        prune_interval: float = 3600.0,
    ) -> None:
        self._store = store
        self._db = db
        self._sample_interval = max(0.1, sample_interval)
        self._rollup_interval = max(5.0, rollup_interval)
        self._prune_interval = max(60.0, prune_interval)

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._prev_state: Optional[AppState] = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background sampling thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="metrics-sink", daemon=True
        )
        self._thread.start()
        logger.info(
            "Metrics sink started (sample=%.1fs, db=%s)",
            self._sample_interval, self._db.path,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the thread to stop and wait briefly for it to drain."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    # ── worker ────────────────────────────────────────────────────────────

    def _run(self) -> None:
        conn = self._db.connect()
        next_rollup = time.time() + self._rollup_interval
        next_prune = time.time() + self._prune_interval
        try:
            while not self._stop.is_set():
                tick_start = time.time()
                try:
                    self._sample_once(conn, tick_start)
                except Exception:
                    logger.exception("Metrics sample failed")

                now = time.time()
                if now >= next_rollup:
                    next_rollup = now + self._rollup_interval
                    try:
                        self._db.rollup(conn, now)
                    except Exception:
                        logger.exception("Metrics rollup failed")
                if now >= next_prune:
                    next_prune = now + self._prune_interval
                    try:
                        self._db.prune(conn, now)
                    except Exception:
                        logger.exception("Metrics prune failed")

                # Sleep the remainder of the interval, staying responsive to stop.
                elapsed = time.time() - tick_start
                self._stop.wait(max(0.0, self._sample_interval - elapsed))
        finally:
            conn.close()
            logger.info("Metrics sink stopped")

    def _sample_once(self, conn, ts: float) -> None:
        state = self._store.state
        samples = sample_all(state, ts)
        if samples:
            self._db.write_samples(conn, samples)
        events = detect_events(self._prev_state, state, ts)
        if events:
            self._db.write_events(conn, events)
            for _, etype, detail in events:
                logger.debug("event: %s=%s", etype, detail)
        self._prev_state = state
