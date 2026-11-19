"""USB tree watch: notices when the top hub (``1-1``) re-enumerates.

A whole-tree drop (the top hub leaves the bus and comes back with a new device
number, every device behind it re-enumerating) is a host-side event. While the
tree is unstable, link watchdogs that cut power to a board (the MFD Pi's
"unreachable in ON" cycle) only add relay transients and hard power cuts; they
must wait until the tree has been quiet for their whole window.

The watch is a cheap sysfs poll (one small file read per call): the top hub's
``devnum`` changes on every re-enumeration, and the hub directory vanishes while
it is gone. Seen on the car 2026-11-19: up to 2.5 tree drops per minute.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable, Deque, Optional

logger = logging.getLogger(__name__)

SYSFS_USB = "/sys/bus/usb/devices"


class UsbTreeWatch:
    """Counts re-enumerations of one hub (default the top hub ``1-1``).

    Call :meth:`poll` from the backend loop (it self-limits to ``poll_s``);
    consumers ask :meth:`last_reset_age` / :meth:`resets_within`.
    """

    def __init__(
        self,
        hub: str = "1-1",
        poll_s: float = 1.0,
        storm_n: int = 3,
        storm_window_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        read_devnum: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        self.hub = hub
        self.poll_s = poll_s
        self.storm_n = storm_n
        self.storm_window_s = storm_window_s
        self._clock = clock
        self._read = read_devnum or self._default_read
        self._devnum: Optional[str] = None
        self._seen = False            # a devnum has been read at least once
        self._gone = False            # hub absent at the last poll
        self._last_poll = 0.0
        self._resets: Deque[float] = deque(maxlen=256)
        self.total_resets = 0
        self._storm = False

    @staticmethod
    def _default_read(hub: str) -> Optional[str]:
        try:
            with open(f"{SYSFS_USB}/{hub}/devnum") as f:
                return f.read().strip()
        except OSError:
            return None

    def poll(self) -> None:
        now = self._clock()
        if self._last_poll and now - self._last_poll < self.poll_s:
            return
        self._last_poll = now
        devnum = self._read(self.hub)
        if devnum is None:
            if self._seen and not self._gone:
                # Gone: count the reset now (the return may take seconds, or
                # never come if the host controller died).
                self._gone = True
                self._record(now, "left the bus")
            return
        if self._seen and not self._gone and devnum != self._devnum:
            # Left and came back between two polls.
            self._record(now, "re-enumerated")
        self._gone = False
        self._seen = True
        self._devnum = devnum

    def _record(self, now: float, what: str) -> None:
        self._resets.append(now)
        self.total_resets += 1
        n = self.resets_within(self.storm_window_s)
        logger.warning(
            "USB tree: hub %s %s (reset #%d, %d in the last %.0f min)",
            self.hub, what, self.total_resets, n, self.storm_window_s / 60.0)
        storm = n >= self.storm_n
        if storm and not self._storm:
            logger.error(
                "USB tree STORM: hub %s reset %d times in %.0f min — power or "
                "cabling of the hub tree; power-cycle watchdogs held back",
                self.hub, n, self.storm_window_s / 60.0)
        self._storm = storm

    def last_reset_age(self) -> Optional[float]:
        """Seconds since the last reset; 0 while the hub is gone; None if never."""
        if self._gone:
            return 0.0
        if not self._resets:
            return None
        return self._clock() - self._resets[-1]

    def resets_within(self, window_s: float) -> int:
        now = self._clock()
        return sum(1 for t in self._resets if now - t <= window_s)

    def storm(self) -> bool:
        if self._storm and self.resets_within(self.storm_window_s) < self.storm_n:
            logger.warning("USB tree: storm over (hub %s quiet for a while)", self.hub)
            self._storm = False
        return self._storm

    def status(self) -> dict:
        age = self.last_reset_age()
        return {
            "hub": self.hub,
            "present": not self._gone and self._seen,
            "resets_total": self.total_resets,
            "resets_10min": self.resets_within(600.0),
            "last_reset_age_s": None if age is None else round(age, 1),
            "storm": self.storm(),
        }
