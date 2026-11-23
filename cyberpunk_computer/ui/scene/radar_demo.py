"""
Radar view on the demo drive (ui/scene/demo.py): screenshots, frame cost and a desk demo without the car.

    python -m cyberpunk_computer.ui.scene.radar_demo --shots OUT_DIR [--scale 3]   # headless PNGs, 1x and Nx
    python -m cyberpunk_computer.ui.scene.radar_demo --bench                       # road view vs radar view
    python -m cyberpunk_computer.ui.scene.radar_demo --live [--scale 3]            # window: SPACE toggles, 1-9 situation

Each situation is a function of the demo time that returns the scene's display channel block ``vw`` the way the
backend sends it (``roadsense_scene.view_to_scene``: y RIGHT positive, velocities relative to the car); the
objects move with their velocities, so the leaders, the range and the tags behave as on the road. The driver's
tap on the button is ``MainScreen._toggle_radar`` (the touch zone's handler).
"""

from __future__ import annotations

import math
import os
import sys
from typing import Any, Callable, Dict, List, Optional

from ...backend.roadsense_scene import view_to_scene
from .demo import SIZE, Sim, speed_at
from .inputs import DEFAULT_XS

XS = DEFAULT_XS


def _line(off_right: float, prob: float = 0.9) -> Dict[str, Any]:
    return {"y": [off_right] * len(XS), "prob": prob}


_MOTION = {"s": "stationary", "m": "moving"}


FOV_HALF_DEG = 35.0          # demo: the forward camera's half field; outside it the radar only remembers
MEM_SD0, MEM_SD_RATE = 0.4, 0.12     # demo: a remembered standing object's 1 sigma (m) and its growth (m/s)
AROUND_M = 85.0                      # chris-integrator's around_range_m


def _integrator_object(o: dict) -> dict:
    """A compact demo object (the scene's short keys) -> chris-integrator's view object (docs/TOPICS.md)."""
    m = o.get("s", "")
    return {"id": o["id"], "kind": "road_user", "role": "other", "x": o["d"], "y": o["y"], "vx": o.get("vx", 0.0),
            "vy": o.get("vy", 0.0), "sx": o.get("sd"), "sy": None, "phase": "steady", "alpha": 1.0,
            "cls": {"label": o["k"], "confidence": 0.9, "source": "roadusers"},
            "attrs": {"source": "roadusers" if o.get("c") else "radar", "approximate": True,
                      "motion": _MOTION.get(o.get("m"), "unknown"), "parked": m == "p", "obstacle": m == "o",
                      "threat": m == "t", "with_traffic": bool(o.get("w")), "ttc_s": o.get("ttc")}}


def _track(o: dict, v: float) -> tuple:
    """(state, seconds since seen, 1 sigma) of a demo object as the radar would list it: given ("st"), or
    remembered once it is out of the camera's field (a standing one since it left the field at the car's
    speed), else seen."""
    if o.get("st"):
        return o["st"], o.get("ago", 0.0), o.get("sd")
    x, y = o["d"], o["y"]
    if x > 1.0 and abs(math.degrees(math.atan2(y, x))) <= FOV_HALF_DEG:
        return "confirmed", 0.0, o.get("sd")
    x_exit = abs(y) / math.tan(math.radians(FOV_HALF_DEG))
    ago = max(0.0, (x_exit - x) / max(v, 0.5)) if v > 0.5 and o.get("m") == "s" else o.get("ago", 2.0)
    return "memory", ago, max(o.get("sd") or 0.0, MEM_SD0 + MEM_SD_RATE * ago)


