"""
Scene director: decides what the main-screen scene shows and animates it.

Pure Python state machine (no pygame), stepped once per UI frame with the
current ``SceneInputs``. The renderer only reads the public attributes.

Views
    ``view`` runs 0.0 (parked "hero" view of the car) .. 1.0 (chase view of
    the road). It opens when the car moves forward and closes again
    ``STOP_HOLD_S`` after it stopped (at once in P or with a door open).

Vision (road perception) status
    off    roadsense not running                -> arcade floor only
    wait   running, no road data received yet   -> floor + "NO ROAD DATA"
    live   fresh road data                      -> lanes, lead, objects
    stale  data older than ``STALE_S``          -> frozen lanes, dimmed,
                                                   no lead (never an old car)
    lost   data older than ``LOST_S``           -> floor + blinking "NO SIGNAL"
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

from .inputs import DOOR_KEYS, Lead, RoadScene, SceneInputs

VIEW_STEPS = 14            # camera positions between hero and chase (cache keys)
VIEW_SECONDS = 1.3         # duration of the opening/closing move
MOVE_ON_KMH = 3.0
MOVE_OFF_KMH = 1.0
STOP_HOLD_S = 2.5          # standstill before the scene closes (not in P)
DOOR_SECONDS = 0.55        # door swing time
DOOR_STEPS = 6             # quantisation of door angles (cache keys)
STALE_S = 2.5              # road data older than this is "stale"
LOST_S = 7.0               # ... and older than this is dropped
SMOOTH_TAU_S = 0.30        # low-pass on lane/lead geometry
LEAD_FADE_S = 0.4
REAR_GROUP_AJAR = 0.34     # opening shown for the shared rear/back door bit


def ease(t: float) -> float:
    """Smoothstep 0..1."""
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    return t * t * (3.0 - 2.0 * t)


class SceneDirector:
    def __init__(self) -> None:
        self.mode = "parked"            # parked | drive (the target view)
        self.view_raw = 0.0             # linear 0..1
        self.view = 0.0                 # eased
        self.view_step = 0              # 0..VIEW_STEPS
        self.moving = False
        self.door_frac: List[float] = [0.0] * len(DOOR_KEYS)
        self.door_step: Tuple[int, ...] = (0,) * len(DOOR_KEYS)
        self.odo = 0.0                  # metres travelled (floor scroll)
        self.vision = "off"
        self.lanes: List[Optional[List[float]]] = [None] * 4
        self.lane_prob: List[float] = [0.0] * 4
        self.edges: List[Optional[List[float]]] = [None] * 2
        self.xs: Tuple[float, ...] = ()
        self.lead: Optional[Lead] = None      # smoothed
        self.lead_alpha = 0.0                 # 0..1 appear/disappear
        self.objects: tuple = ()
        self.animating = True
        self.clock = 0.0
        self._stopped_for = 99.0
        self._road_seq: Optional[int] = None
        self._road_obj: Optional[RoadScene] = None
        self._road_age = 99.0
        self._have_road = False
        self._lost = False

    # -- per-frame step -------------------------------------------------------

    def update(self, inp: SceneInputs, dt: float) -> None:
        dt = max(0.0, min(dt, 0.5))
        self.clock += dt
        was = (self.view_step, self.door_step, self.vision)

        # movement with hysteresis
        if inp.real_speed_kmh >= MOVE_ON_KMH:
            self.moving = True
        elif inp.real_speed_kmh < MOVE_OFF_KMH:
            self.moving = False
        if self.moving:
            self._stopped_for = 0.0
            self.odo += inp.real_speed_kmh / 3.6 * dt
        else:
            self._stopped_for += dt

        forward = self.moving and inp.gear != "R"
        if forward and not inp.any_door_open:
            self.mode = "drive"
        elif (not self.moving) and (inp.gear == "P" or inp.any_door_open
                                    or self._stopped_for >= STOP_HOLD_S):
            self.mode = "parked"
        elif self.moving and inp.gear == "R":
            self.mode = "parked"

        # view animation
        target = 1.0 if self.mode == "drive" else 0.0
        step = dt / VIEW_SECONDS
        if self.view_raw < target:
            self.view_raw = min(target, self.view_raw + step)
        elif self.view_raw > target:
            self.view_raw = max(target, self.view_raw - step)
        self.view = ease(self.view_raw)
        self.view_step = int(round(self.view * VIEW_STEPS))

        # doors
        dstep = dt / DOOR_SECONDS
        moving_doors = False
        for i, d in enumerate(inp.doors):
            tgt = 1.0 if d else 0.0
            if d is None and i >= 2 and inp.rear_group:
                tgt = REAR_GROUP_AJAR      # "one of these": ajar, not open
            cur = self.door_frac[i]
            if cur != tgt:
                moving_doors = True
                cur = min(tgt, cur + dstep) if cur < tgt else max(tgt, cur - dstep)
                self.door_frac[i] = cur
        self.door_step = tuple(int(round(ease(f) * DOOR_STEPS)) for f in self.door_frac)

        self._update_road(inp, dt)

        self.animating = (
            self.view_raw != target or moving_doors or self.moving
            or (self.lead_alpha not in (0.0, 1.0))
            or was != (self.view_step, self.door_step, self.vision)
        )

    # -- road perception ------------------------------------------------------

    def _update_road(self, inp: SceneInputs, dt: float) -> None:
        road = inp.road
        fresh = False
        if road is not None:
            if road.seq:
                fresh = road.seq != self._road_seq
            else:
                fresh = road is not self._road_obj and road != self._road_obj
            self._road_seq = road.seq
            self._road_obj = road
        if fresh:
            self._road_age = 0.0
            self._have_road = True
            self._lost = False
        else:
            self._road_age += dt

        if not inp.rs_ok:
            self.vision = "off" if inp.rs_state in ("", "off", "stopping") else "wait"
            self._have_road = False
            self._lost = False
        elif not self._have_road or self._road_age > LOST_S:
            if self._have_road:
                self._lost = True
            self.vision = "lost" if self._lost else "wait"
            self._have_road = False
        elif self._road_age > STALE_S:
            self.vision = "stale"
        else:
            self.vision = "live"

        if self.vision in ("off", "wait", "lost"):
            self.lanes = [None] * 4
            self.edges = [None] * 2
            self.lead = None
            self.lead_alpha = 0.0
            self.objects = ()
            return
        if road is None:
            return

        a = 1.0 - math.exp(-dt / SMOOTH_TAU_S) if dt > 0 else 1.0
        self.xs = road.xs
        for i in range(4):
            ln = road.lanes[i]
            if ln is None or ln.prob < 0.3:
                self.lanes[i] = None
                self.lane_prob[i] = 0.0
                continue
            cur = self.lanes[i]
            if cur is None or len(cur) != len(ln.ys):
                self.lanes[i] = list(ln.ys)
            else:
                for k, y in enumerate(ln.ys):
                    cur[k] += (y - cur[k]) * a
            self.lane_prob[i] = ln.prob
        for i in range(2):
            ed = road.edges[i]
            if ed is None:
                self.edges[i] = None
                continue
            cur = self.edges[i]
            if cur is None or len(cur) != len(ed.ys):
                self.edges[i] = list(ed.ys)
            else:
                for k, y in enumerate(ed.ys):
                    cur[k] += (y - cur[k]) * a

        lead = road.lead if self.vision == "live" else None
        if lead is not None and lead.prob < 0.5:
            lead = None
        fade = dt / LEAD_FADE_S
        if lead is not None:
            cur_l = self.lead
            if cur_l is None or self.lead_alpha == 0.0:
                self.lead = lead
            else:
                self.lead = Lead(cur_l.dist_m + (lead.dist_m - cur_l.dist_m) * a,
                                 cur_l.lat_m + (lead.lat_m - cur_l.lat_m) * a,
                                 lead.v_rel, lead.prob, lead.kind)
            self.lead_alpha = min(1.0, self.lead_alpha + fade)
        else:
            self.lead_alpha = max(0.0, self.lead_alpha - fade)
            if self.lead_alpha == 0.0:
                self.lead = None
        self.objects = road.objects if self.vision == "live" else ()

    # -- derived ------------------------------------------------------------------

    def time_gap_s(self, speed_kmh: float) -> Optional[float]:
        """Time gap to the lead vehicle, None without a lead or at walking pace."""
        if self.lead is None or speed_kmh < 10.0:
            return None
        return self.lead.dist_m / (speed_kmh / 3.6)
