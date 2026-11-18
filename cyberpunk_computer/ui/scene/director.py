"""
Scene director: decides what the main-screen scene shows and animates it.

Pure Python state machine (no pygame), stepped once per UI frame with the
current ``SceneInputs``. The renderer only reads the public attributes.

Views
    ``view`` runs 0.0 (parked "hero" view of the car) .. 1.0 (chase view of
    the road). It opens when the car moves forward. It closes only when the
    car stands and is really parked: gear P, power off (neither READY nor
    ACC/IG), or a door open. A stop in D or N in traffic keeps the road
    (owner, 2026-11-16; before that the view closed after 2.5 s standing).

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
from .mapinfo import MapDirector

VIEW_STEPS = 14            # camera positions between hero and chase (cache keys)
VIEW_SECONDS = 1.3         # duration of the opening/closing move
MOVE_ON_KMH = 3.0
MOVE_OFF_KMH = 1.0
DOOR_SECONDS = 0.55        # door swing time
DOOR_STEPS = 6             # quantisation of door angles (cache keys)
STALE_S = 2.5              # road data older than this is "stale"
LOST_S = 7.0               # ... and older than this is dropped
SMOOTH_TAU_S = 0.30        # low-pass on lane/lead geometry (older roadsense without the view channel)
LEAD_FADE_S = 0.4
# roadsense 2.0 display channel ("vw", contract 7.4): the samples are already display-filtered and carry their
# frame time and velocities; the director places them on its own clock and extrapolates to the frame drawn
VIEW_EASE_S = 0.08         # residual corrections between two samples are eased over this (motion is not)
VIEW_LINE_EASE_S = 0.10    # lines: short ease between samples
VIEW_MAX_EXTRAP_S = 0.35   # a sample is carried forward by its velocity at most this long
VIEW_OFFSET_WINDOW_S = 3.0 # clock offset = the smallest (arrival - frame time) seen over this window
READOUT_HYST_M = 0.3       # a whole-metre read-out changes when the value is 0.5 + this from it
REAR_GROUP_AJAR = 0.34     # opening shown for the shared rear/back door bit


def ease(t: float) -> float:
    """Smoothstep 0..1."""
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    return t * t * (3.0 - 2.0 * t)


class SceneDirector:
    def __init__(self, tuning=None) -> None:
        self.sign_hold_s = float(getattr(tuning, "sign_hold_s", 0.0))
        self.warning_max_m = float(getattr(tuning, "warning_max_m", 1e9))
        self.signs_on: tuple = ()             # rules in effect as shown (held over feed gaps)
        self.signs_ahead: tuple = ()          # warnings as shown (within warning_max_m)
        self._signs_age = 0.0
        self.map = MapDirector(tuning)        # map elements as shown (mapinfo.py)
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
        self.path: Optional[List[float]] = None       # planned path (may be shorter than xs)
        self.xs: Tuple[float, ...] = ()
        self.lead: Optional[Lead] = None      # smoothed
        self.lead_alpha = 0.0                 # 0..1 appear/disappear
        self.objects: tuple = ()
        self.lead_shown_m: Optional[int] = None   # the lead's whole-metre read-out (with hysteresis)
        self.light_pos: Optional[Tuple[float, float]] = None   # drawn (distance, lateral LEFT+) of the ego light
        self.light_alpha = 0.0
        self.light_shown_m: Optional[int] = None
        self.line_alpha: List[float] = [1.0] * 6   # lane lines 0..3, edges 0..1 (display channel fades)
        self.road_sig: tuple = ()                 # what the road picture shows, quantised
        self.road_dirty = False                   # it changed since the renderer last drew it
        self._offs: List[Tuple[float, float]] = []   # (arrival on our clock, arrival - frame time)
        self._light_id = ""
        self._view_age = 0.0
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
                                    or not (inp.ready or inp.acc_on)):
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
        self._update_signs(inp, dt)
        self.map.update(inp, self, dt)

        sig = self.road_sig
        self._update_light(inp, dt)
        self.road_sig = self._road_signature()
        if self.road_sig != sig:
            self.road_dirty = True
        self.animating = (
            self.view_raw != target or moving_doors or self.moving
            or (self.lead_alpha not in (0.0, 1.0))
            or was != (self.view_step, self.door_step, self.vision)
            # standing in traffic: the road picture is still live (lead, lanes, light), so it is redrawn
            # whenever what it shows moved (before L19 it froze until the car moved)
            or (self.view_step == VIEW_STEPS and self.road_dirty)
        )

    # -- signs ----------------------------------------------------------------

    def _update_signs(self, inp: SceneInputs, dt: float) -> None:
        """The sign lists as shown: the feed's lists, kept for ``sign_hold_s``
        when the feed stops carrying them (its rules key expires after 3 s
        without a roadsense rules message), never while roadsense is off."""
        if not inp.rs_ok:
            self.signs_on = self.signs_ahead = ()
            return
        if inp.signs_on or inp.signs_ahead:
            self._signs_age = 0.0
            self.signs_on = inp.signs_on
            self.signs_ahead = tuple(w for w in inp.signs_ahead
                                     if w.m is None or w.m <= self.warning_max_m)
        else:
            self._signs_age += dt
            if self._signs_age > self.sign_hold_s:
                self.signs_on = self.signs_ahead = ()

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
            self.path = None
            self.lead = None
            self.lead_alpha = 0.0
            self.objects = ()
            return
        if road is None:
            return

        if road.t is not None:
            self._update_road_view(road, fresh, dt)
            return
        self.line_alpha = [1.0] * 6
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

        pth = road.path
        if pth is None:
            self.path = None
        elif self.path is None or len(self.path) != len(pth.ys):
            self.path = list(pth.ys)
        else:
            cur_p = self.path
            for k, y in enumerate(pth.ys):
                cur_p[k] += (y - cur_p[k]) * a

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
                                 lead.v_rel, lead.prob, lead.kind, lead.accel)
            self.lead_alpha = min(1.0, self.lead_alpha + fade)
        else:
            self.lead_alpha = max(0.0, self.lead_alpha - fade)
            if self.lead_alpha == 0.0:
                self.lead = None
        self.objects = road.objects if self.vision == "live" else ()
        self._readout()

    # -- display channel (roadsense view) -------------------------------------------

    def _sample_age(self, t_src: Optional[float], fresh: bool) -> float:
        """Seconds between the frame time of the newest sample and the frame being drawn, on our clock: the
        offset between the two clocks is the smallest (arrival - frame time) of the last seconds (transport
        delays only add), so jitter of the link does not move the picture."""
        if t_src is None:
            return 0.0
        if fresh:
            self._offs.append((self.clock, self.clock - t_src))
            while self._offs and self._offs[0][0] < self.clock - VIEW_OFFSET_WINDOW_S:
                self._offs.pop(0)
            if len(self._offs) > 64:
                self._offs.pop(0)
        if not self._offs:
            return 0.0
        off = min(o for _, o in self._offs)
        return max(0.0, min(VIEW_MAX_EXTRAP_S, self.clock - (t_src + off)))

    def _update_road_view(self, road: RoadScene, fresh: bool, dt: float) -> None:
        age = self._sample_age(road.t, fresh)
        a = 1.0 - math.exp(-dt / VIEW_LINE_EASE_S) if dt > 0 else 1.0
        self.xs = road.xs

        def ease_line(cur, ln):
            if ln is None:
                return None
            if cur is None or len(cur) != len(ln.ys):
                return list(ln.ys)
            for k, y in enumerate(ln.ys):
                cur[k] += (y - cur[k]) * a
            return cur
        for i in range(4):
            ln = road.lanes[i]
            self.lanes[i] = ease_line(self.lanes[i], ln)
            self.lane_prob[i] = ln.prob if ln is not None else 0.0
            self.line_alpha[i] = ln.alpha if ln is not None else 0.0
        for i in range(2):
            self.edges[i] = ease_line(self.edges[i], road.edges[i])
            self.line_alpha[4 + i] = road.edges[i].alpha if road.edges[i] is not None else 0.0
        self.path = ease_line(self.path, road.path)

        lead = road.lead if self.vision == "live" else None
        if lead is not None:
            v = lead.v_rel or 0.0
            target = lead.dist_m + v * age
            cur = self.lead
            if cur is None or (lead.oid and cur.oid != lead.oid):
                d, lat = target, lead.lat_m
            else:
                k = 1.0 - math.exp(-dt / VIEW_EASE_S) if dt > 0 else 1.0
                d = cur.dist_m + v * dt                      # motion: exact
                d += (target - d) * k                        # corrections: eased
                lat = cur.lat_m + (lead.lat_m - cur.lat_m) * k
            self.lead = lead._replace(dist_m=d, lat_m=lat)
            self.lead_alpha = lead.alpha
        else:
            self.lead_alpha = max(0.0, self.lead_alpha - dt / LEAD_FADE_S)
            if self.lead_alpha == 0.0:
                self.lead = None
        self.objects = road.objects if self.vision == "live" else ()
        self._view_age = age
        self._readout()

    def _update_light(self, inp: SceneInputs, dt: float) -> None:
        """The drawn position of the ego light. From the display channel: carried by its velocity between two
        samples, corrections eased; otherwise (older roadsense) the light's own values."""
        lt = inp.light
        if lt is None or lt.dist_m is None:
            self.light_pos, self.light_alpha, self._light_id, self.light_shown_m = None, 0.0, "", None
            return
        lat = lt.lat_m if lt.lat_m is not None else -4.5
        if not lt.car_frame:
            self.light_pos, self.light_alpha = (lt.dist_m, lat), 1.0
        else:
            age = self._view_age
            vx, vy = lt.vx or 0.0, lt.vy or 0.0
            tx, ty = lt.dist_m + vx * age, lat + vy * age
            if self.light_pos is None or lt.oid != self._light_id:
                self.light_pos = (tx, ty)
            else:
                k = 1.0 - math.exp(-dt / VIEW_EASE_S) if dt > 0 else 1.0
                x, y = self.light_pos[0] + vx * dt, self.light_pos[1] + vy * dt
                self.light_pos = (x + (tx - x) * k, y + (ty - y) * k)
            self.light_alpha = lt.alpha
        self._light_id = lt.oid
        self.light_shown_m = self._hyst(self.light_shown_m, self.light_pos[0])

    @staticmethod
    def _hyst(shown: Optional[int], value: float) -> int:
        if shown is None or abs(value - shown) > 0.5 + READOUT_HYST_M:
            return int(value + 0.5)
        return shown

    def _readout(self) -> None:
        self.lead_shown_m = None if self.lead is None else self._hyst(self.lead_shown_m, self.lead.dist_m)

    def _road_signature(self) -> tuple:
        """What the road picture shows, quantised to about a pixel: a change redraws the scene."""
        def q(v, step):
            return None if v is None else int(round(v / step))
        lines = tuple(None if ln is None else (q(ln[2], 0.05), q(ln[min(6, len(ln) - 1)], 0.2))
                      for ln in self.lanes + self.edges)
        ld = None if self.lead is None else (q(self.lead.dist_m, 0.05), q(self.lead.lat_m, 0.05),
                                             q(self.lead_alpha, 0.1), self.lead_shown_m)
        lt = None if self.light_pos is None else (q(self.light_pos[0], 0.25), q(self.light_pos[1], 0.1),
                                                 q(self.light_alpha, 0.1))
        return (lines, ld, lt, tuple(q(a, 0.1) for a in self.line_alpha))

    # -- derived ------------------------------------------------------------------

    def time_gap_s(self, speed_kmh: float) -> Optional[float]:
        """Time gap to the lead vehicle, None without a lead or at walking pace."""
        if self.lead is None or speed_kmh < 10.0:
            return None
        return self.lead.dist_m / (speed_kmh / 3.6)