def _around_entry(o: dict, v: float) -> Optional[dict]:
    """A demo object -> chris-integrator's `view.around` entry (docs/TOPICS.md), None beyond its range."""
    if math.hypot(o["d"], o["y"]) > AROUND_M:
        return None
    st, ago, sd = _track(o, v)
    e: Dict[str, Any] = {"id": o["id"], "x": round(o["d"], 1), "y": round(o["y"], 1)}
    gx, gy = o.get("vx", 0.0) + v, o.get("vy", 0.0)
    if math.hypot(gx, gy) >= 0.5:
        e["gx"], e["gy"] = round(gx, 1), round(gy, 1)
    e["sd"] = round(sd if sd is not None else (0.5 if o.get("c") is None else 0.15 * o["d"]), 1)
    if st != "confirmed":
        e["st"], e["ago"] = st, round(ago, 1)
    e["k"] = o["k"]
    if o.get("m"):
        e["mv"] = o["m"]
    fl = "".join(ch for ch in "top" if o.get("s") == ch and not (st == "memory" and ch in "to"))
    if o.get("w"):
        fl += "w"
    if fl:
        e["fl"] = fl
    if ("t" in fl or "o" in fl) and o.get("ttc") is not None:
        e["ttc"] = o["ttc"]
    return e


def _street(t: float, t0: float, objs: List[dict], lead: Optional[dict] = None, alert: Optional[dict] = None,
            radar: bool = True, two_way: bool = True, ground: bool = False) -> Dict[str, Any]:
    """The scene's vw block at demo time t, made the way the backend makes it: a chris-integrator `view` message
    through ``roadsense_scene.view_to_scene`` (so the caps on road users and their priorities apply). Object
    positions are given for t0 and carried by their relative velocity (``ground``: by their ground velocity
    and the distance the car drove since t0). `objects` (the road view's) holds what the integrator puts there
    (seen, 1-70 m ahead, 12 m to the side, not the lead); `around` (the radar view's) everything within 85 m."""
    dt = t - t0
    v = _v(t)
    objects, around = [], []
    for o in objs:
        if ground:
            g = o.get("g", 0.0)
            x = o["d"] + g * dt - (_odo(t) - _odo(t0))
            o = dict(o, vx=g - v)
        else:
            x = o["d"] + o.get("vx", 0.0) * dt
        y = o["y"] + o.get("vy", 0.0) * dt
        o = dict(o, d=round(x, 2), y=round(y, 2))
        st = _track(o, v)[0]
        if 1.0 < x <= 70.0 and abs(y) <= 12.0 and st != "memory":
            objects.append(_integrator_object(o))
        if radar and (x <= 70.0 or st == "memory"):        # the camera places nothing beyond about 70 m
            e = _around_entry(o, v)
            if e is not None:
                around.append(e)
    if lead is not None:
        lo = {"id": lead["id"], "kind": "vehicle", "role": "lead",
              "x": round(lead["d"] + lead.get("v", 0.0) * dt, 2), "y": lead.get("y", 0.0),
              "vx": lead.get("v", 0.0), "vy": 0.0, "sx": lead.get("sd"), "alpha": 1.0,
              "cls": {"label": lead.get("k", "car"), "confidence": 0.9, "source": "roadusers"},
              "attrs": {"dist_std_m": lead.get("sd"), "ttc_s": lead.get("ttc")}}
        objects.append(lo)
        e = {"id": lo["id"], "x": round(lo["x"], 1), "y": lo["y"], "k": lead.get("k", "car"), "fl": "L"}
        if abs(lo["vx"] + v) >= 0.5:
            e["gx"], e["gy"] = round(lo["vx"] + v, 1), 0.0
        if lead.get("sd") is not None:
            e["sd"] = lead["sd"]
        around.append(e)
    lines = [{"id": "lane:1", "kind": "lane_line", "y": [-1.75] * len(XS), "conf": 0.9, "alpha": 1.0},
             {"id": "lane:2", "kind": "lane_line", "y": [1.75] * len(XS), "conf": 0.9, "alpha": 1.0}]
    if two_way:
        lines.append({"id": "lane:0", "kind": "lane_line", "y": [-5.25] * len(XS), "conf": 0.7, "alpha": 1.0})
    msg: Dict[str, Any] = {"type": "view", "ts_mono_ns": int(t * 1e9), "x": list(XS), "lines": lines,
                           "ego": {"speed_mps": round(v, 2)},
                           "path": {"y": [0.0] * 8 + [None] * (len(XS) - 8)}, "objects": objects}
    if radar:
        msg["alert"] = None
        if alert is not None:
            msg["alert"] = {"level": {"t": "threat", "o": "obstacle"}[alert["lvl"]], "id": alert.get("id", ""),
                            "x": round(alert["d"] + alert.get("v", 0.0) * dt, 2), "y": alert["y"],
                            "ttc_s": alert.get("ttc"), "lead": bool(alert.get("lead"))}
        msg["around"] = sorted(around, key=lambda e: math.hypot(e["x"], e["y"]))[:20]
    return view_to_scene(msg)


