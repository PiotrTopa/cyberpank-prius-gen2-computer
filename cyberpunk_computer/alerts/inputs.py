"""
Alert inputs: one flat snapshot per evaluation, the only place that names
state fields and scene keys for the alert functions.

Every value is ``None`` when it is not known. Sources (verified in the code,
2026-11-15):

==================  ==========================================================
true speed          ``vehicle.real_speed_kmh`` (CAN 0x0B4 / wheel speeds);
                    the speedometer value ``vehicle.display_speed_kmh`` (BEAN
                    FE:24, reads ~12 % high) is kept only for display
gear, READY, ACC    ``vehicle.gear``, ``vehicle.ready_mode``, ``powerbox.acc_on``
                    or ``vehicle.acc_on`` (BEAN FE:83)
brake               ``vehicle.brake_switch`` (BEAN FE:29) else
                    ``vehicle.brake_pressed`` > 0 (CAN 0x030, 0..127)
accelerator         ``vehicle.throttle_position`` (CAN 0x244, third-party scale)
steering            ``dynamics.steering_angle`` (CAN 0x025, 1.5 deg, + = left)
outside temperature ``climate.outside_temp`` (AVC E0 5D F7, BEAN FE:CD fallback,
                    whole degrees; the only writer, None when stale)
climate             ``climate.*`` from AVC E0 5D F3 (or BEAN FE:FC)
body (BEAN)         ``bean.decoded``: doors_locked, *_door_open, lock_event,
                    lamp_tail, lamp_head, odometer_km, climate_buttons
12 V                ``powerbox.system_voltage`` (INA219 on the computer's feed)
hybrid 21C3         ``vehicle.regen_torque_actual``, ``master_cylinder_torque``
                    (solicited, 1 Hz)
fuel                ``vehicle.fuel_level`` (CAN 0x5A4, assumed litres; the
                    state default 30 is a placeholder, so ``fuel_seen`` says
                    whether a real value arrived), consumption figures
DTCs                ``diagnostics.stored_dtcs`` / ``pending_dtcs`` and
                    ``diagnostics.ecus[*].dtcs`` (filled by scans the backend
                    already runs; no polling is added here)
scene               the ``roadsense_scene`` mapping the backend publishes for
                    the MFD (docs/MFD_MAIN_SCREEN.md section 7): lead, lanes,
                    ego light, vision limit, ``map`` (fused facts)
turn signal         not decoded on any bus: always "unknown"
==================  ==========================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple

KMH = 3.6

# Limit sources the speed-limit warning trusts at the normal bar.
STRONG_LIMIT_SOURCES = frozenset({"sign", "sign_held", "map"})
# Weak: statutory defaults and implicit map limits need the higher bar.
WEAK_LIMIT_SOURCES = frozenset({"map_implicit", "vision_default", "default"})


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _get(obj: Any, *path: str) -> Any:
    for p in path:
        if obj is None:
            return None
        if isinstance(obj, Mapping):
            obj = obj.get(p)
        else:
            obj = getattr(obj, p, None)
    return obj


def bean_value(decoded: Any, name: str) -> Any:
    entry = decoded.get(name) if isinstance(decoded, Mapping) else None
    return entry.get("value") if isinstance(entry, Mapping) else None


def bean_ts(decoded: Any, name: str) -> Optional[float]:
    entry = decoded.get(name) if isinstance(decoded, Mapping) else None
    return _num(entry.get("ts")) if isinstance(entry, Mapping) else None


@dataclass(frozen=True)
class Lead:
    d: float                         # m from the camera
    y: float = 0.0                   # m, right positive (roadsense frame)
    v_rel: Optional[float] = None    # m/s, negative = closing
    a: Optional[float] = None        # m/s2 (the lead's)
    prob: Optional[float] = None     # vision probability (None for the tracked lead)
    src: str = "vision"              # "tracked" (roadsense fused/lead topic) | "vision" (modeld leads[0])
    standing: Optional[bool] = None  # tracked only: ego and lead stand (hold mode)
    departed: Optional[bool] = None  # tracked only: the lead moved off while ego stood
    ttc_s: Optional[float] = None
    gap_s: Optional[float] = None
    track_id: Optional[int] = None


@dataclass(frozen=True)
class Limit:
    kmh: int
    src: str                         # sign | sign_held | map | map_implicit | vision_default | default | unknown | ...
    conf: Optional[float] = None
    agree: Optional[str] = None      # agree | conflict | vision_only | map_only | none
    variable: bool = False
    from_map_layer: bool = False     # True = the fused value (map usable), False = the camera engine

    @property
    def strong(self) -> bool:
        return self.src in STRONG_LIMIT_SOURCES

    @property
    def weak(self) -> bool:
        return self.src in WEAK_LIMIT_SOURCES


@dataclass(frozen=True)
class AlertInputs:
    now: float = 0.0                       # monotonic s
    wall: float = 0.0                      # wall clock s (display / logs only)
    # ── vehicle ──
    real_kmh: Optional[float] = None
    display_kmh: Optional[float] = None
    gear: Optional[str] = None             # PARK | REVERSE | NEUTRAL | DRIVE | B
    ready: bool = False
    acc: bool = False
    ig: bool = False
    brake: Optional[bool] = None
    brake_level: Optional[float] = None    # 0..127
    throttle_pct: Optional[float] = None
    steering_deg: Optional[float] = None
    turn_signal: str = "unknown"           # not decoded on any bus
    # ── climate ──
    outside_c: Optional[float] = None
    climate_live: bool = False             # a fresh AVC/BEAN climate status exists
    climate: Dict[str, Any] = field(default_factory=dict)
    climate_buttons_ts: Optional[float] = None   # BEAN FE:FB (a climate key pressed), wall ts
    # ── body (BEAN) ──
    doors_locked: Optional[bool] = None
    door_open: Optional[bool] = None
    door_open_which: Tuple[str, ...] = ()
    lamp_tail: Optional[bool] = None
    lamp_head: Optional[bool] = None
    lock_event: Optional[str] = None
    lock_event_ts: Optional[float] = None  # wall ts of the last lock/unlock event frame
    door_ts: Optional[float] = None        # wall ts of the last door-state frame
    bean_frames: int = 0
    odometer_km: Optional[float] = None
    # ── power ──
    v12: Optional[float] = None
    powerbox_fresh: bool = False
    # ── hybrid / fuel ──
    regen_nm: Optional[float] = None
    master_cyl_nm: Optional[float] = None
    hybrid_seq: int = 0                    # bumps on every 21C3 update seen
    fuel_l: Optional[float] = None         # None unless a real 0x5A4 value arrived
    trip_fuel_l: Optional[float] = None
    drive_km: Optional[float] = None
    meter_avg_l100: Optional[float] = None
    drive_avg_l100: Optional[float] = None
    # ── diagnostics ──
    dtcs: FrozenSet[str] = frozenset()     # "ECU:CODE"
    dtc_source_seen: bool = False          # any scan result is in the state
    mil_on: bool = False
    # ── scene (roadsense) ──
    scene_age: Optional[float] = None      # s since the last scene event; None = never
    roadsense_running: bool = False
    lead: Optional[Lead] = None
    lead_tracked_available: bool = False   # the fused topic carries the tracker's lead
    lanes: Tuple[Optional[Dict[str, Any]], ...] = ()
    light: Optional[Dict[str, Any]] = None
    limit: Optional[Limit] = None
    map_ok: bool = False                   # map layer usable (ok, not synthetic)
    map_conf: Optional[float] = None
    tunnel: Optional[Dict[str, Any]] = None
    built_up: Optional[bool] = None
    ahead: Dict[str, Any] = field(default_factory=dict)   # map look-ahead (junction, curve, limit, feature)
    signs_ahead: Tuple[Dict[str, Any], ...] = ()
    env: Dict[str, Any] = field(default_factory=dict)

    @property
    def real_mps(self) -> Optional[float]:
        return None if self.real_kmh is None else self.real_kmh / KMH

    @property
    def stopped(self) -> bool:
        return self.real_kmh is not None and self.real_kmh < 0.5

    @property
    def powered(self) -> bool:
        return self.ready or self.acc or self.ig

    @property
    def scene_fresh(self) -> bool:
        return self.scene_age is not None and self.scene_age < 1.0

    def compact(self) -> Dict[str, Any]:
        """The standard inputs logged with every fire (shadow review)."""
        out: Dict[str, Any] = {
            "real_kmh": None if self.real_kmh is None else round(self.real_kmh, 1),
            "display_kmh": self.display_kmh, "gear": self.gear, "ready": self.ready,
            "brake": self.brake, "steer": self.steering_deg, "outside_c": self.outside_c,
            "scene_age": None if self.scene_age is None else round(self.scene_age, 2),
        }
        if self.limit is not None:
            out["limit"] = {"kmh": self.limit.kmh, "src": self.limit.src, "conf": self.limit.conf,
                            "agree": self.limit.agree, "map": self.limit.from_map_layer}
        if self.lead is not None:
            ld = self.lead
            out["lead"] = {"d": ld.d, "y": ld.y, "v_rel": ld.v_rel, "prob": ld.prob, "src": ld.src}
        if self.map_ok:
            out["map_conf"] = self.map_conf
        return out


# ── builders ─────────────────────────────────────────────────────────────────

def _scene_limit(scene: Mapping, map_limit: bool = True) -> Tuple[Optional[Limit], bool]:
    """The limit the MFD shows (same rule as ui/scene/inputs.py): the fused
    value when the map layer is usable, else the camera engine's."""
    mp = scene.get("map") if isinstance(scene.get("map"), Mapping) else None
    usable = bool(mp and mp.get("ok") and not mp.get("syn")
                  and (_get(mp, "in", "gnss") != "synthetic"))
    if map_limit and usable:
        ml = mp.get("limit")
        if isinstance(ml, Mapping) and _num(ml.get("kmh")):
            return Limit(int(ml["kmh"]), str(ml.get("src") or "unknown"), _num(ml.get("conf")),
                         ml.get("agree") if isinstance(ml.get("agree"), str) else None,
                         bool(ml.get("var")), True), usable
    lim = scene.get("limit")
    if isinstance(lim, Mapping) and _num(lim.get("kmh")):
        return Limit(int(lim["kmh"]), str(lim.get("src") or "unknown"), _num(lim.get("conf")),
                     None, False, False), usable
    return None, usable


