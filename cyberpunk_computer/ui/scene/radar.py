"""
Radar view: a 360-degree plan position display of the road users chris-integrator tracks, in the visual language
of an air-traffic-control radar scope.

The alternative picture of the scene rectangle while the road view is open (``ScenePage``: a small button
switches between the two). Seen from above, the own car in the CENTRE, heading up (relative bearings):

    ┌──────────────────────────────┐ y 0
    │40 RNG      ▼            (50) │  range readout, the heading marker over the rim, the limit sign
    │     .  ·  ' | '  ·  .        │  rim with bearing ticks (10 / 30 / 90 degrees), 270 / 90 / 180 labelled
    │   ·    ▢ ·  |  · ▢     ·     │  range rings (quarters of the range), labels on the lower-left spoke
    │270- - - ▢ - ▲ - - ▣ - - - 90 │  cross-hair spokes; the own car (chevron) in the centre
    │   ·   ⁘ ▢   |    ⁘     ·     │  targets: symbol by kind, history dots, velocity vector, data block
    │     .  ·  , | ,  ·  .        │  remembered targets: dotted ghost symbol inside a dotted ring of +-1 sigma
    │12 TRK      180         [ROAD]│  track count / status, the switch button
    └──────────────────────────────┘ y 216

Scale: one scale in every direction (a plan): ``RANGES`` steps to the rim at ``R_PX`` (100 px) from the centre.
The range is chosen by what matters AHEAD (lead, threats, obstacles, moving traffic in the path, persons near
it, anything seen that reaches the car within a few seconds, ``RANGE_AHEAD_S`` of driving); a larger range is
taken at once, a smaller one after it has fitted for ``RANGE_HOLD_S``. Remembered objects behind never widen it.
``Tuning.radar_centre = "ahead"`` moves the centre down at the two larger ranges (more picture ahead, less
behind, the scale set by the distance ahead); the default is the centre.

Targets (1 px lines and flat fills, 1-bit): vehicle = a box (truck / bus larger), two-wheeler = a diamond,
person = a ring; the lead filled with target corners. Seen: solid; coasting (in view, not seen for a moment):
the same at 70 %; REMEMBERED (out of every sensor's field, carried on by its motion: with today's one forward
camera everything behind and far beside): a dotted ghost of the symbol, dim and dimmer as its place blurs, inside
a dotted ring of its 1 sigma, no history dots, never red. History: up to ``TRAIL_N`` dots of where a moving seen
target was over the ground (``TRAIL_DT`` apart, odometry-corrected; a standing one has none). Velocity vector:
where it is in ``LEADER_S`` over the ground. Data blocks with a leader line: an identifier line (kind letter and
track number, ``LEAD``; ``CST`` / ``MEM`` and the age for a coasting / remembered track), a value line (range in m,
ground speed in km/h with a heading glyph), the time to reach it for a threat; de-conflicted against each other,
the targets, the own car and the screen furniture. Conflict alert: a threat's symbol, ring and block flash at
1 Hz in red; an obstacle is red with a frame; the rest of the scope is cyan.

Sweep (``Tuning.radar_sweep``): a line turning once in ``SWEEP_PERIOD_S``, stepped ``SWEEP_HZ`` times a second
with an afterglow of the last ``SWEEP_GLOW`` steps, composited over the finished picture (the picture is not
redrawn for it: one blit of the kept picture, a few lines, the tags and the furniture blitted back over them).

Cost (docs/MFD_MAIN_SCREEN.md 10.5): the scope furniture is one cached surface per (range, centre); a redraw
blits it and draws the path, at most 17 symbols with history, vectors and rings, and up to ``TAGS_MAX`` blocks
from cached text. It redraws only when a symbol moved by about a pixel, a history dot was added, or a threat
blinks, at most at ``fps_scene`` like the road view.
"""

from __future__ import annotations

import math
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

import pygame

from .director import SceneDirector
from .inputs import SceneInputs

RGB = Tuple[int, int, int]

RANGES = (20, 40, 60, 80)                   # m from the centre to the rim (the integrator sends 85 m all round)
R_PX = 100                                  # rim radius, px (the picture is 238 x 216)
RANGE_HOLD_S = 4.0                          # a smaller range must fit this long before it is taken
RANGE_MARGIN_M = 4.0                        # an object that sets the range stays this far inside it
RANGE_AHEAD_S = 2.0                         # the range covers at least this much driving ahead
REACH_S = 3.0                               # a seen object that comes within REACH_M of the car within this
PATH_NEAR_S, PATH_NEAR_M = 3.0, 25.0         # traffic in the path / persons near it count within this
REACH_M = 2.0                               # ... sets the range and gets a data block
RADAR_STALE_S = 1.5                         # no radar frame for this long: NO RADAR DATA
LEADER_S = 1.0                              # the velocity vector: where it is after this long
LEADER_MIN_MPS = 1.0                        # no vector (and no history) below this ground speed
LEADER_MAX_PX = 30
STAND_MPS = 0.6                             # the lead's ground speed under this reads as standing (no radar verdict)
SD_MIN_PX = 3                               # a range bar shorter than this (each side) is not drawn
MEM_RING_MIN_PX = 4                         # a remembered object's sigma ring from this radius
MEM_RING_MAX_PX = 40
MEM_DIM = (0.7, 0.35)                       # a remembered object's brightness: at first, when its place is poor
MEM_BLUR_M = 8.0                            # ... "poor": 1 sigma this large (the radar drops it at 10 m)
COAST_DIM = 0.7
TRAIL_DT = 0.5                              # s between two history dots
TRAIL_N = 4
TRAIL_DIM = (0.6, 0.45, 0.32, 0.22)
TAGS_MAX = 5
IN_PATH_M = 1.6                             # half width of the corridor around the path ("in path")
VRU_TAG_M = 30.0                            # the nearest person / two-wheeler within this gets a block
MEM_TAG_M = 20.0                            # the nearest remembered object within this gets one (if room)
BLINK_HZ = 1.0                              # conflict alert: calm (on 0.5 s, off 0.5 s)
PARKED_DIM = 0.6                            # as the road view (render.PARKED_DIM)
AHEAD_CY = 150                              # radar_centre "ahead": the centre's row at the two larger ranges
SWEEP_PERIOD_S = 4.0
SWEEP_HZ = 5.0
SWEEP_GLOW = 3                              # afterglow: this many earlier steps, dimmer
SWEEP_GLOW_DIM = (0.42, 0.26, 0.14)

