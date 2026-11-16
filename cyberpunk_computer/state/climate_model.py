"""
Climate view-model: one screen-independent adapter between the climate UI and
the store.

Climate on this car is read and controlled over AVC-LAN only:

* **read** — ``AppState.climate`` is written by the backend from the climate
  ECU's AVC frames (io/avc_ingest.py): ``E0 5D F3`` status (fan, AUTO, A/C,
  intake, vent, front defrost, rear defogger, system on/off), ``E0 5D F5``
  setpoint (0.5 °C grid, "OFF" sentinel) and ``E0 5D F7`` outside temperature.
  Inside temperature is not on AVC-LAN; it comes from BEAN 52:6D b0
  (io.bean_ingest, scale probable, offset a guess).
* **write** — every command below becomes a UI-sourced store action. On the
  Pi frontend ``RemoteStore`` turns it into a backend REST command
  (frontend/remote_store.py ``_translate``); in the backend (or a local twin)
  the egress middleware hands it to the AVC :class:`ClimateRemote`, which
  sends the display's own climate touch frames ``110->1C6 00 25 E0 84 ...``.

  ====================  ===================================  ==========================  ==================================
  ClimateController     store action                         backend command             AVC press (ClimateRemote)
  ====================  ===================================  ==========================  ==================================
  set_target_temp(t)    SetTargetTempAction(t)               set_climate target_temp     temp_up / temp_down (80 / 40), closed loop on F5
  set_fan(1..7)         SetFanSpeedAction(n)                 set_climate fan=n           fan_n
  set_fan_auto()        SetAutoModeAction(True)              set_climate auto=true       auto (08), skipped if already AUTO
  set_mode("auto")      SetAutoModeAction(True)              set_climate auto=true       auto (08); also turns the system on
  set_mode("manual")    SetFanSpeedAction(current fan)       set_climate fan=n           fan_n (any manual setting leaves AUTO;
                                                                                         there is no AUTO-off button)
  set_mode("off")       ClimateButtonAction("off")           climate_button off          off (04)
  set_ac(b)             SetACAction(b)                       set_climate ac=b            ac (00 20) only if F3 differs
  set_recirculation(b)  SetRecirculationAction(b)            set_climate recirculation   recirc (02) only if F3 differs
  set_air_direction(m)  SetAirDirectionAction(0..3)          set_climate air_direction   vent_face / vent_face_foot /
                                                                                         vent_foot / vent_windscreen_foot
  set_air_direction     (= set_front_defrost(True))
    ("defrost")
  set_front_defrost(b)  ClimateButtonAction("front_defrost") climate_button              front_defrost (01), toggle
  set_rear_defogger(b)  ClimateButtonAction("rear_defogger") climate_button              rear_defogger (00 80), toggle
  ====================  ===================================  ==========================  ==================================

  The two ``climate_button`` toggles are blind presses on the backend, so the
  controller only sends them when the live status says the state differs and
  no press for that control is still awaiting reconciliation.

Optimistic UI: a command records the expected value as *pending*; the view
shows it until the store (= the next AVC status) agrees, or until
``PENDING_S`` passes, after which the car's real value wins again.

Usage (any screen)::

    ctl = ClimateController(store)        # anything with .state and .dispatch
    v = ctl.view()                        # call per frame / per store update
    v.target_text, v.fan_text, v.air_direction, v.stale ...
    ctl.step_target_temp(+1)              # +0.5 °C
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, Optional, Tuple

from .actions import (
    ActionSource,
    ClimateButtonAction,
    SetACAction,
    SetAirDirectionAction,
    SetAutoModeAction,
    SetFanSpeedAction,
    SetRecirculationAction,
    SetTargetTempAction,
)

#: setpoint range accepted by the backend ``set_climate`` command (api/commands.py)
TEMP_MIN = 18.0
TEMP_MAX = 28.0
TEMP_STEP = 0.5          # one temp_up / temp_down press
FAN_MIN, FAN_MAX = 1, 7

#: vent modes the climate ECU reports in ``E0 5D F3`` b2 (decoder.VENT_MODES),
#: plus "defrost" = front windscreen defrost (F3 b1 0x10), which overrides them.
AIR_DIRECTIONS: Tuple[str, ...] = ("face", "face_foot", "foot", "windscreen_foot", "defrost")
AIR_DIRECTION_LABELS: Dict[str, str] = {
    "face": "FACE",
    "face_foot": "FACE+FEET",
    "foot": "FEET",
    "windscreen_foot": "FEET+WS",
    "defrost": "DEFROST",
}
#: legacy ``SetAirDirectionAction`` ints -> comm.avc.climate_remote.AIR_DIRECTION_BUTTONS
_AIR_DIRECTION_INDEX = {"face": 0, "face_foot": 1, "foot": 2, "windscreen_foot": 3}

MODES: Tuple[str, ...] = ("auto", "manual", "off")

#: how long an optimistic value is shown without a confirming AVC status
PENDING_S = 3.0
#: the AVC link counts as stale when ``avc.last_frame_time`` stops advancing
#: for this long (the backend refreshes it ~1 Hz while frames arrive)
STALE_S = 12.0


def snap_temp(t: float) -> float:
    """Round to the car's 0.5 °C grid."""
    return round(float(t) * 2.0) / 2.0


