"""
Simulated drive for the main-screen scene: screenshots and a desk demo.

    python -m cyberpunk_computer.ui.scene.demo --shots OUT_DIR   # headless PNGs
    python -m cyberpunk_computer.ui.scene.demo --live [--scale 3] # window, real time
    python -m cyberpunk_computer.ui.scene.demo --bench           # frame cost

The timeline (seconds): parked, doors open and close, READY, lamps on, pull
away, lead vehicle closing in, speed-limit sign and rules, left curve with
street lamps, red traffic light, stop, scene closes. ``state_at`` /
``scene_at`` are pure Python (AppState + a roadsense scene mapping).

``mods`` (a dict) overrides parts of the timeline for the variant shots:
``env`` (place / light / weather labels), ``signs``, ``limit``, ``lamps``
(tail / head / high / fog), ``cruise`` (VehicleState cruise fields), ``lanes``
("1", "2L", "2R", "3M": lane layout), ``tuning`` (overrides of ui/scene/tuning.py),
``lead`` (False = none), ``objects`` (False = none), ``straight``, ``light``
(the scene's ``light`` block), ``lead_kind`` (the lead's road-user class) and ``road_users`` (other road users:
(kind, x, y right) placed on the road, lane L42), ``vw`` (a function of t returning the scene's display channel
block, or None; radar_demo.py).

Vehicle values go through the real state fields; the road arrives the way the
backend is expected to send it: ``roadsense_scene`` events into a SceneFeed.
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...state.app_state import AppState, GearPosition
from .inputs import DEFAULT_XS, SCENE_EVENT, SceneFeed

SIZE = (480, 240)
DURATION = 44.0


def _ramp(t: float, t0: float, t1: float, a: float, b: float) -> float:
    if t <= t0:
        return a
    if t >= t1:
        return b
    return a + (b - a) * (t - t0) / (t1 - t0)


def speed_at(t: float) -> float:
    """km/h along the timeline."""
    if t < 9.0:
        return 0.0
    if t < 17.0:
        return _ramp(t, 9.0, 17.0, 0.0, 52.0)
    if t < 30.0:
        return 52.0 + 2.0 * math.sin(t * 0.7)
    if t < 36.0:
        return _ramp(t, 30.0, 36.0, 52.0, 0.0)
    return 0.0


SIGNS_DEMO = {
    "on": [{"id": "built_up_area", "s": "D-42", "q": True},
           {"id": "priority_road", "s": "D-1"},
           {"id": "no_overtaking", "s": "B-25"},
           {"id": "no_stopping", "s": "B-36", "side": "r"}],
    "ahead": [{"id": "pedestrian_crossing_ahead", "s": "A-16", "m": 80}],
}


def scene_at(t: float, seq: int, mods: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The roadsense scene mapping at time t (wire format, y RIGHT positive)."""
    mods = mods or {}
    if t < 9.5:
        return None
    xs = DEFAULT_XS
    # curvature: straight, then a left curve between 22 s and 29 s
    kappa = 0.0
    if 21.0 < t < 30.0 and not mods.get("straight"):
        kappa = 0.0052 * math.sin(math.pi * (t - 21.0) / 9.0)
    wander = 0.15 * math.sin(t * 0.9)

    def line(offset_left: float, prob: float) -> Dict[str, Any]:
        # centre line bends left: left = negative on the wire
        return {"y": [round(-(offset_left + wander + 0.5 * kappa * x * x), 2) for x in xs],
                "prob": prob}

    # lane layout: "1" one lane, "2R" two lanes with the car in the right one
    # (the default), "2L" in the left one, "3M" three lanes, the car in the middle
    layout = mods.get("lanes", "2R")
    far_l = line(5.25, 0.85) if layout in ("2R", "3M") else None
    far_r = line(-5.25, 0.85) if layout in ("2L", "3M") else None
    lanes = [far_l, line(1.75, 0.95), line(-1.75, 0.92), far_r]
    edges = [line(5.7 if far_l else 2.3, 1.0), line(-5.8 if far_r else -2.4, 1.0)]
    lead = None
    if 12.0 <= t < 41.5:
        d = _ramp(t, 12.0, 20.0, 78.0, 24.0)
        if t >= 20.0:
            d = 24.0 + 3.0 * math.sin((t - 20.0) * 0.5)
        if t >= 30.0:
            d = _ramp(t, 30.0, 36.0, 24.0, 6.5)
        v_rel = -6.7 if t < 20.0 else (-2.9 if t >= 30.0 else 0.0)
        lead = {"d": round(d, 1), "y": round(-(wander * 0.5 + 0.5 * kappa * d * d), 2),
                "v_rel": v_rel, "prob": 0.95, "a": -2.4 if 30.0 <= t < 36.0 else 0.1}
    if mods.get("lead") is False:
        lead = None
    if lead is not None and "lead_kind" in mods:
        lead["kind"] = mods["lead_kind"]          # roadsense's road-user class of the lead (lane L42)
    objects: List[Dict[str, Any]] = []
    if 19.0 < t < 36.0:
        # street lamps every 30 m on the right, anchored to the road
        travelled = sum(speed_at(k * 0.25) / 3.6 * 0.25 for k in range(int(t * 4)))
        phase = travelled % 30.0
        for i in range(4):
            x = 30.0 * (i + 1) - phase
            if x > 2.0:
                objects.append({"kind": "lamp", "x": round(x, 1),
                                "y": round(4.2 - (wander + 0.5 * kappa * x * x), 2), "state": "on"})
    if mods.get("objects") is False:
        objects = []
    for o in mods.get("road_users") or ():        # other road users roadsense placed (lane L42), y right
        objects.append({"kind": o[0], "x": o[1], "y": round(o[2] - (wander + 0.5 * kappa * o[1] * o[1]), 2)})
    reach = min(100.0, 25.0 + speed_at(t) / 3.6 * 6.0)       # the plan covers ~6 s
    path = {"y": [round(-(wander * (1 - x / 100.0) + 0.5 * kappa * x * x), 2)
                  for x in xs if x <= reach]}
    out: Dict[str, Any] = {"seq": seq, "x": list(xs), "lanes": lanes, "edges": edges,
                           "path": path, "lead": lead, "objects": objects,
                           "env": {"light": "dusk_dawn" if t < 19.0 else "night",
                                   "place": "city_street", "weather": "clear"}}
    if t >= 13.5:
        out["limit"] = {"kmh": 50, "src": "sign", "conf": 0.8}
        out["signs"] = SIGNS_DEMO
    for key in ("env", "signs", "limit", "map"):
        if key in mods:
            if mods[key] is None:
                out.pop(key, None)
            elif callable(mods[key]):
                out[key] = mods[key](t)          # a block that changes along the timeline
            else:
                out[key] = mods[key]
    if 28.5 <= t < 40.0:
        out["light"] = {"state": "red" if t < 39.0 else "green",
                        "d": round(_ramp(t, 28.5, 36.0, 70.0, 12.0), 0), "y": 4.0}
    if "light" in mods:
        if mods["light"] is None:
            out.pop("light", None)
        else:
            out["light"] = mods["light"]
    # lane L44: chris-integrator's verdicts as the backend carries them (scene vis, lead_unknown)
    if "vis" in mods:
        out["vis"] = mods["vis"]
        if not mods["vis"].get("ok", True):
            out.update(lanes=[None] * 4, edges=[None] * 2, lead=None, objects=[])
            out.pop("path", None)
    if mods.get("lead_unknown"):
        out["lead"], out["lead_unknown"] = None, True
    if callable(mods.get("vw")):
        vw = mods["vw"](t)                       # the display channel (radar_demo.py), or None: not sent
        if vw is not None:
            out["vw"] = vw
    return out


