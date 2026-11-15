"""
Map layer of the main-screen scene: ambient information from roadsense's map.

Pure Python (no pygame). Two parts:

``parse_map(raw_scene)``
    the scene's ``map`` block (contract: docs/MFD_MAIN_SCREEN.md 7.2) as a
    ``MapInfo``, or None when the map is not usable. Usable means what it
    means for the speed limit (``usable_map``): roadsense uses the map
    (``ok``: fresh, on a road, match confidence >= 0.5) and the position is
    not synthetic (``syn``, ``in.gnss == "synthetic"``). Without a usable map
    the scene is exactly the vision-only picture.

``MapDirector``
    behaviour over time of the map elements (docs/MFD_MAIN_SCREEN.md 6.5),
    stepped by ``SceneDirector.update``; ``maplayer.py`` draws what it holds:

        mark          the map is in use: "good" | "dr" (dead reckoning) | ""
        next_limit    the next speed-limit change on the most likely path
        tunnel_exit_m distance to the end of the tunnel the car is in
        far           the road beyond camera range (100 m .. 600 m)
        lanes         lanes of the own direction / oncoming, the own lane
        label         the road's number (and name) for a while after it changed

Every element hides rather than shows doubtful data: each has a confidence
and a position-accuracy gate, distances are advanced by the distance driven
between two map messages (2 per second), and everything is dropped at once
when the map stops being usable or the road picture is not live.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, List, NamedTuple, Optional, Tuple

FAR_FROM_M = 100.0         # the far road starts where the camera's lanes end
FAR_MIN_LEN_M = 40.0       # ... and is drawn only when at least this long
NEXT_LIMIT_STABLE_S = 1.0  # the same change must be reported this long before it is shown
NEXT_LIMIT_NEAR_M = 20.0   # not shown when first seen closer than this (it is about to apply)
FAR_AGREE_S = 1.0          # camera and map must agree this long before the far road appears
FAR_DISAGREE_S = 0.4       # ... and disagree this long before it goes
FAR_FADE_S = 0.5
LABEL_STABLE_S = 2.0       # a new road number / name must hold this long before it is shown
TUNNEL_HACC_SHARE = 0.1    # tunnel exit distance: shown while hacc <= this share of it
CONF_HYST = 0.15           # a confidence gate opens at its threshold and closes this far below
MAX_ADVANCE_S = 3.5        # distances are advanced by odometry for at most this long


class NextLimit(NamedTuple):
    kmh: int
    m: float                         # distance along the most likely path
    conf: float                      # path confidence up to it x the limit's own confidence
    src: str = ""                    # maxspeed (tagged) | implicit (statutory)


class MapInfo(NamedTuple):
    """The usable map block, normalised. Lateral values LEFT positive."""
    state: str = "good"              # good | dead_reckoning
    conf: float = 0.0                # match confidence
    hacc: Optional[float] = None     # position accuracy (m, RMS radial)
    road_ref: str = ""
    road_name: str = ""
    road_cls: str = ""
    lanes: Optional[Tuple[int, int, int]] = None      # (own direction, oncoming, ego index from the left)
    lanes_conf: float = 0.0
    onc_left: Optional[bool] = None  # the lane to the left carries oncoming traffic
    tunnel_in: bool = False          # in a tunnel by the map
    tunnel_exit_m: Optional[float] = None
    next_limit: Optional[NextLimit] = None
    junctions: Tuple[Tuple[float, float, str], ...] = ()   # (m, confidence, code j|r|x|d|n), nearest first
    shape_ds: float = 0.0            # metres of path between two shape points
    shape: Tuple[Tuple[float, float], ...] = ()            # (x forward, y left) from the matched position


class Far(NamedTuple):
    pts: Tuple[Tuple[float, float], ...]   # (x forward, y left), from the car to the cut
    end: str = ""                          # "" | roundabout | dead_end


class LaneBoxes(NamedTuple):
    fwd: int
    back: int
    ego: int


class RoadLabel(NamedTuple):
    ref: str
    cls: str
    name: str = ""


def _f(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    return float(v)


def usable_map(raw_scene: Any) -> Optional[Mapping]:
    """The scene's ``map`` block when the map layer may be used, else None."""
    if not isinstance(raw_scene, Mapping):
        return None
    mp = raw_scene.get("map")
    if not isinstance(mp, Mapping) or mp.get("ok") is not True or mp.get("syn"):
        return None
    mi = mp.get("in")
    if isinstance(mi, Mapping) and mi.get("gnss") == "synthetic":
        return None
    return mp