def format_temp(t: Optional[float]) -> str:
    """``21.5`` -> "21.5", ``22.0`` -> "22", ``None`` -> "--"."""
    if t is None:
        return "--"
    t = snap_temp(t)
    return f"{t:.0f}" if t == int(t) else f"{t:.1f}"


@dataclass(frozen=True)
class ClimateView:
    """Everything a climate screen shows, already merged with pending
    optimistic values. Build with :meth:`from_state` or
    :meth:`ClimateController.view`."""

    live: bool                       # a climate status has been received
    stale: bool                      # no status yet, or the AVC link went quiet
    status_source: Optional[str]     # "avc" (or "bean") — who delivered it
    system_on: Optional[bool]        # False = climate OFF; None = unknown
    mode: str                        # "auto" | "manual" | "off" | "unknown"
    target_temp: float               # last known setpoint, 0.5 °C grid
    inside_temp: Optional[float]     # BEAN 52:6D (not on AVC-LAN)
    outside_temp: Optional[float]    # AVC F7; None when stale
    fan: int                         # 0..7 (0 = system off)
    fan_auto: bool                   # fan level chosen by AUTO
    ac: bool
    recirculation: bool
    fresh_air: bool
    vent: Optional[str]              # raw ECU vent: face | face_foot | foot | windscreen_foot | None
    air_direction: Optional[str]     # vent, or "defrost" while front defrost is on
    front_defrost: bool
    rear_defogger: bool
    pending: FrozenSet[str] = field(default_factory=frozenset)

    # ── derived ───────────────────────────────────────────────────────────

    @property
    def target_settable(self) -> bool:
        """The remote can step the setpoint (needs a live, ON system)."""
        return self.live and self.system_on is not False

    @property
    def intake(self) -> Optional[str]:
        if not self.live:
            return None
        return "recirc" if self.recirculation else "fresh"

    @property
    def target_text(self) -> str:
        return "OFF" if self.system_on is False else format_temp(self.target_temp)

    @property
    def fan_text(self) -> str:
        if self.system_on is False:
            return "OFF"
        if not self.live:
            return "--"
        return f"AUTO {self.fan}" if self.fan_auto else str(self.fan)

    @property
    def mode_text(self) -> str:
        return {"auto": "AUTO", "manual": "MANUAL", "off": "OFF"}.get(self.mode, "--")

    @property
    def air_direction_text(self) -> str:
        return AIR_DIRECTION_LABELS.get(self.air_direction or "", "--")

    # ── construction ──────────────────────────────────────────────────────

    @classmethod
    def from_state(cls, state, stale: Optional[bool] = None,
                   pending: Optional[Dict[str, object]] = None) -> "ClimateView":
        """Build from an ``AppState`` (or a bare ``ClimateState``)."""
        c = getattr(state, "climate", state)
        live = c.status_source is not None
        values = truth_values(c)
        pending = pending or {}
        values.update(pending)
        mode = values["mode"]
        system_on = values["system_on"]
        fan_auto = mode == "auto"
        defrost = bool(values["front_defrost"])
        vent = c.air_direction
        air = values["air_direction"]
        if "front_defrost" in pending and "air_direction" not in pending:
            air = "defrost" if defrost else vent
        return cls(
            live=live,
            stale=(not live) if stale is None else stale,
            status_source=c.status_source,
            system_on=system_on,
            mode=mode,
            target_temp=float(values["target_temp"]),
            inside_temp=c.inside_temp,
            outside_temp=c.outside_temp,
            fan=int(values["fan"]),
            fan_auto=fan_auto,
            ac=bool(values["ac"]),
            recirculation=bool(values["recirculation"]),
            fresh_air=(not values["recirculation"]) if "recirculation" in pending else c.fresh_air,
            vent=vent,
            air_direction=air,
            front_defrost=defrost,
            rear_defogger=bool(values["rear_defogger"]),
            pending=frozenset(pending),
        )


