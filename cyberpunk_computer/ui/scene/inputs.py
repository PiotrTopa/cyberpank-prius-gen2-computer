"""
Scene inputs: everything the main-screen scene reads, in one place.

``read_inputs(state)`` turns the AppState into a flat, normalised
``SceneInputs``. All field-name knowledge lives here, so the scene never
touches the state tree directly and a renamed/added backend field is a
one-line change. Fields that do not exist (yet) read as ``None`` = unknown;
the scene draws nothing for unknown data.

Road perception arrives as a compact "scene" mapping (contract in
docs/MFD_MAIN_SCREEN.md), looked up in this order:

1. the ``roadsense_scene`` backend event (``SceneFeed``; fast path, 5-10 Hz),
2. ``state.roadsense.scene`` (state field, 1 Hz publish),
3. ``state.roadsense.health`` for the slow items only (speed limit, ego
   traffic light, environment labels) -- this is what exists today.

Pure Python (no pygame). The records are NamedTuples, not dataclasses: a
dataclass costs ~1 ms of import time each on a desktop, which is tens of
milliseconds per class on the Pi Zero.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping   # isinstance on typing.Mapping is several times slower
from typing import Any, Callable, NamedTuple, Optional, Sequence, Tuple

# Default sample distances (metres ahead of the camera) of lane/edge/path
# polylines when the scene mapping carries no "x".
DEFAULT_XS: Tuple[float, ...] = (0.0, 5.0, 10.0, 15.0, 20.0, 30.0, 40.0, 50.0, 65.0, 80.0, 100.0)

DOOR_KEYS = ("door_fl", "door_fr", "door_rl", "door_rr", "hatch")

SCENE_EVENT = "roadsense_scene"


class Lead(NamedTuple):
    dist_m: float                    # camera -> rear of the vehicle ahead
    lat_m: float = 0.0               # lateral offset, LEFT positive
    v_rel: Optional[float] = None    # m/s, negative = closing
    prob: float = 1.0
    kind: str = "car"
    accel: Optional[float] = None    # m/s2 (the model's estimate), negative = braking


class RoadLine(NamedTuple):
    ys: Tuple[float, ...]            # lateral offsets at RoadScene.xs, LEFT positive
    prob: float = 1.0


class RoadObject(NamedTuple):
    kind: str                        # car | truck | bus | bike | person | lamp | cone | ...
    x: float                         # metres ahead
    y: float                         # LEFT positive
    state: str = ""                  # kind-specific (e.g. lamp: "on")


class SignItem(NamedTuple):
    """A rule in effect (``m`` None) or a warning sign ahead (``m`` metres)."""
    id: str                          # roadsense rule id: no_overtaking, priority_road, ...
    code: str = ""                   # sign code as roadsense prints it: "B-25"
    value: Optional[float] = None    # e.g. 20 for a living street
    side: str = ""                   # "l" / "r" for rules that bind one side of the road
    unsure: bool = False             # roadsense marks the entry uncertain
    m: Optional[int] = None          # expected distance of a warning's hazard


class TrafficLight(NamedTuple):
    state: str                       # red | yellow | red_yellow | green | off | unknown
    dist_m: Optional[float] = None
    lat_m: Optional[float] = None    # LEFT positive
    flashing: bool = False


class RoadScene(NamedTuple):
    seq: int
    xs: Tuple[float, ...] = DEFAULT_XS
    lanes: Tuple[Optional[RoadLine], ...] = (None, None, None, None)   # far-left, left, right, far-right
    edges: Tuple[Optional[RoadLine], ...] = (None, None)               # left, right
    lead: Optional[Lead] = None
    objects: Tuple[RoadObject, ...] = ()
    path: Optional[RoadLine] = None                                    # the model's planned path


class SceneInputs(NamedTuple):
    # vehicle
    speed_kmh: float = 0.0                 # for display: the speedometer value when known
    real_speed_kmh: float = 0.0            # true speed: motion detection, time gap
    gear: str = "P"
    ready: bool = False
    acc_on: bool = False
    ice_running: Optional[bool] = None
    # body: True/False, None = unknown
    doors: Tuple[Optional[bool], ...] = (None, None, None, None, None)   # DOOR_KEYS order
    # The car has ONE switch bit for "a rear door or the back door": when
    # that is all we know, rear_group carries it and the three rear closures
    # stay None (the scene marks the group, it does not pick a door).
    rear_group: Optional[bool] = None
    door_any: Optional[bool] = None        # "a door is open" indicator (CAN 0x57F / BEAN FE:7F)
    head: Optional[bool] = None            # low beam
    high: Optional[bool] = None
    tail: Optional[bool] = None            # tail / position lamps
    fog: Optional[bool] = None
    brake: Optional[bool] = None
    # cruise
    cruise_active: bool = False
    cruise_main: Optional[bool] = None     # main switch; None = not known
    cruise_set_kmh: Optional[int] = None
    cruise_mem_kmh: Optional[int] = None
    cruise_gap: Optional[int] = None       # adaptive-cruise gap setting 1..3 (no source yet)
    # perception
    rs_state: str = ""                     # RoadsenseState.state ("" = not configured)
    rs_ok: bool = False                    # stack running and API answering
    road: Optional[RoadScene] = None
    limit_kmh: Optional[int] = None
    limit_source: str = ""                 # sign | default | ...
    limit_conf: Optional[float] = None     # roadsense's 0..1 heuristic
    signs_on: Tuple[SignItem, ...] = ()    # rules in effect (without the speed limit)
    signs_ahead: Tuple[SignItem, ...] = () # warning signs, nearest first
    light: Optional[TrafficLight] = None
    env_light: Optional[str] = None        # day | dusk_dawn | night
    env_place: Optional[str] = None
    env_weather: Optional[str] = None

    @property
    def any_door_open(self) -> bool:
        return (any(d is True for d in self.doors) or bool(self.rear_group)
                or bool(self.door_any))

    @property
    def doors_known(self) -> bool:
        return (any(d is not None for d in self.doors) or self.rear_group is not None
                or self.door_any is not None)


class SceneFeed:
    """Latest-only hand-off of ``roadsense_scene`` events (client thread ->
    main loop). ``push`` has the backend event-callback signature."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._latest: Optional[Mapping] = None
        self._t = 0.0
        self.received = 0

    def push(self, name: str, data: Any) -> None:
        if name != SCENE_EVENT or not isinstance(data, Mapping):
            return
        with self._lock:
            self._latest = data
            self._t = self._clock()
            self.received += 1

    def latest(self, max_age_s: float = 6.0) -> Optional[Mapping]:
        with self._lock:
            if self._latest is None or self._clock() - self._t > max_age_s:
                return None
            return self._latest