def _parse(mp: Mapping) -> MapInfo:
    def sub(key: str) -> Mapping:
        v = mp.get(key)
        return v if isinstance(v, Mapping) else {}

    road, ln, tn, ah, pos = sub("road"), sub("lanes"), sub("tunnel"), sub("ahead"), sub("pos")
    lanes = None
    fwd, back, ego = _f(ln.get("fwd")), _f(ln.get("back")), _f(ln.get("ego"))
    if fwd is not None and back is not None and ego is not None and 0 <= ego < fwd:
        lanes = (int(fwd), int(back), int(ego))
    onc = ln.get("onc_left")
    exit_m = _f(tn.get("exit_m"))
    src = tn.get("src")
    tunnel_in = bool(tn.get("in")) and isinstance(src, str) and "map" in src
    nl = None
    lim = ah.get("limit")
    if isinstance(lim, Mapping):
        kmh, m = _f(lim.get("kmh")), _f(lim.get("m"))
        if kmh is not None and kmh > 0 and m is not None and m >= 0:
            c = _f(lim.get("c"))
            nl = NextLimit(int(kmh), m, 0.0 if c is None else c, str(lim.get("src") or ""))
    jn: List[Tuple[float, float, str]] = []
    for e in ah.get("jn") or ():
        if isinstance(e, (list, tuple)) and len(e) >= 3:
            m, c = _f(e[0]), _f(e[1])
            if m is not None and c is not None and isinstance(e[2], str):
                jn.append((m, c, e[2]))
    shape: Tuple[Tuple[float, float], ...] = ()
    sh = sub("shape")
    ds = _f(sh.get("ds")) or 0.0
    xs, ys = sh.get("x"), sh.get("y")
    if ds > 0 and isinstance(xs, (list, tuple)) and isinstance(ys, (list, tuple)):
        pts = []
        for x, y in zip(xs, ys):
            fx, fy = _f(x), _f(y)
            if fx is None or fy is None:
                pts = []
                break
            pts.append((fx, -fy))             # the wire is y right
        shape = tuple(pts)
    return MapInfo(
        state=str(mp.get("state") or ""), conf=_f(mp.get("conf")) or 0.0, hacc=_f(pos.get("hacc")),
        road_ref=str(road.get("ref") or ""), road_name=str(road.get("name") or ""),
        road_cls=str(road.get("cls") or ""),
        lanes=lanes, lanes_conf=_f(ln.get("c")) or 0.0, onc_left=onc if isinstance(onc, bool) else None,
        tunnel_in=tunnel_in, tunnel_exit_m=exit_m if tunnel_in else None,
        next_limit=nl, junctions=tuple(jn), shape_ds=ds, shape=shape)


_last: List[Any] = [None, None]


def parse_map(raw_scene: Any) -> Optional[MapInfo]:
    """The usable map block of a scene mapping as ``MapInfo``, else None.

    The block changes twice a second but rides on every scene event (up to
    10 a second): an unchanged block returns the SAME object, so the cost is
    one dict comparison and "a new map message" is an identity test."""
    mp = usable_map(raw_scene)
    if mp is None:
        return None
    if mp is _last[0] or mp == _last[0]:
        return _last[1]
    info = _parse(mp)
    _last[0], _last[1] = mp, info
    return info