def state_at(t: float, vision: str = "live", mods: Optional[Dict[str, Any]] = None) -> AppState:
    """AppState at time t. ``vision``: ``live`` (scene events), ``off``
    (roadsense not running) or ``health`` (running, but only the 5 s health
    mirror reaches the state: what the backend delivers today)."""
    mods = mods or {}
    st = AppState()
    spd = speed_at(t)
    ready = t >= 6.0
    lights = t >= 6.8
    gear = GearPosition.PARK if (t < 8.0 or t >= 41.0) else GearPosition.DRIVE
    braking = (30.0 <= t < 41.0)
    accel = (spd - speed_at(t - 0.5)) / 0.5 if t > 0.5 else 0.0
    power = 0.0
    if ready:
        power = 0.6 + spd * 0.09 + accel * 2.3
        if braking and spd > 1:
            power = -4.0 - spd * 0.25
    ice = ready and 11.0 < t < 30.0
    veh = replace(
        st.vehicle, ready_mode=ready, acc_on=True, ig_on=True, gear=gear, speed_kmh=spd,
        real_speed_kmh=spd, display_speed_kmh=float(int(spd * 1.08 + 0.5)),
        ice_running=ice, brake_switch=braking, brake_pressed=40 if braking else 0,
        cruise_main_switch=False if ready else None)
    if "cruise" in mods:
        veh = replace(veh, **mods["cruise"])
    energy = replace(st.energy, hv_battery_voltage=228.0 - power * 0.6,
                     hv_battery_current=power * 1000.0 / 228.0,
                     battery_soc=0.62 - t * 0.0004)
    lamps = {"tail": lights, "head": lights, "high": False, "fog": False}
    lamps.update(mods.get("lamps") or {})
    decoded = {
        "driver_door_open": {"value": 2.0 <= t < 5.2},
        "passenger_door_open": {"value": 3.0 <= t < 4.6},
        "rear_door_open": {"value": 3.6 <= t < 4.9},
        "lamp_tail": {"value": lamps["tail"]},
        "lamp_head": {"value": lamps["head"]},
        "lamp_high_beam": {"value": lamps["high"]},
        "lamp_fog_front": {"value": lamps["fog"]},
    }
    rs_state = "running" if vision != "off" else "off"
    health = None
    if vision == "health":
        health = {"speed_limit": {"limit_kmh": 50, "source": "sign"},
                  "env": {"light": "night", "place": "city_street", "weather": "clear"},
                  "lights": None}
    rs = replace(st.roadsense, enabled=True, state=rs_state, api_ok=vision != "off",
                 health=health)
    return replace(
        st, vehicle=veh, energy=energy,
        bean=replace(st.bean, decoded=decoded),
        roadsense=rs,
        powerbox=replace(st.powerbox, acc_on=True, connected=True),
        connection=replace(st.connection, connected=True),
        avc=replace(st.avc, battery_bars=5),
        climate=replace(st.climate, outside_temp=7.0, inside_temp=20.0),
    )


class DemoStore:
    """Just enough Store for MainScreen."""

    def __init__(self) -> None:
        from ...state.chart_data import ChartDataStore
        self.state = AppState()
        self.chart_data = ChartDataStore()
        self.replay_speed = 1.0
        self._subs: list = []

    def subscribe(self, _slice, cb):
        self._subs.append(cb)
        return lambda: None

    def dispatch(self, _action) -> None:
        pass

    def push(self, state: AppState) -> None:
        self.state = state
        for cb in self._subs:
            cb(state)