_ODO: Dict[int, float] = {}


def _odo(t: float) -> float:
    """Metres the demo car has driven by time t (the demo drive's speed profile)."""
    k = int(t * 20)
    if k not in _ODO:
        _ODO[k] = sum(speed_at(i / 20.0) / 3.6 / 20.0 for i in range(k))
    return _ODO[k]


def _parked_rows(v: float, near: float = 5.0) -> List[dict]:
    """Parked cars both sides of a two-way street: right kerb at 3.3 m, left beyond the oncoming lane."""
    rows = []
    for i, x in enumerate((near, near + 6.5, near + 13.0, near + 20.5, near + 27.0, near + 34.0, near + 42.0)):
        rows.append({"id": "ru:%d" % (10 + i), "k": "van" if i == 3 else "car", "d": x, "y": 3.3, "vx": -v,
                     "vy": 0.0, "s": "p", "m": "s"})
    for i, x in enumerate((near + 3.0, near + 11.0, near + 18.0, near + 31.0)):
        rows.append({"id": "ru:%d" % (30 + i), "k": "car", "d": x, "y": -6.4, "vx": -v, "vy": 0.0, "s": "p",
                     "m": "s"})
    # the cars already passed: behind the car, out of the camera's field, remembered by the radar
    for i, x in enumerate((near - 7.0, near - 14.0, near - 21.5, near - 29.0)):
        rows.append({"id": "ru:%d" % (40 + i), "k": "car", "d": x, "y": 3.3, "vx": -v, "vy": 0.0, "s": "p",
                     "m": "s"})
    for i, x in enumerate((near - 6.0, near - 17.0)):
        rows.append({"id": "ru:%d" % (50 + i), "k": "car", "d": x, "y": -6.4, "vx": -v, "vy": 0.0, "s": "p",
                     "m": "s"})
    return rows


def _v(t: float) -> float:
    return speed_at(t) / 3.6


def city(t0: float) -> Callable[[float], Optional[Dict[str, Any]]]:
    """A two-way city street: the lead, parked rows both sides, an oncoming car, a pedestrian the camera alone
    places (range bar), a cyclist with the traffic at the right kerb."""
    def vw(t: float):
        if t < t0 - 6.0:
            return None
        v = _v(t)
        objs = _parked_rows(v, near=6.0)[1:] + [
            {"id": "ru:2", "k": "car", "d": 40.0, "y": -3.5, "vx": -v - 12.5, "vy": 0.0, "m": "m"},
            {"id": "ru:3", "k": "human", "d": 15.0, "y": 5.2, "vx": -v, "vy": -1.1, "c": 1},
            {"id": "ru:4", "k": "bike", "d": 9.5, "y": 2.3, "vx": -v + 5.0, "vy": 0.0, "m": "m", "w": 1, "sd": 0.6},
        ]
        return _street(t, t0, objs, lead={"id": "lead:1", "d": 26.0, "y": 0.1, "v": -0.8, "k": "car", "sd": 0.7})
    return vw