def _scene_lead(scene: Mapping) -> Tuple[Optional[Lead], bool]:
    """The tracked lead, else modeld's ``leads[0]``.

    1. roadsense's ``lead`` topic (10-14/s): scene ``lead`` with
       ``lead_src: "tracked"`` (null = the tracker has no lead);
    2. the same tracker's copy in ``fused`` (about 2/s), scene ``map.lead``;
    3. modeld's raw ``leads[0]`` (scene ``lead`` without ``lead_src``).
    """
    mp = scene.get("map") if isinstance(scene.get("map"), Mapping) else None
    tl = mp.get("lead") if mp else None
    if scene.get("lead_src") == "tracked":
        ld = scene.get("lead")
        if isinstance(ld, Mapping) and _num(ld.get("d")):
            return Lead(d=float(ld["d"]), y=_num(ld.get("y")) or 0.0, v_rel=_num(ld.get("v_rel")),
                        a=_num(ld.get("a")), prob=None, src="tracked",
                        standing=bool(ld.get("standing")), departed=bool(ld.get("departed")),
                        ttc_s=_num(ld.get("ttc")), gap_s=_num(ld.get("gap")),
                        track_id=ld.get("id") if isinstance(ld.get("id"), int) else None), True
        if scene.get("lead_departed") is True:
            # lead gone from the tracker but the departed cue is still up
            return Lead(d=0.0, src="tracked", departed=True, standing=False), True
        return None, True
    tracked_avail = isinstance(tl, Mapping)
    if tracked_avail and tl.get("present") and _num(tl.get("d")):
        return Lead(d=float(tl["d"]), y=_num(tl.get("y")) or 0.0, v_rel=_num(tl.get("v_rel")),
                    a=_num(tl.get("a")), prob=None, src="tracked",
                    standing=bool(tl.get("standing")), departed=bool(tl.get("departed")),
                    ttc_s=_num(tl.get("ttc")), gap_s=_num(tl.get("gap")),
                    track_id=tl.get("id") if isinstance(tl.get("id"), int) else None), True
    if tracked_avail and tl.get("departed"):
        # lead gone from the tracker but the departed cue is still up
        return Lead(d=_num(tl.get("d")) or 0.0, src="tracked", departed=True,
                    standing=False), True
    ld = scene.get("lead")
    if isinstance(ld, Mapping) and _num(ld.get("d")):
        return Lead(d=float(ld["d"]), y=_num(ld.get("y")) or 0.0, v_rel=_num(ld.get("v_rel")),
                    a=_num(ld.get("a")), prob=_num(ld.get("prob")), src="vision"), tracked_avail
    return None, tracked_avail


