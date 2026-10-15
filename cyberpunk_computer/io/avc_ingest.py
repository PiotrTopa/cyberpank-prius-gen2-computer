"""
AVC-LAN ingest: gateway ID 2 lines → typed updates → store actions + events.

Decoding is done by :mod:`cyberpunk_computer.comm.avc` (AVC_MAP.md tables).
This module adds the state integration rules:

* **AVC priority.** Signals that AVC provides (outside temperature, climate,
  energy-flow arrows, engine running, battery bars / EV mode) are tracked with
  a freshness window (:data:`FRESH_S`). While AVC is fresh, the CAN/OBD
  ingest paths skip their competing actions (see ``IngressController``);
  when AVC goes stale they take over again.
* **On change only.** Periodic frames (outside temp 1 Hz, energy 2 Hz,
  climate status 5 s) dispatch only when the decoded value changes.
* **Events.** Calibrated touch and nav/info buttons are published on the
  backend event channel (ZMQ PUB ``{"type":"event","name":...}``):

  ``touch``  ``{"x": 0..479, "y": 0..239, "pressed": bool, "raw_x": int,
  "raw_y": int, "t": epoch}`` — every touch frame while the finger is down
  (10–15 Hz); the release (``00 00 00 00``) is ``pressed: false`` with the
  last known x/y (releases without a preceding press are dropped).

  ``button`` ``{"key": "menu"|"dest"|"map"|"maintenance"|"calendar"|"language",
  "pressed": bool, "t": epoch}`` — hardware keys send a release frame
  (``KK = 00``); the info soft buttons have none on the bus, so their
  ``pressed: false`` follows the press immediately.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Dict, List, Optional

from ..comm.avc import (
    AvcDecoder, AvcFrame, BatteryInfo, ClimateButtonFrame, ClimateSetpoint,
    ClimateStatus, DisplayScreen, EnergyFlow, Lights, NavButton, OutsideTemp,
    Touch,
)
from ..state.actions import (
    Action, ActionSource, AVCButtonPressAction, AVCTouchEventAction,
    SetAvcBatteryInfoAction, SetAvcClimateAction, SetAvcStateAction,
    SetEnergyFlowFlagsAction, SetICERunningAction, SetOutsideTempAction,
)

logger = logging.getLogger(__name__)

#: Seconds an AVC value stays authoritative after its last frame. Outside
#: temperature arrives at 1 Hz, energy at ~2 Hz, climate status every 5 s.
FRESH_S = 15.0

EventSink = Callable[[str, Dict], None]


class AvcIngest:
    def __init__(self, fresh_s: float = FRESH_S,
                 clock: Callable[[], float] = time.time) -> None:
        self.decoder = AvcDecoder()
        self.fresh_s = fresh_s
        self._clock = clock
        self._event_sink: Optional[EventSink] = None
        self._update_listeners: List[Callable[[object], None]] = []
        self._seen: Dict[str, float] = {}
        # last values (change detection)
        self._outside: Optional[int] = None
        self._climate: Optional[tuple] = None
        self._setpoint: Optional[int] = None
        self._energy: Optional[tuple] = None
        self._engine: Optional[bool] = None
        self._battery: Optional[tuple] = None
        self._screen: Optional[int] = None
        # touch / button tracking
        self.touch_down = False
        self.touch_x = 0
        self.touch_y = 0
        self.touch_raw_x = 0
        self.touch_raw_y = 0
        self._button_down: Optional[str] = None
        self.frames = 0
        self._last_frame_dispatch = 0.0

    # ── wiring ────────────────────────────────────────────────────────────

    def set_event_sink(self, sink: Optional[EventSink]) -> None:
        self._event_sink = sink

    def add_update_listener(self, fn: Callable[[object], None]) -> None:
        """Called with every typed update (e.g. the climate remote's status feed)."""
        self._update_listeners.append(fn)

    def fresh(self, signal: str) -> bool:
        """Is the AVC value for ``signal`` authoritative right now?

        Signals: ``outside_temp``, ``climate``, ``energy``, ``battery``.
        """
        t = self._seen.get(signal)
        return t is not None and (self._clock() - t) <= self.fresh_s

    def _emit(self, name: str, data: Dict) -> None:
        if self._event_sink is not None:
            try:
                self._event_sink(name, data)
            except Exception:
                logger.exception("AVC event sink failed")

    # ── processing ────────────────────────────────────────────────────────

    def process(self, d: dict) -> List[Action]:
        """Gateway ``d`` payload of an ID 2 line → actions to dispatch."""
        frame = AvcFrame.from_gateway(d)
        if frame is None:
            return []
        self.frames += 1
        now = self._clock()
        update = self.decoder.decode(frame)
        actions: List[Action] = []
        if update is not None:
            for fn in self._update_listeners:
                try:
                    fn(update)
                except Exception:
                    logger.exception("AVC update listener failed")
            actions.extend(self._to_actions(update, frame, now))
        # Link liveness, throttled to 1 Hz so it does not dispatch per frame.
        if now - self._last_frame_dispatch >= 1.0:
            self._last_frame_dispatch = now
            actions.append(SetAvcStateAction(last_frame_time=now))
        return actions

    def _to_actions(self, u, frame: AvcFrame, now: float) -> List[Action]:
        src = ActionSource.GATEWAY
        out: List[Action] = []

        if isinstance(u, OutsideTemp):
            self._seen["outside_temp"] = now
            if u.celsius != self._outside:
                self._outside = u.celsius
                out.append(SetOutsideTempAction(float(u.celsius), src, origin="avc"))

        elif isinstance(u, ClimateStatus):
            self._seen["climate"] = now
            key = (u.system_on, u.fan, u.auto, u.fresh_air, u.recirculation,
                   u.front_defrost, u.rear_defogger, u.ac, u.vent)
            if key != self._climate:
                self._climate = key
                out.append(SetAvcClimateAction(
                    fan_speed=u.fan, ac_on=u.ac, auto_mode=u.auto,
                    recirculation=u.recirculation, fresh_air=u.fresh_air,
                    defrost=u.front_defrost, rear_defogger=u.rear_defogger,
                    air_direction=u.vent, system_on=u.system_on))
                raw = " ".join(f"{b:02X}" for b in u.raw)
                out.append(SetAvcStateAction(climate_raw=raw))

        elif isinstance(u, ClimateSetpoint):
            self._seen["climate"] = now
            if u.raw != self._setpoint:
                self._setpoint = u.raw
                if u.celsius is None:
                    out.append(SetAvcClimateAction(system_on=False))
                else:
                    out.append(SetAvcClimateAction(target_temp=u.celsius))

        elif isinstance(u, EnergyFlow):
            self._seen["energy"] = now
            key = (u.f1, u.f2)
            if key != self._energy:
                self._energy = key
                out.append(SetEnergyFlowFlagsAction(
                    # legacy flags, mapped onto the MFD model
                    engine_to_wheels=u.engine_to_wheels,
                    battery_to_motor=u.battery_to_mg,
                    motor_to_battery=u.mg_to_battery,
                    engine_to_battery=u.engine_to_mg and u.mg_to_battery,
                    battery_to_wheels=u.battery_to_mg and u.mg_to_wheels,
                    source=src,
                    engine_to_mg=u.engine_to_mg, mg_to_wheels=u.mg_to_wheels,
                    wheels_to_mg=u.wheels_to_mg, battery_to_mg=u.battery_to_mg,
                    mg_to_battery=u.mg_to_battery, origin="avc"))
                out.append(SetAvcStateAction(energy_raw=f"{u.f1:02X} {u.f2:02X}"))
            if u.engine_running != self._engine:
                self._engine = u.engine_running
                out.append(SetICERunningAction(u.engine_running, src))

        elif isinstance(u, BatteryInfo):
            self._seen["battery"] = now
            key = (u.bars, u.ev_mode, u.ev_cancelled)
            if key != self._battery:
                self._battery = key
                out.append(SetAvcBatteryInfoAction(u.bars, u.ev_mode, u.ev_cancelled, src))

        elif isinstance(u, DisplayScreen):
            if u.code != self._screen:
                self._screen = u.code
                out.append(SetAvcStateAction(screen=f"{u.code:02X}", screen_name=u.name,
                                             nav_screen_active=u.code == 0x58))

        elif isinstance(u, Lights):
            out.append(SetAvcStateAction(lights_on=u.on))

        elif isinstance(u, Touch):
            out.extend(self._touch(u, frame, now))

        elif isinstance(u, NavButton):
            out.extend(self._button(u, frame, now))

        elif isinstance(u, ClimateButtonFrame):
            if u.buttons and not u.echo:
                logger.info("display climate touch: %s", ", ".join(u.buttons))

        return out

    def _touch(self, u: Touch, frame: AvcFrame, now: float) -> List[Action]:
        if u.pressed:
            self.touch_down = True
            self.touch_x, self.touch_y = u.x, u.y
            self.touch_raw_x, self.touch_raw_y = u.raw_x, u.raw_y
            kind = "press"
        else:
            if not self.touch_down:
                return []                   # stray release (e.g. at a screen switch)
            self.touch_down = False
            kind = "release"
        self._emit("touch", {
            "x": self.touch_x, "y": self.touch_y, "pressed": self.touch_down,
            "raw_x": self.touch_raw_x, "raw_y": self.touch_raw_y, "t": now,
        })
        return [
            AVCTouchEventAction(x=self.touch_x, y=self.touch_y, touch_type=kind,
                                raw_data=list(frame.data), source=ActionSource.GATEWAY),
            SetAvcStateAction(touch_pressed=self.touch_down, touch_x=self.touch_x,
                              touch_y=self.touch_y),
        ]

    def _button(self, u: NavButton, frame: AvcFrame, now: float) -> List[Action]:
        actions: List[Action] = []
        if u.pressed:
            key = u.key
            if key is None:
                logger.info("unknown nav button code %02X (soft=%s)", u.code, u.soft)
                return []
            self._emit("button", {"key": key, "pressed": True, "t": now})
            actions.append(AVCButtonPressAction(
                button_code=u.code, is_press=True, raw_data=list(frame.data),
                button_name=key, source=ActionSource.GATEWAY))
            actions.append(SetAvcStateAction(last_button=key))
            if u.soft:
                # no release frame on the bus for the info soft buttons
                self._emit("button", {"key": key, "pressed": False, "t": now})
            else:
                self._button_down = key
        else:
            key = self._button_down
            self._button_down = None
            if key is None:
                return []
            self._emit("button", {"key": key, "pressed": False, "t": now})
            actions.append(AVCButtonPressAction(
                button_code=0, is_press=False, raw_data=list(frame.data),
                button_name=key, source=ActionSource.GATEWAY))
        return actions