def obstacle(t0: float):
    """No lead; a van stands on the path ahead (the radar's obstacle), parked rows both sides."""
    def vw(t: float):
        if t < t0 - 6.0:
            return None
        v = _v(t)
        objs = _parked_rows(v, near=4.0) + [
            {"id": "ru:5", "k": "van", "d": 34.0, "y": 0.3, "vx": -v, "vy": 0.0, "s": "o", "m": "s", "sd": 0.8}]
        return _street(t, t0, objs, alert={"lvl": "o", "d": 34.0, "y": 0.3, "v": -v})
    return vw


def threat(t0: float):
    """A car pulls out of the right parked row into the path: a threat with its time to reach; the lead further
    on, an oncoming car."""
    def vw(t: float):
        if t < t0 - 6.0:
            return None
        v = _v(t)
        # parked in the row until 1.5 s before t0, then it pulls out (the radar flags it a threat)
        out = t >= t0 - 1.5
        objs = _parked_rows(v, near=7.0)[1:] + [
            {"id": "ru:6", "k": "car", "d": 16.0, "y": 1.9, "vx": -v + 2.0, "vy": -0.6, "s": "t", "m": "m",
             "ttc": 1.3} if out else
            {"id": "ru:6", "k": "car", "d": 13.0, "y": 2.8, "vx": -v,
             "vy": 0.0, "s": "p", "m": "s"},
            {"id": "ru:2", "k": "car", "d": 48.0, "y": -3.5, "vx": -v - 12.0, "vy": 0.0, "m": "m"},
        ]
        return _street(t, t0, objs, lead={"id": "lead:1", "d": 38.0, "y": 0.0, "v": -1.0, "k": "car"},
                       alert={"lvl": "t", "d": 16.0, "y": 1.9, "ttc": 1.3, "v": -v + 2.0} if out else None)
    return vw


def crossing(t0: float):
    """Slow in a narrow street: a pedestrian crossing in front, a cyclist coming the other way, a bus at the
    stop on the right (standing, not parked), the lead close."""
    def vw(t: float):
        if t < t0 - 6.0:
            return None
        v = _v(t)
        objs = [
            {"id": "ru:7", "k": "human", "d": 12.0, "y": -1.0, "vx": -v, "vy": 1.3, "m": "m", "sd": 0.5},
            {"id": "ru:8", "k": "bike", "d": 21.0, "y": -2.8, "vx": -v - 4.5, "vy": 0.0, "m": "m"},
            {"id": "ru:9", "k": "bus", "d": 30.0, "y": 3.6, "vx": -v, "vy": 0.0, "m": "s"},
            {"id": "ru:11", "k": "car", "d": 8.0, "y": 3.4, "vx": -v, "vy": 0.0, "s": "p", "m": "s"},
            {"id": "ru:12", "k": "car", "d": 43.0, "y": 3.4, "vx": -v, "vy": 0.0, "s": "p", "m": "s"},
        ]
        return _street(t, t0, objs, lead={"id": "lead:1", "d": 17.0, "y": 0.2, "v": 0.0, "k": "car", "sd": 0.6})
    return vw


def far(t0: float):
    """An open road: the lead far ahead, placed by the camera alone (large range sigma), a car in the oncoming
    lane far off, a motorbike with the traffic: the range steps out to 80 m."""
    def vw(t: float):
        if t < t0 - 6.0:
            return None
        v = _v(t)
        objs = [
            {"id": "ru:13", "k": "car", "d": 52.0, "y": -3.6, "vx": -v - 22.0, "vy": 0.0, "m": "m"},
            {"id": "ru:14", "k": "motorbike", "d": 14.0, "y": 3.1, "vx": -v + 4.0, "vy": 0.0, "m": "m", "w": 1},
            {"id": "ru:15", "k": "truck", "d": 44.0, "y": 4.4, "vx": -v, "vy": 0.0, "s": "p", "m": "s",
             "sd": 1.5},
        ]
        return _street(t, t0, objs, lead={"id": "lead:1", "d": 58.0, "y": 0.4, "v": -1.5, "k": "car", "sd": 7.5})
    return vw