FEED = SceneFeed()


# ── defensive readers ────────────────────────────────────────────────────────

def _first(objs: Sequence[Any], names: Sequence[str]) -> Any:
    """First non-None attribute among ``names`` on any of ``objs``."""
    for o in objs:
        if o is None:
            continue
        for n in names:
            v = getattr(o, n, None)
            if v is not None:
                return v
    return None


def _bean(decoded: Any, name: str) -> Any:
    """Value of a BEAN decoded entry ({value, ts, src}) or None."""
    if not isinstance(decoded, Mapping):
        return None
    e = decoded.get(name)
    if isinstance(e, Mapping):
        return e.get("value")
    return None


def _opt_bool(v: Any) -> Optional[bool]:
    return None if v is None else bool(v)


def _num(v: Any) -> Optional[float]:
    if type(v) is float:          # the common case, first (Pi Zero: ~20k calls/s)
        return None if v != v else v
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v != v:   # NaN
        return None
    return float(v)


def _line(raw: Any, n: int) -> Optional[RoadLine]:
    """{"y": [...], "prob": p} or a bare list; y RIGHT positive on the wire."""
    if raw is None:
        return None
    prob = 1.0
    ys = raw
    if isinstance(raw, Mapping):
        ys = raw.get("y")
        p = _num(raw.get("prob"))
        if p is not None:
            prob = p
    if not isinstance(ys, (list, tuple)) or len(ys) < 2:
        return None
    vs = ys[:n]
    # the wire carries floats: one comprehension (NaN and non-floats fall
    # through to the checked loop below)
    out = [-v for v in vs if type(v) is float and v == v]
    if len(out) == len(vs):
        return RoadLine(tuple(out), prob)
    out = []
    for v in vs:
        f = _num(v)
        if f is None:
            return None
        out.append(-f)
    return RoadLine(tuple(out), prob)


