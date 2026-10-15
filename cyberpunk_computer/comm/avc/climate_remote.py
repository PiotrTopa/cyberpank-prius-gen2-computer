"""
Remote climate control over AVC-LAN (verified in the car 2026-10-14).

The backend sends the display's own climate touch frames
(AVC_MAP.md "Climate touch buttons" / "Remote climate control"):

    press:   110->1C6  00 25 E0 84 B1 B2 B3 B4 B5 B6
    release: 110->1C6  00 25 E0 84 00 00 00 00 00 00   (≈100 ms later)

from ONE serial connection (two separate gwctl invocations were too slow and
the ECU auto-repeated). One press = one step; a 0.6 s hold auto-repeats, so
presses are strictly serialised through a small queue and never overlap.

Timing: ``PRESS_S`` (press → release) is the verified ≈100 ms. ``GAP_S`` (idle
time after a release before the next press) is NOT measured; 0.35 s is chosen
to stay clear of the auto-repeat while the display's own presses in
``climate_setpoint_touch.ndjson`` were ≥1 s apart. Adjust if the car disagrees.

Toggle buttons (A/C, recirculation, rear defogger, front defrost) are only
pressed when the last climate status (``E0 5D F3``) says the state differs.
"""

from __future__ import annotations

import collections
import logging
import threading
from typing import Callable, Deque, Dict, Optional, Tuple

from .decoder import CLIMATE_BUTTONS, ClimateSetpoint, ClimateStatus
from .frames import CLIMATE, DISPLAY, tx_payload

logger = logging.getLogger(__name__)

SendFn = Callable[[int, dict], bool]

PRESS_PREFIX = (0x00, 0x25, 0xE0, 0x84)
RELEASE = PRESS_PREFIX + (0, 0, 0, 0, 0, 0)

#: legacy SetAirDirectionAction ints (0=FACE, 1=FACE+FEET, 2=FEET, 3=DEFROST)
AIR_DIRECTION_BUTTONS = {
    0: "vent_face",
    1: "vent_face_foot",
    2: "vent_foot",
    3: "vent_windscreen_foot",
}

#: ClimateStatus.vent → button
VENT_BUTTONS = {
    "face": "vent_face",
    "face_foot": "vent_face_foot",
    "foot": "vent_foot",
    "windscreen_foot": "vent_windscreen_foot",
}

BUTTON_NAMES: Tuple[str, ...] = tuple(CLIMATE_BUTTONS.keys())


def press_frame(button: str) -> Tuple[int, ...]:
    return PRESS_PREFIX + tuple(CLIMATE_BUTTONS[button])