def passed(t0: float):
    """Driving past parked rows at 52 km/h: ahead they are seen, beside and behind remembered, the further
    behind the longer ago and the blurrier; a cyclist overtaken 4 s ago, remembered behind; the lead."""
    def vw(t: float):
        if t < t0 - 6.0:
            return None
        objs = []
        for i, x in enumerate(range(-60, 50, 7)):
            objs.append({"id": "ru:%d" % (60 + i), "k": "van" if i % 5 == 2 else "car", "d": float(x), "y": 3.3,
                         "s": "p", "m": "s"})
        for i, x in enumerate(range(-52, 50, 11)):
            objs.append({"id": "ru:%d" % (80 + i), "k": "car", "d": float(x) + 3.0, "y": -6.4, "s": "p", "m": "s"})
        objs.append({"id": "ru:21", "k": "bike", "d": -38.0, "y": 2.4, "g": 5.0, "m": "m", "w": 1,
                     "st": "memory", "ago": 4.0 + (t - t0), "sd": 3.0 + 0.8 * (t - t0)})
        return _street(t, t0, objs, lead={"id": "lead:1", "d": 27.0, "y": 0.1, "v": -0.5, "k": "car", "sd": 0.7},
                       ground=True)
    return vw


def cross_traffic(t0: float):
    """Slowing for a junction: crossing traffic far to both sides (a car seen at the field's edge on the left,
    another that just left the field and coasts, a van on the right remembered), a pedestrian waiting at the
    corner, the lead stopping, a car remembered behind."""
    def vw(t: float):
        if t < t0 - 6.0:
            return None
        objs = [
            {"id": "ru:22", "k": "car", "d": 30.0, "y": -19.0, "g": 0.0, "vy": 9.0, "m": "m"},
            {"id": "ru:23", "k": "car", "d": 24.0, "y": -27.0, "g": 0.0, "vy": 8.0, "m": "m", "st": "coast",
             "ago": 0.4, "sd": 1.2},
            {"id": "ru:24", "k": "van", "d": 21.0, "y": 34.0, "g": 0.0, "vy": -8.5, "m": "m", "st": "memory",
             "ago": 2.5, "sd": 3.5},
            {"id": "ru:25", "k": "human", "d": 19.0, "y": 6.0, "g": 0.0, "m": "s", "sd": 0.6},
            {"id": "ru:26", "k": "car", "d": -16.0, "y": 0.2, "g": 0.0, "m": "m", "st": "memory", "ago": 9.0,
             "sd": 4.5},
            {"id": "ru:27", "k": "car", "d": 9.0, "y": 3.4, "g": 0.0, "s": "p", "m": "s"},
            {"id": "ru:28", "k": "car", "d": 1.0, "y": 3.4, "g": 0.0, "s": "p", "m": "s"},
        ]
        return _street(t, t0, objs, lead={"id": "lead:1", "d": 12.0, "y": 0.0, "v": -1.5, "k": "car", "sd": 0.5},
                       ground=True)
    return vw


def ageing(t_out: float):
    """A pedestrian walks off the kerb on the left and out of the camera's field at ``t_out``: remembered, its
    place blurring at about 1 m/s (it may walk anywhere); the cars passed are remembered behind."""
    def vw(t: float):
        if t < t_out - 8.0:
            return None
        ago = max(0.0, t - t_out)
        objs = [{"id": "ru:29", "k": "human", "d": 9.0, "y": -5.0, "g": 0.0, "vy": -1.2, "m": "m",
                 "st": "memory" if t >= t_out else "", "ago": ago, "sd": 1.0 + 1.0 * ago}]
        for i, x in enumerate((-30.0, -22.0, -14.5, -7.0, 1.0, 9.0)):
            objs.append({"id": "ru:%d" % (90 + i), "k": "car", "d": x, "y": 3.3, "g": 0.0, "s": "p", "m": "s"})
        return _street(t, t_out, objs, lead={"id": "lead:1", "d": 13.0, "y": 0.0, "v": -1.0, "k": "car", "sd": 0.5},
                       ground=True)
    return vw