def far_path(mi: MapInfo, min_conf: float, max_m: float) -> Optional[Far]:
    """The road ahead as far as the most likely path can be trusted.

    Cut at the first junction (with a side road that is not a service road)
    whose path confidence is below ``min_conf``, at a roundabout (what comes
    after it is a guess), at a dead end, and at ``max_m``. None when what is
    left does not reach ``FAR_MIN_LEN_M`` beyond camera range."""
    shape, ds = mi.shape, mi.shape_ds
    if len(shape) < 3 or ds <= 0:
        return None
    cut, end = max_m, ""
    for m, c, code in mi.junctions:
        if m >= cut:
            break
        if code in ("d", "n"):
            cut, end = m, "dead_end"
            break
        if code == "r":
            cut, end = m, "roundabout"
            break
        if c < min_conf:
            cut = m
            break
    total = (len(shape) - 1) * ds
    if cut > total:
        cut, end = total, ""
    if cut < FAR_FROM_M + FAR_MIN_LEN_M:
        return None
    pts: List[Tuple[float, float]] = []
    for i, (x, y) in enumerate(shape):
        s = i * ds
        if s <= cut:
            pts.append((x, y))
            continue
        px, py = pts[-1]
        t = (cut - (s - ds)) / ds
        if t > 0.05:
            pts.append((px + (x - px) * t, py + (y - py) * t))
        break
    return Far(tuple(pts), end)


def _lat_at(pts: Tuple[Tuple[float, float], ...], x: float) -> Optional[float]:
    """Lateral of the shape at forward distance x (None beyond its reach)."""
    for i in range(1, len(pts)):
        x1, y1 = pts[i]
        if x <= x1:
            x0, y0 = pts[i - 1]
            if x1 - x0 <= 0.5:
                return None
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return None


def camera_bend(d: Any) -> Optional[Tuple[float, float]]:
    """(distance, lateral change of the camera's road centre from the car to
    there), from the ego lane lines, else the planned path; None when the
    camera says nothing about the road 50 m ahead."""
    xs = d.xs
    left, right, path = d.lanes[1], d.lanes[2], d.path
    if left and right:
        n = min(len(left), len(right), len(xs))
        if n >= 2 and xs[n - 1] >= 50.0:
            return xs[n - 1], (left[n - 1] + right[n - 1] - left[0] - right[0]) * 0.5
    for ys in (left, right, path):
        if ys:
            n = min(len(ys), len(xs))
            if n >= 2 and xs[n - 1] >= 50.0:
                return xs[n - 1], ys[n - 1] - ys[0]
    return None


class _Distance:
    """A distance ahead that shrinks with the distance driven between two map
    messages and follows each new message."""

    def __init__(self) -> None:
        self.m: Optional[float] = None

    def step(self, target: Optional[float], fresh: bool, moved: float, can_advance: bool) -> Optional[float]:
        if target is None:
            self.m = None
        elif fresh or self.m is None:
            # a small difference is the estimate being a little ahead: ease, do not jump
            self.m = target if (self.m is None or abs(target - self.m) > 15.0) \
                else self.m + (target - self.m) * 0.5
        elif can_advance:
            self.m = max(0.0, self.m - moved)
        return self.m