class ClimateRemote:
    PRESS_S = 0.10          # verified: release ≈100 ms after the press
    GAP_S = 0.35            # unverified choice, see module doc
    MAX_QUEUE = 16
    MAX_TEMP_STEPS = 30
    CONFIRM_TIMEOUT_S = 1.2  # temperature loop: give up if the setpoint does not move

    def __init__(self, send: SendFn, scheduler, status_max_age: float = 15.0) -> None:
        self._send = send
        self._sched = scheduler
        self._lock = threading.RLock()
        self._queue: Deque[str] = collections.deque()
        self._busy = False
        self.inflight: Optional[str] = None
        self.status: Optional[ClimateStatus] = None
        self.status_t: float = -1e9
        self.setpoint: Optional[ClimateSetpoint] = None
        self.setpoint_t: float = -1e9
        self.status_max_age = status_max_age
        self.sent = 0
        self.failed = 0
        # temperature closed loop
        self._temp_target: Optional[float] = None
        self._temp_steps = 0
        self._temp_press_t: Optional[float] = None
        self._temp_press_raw: Optional[int] = None

    # ── feedback from the decoder ─────────────────────────────────────────

    def on_update(self, update) -> None:
        with self._lock:
            now = self._sched.now()
            if isinstance(update, ClimateStatus):
                self.status, self.status_t = update, now
            elif isinstance(update, ClimateSetpoint):
                self.setpoint, self.setpoint_t = update, now

    def _fresh_status(self) -> Optional[ClimateStatus]:
        if self.status is None or self._sched.now() - self.status_t > self.status_max_age:
            return None
        return self.status

    def _fresh_setpoint(self) -> Optional[ClimateSetpoint]:
        if self.setpoint is None or self._sched.now() - self.setpoint_t > self.status_max_age:
            return None
        return self.setpoint

    # ── raw button presses ────────────────────────────────────────────────

    def press(self, button: str, count: int = 1) -> int:
        """Queue ``count`` presses of ``button``. Returns the number queued."""
        if button not in CLIMATE_BUTTONS:
            raise ValueError(f"unknown climate button {button!r}; known: {', '.join(BUTTON_NAMES)}")
        with self._lock:
            n = 0
            for _ in range(max(1, int(count))):
                if len(self._queue) >= self.MAX_QUEUE:
                    logger.warning("climate queue full, dropping %s", button)
                    break
                self._queue.append(button)
                n += 1
            self._pump()
            return n

    def clear(self) -> None:
        with self._lock:
            self._queue.clear()
            self._temp_target = None

    @property
    def queued(self) -> int:
        return len(self._queue)

    def _queued_or_inflight(self, button: str) -> bool:
        return self.inflight == button or button in self._queue

    def _pump(self) -> None:
        if self._busy:
            return
        if not self._queue:
            self._temp_loop()
            if not self._queue:
                return
        button = self._queue.popleft()
        self._busy = True
        self.inflight = button
        ok = self._send(2, tx_payload(DISPLAY, CLIMATE, press_frame(button)))
        if ok:
            self.sent += 1
            logger.info("climate press %s", button)
        else:
            self.failed += 1
            logger.warning("climate press %s: gateway not connected", button)
        if button in ("temp_up", "temp_down"):
            sp = self.setpoint
            self._temp_press_t = self._sched.now()
            self._temp_press_raw = sp.raw if sp is not None else None
        # Always release (even if the press may have failed: harmless all-00 frame).
        self._sched.call_later(self.PRESS_S, self._release)

    def _release(self) -> None:
        with self._lock:
            self._send(2, tx_payload(DISPLAY, CLIMATE, RELEASE))
            self.inflight = None
            self._sched.call_later(self.GAP_S, self._done)

    def _done(self) -> None:
        with self._lock:
            self._busy = False
            self._pump()

    # ── temperature closed loop ───────────────────────────────────────────

    def set_temperature(self, celsius: float) -> bool:
        """Step the setpoint to ``celsius`` (0.5 °C steps) one press at a time,
        re-checking ``E0 5D F5`` after every press."""
        with self._lock:
            sp = self._fresh_setpoint()
            if sp is None or sp.celsius is None:
                logger.warning("climate set_temperature(%.1f): setpoint unknown or climate OFF", celsius)
                return False
            self._temp_target = round(float(celsius) * 2) / 2
            self._temp_steps = 0
            self._temp_press_t = None
            # drop stale queued temp presses; the loop decides from now on
            self._queue = collections.deque(b for b in self._queue if b not in ("temp_up", "temp_down"))
            self._pump()
            return True

    def _temp_loop(self) -> None:
        target = self._temp_target
        if target is None:
            return
        sp = self._fresh_setpoint()
        if sp is None or sp.celsius is None:
            self._temp_target = None
            return
        diff = target - sp.celsius
        if abs(diff) < 0.25:
            logger.info("climate setpoint reached %.1f", sp.celsius)
            self._temp_target = None
            return
        if self._temp_press_t is not None and sp.raw == self._temp_press_raw:
            # last press not confirmed yet
            if self._sched.now() - self._temp_press_t > self.CONFIRM_TIMEOUT_S:
                logger.warning("climate setpoint stuck at %.1f (target %.1f), giving up", sp.celsius, target)
                self._temp_target = None
            else:
                self._sched.call_later(0.1, self._wake)
            return
        if self._temp_steps >= self.MAX_TEMP_STEPS:
            self._temp_target = None
            return
        self._temp_steps += 1
        self._queue.append("temp_up" if diff > 0 else "temp_down")

    def _wake(self) -> None:
        with self._lock:
            self._pump()

    # ── state-level helpers (used by set_climate commands) ────────────────

    def _toggle_to(self, button: str, current: Optional[bool], want: bool) -> bool:
        if current is None:
            logger.warning("climate %s: status unknown, not toggling blind", button)
            return False
        if current == want or self._queued_or_inflight(button):
            return True
        self.press(button)
        return True

    def set_ac(self, on: bool) -> bool:
        st = self._fresh_status()
        return self._toggle_to("ac", st.ac if st else None, on)

    def set_recirculation(self, on: bool) -> bool:
        # The recirculation button is assumed to toggle recirc/fresh (only the
        # fresh -> recirc direction is in the captures: map3 mark4 43 -> 23).
        st = self._fresh_status()
        return self._toggle_to("recirc", st.recirculation if st else None, on)

    def set_rear_defogger(self, on: bool) -> bool:
        st = self._fresh_status()
        return self._toggle_to("rear_defogger", st.rear_defogger if st else None, on)

    def set_front_defrost(self, on: bool) -> bool:
        st = self._fresh_status()
        return self._toggle_to("front_defrost", st.front_defrost if st else None, on)

    def set_auto(self, on: bool) -> bool:
        if not on:
            # No verified "AUTO off" button: AUTO is left by any manual
            # setting (fan / vent / A/C) — the caller must pick one.
            logger.warning("climate set_auto(False): no AUTO-off button on the bus; ignored")
            return False
        st = self._fresh_status()
        if st is not None and st.auto and st.system_on:
            return True
        self.press("auto")
        return True

    def set_fan(self, speed: int) -> bool:
        speed = int(speed)
        if speed == 0:
            self.press("off")           # fan 0 = climate system OFF (F3 41 00 .. 00)
            return True
        if not 1 <= speed <= 7:
            return False
        self.press(f"fan_{speed}")
        return True

    def set_vent(self, mode: str) -> bool:
        button = VENT_BUTTONS.get(mode, mode if mode in CLIMATE_BUTTONS else None)
        if button is None or not button.startswith("vent_"):
            return False
        self.press(button)
        return True

    def set_air_direction(self, direction: int) -> bool:
        button = AIR_DIRECTION_BUTTONS.get(int(direction))
        if button is None:
            return False
        self.press(button)
        return True

    def describe(self) -> Dict[str, object]:
        return {"queued": len(self._queue), "inflight": self.inflight, "sent": self.sent,
                "failed": self.failed, "temp_target": self._temp_target}
