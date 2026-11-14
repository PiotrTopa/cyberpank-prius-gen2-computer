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
(tail / head / high / fog), ``cruise`` (VehicleState cruise fields),
``lead`` (False = none), ``objects`` (False = none), ``straight``.

Vehicle values go through the real state fields; the road arrives the way the
backend is expected to send it: ``roadsense_scene`` events into a SceneFeed.
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

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

    lanes = [line(5.25, 0.55), line(1.75, 0.95), line(-1.75, 0.92), None]
    edges = [line(5.7, 1.0), line(-2.4, 1.0)]
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
    for key in ("env", "signs", "limit"):
        if key in mods:
            if mods[key] is None:
                out.pop(key, None)
            else:
                out[key] = mods[key]
    if 28.5 <= t < 40.0:
        out["light"] = {"state": "red" if t < 39.0 else "green",
                        "d": round(_ramp(t, 28.5, 36.0, 70.0, 12.0), 0), "y": 4.0}
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
        self.screen.scene_page = ScenePage(old.x, old.y, old.width, feed=self.feed,
                                           clock=lambda: self.t)
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
)


def render_shots(out_dir: str, scale: int = 4, copy_to: Optional[str] = None,
                 prefix: str = "final_") -> List[str]:
    """Render every SHOTS entry: ``NN_name.png`` (native) and ``NN_name@4x.png``;
    the upscaled ones are also copied to ``copy_to`` as ``<prefix>NN_name.png``."""
    import pygame
    os.makedirs(out_dir, exist_ok=True)
    pygame.init()
    pygame.display.set_mode(SIZE)
    surf = pygame.Surface(SIZE)
    paths = []
    for i, (name, t, vision, stale_from, _caption, mods) in enumerate(SHOTS):
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
    args = ap.parse_args(argv)
    if args.shots or args.bench:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    if args.shots:
        for p in render_shots(args.shots, args.scale, args.copy_to, args.prefix):
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