def no_radar(t0: float):
    """The integrator's radar is silent (no rdr): only the camera's road users."""
    def vw(t: float):
        if t < t0 - 12.0:
            return None
        v = _v(t)
        objs = [{"id": "ru:16", "k": "human", "d": 18.0, "y": 4.5, "vx": -v, "vy": 0.0, "c": 1}]
        return _street(t, t0, objs, lead={"id": "lead:1", "d": 24.0, "y": 0.0, "v": 0.0, "k": "car"},
                       radar=False)
    return vw


_BASE = {"straight": True, "objects": False, "light": None}

# (name, t, radar chosen, caption, mods)
SHOTS: List[tuple] = [
    ("road_view_button", 24.0, False, "the road view with the RDR button bottom-right (lane boxes left of it)",
     dict(_BASE, lanes="2R", vw=city(24.0))),
    ("radar_city", 24.0, True, "city street: lead, parked rows both sides, oncoming car, pedestrian "
     "(camera only, range bar), cyclist with the traffic", dict(_BASE, vw=city(24.0))),
    ("radar_obstacle", 24.0, True, "a van stands on the path: red with a frame, the tag in red; no lead",
     dict(_BASE, vw=obstacle(24.0))),
    ("radar_threat_on", 24.0, True, "a car pulls out of the parked row: threat, red, its time to reach "
     "(blink phase on)", dict(_BASE, vw=threat(24.0))),
    ("radar_threat_off", 24.5, True, "the same half a second later (blink phase off)",
     dict(_BASE, vw=threat(24.0))),
    ("radar_crossing", 24.0, True, "pedestrian crossing, oncoming cyclist, a bus standing at the stop",
     dict(_BASE, vw=crossing(24.0))),
    ("radar_far", 24.0, True, "open road: the lead at 58 m placed by the camera alone (range bar), the "
     "range steps out to 80 m", dict(_BASE, vw=far(24.0))),
    ("radar_no_data", 24.0, True, "radar chosen, the integrator's radar silent: NO RADAR DATA (before the "
     "fallback)", dict(_BASE, vw=no_radar(24.0))),
    ("radar_fallback", 30.5, True, "after 5 s without radar data the road view is back; the button stays lit "
     "(the choice stands)", dict(_BASE, vw=no_radar(30.5))),
    ("radar_passed", 27.0, True, "52 km/h past parked rows: seen ahead, remembered beside and behind (dotted "
     "ghosts in growing sigma rings), an overtaken cyclist remembered behind", dict(_BASE, vw=passed(27.0))),
    ("radar_cross_traffic", 34.0, True, "slowing for a junction: crossing traffic both sides (seen, coasting, "
     "remembered), a pedestrian at the corner, a car remembered behind", dict(_BASE, vw=cross_traffic(34.0))),
    ("radar_memory_young", 33.0, True, "a pedestrian walked out of the camera's field 1 s ago: remembered, ring "
     "of 2 m", dict(_BASE, vw=ageing(32.0))),
    ("radar_memory_old", 37.0, True, "the same 4 s later: dimmer, its ring 6 m, the block says MEM 5s",
     dict(_BASE, vw=ageing(32.0))),
    ("radar_ahead_centre", 24.0, True, "tuning radar_centre=ahead on the open road: the centre moves down at the "
     "larger ranges (more ahead, less behind)", dict(_BASE, vw=far(24.0), tuning={"radar_centre": "ahead"})),
    ("radar_sweep", 24.0, True, "tuning radar_sweep: the stepped sweep line with its afterglow over the city "
     "street", dict(_BASE, vw=city(24.0), tuning={"radar_sweep": True})),
]