QMARK = ((1, 0), (2, 0), (0, 1), (3, 1), (3, 2), (2, 3), (2, 4), (2, 6))
VEHICLES = ("car", "van", "truck", "bus")
TWO_WHEELERS = ("bike", "motorbike")
KIND_LETTER = {"car": "C", "van": "V", "truck": "T", "bus": "B", "bike": "K", "motorbike": "M", "person": "P"}


def _k(c: RGB, k: float) -> RGB:
    return (int(c[0] * k), int(c[1] * k), int(c[2] * k))


def it_vru(it) -> bool:
    return it.kind == "person" or it.kind in TWO_WHEELERS


def camera_sigma(x: float) -> float:
    """1 sigma of a distance placed by the camera alone (chris-integrator docs/TOPICS.md: about 15 % of the
    distance at 20 m, 30 % at 40 m; held at 30 % beyond)."""
    x = max(0.0, x)
    return x * min(0.30, 0.0075 * x)


class Item(NamedTuple):
    """One drawn target: car frame (x ahead, y LEFT), ground velocity (gx, gy)."""
    oid: str
    kind: str
    x: float
    y: float
    gx: float
    gy: float
    state: str          # "" | parked | obstacle | threat
    motion: str
    sd: Optional[float]
    ttc: Optional[float]
    lead: bool
    alpha: float
    track: str = ""     # "" seen | coast | memory
    ago: float = 0.0

    @property
    def rng(self) -> float:
        return math.hypot(self.x, self.y)