def scene_fields(scene: Optional[Mapping], scene_age: Optional[float]) -> Dict[str, Any]:
    if not isinstance(scene, Mapping) or scene_age is None or scene_age > 3.0:
        return {"scene_age": scene_age}
    limit, usable = _scene_limit(scene)
    lead, tracked = _scene_lead(scene)
    mp = scene.get("map") if isinstance(scene.get("map"), Mapping) else {}
    lanes = scene.get("lanes") if isinstance(scene.get("lanes"), (list, tuple)) else ()
    light = scene.get("light") if isinstance(scene.get("light"), Mapping) else None
    signs = scene.get("signs") if isinstance(scene.get("signs"), Mapping) else {}
    ahead = mp.get("ahead") if usable and isinstance(mp.get("ahead"), Mapping) else {}
    bu = mp.get("built_up") if isinstance(mp.get("built_up"), Mapping) else None
    tn = mp.get("tunnel") if isinstance(mp.get("tunnel"), Mapping) else None
    if tn is not None and not usable:
        # without a usable map only the vision part of the tunnel fact counts
        tn = dict(tn, map_usable=False)
    elif tn is not None:
        tn = dict(tn, map_usable=True)
    return {
        "scene_age": scene_age, "lead": lead, "lead_tracked_available": tracked,
        "lanes": tuple(lanes), "light": dict(light) if light else None, "limit": limit,
        "map_ok": usable, "map_conf": _num(mp.get("conf")) if usable else None,
        "tunnel": tn, "built_up": bu.get("v") if bu and isinstance(bu.get("v"), bool) else None,
        "ahead": dict(ahead), "signs_ahead": tuple(x for x in (signs.get("ahead") or ()) if isinstance(x, Mapping)),
        "env": dict(scene.get("env")) if isinstance(scene.get("env"), Mapping) else {},
    }


