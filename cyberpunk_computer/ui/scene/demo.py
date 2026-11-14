"""
Simulated drive for the main-screen scene: screenshots and a desk demo.

    python -m cyberpunk_computer.ui.scene.demo --shots OUT_DIR   # headless PNGs
    python -m cyberpunk_computer.ui.scene.demo --live [--scale 3] # window, real time
    python -m cyberpunk_computer.ui.scene.demo --bench           # frame cost

The timeline (seconds): parked, doors open and close, READY, lamps on, pull
away, lead vehicle closing in, speed-limit sign, left curve with street
lamps, red traffic light, stop, scene closes. ``timeline()`` is pure Python
(AppState + a roadsense scene mapping), so tests use it without pygame.

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


def scene_at(t: float, seq: int) -> Optional[Dict[str, Any]]:
    """The roadsense scene mapping at time t (wire format, y RIGHT positive)."""
    if t < 9.5:
        return None
    xs = DEFAULT_XS
    # curvature: straight, then a left curve between 22 s and 29 s
    kappa = 0.0
    if 21.0 < t < 30.0:
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
                "v_rel": v_rel, "prob": 0.95}
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
    out: Dict[str, Any] = {"seq": seq, "x": list(xs), "lanes": lanes, "edges": edges,
                           "lead": lead, "objects": objects,
                           "env": {"light": "dusk_dawn" if t < 19.0 else "night",
                                   "place": "city_street", "weather": "clear"}}
    if t >= 13.5:
        out["limit"] = {"kmh": 50, "src": "sign"}
    if 28.5 <= t < 40.0:
        out["light"] = {"state": "red" if t < 39.0 else "green",
                        "d": round(_ramp(t, 28.5, 36.0, 70.0, 12.0), 0), "y": 4.0}
    return out


def state_at(t: float, vision: str = "live") -> AppState:
    """AppState at time t. ``vision``: ``live`` (scene events), ``off``
    (roadsense not running) or ``health`` (running, but only the 5 s health
    mirror reaches the state: what the backend delivers today)."""
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
        ice_running=ice, brake_switch=braking, brake_pressed=40 if braking else 0)
    energy = replace(st.energy, hv_battery_voltage=228.0 - power * 0.6,
                     hv_battery_current=power * 1000.0 / 228.0,
                     battery_soc=0.62 - t * 0.0004)
    decoded = {
        "driver_door_open": {"value": 2.0 <= t < 5.2},
        "passenger_door_open": {"value": 3.0 <= t < 4.6},
        "rear_door_open": {"value": 3.6 <= t < 4.9},
        "lamp_tail": {"value": lights},
        "lamp_head": {"value": lights},
        "lamp_high_beam": {"value": False},
        "lamp_fog_front": {"value": False},
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

    def __init__(self, vision: str = "live", size: Tuple[int, int] = SIZE):
        from ..screens.main_screen import MainScreen
        from .page import ScenePage
        self.t = 0.0
        self.vision = vision
        self.feed = SceneFeed(clock=lambda: self.t)
        self.store = DemoStore()
        self.screen = MainScreen(size, None)
        old = self.screen.scene_page
        self.screen.scene_page = ScenePage(old.x, old.y, old.width, feed=self.feed,
                                           clock=lambda: self.t)
        self.screen.set_store(self.store)
        self._seq = 0
        self._last_scene_t = -1.0
        self.stale_from: Optional[float] = None

    def step(self, dt: float) -> None:
        self.t += dt
        self.store.push(state_at(self.t, self.vision))
        stale = self.stale_from is not None and self.t >= self.stale_from
        if self.vision == "live" and not stale and self.t - self._last_scene_t >= 0.2:
            sc = scene_at(self.t, self._seq + 1)
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


# (file name, time, vision variant, stale from, caption)
SHOTS = (
    ("01_parked_acc", 1.0, "live", None, "parked, ACC on, doors closed"),
    ("02_doors_opening", 3.3, "live", None, "driver + passenger door, rear swinging open"),
    ("03_doors_open", 4.4, "live", None, "driver, passenger and rear doors open"),
    ("04_ready_lights", 7.6, "live", None, "READY, low beam + tail lamps on"),
    ("05_pull_away", 9.7, "live", None, "pulling away: camera swings behind the car"),
    ("06_transition", 10.1, "live", None, "transition: road fades in"),
    ("07_drive_road", 11.5, "live", None, "driving, lanes live, no lead vehicle"),
    ("08_lead_far", 14.0, "live", None, "lead vehicle far, speed limit sign seen"),
    ("09_lead_close", 19.5, "live", None, "lead vehicle closing in (gap colouring)"),
    ("10_curve_night", 25.5, "live", None, "left curve at night, street lamps"),
    ("11_red_light", 33.0, "live", None, "red light ahead, braking behind the lead"),
    ("12_stopped_hold", 37.0, "live", None, "stopped at the light, scene still open"),
    ("13_closing", 39.2, "live", None, "scene closing after the stop"),
    ("14_parked_again", 43.0, "live", None, "back to the parked view (P)"),
    ("15_drive_no_vision", 19.5, "off", None, "driving, roadsense off: floor only"),
    ("16_drive_stale", 24.0, "live", 20.0, "driving, road data stale: dimmed, no lead"),
    ("17_drive_lost", 29.0, "live", 20.0, "driving, road data lost: NO SIGNAL"),
    ("18_drive_health_only", 19.5, "health", None,
     "driving, roadsense up but no scene feed (today's backend): limit + sky only"),
)


def render_shots(out_dir: str, scale: int = 4) -> List[str]:
    import pygame
    os.makedirs(out_dir, exist_ok=True)
    pygame.init()
    pygame.display.set_mode(SIZE)
    surf = pygame.Surface(SIZE)
    paths = []
    for name, t, vision, stale_from, _caption in SHOTS:
        sim = Sim(vision)
        sim.stale_from = stale_from
        sim.run_to(t)
        sim.screen.scene_page.scene and sim.screen.scene_page.scene.invalidate()
        sim.frame(surf)
        p = os.path.join(out_dir, name + ".png")
        pygame.image.save(surf, p)
        big = pygame.transform.scale(surf, (SIZE[0] * scale, SIZE[1] * scale))
        pygame.image.save(big, os.path.join(out_dir, name + "@%dx.png" % scale))
        paths.append(p)
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
    args = ap.parse_args(argv)
    if args.shots or args.bench:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    if args.shots:
        for p in render_shots(args.shots, args.scale):
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