def truth_values(c) -> Dict[str, object]:
    """The store's climate values under the controller's field names."""
    if c.status_source is None:
        mode = "unknown"
    elif c.system_on is False:
        mode = "off"
    elif c.auto_mode:
        mode = "auto"
    else:
        mode = "manual"
    return {
        "target_temp": snap_temp(c.target_temp),
        "fan": c.fan_speed,
        "mode": mode,
        "system_on": c.system_on,
        "ac": c.ac_on,
        "recirculation": c.recirculation,
        "air_direction": "defrost" if c.defrost else c.air_direction,
        "front_defrost": c.defrost,
        "rear_defogger": c.rear_defogger,
    }


class ClimateController:
    """Reads climate from ``store.state`` and sends climate commands through
    ``store.dispatch`` (the local ``Store`` or the frontend ``RemoteStore``).

    Every command returns ``True`` when something was dispatched (or nothing
    had to be: already in that state), ``False`` when it was refused (no live
    status for a blind toggle, value out of range, system OFF for the setpoint).
    """

    def __init__(self, store, clock: Callable[[], float] = time.monotonic,
                 pending_s: float = PENDING_S, stale_s: float = STALE_S) -> None:
        self._store = store
        self._clock = clock
        self.pending_s = pending_s
        self.stale_s = stale_s
        self._pending: Dict[str, Tuple[object, float]] = {}
        self._frame_t: Optional[float] = None       # last seen avc.last_frame_time
        self._frame_seen_at: float = -1e9           # local clock when it last advanced

    # ── reading ───────────────────────────────────────────────────────────

    @property
    def state(self):
        return self._store.state

    def _stale(self, state) -> bool:
        c = state.climate
        if c.status_source is None:
            return True
        avc = getattr(state, "avc", None)
        if c.status_source != "avc" or avc is None:
            return False
        now = self._clock()
        ft = avc.last_frame_time
        if ft != self._frame_t:
            self._frame_t = ft
            if ft:
                self._frame_seen_at = now
        if not ft:
            return True
        return now - self._frame_seen_at > self.stale_s

    def _reconcile(self, truth: Dict[str, object]) -> Dict[str, object]:
        now = self._clock()
        out = {}
        for name, (value, deadline) in list(self._pending.items()):
            if truth.get(name) == value or now >= deadline:
                del self._pending[name]
            else:
                out[name] = value
        return out

    def view(self) -> ClimateView:
        """Current climate, with optimistic values not yet confirmed."""
        state = self._store.state
        pending = self._reconcile(truth_values(state.climate))
        return ClimateView.from_state(state, stale=self._stale(state), pending=pending)

    def is_pending(self, name: str) -> bool:
        self.view()
        return name in self._pending

    # ── plumbing ──────────────────────────────────────────────────────────

    def _expect(self, hold_s: Optional[float] = None, **values) -> None:
        deadline = self._clock() + (self.pending_s if hold_s is None else hold_s)
        for k, v in values.items():
            self._pending[k] = (v, deadline)

    def _dispatch(self, action) -> None:
        self._store.dispatch(action)

    # ── commands ──────────────────────────────────────────────────────────

    def set_target_temp(self, celsius: float) -> bool:
        v = self.view()
        if not v.target_settable:
            return False
        t = max(TEMP_MIN, min(TEMP_MAX, snap_temp(celsius)))
        if t == v.target_temp:
            return True
        steps = abs(t - truth_values(self.state.climate)["target_temp"]) / TEMP_STEP
        # the remote steps one press per ~0.45 s and re-checks F5 after each
        self._expect(hold_s=self.pending_s + 0.5 * steps, target_temp=t)
        self._dispatch(SetTargetTempAction(t, source=ActionSource.UI))
        return True

    def step_target_temp(self, steps: int) -> bool:
        """``steps`` × 0.5 °C from the shown setpoint."""
        return self.set_target_temp(self.view().target_temp + steps * TEMP_STEP)

    def set_fan(self, level: int) -> bool:
        level = int(level)
        if not FAN_MIN <= level <= FAN_MAX:
            return False
        self._expect(fan=level, mode="manual", system_on=True)
        self._dispatch(SetFanSpeedAction(level, source=ActionSource.UI))
        return True

    def step_fan(self, delta: int) -> bool:
        v = self.view()
        if v.fan < FAN_MIN or v.system_on is False:
            return self.set_fan(FAN_MIN if delta > 0 else FAN_MAX)
        return self.set_fan(max(FAN_MIN, min(FAN_MAX, v.fan + delta)))

    def set_fan_auto(self) -> bool:
        return self.set_mode("auto")

    def set_mode(self, mode: str) -> bool:
        if mode not in MODES:
            return False
        v = self.view()
        if v.mode == mode:
            return True
        if mode == "auto":
            self._expect(mode="auto", system_on=True)
            self._dispatch(SetAutoModeAction(True, source=ActionSource.UI))
        elif mode == "manual":
            # No AUTO-off button exists: a fan press is a manual setting that
            # leaves AUTO (and keeps the current fan level when known).
            level = v.fan if FAN_MIN <= v.fan <= FAN_MAX else 3
            self._expect(mode="manual", system_on=True, fan=level)
            self._dispatch(SetFanSpeedAction(level, source=ActionSource.UI))
        else:
            self._expect(mode="off", system_on=False)
            self._dispatch(ClimateButtonAction("off", 1, source=ActionSource.UI))
        return True

    def system_off(self) -> bool:
        return self.set_mode("off")

    def system_on(self) -> bool:
        """AUTO restores the previous setpoint (AVC_MAP climate OFF row)."""
        return self.set_mode("auto")

    def set_ac(self, on: bool) -> bool:
        on = bool(on)
        if self.view().ac == on:
            return True
        self._expect(ac=on)
        self._dispatch(SetACAction(on, source=ActionSource.UI))
        return True

    def toggle_ac(self) -> bool:
        return self.set_ac(not self.view().ac)

    def set_recirculation(self, on: bool) -> bool:
        on = bool(on)
        if self.view().recirculation == on:
            return True
        self._expect(recirculation=on)
        self._dispatch(SetRecirculationAction(on, source=ActionSource.UI))
        return True

    def toggle_recirculation(self) -> bool:
        return self.set_recirculation(not self.view().recirculation)

    def set_air_direction(self, mode: str) -> bool:
        if mode == "defrost":
            return self.set_front_defrost(True)
        idx = _AIR_DIRECTION_INDEX.get(mode)
        if idx is None:
            return False
        v = self.view()
        if v.air_direction == mode:
            return True
        expect = {"air_direction": mode}
        if v.front_defrost:
            expect["front_defrost"] = False      # a vent choice leaves defrost
        self._expect(**expect)
        self._dispatch(SetAirDirectionAction(idx, source=ActionSource.UI))
        return True

    def step_air_direction(self, delta: int) -> bool:
        cur = self.view().air_direction
        i = AIR_DIRECTIONS.index(cur) if cur in AIR_DIRECTIONS else (-1 if delta > 0 else 0)
        return self.set_air_direction(AIR_DIRECTIONS[(i + delta) % len(AIR_DIRECTIONS)])

    def _blind_toggle(self, field_name: str, button: str, on: bool, **extra) -> bool:
        v = self.view()
        if not v.live:
            return False                      # never toggle blind
        if getattr(v, field_name) == bool(on):
            return True
        if field_name in self._pending:
            return False                      # previous press not confirmed yet
        self._expect(**{field_name: bool(on)}, **extra)
        self._dispatch(ClimateButtonAction(button, 1, source=ActionSource.UI))
        return True

    def set_front_defrost(self, on: bool) -> bool:
        extra = {"air_direction": "defrost"} if on else {}
        if not on and self._pending.get("air_direction", (None,))[0] == "defrost":
            del self._pending["air_direction"]
        return self._blind_toggle("front_defrost", "front_defrost", on, **extra)

    def toggle_front_defrost(self) -> bool:
        return self.set_front_defrost(not self.view().front_defrost)

    def set_rear_defogger(self, on: bool) -> bool:
        return self._blind_toggle("rear_defogger", "rear_defogger", on)

    def toggle_rear_defogger(self) -> bool:
        return self.set_rear_defogger(not self.view().rear_defogger)