def parse_scene(raw: Any) -> Optional[RoadScene]:
    """Scene mapping (wire format, y RIGHT positive) -> RoadScene."""
    if not isinstance(raw, Mapping):
        return None
    xs_raw = raw.get("x")
    xs: Tuple[float, ...] = DEFAULT_XS
    if isinstance(xs_raw, (list, tuple)) and len(xs_raw) >= 2:
        vals = [_num(v) for v in xs_raw]
        if all(v is not None for v in vals):
            xs = tuple(vals)   # type: ignore[arg-type]
    n = len(xs)
    lanes_raw = raw.get("lanes") or ()
    edges_raw = raw.get("edges") or ()
    lanes = tuple(_line(lanes_raw[i], n) if i < len(lanes_raw) else None for i in range(4))
    edges = tuple(_line(edges_raw[i], n) if i < len(edges_raw) else None for i in range(2))
    lead = None
    lr = raw.get("lead")
    if isinstance(lr, Mapping):
        d = _num(lr.get("d"))
        if d is not None and d > 0:
            y = _num(lr.get("y")) or 0.0
            p = _num(lr.get("prob"))
            lead = Lead(d, -y, _num(lr.get("v_rel")), 1.0 if p is None else p,
                        str(lr.get("kind") or "car"), _num(lr.get("a")))
    objs = []
    for o in raw.get("objects") or ():
        if not isinstance(o, Mapping):
            continue
        x, y = _num(o.get("x")), _num(o.get("y"))
        if x is None or y is None:
            continue
        objs.append(RoadObject(str(o.get("kind") or ""), x, -y, str(o.get("state") or "")))
    seq = raw.get("seq")
    return RoadScene(int(seq) if isinstance(seq, (int, float)) else 0,
                     xs, lanes, edges, lead, tuple(objs), _line(raw.get("path"), n))


def parse_signs(raw: Any) -> Tuple[Tuple[SignItem, ...], Tuple[SignItem, ...]]:
    """Scene ``signs`` mapping -> (in effect, ahead)."""
    if not isinstance(raw, Mapping):
        return (), ()

    def items(lst: Any, ahead: bool) -> Tuple[SignItem, ...]:
        out = []
        for e in lst if isinstance(lst, (list, tuple)) else ():
            if not isinstance(e, Mapping):
                continue
            rid = e.get("id")
            if not isinstance(rid, str) or not rid:
                continue
            m = _num(e.get("m")) if ahead else None
            side = e.get("side")
            out.append(SignItem(rid, str(e.get("s") or ""), _num(e.get("v")),
                                side if side in ("l", "r") else "", bool(e.get("q")),
                                None if m is None else int(m)))
        return tuple(out)

    return items(raw.get("on"), False), items(raw.get("ahead"), True)


def _traffic_light(raw: Any) -> Optional[TrafficLight]:
    if not isinstance(raw, Mapping):
        return None
    st = raw.get("state")
    if not isinstance(st, str) or not st:
        return None
    d = _num(raw.get("dist_m", raw.get("d")))
    y = _num(raw.get("lateral_m", raw.get("y")))
    return TrafficLight(st, d, None if y is None else -y, bool(raw.get("flashing")))


_GEAR = {"PARK": "P", "REVERSE": "R", "NEUTRAL": "N", "DRIVE": "D", "B": "B"}