def make_sim(t: float, radar_on: bool, mods: Dict[str, Any], toggle_at: float = 18.0, surf=None) -> Sim:
    """The demo drive up to t; with ``radar_on`` the driver taps the button at ``toggle_at``. From 3 s before
    the toggle every step is also rendered (as on the car: the radar view is built at its first frame, its
    range and tag places follow the frames)."""
    import pygame
    surf = surf if surf is not None else pygame.Surface(SIZE)
    sim = Sim("live", mods=mods)
    sim.run_to(max(0.0, min(t, toggle_at) - 3.0))
    while sim.t < min(t, toggle_at) - 1e-9:
        sim.step(min(0.05, min(t, toggle_at) - sim.t))
        sim.frame(surf)
    if radar_on:
        sim.screen._toggle_radar()          # the touch zone's handler
    while sim.t < t - 1e-9:
        sim.step(min(0.05, t - sim.t))
        sim.frame(surf)
    return sim


def render_shots(out_dir: str, scale: int = 3, only: Optional[List[str]] = None) -> List[str]:
    import pygame
    os.makedirs(out_dir, exist_ok=True)
    pygame.init()
    pygame.display.set_mode(SIZE)
    surf = pygame.Surface(SIZE)
    paths = []
    for i, (name, t, radar_on, _cap, mods) in enumerate(SHOTS):
        if only and name not in only:
            continue
        toggle = 18.0 if name != "radar_no_data" else 22.5
        if name == "radar_fallback":
            toggle = 22.5
        sim = make_sim(t, radar_on, mods, toggle, surf)
        sim.frame(surf)
        base = "%02d_%s" % (i + 1, name)
        p = os.path.join(out_dir, base + ".png")
        pygame.image.save(surf, p)
        big = pygame.transform.scale(surf, (SIZE[0] * scale, SIZE[1] * scale))
        pygame.image.save(big, os.path.join(out_dir, base + "@%dx.png" % scale))
        paths.append(p)
    return paths


def standing(t0: float):
    """Standing at the lights (the demo car stops at 36 s): the lead standing, parked cars beside and behind
    (remembered), nothing moving: what the scope costs when nothing changes."""
    def vw(t: float):
        if t < t0 - 8.0:
            return None
        objs = [{"id": "ru:%d" % (100 + i), "k": "car", "d": x, "y": 3.3, "g": 0.0, "s": "p", "m": "s"}
                for i, x in enumerate((-20.0, -12.0, -4.0, 4.0, 12.0, 20.0))]
        return _street(t, t0, objs, lead={"id": "lead:1", "d": 7.0, "y": 0.0, "v": 0.0, "k": "car", "sd": 0.4},
                       ground=True)
    return vw