class RadarView:
    """Owns the radar view's surface; ``update()`` steps its range and history, ``render()`` returns the surface,
    redrawn when needed. ``host`` is the scene's ``SceneRenderer`` (palette, text cache, chips, the limit sign)."""

    def __init__(self, host, size: Tuple[int, int] = (238, 216)):
        self.host = host
        self.w, self.h = size
        self.cx = self.w // 2
        self.cy0 = self.h // 2
        pal = host.pal
        self.col = {
            "rim": (0, 120, 120),
            "ring": (0, 66, 70),
            "spoke": (0, 46, 50),
            "tick": (0, 100, 100),
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
            "sweep": (0, 150, 140),
        }
        tune = host.tune
        self.centre_mode = str(getattr(tune, "radar_centre", "centre"))
        self.sweep_on = bool(getattr(tune, "radar_sweep", False))
        self.trail_on = bool(getattr(tune, "radar_trail", True))
        self.surface = pygame.Surface(size)
        self.surface.fill((0, 0, 0))
        self.base = pygame.Surface(size)      # the picture without the sweep (the sweep is composited over it)
        self.range_m = RANGES[1]
        self._fit_s = 0.0                    # how long a smaller range has fitted
        self._static: Dict[tuple, pygame.Surface] = {}
        self._rings: Dict[tuple, pygame.Surface] = {}
        self._slot: Dict[str, int] = {}      # tag placement memory: object id -> candidate index
        self._shown_d: Dict[str, int] = {}   # read-outs with a hysteresis (no flicker of the last digit)
        self._shown_v: Dict[str, int] = {}
        self._sig: Optional[tuple] = None
        self._sweep_drawn: Optional[int] = None
        self._last_draw = -1.0
        self.min_redraw_s = host.min_redraw_s
        self.redraws = 0
        self.sweeps = 0                      # sweep-only composites (tests, the bench)
        self.keepout: List[pygame.Rect] = []  # set by the page: the button (scene coordinates)
        self.tags: List[Tuple[str, pygame.Rect]] = []   # last drawn tags (id, rect): tests / debugging
        self.host_limit_rect: Optional[pygame.Rect] = None
        self._over: List[pygame.Rect] = []   # what the sweep must not cross (tags, furniture)
        # history: odometer (m driven) and, per object, (odometer, x, y) every TRAIL_DT
        self.odo = 0.0
        self._trail: Dict[str, List[Tuple[float, float, float]]] = {}
        self._trail_t = 0.0
        self._trail_n = 0

    # ── geometry ─────────────────────────────────────────────────────────────

    @property
    def cy(self) -> int:
        if self.centre_mode == "ahead" and self.range_m >= RANGES[2]:
            return AHEAD_CY
        return self.cy0

    @property
    def rim_px(self) -> int:
        """Pixels from the centre to the rim AHEAD (the range): R_PX, or what the moved-down centre leaves."""
        return R_PX if self.cy == self.cy0 else self.cy - 8

    @property
    def ppm(self) -> float:
        return self.rim_px / float(self.range_m)

    def to_px(self, x: float, y_left: float) -> Tuple[int, int]:
        """Car frame (m ahead, m LEFT positive) -> scene pixel."""
        p = self.ppm
        return int(round(self.cx - y_left * p)), int(round(self.cy - x * p))

    # ── state ────────────────────────────────────────────────────────────────

    @staticmethod
    def radar_live(d: SceneDirector) -> bool:
        return d.vision == "live" and d.radar_age <= RADAR_STALE_S

    def _need_m(self, d: SceneDirector, inp: SceneInputs) -> float:
        """The distance the picture must show (ahead first): lead, threats, obstacles, moving traffic in the path
        and persons near it (within PATH_NEAR_S of driving), what is seen coming within REACH_M of the car within
        REACH_S, and RANGE_AHEAD_S of driving."""
        need = max(15.0, inp.real_speed_kmh / 3.6 * RANGE_AHEAD_S)
        if d.vision != "live":
            return need
        for it in self._items(d, inp):
            if it.track == "memory":
                continue                     # remembered objects never widen the scope
            r = it.rng
            if it.lead or it.state in ("threat", "obstacle"):
                need = max(need, r + RANGE_MARGIN_M)
            elif it.state != "parked" and it.motion != "stationary":
                if 0.0 < it.x <= max(PATH_NEAR_M, inp.real_speed_kmh / 3.6 * PATH_NEAR_S):
                    off = abs(it.y - self._path_y(d, it.x))
                    if off < IN_PATH_M or (off < 4.0 and it.kind == "person"):
                        need = max(need, r + RANGE_MARGIN_M)
                        continue
                # coming close to the car soon (closest approach within REACH_M in REACH_S at the present
                # relative motion): crossing traffic heading for us, not the oncoming lane passing by
                if self.reaches(it, inp.real_speed_kmh / 3.6):
                    need = max(need, r + RANGE_MARGIN_M)
        return need

    @staticmethod
    def reaches(it: Item, v_ego: float) -> bool:
        """It comes within REACH_M of the car within REACH_S at the present relative motion."""
        ux, uy = it.gx - v_ego, it.gy
        uu = ux * ux + uy * uy
        if uu < 1.0:
            return False
        t = -(it.x * ux + it.y * uy) / uu
        if not 0.0 < t < REACH_S:
            return False
        return math.hypot(it.x + ux * t, it.y + uy * t) < REACH_M

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
        self._update_trails(d, inp, dt)

    def _update_trails(self, d: SceneDirector, inp: SceneInputs, dt: float) -> None:
        """History dots: where each moving seen target was over the ground, every TRAIL_DT. Positions are kept
        in the car frame with the odometer at that time and shifted back by the distance driven since (the
        scene carries no yaw: in a turn the dots of a target lag sideways a little; standing targets have none)."""
        self.odo += max(0.0, inp.real_speed_kmh) / 3.6 * dt
        if not self.trail_on:
            return
        self._trail_t += dt
        if self._trail_t < TRAIL_DT:
            return
        self._trail_t = 0.0
        if d.vision != "live":
            self._trail.clear()
            return
        keep: Dict[str, List[Tuple[float, float, float]]] = {}
        for it in self._items(d, inp):
            if it.track == "memory" or math.hypot(it.gx, it.gy) < LEADER_MIN_MPS:
                continue
            h = self._trail.get(it.oid, [])
            keep[it.oid] = ([(self.odo, it.x, it.y)] + h)[:TRAIL_N + 1]
        if keep or self._trail:
            self._trail_n += 1               # the dots moved on (nothing moves: no redraw for them)
        self._trail = keep

    # ── frame ────────────────────────────────────────────────────────────────

    def _sweep_step(self, d: SceneDirector) -> Optional[int]:
        if not self.sweep_on or not self.radar_live(d):
            return None
        return int(d.clock * SWEEP_HZ)

    def _signature(self, d: SceneDirector, inp: SceneInputs) -> tuple:
        step = 1.0 / self.ppm                  # about a pixel
        live = d.vision == "live"

        def q(v):
            return int(round(v / step))
        items = self._items(d, inp) if live else []
        objs = tuple((it.oid, it.kind, it.state, it.motion, it.track, q(it.x), q(it.y), int(it.alpha * 10),
                      int(it.gx * 2), int(it.gy * 2), q(it.sd or 0.0), int(it.ago)) for it in items)
        path = tuple(q(v) for v in d.path[:8]) if (live and d.path) else ()
        threat = live and (any(it.state == "threat" for it in items) or (d.alert is not None
                                                                       and d.alert[0] == "threat"))
        blink = int(d.clock * 2 * BLINK_HZ) % 2 if threat else 0
        return (self.range_m, d.vision, self.radar_live(d), objs, path, blink, self._trail_n,
                int(inp.real_speed_kmh / 2), inp.limit_kmh, inp.limit_source, int(inp.speed_kmh + 0.5), d.alert,
                len(self.keepout))

    def due(self, d: SceneDirector, inp: SceneInputs, now: float) -> bool:
        return (self._signature(d, inp) != self._sig
                or self._sweep_step(d) != self._sweep_drawn
                or (d.animating and now - self._last_draw >= self.min_redraw_s))

    def render(self, d: SceneDirector, inp: SceneInputs, now: float) -> pygame.Surface:
        sig = self._signature(d, inp)
        sweep = self._sweep_step(d)
        if sig != self._sig or (d.animating and now - self._last_draw >= self.min_redraw_s):
            self._sig = sig
            self._last_draw = now
            d.road_dirty = False          # what the road data shows is on the surface (director.animating)
            self._draw(d, inp)
            self.redraws += 1
            self._compose(sweep)
        elif sweep != self._sweep_drawn:
            self._compose(sweep)
            self.sweeps += 1
        return self.surface

    def invalidate(self) -> None:
        self._sig = None

    def _compose(self, sweep: Optional[int]) -> None:
        """The kept picture, and over it the sweep with its afterglow; tags and furniture stay on top."""
        self._sweep_drawn = sweep
        self.surface.blit(self.base, (0, 0))
        if sweep is None:
            return
        s = self.surface
        cx, cy, r = self.cx, self.cy, self.rim_px - 1
        clip = s.get_clip()
        s.set_clip(pygame.Rect(0, 0, self.w, self.h))
        for i in range(SWEEP_GLOW, -1, -1):
            a = 2.0 * math.pi * (((sweep - i) / SWEEP_HZ) % SWEEP_PERIOD_S) / SWEEP_PERIOD_S
            col = _k(self.col["sweep"], 1.0 if i == 0 else SWEEP_GLOW_DIM[i - 1])
            pygame.draw.line(s, col, (cx + 9 * math.sin(a), cy - 9 * math.cos(a)),
                             (cx + r * math.sin(a), cy - r * math.cos(a)))
        for rect in self._over:
            s.blit(self.base, rect, rect)
        s.set_clip(clip)

    # ── static layer: the scope ──────────────────────────────────────────────

    def _dotted_circle(self, surf: pygame.Surface, c: Tuple[int, int], r: float, col: RGB, every: int = 2) -> None:
        n = max(24, int(2 * math.pi * r))
        last = None
        k = 0
        for i in range(n):
            a = 2.0 * math.pi * i / n
            p = (int(round(c[0] + r * math.cos(a))), int(round(c[1] - r * math.sin(a))))
            if p == last:
                continue
            last = p
            k += 1
            if k % every == 0 and 0 <= p[0] < surf.get_width() and 0 <= p[1] < surf.get_height():
                surf.set_at(p, col)

    def _ring_sprite(self, r: int, col: RGB) -> pygame.Surface:
        """A remembered target's dotted sigma ring, cached (black is transparent)."""
        key = (r, col)
        spr = self._rings.get(key)
        if spr is None:
            if len(self._rings) > 48:
                self._rings.clear()
            spr = pygame.Surface((2 * r + 1, 2 * r + 1))
            spr.fill((0, 0, 0))
            spr.set_colorkey((0, 0, 0))
            self._dotted_circle(spr, (r, r), r, col, every=3)
            self._rings[key] = spr
        return spr

    def _static_layer(self) -> pygame.Surface:
        key = (self.range_m, self.cy)
        surf = self._static.get(key)
        if surf is not None:
            return surf
        surf = pygame.Surface((self.w, self.h))
        surf.fill((0, 0, 0))
        cx, cy, R = self.cx, self.cy, self.rim_px
        col, host = self.col, self.host
        # cross-hair spokes (fine, sparse dots), from the first ring outwards
        q = R / 4.0
        for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
            for t in range(int(q * 0.6), R, 3):
                x, y = cx + dx * t, cy + dy * t
                if 0 <= x < self.w and 0 <= y < self.h:
                    surf.set_at((x, y), col["spoke"])
        # range rings: quarters of the range, dotted; the rim solid
        for i in (1, 2, 3):
            self._dotted_circle(surf, (cx, cy), q * i, col["ring"])
        pygame.draw.circle(surf, col["rim"], (cx, cy), R, 1)
        # bearing ticks inward from the rim: 10 / 30 / 90 degrees (relative bearings, heading up)
        for deg in range(0, 360, 10):
            ln = 6 if deg % 90 == 0 else 4 if deg % 30 == 0 else 2
            a = math.radians(deg)
            sx, sy = math.sin(a), -math.cos(a)
            pygame.draw.line(surf, col["tick"], (cx + (R - 1) * sx, cy + (R - 1) * sy),
                             (cx + (R - ln) * sx, cy + (R - ln) * sy))
        # the car's heading: a filled marker over the rim (the lubber line)
        hy = cy - R - 1
        if hy >= 6:
            pygame.draw.polygon(surf, col["value"], [(cx, hy), (cx - 4, hy - 6), (cx + 4, hy - 6)])
        # 90 / 180 / 270 outside the rim where there is room
        for deg, txt in ((90, "90"), (180, "180"), (270, "270")):
            lab = host.text(txt, 8, col["ring_label"], mono=False)
            a = math.radians(deg)
            px, py = cx + (R + 4) * math.sin(a), cy - (R + 4) * math.cos(a)
            if deg == 90:
                pos = (int(px), int(py) - 3)
            elif deg == 270:
                pos = (int(px) - lab.get_width(), int(py) - 3)
            else:
                pos = (int(px) - lab.get_width() // 2, int(py) - 2)
            if pos[0] >= 0 and pos[0] + lab.get_width() <= self.w and pos[1] + 6 <= self.h:
                surf.blit(lab, pos)
        # ring labels on the lower-left spoke (225 degrees), inside each ring
        for i in (1, 2, 3, 4):
            m = self.range_m * i / 4.0
            lab = host.text("%d" % m if m == int(m) else "%.0f" % m, 8, col["ring_label"], mono=False)
            r = q * i
            px, py = cx - r * 0.7071, cy + r * 0.7071
            pos = (int(px) + 2, int(py) - 8)
            if pos[1] + 6 < self.h:
                pygame.draw.rect(surf, (0, 0, 0), (pos[0] - 1, pos[1], lab.get_width() + 2, 7))
                surf.blit(lab, pos)
        # the own car: a filled chevron, the camera (range 0) at its centre
        pygame.draw.polygon(surf, host.pal["lane_ego"],
                            [(cx, cy - 6), (cx + 4, cy + 5), (cx, cy + 2), (cx - 4, cy + 5)])
        if len(self._static) > 6:
            self._static.clear()
        self._static[key] = surf
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
        top = self.range_m
        pts = []
        for x, y in zip(xs, ys):
            if x > top or y is None:
                break
            if math.hypot(x, y) > top:
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
        s = self.base
        s.blit(self._static_layer(), (0, 0))
        # the limit sign's corner is kept free of tags
        self.host_limit_rect = pygame.Rect(self.w - 48, 1, 46, 32) if inp.limit_kmh else None
        self._over = [pygame.Rect(self.cx - 5, self.cy - 7, 11, 14)]    # the own car
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

    def _items(self, d: SceneDirector, inp: SceneInputs) -> List[Item]:
        """The drawn targets with their ground velocity (relative + the car's own speed along x): the all-round
        list (vw.rd) when the integrator sends it, else the road users ahead and the lead (older integrator)."""
        v_ego = inp.real_speed_kmh / 3.6
        out: List[Item] = []
        around = getattr(d, "around", None)
        if around is not None:
            for o in around:
                sd = o.sd
                out.append(Item(o.oid or "%s@%.0f" % (o.kind, o.x), o.kind, o.x, o.y, o.vx + v_ego, o.vy,
                                "" if (o.track == "memory" and o.state in ("threat", "obstacle")) else o.state,
                                o.motion, sd, o.ttc, o.lead, o.alpha, o.track, o.ago))
            return out
        for o in d.objects:
            if o.x <= 0.0:
                continue
            sd = o.sd if o.sd is not None else (camera_sigma(o.x) if o.cam else None)
            out.append(Item(o.oid or "%s@%.0f" % (o.kind, o.x), o.kind, o.x, o.y, o.vx + v_ego, o.vy, o.state,
                            o.motion, sd, o.ttc, False, o.alpha))
        ld = d.lead
        if ld is not None and d.lead_alpha > 0.0:
            al = d.alert
            state = ""
            if al is not None and al[4]:
                state = al[0]                       # the alert is on the lead
            gx = (ld.v_rel or 0.0) + v_ego
            out.append(Item(ld.oid or "lead", ld.kind, ld.dist_m, ld.lat_m, gx, 0.0, state, "", ld.sd,
                            ld.ttc if ld.ttc is not None else (al[3] if (al is not None and al[4]) else None),
                            True, d.lead_alpha))
        return out

    def _colour(self, it: Item, blink_on: bool) -> RGB:
        col = self.col
        if it.state == "threat":
            return col["red"] if blink_on else _k(col["red"], 0.45)
        if it.state == "obstacle":
            return col["red"]
        if it.lead:
            base = col["lead"]
        elif it.kind in TWO_WHEELERS or it.kind == "person":
            base = col["vru"]
        elif it.state == "parked":
            base = _k(col["traffic"], PARKED_DIM)
        else:
            base = col["traffic"]
        if it.track == "memory":
            blur = min(1.0, (it.sd or 0.0) / MEM_BLUR_M)
            return _k(base, MEM_DIM[0] + (MEM_DIM[1] - MEM_DIM[0]) * blur)
        if it.track == "coast":
            return _k(base, COAST_DIM)
        return base

    @staticmethod
    def symbol_rect(kind: str, px: int, py: int) -> pygame.Rect:
        if kind in ("truck", "bus"):
            return pygame.Rect(px - 3, py - 6, 7, 13)
        if kind in VEHICLES:
            return pygame.Rect(px - 2, py - 4, 5, 9)
        return pygame.Rect(px - 3, py - 3, 7, 7)

    @staticmethod
    def _dotted_rect(s: pygame.Surface, rect: pygame.Rect, col: RGB) -> None:
        x0, y0, x1, y1 = rect.left, rect.top, rect.right - 1, rect.bottom - 1
        pts = ([(x, y0) for x in range(x0, x1 + 1)] + [(x1, y) for y in range(y0 + 1, y1 + 1)]
               + [(x, y1) for x in range(x1 - 1, x0 - 1, -1)] + [(x0, y) for y in range(y1 - 1, y0, -1)])
        for i, p in enumerate(pts):
            if i % 2 == 0:
                s.set_at(p, col)

    def _symbol(self, s: pygame.Surface, it: Item, px: int, py: int, col: RGB) -> pygame.Rect:
        kind, lead = it.kind, it.lead
        rect = self.symbol_rect(kind, px, py)
        if it.track == "memory":
            # a remembered target: a dotted ghost of its symbol (a coasting track on a scope)
            if kind in VEHICLES:
                self._dotted_rect(s, rect, col)
            elif kind in TWO_WHEELERS:
                for p in ((px, py - 3), (px + 2, py - 1), (px + 2, py + 1), (px, py + 3), (px - 2, py + 1),
                          (px - 2, py - 1)):
                    s.set_at(p, col)
            else:
                for p in ((px - 1, py - 3), (px + 1, py - 3), (px + 3, py - 1), (px + 3, py + 1), (px + 1, py + 3),
                          (px - 1, py + 3), (px - 3, py + 1), (px - 3, py - 1)):
                    s.set_at(p, col)
            return rect
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
        ppm = self.ppm
        blink_on = int(d.clock * 2 * BLINK_HZ) % 2 == 0
        clip = s.get_clip()
        s.set_clip(pygame.Rect(0, 0, self.w, self.h))
        items = self._items(d, inp)
        limit = self.range_m + 1.0
        cx, cy = self.cx, self.cy
        placed: Dict[str, Tuple[int, int, pygame.Rect, Item]] = {}

        def order(it: Item):
            # remembered first (underneath), then far before near; red always on top
            return (it.state in ("threat", "obstacle"), it.track != "memory", -it.rng)
        for it in sorted(items, key=order):
            if it.rng > limit:
                continue
            px, py = self.to_px(it.x, it.y)
            if not (-6 <= px < self.w + 6 and -6 <= py < self.h + 6):
                continue
            col = _k(self._colour(it, blink_on), max(0.25, it.alpha))
            dim = _k(col, 0.6)
            if it.track == "memory":
                # the place as it is known: a dotted ring of 1 sigma that grows as it blurs
                if it.sd is not None:
                    rr = min(MEM_RING_MAX_PX, it.sd * ppm)
                    if rr >= MEM_RING_MIN_PX:
                        ring = self._ring_sprite(int(round(rr)), dim)
                        s.blit(ring, (px - ring.get_width() // 2, py - ring.get_height() // 2))
            elif it.sd is not None and it.sd * ppm >= SD_MIN_PX:
                # range bar: +-1 sigma along the bearing
                rng = it.rng or 1.0
                ux, uy = it.x / rng, it.y / rng
                a = self.to_px(it.x - ux * it.sd, it.y - uy * it.sd)
                b = self.to_px(it.x + ux * it.sd, it.y + uy * it.sd)
                pygame.draw.line(s, dim, a, b)
                tx, ty = int(round(uy * 2)), int(round(ux * 2))      # perpendicular in screen space
                for e in (a, b):
                    pygame.draw.line(s, dim, (e[0] - ty, e[1] - tx), (e[0] + ty, e[1] + tx))
            # history: where it was over the ground (odometry-corrected), fading dots
            g = math.hypot(it.gx, it.gy)
            hist = self._trail.get(it.oid) if (self.trail_on and it.track != "memory" and g >= LEADER_MIN_MPS) else None
            if hist:
                for j, (odo, hx, hy) in enumerate(hist[1:TRAIL_N + 1]):
                    qx, qy = self.to_px(hx - (self.odo - odo), hy)
                    if abs(qx - px) + abs(qy - py) < 3:
                        continue
                    dc = _k(self._colour(it, True), TRAIL_DIM[j] * max(0.25, it.alpha))
                    pygame.draw.rect(s, dc, (qx, qy, 2, 2))
            # velocity vector: ground velocity, where it is after LEADER_S
            if g >= LEADER_MIN_MPS:
                lx, ly = -it.gy * LEADER_S * ppm, -it.gx * LEADER_S * ppm
                ln = math.hypot(lx, ly)
                if ln > LEADER_MAX_PX:
                    lx, ly = lx * LEADER_MAX_PX / ln, ly * LEADER_MAX_PX / ln
                if ln >= 3:
                    pygame.draw.line(s, dim if it.track == "memory" else col, (px, py), (px + lx, py + ly))
            rect = self._symbol(s, it, px, py, col)
            if it.state == "obstacle":
                pygame.draw.rect(s, col, rect.inflate(6, 6), 1)
            elif it.state == "threat" and blink_on:
                pygame.draw.circle(s, col, (px, py), max(rect.w, rect.h) // 2 + 5, 1)
            placed[it.oid] = (px, py, rect, it)
            if self.sweep_on:
                self._over.append(rect.inflate(2, 2))   # the sweep passes under the targets
        self._draw_tags(s, d, inp, placed)
        s.set_clip(clip)

    # -- data blocks ------------------------------------------------------------------

    def _tag_targets(self, d: SceneDirector, inp: SceneInputs, placed: dict) -> List[str]:
        """The few targets that get a data block, most important first: threats, obstacles, the lead, the
        nearest in the path ahead, the nearest person / two-wheeler, the nearest seen target closing on the car
        from anywhere, the nearest remembered one close by."""
        its: List[Item] = [p[3] for p in placed.values()]
        order: List[str] = []

        def add(oid):
            if oid not in order and len(order) < TAGS_MAX:
                order.append(oid)
        for st in ("threat", "obstacle"):
            for it in sorted(its, key=lambda t: t.rng):
                if it.state == st:
                    add(it.oid)
        for it in its:
            if it.lead:
                add(it.oid)
        seen = [it for it in its if it.track != "memory"]
        inpath = [it for it in seen if not it.lead and it.x > 0 and it.state != "parked"
                  and abs(it.y - self._path_y(d, it.x)) < IN_PATH_M]
        if inpath:
            add(min(inpath, key=lambda t: t.x).oid)
        vru = [it for it in seen if (it.kind == "person" or it.kind in TWO_WHEELERS) and it.rng <= VRU_TAG_M]
        if vru:
            add(min(vru, key=lambda t: t.rng).oid)
        v_ego = inp.real_speed_kmh / 3.6
        near = [it for it in seen if it.state != "parked" and self.reaches(it, v_ego)]
        if near:
            add(min(near, key=lambda t: t.rng).oid)
        # the nearest remembered target close by: a person / two-wheeler or a moving one before a parked car
        mem = [it for it in its if it.track == "memory" and it.rng <= MEM_TAG_M]
        if mem:
            add(min(mem, key=lambda t: (not (it_vru(t) or t.motion == "moving"), t.rng)).oid)
        return order

    @staticmethod
    def _hyst(store: Dict[str, int], key: str, value: float, band: float) -> int:
        shown = store.get(key)
        if shown is None or abs(value - shown) > 0.5 + band:
            shown = int(value + 0.5)
            store[key] = shown
        return shown

    @staticmethod
    def ident(it: Item) -> str:
        """The block's first line: kind letter + track number (``C12``), ``LEAD``; ``CST`` / ``MEM 6s``."""
        if it.lead:
            s = "LEAD"
        else:
            num = it.oid.rsplit(":", 1)[-1]
            if not num.isdigit():
                num = ""
            s = KIND_LETTER.get(it.kind, "U") + num[-3:]
        if it.track == "memory":
            s += " MEM %ds" % int(it.ago + 0.5) if it.ago >= 1.0 else " MEM"
        elif it.track == "coast":
            s += " CST"
        return s

    def _tag_lines(self, it: Item, red: RGB) -> list:
        """Lines of a block: ("caption", text surface) or ("value", number surface, glyph, glyph colour)."""
        host, col = self.host, self.col
        rng = it.rng
        dm = self._hyst(self._shown_d, it.oid, rng, 0.3)
        kmh = self._hyst(self._shown_v, it.oid, math.hypot(it.gx, it.gy) * 3.6, 1.5)
        approx = it.track == "memory" or (it.sd is not None and it.sd >= 0.15 * max(rng, 1.0))
        alarm = it.state in ("threat", "obstacle")
        id_col = red if alarm else (col["dim"] if it.track else col["label"])
        lines: list = [("caption", host.text(self.ident(it), 8, id_col, mono=False))]
        d_col = red if alarm else (_k(col["text"], 0.6) if it.track == "memory" else col["text"])
        motion = it.motion
        if not motion and it.lead:
            # the lead tracker's speed is good enough to tell; a camera-only road user stays unknown
            motion = "stationary" if math.hypot(it.gx, it.gy) < STAND_MPS else "moving"
        if motion == "stationary":
            glyph = "stand"
        elif motion == "moving":       # an arrow in its ground heading: with us, towards us, across
            glyph = ("up" if it.gx >= 0 else "down") if abs(it.gx) >= abs(it.gy) else ("left" if it.gy > 0 else "right")
        else:
            glyph = "?"
        v_col = _k(col["value"], 0.6) if it.track == "memory" else col["value"]
        lines.append(("value", host.text(("~%d" if approx else "%d") % dm, 14, d_col),
                      host.text("%d" % kmh, 14, v_col), glyph, v_col))
        if it.state == "threat" and it.ttc is not None:
            lines.append(("ttc", host.text("%.1fs" % it.ttc, 14, red)))
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
        if g == "left":                   # moving to the left
            pygame.draw.polygon(s, col, [(x, y + 5), (x + 4, y + 3), (x + 4, y + 7)])
            return 5
        if g == "right":                  # moving to the right
            pygame.draw.polygon(s, col, [(x + 4, y + 5), (x, y + 3), (x, y + 7)])
            return 5
        if g == "?":                      # not known: a 4 x 7 question mark (04B03's is too small to read)
            for dx, dy in QMARK:
                s.set_at((x + dx, y + 2 + dy), col)
            return 5
        return 0

    @staticmethod
    def _line_size(ln) -> Tuple[int, int]:
        if ln[0] == "caption":
            return ln[1].get_width(), 8
        if ln[0] == "value":
            return ln[1].get_width() + 5 + 4 + ln[2].get_width() + 6, 12
        return ln[1].get_width(), 12

    def _hud_rects(self) -> List[pygame.Rect]:
        r = [pygame.Rect(0, 0, 44, 15),                       # range readout
             pygame.Rect(0, self.h - 17, 64, 17)]              # track count / status
        if self.host.tune.show_speed:
            r.append(pygame.Rect(0, 0, 70, 30))
        if self.host_limit_rect is not None:
            r.append(self.host_limit_rect)
        return r + list(self.keepout)

    def _keepouts(self) -> List[pygame.Rect]:
        return [pygame.Rect(self.cx - 6, self.cy - 8, 13, 15)] + self._hud_rects()

    def _draw_tags(self, s: pygame.Surface, d: SceneDirector, inp: SceneInputs, placed: dict) -> None:
        self.tags = []
        targets = self._tag_targets(d, inp, placed)
        blink_on = int(d.clock * 2 * BLINK_HZ) % 2 == 0
        red = self.col["red"] if blink_on else _k(self.col["red"], 0.5)
        # a block keeps clear of the tagged symbols with their frames (obstacle) and rings (threat)
        grow = {"threat": 14, "obstacle": 8}
        blocked = self._keepouts() + [placed[t][2].inflate(2 + grow.get(placed[t][3].state, 0),
                                                           2 + grow.get(placed[t][3].state, 0)) for t in targets]
        others = [p[2].inflate(2, 2) for oid, p in placed.items() if oid not in targets]   # avoided when there is room
        bounds = pygame.Rect(1, 1, self.w - 2, self.h - 2)
        for oid in targets:
            px, py, rect, it = placed[oid]
            steady_red = self.col["red"] if it.state == "obstacle" else red
            lines = self._tag_lines(it, steady_red)
            sizes = [self._line_size(ln) for ln in lines]
            w = max(sw for sw, _ in sizes) + 4
            h = sum(sh for _, sh in sizes) + 3
            g = grow.get(it.state, 0) // 2               # outside its own frame / ring
            own = rect.inflate(2 * g, 2 * g)
            # eight places round the symbol; those away from the centre (the own car) first, the one used last
            # before them (a block does not hop)
            cands = [(own.right + 4, own.top - h + 2), (own.left - 4 - w, own.top - h + 2),
                     (own.right + 4, py - h // 2), (own.left - 4 - w, py - h // 2),
                     (own.right + 4, own.bottom - 2), (own.left - 4 - w, own.bottom - 2),
                     (px - w // 2, own.top - 4 - h), (px - w // 2, own.bottom + 4)]
            ox, oy = px - self.cx, py - self.cy

            def outward(i):
                bx, by = cands[i][0] + w / 2.0 - px, cands[i][1] + h / 2.0 - py
                return -(bx * ox + by * oy)
            pref = sorted(range(len(cands)), key=outward)
            first = self._slot.get(oid)
            order = ([first] if first is not None else []) + [j for j in pref if j != first]
            box = None
            for avoid in (blocked + others, blocked):
                for i in order:
                    cand = pygame.Rect(cands[i][0], cands[i][1], w, h)
                    if bounds.contains(cand) and cand.collidelist(avoid) < 0:
                        box, self._slot[oid] = cand, i
                        break
                if box is not None:
                    break
            if box is None:
                continue                         # no room without overlap: no block rather than a pile
            blocked.append(box.inflate(4, 4))
            self.tags.append((oid, box))
            self._over.append(box)
            # leader line from the symbol to the block's near edge
            ax = box.left if box.left > rect.right else (box.right - 1 if box.right <= rect.left else px)
            ay = min(max(py, box.top), box.bottom - 1)
            if box.left <= px < box.right:
                ay = box.bottom - 1 if box.bottom <= rect.top else box.top
            pygame.draw.line(s, steady_red if it.state in ("threat", "obstacle") else
                             (_k(self.col["tag_line"], 0.6) if it.track == "memory" else self.col["tag_line"]),
                             (px, py), (ax, ay))
            pygame.draw.rect(s, (0, 0, 0), box)
            if it.state == "obstacle" or (it.state == "threat" and blink_on):
                pygame.draw.rect(s, self.col["red"], box, 1)      # conflict alert: the block flashes
            yy = box.top + 2
            for ln, (sw, sh) in zip(lines, sizes):
                x = box.left + 2
                if ln[0] == "caption":
                    s.blit(ln[1], (x, yy))
                elif ln[0] == "value":
                    num, spd, glyph, gcol = ln[1], ln[2], ln[3], ln[4]
                    s.blit(num, (x, yy - 1))
                    x += num.get_width()
                    x += self._glyph(s, "m", x + 1, yy - 1, self.col["label"]) + 4
                    s.blit(spd, (x, yy - 1))
                    self._glyph(s, glyph, x + spd.get_width() + 1, yy - 1, gcol)
                else:
                    s.blit(ln[1], (x, yy - 1))
                yy += sh + (1 if ln[0] == "caption" else 0)
            # the symbol over the leader
            col = _k(self._colour(it, blink_on), max(0.25, it.alpha))
            self._symbol(s, it, px, py, col)

    # -- HUD ---------------------------------------------------------------------------

    def _draw_hud(self, s: pygame.Surface, d: SceneDirector, inp: SceneInputs) -> None:
        host, col = self.host, self.col
        tune = host.tune
        if tune.show_speed:
            spd = host.text("%d" % int(inp.speed_kmh + 0.5), 28, col["text"])
            pygame.draw.rect(s, (0, 0, 0), (2, 2, spd.get_width() + 30, 27))
            s.blit(spd, (5, 1))
            s.blit(host.text("km/h", 8, col["label"], mono=False), (5 + spd.get_width() + 3, 6))
            ry = 31
        else:
            ry = 1
        # range scale readout, top-left: the rim's distance
        t = host.text("%d" % self.range_m, 14, col["value"])
        s.blit(t, (3, ry))
        s.blit(host.text("M RNG", 8, col["label"], mono=False), (3 + t.get_width() + 2, ry + 5))
        self._over.append(pygame.Rect(0, ry, 44, 14))
        if inp.limit_kmh:
            x_right = self.w - 20
            over = inp.speed_kmh > inp.limit_kmh + 3
            host._draw_limit(s, x_right - 14, 16, inp.limit_kmh, over,
                             host.limit_style(inp.limit_source, inp.limit_conf))
            self._over.append(self.host_limit_rect)
        # status, bottom-left: targets tracked and how many of them are remembered, or why there is nothing
        y = self.h - 17
        if not self.radar_live(d):
            w = host._chip(s, 3, y - 1, "NO RADAR DATA", col["dim"])
            self._over.append(pygame.Rect(3, y - 1, w, 17))
        else:
            items = self._items(d, inp)
            n = len(items)
            mem = sum(1 for it in items if it.track == "memory")
            t = host.text("%d" % n, 14, col["value"])
            s.blit(t, (3, y + 3))
            s.blit(host.text("TRK", 8, col["label"], mono=False), (3 + t.get_width() + 2, y + 2))
            if mem:
                s.blit(host.text("%d MEM" % mem, 8, col["dim"], mono=False), (3 + t.get_width() + 2, y + 9))
            self._over.append(pygame.Rect(0, y, 64, 17))