def read_inputs(state: Any, feed: Optional[SceneFeed] = None) -> SceneInputs:
    """AppState (or anything shaped like it) -> SceneInputs."""
    veh = getattr(state, "vehicle", None)
    dyn = getattr(state, "dynamics", None)
    body = getattr(state, "body", None)
    pbx = getattr(state, "powerbox", None)
    decoded = getattr(getattr(state, "bean", None), "decoded", None)

    # Speed: the speedometer value for display (BEAN; None while BEAN is
    # silent), the true speed for everything physical.
    real = _num(_first((veh,), ("real_speed_kmh", "speed_kmh"))) or 0.0
    spd = _num(getattr(veh, "display_speed_kmh", None))
    if spd is None:
        spd = real
    gear = getattr(veh, "gear", None)
    gear = _GEAR.get(getattr(gear, "name", str(gear or "")), "P")

    # Doors. Explicit per-door state fields win if a backend ever has them;
    # today the BEAN body frame FE:B6 gives driver, front passenger and one
    # shared bit for rear-left / rear-right / back door.
    srcs = (body, dyn, veh)

    def door(names: Sequence[str], bean_name: str = "") -> Optional[bool]:
        v = _first(srcs, names)
        if v is None and bean_name:
            v = _bean(decoded, bean_name)
        return _opt_bool(v)

    doors = (
        door(("door_fl_open", "driver_door_open"), "driver_door_open"),
        door(("door_fr_open", "passenger_door_open"), "passenger_door_open"),
        door(("door_rl_open", "rear_left_door_open")),
        door(("door_rr_open", "rear_right_door_open")),
        door(("hatch_open", "back_door_open", "tailgate_open")),
    )
    rear_group = None
    if doors[2] is None and doors[3] is None and doors[4] is None:
        rear_group = _opt_bool(_bean(decoded, "rear_door_open"))
    door_any = _bean(decoded, "any_door_open")
    if door_any is None:
        door_any = _bean(decoded, "door_open")
    if door_any is None and getattr(dyn, "door_open", None):
        door_any = True          # the state default (False) is not evidence of "closed"
    door_any = _opt_bool(door_any)

    def lamp(bean_name: str, names: Sequence[str]) -> Optional[bool]:
        v = _bean(decoded, bean_name)
        if v is None:
            v = _first((body, dyn), names)
        return _opt_bool(v)

    head = lamp("lamp_head", ("low_beam", "lamp_low"))
    high = lamp("lamp_high_beam", ("high_beam",))
    tail = lamp("lamp_tail", ("parking_lights", "lamp_tail"))
    fog = lamp("lamp_fog_front", ("fog_front",))
    brake_v = getattr(veh, "brake_switch", None)
    if brake_v is None:
        bp = _num(getattr(veh, "brake_pressed", None))
        brake_v = None if bp is None else bp > 0
    ice = getattr(veh, "ice_running", None)
    if getattr(veh, "ice_stopped", None) is True:
        ice = False

    cs = _num(getattr(veh, "cruise_set_speed", None))
    cm = _num(getattr(veh, "cruise_memory_speed", None))
    cg = _num(_first((veh,), ("cruise_gap", "acc_gap")))
    return SceneInputs(
        speed_kmh=max(0.0, spd),
        real_speed_kmh=max(0.0, real),
        gear=gear,
        ready=bool(getattr(veh, "ready_mode", False)),
        acc_on=bool(getattr(pbx, "acc_on", False) or getattr(veh, "acc_on", False)),
        ice_running=_opt_bool(ice),
        doors=doors, rear_group=rear_group, door_any=door_any,
        head=head, high=high, tail=tail, fog=fog, brake=_opt_bool(brake_v),
        cruise_active=bool(getattr(veh, "cruise_active", False)),
        cruise_main=_opt_bool(getattr(veh, "cruise_main_switch", None)),
        cruise_set_kmh=int(cs) if cs else None,
        cruise_mem_kmh=int(cm) if cm else None,
        cruise_gap=int(cg) if cg else None,
        **read_perception(state, feed),
    )


def read_perception(state: Any, feed: Optional[SceneFeed] = None) -> dict:
    """The perception fields of ``SceneInputs`` (road scene, limit, signs,
    light, environment) from the state and the newest feed event. A new feed
    event changes only these: ``inputs._replace(**read_perception(...))``."""
    rs = getattr(state, "roadsense", None)
    rs_state = str(getattr(rs, "state", "") or "")
    rs_ok = rs_state == "running" and getattr(rs, "api_ok", None) is not False
    health = getattr(rs, "health", None)
    health = health if isinstance(health, Mapping) else {}
    raw_scene = None
    if feed is not None:
        raw_scene = feed.latest()
    if raw_scene is None:
        raw_scene = getattr(rs, "scene", None)
    if raw_scene is None:
        raw_scene = health.get("scene")
    raw_scene = raw_scene if isinstance(raw_scene, Mapping) else {}
    road = parse_scene(raw_scene) if raw_scene else None
    if (road is not None and road.seq == 0 and not any(road.lanes) and road.lead is None
            and road.path is None):
        road = None

    limit = raw_scene.get("limit") if raw_scene else None
    if not isinstance(limit, Mapping):
        limit = health.get("speed_limit")
    limit_kmh = None
    limit_src = ""
    limit_conf = None
    if isinstance(limit, Mapping):
        lk = _num(limit.get("kmh", limit.get("limit_kmh")))
        if lk is not None and lk > 0:
            limit_kmh = int(lk)
            limit_src = str(limit.get("src", limit.get("source")) or "")
            limit_conf = _num(limit.get("conf", limit.get("confidence")))
    signs_on, signs_ahead = parse_signs(raw_scene.get("signs") if raw_scene else None)
    light = _traffic_light(raw_scene.get("light") if raw_scene else None)
    if light is None:
        light = _traffic_light(health.get("lights"))
    env = raw_scene.get("env") if raw_scene else None
    if not isinstance(env, Mapping):
        env = health.get("env")
    env = env if isinstance(env, Mapping) else {}

    return dict(
        rs_state=rs_state,
        rs_ok=rs_ok,
        road=road if rs_ok else None,
        limit_kmh=limit_kmh if rs_ok else None,
        limit_source=limit_src,
        limit_conf=limit_conf,
        signs_on=signs_on if rs_ok else (),
        signs_ahead=signs_ahead if rs_ok else (),
        light=light if rs_ok else None,
        env_light=env.get("light") if rs_ok else None,
        env_place=env.get("place") if rs_ok else None,
        env_weather=env.get("weather") if rs_ok else None,
    )
