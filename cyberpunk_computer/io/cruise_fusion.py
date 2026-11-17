"""
Cruise control state from four sources (docs/CRUISE_CONTROL.md, decoded on
the 2026-11-16/17 cruise drives; latencies measured 2026-11-17 with the
panda reader fix).

==============  ===================  ============  ==========================
signal          source               rate          note
==============  ===================  ============  ==========================
main switch     CAN 0x5C8 C 0x10     1 s + change  first: 40-60 ms before BEAN
                BEAN 62:C8 b1 0x10   1 s + change  gateway copy of the same bit
                21D3 D bits 0-3      1 Hz poll     all four set together
engaged         CAN 0x120 E 0x80     17 ms         first (C)
                21D3 D bit 6         1 Hz poll     0.04-1.1 s later: confirms
set speed       21D3 B               1 Hz poll     only source; floored km/h
resume memory   21D3 B               1 Hz poll     kept after a cancel
drive request   21D3 C               1 Hz poll     raw 0..~107 (probable)
switches        21D3 E               1 Hz sample   0x10 SET/COAST, 0x08
                                                   RES/ACC, 0x07 brake
==============  ===================  ============  ==========================

Main switch and engaged go through the shared
:class:`~cyberpunk_computer.comm.source_priority.SourceArbiter` (policies
``cruise_main`` and ``cruise_active``): the best fresh source wins, the next
one takes over when it goes stale. CAN 0x120 is preferred for "engaged"
because it arrives first; the set speed is shown only once a 21D3 reply that
itself says "engaged" confirms it (the B of the poll before an engage is the
old memory).

Resume memory (docs/CRUISE_CONTROL.md §5): the ECU sets B to the vehicle
speed at the moment of a cancel (P, 6 of 6) and clears it at 21D3 A <= 32
km/h (C) and with the main switch (C). The fusion applies both at once
from the broadcast speed (0x0B4) and the 0x120 cancel edge, and the next
21D3 reply confirms or corrects it.

21D3 E is a sample of the HV ECU's inputs, not a latch: the switch fields
show what the last poll saw. E = 0x02 at power-off and 0x01 alone while
driving are not used (unexplained). The 0x5C8 header bit 7 (A4 = the
gateway's immediate-copy header) is not a signal.

When every source has been silent for :data:`STALE_S` (car off, gateway or
powerbox gone) the whole cruise state goes back to unknown.

Read-only: the system informs the driver; nothing here touches the car.
"""

from __future__ import annotations

import math
import time
from typing import Callable, Dict, List, Optional

from ..comm.source_priority import SourceArbiter
from ..state.actions import Action, ActionSource, SetCruiseControlAction

#: All cruise sources silent for this long -> cruise state unknown.
STALE_S = 5.0

#: The ECU clears the resume memory once 21D3 A (integer km/h, the floor of
#: the speed) is <= 32: kept at 36 and 38, cleared at 32, 30, 29, 22, 31
#: (docs/CRUISE_CONTROL.md §5). Below this true speed it is gone.
MEMORY_CLEAR_BELOW_KMH = 33.0

#: At a cancel the memory is predicted from the speed only when it differs
#: from the set speed by at least this much (km/h).
MEMORY_PREDICT_MIN_KMH = 2

#: A speed sample older than this is not used for the memory rules.
SPEED_MAX_AGE_S = 1.0


