"""
Radar view: an air-traffic-control style plan view of the road users chris-integrator tracks.

The alternative picture of the scene rectangle while the road view is open (``ScenePage``: a small button
switches between the two). Seen from above, the car at the bottom centre heading up:

    ┌──────────────────────────────┐ y 0
    │ 50 · · · · · · · · ·    (50) │  range rings (dotted half-ellipses), their labels; the limit sign
    │      ▢    34m       ▢        │  every road user as a fixed-size symbol by kind, a velocity leader
    │ 25 · ▢ · ·0■· ▣ · · ▢ · ·    │  (ground speed, where it is in LEADER_S) and a range bar of +-1 sigma
    │      ▢    ┆  │ ┆    ▢        │  where the distance is poor; ATC data tags (distance m, speed km/h,
    │      ▢  ·  ┆ ▣ ┆ ·  ▢        │  a one-glyph state, the time to reach it for a threat) for at most
    │ 10 ·      ┆   ┆      ·       │  TAGS_MAX objects, placed so that they never overlap
    │12 TRK         ▲        [ROAD]│  status bottom-left (count / NO RADAR DATA), the own car, the button
    └──────────────────────────────┘ y 216

Scale: linear (a plan, not a perspective). The range scale is one of ``RANGES`` chosen by what matters (lead,
threats, obstacles, moving traffic in the path, about 3 s of driving); a larger range is taken at once, a
smaller one only after it has fitted for ``RANGE_HOLD_S`` (the scale does not breathe). Across, the scale is
up to twice the range scale (at most 8 px/m), so at 50 or 80 m the lanes and the parked rows do not collapse
into one column: the range rings are then half-ellipses (a curve of equal distance), the bearing ticks sit at
the true bearings through the same mapping. At 25 m both scales are the same.

Symbols (1 px lines and flat fills, 1-bit): vehicle = a box (truck / bus larger), two-wheeler = a diamond,
person = a ring; the lead filled with target corners. Colours: traffic cyan, parked dimmed, persons and
two-wheelers amber (as in the road view), ``obstacle`` red with a frame, ``threat`` red blinking at 1 Hz with
its time to reach. Objects placed by the camera alone carry a range bar of the camera's documented error
(about 15 % of the distance at 20 m, 30 % at 40 m) when the scene gives no sigma.

Cost (docs/MFD_MAIN_SCREEN.md 10.5): the rings, ticks, labels and the own car are one cached surface per range;
a redraw blits it and draws the path, at most 11 symbols (10 road users, the lead) with their leaders and
bars, and up to four tags from cached text. It redraws only when a symbol moved by about a pixel (or a threat
blinks), at most at ``fps_scene`` like the road view.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import pygame

from .director import SceneDirector
from .inputs import SceneInputs

RGB = Tuple[int, int, int]

RANGES = (25, 50, 80)                       # m: the scale steps (the integrator's objects reach 70 m)
RINGS = {25: (10, 25), 50: (10, 25, 50), 80: (25, 50, 80)}
RANGE_HOLD_S = 4.0                          # a smaller range must fit this long before it is taken
RANGE_MARGIN_M = 4.0                        # an object that sets the range stays this far inside it
RANGE_AHEAD_S = 3.0                         # the range covers at least this much driving ahead
RADAR_STALE_S = 1.5                         # no radar frame for this long: NO RADAR DATA
LEADER_S = 1.0                              # the velocity leader: where it is after this long
LEADER_MIN_MPS = 1.0                        # no leader below this ground speed
LEADER_MAX_PX = 32
LATERAL_GAIN = 2.0                          # the lateral scale is up to this many times the range scale ...
LATERAL_MAX_PPM = 8.0                       # ... and at most this (px/m): +-14 m across at every range
STAND_MPS = 0.6                             # the lead's ground speed under this reads as standing (no radar verdict)
SD_MIN_PX = 3                               # a range bar shorter than this (each side) is not drawn
TAGS_MAX = 4
IN_PATH_M = 1.6                             # half width of the corridor around the path ("in path")
VRU_TAG_M = 30.0                            # the nearest person / two-wheeler within this gets a tag
BLINK_HZ = 1.0                              # threat blink: calm (on 0.5 s, off 0.5 s)
PARKED_DIM = 0.6                            # as the road view (render.PARKED_DIM)
EGO_BOTTOM = 14                             # the car's nose (range 0) this many px over the bottom edge
TOP_PX = 10                                 # the outer ring touches this row

QMARK = ((1, 0), (2, 0), (0, 1), (3, 1), (3, 2), (2, 3), (2, 4), (2, 6))
VEHICLES = ("car", "van", "truck", "bus")
TWO_WHEELERS = ("bike", "motorbike")


def _k(c: RGB, k: float) -> RGB:
    return (int(c[0] * k), int(c[1] * k), int(c[2] * k))


def camera_sigma(x: float) -> float:
    """1 sigma of a distance placed by the camera alone (chris-integrator docs/TOPICS.md: about 15 % of the
    distance at 20 m, 30 % at 40 m; held at 30 % beyond)."""
    x = max(0.0, x)
    return x * min(0.30, 0.0075 * x)


class RadarView:
    """Owns the radar view's surface; ``update()`` steps its range, ``render()`` returns the surface, redrawn
    when needed. ``host`` is the scene's ``SceneRenderer`` (palette, text cache, chips, the limit sign)."""

    def __init__(self, host, size: Tuple[int, int] = (238, 216)):
        self.host = host
        self.w, self.h = size
        self.cx = self.w // 2
        self.ey = self.h - EGO_BOTTOM
        pal = host.pal
        self.col = {
            "ring": (0, 70, 74),
            "tick": (0, 96, 96),
            "ring_label": _k(pal["label"], 0.9),
            "path": (0, 108, 98),
            "lane": (30, 78, 86),
            "traffic": pal["lane_mid"],
            "lead": pal["lane_ego"],
            "vru": pal["warn"],
            "red": pal["red"],
            "text": pal["text"],
            "value": pal["value"],
            "label": pal["label"],
            "dim": pal["dim"],
            "tag_line": (0, 96, 96),
        }
        self.surface = pygame.Surface(size)
        self.surface.fill((0, 0, 0))
        self.range_m = RANGES[1]
        self._fit_s = 0.0                    # how long a smaller range has fitted
        self._static: Dict[int, pygame.Surface] = {}
        self._slot: Dict[str, int] = {}      # tag placement memory: object id -> candidate index
        self._shown_d: Dict[str, int] = {}   # read-outs with a hysteresis (no flicker of the last digit)
        self._shown_v: Dict[str, int] = {}
        self._sig: Optional[tuple] = None
        self._last_draw = -1.0
        self.min_redraw_s = host.min_redraw_s
        self.redraws = 0
        self.keepout: List[pygame.Rect] = []  # set by the page: the button (scene coordinates)
        self.tags: List[Tuple[str, pygame.Rect]] = []   # last drawn tags (id, rect): tests / debugging
        self.host_limit_rect: Optional[pygame.Rect] = None

    # ── geometry ─────────────────────────────────────────────────────────────

    @property
    def ppm(self) -> float:
        """Pixels per metre along the car's axis (the range)."""
        return (self.ey - TOP_PX) / float(self.range_m)

    @property
    def lpm(self) -> float:
        """Pixels per metre across: up to LATERAL_GAIN x the range scale (at most LATERAL_MAX_PPM), so that the
        lanes and the parked rows do not collapse into one column at 50 or 80 m."""
        p = self.ppm
        return max(p, min(LATERAL_GAIN * p, LATERAL_MAX_PPM))

    def to_px(self, x: float, y_left: float) -> Tuple[int, int]:
        """Car frame (m ahead, m LEFT positive) -> scene pixel."""
        return int(round(self.cx - y_left * self.lpm)), int(round(self.ey - x * self.ppm))

    # ── state ────────────────────────────────────────────────────────────────

    @staticmethod
    def radar_live(d: SceneDirector) -> bool:
        return d.vision == "live" and d.radar_age <= RADAR_STALE_S

    def _need_m(self, d: SceneDirector, inp: SceneInputs) -> float:
        """The distance the picture must show: lead, threats, obstacles, moving traffic near the path, and
        about RANGE_AHEAD_S of driving."""
        need = max(15.0, inp.real_speed_kmh / 3.6 * RANGE_AHEAD_S)
        if d.vision != "live":
            return need
        if d.lead is not None:
            need = max(need, d.lead.dist_m + RANGE_MARGIN_M)
        for o in d.objects:
            if o.state in ("threat", "obstacle"):
                need = max(need, o.x + RANGE_MARGIN_M)
            elif o.state != "parked" and o.motion != "stationary":
                # moving traffic in the path (not the oncoming lane far off), persons near it
                off = abs(o.y - self._path_y(d, o.x))
                if off < IN_PATH_M or (off < 4.0 and o.kind == "person"):
                    need = max(need, o.x + RANGE_MARGIN_M)
        return need

    def update(self, d: SceneDirector, inp: SceneInputs, dt: float) -> None:
        need = self._need_m(d, inp)
        want = next((r for r in RANGES if r >= need), RANGES[-1])
        if want > self.range_m:
            self.range_m, self._fit_s = want, 0.0
        elif want < self.range_m:
            self._fit_s += dt
            if self._fit_s >= RANGE_HOLD_S:
                # one step down at a time
                self.range_m = RANGES[max(0, RANGES.index(self.range_m) - 1)]
                self._fit_s = 0.0
        else:
            self._fit_s = 0.0

    # ── frame ────────────────────────────────────────────────────────────────

    def _signature(self, d: SceneDirector, inp: SceneInputs) -> tuple:
        step = 1.0 / max(self.ppm, self.lpm)       # about a pixel
        live = d.vision == "live"

        def q(v):
            return int(round(v / step))
        objs = tuple((o.oid or o.kind, o.kind, o.state, o.motion, q(o.x), q(o.y), int(o.alpha * 10),
                      int(o.vx * 2), int(o.vy * 2)) for o in d.objects) if live else ()
        ld = None
        if live and d.lead is not None:
            ld = (q(d.lead.dist_m), q(d.lead.lat_m), int(d.lead_alpha * 10), int((d.lead.v_rel or 0.0) * 2),
                  d.lead.kind)
        path = tuple(q(v) for v in d.path[:8]) if (live and d.path) else ()
        threat = live and (any(o.state == "threat" for o in d.objects) or (d.alert is not None
                                                                         and d.alert[0] == "threat"))
        blink = int(d.clock * 2 * BLINK_HZ) % 2 if threat else 0
        return (self.range_m, d.vision, self.radar_live(d), objs, ld, path, blink, int(inp.real_speed_kmh / 2),
                inp.limit_kmh, inp.limit_source, int(inp.speed_kmh + 0.5), d.alert, len(self.keepout))

    def due(self, d: SceneDirector, inp: SceneInputs, now: float) -> bool:
        return (self._signature(d, inp) != self._sig
                or (d.animating and now - self._last_draw >= self.min_redraw_s))

    def render(self, d: SceneDirector, inp: SceneInputs, now: float) -> pygame.Surface:
        sig = self._signature(d, inp)
        if sig != self._sig or (d.animating and now - self._last_draw >= self.min_redraw_s):
            self._sig = sig
            self._last_draw = now
            d.road_dirty = False          # what the road data shows is on the surface (director.animating)
            self._draw(d, inp)
            self.redraws += 1
        return self.surface

    def invalidate(self) -> None:
        self._sig = None

    # ── static layer ─────────────────────────────────────────────────────────

    def _static_layer(self) -> pygame.Surface:
        r = self.range_m
        surf = self._static.get(r)
        if surf is not None:
            return surf
        surf = pygame.Surface((self.w, self.h))
        surf.fill((0, 0, 0))
        cx, ey, ppm, lpm = self.cx, self.ey, self.ppm, self.lpm
        col = self.col
        for m in RINGS[r]:
            rx, ry = m * lpm, m * ppm
            n = max(48, int((rx + ry) * 1.2))
            # dotted: every other pixel step of the ring (a faint ring on the analog link)
            last = None
            k = 0
            for i in range(n + 1):
                a = math.pi * i / n
                px, py = int(round(cx + rx * math.cos(a))), int(round(ey - ry * math.sin(a)))
                if (px, py) == last:
                    continue
                last = (px, py)
                k += 1
                if k % 2 and 0 <= px < self.w and 0 <= py < self.h:
                    surf.set_at((px, py), col["ring"])
            # the label where the ring leaves the picture at the left edge, else on top beside the axis
            lab = self.host.text("%d" % m, 8, col["ring_label"], mono=False)
            dx = cx - 8
            if rx > dx and ry * math.sqrt(1.0 - (dx / rx) ** 2) < ey - 24:
                ly = int(ey - ry * math.sqrt(1.0 - (dx / rx) ** 2))
                surf.blit(lab, (3, ly - 8))
            else:
                # on the ring's upper left, clear of the lanes around the axis
                dx = 0.62 * rx
                ly = int(ey - ry * math.sqrt(1.0 - 0.62 ** 2))
                if ly - 8 >= 0:
                    surf.blit(lab, (int(cx - dx) - lab.get_width() - 1, ly - 8))
        # bearing ticks every 15 degrees on the outer ring, inward (true bearings through the scale)
        R = RINGS[r][-1]
        for deg in range(-75, 76, 15):
            a = math.radians(deg)
            p0 = self.to_px(R * math.cos(a), R * math.sin(a))
            p1 = self.to_px(0.95 * R * math.cos(a), 0.95 * R * math.sin(a))
            pygame.draw.line(surf, col["tick"], p0, p1)
        # the car's axis: a short dim line from the nose up
        pygame.draw.line(surf, col["ring"], (cx, ey - 6), (cx, ey - 10))
        # the own car: a filled chevron, nose at range 0
        pygame.draw.polygon(surf, self.host.pal["lane_ego"],
                            [(cx, ey - 1), (cx + 4, ey + 9), (cx, ey + 6), (cx - 4, ey + 9)])
        if len(self._static) > 4:
            self._static.clear()
        self._static[r] = surf
        return surf

    # ── drawing ──────────────────────────────────────────────────────────────

    @staticmethod
    def _interp(xs: Sequence[float], ys: Optional[Sequence[float]], x: float) -> float:
        if not ys:
            return 0.0
        n = min(len(xs), len(ys))
        if n == 0:
            return 0.0
        for i in range(1, n):
            if x <= xs[i]:
                t = (x - xs[i - 1]) / ((xs[i] - xs[i - 1]) or 1.0)
                return ys[i - 1] + (ys[i] - ys[i - 1]) * t
        return ys[n - 1]

    def _path_y(self, d: SceneDirector, x: float) -> float:
        """Lateral offset (LEFT+) of the planned path at x, else of the ego lane's centre, else 0."""
        if d.path:
            return self._interp(d.xs, d.path, x)
        l, r = d.lanes[1], d.lanes[2]
        if l and r:
            return (self._interp(d.xs, l, x) + self._interp(d.xs, r, x)) * 0.5
        return 0.0

    def _line_px(self, xs: Sequence[float], ys: Sequence[float]) -> List[Tuple[int, int]]:
        top = self.range_m + 2.0
        pts = []
        for x, y in zip(xs, ys):
            if x > top:
                break
            pts.append(self.to_px(x, y))
        return pts

    def _dashed(self, s: pygame.Surface, col: RGB, pts: List[Tuple[int, int]], on: int = 4, off: int = 4) -> None:
        carry = 0.0
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            seg = math.hypot(x1 - x0, y1 - y0)
            if seg <= 0:
                continue
            t = -carry
            while t < seg:
                a, b = max(0.0, t), min(seg, t + on)
                if b > a:
                    pygame.draw.line(s, col, (x0 + (x1 - x0) * a / seg, y0 + (y1 - y0) * a / seg),
                                     (x0 + (x1 - x0) * b / seg, y0 + (y1 - y0) * b / seg))
                t += on + off
            carry = t - seg - off if t - off > seg else 0.0

    def _draw(self, d: SceneDirector, inp: SceneInputs) -> None:
        s = self.surface
        s.blit(self._static_layer(), (0, 0))
        # the limit sign's corner is kept free of tags
        self.host_limit_rect = pygame.Rect(self.w - 48, 1, 46, 32) if inp.limit_kmh else None
        live = d.vision == "live"
        if live:
            for i in (1, 2):
                ln = d.lanes[i]
                if ln:
                    pts = self._line_px(d.xs, ln)
                    if len(pts) >= 2:
                        pygame.draw.lines(s, _k(self.col["lane"], d.line_alpha[i]), False, pts)
            if d.path:
                pts = self._line_px(d.xs, d.path)
                if len(pts) >= 2:
                    self._dashed(s, self.col["path"], pts)
            self._draw_objects(s, d, inp)
        else:
            self.tags = []
        self._draw_hud(s, d, inp)

    # -- objects -----------------------------------------------------------------

    def _items(self, d: SceneDirector, inp: SceneInputs) -> list:
        """(id, kind, x, y_left, gx, gy, state, motion, sd, ttc, lead, alpha) per drawn object: the road users
        and the lead, with the ground velocity (relative + the car's own speed along x)."""
        v_ego = inp.real_speed_kmh / 3.6
        out = []
        for o in d.objects:
            if o.x <= 0.0:
                continue
            sd = o.sd if o.sd is not None else (camera_sigma(o.x) if o.cam else None)
            out.append((o.oid or "%s@%.0f" % (o.kind, o.x), o.kind, o.x, o.y, o.vx + v_ego, o.vy, o.state,
                        o.motion, sd, o.ttc, False, o.alpha))
        ld = d.lead
        if ld is not None and d.lead_alpha > 0.0:
            al = d.alert
            state = ""
            if al is not None and al[4]:
                state = al[0]                       # the alert is on the lead
            gx = (ld.v_rel or 0.0) + v_ego
            out.append((ld.oid or "lead", ld.kind, ld.dist_m, ld.lat_m, gx, 0.0, state, "", ld.sd,
                        ld.ttc if ld.ttc is not None else (al[3] if (al is not None and al[4]) else None),
                        True, d.lead_alpha))
        return out

    def _colour(self, kind: str, state: str, lead: bool, blink_on: bool) -> RGB:
        col = self.col
        if state == "threat":
            return col["red"] if blink_on else _k(col["red"], 0.45)
        if state == "obstacle":
            return col["red"]
        if lead:
            return col["lead"]
        if kind in TWO_WHEELERS or kind == "person":
            return col["vru"]
        if state == "parked":
            return _k(col["traffic"], PARKED_DIM)
        return col["traffic"]

    @staticmethod
    def symbol_rect(kind: str, px: int, py: int) -> pygame.Rect:
        if kind in ("truck", "bus"):
            return pygame.Rect(px - 3, py - 6, 7, 13)
        if kind in VEHICLES:
            return pygame.Rect(px - 2, py - 4, 5, 9)
        return pygame.Rect(px - 3, py - 3, 7, 7)

    def _symbol(self, s: pygame.Surface, kind: str, px: int, py: int, col: RGB, lead: bool) -> pygame.Rect:
        rect = self.symbol_rect(kind, px, py)
        if kind in VEHICLES:
            if lead:
                pygame.draw.rect(s, col, rect)
                # target corners, the road view's motif
                o = rect.inflate(6, 6)
                for (cx, cy, dx, dy) in ((o.left, o.top, 1, 1), (o.right - 1, o.top, -1, 1),
                                         (o.left, o.bottom - 1, 1, -1), (o.right - 1, o.bottom - 1, -1, -1)):
                    pygame.draw.line(s, col, (cx, cy), (cx + 2 * dx, cy))
                    pygame.draw.line(s, col, (cx, cy), (cx, cy + 2 * dy))
                return o
            pygame.draw.rect(s, (0, 0, 0), rect)
            pygame.draw.rect(s, col, rect, 1)
        elif kind in TWO_WHEELERS:
            pts = [(px, py - 3), (px + 3, py), (px, py + 3), (px - 3, py)]
            pygame.draw.polygon(s, (0, 0, 0), pts)
            pygame.draw.polygon(s, col, pts, 1)
            if lead:
                pygame.draw.rect(s, col, rect.inflate(4, 4), 1)
        else:   # person
            pygame.draw.circle(s, (0, 0, 0), (px, py), 3)
            pygame.draw.circle(s, col, (px, py), 3, 1)
            s.set_at((px, py), col)
            if lead:
                pygame.draw.rect(s, col, rect.inflate(4, 4), 1)
        return rect

    def _draw_objects(self, s: pygame.Surface, d: SceneDirector, inp: SceneInputs) -> None:
        ppm, lpm = self.ppm, self.lpm
        blink_on = int(d.clock * 2 * BLINK_HZ) % 2 == 0
        clip = s.get_clip()
        s.set_clip(pygame.Rect(0, 0, self.w, self.h))
        items = self._items(d, inp)
        top = self.range_m + 1.0
        placed: Dict[str, Tuple[int, int, pygame.Rect, tuple]] = {}
        # far first: the near ones (that matter more) end up on top
        for it in sorted(items, key=lambda t: -t[2]):
            oid, kind, x, y, gx, gy, state, motion, sd, ttc, lead, alpha = it
            if x > top:
                continue
            px, py = self.to_px(x, y)
            if not (-6 <= px < self.w + 6):
                continue
            col = _k(self._colour(kind, state, lead, blink_on), max(0.25, alpha))
            dim = _k(col, 0.6)
            # range bar: +-1 sigma along the bearing
            if sd is not None and sd * ppm >= SD_MIN_PX:
                rng = math.hypot(x, y) or 1.0
                ux, uy = x / rng, y / rng
                a = self.to_px(max(0.0, x - ux * sd), y - uy * sd)
                b = self.to_px(x + ux * sd, y + uy * sd)
                pygame.draw.line(s, dim, a, b)
                # end ticks across the bearing
                tx, ty = int(round(uy * 2)), int(round(ux * 2))      # perpendicular in screen space
                for e in (a, b):
                    pygame.draw.line(s, dim, (e[0] - ty, e[1] - tx), (e[0] + ty, e[1] + tx))
            # velocity leader: ground velocity, where it is after LEADER_S
            g = math.hypot(gx, gy)
            if g >= LEADER_MIN_MPS:
                lx, ly = -gy * LEADER_S * lpm, -gx * LEADER_S * ppm
                ln = math.hypot(lx, ly)
                if ln > LEADER_MAX_PX:
                    lx, ly = lx * LEADER_MAX_PX / ln, ly * LEADER_MAX_PX / ln
                if ln >= 3:
                    pygame.draw.line(s, col, (px, py), (px + lx, py + ly))
            rect = self._symbol(s, kind, px, py, col, lead)
            if state == "obstacle":
                pygame.draw.rect(s, col, rect.inflate(6, 6), 1)
            elif state == "threat" and blink_on:
                pygame.draw.circle(s, col, (px, py), max(rect.w, rect.h) // 2 + 5, 1)
            placed[oid] = (px, py, rect, it)
        self._draw_tags(s, d, placed)
        s.set_clip(clip)

    # -- tags ------------------------------------------------------------------------

    def _tag_targets(self, d: SceneDirector, placed: dict) -> List[str]:
        """The few objects that get a data tag, most important first: threats, obstacles, the lead, the
        nearest in the path, the nearest person / two-wheeler."""
        its = [p[3] for p in placed.values()]
        order: List[str] = []

        def add(oid):
            if oid not in order and len(order) < TAGS_MAX:
                order.append(oid)
        for it in sorted(its, key=lambda t: t[2]):
            if it[6] == "threat":
                add(it[0])
        for it in sorted(its, key=lambda t: t[2]):
            if it[6] == "obstacle":
                add(it[0])
        for it in its:
            if it[10]:
                add(it[0])
        inpath = [it for it in its if not it[10] and it[6] != "parked" and abs(it[3] - self._path_y(d, it[2])) < IN_PATH_M]
        if inpath:
            add(min(inpath, key=lambda t: t[2])[0])
        vru = [it for it in its if (it[1] == "person" or it[1] in TWO_WHEELERS) and it[2] <= VRU_TAG_M]
        if vru:
            add(min(vru, key=lambda t: math.hypot(t[2], t[3]))[0])
        return order

    @staticmethod
    def _hyst(store: Dict[str, int], key: str, value: float, band: float) -> int:
        shown = store.get(key)
        if shown is None or abs(value - shown) > 0.5 + band:
            shown = int(value + 0.5)
            store[key] = shown
        return shown

    def _tag_lines(self, it: tuple) -> List[Tuple[pygame.Surface, Optional[str], RGB]]:
        """Lines of a tag: (number, unit / glyph, colour of the glyph)."""
        oid, kind, x, y, gx, gy, state, motion, sd, ttc, lead, alpha = it
        host, col = self.host, self.col
        rng = math.hypot(x, y)
        dm = self._hyst(self._shown_d, oid, rng, 0.3)
        kmh = self._hyst(self._shown_v, oid, math.hypot(gx, gy) * 3.6, 1.5)
        approx = sd is not None and sd >= 0.15 * max(rng, 1.0)
        d_col = col["red"] if state in ("threat", "obstacle") else col["text"]
        lines = [(host.text(("~%d" if approx else "%d") % dm, 14, d_col), "m", col["label"])]
        if not motion and lead:
            # the lead tracker's speed is good enough to tell; a camera-only road user stays unknown
            motion = "stationary" if math.hypot(gx, gy) < STAND_MPS else "moving"
        if motion == "stationary":
            glyph = "stand"
        elif motion == "moving":       # an arrow in its ground heading: with us, towards us, across
            glyph = ("up" if gx >= 0 else "down") if abs(gx) >= abs(gy) else ("left" if gy > 0 else "right")
        else:
            glyph = "?"
        lines.append((host.text("%d" % kmh, 14, col["value"]), glyph, col["value"]))
        if state == "threat" and ttc is not None:
            lines.append((host.text("%.1fs" % ttc, 14, col["red"]), None, col["red"]))
        return lines

    def _glyph(self, s: pygame.Surface, g: str, x: int, y: int, col: RGB) -> int:
        """One-glyph state at (x, y) = top-left of a 5 x 10 cell; returns its width."""
        if g == "m":
            s.blit(self.host.text("m", 8, col, mono=False), (x, y + 3))
            return 5
        if g == "stand":                  # standing: a filled square
            pygame.draw.rect(s, col, (x, y + 3, 4, 4))
            return 5
        if g == "up":                     # moving our way: a triangle up
            pygame.draw.polygon(s, col, [(x + 2, y + 2), (x + 4, y + 7), (x, y + 7)])
            return 5
        if g == "down":                   # coming towards us: a triangle down
            pygame.draw.polygon(s, col, [(x, y + 2), (x + 4, y + 2), (x + 2, y + 7)])
            return 5
        if g == "left":                   # crossing to the left
            pygame.draw.polygon(s, col, [(x, y + 5), (x + 4, y + 3), (x + 4, y + 7)])
            return 5
        if g == "right":                  # crossing to the right
            pygame.draw.polygon(s, col, [(x + 4, y + 5), (x, y + 3), (x, y + 7)])
            return 5
        if g == "?":                      # not known: a 4 x 7 question mark (04B03's is too small to read)
            for dx, dy in QMARK:
                s.set_at((x + dx, y + 2 + dy), col)
            return 5
        return 0

    def _keepouts(self) -> List[pygame.Rect]:
        r = [pygame.Rect(self.cx - 6, self.ey - 3, 13, EGO_BOTTOM + 3),   # the own car
             pygame.Rect(0, self.h - 21, 92, 21)]                          # the status line
        if self.host_limit_rect is not None:
            r.append(self.host_limit_rect)
        return r + list(self.keepout)

    def _draw_tags(self, s: pygame.Surface, d: SceneDirector, placed: dict) -> None:
        self.tags = []
        targets = self._tag_targets(d, placed)
        # a tag keeps clear of the tagged symbols with their frames (obstacle) and rings (threat)
        grow = {"threat": 14, "obstacle": 8}
        blocked = self._keepouts() + [placed[t][2].inflate(2 + grow.get(placed[t][3][6], 0),
                                                           2 + grow.get(placed[t][3][6], 0)) for t in targets]
        others = [p[2] for oid, p in placed.items() if oid not in targets]   # avoided when there is room
        bounds = pygame.Rect(1, 1, self.w - 2, self.h - 2)
        for oid in targets:
            px, py, rect, it = placed[oid]
            lines = self._tag_lines(it)
            w = max(t.get_width() + (6 if g else 0) for t, g, _ in lines) + 2
            h = 12 * len(lines) + 1
            g = grow.get(it[6], 0) // 2               # outside its own frame / ring
            own = rect.inflate(2 * g, 2 * g)
            cands = ((own.right + 4, own.top - h + 2), (own.left - 4 - w, own.top - h + 2),
                     (own.right + 4, py - h // 2), (own.left - 4 - w, py - h // 2),
                     (own.right + 4, own.bottom - 2), (own.left - 4 - w, own.bottom - 2))
            first = self._slot.get(oid, 0)
            box = None
            order = [first] + [j for j in range(len(cands)) if j != first]
            for avoid in (blocked + others, blocked):
                for i in order:
                    cand = pygame.Rect(cands[i][0], cands[i][1], w, h)
                    if bounds.contains(cand) and cand.collidelist(avoid) < 0:
                        box, self._slot[oid] = cand, i
                        break
                if box is not None:
                    break
            if box is None:
                continue                         # no room without overlap: no tag rather than a pile
            blocked.append(box.inflate(4, 4))
            self.tags.append((oid, box))
            # leader from the symbol to the tag's near edge
            ax = box.left if box.left > rect.right else box.right - 1
            ay = min(max(py, box.top), box.bottom - 1)
            pygame.draw.line(s, self.col["tag_line"], (rect.centerx, rect.centery), (ax, ay))
            pygame.draw.rect(s, (0, 0, 0), box)
            yy = box.top
            for t, g, gcol in lines:
                s.blit(t, (box.left + 1, yy - 1))
                if g:
                    self._glyph(s, g, box.left + 2 + t.get_width(), yy, gcol)
                yy += 12
            # redraw the symbol over the leader
            col = self._colour(it[1], it[6], it[10], int(d.clock * 2 * BLINK_HZ) % 2 == 0)
            self._symbol(s, it[1], px, py, _k(col, max(0.25, it[11])), it[10])

    # -- HUD ---------------------------------------------------------------------------

    def _draw_hud(self, s: pygame.Surface, d: SceneDirector, inp: SceneInputs) -> None:
        host, col = self.host, self.col
        tune = host.tune
        if tune.show_speed:
            spd = host.text("%d" % int(inp.speed_kmh + 0.5), 28, col["text"])
            pygame.draw.rect(s, (0, 0, 0), (2, 2, spd.get_width() + 30, 27))
            s.blit(spd, (5, 1))
            s.blit(host.text("km/h", 8, col["label"], mono=False), (5 + spd.get_width() + 3, 6))
        if inp.limit_kmh:
            x_right = self.w - 20
            over = inp.speed_kmh > inp.limit_kmh + 3
            host._draw_limit(s, x_right - 14, 16, inp.limit_kmh, over,
                             host.limit_style(inp.limit_source, inp.limit_conf))
        # status, bottom-left: objects tracked, or why there is nothing
        y = self.h - 20
        if not self.radar_live(d):
            host._chip(s, 4, y, "NO RADAR DATA", col["dim"])
        else:
            n = len(d.objects) + (1 if (d.lead is not None and d.lead_alpha > 0.0) else 0)
            t = host.text("%d" % n, 14, col["value"])
            s.blit(t, (5, y + 2))
            s.blit(host.text("TRK", 8, col["label"], mono=False), (5 + t.get_width() + 3, y + 7))