class MapDirector:
    """What the map elements show, frame by frame (see the module docstring)."""

    def __init__(self, tuning: Any = None) -> None:
        g = lambda name, default: getattr(tuning, name, default)      # noqa: E731
        self.on_mark = bool(g("map_mark", True))
        self.next_mode = str(g("map_next_limit", "ahead"))
        self.on_far = bool(g("map_far_road", True))
        self.on_tunnel = bool(g("map_tunnel", True))
        self.on_lanes = bool(g("map_lanes", True))
        self.label_mode = str(g("map_road_label", "off"))
        self.label_s = float(g("map_road_label_s", 8.0))
        self.ahead_s = float(g("map_ahead_s", 12.0))
        self.ahead_min_m = float(g("map_ahead_min_m", 150))
        self.ahead_max_m = float(g("map_ahead_max_m", 400))
        self.min_match = float(g("map_min_match", 0.7))
        self.hold_s = float(g("map_hold_s", 1.5))
        self.min_conf = float(g("map_min_conf", 0.5))
        self.path_min_conf = float(g("map_path_min_conf", 0.6))
        self.max_hacc_m = float(g("map_max_hacc_m", 20.0))
        self.far_disagree_m = float(g("map_far_disagree_m", 8.0))
        self.far_max_m = float(g("map_far_max_m", 600))
        # ── shown (read by maplayer.py) ──
        self.mark = ""
        self.next_limit: Optional[NextLimit] = None      # m = the advanced distance
        self.tunnel_exit_m: Optional[float] = None
        self.far: Optional[Far] = None
        self.far_alpha = 0.0
        self.adv = 0.0                                   # metres driven since the map message of `far`
        self.lanes: Optional[LaneBoxes] = None
        self.onc_left = False
        self.label: Optional[RoadLabel] = None
        self.sig: tuple = ()
        # ── internal ──
        self._mi: Optional[MapInfo] = None
        self._age = 99.0
        self._lost_for = 0.0
        self._dead: Optional[MapInfo] = None
        self._seen: Optional[MapInfo] = None
        self._conf_s: Optional[float] = None
        self._trusted = False
        self._lanes_on = False
        self._nl = _Distance()
        self._nl_kmh: Optional[int] = None
        self._nl_for = 0.0
        self._nl_on = False
        self._tn = _Distance()
        self._far_src: Optional[Far] = None
        self._agree_for = 0.0
        self._disagree_for = 0.0
        self._far_ok = False
        self._lab_cand: Optional[RoadLabel] = None
        self._lab_for = 0.0
        self._lab_shown: Optional[RoadLabel] = None
        self._lab_left = 0.0

    def _clear(self) -> None:
        self.mark = ""
        self.next_limit = None
        self.tunnel_exit_m = None
        self.far = None
        self.far_alpha = 0.0
        self.lanes = None
        self.onc_left = False
        self.label = None
        self._mi = None
        self._nl.m = self._tn.m = None
        self._nl_kmh = None
        self._nl_on = False
        self._far_src = None
        self._far_ok = False
        self._agree_for = self._disagree_for = 0.0
        self._lab_cand = None
        self._lab_left = 0.0
        self.sig = ()

    def update(self, inp: Any, d: Any, dt: float) -> None:
        mi: Optional[MapInfo] = getattr(inp, "map", None)
        if mi is not None and mi is self._dead:
            mi = None                  # a message that stopped changing while the car drives on
        if mi is not None:
            # match confidence dips at every side road (the winner's share among the
            # candidates): judge its 3 s average, with hysteresis
            if mi is not self._seen:
                self._seen = mi
                self._conf_s = mi.conf if self._conf_s is None else self._conf_s + (mi.conf - self._conf_s) * 0.25
            if self._conf_s >= self.min_match:
                self._trusted = True
            elif self._conf_s < self.min_match - CONF_HYST:
                self._trusted = False
            if not self._trusted:
                mi = None              # a doubtful match: the map says nothing
        if mi is None and d.vision == "live" and self._mi is not None:
            # the map drops out for a message or two at junctions (match confidence):
            # keep the last good one for a moment, its distances advanced by odometry
            self._lost_for += dt
            if self._lost_for <= self.hold_s:
                mi = self._mi
        else:
            self._lost_for = 0.0
        # map elements belong to a live road picture: a stale feed carries a stale map
        if mi is None or d.vision != "live":
            if getattr(inp, "map", None) is None and self._lost_for > self.hold_s:
                self._conf_s = None            # the map was gone: trust is earned again
                self._trusted = False
            if self._mi is not None or self.sig:
                self._clear()
            return
        fresh = mi is not self._mi
        self._mi = mi
        v = inp.real_speed_kmh / 3.6
        moved = v * dt if d.moving else 0.0
        if fresh:
            self._age = 0.0
            self.adv = 0.0
        elif moved > 0.0:
            # standing still the map repeats itself: only driving ages a message
            self._age += dt
            self.adv += moved
        if self._age > MAX_ADVANCE_S:
            # driven this long on one message: the map feed stands still (mapd stalled,
            # the backend repeating its last block): gone, until a new message comes
            self._dead = getattr(inp, "map", None)
            self._clear()
            return
        can_adv = True
        accurate = mi.hacc is None or mi.hacc <= self.max_hacc_m

        self.mark = "" if not self.on_mark else ("dr" if mi.state == "dead_reckoning" else "good")

        # ── next speed limit ──
        cand = mi.next_limit if (self.next_mode != "off" and accurate) else None
        if cand is not None and (cand.conf < self.min_conf or cand.kmh == inp.limit_kmh):
            cand = None
        if cand is None or cand.kmh != self._nl_kmh:
            self._nl_kmh = cand.kmh if cand is not None else None
            self._nl_for = 0.0
            self._nl_on = False
            self._nl.m = None
        m = self._nl.step(cand.m if cand is not None else None, fresh, moved, can_adv)
        if cand is not None and m is not None:
            self._nl_for += dt
            window = min(self.ahead_max_m, max(self.ahead_min_m, v * self.ahead_s))
            if not self._nl_on:
                self._nl_on = (self._nl_for >= NEXT_LIMIT_STABLE_S and NEXT_LIMIT_NEAR_M <= m <= window)
            elif m > window * 1.25:
                self._nl_on = False
        self.next_limit = cand._replace(m=m) if (self._nl_on and cand is not None and m is not None) else None

        # ── tunnel exit ──
        # a long tunnel is driven on dead reckoning: the exit distance stays while the
        # position error is small against it (TUNNEL_HACC_SHARE), then it goes
        t_m = mi.tunnel_exit_m if (self.on_tunnel and mi.tunnel_in) else None
        if t_m is not None and not accurate and (mi.hacc or 0.0) > t_m * TUNNEL_HACC_SHARE:
            t_m = None
        self.tunnel_exit_m = self._tn.step(t_m, fresh, moved, can_adv)

        # ── far road ──
        if self.on_far and accurate and can_adv:
            if fresh:
                self._far_src = far_path(mi, self.path_min_conf, self.far_max_m)
            far = self._far_src
        else:
            far = self._far_src = None
        agree = False
        if far is not None:
            cam = camera_bend(d)
            if cam is not None:
                x, bend = cam
                y1 = _lat_at(far.pts, x + self.adv)
                y0 = _lat_at(far.pts, self.adv)
                if y1 is not None and y0 is not None:
                    agree = abs(bend - (y1 - y0)) <= self.far_disagree_m * x / 100.0 + 1.0
        if agree:
            self._agree_for += dt
            self._disagree_for = 0.0
            if self._agree_for >= FAR_AGREE_S:
                self._far_ok = True
        else:
            self._disagree_for += dt
            self._agree_for = 0.0
            if self._disagree_for >= FAR_DISAGREE_S or far is None:
                self._far_ok = False
        step = dt / FAR_FADE_S
        if self._far_ok and far is not None:
            self.far = far
            self.far_alpha = min(1.0, self.far_alpha + step)
        else:
            self.far_alpha = max(0.0, self.far_alpha - step) if far is not None else 0.0
            if self.far_alpha == 0.0:
                self.far = None
            elif far is not None:
                self.far = far

        # ── lanes ──
        if self.on_lanes and mi.lanes is not None:
            if mi.lanes_conf >= self.min_conf:
                self._lanes_on = True
            elif mi.lanes_conf < self.min_conf - CONF_HYST:
                self._lanes_on = False
        else:
            self._lanes_on = False
        if self._lanes_on and mi.lanes is not None:
            self.lanes = LaneBoxes(*mi.lanes)
            self.onc_left = mi.onc_left is True
        else:
            self.lanes = None
            self.onc_left = False

        # ── road label: for a while after the road changed ──
        self.label = None
        if self.label_mode != "off":
            ref = mi.road_ref.split(";")[0].strip()
            name = mi.road_name if self.label_mode == "name" else ""
            cand_l = RoadLabel(ref, mi.road_cls if ref else "", name) if (ref or name) else None
            if cand_l != self._lab_cand:
                self._lab_cand = cand_l
                self._lab_for = 0.0
            else:
                self._lab_for += dt
            if (cand_l is not None and cand_l != self._lab_shown and self._lab_for >= LABEL_STABLE_S):
                self._lab_shown = cand_l
                self._lab_left = self.label_s
            if self._lab_left > 0.0:
                self._lab_left -= dt
                if self._lab_shown == cand_l:
                    self.label = self._lab_shown
                else:
                    self._lab_left = 0.0
            if cand_l is None:
                self._lab_shown = None

        nl = self.next_limit
        self.sig = (self.mark, None if nl is None else (nl.kmh, int(nl.m / 10.0), nl.src),
                    None if self.tunnel_exit_m is None else int(self.tunnel_exit_m / 10.0),
                    int(self.far_alpha * 4), self.far is not None and int(self.adv),
                    self.lanes, self.onc_left, self.label)