class Sim:
    """MainScreen driven by the timeline with a simulated clock."""

    def __init__(self, vision: str = "live", size: Tuple[int, int] = SIZE,
                 mods: Optional[Dict[str, Any]] = None):
        self.mods = mods or {}
        from ..screens.main_screen import MainScreen
        from .page import ScenePage
        self.t = 0.0
        self.vision = vision
        self.feed = SceneFeed(clock=lambda: self.t)
        self.store = DemoStore()
        self.screen = MainScreen(size, None)
        self.screen._frames = 1            # no start-up frame without the scene
        old = self.screen.scene_page
        tune = None
        if self.mods.get("tuning"):
            from .tuning import Tuning
            tune = Tuning(self.mods["tuning"])
        self.screen.scene_page = ScenePage(old.x, old.y, old.width, feed=self.feed,
                                           clock=lambda: self.t, tuning=tune)
        self.screen.set_store(self.store)
        self._seq = 0
        self._last_scene_t = -1.0
        self.stale_from: Optional[float] = None

    def step(self, dt: float) -> None:
        self.t += dt
        self.store.push(state_at(self.t, self.vision, self.mods))
        stale = self.stale_from is not None and self.t >= self.stale_from
        if self.vision == "live" and not stale and self.t - self._last_scene_t >= 0.2:
            sc = scene_at(self.t, self._seq + 1, self.mods)
            if sc is not None:
                self._seq += 1
                self._last_scene_t = self.t
                self.feed.push(SCENE_EVENT, sc)
        self.screen.update(dt)

    def run_to(self, t: float, dt: float = 0.05) -> None:
        while self.t < t - 1e-9:
            self.step(min(dt, t - self.t))

    def frame(self, surface) -> None:
        surface.fill((0, 0, 0))
        self.screen.render(surface)


def _env(place: Optional[str], light: Optional[str], weather: Optional[str] = "clear") -> Dict[str, Any]:
    return {"env": {k: v for k, v in (("place", place), ("light", light), ("weather", weather)) if v}}


_NO_LAMPS = {"tail": False, "head": False, "high": False, "fog": False}
_OPEN_ROAD = {"lead": False, "objects": False, "signs": None, "straight": True}



def _MAP_DEMO(src: str, kmh: int) -> Dict[str, Any]:
    """A scene ``map`` block with a usable map and the given fused limit."""
    return {"ok": True, "state": "good", "conf": 0.93, "limit": {"kmh": kmh, "src": src, "conf": 0.8},
            "in": {"gnss": "headunit", "fix": "3d", "motion": "vehicle", "tag": "prius-can"}}


def _map_drive(next_m: Optional[float] = 120.0, next_kmh: int = 90, bend: float = 1.0, end: str = "",
               lanes=(1, 1, 0), tunnel_m: Optional[float] = None, state: str = "good", hacc: float = 3.0,
               ref: str = "DK46", cls: str = "primary", name: str = "Opolska", turn=None, view_lanes=None,
               route=None, **extra: Any):
    """A scene ``map`` block as a function of the timeline: a usable map on a road
    that bends right from 200 m (``bend``), distances as they are at 19.5 s and
    shrinking with the 52 km/h of the demo drive (docs/MFD_MAIN_SCREEN.md 6.5)."""
    xs = list(range(0, 601, 40))

    def block(t: float) -> Dict[str, Any]:
        gone = (t - 19.5) * 14.4
        ahead: Dict[str, Any] = {"pc": 0.9, "jn": [[int(420 - gone), 0.9, {"roundabout": "r", "dead_end": "d"}[end]]]
                                 if end else [[int(700 - gone), 0.9, "j"]]}
        if next_m is not None:
            ahead["limit"] = {"m": max(0, int(next_m - gone)), "kmh": next_kmh, "src": "maxspeed", "c": 0.85}
        tun = {"in": False, "src": "map"}
        if tunnel_m is not None:
            tun = {"in": True, "exit_m": max(0, int(tunnel_m - gone)), "src": "map+vision"}
        m = {"ok": True, "state": state, "conf": 0.93, "limit": {"kmh": 50, "src": "map", "conf": 0.8},
             "road": {"cls": cls, "ref": ref, "name": name},
             "lanes": ({"n": lanes[0] + lanes[1], "fwd": lanes[0], "back": lanes[1], "ego": lanes[2],
                        "onc_left": lanes[1] > 0 and lanes[2] == 0, "c": 0.8} if lanes[2] >= 0 else
                       {"n": lanes[0] + lanes[1], "fwd": lanes[0], "back": lanes[1], "c": 0.0,
                        "p": list(lanes[3])}),        # L20: the ego lane unknown, probabilities per lane
             "tunnel": tun, "ahead": ahead,
             "pos": {"src": "gnss" if state == "good" else "dead_reckoning", "hacc": hacc},
             "in": {"gnss": "headunit", "fix": "3d", "motion": "vehicle", "tag": "prius-can"},
             "shape": {"ds": 40, "x": xs, "y": [0 if x < 200 else int(bend * (x - 200) ** 2 / 1600) for x in xs]}}
        if turn is not None:                          # L22: painted turn arrows per own lane, lanes of the likely way
            m["lanes"]["tl"], m["lanes"]["tp"] = list(turn[0]), list(turn[1])
        if view_lanes is not None:
            # L36: the lane boxes as the backend makes them from roadsense's view.road (turn arrows only while a
            # navigation route is followed: roadsense_scene.route_active)
            from ...backend.roadsense_scene import road_lanes_to_scene
            m["lanes"] = road_lanes_to_scene({"lanes": view_lanes, "route": route})
        m.update({k: (v(t) if callable(v) else v) for k, v in extra.items()})
        return m

    return block


_MAP_ROAD = dict(_OPEN_ROAD, limit=None, lamps=_NO_LAMPS, **_env("rural_road", "day"))
# roadsense's view.road.lanes 80 m before a junction with painted arrows (L22 / L36), the car in the middle lane
_VIEW_LANES_TURN = {"count": 3, "forward": 3, "backward": 0, "count_source": "map:tagged", "ego_index": 1,
                    "likely_index": None, "probs": [0.05, 0.9, 0.05], "probs_from": "left", "confidence": 0.9,
                    "oncoming_left": False, "count_held": False,
                    "turn": {"lanes": [["left"], ["through"], ["through", "right"]], "arrows": ["l", "t", "tr"],
                             "count": 3, "dist_m": 80.0, "junction_m": 140.0, "path": [False, True, True],
                             "path_source": "map", "here": False, "held": False}}
_JV_ON = {"map_junction": True}


