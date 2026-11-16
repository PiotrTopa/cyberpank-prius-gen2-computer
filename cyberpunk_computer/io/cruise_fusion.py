"""
Cruise control state from four sources (docs/BEAN_SIGNALS.md §1, decoded on
the 2026-11-16 cruise drive).

==============  ==================  ============  ===========================
signal          source              rate          note
==============  ==================  ============  ===========================
main switch     BEAN 62:C8 b1 0x10  1 s + change  fastest (~0.3 s)
                21D3 D bits 0-3     1 Hz poll     all four set together
                CAN 0x5C8 b2 0x10   ~0.4 s        gateway copy of 62:C8, but
                                                  its content lags ~3 s
engaged         21D3 D bit 6        1 Hz poll     at the poll
                CAN 0x120 b4 0x80   0.26 s        ~2 s behind 21D3
set speed       21D3 B              1 Hz poll     km/h; kept after a cancel
drive request   21D3 C              1 Hz poll     raw 0..~110 (probable)
==============  ==================  ============  ===========================

Main switch and engaged go through the shared
:class:`~cyberpunk_computer.comm.source_priority.SourceArbiter` (policies
``cruise_main`` and ``cruise_active``): the best fresh source wins, the next
one takes over when it goes stale. 21D3 is preferred for "engaged" because
0x120 runs two seconds late at engage and cancel.

The set speed is shown only while engaged; B outside that is the resume
(memory) speed. When every source has been silent for :data:`STALE_S` (car
off, gateway or powerbox gone) the whole cruise state goes back to unknown.

Read-only: the system informs the driver; nothing here touches the car.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional

from ..comm.source_priority import SourceArbiter
from ..state.actions import Action, ActionSource, SetCruiseControlAction

#: All cruise sources silent for this long -> cruise state unknown.
STALE_S = 5.0


class CruiseFusion:
    def __init__(self, sources: SourceArbiter,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.sources = sources
        self._clock = clock
        self._seen: Dict[str, float] = {}
        self._known = False

    def _mark(self, src: str, now: float) -> None:
        self._seen[src] = now
        self._known = True

    def _action(self, **kw) -> List[Action]:
        kw = {k: v for k, v in kw.items() if v is not None}
        return [SetCruiseControlAction(source=ActionSource.GATEWAY, **kw)] if kw else []

    # -- sources ---------------------------------------------------------------

    def from_21d3(self, values: dict, now: Optional[float] = None) -> List[Action]:
        """Decoded 21D3 reply (comm.solicited_can._hybrid_21d3_parser keys)."""
        if "cruise_status_raw" not in values:
            return []
        now = self._clock() if now is None else now
        self._mark("obd", now)
        main = values.get("cruise_main_switch")
        active = values.get("cruise_active")
        b = values.get("cruise_set_speed")
        kw = {"cruise_drive_request": values.get("cruise_drive_request"),
              "cruise_memory_speed": b}
        if main is not None and self.sources.offer("cruise_main", "obd", now):
            kw["cruise_main_switch"] = main
        if active is not None and self.sources.offer("cruise_active", "obd", now):
            kw["cruise_active"] = active
            if active and b:
                kw["cruise_set_speed"] = b
        return self._action(**kw)

    def from_bean_62c8(self, main: bool, now: Optional[float] = None) -> List[Action]:
        now = self._clock() if now is None else now
        self._mark("bean", now)
        if self.sources.offer("cruise_main", "bean", now):
            return self._action(cruise_main_switch=main)
        return []

    def from_can_5c8(self, main: bool, now: Optional[float] = None) -> List[Action]:
        now = self._clock() if now is None else now
        self._mark("can5c8", now)
        if self.sources.offer("cruise_main", "can", now):
            return self._action(cruise_main_switch=main)
        return []

    def from_can_120(self, active: bool, now: Optional[float] = None) -> List[Action]:
        now = self._clock() if now is None else now
        self._mark("can120", now)
        if self.sources.offer("cruise_active", "can", now):
            return self._action(cruise_active=active)
        return []

    # -- staleness -------------------------------------------------------------

    def expire(self, now: Optional[float] = None) -> List[Action]:
        """Every source silent for :data:`STALE_S` -> one clear action."""
        if not self._known:
            return []
        now = self._clock() if now is None else now
        if any(now - t <= STALE_S for t in self._seen.values()):
            return []
        self._known = False
        self._seen.clear()
        return [SetCruiseControlAction(clear=True, source=ActionSource.GATEWAY)]