def bench(frames: int = 300) -> Dict[str, Dict[str, float]]:
    """Cost of the road view and the radar view on the same stretch of the demo drive (a city street with
    sixteen road users all round and the lead, 52 km/h then braking; 20 frames a second): the whole main screen
    (``MainScreen.update`` + ``render``) per frame, the scene picture's own redraws (``_draw``) within it and how
    often they ran; with the sweep, the sweep-only composites. Then standing at the lights with nothing moving
    (``standing``, 3 s at 20 loop passes a second): how many passes need a frame, with and without the sweep."""
    import time
    import pygame
    pygame.init()
    pygame.display.set_mode(SIZE)
    surf = pygame.Surface(SIZE)
    out: Dict[str, Dict[str, float]] = {}
    for label, radar_on, tune in (("road_view", False, {}), ("radar_view", True, {}),
                                  ("radar_sweep", True, {"radar_sweep": True})):
        mods = dict(_BASE, vw=city(29.0), tuning=tune)    # the city street from 23 s on: frames 22.5 .. 37.5 s
        sim = make_sim(22.5, radar_on, mods, toggle_at=21.0, surf=surf)
        page = sim.screen.scene_page
        pic = page.radar if radar_on else page.scene
        spent = {"draw": [0.0, 0], "compose": [0.0, 0]}

        def wrap(fn, key):
            def timed(*a, **k):
                t0 = time.perf_counter()
                r = fn(*a, **k)
                spent[key][0] += time.perf_counter() - t0
                spent[key][1] += 1
                return r
            return timed
        pic._draw = wrap(pic._draw, "draw")
        if radar_on:
            pic._compose = wrap(pic._compose, "compose")
        dt = 1.0 / 20.0
        start = time.perf_counter()
        for _ in range(frames):
            sim.step(dt)
            sim.frame(surf)
        frame_ms = (time.perf_counter() - start) / frames * 1000.0
        sweeps = pic.sweeps if radar_on else 0
        out[label] = {"frame_ms": frame_ms, "draw_ms": spent["draw"][0] / max(1, spent["draw"][1]) * 1000.0,
                      "redraws_per_frame": spent["draw"][1] / float(frames),
                      "compose_ms": spent["compose"][0] / max(1, spent["compose"][1]) * 1000.0,
                      "sweeps_per_frame": sweeps / float(frames)}
    for label, tune in (("standing_radar", {}), ("standing_sweep", {"radar_sweep": True})):
        mods = dict(_BASE, vw=standing(38.0), tuning=tune)
        sim = make_sim(38.0, True, mods, toggle_at=35.0, surf=surf)
        page = sim.screen.scene_page
        need, n, t_frames = 0, 60, 0.0
        for _ in range(n):
            sim.step(0.05)
            if page.needs_frame():
                need += 1
                t0 = time.perf_counter()
                sim.frame(surf)
                t_frames += time.perf_counter() - t0
        out[label] = {"frames_per_s": need / (n * 0.05), "frame_ms": t_frames / max(1, need) * 1000.0}
    return out


def run_live(scale: int = 3) -> None:
    import pygame
    pygame.init()
    win = pygame.display.set_mode((SIZE[0] * scale, SIZE[1] * scale))
    pygame.display.set_caption("MFD radar view demo (SPACE: toggle, 1-9: situation)")
    surf = pygame.Surface(SIZE)
    clock = pygame.time.Clock()
    makers = (city, obstacle, threat, crossing, far, no_radar, passed, cross_traffic, ageing)
    which = 0
    sim = make_sim(20.0, True, dict(_BASE, vw=makers[which](10_000.0)))
    while True:
        dt = clock.tick(20) / 1000.0
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE):
                return
            if ev.type == pygame.KEYDOWN and ev.key == pygame.K_SPACE:
                sim.screen._toggle_radar()
            if ev.type == pygame.KEYDOWN and pygame.K_1 <= ev.key <= pygame.K_9:
                which = ev.key - pygame.K_1
                sim = make_sim(20.0, True, dict(_BASE, vw=makers[which](sim.t + 30.0)))
        if sim.t >= 29.5:
            sim = make_sim(20.0, sim.screen.scene_page.radar_on, dict(_BASE, vw=makers[which](30.0)))
        sim.step(dt)
        sim.frame(surf)
        pygame.transform.scale(surf, win.get_size(), win)
        pygame.display.flip()


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--shots", metavar="DIR")
    ap.add_argument("--only", help="comma list of shot names")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--scale", type=int, default=3)
    a = ap.parse_args(argv)
    if a.shots or a.bench:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    if a.shots:
        only = [x.strip() for x in a.only.split(",")] if a.only else None
        for p in render_shots(a.shots, a.scale, only):
            print(p)
    if a.bench:
        for k, v in bench().items():
            if "draw_ms" in v:
                print("%-14s frame %.3f ms  picture redraw %.3f ms  (redraws/frame %.2f)  sweep composite %.3f ms "
                      "(per frame %.2f)" % (k, v["frame_ms"], v["draw_ms"], v["redraws_per_frame"], v["compose_ms"],
                                            v["sweeps_per_frame"]))
            else:
                print("%-14s %.1f frames/s needed, %.3f ms per frame" % (k, v["frames_per_s"], v["frame_ms"]))
    if a.live:
        run_live(a.scale)
    return 0


if __name__ == "__main__":
    sys.exit(main())