def _jv(kind: str, m: float, arms: list, c: float = 0.9, ctl: str = "", to: str = "",
        ring: Optional[Dict[str, Any]] = None):
    """A ``map.jv`` block (the junction view) whose distance shrinks with the demo drive."""
    def block(t: float) -> Dict[str, Any]:
        out: Dict[str, Any] = {"k": kind, "m": max(0, int(m - (t - 19.5) * 14.4)), "c": c, "arms": arms}
        if ctl:
            out["ctl"] = ctl
        if to:
            out["to"] = to
        if ring:
            out["ring"] = ring
        return out
    return block


# (file name, time, vision variant, stale from, caption, mods)
SHOTS = (
    # ── states ──
    ("parked_acc", 1.0, "live", None, "parked, ACC on, doors closed", None),
    ("doors_opening", 3.3, "live", None, "driver + passenger door, rear swinging open", None),
    ("doors_open", 4.4, "live", None, "driver, passenger and rear group open", None),
    ("parked_position_lamps", 7.6, "live", None, "parked, READY, position lamps only",
     {"lamps": {"tail": True, "head": False}}),
    ("parked_low_beam", 7.6, "live", None, "parked, READY, low beam", None),
    ("parked_high_beam_fog", 7.6, "live", None, "parked, high beam + front fog",
     {"lamps": {"tail": True, "head": True, "high": True, "fog": True}}),
    ("pull_away", 9.7, "live", None, "pulling away: camera swings behind the car", None),
    ("transition", 10.1, "live", None, "transition: road fades in", None),
    ("drive_road", 11.5, "live", None, "driving, lanes + path live, no lead vehicle", None),
    ("lead_far_signs", 14.0, "live", None, "lead far, limit sign, rules in effect, warning ahead", None),
    ("lead_close", 19.5, "live", None, "lead vehicle closing in (gap colouring, closing speed)", None),
    ("curve_night", 25.5, "live", None, "left curve at night: backdrop pans, street lamps", None),
    ("red_light_lead_braking", 33.0, "live", None, "red light, lead braking (stop lamps), own stop lamps", None),
    ("stopped_hold", 37.0, "live", None, "stopped at the light, scene still open", None),
    ("closing", 39.2, "live", None, "scene closing after the stop", None),
    ("parked_again", 43.0, "live", None, "back to the parked view (P)", None),
    ("drive_no_vision", 19.5, "off", None, "driving, roadsense off: floor only", None),
    ("drive_stale", 24.0, "live", 20.0, "road data stale: dimmed, no lead", None),
    ("drive_lost", 29.0, "live", 20.0, "road data lost: NO SIGNAL", None),
    ("drive_health_only", 19.5, "health", None, "roadsense up, no scene feed: limit + sky only", None),
    # ── headlamps on the road (rural night, open road) ──
    ("beam_off", 19.5, "live", None, "lamps off",
     dict(_OPEN_ROAD, lamps=_NO_LAMPS, **_env("rural_road", "night"))),
    ("beam_position", 19.5, "live", None, "position lamps: nothing on the road",
     dict(_OPEN_ROAD, lamps={"tail": True, "head": False}, **_env("rural_road", "night"))),
    ("beam_low", 19.5, "live", None, "low beam: 46 m fan with the cut-off step",
     dict(_OPEN_ROAD, **_env("rural_road", "night"))),
    ("beam_high", 19.5, "live", None, "high beam: 112 m, wider, brighter",
     dict(_OPEN_ROAD, lamps={"tail": True, "head": True, "high": True}, **_env("rural_road", "night"))),
    ("beam_low_fog", 19.5, "live", None, "low beam + front fog lamps",
     dict(_OPEN_ROAD, lamps={"tail": True, "head": True, "fog": True}, **_env("rural_road", "night", "fog"))),
    # ── scenery by place / light / weather ──
    ("env_unknown", 19.5, "live", None, "no environment labels: neutral ridge", {"env": None}),
    ("env_city_day", 19.5, "live", None, "city_street, day, clear", _env("city_street", "day")),
    ("env_city_dusk", 19.5, "live", None, "city_street, dusk_dawn, clear", _env("city_street", "dusk_dawn")),
    ("env_city_night_rain", 19.5, "live", None, "city_street, night, rain", _env("city_street", "night", "rain")),
    ("env_residential_dusk", 19.5, "live", None, "residential, dusk_dawn", _env("residential", "dusk_dawn")),
    ("env_residential_night_snow", 19.5, "live", None, "residential, night, snow",
     _env("residential", "night", "snow")),
    ("env_highway_day", 19.5, "live", None, "highway, day, clear",
     dict(_env("highway", "day"), limit={"kmh": 140, "src": "default"},
          signs={"on": [{"id": "motorway", "s": "D-9"}]}, lamps=_NO_LAMPS)),
    ("env_highway_night", 19.5, "live", None, "highway, night", _env("highway", "night")),
    ("env_highway_dusk_signs", 19.5, "live", None, "highway, dusk, five rules and two warnings",
     dict(_env("highway", "dusk_dawn"), objects=False, limit={"kmh": 90, "src": "sign", "conf": 0.8},
          signs={"on": [{"id": "priority_road", "s": "D-1"}, {"id": "no_overtaking", "s": "B-25"},
                        {"id": "no_stopping", "s": "B-36", "side": "r"}, {"id": "expressway", "s": "D-7"},
                        {"id": "height_limit", "s": "B-16"}],
                 "ahead": [{"id": "roundabout_ahead", "s": "A-8", "m": 80},
                           {"id": "roadworks_ahead", "s": "A-14", "m": 190}]})),
    ("env_rural_day_overcast", 19.5, "live", None, "rural_road, day, overcast",
     dict(_env("rural_road", "day", "overcast"), lamps=_NO_LAMPS)),
    ("env_rural_dusk", 19.5, "live", None, "rural_road, dusk_dawn, clear", _env("rural_road", "dusk_dawn")),
    ("env_forest_day", 19.5, "live", None, "forest_road, day", dict(_env("forest_road", "day"), lamps=_NO_LAMPS)),
    ("env_forest_night_fog", 19.5, "live", None, "forest_road, night, fog", _env("forest_road", "night", "fog")),
    ("env_bridge_dusk", 19.5, "live", None, "bridge, dusk_dawn", _env("bridge", "dusk_dawn")),
    ("env_gas_station_night", 19.5, "live", None, "gas_station, night", _env("gas_station", "night")),
    ("env_parking_lot_night", 19.5, "live", None, "parking_lot, night",
     dict(_env("parking_lot", "night"), signs=None, limit=None)),
    ("env_tunnel", 19.5, "live", None, "tunnel", dict(_env("tunnel", "night"), objects=False)),
    ("env_underground_parking", 19.5, "live", None, "underground_parking",
     dict(_env("underground_parking", "night"), objects=False, signs=None, limit=None, lead=False)),
    # ── signs and cruise ──
    ("lanes_1", 19.5, "live", None, "one lane", {"lanes": "1"}),
    ("lanes_2_car_left", 19.5, "live", None, "two lanes, the car in the left one", {"lanes": "2L"}),
    ("lanes_2_car_right", 19.5, "live", None, "two lanes, the car in the right one", {"lanes": "2R"}),
    ("lanes_3_car_middle", 19.5, "live", None, "three lanes, the car in the middle", {"lanes": "3M"}),
    ("top_line_speed_on", 19.5, "live", None, "tuning show_speed: speed and gear back, signs in a row below",
     {"tuning": {"show_speed": True}}),
    ("signs_many", 19.5, "live", None, "six rules in effect, three warnings, default-rule limit",
     {"limit": {"kmh": 50, "src": "default"}, "signs": {
         "on": [{"id": "built_up_area", "s": "D-42"}, {"id": "living_street", "s": "D-40", "v": 20},
                {"id": "one_way", "s": "D-3"}, {"id": "no_parking", "s": "B-35", "side": "l"},
                {"id": "no_left_turn", "s": "B-21"}, {"id": "no_dangerous_goods", "s": "B-13"}],
         "ahead": [{"id": "roundabout_ahead", "s": "A-8", "m": 60},
                   {"id": "children_ahead", "s": "A-17", "m": 140},
                   {"id": "uneven_road_ahead", "s": "A-11", "m": 210}]}}),
    ("cruise_ready", 19.5, "live", None, "cruise main switch on, memory speed",
     {"cruise": {"cruise_main_switch": True, "cruise_memory_speed": 70}}),
    ("cruise_active", 19.5, "live", None, "cruise engaged, set speed",
     {"cruise": {"cruise_main_switch": True, "cruise_active": True, "cruise_set_speed": 50}}),
    # ── speed limit from the map layer (fused value, docs/MFD_MAIN_SCREEN.md 4 / 7.2) ──
    ("limit_sign_held", 19.5, "live", None, "camera sign held by the map: thick ring",
     {"limit": {"kmh": 90, "src": "default"}, "map": _MAP_DEMO("sign_held", 40)}),
    ("limit_map_tagged", 19.5, "live", None, "the map's tagged limit, no sign seen: thin double ring",
     {"limit": {"kmh": 50, "src": "default"}, "map": _MAP_DEMO("map", 70)}),
    ("limit_map_implicit", 19.5, "live", None, "implicit (statutory) map limit: thin dim ring",
     {"limit": None, "map": _MAP_DEMO("map_implicit", 50)}),
    # ── ambient information from the map layer (docs/MFD_MAIN_SCREEN.md 6.5) ──
    ("map_next_limit", 19.5, "live", None, "map: next limit 90 in 120 m (dashed ring), the road beyond 100 m, map pin",
     dict(_MAP_ROAD, map=_map_drive())),
    ("map_next_limit_roadside", 19.5, "live", None, "map: the next limit 40 m ahead also stands at the roadside",
     dict(_MAP_ROAD, map=_map_drive(next_m=40.0))),
    ("map_next_limit_beside", 19.5, "live", None, "variant map_next_limit=beside: next limit left of the limit sign",
     dict(_MAP_ROAD, map=_map_drive(), tuning={"map_next_limit": "beside"})),
    ("map_far_road_roundabout", 19.5, "live", None, "map: the far road ends at a roundabout 420 m ahead",
     dict(_MAP_ROAD, map=_map_drive(next_m=None, end="roundabout"))),
    ("map_far_road_off", 19.5, "live", None, "variant map_far_road=false: the camera's 100 m only",
     dict(_MAP_ROAD, map=_map_drive(), tuning={"map_far_road": False})),
    ("map_lanes_three", 19.5, "live", None, "map: three lanes, the car in the middle one; the camera sees no neighbour",
     dict(_MAP_ROAD, lanes="1", map=_map_drive(next_m=None, lanes=(3, 0, 1), bend=0.0))),
    ("map_lanes_oncoming", 19.5, "live", None, "map: the lane seen on the left is oncoming (violet), boxes 1 + 1",
     dict(_MAP_ROAD, lanes="2R", map=_map_drive(next_m=None, bend=-1.0))),
    ("map_lanes_2x2", 19.5, "live", None, "map: two lanes each way, the car in the left one",
     dict(_MAP_ROAD, lanes="3M", map=_map_drive(next_m=None, lanes=(2, 2, 0), bend=0.0))),
    ("map_tunnel_exit", 19.5, "live", None, "map: in a tunnel, 640 m to its exit",
     dict(_OPEN_ROAD, limit=None, map=_map_drive(next_m=None, tunnel_m=640.0, bend=0.0, lanes=(2, 0, 1)), **_env("tunnel", "night"))),
    ("map_tunnel_exit_km", 19.5, "live", None, "map: in a long tunnel on dead reckoning (amber pin), 1.8 km to go",
     dict(_OPEN_ROAD, limit=None, map=_map_drive(next_m=None, tunnel_m=1840.0, bend=0.0, lanes=(2, 0, 1), state="dead_reckoning",
                                                 hacc=14.0), **_env("tunnel", "night"))),
    ("map_tunnel_portal", 19.5, "live", None, "map: the tunnel's mouth 60 m ahead",
     dict(_OPEN_ROAD, limit=None, map=_map_drive(next_m=None, tunnel_m=60.0, bend=0.0, lanes=(2, 0, 1)), **_env("tunnel", "night"))),
    ("map_dead_reckoning", 19.5, "live", None, "map: dead reckoning, position still good: amber pin, elements stay",
     dict(_MAP_ROAD, map=_map_drive(state="dead_reckoning", hacc=9.0))),
    ("map_position_poor", 19.5, "live", None, "map: dead reckoning with a 35 m position error: distances and far road hidden",
     dict(_MAP_ROAD, map=_map_drive(state="dead_reckoning", hacc=35.0))),
    ("map_road_label_ref", 19.5, "live", None, "variant map_road_label=ref: the road number for 8 s after it changed",
     dict(_MAP_ROAD, map=_map_drive(next_m=None), tuning={"map_road_label": "ref", "map_road_label_s": 99.0})),
    ("map_road_label_name", 19.5, "live", None, "variant map_road_label=name: number and street name",
     dict(_MAP_ROAD, map=_map_drive(next_m=None, ref="414", cls="secondary", name="Bohaterów Powstań Śląskich"),
          tuning={"map_road_label": "name", "map_road_label_s": 99.0})),
    # ── junction view (tuning map_junction) and the lane assignment of the traffic light ──
    ("junction_crossroads_signals", 19.5, "live", None,
     "junction view: crossroads with signals in 180 m, the way on straight, a main road crossing",
     dict(_MAP_ROAD, map=_map_drive(next_m=None, jv=_jv("j", 180, [[-90, "xe", 0, 0], [0, "xep", 1, 0], [90, "xe", 0, 0]],
                                                      ctl="s", to="Ozimska")), tuning=_JV_ON)),
    ("junction_t_left", 19.5, "live", None, "junction view: T-junction in 90 m, give way, the way on to the left",
     dict(_MAP_ROAD, map=_map_drive(next_m=None, jv=_jv("j", 90, [[-88, "xep", 0, 0], [92, "xe", 0, 0]], ctl="g",
                                                      to="DK46")), tuning=_JV_ON)),
    ("junction_five_arms", 19.5, "live", None,
     "junction view: five arms, a one-way street that cannot be entered (dashed), a service road (thin, dim)",
     dict(_MAP_ROAD, map=_map_drive(next_m=None, jv=_jv("j", 140, [[-120, "e", 2, 0], [-60, "xe", 2, 0], [8, "xep", 1, 0],
                                                                 [75, "xem", 3, 0], [130, "xe", 1, 0]],
                                                      ctl="t", to="1 Maja")), tuning=_JV_ON)),
    ("junction_roundabout_2nd", 19.5, "live", None, "junction view: roundabout in 140 m, second exit (straight on)",
     dict(_MAP_ROAD, map=_map_drive(next_m=None, end="roundabout",
                                    jv=_jv("r", 140, [[95, "xe", 1, 1], [3, "xep", 0, 2], [-88, "xe", 2, 3], [180, "ea", 0, 0]],
                                           ctl="g", to="DW414", ring={"r": 16, "n": 3, "p": 2})), tuning=_JV_ON)),
    ("junction_roundabout_3rd", 19.5, "live", None, "junction view: roundabout in 60 m, third exit (to the left)",
     dict(_MAP_ROAD, map=_map_drive(next_m=None, end="roundabout",
                                    jv=_jv("r", 60, [[95, "xe", 1, 1], [3, "xe", 0, 2], [-88, "xep", 2, 3], [180, "ea", 0, 0]],
                                           ctl="g", to="Plac Wolności", ring={"r": 16, "n": 3, "p": 3})), tuning=_JV_ON)),
    ("junction_roundabout_on_ring", 19.5, "live", None,
     "junction view: on the ring (the dot), the rest of the way to the third exit",
     dict(_MAP_ROAD, map=_map_drive(next_m=None, end="roundabout",
                                    jv=_jv("r", 0, [[95, "xe", 1, 1], [3, "xe", 0, 2], [-88, "xep", 2, 3], [180, "ea", 0, 0]],
                                           to="Plac Wolności", ring={"r": 16, "n": 3, "p": 3, "on": True, "car": 70})),
          tuning=_JV_ON)),
    ("junction_low_confidence_hidden", 19.5, "live", None,
     "junction view: the path to the junction is a coin toss (confidence 0.35): nothing is drawn",
     dict(_MAP_ROAD, map=_map_drive(next_m=None, jv=_jv("j", 120, [[-90, "xe", 0, 0], [0, "xep", 1, 0]], c=0.35)),
          tuning=_JV_ON)),
    ("light_two_groups", 19.5, "live", None,
     "traffic light: green for the car's lane, the left-turn group (red) small on its side",
     {"light": {"state": "green", "d": 34, "y": 0.4, "c": 0.86, "alt": {"s": "red", "side": "l"}}}),
    ("light_ambiguous", 19.5, "live", None,
     "traffic light: two groups, the choice is ambiguous: ? beside the signal, the other group on the right",
     {"light": {"state": "red", "d": 41, "y": -1.2, "c": 0.56, "q": True, "alt": {"s": "green", "side": "r"}}}),
    ("light_other_lane_only", 19.5, "live", None,
     "traffic light: the only light in view hangs over another lane: shown with ?",
     {"light": {"state": "red", "d": 22, "y": -4.8, "c": 0.1, "q": True}}),
    ("map_synthetic_ignored", 19.5, "live", None, "synthetic position (map.syn): the vision-only picture",
     dict(_MAP_ROAD, map=_map_drive(syn=True))),
    ("map_none", 19.5, "live", None, "the same road without a map",
     dict(_MAP_ROAD)),
    # ── lane L20: approximate position, the ego lane from roadsense's filter ──
    ("junction_approx_dead_reckoning", 19.5, "live", None,
     "junction view on dead reckoning (35 m error, 180 m to go): amber frame, ~ distance",
     dict(_MAP_ROAD, map=_map_drive(next_m=None, state="dead_reckoning", hacc=35.0,
                                    jv=_jv("j", 180, [[-90, "xe", 0, 0], [0, "xep", 1, 0], [90, "xe", 0, 0]],
                                           ctl="s", to="Ozimska")))),
    ("map_lanes_three_likely", 19.5, "live", None,
     "map: three lanes, roadsense cannot name the car's lane; the middle one is likely (outlined, 0.6)",
     dict(_MAP_ROAD, lanes="1", map=_map_drive(next_m=None, lanes=(3, 0, -1, (0.15, 0.6, 0.25)), bend=0.0))),
    # ── lane L22 (roadsense 2.0): turn arrows in the lane boxes, roadsense's own "approximate" mark ──
    ("map_lanes_turn_arrows", 19.5, "live", None,
     "three lanes before a signalled junction: left | straight on | straight on + right; the car in the middle, "
     "the lanes of the likely way bright",
     dict(_MAP_ROAD, lanes="1", map=_map_drive(next_m=None, lanes=(3, 0, 1), bend=0.0,
                                               turn=(("l", "t", "tr"), (0, 1, 1)),
                                               jv=_jv("j", 90, [[-90, "xe", 0, 0], [0, "xep", 0, 0], [90, "xe", 1, 0]],
                                                      ctl="s", to="Ozimska")))),
    ("map_lanes_turn_arrows_likely_approx", 19.5, "live", None,
     "turn arrows with the own lane unknown (the left one likely), the junction marked approximate by roadsense "
     "(a held position)",
     dict(_MAP_ROAD, lanes="1", map=_map_drive(next_m=None, lanes=(2, 1, -1, (0.55, 0.45)), bend=0.0,
                                               turn=(("lt", "tr"), (1, 1)),
                                               jv=lambda t: dict(_jv("j", 140, [[-90, "xe", 1, 0], [0, "xep", 0, 0],
                                                                                [90, "xe", 1, 0]], ctl="g")(t), ap=True)))),
    # ── lane L36: turn arrows only with a navigation route (owner 2026-11-19); none exists today ──
    ("map_lanes_junction_no_route", 19.5, "live", None,
     "the junction approach of shot 84 as the backend sends it today, without a navigation route: plain lane "
     "boxes, the own lane lit, no arrows",
     dict(_MAP_ROAD, lanes="1", map=_map_drive(next_m=None, bend=0.0, view_lanes=_VIEW_LANES_TURN, route=None))),
    ("map_lanes_junction_route", 19.5, "live", None,
     "the same with a route active (a future navigation feed: view.road.route.active): the painted arrows",
     dict(_MAP_ROAD, lanes="1", map=_map_drive(next_m=None, bend=0.0, view_lanes=_VIEW_LANES_TURN,
                                               route={"active": True}))),
    # ── lane L42: road users by class (roadsense's voted class; unknown = the generic car shape) ──
    ("road_users_lead_truck", 19.5, "live", None,
     "the lead is a truck; a car in the left lane, a bus further on",
     {"lead_kind": "truck", "road_users": (("car", 32.0, -3.5), ("bus", 55.0, -3.6)), "straight": True,
      "objects": False, "light": None}),
    ("road_users_lead_bus", 19.5, "live", None, "the lead is a bus",
     {"lead_kind": "bus", "straight": True, "objects": False, "light": None}),
    ("road_users_lead_van", 19.5, "live", None, "the lead is a van; a truck in the left lane",
     {"lead_kind": "van", "road_users": (("truck", 40.0, -3.5),), "straight": True, "objects": False, "light": None}),
    ("road_users_lead_motorbike", 19.5, "live", None, "the lead is a motorbike; a cyclist at the right edge",
     {"lead_kind": "motorbike", "road_users": (("bike", 12.0, 3.0),), "straight": True, "objects": False,
      "light": None}),
    ("road_users_lead_bike", 19.5, "live", None, "the lead is a cyclist",
     {"lead_kind": "bike", "straight": True, "objects": False, "light": None}),
    ("road_users_pedestrians", 19.5, "live", None,
     "no lead; pedestrians at the right edge and one crossing ahead, a cyclist on the left",
     {"lead": False, "road_users": (("person", 16.0, 3.4), ("person", 18.5, 3.9), ("person", 26.0, 0.8),
                                    ("bike", 22.0, -2.8)), "straight": True, "objects": False, "light": None}),
    ("road_users_unknown_lead_traffic", 19.5, "live", None,
     "lead class unknown (generic car shape); a van and a car in the left lane, a pedestrian on the right",
     {"road_users": (("van", 21.0, -3.5), ("car", 38.0, -3.4), ("person", 14.0, 3.6)), "straight": True,
      "objects": False, "light": None}),
    # ── lane L44: chris-integrator says what it does not know ──
    ("camera_unsure", 19.5, "live", None,
     "chris-integrator holds the camera not believable (model out of range): nothing measured is drawn, the road "
     "view says CAMERA UNSURE instead of showing an empty road that looks clear",
     {"vis": {"ok": False, "why": "vision: fault: model_out_of_range"}, "straight": True, "light": None}),
    ("lead_unknown", 19.5, "live", None,
     "the camera is fine but the lead is not known (e.g. the ego speed is unknown): lanes drawn, no vehicle, "
     "LEAD ? (not the same as no lead)",
     {"vis": {"ok": True}, "lead_unknown": True, "straight": True, "objects": False, "light": None}),
)