class CruiseFusion:
    def __init__(self, sources: SourceArbiter,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.sources = sources
        self._clock = clock
        self._seen: Dict[str, float] = {}
        self._known = False
        self._active: Optional[bool] = None     # fused engaged state
        self._main: Optional[bool] = None       # fused main switch
        self._memory: Optional[int] = None      # resume memory as dispatched (0 = none)
        self._speed: Optional[float] = None     # true speed km/h (0x0B4)
        self._speed_t: Optional[float] = None

    def _mark(self, src: str, now: float) -> None:
        self._seen[src] = now
        self._known = True

    def _action(self, **kw) -> List[Action]:
        kw = {k: v for k, v in kw.items() if v is not None}
        return [SetCruiseControlAction(source=ActionSource.GATEWAY, **kw)] if kw else []

    def _speed_now(self, now: float) -> Optional[float]:
        if self._speed is None or self._speed_t is None or now - self._speed_t > SPEED_MAX_AGE_S:
            return None
        return self._speed

    def _set_memory(self, kw: dict, value: int) -> None:
        self._memory = value
        kw["cruise_memory_speed"] = value       # 0 -> None in the reducer

    def _set_main(self, main: bool) -> None:
        self._main = main
        if not main:                    # main off: no memory (reducer clears the state)
            self._memory = 0
            self._active = False

    def _apply_active(self, kw: dict, active: bool, now: float) -> None:
        """Fused engaged state changed or refreshed."""
        was = self._active
        self._active = active
        kw["cruise_active"] = active
        if was and not active:
            # Cancel: the ECU replaces the memory with the speed at the
            # cancel (P). Predicted here only where the two clearly differ
            # (>= 2 km/h: 66 -> 63, 61 -> 58 on the drives); a 1 km/h
            # difference is within B's flooring (54 kept at 53.x, 48 -> 49),
            # so the next 21D3 reply (<= 1 s) decides.
            v = self._speed_now(now)
            if v is not None and self._main is not False:
                if v < MEMORY_CLEAR_BELOW_KMH:
                    self._set_memory(kw, 0)
                elif self._memory and abs(int(math.floor(v)) - self._memory) >= MEMORY_PREDICT_MIN_KMH:
                    self._set_memory(kw, int(math.floor(v)))

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
              "cruise_switch_set_coast": values.get("cruise_switch_set_coast"),
              "cruise_switch_res_acc": values.get("cruise_switch_res_acc"),
              "cruise_brake_inputs": values.get("cruise_brake_inputs")}
        if b is not None:
            self._set_memory(kw, b)
        if main is not None and self.sources.offer("cruise_main", "obd", now):
            self._set_main(main)
            kw["cruise_main_switch"] = main
        if active is not None and self.sources.offer("cruise_active", "obd", now):
            self._apply_active(kw, active, now)
            if b is not None and active:
                self._set_memory(kw, b)
        # Set speed: only from a reply that itself says "engaged", while the
        # fused state (0x120 first) is engaged too.
        if active and b and self._active:
            kw["cruise_set_speed"] = b
        return self._action(**kw)

    def from_bean_62c8(self, main: bool, now: Optional[float] = None) -> List[Action]:
        now = self._clock() if now is None else now
        self._mark("bean", now)
        if self.sources.offer("cruise_main", "bean", now):
            self._set_main(main)
            return self._action(cruise_main_switch=main)
        return []

    def from_can_5c8(self, main: bool, now: Optional[float] = None) -> List[Action]:
        now = self._clock() if now is None else now
        self._mark("can5c8", now)
        if self.sources.offer("cruise_main", "can", now):
            self._set_main(main)
            return self._action(cruise_main_switch=main)
        return []

    def from_can_120(self, active: bool, now: Optional[float] = None) -> List[Action]:
        now = self._clock() if now is None else now
        self._mark("can120", now)
        if self.sources.offer("cruise_active", "can", now):
            if active == self._active:
                return []
            kw: dict = {}
            self._apply_active(kw, active, now)
            return self._action(**kw)
        return []

    def observe_speed(self, kmh: float, now: Optional[float] = None) -> List[Action]:
        """True vehicle speed (CAN 0x0B4). Clears the resume memory below
        :data:`MEMORY_CLEAR_BELOW_KMH` while not engaged (the ECU does the
        same; the next 21D3 reply confirms)."""
        now = self._clock() if now is None else now
        self._speed = kmh
        self._speed_t = now
        if self._memory and not self._active and kmh < MEMORY_CLEAR_BELOW_KMH:
            kw: dict = {}
            self._set_memory(kw, 0)
            return self._action(**kw)
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
        self._active = None
        self._main = None
        self._memory = None
        return [SetCruiseControlAction(clear=True, source=ActionSource.GATEWAY)]
