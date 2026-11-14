"""
Scene renderer (pygame): the car, the floor, the road and the HUD.

The whole scene is drawn into one cached surface (``SceneRenderer.surface``)
that the main screen blits every frame. It is re-drawn only when something
on it changed, and at most every ``MIN_REDRAW_S`` while it animates, so a
parked car costs one opaque blit per UI frame.

Layers, back to front (each one a function, so new elements slot in):

    sky strip (pre-rendered per light condition)
    floor grid                     _draw_floor
    road: lane fill, lines, edges  _draw_road
    world objects, lead vehicle    _draw_objects   (far -> near)
    own car (cached sprite)        CarCache
    lamp glows (additive sprites)  _draw_glows
    HUD                            _draw_hud_drive / _draw_hud_parked

No anti-aliasing anywhere (analog MFD link): lines and text are 1-bit, the
only soft pixels are the stepped lamp halos.
"""

from __future__ import annotations

import math
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import pygame

from ..colors import COLORS
from ..fonts import get_mono_font, get_tiny_font
from .car import CarRenderer, car_palette
from .director import DOOR_STEPS, VIEW_STEPS, SceneDirector
from .inputs import DOOR_KEYS, SceneInputs
from .model import Camera

RGB = Tuple[int, int, int]

KEY = (255, 0, 255)            # colour key of cached car sprites
MIN_REDRAW_S = 0.095           # scene redraw cap while animating (~10 fps)
CAM_X = 0.4                    # roadsense camera ahead of the car centre (m)
CHASE_POS = (-8.0, 0.0, 3.6)   # chase camera
CHASE_TARGET = (12.0, 0.0, 0.2)
CHASE_F = 196.0
# Depth mapping of the chase view. True perspective squeezes 30..100 m into a
# few pixel rows under the horizon; instead the inverse depth (= screen
# height above the car) falls with log(distance), like a radar range scale:
# the road keeps 100 m on screen and a far lead vehicle stays >= 10 px wide.
COMP_X0 = 1.0                  # mapping starts here (world x, under the nose)
COMP_DFAR = 32.0               # displayed camera depth of the 100 m line
COMP_S = 1.55                  # log knee; chosen so the scale is 1:1 at X0
_COMP_DN = COMP_X0 - CHASE_POS[0]
_COMP_LN = math.log(1.0 + 100.0 / COMP_S)
NEAR = 0.4                     # near plane (m)
GAP_WARN_S = 1.5               # time gap colouring
GAP_CRIT_S = 0.8

DOOR_LABELS = ("DRIVER", "PASSENGER", "REAR L", "REAR R", "BACK")


def _k(c: RGB, k: float) -> RGB:
    return (int(c[0] * k), int(c[1] * k), int(c[2] * k))


def compress(x: float) -> float:
    """World x (m, car centre = 0) -> displayed world x in the chase view."""
    r = x - COMP_X0
    if r <= 0.0:
        return x
    if r > 180.0:
        r = 180.0
    u = 1.0 / _COMP_DN - (1.0 / _COMP_DN - 1.0 / COMP_DFAR) * math.log(1.0 + r / COMP_S) / _COMP_LN
    return CHASE_POS[0] + 1.0 / u


def make_glow(radius: int, color: RGB) -> pygame.Surface:
    """Stepped halo sprite for additive blits (3 rings + core)."""
    s = pygame.Surface((radius * 2, radius * 2))
    s.fill((0, 0, 0))
    c = (radius, radius)
    for frac, k in ((1.0, 0.07), (0.66, 0.18), (0.40, 0.45), (0.2, 1.0)):
        pygame.draw.circle(s, _k(color, k), c, max(1, int(radius * frac)))
    return s


class CarCache:
    """Cached sprites of the own car per (camera step, openings, lamps)."""

    MAX = 64

    def __init__(self, size: Tuple[int, int], renderer: CarRenderer,
                 cameras: Sequence[Camera]):
        self.size = size
        self.renderer = renderer
        self.cameras = cameras
        self._cache: Dict[tuple, Tuple[pygame.Surface, Tuple[int, int]]] = {}
        self.renders = 0

    def has(self, step: int, doors: Tuple[int, ...], lamps: Tuple[bool, bool, bool]) -> bool:
        return (step, doors, lamps) in self._cache

    def get(self, step: int, doors: Tuple[int, ...], lamps: Tuple[bool, bool, bool]):
        key = (step, doors, lamps)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        if len(self._cache) >= self.MAX:
            self._cache.clear()
        tmp = pygame.Surface(self.size)
        tmp.fill(KEY)
        tmp.set_colorkey(KEY)
        openings = {DOOR_KEYS[i]: doors[i] / DOOR_STEPS for i in range(len(DOOR_KEYS))}
        bb = self.renderer.draw(tmp, self.cameras[step], openings,
                                {"head": lamps[0], "tail": lamps[1], "brake": lamps[2]})
        rect = pygame.Rect(bb[0], bb[1], bb[2] - bb[0], bb[3] - bb[1]).clip(tmp.get_rect())
        if rect.width == 0 or rect.height == 0:
            rect = pygame.Rect(0, 0, 1, 1)
        spr = pygame.Surface(rect.size)
        spr.fill(KEY)
        spr.blit(tmp, (0, 0), rect)
        spr.set_colorkey(KEY, pygame.RLEACCEL)
        self.renders += 1
        hit = (spr, rect.topleft)
        self._cache[key] = hit
        return hit