def _dtcs(diag: Any) -> Tuple[FrozenSet[str], bool, bool]:
    codes = set()
    seen = False
    for tup in (_get(diag, "stored_dtcs") or ()):
        if isinstance(tup, (list, tuple)) and tup:
            codes.add("%s:%s" % (tup[1] if len(tup) > 1 else "?", tup[0]))
            seen = True
    if _get(diag, "last_scan_time"):
        seen = True
    ecus = _get(diag, "ecus")
    if isinstance(ecus, Mapping):
        for name, r in ecus.items():
            if not isinstance(r, Mapping):
                continue
            if r.get("responded"):
                seen = True
            for d in r.get("dtcs") or ():
                code = d.get("code") if isinstance(d, Mapping) else d
                if isinstance(code, str) and code:
                    codes.add("%s:%s" % (name, code))
    return frozenset(codes), seen, bool(_get(diag, "mil_on"))


def build_inputs(state: Any, scene: Optional[Mapping], scene_age: Optional[float],
                 now: float, wall: float, seen: Optional[Mapping[str, Any]] = None) -> AlertInputs:
    """AlertInputs from an AppState (or anything shaped like it), the latest
    scene and the service's "seen" markers (``fuel``: a real fuel level
    arrived, ``hybrid_seq``: 21C3 updates counted, ``climate_t``: monotonic
    time of the last climate status)."""
    seen = seen or {}
    v = _get(state, "vehicle")
    pb = _get(state, "powerbox")
    cl = _get(state, "climate")
    decoded = _get(state, "bean", "decoded") or {}
    gear = _get(v, "gear")
    gear_name = getattr(gear, "name", None) if gear is not None else None
    bs = _get(v, "brake_switch")
    bp = _num(_get(v, "brake_pressed"))
    brake = bool(bs) if bs is not None else (None if bp is None else bp > 0)
    acc = bool(_get(pb, "acc_on")) or bool(_get(v, "acc_on"))
    which = tuple(n for n, k in (("driver", "driver_door_open"), ("passenger", "passenger_door_open"),
                                 ("rear", "rear_door_open")) if bean_value(decoded, k))
    door_open = None
    for k in ("any_door_open", "door_open"):
        val = bean_value(decoded, k)
        if val is not None:
            door_open = bool(val) or bool(door_open)
    if door_open is None and _get(state, "dynamics", "door_open"):
        door_open = True
    climate_t = seen.get("climate_t")
    climate_live = (_get(cl, "status_source") is not None and climate_t is not None
                    and now - climate_t < 15.0)
    climate = {}
    if cl is not None:
        for k in ("system_on", "recirculation", "fresh_air", "ac_on", "auto_mode", "defrost",
                  "rear_defogger", "fan_speed", "air_direction", "target_temp", "status_source"):
            climate[k] = _get(cl, k)
    dtcs, dtc_seen, mil = _dtcs(_get(state, "diagnostics"))
    odo = _num(bean_value(decoded, "odometer_km"))
    pb_t = _num(_get(pb, "last_update_time"))
    rs_state = _get(state, "roadsense", "state")
    fields = dict(
        now=now, wall=wall,
        real_kmh=_num(_get(v, "real_speed_kmh")),
        display_kmh=_num(_get(v, "display_speed_kmh")),
        gear=gear_name, ready=bool(_get(v, "ready_mode")), acc=acc, ig=bool(_get(v, "ig_on")),
        brake=brake, brake_level=bp, throttle_pct=_num(_get(v, "throttle_position")),
        steering_deg=_num(_get(state, "dynamics", "steering_angle")),
        outside_c=_num(_get(cl, "outside_temp")), climate_live=climate_live, climate=climate,
        climate_buttons_ts=bean_ts(decoded, "climate_buttons"),
        doors_locked=bean_value(decoded, "doors_locked"),
        door_open=door_open, door_open_which=which,
        lamp_tail=bean_value(decoded, "lamp_tail"), lamp_head=bean_value(decoded, "lamp_head"),
        lock_event=bean_value(decoded, "lock_event"), lock_event_ts=bean_ts(decoded, "lock_event"),
        door_ts=bean_ts(decoded, "any_door_open"),
        bean_frames=int(_get(state, "bean", "frames_total") or 0),
        odometer_km=odo,
        v12=_num(_get(pb, "system_voltage")),
        powerbox_fresh=bool(_get(pb, "connected")) and pb_t is not None and wall - pb_t < 10.0,
        regen_nm=_num(_get(v, "regen_torque_actual")),
        master_cyl_nm=_num(_get(v, "master_cylinder_torque")),
        hybrid_seq=int(seen.get("hybrid_seq") or 0),
        fuel_l=_num(_get(v, "fuel_level")) if seen.get("fuel") else None,
        trip_fuel_l=_num(_get(v, "trip_fuel_consumed")),
        drive_km=_num(_get(v, "drive_distance_km")),
        meter_avg_l100=_num(_get(v, "meter_avg_consumption_l100km")),
        drive_avg_l100=_num(_get(v, "drive_avg_consumption_l100km")),
        dtcs=dtcs, dtc_source_seen=dtc_seen, mil_on=mil,
        roadsense_running=rs_state == "running",
    )
    fields.update(scene_fields(scene, scene_age))
    return AlertInputs(**fields)


def lane_line_y0(lanes: Tuple[Optional[Dict[str, Any]], ...], idx: int, sample: int = 1) -> Optional[float]:
    """Lateral offset (m, right positive) of lane line ``idx`` at scene sample
    ``sample`` (default index 1 = 5 m ahead), or None."""
    if idx >= len(lanes):
        return None
    ln = lanes[idx]
    if not isinstance(ln, Mapping):
        return None
    ys = ln.get("y")
    if not isinstance(ys, (list, tuple)) or len(ys) <= sample:
        return None
    return _num(ys[sample])


def lane_prob(lanes: Tuple[Optional[Dict[str, Any]], ...], idx: int) -> float:
    if idx >= len(lanes) or not isinstance(lanes[idx], Mapping):
        return 0.0
    return _num(lanes[idx].get("prob")) or 0.0


__all__ = ["AlertInputs", "Lead", "Limit", "build_inputs", "scene_fields", "bean_value",
           "bean_ts", "lane_line_y0", "lane_prob", "STRONG_LIMIT_SOURCES", "WEAK_LIMIT_SOURCES"]
