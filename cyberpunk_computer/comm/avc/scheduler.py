"""
Tiny timer scheduler for AVC-LAN transmissions (nav emulator replies with
millisecond delays, 5 s heartbeats, climate press → release timing).

:class:`ThreadScheduler` runs callbacks on one daemon thread;
:class:`ManualScheduler` is driven by tests (``advance(dt)``).
Both expose ``call_later(delay, fn)`` and ``now()``.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from typing import Callable, List, Tuple

logger = logging.getLogger(__name__)


class ThreadScheduler:
    def __init__(self, name: str = "avc-scheduler") -> None:
        self._heap: List[Tuple[float, int, Callable[[], None]]] = []
        self._seq = itertools.count()
        self._cond = threading.Condition()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @staticmethod
    def now() -> float:
        return time.monotonic()

    def call_later(self, delay: float, fn: Callable[[], None]) -> None:
        with self._cond:
            heapq.heappush(self._heap, (time.monotonic() + max(0.0, delay), next(self._seq), fn))
            self._cond.notify()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._stop:
                    if not self._heap:
                        self._cond.wait()
                        continue
                    due = self._heap[0][0] - time.monotonic()
                    if due <= 0:
                        break
                    self._cond.wait(due)
                if self._stop:
                    return
                _, _, fn = heapq.heappop(self._heap)
            try:
                fn()
            except Exception:
                logger.exception("AVC scheduler callback failed")


class ManualScheduler:
    """Deterministic scheduler for tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start
        self._heap: List[Tuple[float, int, Callable[[], None]]] = []
        self._seq = itertools.count()

    def now(self) -> float:
        return self._t

    def call_later(self, delay: float, fn: Callable[[], None]) -> None:
        heapq.heappush(self._heap, (self._t + max(0.0, delay), next(self._seq), fn))

    def advance(self, dt: float) -> None:
        end = self._t + dt
        while self._heap and self._heap[0][0] <= end:
            t, _, fn = heapq.heappop(self._heap)
            self._t = max(self._t, t)
            fn()
        self._t = end

    def stop(self) -> None:
        self._heap.clear()