class SceneRenderer:
    """Owns the scene surface; ``render()`` returns it, redrawn if needed."""

    def __init__(self, size: Tuple[int, int] = (238, 168),
                 colors: Optional[Mapping[str, RGB]] = None):
        self.w, self.h = size
        c = dict(colors or COLORS)
        self.pal: Dict[str, RGB] = {
            "bg": (0, 0, 0),
            "grid": (44, 34, 84),
            "grid_hi": (70, 52, 128),
            "lane_fill": (0, 24, 28),
            "lane_ego": c["cyan_bright"],
            "lane_mid": c["cyan_mid"],
            "lane_dim": (30, 78, 86),
            "edge": (150, 64, 180),
            "horizon": c["cyan_dim"],
            "ridge": (86, 60, 150),
            "text": c["text_highlight"],
            "value": c["cyan_bright"],
            "label": c["cyan_dim"],
            "dim": c["inactive"],
            "warn": c["amber"],
            "orange": c["orange"],
            "red": c["red_bright"],
            "green": c["green_bright"],
            "veh_body": (0, 30, 36),
            "veh_glass": (26, 22, 56),
            "tail": (220, 30, 34),
        }
        self.surface = pygame.Surface(size)
        self.surface.fill((0, 0, 0))
        cx = self.w / 2.0
        hero = self._orbit(52.0, 17.0, 11.0, 470.0, (0.0, 0.0, 0.5), cx, self.h / 2.0 + 12)
        chase = Camera(CHASE_POS, CHASE_TARGET, CHASE_F, cx, self.h / 2.0)
        self.cameras: List[Camera] = [
            Camera.lerp(hero, chase, i / VIEW_STEPS) for i in range(VIEW_STEPS + 1)]
        self.car = CarRenderer(car_palette(c))
        self.cache = CarCache(size, self.car, self.cameras)
        self._text: Dict[tuple, pygame.Surface] = {}
        self._glow: Dict[tuple, pygame.Surface] = {}
        self._sky: Dict[str, pygame.Surface] = {}
        # Chase-view fast path: that camera looks straight down the road, so
        # a ground point's screen row and scale depend on its distance only
        # and its column is linear in the lateral offset (no per-point
        # projection). Used whenever the view is fully open.
        cf, cu = chase._f, chase._u
        self._row_f0 = cf[0]
        self._row_fz = -CHASE_POS[2] * cf[2]
        self._row_u0 = cu[0]
        self._row_uz = -CHASE_POS[2] * cu[2]
        self._fast = False
        self._xs_tab: Dict[tuple, tuple] = {}
        self._sig: Optional[tuple] = None
        self._cen_xs: Sequence[float] = ()
        self._cen: List[float] = []
        self._cen_v = 0.0
        self._last_draw = -1.0
        self.redraws = 0

    @staticmethod
    def _orbit(az: float, el: float, dist: float, f: float, target, cx, cy) -> Camera:
        a, e = math.radians(az), math.radians(el)
        pos = (target[0] + dist * math.cos(e) * math.cos(a),
               target[1] + dist * math.cos(e) * math.sin(a),
               target[2] + dist * math.sin(e))
        return Camera(pos, target, f, cx, cy)

    # ── public ───────────────────────────────────────────────────────────────

    def render(self, d: SceneDirector, inp: SceneInputs, now: float) -> pygame.Surface:
        sig = self._signature(d, inp)
        if sig != self._sig or (d.animating and now - self._last_draw >= MIN_REDRAW_S):
            if sig == self._sig and now - self._last_draw < MIN_REDRAW_S:
                return self.surface
            self._sig = sig
            self._last_draw = now
            self._draw(d, inp)
            self.redraws += 1
        return self.surface

    def invalidate(self) -> None:
        self._sig = None

    @staticmethod
    def _lamps(inp: SceneInputs) -> Tuple[bool, bool, bool]:
        return (bool(inp.head), bool(inp.tail or inp.head), bool(inp.brake))

    def prewarm(self, inp: SceneInputs) -> bool:
        """Render ONE not-yet-cached car sprite of the opening/closing move
        (doors shut, current lamps, brake lamps off and on). Called at a slow
        rate while the scene is steady, so the first swing of the camera does
        not have to render a car per frame. Returns False when all are cached."""
        head, tail, brake = self._lamps(inp)
        closed = (0,) * len(DOOR_KEYS)
        for b in (brake, not brake):
            for step in range(VIEW_STEPS + 1):
                if not self.cache.has(step, closed, (head, tail, b)):
                    self.cache.get(step, closed, (head, tail, b))
                    return True
        return False

    def _signature(self, d: SceneDirector, inp: SceneInputs) -> tuple:
        blink = int(d.clock * 2) if d.vision in ("stale", "lost") or (
            inp.light is not None and inp.light.flashing) else 0
        return (d.view_step, d.door_step, d.vision, int(inp.speed_kmh + 0.5), inp.gear,
                inp.ready, inp.acc_on, inp.doors, inp.rear_group, inp.door_any, inp.head, inp.high, inp.tail, inp.fog,
                inp.brake, inp.limit_kmh, inp.light, inp.env_light, inp.ice_running,
                inp.cruise_active, inp.cruise_set_kmh, blink)

    # ── helpers ──────────────────────────────────────────────────────────────

    def text(self, s: str, size: int, color: RGB, mono: bool = True) -> pygame.Surface:
        key = (s, size, color, mono)
        t = self._text.get(key)
        if t is None:
            if len(self._text) > 160:
                self._text.clear()
            font = get_mono_font(size) if mono else get_tiny_font(size)
            t = font.render(s, False, color)
            self._text[key] = t
        return t

    def glow(self, radius: int, color: RGB) -> pygame.Surface:
        key = (radius, color)
        g = self._glow.get(key)
        if g is None:
            g = make_glow(radius, color)
            self._glow[key] = g
        return g

    def _add_glow(self, surf: pygame.Surface, pos: Tuple[int, int], radius: int, color: RGB) -> None:
        surf.blit(self.glow(radius, color), (pos[0] - radius, pos[1] - radius),
                  special_flags=pygame.BLEND_RGB_ADD)

    def _wx(self, x: float, v: float) -> float:
        """World x with the chase view's depth compression blended in by v."""
        if x <= COMP_X0 or v <= 0.0:
            return x
        if x > 180.0:
            x = 180.0
        return x + (compress(x) - x) * v

    def _seg(self, cam: Camera, x0: float, y0: float, x1: float, y1: float):
        """Ground segment -> screen segment, clipped at the near plane."""
        f, p = cam._f, cam.pos
        z0 = (x0 - p[0]) * f[0] + (y0 - p[1]) * f[1] - p[2] * f[2]
        z1 = (x1 - p[0]) * f[0] + (y1 - p[1]) * f[1] - p[2] * f[2]
        if z0 < NEAR and z1 < NEAR:
            return None
        if z0 < NEAR:
            t = (NEAR - z0) / (z1 - z0)
            x0, y0 = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
        elif z1 < NEAR:
            t = (NEAR - z1) / (z0 - z1)
            x1, y1 = x1 + (x0 - x1) * t, y1 + (y0 - y1) * t
        a = cam.project((x0, y0, 0.0))
        b = cam.project((x1, y1, 0.0))
        lim = 3000.0
        return ((max(-lim, min(lim, a[0])), max(-lim, min(lim, a[1]))),
                (max(-lim, min(lim, b[0])), max(-lim, min(lim, b[1]))))

    def _row(self, wx: float) -> Tuple[float, float]:
        """Chase camera: ground row at world x -> (screen y, pixels per metre)."""
        dx = wx - CHASE_POS[0]
        k = CHASE_F / (dx * self._row_f0 + self._row_fz)
        return (self.h / 2.0 - k * (dx * self._row_u0 + self._row_uz), k)

    def _xs_table(self, xs: Sequence[float]) -> tuple:
        """Per sample distance: (screen y, px/m, 1 - depth ratio), plus the
        run-in row behind the camera. Cached per distance grid."""
        key = tuple(xs)
        tab = self._xs_tab.get(key)
        if tab is None:
            if len(self._xs_tab) > 8:
                self._xs_tab.clear()
            rows = []
            for x in xs:
                wx = CAM_X + x
                shown = compress(wx)
                sy, k = self._row(shown)
                real = wx - CHASE_POS[0]
                q = 0.0 if wx <= COMP_X0 else 1.0 - (shown - CHASE_POS[0]) / real
                rows.append((sy, k, q))
            tab = (tuple(rows), self._row(CAM_X - 5.5))
            self._xs_tab[key] = tab
        return tab

    def _set_centre(self, d: SceneDirector, v: float) -> None:
        """Road centre line (lateral, per sample distance) for ``_lat``."""
        xs = d.xs
        left, right = d.lanes[1], d.lanes[2]
        cen: List[float] = []
        for i in range(len(xs)):
            lv = left[i] if left and i < len(left) else None
            rv = right[i] if right and i < len(right) else None
            if lv is not None and rv is not None:
                cen.append((lv + rv) * 0.5)
            elif lv is not None:
                cen.append(lv - 1.75)
            elif rv is not None:
                cen.append(rv + 1.75)
            else:
                cen.append(0.0)
        self._cen_xs = xs
        self._cen = cen
        self._cen_v = v

    def _lat(self, x: float, y: float) -> float:
        """Displayed lateral position of a point ``x`` m ahead of the camera.

        The depth mapping pulls far points closer; a lateral offset drawn
        unchanged at the pulled-in depth would exaggerate every curve. So the
        road CENTRE is scaled back to where true perspective puts it, while
        offsets from the centre (lane width, a vehicle's place in its lane)
        keep the displayed scale."""
        v = self._cen_v
        if v <= 0.0 or not self._cen:
            return y
        wx = CAM_X + x
        if wx <= COMP_X0:
            return y
        c = self._interp(self._cen_xs, self._cen, x)
        real = wx - CHASE_POS[0]
        shown = compress(min(wx, 180.0)) - CHASE_POS[0]
        ratio = shown / real
        return y - c * (1.0 - ratio) * v

    def _ground(self, cam: Camera, x: float, y: float) -> Optional[Tuple[float, float, float]]:
        f, p = cam._f, cam.pos
        z = (x - p[0]) * f[0] + (y - p[1]) * f[1] - p[2] * f[2]
        if z < NEAR:
            return None
        return cam.project((x, y, 0.0))

    # ── drawing ──────────────────────────────────────────────────────────────

    def _draw(self, d: SceneDirector, inp: SceneInputs) -> None:
        s = self.surface
        s.fill(self.pal["bg"])
        cam = self.cameras[d.view_step]
        v = d.view_step / VIEW_STEPS
        road_k = max(0.0, min(1.0, (v - 0.45) / 0.4))       # road/HUD fade-in

        self._fast = d.view_step == VIEW_STEPS
        self._draw_sky(s, cam, inp)
        road = road_k > 0.0 and d.vision in ("live", "stale")
        if road:
            # lane fill under the floor lines: they double as speed stripes
            self._set_centre(d, v)
            lines = self._lane_points(cam, d, v)
            self._draw_lane_fill(s, lines, d, road_k)
            self._draw_floor(s, cam, d, v)
            self._draw_road(s, cam, d, v, road_k, lines)
            self._draw_objects(s, cam, d, inp, v, road_k)
        else:
            self._cen = []
            self._draw_floor(s, cam, d, v)

        spr, pos = self.cache.get(d.view_step, d.door_step, self._lamps(inp))
        s.blit(spr, pos)
        self._draw_glows(s, cam, inp)

        if v >= 0.5:
            self._draw_hud_drive(s, d, inp)
        else:
            self._draw_hud_parked(s, d, inp)

    # -- sky ---------------------------------------------------------------------

    SKY_H = 64

    def _sky_strip(self, light: Optional[str]) -> pygame.Surface:
        key = light or "none"
        strip = self._sky.get(key)
        if strip is not None:
            return strip
        w, h = self.w, self.SKY_H
        strip = pygame.Surface((w, h))
        strip.fill((0, 0, 0))
        cx = w // 2
        if key == "night":
            # fixed star field + crescent
            seed = 12345
            for _ in range(46):
                seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
                x = seed % w
                seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
                y = seed % (h - 14)
                c = self.pal["label"] if seed & 0x300 else self.pal["text"]
                strip.set_at((x, y), c)
            mx, my = cx + 6, 17
            pygame.draw.circle(strip, self.pal["warn"], (mx, my), 7)
            pygame.draw.circle(strip, (0, 0, 0), (mx + 4, my - 2), 7)
        elif key in ("dusk_dawn", "day"):
            # striped sun: half set at dusk, clear of the horizon by day
            r = 23 if key == "dusk_dawn" else 15
            cy = h - 2 if key == "dusk_dawn" else h - 30
            top, bot = self.pal["warn"], (232, 96, 96)
            for yy in range(cy - r, min(h, cy + r + 1)):
                dy = yy - cy
                half = int(math.sqrt(max(0, r * r - dy * dy)))
                t = (yy - (cy - r)) / float(2 * r)
                if key == "dusk_dawn":
                    # gaps grow towards the horizon
                    band = (yy - (cy - r)) % 5
                    if t > 0.28 and band >= 5 - int(1 + t * 3.2):
                        continue
                col = (int(top[0] + (bot[0] - top[0]) * t), int(top[1] + (bot[1] - top[1]) * t),
                       int(top[2] + (bot[2] - top[2]) * t))
                pygame.draw.line(strip, col, (cx - half, yy), (cx + half, yy))
        # far ridge (decoration; black fill hides the sun behind it)
        pts = [(0, h)]
        heights = (7, 12, 6, 15, 9, 4, 3, 5, 3, 4, 8, 14, 7, 11, 5)
        step = w / float(len(heights) - 1)
        for i, hh in enumerate(heights):
            pts.append((int(i * step), h - 1 - hh))
        pts.append((w, h))
        pygame.draw.polygon(strip, (0, 0, 0), pts)
        pygame.draw.lines(strip, self.pal["ridge"], False, pts[1:-1])
        pygame.draw.line(strip, self.pal["horizon"], (0, h - 1), (w, h - 1))
        self._sky[key] = strip
        return strip

    def _draw_sky(self, s: pygame.Surface, cam: Camera, inp: SceneInputs) -> None:
        hy = int(cam.horizon_y())
        if hy < -4 or hy > self.h:
            return
        s.blit(self._sky_strip(inp.env_light), (0, hy - self.SKY_H + 1))

    # -- floor -------------------------------------------------------------------

    def _draw_floor(self, s: pygame.Surface, cam: Camera, d: SceneDirector, v: float) -> None:
        col = self.pal["grid"]
        line = pygame.draw.line
        seg = self._seg
        # a pad under the parked car that opens into the road ahead
        x_back = -5.0 - v
        x_front = 5.0 + 55.0 * v
        half_w = 3.0 + 13.0 * v
        road = d.vision in ("live", "stale") and v > 0.6
        if self._fast:
            w = self.w
            cx = w / 2.0
            row = self._row
            if not road:
                y0, k0 = row(x_back)
                y1, k1 = row(compress(100.0))
                for i in range(-4, 5):
                    y = i * 4.0
                    line(s, col, (cx - k0 * y, y0), (cx - k1 * y, y1))
            x = math.floor(x_back / 10.0) * 10.0 - (d.odo % 10.0) + 10.0
            while x <= x_front:
                sy = row(compress(x))[0]
                line(s, col, (0, sy), (w, sy))
                x += 10.0
            return
        # longitudinal lines (not over a live road: the lanes replace them).
        # 4 m pitch with one line under the car: a floor, never a fake lane.
        if not road:
            xf = self._wx(6.0 + 94.0 * v, v)
            pitch = 1.0 if v < 0.25 else 2.0 if v < 0.5 else 4.0
            n = int(half_w // pitch)
            for i in range(-n, n + 1):
                y = i * pitch
                p = seg(cam, x_back, y, xf, y)
                if p:
                    line(s, col, p[0], p[1])
        # transverse lines, scrolling with the distance travelled
        spacing = 1.0 if v < 0.25 else 2.0 if v < 0.5 else 10.0
        x = math.floor(x_back / spacing) * spacing - (d.odo % spacing) + spacing
        while x <= x_front:
            wx = self._wx(x, v)
            p = seg(cam, wx, -half_w, wx, half_w)
            if p:
                line(s, col, p[0], p[1])
            x += spacing

    # -- road --------------------------------------------------------------------

    def _line_points(self, cam: Camera, xs: Sequence[float], ys: Sequence[float],
                     v: float) -> List[Tuple[float, float]]:
        pts: List[Tuple[float, float]] = []
        n = min(len(xs), len(ys))
        if n < 2:
            return pts
        # short run-in behind the camera position so lines reach the bottom
        slope = (ys[1] - ys[0]) / ((xs[1] - xs[0]) or 1.0)
        back = -5.5
        if self._fast:
            rows, rb = self._xs_table(xs)
            cen = self._cen
            cx = self.w / 2.0
            pts.append((cx - rb[1] * (ys[0] + slope * back), rb[0]))
            for i in range(n):
                sy, k, q = rows[i]
                pts.append((cx - k * (ys[i] - cen[i] * q), sy))
            return pts
        g = self._ground(cam, CAM_X + back, ys[0] + slope * back)
        if g is not None:
            pts.append((g[0], g[1]))
        for i in range(n):
            g = self._ground(cam, self._wx(CAM_X + xs[i], v), self._lat(xs[i], ys[i]))
            if g is not None:
                pts.append((g[0], g[1]))
        return pts

    def _lane_points(self, cam: Camera, d: SceneDirector, v: float) -> list:
        return [self._line_points(cam, d.xs, ys, v) if ys else [] for ys in d.lanes]

    def _draw_lane_fill(self, s: pygame.Surface, lines: list, d: SceneDirector, k: float) -> None:
        left, right = lines[1], lines[2]
        if len(left) >= 2 and len(right) >= 2 and d.vision != "stale":
            pygame.draw.polygon(s, _k(self.pal["lane_fill"], k), left + right[::-1])

    def _draw_road(self, s: pygame.Surface, cam: Camera, d: SceneDirector,
                   v: float, k: float, lines: list) -> None:
        pal = self.pal
        stale = d.vision == "stale"
        xs = d.xs
        for i, ed in enumerate(d.edges):
            if not ed:
                continue
            pts = self._line_points(cam, xs, ed, v)
            if len(pts) >= 2:
                pygame.draw.lines(s, _k(pal["edge"], k * (0.5 if stale else 1.0)), False, pts)
        for i in (0, 3, 1, 2):
            pts = lines[i]
            if len(pts) < 2:
                continue
            if stale:
                col = pal["lane_dim"]
            elif i in (1, 2):
                col = pal["lane_ego"] if d.lane_prob[i] >= 0.6 else pal["lane_mid"]
            else:
                col = pal["lane_mid"] if d.lane_prob[i] >= 0.6 else pal["lane_dim"]
            pygame.draw.lines(s, _k(col, k), False, pts, 2 if (i in (1, 2) and not stale) else 1)

    @staticmethod
    def _interp(xs: Sequence[float], ys: Optional[Sequence[float]], x: float) -> float:
        if not ys:
            return 0.0
        n = min(len(xs), len(ys))
        for i in range(1, n):
            if x <= xs[i]:
                t = (x - xs[i - 1]) / ((xs[i] - xs[i - 1]) or 1.0)
                return ys[i - 1] + (ys[i] - ys[i - 1]) * t
        return ys[n - 1]

    # -- objects -----------------------------------------------------------------

    def _draw_objects(self, s: pygame.Surface, cam: Camera, d: SceneDirector,
                      inp: SceneInputs, v: float, k: float) -> None:
        items = []     # (distance, draw function)
        for o in d.objects:
            items.append((o.x, lambda o=o: self._draw_object(s, cam, o, v, k)))
        lt = inp.light
        if lt is not None and lt.dist_m is not None and d.vision == "live":
            items.append((lt.dist_m, lambda: self._draw_light_world(s, cam, lt, d, v)))
        if d.lead is not None and d.lead_alpha > 0.0:
            items.append((d.lead.dist_m, lambda: self._draw_lead(s, cam, d, inp, v, k)))
        items.sort(key=lambda t: -t[0])
        for _, fn in items:
            fn()

    def _place(self, cam: Camera, x: float, y: float, v: float):
        """Ground point (metres ahead of the roadsense camera, left+) ->
        (screen x, screen y, pixels per metre) or None."""
        if self._fast:
            wx = CAM_X + x
            if wx <= CHASE_POS[0] + 2.0:
                return None
            shown = compress(wx)
            sy, k = self._row(shown)
            if wx > COMP_X0 and self._cen:
                q = 1.0 - (shown - CHASE_POS[0]) / (wx - CHASE_POS[0])
                y -= self._interp(self._cen_xs, self._cen, x) * q
            return self.w / 2.0 - k * y, sy, k
        g = self._ground(cam, self._wx(CAM_X + x, v), self._lat(x, y))
        if g is None:
            return None
        return g[0], g[1], cam.f / g[2]

    def draw_vehicle_rear(self, s: pygame.Surface, cx: float, by: float, w: float,
                          line: RGB, kind: str = "car", k: float = 1.0) -> pygame.Rect:
        """A vehicle seen from behind, ``w`` pixels wide, standing on ``by``."""
        pal = self.pal
        w = max(7.0, w)
        tall = kind in ("truck", "bus")
        h = w * (1.45 if tall else 0.80)
        x0, x1 = cx - w / 2.0, cx + w / 2.0
        top = by - h
        tyre_h = max(1.0, h * (0.10 if tall else 0.15))
        rect = pygame.Rect(int(x0), int(top), int(w) + 1, int(h) + 1)
        polygon = pygame.draw.polygon
        # tyres
        tw = max(2, int(w * 0.17))
        for tx in (x0 + w * 0.05, x1 - w * 0.05 - tw):
            pygame.draw.rect(s, (10, 12, 16), (int(tx), int(by - tyre_h * 1.6), tw, int(tyre_h * 1.6) + 1))
            pygame.draw.rect(s, _k(pal["lane_dim"], k), (int(tx), int(by - tyre_h * 1.6), tw, int(tyre_h * 1.6) + 1), 1)
        sh = 0.0 if tall else w * 0.13      # shoulder inset of the roof
        body = [(x0, by - tyre_h), (x0, by - h * 0.56), (x0 + sh, top),
                (x1 - sh, top), (x1, by - h * 0.56), (x1, by - tyre_h)]
        polygon(s, _k(pal["veh_body"], k), body)
        if w >= 12 and not tall:
            g0, g1 = top + h * 0.09, by - h * 0.58
            glass = [(x0 + sh + w * 0.05, g0), (x1 - sh - w * 0.05, g0),
                     (x1 - w * 0.07, g1), (x0 + w * 0.07, g1)]
            polygon(s, _k(pal["veh_glass"], k), glass)
            polygon(s, _k(pal["lane_dim"], k), glass, 1)
        elif tall and w >= 12:
            pygame.draw.line(s, _k(pal["lane_dim"], k), (cx, top + 2), (cx, by - tyre_h - 2))
        polygon(s, _k(line, k), body, 1)
        # tail lamps
        lw = max(2, int(w * 0.20))
        lh = max(1, int(h * 0.13))
        ly = int(by - h * (0.30 if tall else 0.50))
        red = _k(pal["tail"], k)
        pygame.draw.rect(s, red, (int(x0) + 1, ly, lw, lh))
        pygame.draw.rect(s, red, (int(x1) - lw, ly, lw, lh))
        if w >= 26:
            pw = int(w * 0.24)
            pygame.draw.rect(s, _k(pal["lane_dim"], k),
                             (int(cx - pw / 2), int(by - h * 0.34), pw, max(2, int(h * 0.12))), 1)
        return rect

    def gap_color(self, gap_s: Optional[float]) -> RGB:
        if gap_s is None or gap_s >= GAP_WARN_S:
            return self.pal["value"]
        return self.pal["red"] if gap_s < GAP_CRIT_S else self.pal["warn"]

    def _draw_lead(self, s: pygame.Surface, cam: Camera, d: SceneDirector,
                   inp: SceneInputs, v: float, k: float) -> None:
        lead = d.lead
        if lead is None:
            return
        g = self._place(cam, lead.dist_m, lead.lat_m, v)
        if g is None:
            return
        cx, by, ppm = g
        a = d.lead_alpha * k
        gap = d.time_gap_s(inp.real_speed_kmh)
        col = self.gap_color(gap)
        w = max(9.0, 1.8 * ppm)
        rect = self.draw_vehicle_rear(s, cx, by, w, col, lead.kind, a)
        if a < 0.6:
            return
        # target brackets
        r = rect.inflate(6, 6)
        t = 3
        line = pygame.draw.line
        for (px, py, dx, dy) in ((r.left, r.top, 1, 1), (r.right - 1, r.top, -1, 1),
                                 (r.left, r.bottom - 1, 1, -1), (r.right - 1, r.bottom - 1, -1, -1)):
            line(s, col, (px, py), (px + dx * t, py))
            line(s, col, (px, py), (px, py + dy * t))
        # distance read-out beside the vehicle (left of it when it sits right)
        num = self.text("%d" % int(lead.dist_m + 0.5), 16, self.pal["text"])
        unit = self.text("m", 8, col, mono=False)
        tw = num.get_width() + 2 + unit.get_width()
        right_side = cx <= self.w * 0.60
        tx = r.right + 5 if right_side else r.left - 5 - tw
        tx = max(2, min(self.w - tw - 2, tx))
        ty = max(2, min(self.h - 30, r.centery - 9))
        ax = r.right if right_side else r.left - 1
        line(s, col, (ax, r.centery), (tx - 2 if right_side else tx + tw + 1, r.centery))
        pygame.draw.rect(s, (0, 0, 0), (tx - 1, ty, tw + 2, 16))
        s.blit(num, (tx, ty))
        s.blit(unit, (tx + num.get_width() + 2, ty + 7))
        # second line: time gap, and a closing / opening marker
        sx = tx
        if gap is not None:
            st = self.text("%.1fs" % gap, 8, col, mono=False)
            pygame.draw.rect(s, (0, 0, 0), (tx - 1, ty + 16, st.get_width() + 2, 9))
            s.blit(st, (tx, ty + 17))
            sx = tx + st.get_width() + 4
        if lead.v_rel is not None and abs(lead.v_rel) >= 0.8:
            pygame.draw.rect(s, (0, 0, 0), (sx - 1, ty + 16, 9, 9))
            if lead.v_rel < 0:      # closing: arrow towards us
                pygame.draw.polygon(s, self.pal["warn"], [(sx, ty + 18), (sx + 6, ty + 18), (sx + 3, ty + 23)])
            else:                   # pulling away
                pygame.draw.polygon(s, self.pal["green"], [(sx, ty + 23), (sx + 6, ty + 23), (sx + 3, ty + 18)])

    def _draw_object(self, s: pygame.Surface, cam: Camera, o, v: float, k: float) -> None:
        g = self._place(cam, o.x, o.y, v)
        if g is None:
            return
        cx, by, ppm = g
        pal = self.pal
        if o.kind == "lamp":
            # street lamp: mast with an arm towards the road
            h = 4.6 * ppm
            arm = (-1.0 if o.y > 0 else 1.0) * 1.3 * ppm
            col = _k(pal["lane_dim"], k)
            pygame.draw.line(s, col, (cx, by), (cx, by - h))
            pygame.draw.line(s, col, (cx, by - h), (cx + arm, by - h))
            head = (int(cx + arm), int(by - h))
            if o.state != "off":
                self._add_glow(s, head, max(3, int(1.1 * ppm) + 2), pal["orange"])
            else:
                pygame.draw.rect(s, col, (head[0] - 1, head[1], 2, 2))
        elif o.kind in ("car", "truck", "bus", "van"):
            self.draw_vehicle_rear(s, cx, by, max(7.0, 1.8 * ppm), pal["lane_mid"], o.kind, k)
        elif o.kind == "cone":
            hh = max(3.0, 0.8 * ppm)
            pygame.draw.polygon(s, _k(pal["orange"], k),
                                [(cx - hh * 0.4, by), (cx + hh * 0.4, by), (cx, by - hh)])
        elif o.kind in ("person", "bike"):
            hh = max(5.0, 1.75 * ppm)
            col = _k(pal["warn"], k)
            pygame.draw.line(s, col, (cx, by), (cx, by - hh * 0.75))
            pygame.draw.circle(s, col, (int(cx), int(by - hh * 0.86)), max(1, int(hh * 0.14)))
        else:
            hh = max(3.0, 1.0 * ppm)
            pygame.draw.rect(s, _k(pal["lane_dim"], k), (int(cx - hh / 2), int(by - hh), int(hh), int(hh)), 1)

    LIGHT_COLORS = {"red": (0,), "yellow": (1,), "red_yellow": (0, 1), "green": (2,)}

    def _lamp_colors(self):
        return (self.pal["red"], self.pal["warn"], self.pal["green"])

    def _draw_signal(self, s: pygame.Surface, x: int, y: int, r: int, lt, clock: float) -> None:
        """Three-lamp signal head, top-left at (x, y), lamp radius r."""
        d = 2 * r + 1
        w, h = d + 4, 3 * d + 6
        pygame.draw.rect(s, (0, 0, 0), (x, y, w, h))
        pygame.draw.rect(s, self.pal["dim"], (x, y, w, h), 1)
        on = self.LIGHT_COLORS.get(lt.state, ())
        if lt.flashing and int(clock * 2) % 2:
            on = ()
        cols = self._lamp_colors()
        for i in range(3):
            c = (x + w // 2, y + 3 + r + i * (d + 1))
            if i in on:
                pygame.draw.circle(s, cols[i], c, r)
                self._add_glow(s, c, r + 4, cols[i])
            else:
                pygame.draw.circle(s, _k(cols[i], 0.22), c, r, 1)

    def _draw_light_world(self, s: pygame.Surface, cam: Camera, lt, d: SceneDirector, v: float) -> None:
        y = lt.lat_m if lt.lat_m is not None else -4.5
        g = self._place(cam, max(6.0, min(100.0, lt.dist_m)), y, v)
        if g is None:
            return
        cx, by, ppm = g
        mast = max(12.0, 2.6 * ppm)
        r = 1 if ppm < 9 else 2
        pygame.draw.line(s, self.pal["lane_dim"], (cx, by), (cx, by - mast))
        dd = 2 * r + 1
        self._draw_signal(s, int(cx) - (dd + 4) // 2, int(by - mast) - (3 * dd + 6), r, lt, d.clock)

    # -- glows -------------------------------------------------------------------

    def _draw_glows(self, s: pygame.Surface, cam: Camera, inp: SceneInputs) -> None:
        if not (inp.head or inp.tail or inp.brake):
            return
        pts = self.car.lamp_points(cam)
        scale = cam.f / max(1.0, math.dist(cam.pos, (0.0, 0.0, 0.6)))
        r = max(4, int(scale * 0.30))
        if inp.head:
            for n in ("head_l", "head_r"):
                if n in pts:
                    self._add_glow(s, pts[n], r + 3, self.pal["warn"])
        if inp.brake:
            for n in ("tail_l", "tail_r"):
                if n in pts:
                    self._add_glow(s, pts[n], r + 3, self.pal["red"])
        elif inp.tail or inp.head:
            for n in ("tail_l", "tail_r"):
                if n in pts:
                    self._add_glow(s, pts[n], r, (170, 20, 24))

    # -- HUD ---------------------------------------------------------------------

    def _chip(self, s: pygame.Surface, x: int, y: int, label: str, color: RGB,
              filled: bool = False) -> int:
        """Small label box; returns its width."""
        t = self.text(label, 8, (0, 0, 0) if filled else color, mono=False)
        w = t.get_width() + 6
        if filled:
            pygame.draw.rect(s, color, (x, y, w, 11))
        else:
            pygame.draw.rect(s, (0, 0, 0), (x, y, w, 11))
            pygame.draw.rect(s, color, (x, y, w, 11), 1)
        s.blit(t, (x + 3, y + 2))
        return w

    def _draw_gear(self, s: pygame.Surface, x: int, y: int, gear: str) -> None:
        g = self.text(gear, 16, self.pal["value"])
        pygame.draw.rect(s, COLORS["cyan_dark"], (x, y, 15, 19))
        pygame.draw.rect(s, self.pal["lane_mid"], (x, y, 15, 19), 1)
        s.blit(g, (x + (15 - g.get_width()) // 2 + 1, y + 2))

    def _draw_limit(self, s: pygame.Surface, cx: int, cy: int, kmh: int, over: bool) -> None:
        """European limit sign as a neon ring."""
        r = 13
        pygame.draw.circle(s, (0, 0, 0), (cx, cy), r)
        pygame.draw.circle(s, self.pal["red"], (cx, cy), r, 3)
        txt = self.text(str(kmh), 14, self.pal["warn"] if over else self.pal["text"])
        s.blit(txt, (cx - txt.get_width() // 2 + (1 if kmh < 100 else 0), cy - txt.get_height() // 2))

    def _draw_hud_drive(self, s: pygame.Surface, d: SceneDirector, inp: SceneInputs) -> None:
        pal = self.pal
        # speed + gear, top-left
        spd = self.text("%d" % int(inp.speed_kmh + 0.5), 28, pal["text"])
        s.blit(spd, (5, 1))
        unit = self.text("km/h", 8, pal["label"], mono=False)
        s.blit(unit, (5 + spd.get_width() + 3, 6))
        gx = 5 + spd.get_width() + 3
        g = self.text(inp.gear, 14, pal["value"])
        s.blit(g, (gx + 1, 14))
        # speed limit + traffic light, top-right (the corner dot is the link LED)
        x_right = self.w - 20
        if inp.limit_kmh:
            over = inp.speed_kmh > inp.limit_kmh + 3
            self._draw_limit(s, x_right - 14, 16, inp.limit_kmh, over)
            x_right -= 32
        if inp.light is not None and inp.light.state in self.LIGHT_COLORS:
            self._draw_signal(s, x_right - 12, 3, 3, inp.light, d.clock)
            if inp.light.dist_m is not None:
                t = self.text("%dm" % int(inp.light.dist_m + 0.5), 8, pal["label"], mono=False)
                s.blit(t, (x_right - 12 - t.get_width() - 3, 5))
        # perception status, bottom-left (nothing when live)
        tag = None
        if d.vision == "off":
            tag = ("VISION OFF", pal["dim"])
        elif d.vision == "wait":
            tag = ("NO ROAD DATA", pal["dim"])
        elif d.vision == "lost":
            tag = ("NO SIGNAL", pal["warn"] if int(d.clock * 2) % 2 else pal["dim"])
        elif d.vision == "stale":
            tag = ("SIGNAL LOST", pal["warn"] if int(d.clock * 2) % 2 else pal["orange"])
        if tag:
            self._chip(s, 4, self.h - 14, tag[0], tag[1])

    def _draw_hud_parked(self, s: pygame.Surface, d: SceneDirector, inp: SceneInputs) -> None:
        pal = self.pal
        self._draw_gear(s, 5, 4, inp.gear)
        if inp.ready:
            self._chip(s, 24, 4, "READY", pal["green"], filled=True)
        elif inp.acc_on:
            self._chip(s, 24, 4, "ACC", pal["value"])
        else:
            self._chip(s, 24, 4, "OFF", pal["dim"])
        if inp.ice_running:
            self._chip(s, 24, 16, "ICE", pal["orange"])
        if inp.speed_kmh >= 1.0:
            t = self.text("%d" % int(inp.speed_kmh + 0.5), 16, pal["text"])
            s.blit(t, (66, 3))
            s.blit(self.text("km/h", 8, pal["label"], mono=False), (68 + t.get_width(), 9))
        # lamps that are on, top-right (left of the link LED)
        x = self.w - 20
        for on, label, col in ((inp.fog, "FOG", pal["green"]), (inp.high, "HIGH", (66, 165, 245)),
                               (inp.head, "LOW", pal["warn"]), (inp.tail, "TAIL", pal["red"]),
                               (inp.brake, "BRAKE", pal["red"])):
            if on:
                t = self.text(label, 8, col, mono=False)
                x -= t.get_width() + 6 + 2
                self._chip(s, x, 4, label, col)
        # doors, bottom
        opened = [DOOR_LABELS[i] for i, v in enumerate(inp.doors) if v]
        if inp.rear_group:
            opened.append("REAR/BACK")      # one shared switch bit on this car
        if not opened and inp.door_any:
            opened.append("DOOR")
        if opened:
            w = self._chip(s, 4, self.h - 14, "OPEN", pal["warn"], filled=True)
            t = self.text(" ".join(opened), 8, pal["warn"], mono=False)
            s.blit(t, (4 + w + 4, self.h - 12))
        elif inp.doors_known:
            s.blit(self.text("DOORS CLOSED", 8, pal["label"], mono=False), (5, self.h - 12))
        tag = self.text("NHW20", 8, pal["label"], mono=False)
        s.blit(tag, (self.w - tag.get_width() - 5, self.h - 12))
