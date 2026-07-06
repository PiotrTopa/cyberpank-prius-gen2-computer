"""
USB serial hotplug monitor.

Watches the set of candidate serial ports (see :mod:`.discovery`) and invokes a
callback whenever devices are added or removed, so the backend can re-discover
roles and retarget its :class:`SerialPort` instances after a re-plug. Uses plain
polling (no pyudev dependency) which is more than fast enough for occasional USB
events and works everywhere, including the postmarketOS phone running the car
computer.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Iterable, List, Optional, Set

from .discovery import enumerate_candidates

logger = logging.getLogger(__name__)

# Callback signature: (added, removed, current)
OnChange = Callable[[Set[str], Set[str], List[str]], None]


class UsbSerialMonitor:
    """Poll the serial candidate set and report additions/removals.

    Parameters
    ----------
    on_change:
        Called as ``on_change(added, removed, current)`` whenever the candidate
        set changes. ``added``/``removed`` are sets of paths; ``current`` is the
        full sorted candidate list.
    interval:
        Poll period in seconds.
    enumerator:
        Injectable enumeration function (defaults to
        :func:`discovery.enumerate_candidates`); handy for tests.
    """

    def __init__(
        self,
        on_change: OnChange,
        interval: float = 2.0,
        enumerator: Callable[[], List[str]] = enumerate_candidates,
    ) -> None:
        self._on_change = on_change
        self._interval = interval
        self._enumerate = enumerator
        self._known: Set[str] = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def prime(self, known: Iterable[str]) -> None:
        """Seed the known set so the first poll only reports genuine changes."""
        self._known = set(known)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="usb-serial-monitor", daemon=True
        )
        self._thread.start()
        logger.info("USB serial monitor started (interval=%.1fs)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._interval + 1.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                current = set(self._enumerate())
            except Exception as exc:  # noqa: BLE001 - never kill the poller
                logger.debug("Enumeration failed: %s", exc)
                self._stop.wait(self._interval)
                continue
            added = current - self._known
            removed = self._known - current
            if added or removed:
                self._known = current
                logger.info(
                    "USB serial change: +%s -%s", sorted(added), sorted(removed)
                )
                try:
                    self._on_change(added, removed, sorted(current))
                except Exception as exc:  # noqa: BLE001 - callback must not crash poller
                    logger.exception("USB change handler error: %s", exc)
            self._stop.wait(self._interval)