def render_shots(out_dir: str, scale: int = 4, copy_to: Optional[str] = None,
                 prefix: str = "final_", only: Optional[Sequence[str]] = None) -> List[str]:
    """Render every SHOTS entry: ``NN_name.png`` (native) and ``NN_name@4x.png``;
    the upscaled ones are also copied to ``copy_to`` as ``<prefix>NN_name.png``.
    ``only``: just these shot names (no sign legend; e.g. on the Pi, where the whole set takes too long)."""
    import pygame
    os.makedirs(out_dir, exist_ok=True)
    pygame.init()
    pygame.display.set_mode(SIZE)
    surf = pygame.Surface(SIZE)
    paths = []
    for i, (name, t, vision, stale_from, _caption, mods) in enumerate(SHOTS):
        if only and name not in only:
            continue
        sim = Sim(vision, mods=mods)
        sim.stale_from = stale_from
        sim.run_to(t)
        scene = sim.screen.scene_page.scene
        if scene is not None:
            scene.invalidate()
        sim.frame(surf)
        base = "%02d_%s" % (i + 1, name)
        p = os.path.join(out_dir, base + ".png")
        pygame.image.save(surf, p)
        big = pygame.transform.scale(surf, (SIZE[0] * scale, SIZE[1] * scale))
        pygame.image.save(big, os.path.join(out_dir, base + "@%dx.png" % scale))
        if copy_to:
            os.makedirs(copy_to, exist_ok=True)
            pygame.image.save(big, os.path.join(copy_to, "%s%s.png" % (prefix, base)))
        paths.append(p)
    if only:
        return paths
    # the sign pictograms, enlarged, as a legend
    from .signs import KNOWN_IDS, S, sign_surface
    from ..fonts import get_tiny_font
    font = get_tiny_font(8)
    from ..fonts import get_mono_font
    plate_font = get_mono_font(14)
    cols, cell_w, cell_h = 6, 80, 34
    rows = (len(KNOWN_IDS) + 1 + cols - 1) // cols
    sheet = pygame.Surface((cols * cell_w, rows * cell_h))
    sheet.fill((0, 0, 0))
    ids = list(KNOWN_IDS) + ["(unknown id)"]
    for n, rid in enumerate(ids):
        x, y = (n % cols) * cell_w, (n // cols) * cell_h
        pic = sign_surface(rid, "B-13", False, lambda s_, c_: plate_font.render(s_, False, c_))
        sheet.blit(pic, (x + (cell_w - S) // 2, y + 2))
        lab = font.render(rid.replace("_ahead", "~").replace("_", " ")[:18], False, (110, 150, 160))
        sheet.blit(lab, (x + (cell_w - lab.get_width()) // 2, y + 23))
    big = pygame.transform.scale(sheet, (sheet.get_width() * scale, sheet.get_height() * scale))
    pygame.image.save(sheet, os.path.join(out_dir, "signs_legend.png"))
    pygame.image.save(big, os.path.join(out_dir, "signs_legend@%dx.png" % scale))
    if copy_to:
        pygame.image.save(big, os.path.join(copy_to, prefix + "signs_legend.png"))
    return paths


def run_live(scale: int = 3) -> None:
    import pygame
    pygame.init()
    win = pygame.display.set_mode((SIZE[0] * scale, SIZE[1] * scale))
    pygame.display.set_caption("MFD scene demo")
    surf = pygame.Surface(SIZE)
    clock = pygame.time.Clock()
    sim = Sim()
    while True:
        dt = clock.tick(21) / 1000.0
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE):
                return
        if sim.t >= DURATION + 3.0:
            sim = Sim()
        sim.step(dt)
        sim.frame(surf)
        pygame.transform.scale(surf, win.get_size(), win)
        pygame.display.flip()


def bench(frames_per_state: int = 400) -> Dict[str, Dict[str, float]]:
    """Frame cost of MainScreen.update + render per scene state (ms)."""
    import time
    import pygame
    pygame.init()
    pygame.display.set_mode(SIZE)
    surf = pygame.Surface(SIZE)
    out: Dict[str, Dict[str, float]] = {}
    for label, t0, vision in (("parked", 1.0, "live"), ("doors_moving", 2.0, "live"),
                              ("transition", 9.2, "live"), ("drive_live", 22.0, "live"),
                              ("drive_no_vision", 22.0, "off")):
        sim = Sim(vision)
        sim.run_to(t0)
        sim.frame(surf)
        dt = 1.0 / 21.0
        n = frames_per_state if label in ("parked", "drive_live", "drive_no_vision") else 24
        start = time.perf_counter()
        for _ in range(n):
            if label == "parked":
                sim.screen.update(dt)       # nothing changes
                sim.t += dt
            else:
                sim.step(dt)
            sim.frame(surf)
        el = time.perf_counter() - start
        page = sim.screen.scene_page
        out[label] = {"ms_per_frame": el / n * 1000.0,
                      "scene_redraws_per_frame": page.scene.redraws / float(n + 1)}
    return out


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--shots", metavar="DIR", help="render the screenshot set (headless)")
    ap.add_argument("--live", action="store_true", help="run in a window")
    ap.add_argument("--bench", action="store_true", help="print frame cost")
    ap.add_argument("--scale", type=int, default=4)
    ap.add_argument("--copy-to", metavar="DIR", help="also copy the upscaled shots there")
    ap.add_argument("--prefix", default="final_", help="file name prefix of the copies")
    ap.add_argument("--only", help="comma list of shot names (e.g. camera_unsure,lead_unknown)")
    args = ap.parse_args(argv)
    if args.shots or args.bench:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    if args.shots:
        only = [x.strip() for x in args.only.split(",")] if args.only else None
        for p in render_shots(args.shots, args.scale, args.copy_to, args.prefix, only):
            print(p)
    if args.bench:
        for k, v in bench().items():
            print("%-18s %.3f ms/frame  (scene redraws/frame %.2f)" % (
                k, v["ms_per_frame"], v["scene_redraws_per_frame"]))
    if args.live:
        run_live(min(args.scale, 4))
    return 0


if __name__ == "__main__":
    sys.exit(main())
