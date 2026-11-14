"""
Scene renderer (pygame): the car, the floor, the road, the scenery and the HUD.

The whole scene is drawn into one cached surface (``SceneRenderer.surface``)
that the main screen blits every frame. It is re-drawn only when something
on it changed, and at most every ``MIN_REDRAW_S`` while it animates, so a
parked car costs one opaque blit per UI frame.

Layers, back to front (each one a function, so new elements slot in):

    backdrop strip by place/light/weather   _draw_sky        (scenery.py)
    lane fill, floor grid                   _draw_lane_fill, _draw_floor
    tunnel / car-park frames                Scenery.draw_enclosure
    headlamp beams on the road              _draw_beams
    planned path, lane lines, road edges    _draw_path, _draw_road
    roadside scenery by place               _draw_roadside
    fog band                                Scenery.draw_fog
    world objects, lead vehicle             _draw_objects    (far -> near)
    own car (cached sprite), lamp halos     CarCache, _draw_glows
    rain / snow                             Scenery.draw_precipitation
    HUD: speed, limit, signal, sign columns _draw_hud_drive / _draw_hud_parked

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
from .scenery import ENCLOSED, PAN, Scenery, conifer
from .signs import sign_surface
from .tuning import Tuning

RGB = Tuple[int, int, int]

KEY = (255, 0, 255)            # colour key of cached car sprites
MIN_REDRAW_S = 0.095           # scene redraw cap while animating (~10 fps)
CAM_X = 0.4                    # roadsense camera ahead of the car centre (m)
CHASE_POS = (-8.5, 0.0, 5.2)   # chase camera
CHASE_TARGET = (10.0, 0.0, 0.0)
CHASE_F = 196.0
# Depth mapping of the chase view. True perspective squeezes 30..100 m into a
# few pixel rows under the horizon; instead the inverse depth (= screen
# height above the car) falls with log(distance), like a radar range scale:
# the road keeps 100 m on screen and a far lead vehicle stays >= 10 px wide.
COMP_X0 = 1.0                  # mapping starts here (world x, under the nose)
COMP_DFAR = 48.0               # displayed camera depth of the 100 m line
COMP_S = 1.92                  # log knee; chosen so the scale is 1:1 at X0
_COMP_DN = COMP_X0 - CHASE_POS[0]
_COMP_LN = math.log(1.0 + 100.0 / COMP_S)
NEAR = 0.4                     # near plane (m)
GAP_WARN_S = 1.5               # time gap colouring
GAP_CRIT_S = 0.8

DOOR_LABELS = ("DRIVER", "PASS", "RL", "RR", "BACK")

CHASE_CY_OFFSET = 14           # chase camera centre below the scene centre (px)
SKY_H = 68                     # backdrop strip height (px above the horizon)
LEAD_BRAKING = -1.5            # lead acceleration (m/s2) shown as stop lamps
SIGN_PITCH = 21

# Headlamp beams on the road: reach (m ahead of the nose), parked-view reach,
# half-width at the lamps, spread per metre, widest half-width, colours.
BEAMS = {
    "fog": (13.0, 2.4, 0.9, 0.50, 6.2, (56, 96, 44), (104, 170, 80)),
    "low": (46.0, 3.4, 0.8, 0.05, 2.7, (98, 86, 40), (190, 170, 90)),
    "high": (112.0, 6.0, 0.8, 0.045, 4.6, (150, 140, 96), (236, 226, 170)),
}
LOW_BEAM_NEAR_SIDE = 0.70      # the low beam's cut-off: the left half reaches this share


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

    def __init__(self, size: Tuple[int, int] = (238, 216),
                 colors: Optional[Mapping[str, RGB]] = None,
                 tuning: Optional[Tuning] = None):
        self.tune = tune = tuning or Tuning()
        self.w, self.h = size
        c = dict(colors or COLORS)
        self.pal: Dict[str, RGB] = {
            "bg": (0, 0, 0),
            "grid": (44, 34, 84),
            "grid_hi": (70, 52, 128),
            "lane_fill": (0, 24, 28),
            "lane_side": (8, 14, 30),
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
            "path": (0, 108, 98),
            "tree": (0, 132, 84),
        }
        self.surface = pygame.Surface(size)
        self.surface.fill((0, 0, 0))
        cx = self.w / 2.0
        hero = self._orbit(52.0, 17.0, 11.0, 470.0, (0.0, 0.0, 0.5), cx, self.h / 2.0 + 12)
        self._chase_cy = self.h / 2.0 + CHASE_CY_OFFSET
        chase = Camera(CHASE_POS, CHASE_TARGET, CHASE_F, cx, self._chase_cy)
        self.horizon_y = int(chase.horizon_y())
        self.scenery = Scenery(self.w, SKY_H, self.pal["horizon"], tune.scenery_brightness,
                               tune.scenery_band_px, tune.weather_density, tune.weather_brightness)
        # tiers (tuning.py): the floor, the edges and the enclosure frames
        self._grid_road = _k(self.pal["grid"], tune.grid_brightness)
        self._grid_pad = _k(self.pal["grid"], tune.pad_brightness)
        self.pal["edge"] = _k(self.pal["edge"], tune.edge_brightness)
        self.pal["grid_hi"] = _k(self.pal["grid_hi"], tune.enclosure_brightness)
        self.cameras: List[Camera] = [
            Camera.lerp(hero, chase, i / VIEW_STEPS) for i in range(VIEW_STEPS + 1)]
        self.car = CarRenderer(car_palette(c))
        self.cache = CarCache(size, self.car, self.cameras)
        self._text: Dict[tuple, pygame.Surface] = {}
        self._glow: Dict[tuple, pygame.Surface] = {}
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
        self._beam_sprites: Dict[str, tuple] = {}
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
        return (bool(inp.head or inp.high), bool(inp.tail or inp.head or inp.high),
                bool(inp.brake))

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
        # rain and snow keep falling while the road view is open
        fall = int(d.clock * 10) if (d.view_step == VIEW_STEPS
                                     and inp.env_weather in ("rain", "snow")) else 0
        return (d.view_step, d.door_step, d.vision, int(inp.speed_kmh + 0.5), inp.gear,
                inp.ready, inp.acc_on, inp.doors, inp.rear_group, inp.door_any, inp.head,
                inp.high, inp.tail, inp.fog, inp.brake, inp.limit_kmh, inp.limit_source,
                inp.light, inp.env_light, inp.env_place, inp.env_weather, inp.ice_running,
                d.signs_on, d.signs_ahead, inp.cruise_active, inp.cruise_set_kmh,
                inp.limit_conf is not None and inp.limit_conf < 0.35, blink, fall)

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
        return (self._chase_cy - k * (dx * self._row_u0 + self._row_uz), k)

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
        path = d.path
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
            elif path and i < len(path):
                cen.append(path[i])
            else:
                cen.append(cen[-1] if cen else 0.0)
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

        fast = self._fast = d.view_step == VIEW_STEPS
        place = inp.env_place
        enclosed = fast and place in ENCLOSED
        road = road_k > 0.0 and d.vision in ("live", "stale")
        lines: list = []
        if road:
            self._set_centre(d, v)
            lines = self._lane_points(cam, d, v)
        else:
            self._cen = []
        if not enclosed:
            self._draw_sky(s, cam, inp)
        if road:
            # lane fill under the floor lines: they double as speed stripes
            self._draw_lane_fill(s, lines, d, road_k)
        self._draw_floor(s, cam, d, v)
        if enclosed:
            self.scenery.draw_enclosure(s, place, self._row, compress, d.odo, self.pal["grid_hi"])
        self._draw_beams(s, cam, inp, v)
        if road:
            if self.tune.path_chevrons:
                self._draw_path(s, cam, d, v, road_k)
            self._draw_road(s, cam, d, v, road_k, lines)
        if fast and place and not enclosed and self.tune.roadside != "off":
            self._draw_roadside(s, d, place, inp.env_light)
        if fast and inp.env_weather == "fog" and self.tune.fog_band:
            self.scenery.draw_fog(s, self.horizon_y)
        if road:
            self._draw_objects(s, cam, d, inp, v, road_k)

        spr, pos = self.cache.get(d.view_step, d.door_step, self._lamps(inp))
        s.blit(spr, pos)
        self._draw_glows(s, cam, inp)
        if fast and inp.env_weather in ("rain", "snow"):
            self.scenery.draw_precipitation(s, inp.env_weather, d.clock)

        if v >= 0.5:
            self._draw_hud_drive(s, d, inp)
        else:
            self._draw_hud_parked(s, d, inp)

    # -- sky ---------------------------------------------------------------------

    def _draw_sky(self, s: pygame.Surface, cam: Camera, inp: SceneInputs) -> None:
        hy = int(cam.horizon_y())
        if hy < -4 or hy > self.h:
            return
        strip = self.scenery.backdrop(inp.env_place, inp.env_light, inp.env_weather)
        # pan with the road: the far end of the centre line moves the backdrop
        pan = 0
        if self._fast and self._cen:
            pan = int(max(-PAN, min(PAN, self._cen[-1] * 1.6)))
        s.blit(strip, (0, hy - SKY_H + 1), (PAN - pan, 0, self.w, SKY_H))

    # -- floor -------------------------------------------------------------------

    def _draw_floor(self, s: pygame.Surface, cam: Camera, d: SceneDirector, v: float) -> None:
        col = self._grid_road if v > 0.5 else self._grid_pad
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

    # -- headlamp beams ----------------------------------------------------------

    def _draw_beams(self, s: pygame.Surface, cam: Camera, inp: SceneInputs, v: float) -> None:
        """Light on the road ahead: a hatched fan per lamp set, in the vector
        style (rows of the floor lit, an outline, a cut-off line).

            position lamps only  nothing on the road (the lenses glow)
            low beam             46 m fan with the asymmetric cut-off
            high beam            112 m, wider, brighter, no cut-off
            front fog            short wide fan under the others
        """
        kinds = []
        if self.tune.beam_style == "off":
            return
        if inp.fog:
            kinds.append("fog")
        if inp.high:
            kinds.append("high")
        elif inp.head:
            kinds.append("low")
        for kind in kinds:
            if self._fast:
                # the fan is fixed to the car: one cached sprite per kind
                hit = self._beam_sprites.get(kind)
                if hit is None:
                    tmp = pygame.Surface((self.w, self.h))
                    tmp.fill(KEY)
                    box = self._beam_lines(tmp, cam, kind, 1.0)
                    rect = pygame.Rect(box).clip(tmp.get_rect())
                    spr = pygame.Surface(rect.size)
                    spr.blit(tmp, (0, 0), rect)
                    spr.set_colorkey(KEY, pygame.RLEACCEL)
                    hit = self._beam_sprites[kind] = (spr, rect.topleft)
                s.blit(hit[0], hit[1])
            else:
                self._beam_lines(s, cam, kind, v)

    def _beam_lines(self, s: pygame.Surface, cam: Camera, kind: str, v: float):
        """Draw one beam fan; returns its bounding box (x, y, w, h).

        The fan points straight ahead of the car (it does not follow the
        road), so in the chase view it is the same picture every frame."""
        line = pygame.draw.line
        fast = v >= 1.0
        cx = self.w / 2.0
        reach, parked, w0, spread, wmax, col, edge = BEAMS[kind]
        # style (tuning.beam_style): outline = edges and end line only;
        # hatch_light = every other row, dimmer; hatch = every row
        style = self.tune.beam_style
        every = {"outline": 0, "hatch_light": 2}.get(style, 1)
        if style != "hatch":
            col, edge = _k(col, 0.6), _k(edge, 0.7)
        row_n = 0
        reach = parked + (reach - parked) * v
        near_side = reach * (LOW_BEAM_NEAR_SIDE if kind == "low" else 1.0)
        left_pts = []
        right_pts = []
        r = 0.25
        while True:
            last = r >= reach
            if last:
                r = reach
            hw = min(wmax, w0 + spread * r)
            wx = 2.3 + r
            yl = hw if r <= near_side else 0.0       # left of the cut-off: dark beyond
            if fast:
                sy, k = self._row(compress(wx))
                a = (cx - k * yl, sy)
                b = (cx + k * hw, sy)
            else:
                p = self._seg(cam, self._wx(wx, v), yl, self._wx(wx, v), -hw)
                if p is None:
                    break
                a, b = p
            if last:
                line(s, edge, a, b)
            elif every and row_n % every == 0:
                line(s, col, a, b)
            row_n += 1
            if r <= near_side:
                left_pts.append(a)
            right_pts.append(b)
            if last:
                break
            r = r * 1.16 + (0.28 if v > 0.5 else 0.22)
        if len(left_pts) >= 2:
            pygame.draw.lines(s, edge, False, left_pts)
            if kind == "low" and len(right_pts) > len(left_pts):
                # the step of the cut-off
                n = len(left_pts)
                mid = ((right_pts[n - 1][0] + left_pts[-1][0]) / 2.0, left_pts[-1][1])
                line(s, edge, left_pts[-1], mid)
        if len(right_pts) >= 2:
            pygame.draw.lines(s, edge, False, right_pts)
        pts = left_pts + right_pts
        if not pts:
            return (0, 0, 1, 1)
        x0 = min(p[0] for p in pts)
        x1 = max(p[0] for p in pts)
        y0 = min(p[1] for p in pts)
        y1 = max(p[1] for p in pts)
        return (int(x0) - 1, int(y0) - 1, int(x1 - x0) + 4, int(y1 - y0) + 4)

    # -- roadside scenery (by place label) ---------------------------------------

    def _draw_roadside(self, s: pygame.Surface, d: SceneDirector, place: str,
                       light: Optional[str]) -> None:
        """Things that stream past, chosen by the place label (decoration,
        like the backdrop; detected objects come through ``objects``)."""
        pal = self.pal
        left_e, right_e = d.edges[0], d.edges[1]
        xs = d.xs

        def side_y(x: float, sgn: float, off: float) -> float:
            e = left_e if sgn > 0 else right_e
            base = self._interp(xs, e, x) if e else sgn * 5.5
            return base + sgn * off

        if place == "forest_road":
            # tiered conifers beyond both road edges; "normal" adds a far row
            rows = ((3.8, 26.0, (0, 110, 76), (0, 22, 18)),)
            if self.tune.roadside == "normal":
                rows = ((7.5, 19.0, (0, 78, 58), (0, 18, 16)), (3.6, 19.0, (0, 140, 88), (0, 30, 24)))
            for off, pitch, line_c, fill in rows:
                if light == "night":
                    line_c = _k(line_c, 0.7)
                x = 100.0 - ((d.odo + off * 3.1) % pitch)
                while x > 7.0:
                    for sgn in (1.0, -1.0):
                        g = self._place(None, x, side_y(x, sgn, off + ((x * 13) % 5) * 0.25), 1.0)
                        if g is None:
                            continue
                        gx, gy, k = g
                        tall = min(44.0, k * (6.0 + ((x * 7) % 4) * 0.6))
                        conifer(s, gx, gy, tall, tall * 0.27, line_c, fill, 3 if tall > 26 else 2)
                    x -= pitch
        elif place in ("highway", "rural_road", "bridge"):
            # marker posts: white reflector on the left, red on the right
            pitch = 25.0
            rail = place == "bridge"
            prev = {1.0: None, -1.0: None}
            x = 100.0 - (d.odo % pitch)
            while x > 3.0:
                for sgn in (1.0, -1.0):
                    g = self._place(None, x, side_y(x, sgn, 0.8), 1.0)
                    if g is None:
                        continue
                    gx, gy, k = g
                    top = gy - max(2.0, k * 1.0)
                    pygame.draw.line(s, pal["lane_dim"], (gx, gy), (gx, top))
                    s.set_at((int(gx), int(top)), (220, 220, 220) if sgn > 0 else pal["red"])
                    if rail:
                        if prev[sgn] is not None:
                            pygame.draw.line(s, pal["lane_dim"], prev[sgn], (gx, top))
                        prev[sgn] = (gx, top)
                x -= pitch
        elif place == "parking_lot" and self.tune.roadside == "normal":
            # painted bays on both sides
            pitch = 5.0
            x = 60.0 - (d.odo % pitch)
            while x > 2.0:
                for sgn in (1.0, -1.0):
                    a = self._place(None, x, sgn * 3.2, 1.0)
                    b = self._place(None, x, sgn * 8.0, 1.0)
                    if a and b:
                        pygame.draw.line(s, pal["lane_dim"], (a[0], a[1]), (b[0], b[1]))
                x -= pitch

    # -- road --------------------------------------------------------------------

    def _draw_path(self, s: pygame.Surface, cam: Camera, d: SceneDirector,
                   v: float, k: float) -> None:
        """The model's planned path as chevrons (shows where the road goes
        even without lane lines)."""
        path = d.path
        if not path or len(path) < 3 or d.vision != "live":
            return
        pts = self._line_points(cam, d.xs[:len(path)], path, v)
        col = _k(self.pal["path"], k)
        for i in range(2, len(pts)):
            x, y = pts[i]
            px, py = pts[i - 1]
            half = max(2.0, (py - y) * 0.55)
            if half > 9.0:
                half = 9.0
            drop = max(1.0, half * 0.5)
            pygame.draw.lines(s, col, False, [(x - half, y + drop), (x, y), (x + half, y + drop)])


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

    LANE_WIDTH_OK = (2.3, 4.8)      # a plausible lane width (m)

    def lane_neighbours(self, d: SceneDirector) -> Tuple[bool, bool]:
        """(left, right): is a neighbouring lane seen on that side?

        A neighbour exists when the outer lane line of that side and the ego
        line next to it are both there, the outer one with at least
        ``lane_neighbour_min_prob``, and they are a lane width apart 10 m
        ahead. roadsense gives four lines, so at most one neighbour per side
        can be known, and nothing says whether a lane to the left carries
        oncoming traffic: the picture shows lanes, not directions."""
        if not self.tune.lane_neighbours or d.vision != "live":
            return False, False
        minp = self.tune.lane_neighbour_min_prob
        lo, hi = self.LANE_WIDTH_OK
        out = []
        for outer, inner in ((0, 1), (3, 2)):
            a, b = d.lanes[outer], d.lanes[inner]
            ok = bool(a and b and d.lane_prob[outer] >= minp)
            if ok:
                i = min(2, len(a) - 1, len(b) - 1)
                ok = lo <= abs(a[i] - b[i]) <= hi
            out.append(ok)
        return out[0], out[1]

    def _draw_lane_fill(self, s: pygame.Surface, lines: list, d: SceneDirector, k: float) -> None:
        left, right = lines[1], lines[2]
        if d.vision == "stale":
            return
        nl, nr = self.lane_neighbours(d)
        side = _k(self.pal["lane_side"], k)
        if nl and len(lines[0]) >= 2 and len(left) >= 2:
            pygame.draw.polygon(s, side, lines[0] + left[::-1])
        if nr and len(lines[3]) >= 2 and len(right) >= 2:
            pygame.draw.polygon(s, side, right + lines[3][::-1])
        if len(left) >= 2 and len(right) >= 2:
            pygame.draw.polygon(s, _k(self.pal["lane_fill"], k), left + right[::-1])

    def _draw_lane_indicator(self, s: pygame.Surface, d: SceneDirector) -> None:
        """Which lane of the lanes seen: one box per lane, the own one lit."""
        nl, nr = self.lane_neighbours(d)
        if not (nl or nr) or not self.tune.lane_indicator:
            return
        boxes = [False] * int(nl) + [True] + [False] * int(nr)
        bw, bh, gap = 9, 14, 3
        x = self.w - 5 - len(boxes) * (bw + gap) + gap
        y = self.h - 19
        for own in boxes:
            if own:
                pygame.draw.rect(s, self.pal["lane_ego"], (x, y, bw, bh))
            else:
                pygame.draw.rect(s, (0, 0, 0), (x, y, bw, bh))
                pygame.draw.rect(s, self.pal["lane_mid"], (x, y, bw, bh), 1)
            x += bw + gap

    def _draw_road(self, s: pygame.Surface, cam: Camera, d: SceneDirector,
                   v: float, k: float, lines: list) -> None:
        pal = self.pal
        stale = d.vision == "stale"
        xs = d.xs
        nl, nr = self.lane_neighbours(d)
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
                seen = (nl if i == 0 else nr)
                col = pal["lane_mid"] if (seen or d.lane_prob[i] >= 0.6) else pal["lane_dim"]
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
                          line: RGB, kind: str = "car", k: float = 1.0,
                          braking: bool = False) -> pygame.Rect:
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
        red = _k(pal["red"] if braking else pal["tail"], k)
        pygame.draw.rect(s, red, (int(x0) + 1, ly, lw, lh))
        pygame.draw.rect(s, red, (int(x1) - lw, ly, lw, lh))
        if braking and k > 0.6:
            r = max(3, int(w * 0.22))
            self._add_glow(s, (int(x0) + 1 + lw // 2, ly + lh // 2), r, pal["red"])
            self._add_glow(s, (int(x1) - lw // 2, ly + lh // 2), r, pal["red"])
            if w >= 14:
                pygame.draw.line(s, red, (cx - w * 0.14, top + 1), (cx + w * 0.14, top + 1))
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
        tune = self.tune
        cx, by, ppm = g
        a = d.lead_alpha * k
        gap = d.time_gap_s(inp.real_speed_kmh)
        col = self.gap_color(gap)
        w = max(9.0, 1.8 * ppm)
        braking = lead.accel is not None and lead.accel <= LEAD_BRAKING
        rect = self.draw_vehicle_rear(s, cx, by, w, col, lead.kind, a, braking)
        if a < 0.6:
            return
        r = rect.inflate(6, 6)
        line = pygame.draw.line
        if tune.lead_brackets:
            t = 3
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
        ty = max(2, min(self.h - 34, r.centery - 9))
        pygame.draw.rect(s, (0, 0, 0), (tx - 1, ty, tw + 2, 16))
        s.blit(num, (tx, ty))
        s.blit(unit, (tx + num.get_width() + 2, ty + 7))
        # second line, Terminus 14, only when it matters (tuning): the time
        # gap when short, the closing / opening speed when large
        sx = tx
        y2 = ty + 16
        if gap is not None and gap < tune.gap_show_below_s:
            st = self.text("%.1fs" % gap, 14, col)
            pygame.draw.rect(s, (0, 0, 0), (sx - 1, y2, st.get_width() + 3, 14))
            s.blit(st, (sx, y2))
            sx += st.get_width() + 5
        if lead.v_rel is not None and abs(lead.v_rel) * 3.6 >= max(0.5, tune.closing_show_kmh):
            closing = lead.v_rel < 0
            vc = self.pal["warn"] if closing else self.pal["green"]
            vt = self.text("%d" % int(abs(lead.v_rel) * 3.6 + 0.5), 14, vc)
            pygame.draw.rect(s, (0, 0, 0), (sx - 1, y2, 11 + vt.get_width(), 14))
            if closing:             # arrow towards us, closing speed in km/h
                pygame.draw.polygon(s, vc, [(sx, y2 + 4), (sx + 6, y2 + 4), (sx + 3, y2 + 10)])
            else:                   # pulling away
                pygame.draw.polygon(s, vc, [(sx, y2 + 10), (sx + 6, y2 + 10), (sx + 3, y2 + 4)])
            s.blit(vt, (sx + 9, y2))

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
        if not (inp.head or inp.high or inp.tail or inp.brake):
            return
        pts = self.car.lamp_points(cam)
        scale = cam.f / max(1.0, math.dist(cam.pos, (0.0, 0.0, 0.6)))
        r = max(4, int(scale * 0.30))
        if inp.head or inp.high:
            for n in ("head_l", "head_r"):
                if n in pts:
                    self._add_glow(s, pts[n], r + (6 if inp.high else 3), self.pal["warn"])
        elif inp.tail:
            # position lamps: a small marker glow in the headlamp
            for n in ("head_l", "head_r"):
                if n in pts:
                    self._add_glow(s, pts[n], max(3, r - 2), (150, 130, 70))
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
        """Label box in Terminus 14 (18 px tall); returns its width."""
        t = self.text(label, 14, (0, 0, 0) if filled else color)
        w = t.get_width() + 7
        if filled:
            pygame.draw.rect(s, color, (x, y, w, 17))
        else:
            pygame.draw.rect(s, (0, 0, 0), (x, y, w, 17))
            pygame.draw.rect(s, color, (x, y, w, 17), 1)
        s.blit(t, (x + 4, y + 2))
        return w

    def _draw_gear(self, s: pygame.Surface, x: int, y: int, gear: str) -> None:
        g = self.text(gear, 16, self.pal["value"])
        pygame.draw.rect(s, COLORS["cyan_dark"], (x, y, 15, 19))
        pygame.draw.rect(s, self.pal["lane_mid"], (x, y, 15, 19), 1)
        s.blit(g, (x + (15 - g.get_width()) // 2 + 1, y + 2))

    def _draw_limit(self, s: pygame.Surface, cx: int, cy: int, kmh: int, over: bool,
                    seen: bool = True) -> None:
        """European limit sign as a neon ring. A limit that follows from a
        default rule (no sign seen) gets a thin dim ring."""
        r = 13
        pygame.draw.circle(s, (0, 0, 0), (cx, cy), r)
        if seen:
            pygame.draw.circle(s, self.pal["red"], (cx, cy), r, 3)
        else:
            pygame.draw.circle(s, (150, 30, 34), (cx, cy), r, 1)
        col = self.pal["warn"] if over else self.pal["text"] if seen else self.pal["dim"]
        txt = self.text(str(kmh), 14, col)
        s.blit(txt, (cx - txt.get_width() // 2 + (1 if kmh < 100 else 0), cy - txt.get_height() // 2))

    def _plate_text(self, string: str, color: RGB) -> pygame.Surface:
        return self.text(string, 14, color)

    SIGN_ROW_Y = 33         # the sign row under the speed line (show_speed on)
    TOP_ROW_Y = 7           # the signs in the top line (show_speed off)

    def _draw_sign_row(self, s: pygame.Surface, d: SceneDirector, y: int, x_right: int) -> None:
        """Warnings ahead from the left (each with its expected distance in
        metres), rules in effect right-aligned at ``x_right``."""
        pal = self.pal
        tune = self.tune
        on = d.signs_on
        shown = on[:max(0, int(tune.signs_max_on))]
        ahead = d.signs_ahead[:max(0, int(tune.signs_max_ahead))]
        # room: the warnings take what they need first, the rules the rest
        need_ahead = 4
        for it in ahead:
            pic = sign_surface(it.id, it.code, it.unsure, self._plate_text)
            need_ahead += pic.get_width() + (self.text("%d" % it.m, 14, pal["warn"]).get_width() + 8
                                             if it.m is not None else 3)
        room = x_right - need_ahead - 4
        fit = []
        used = 0
        for it in shown:
            pic = sign_surface(it.id, it.code, it.unsure, self._plate_text)
            if used + pic.get_width() + 3 > room:
                break
            fit.append((it, pic))
            used += pic.get_width() + 3
        more = len(on) - len(fit)
        x = x_right
        if more > 0:
            t = self.text("+%d" % more, 14, pal["label"])
            while fit and used + t.get_width() + 4 > room:
                used -= fit[-1][1].get_width() + 3
                fit.pop()
                more += 1
                t = self.text("+%d" % more, 14, pal["label"])
            x -= t.get_width()
            s.blit(t, (x, y + 3))
            x -= 4
        for it, pic in reversed(fit):
            x -= pic.get_width()
            s.blit(pic, (x, y))
            if it.side:
                # binds one side of the road: a small pointer under the sign
                mx = x + pic.get_width() // 2
                dx = -3 if it.side == "l" else 3
                pygame.draw.polygon(s, pal["label"], [(mx - dx, y + 20), (mx - dx, y + 24), (mx + dx, y + 22)])
            x -= 3
        x = 4
        for it in ahead:
            pic = sign_surface(it.id, it.code, it.unsure, self._plate_text)
            s.blit(pic, (x, y))
            x += pic.get_width() + 2
            if it.m is not None:
                dist = self.text("%d" % it.m, 14, pal["warn"])
                pygame.draw.rect(s, (0, 0, 0), (x - 1, y + 3, dist.get_width() + 2, 14))
                s.blit(dist, (x, y + 3))
                x += dist.get_width() + 6

    def _draw_hud_drive(self, s: pygame.Surface, d: SceneDirector, inp: SceneInputs) -> None:
        pal = self.pal
        tune = self.tune
        if tune.show_speed:
            # speed + gear, top-left
            spd = self.text("%d" % int(inp.speed_kmh + 0.5), 28, pal["text"])
            pygame.draw.rect(s, (0, 0, 0), (2, 2, spd.get_width() + 30, 27))
            s.blit(spd, (5, 1))
            gx = 5 + spd.get_width() + 3
            s.blit(self.text("km/h", 8, pal["label"], mono=False), (gx, 6))
            s.blit(self.text(inp.gear, 14, pal["value"]), (gx + 1, 14))
            if tune.cruise_chip and inp.cruise_active and inp.cruise_set_kmh:
                # engaged cruise: the set speed beside the speed (the cruise
                # slot itself is in the strip below the scene)
                self._chip(s, gx + 30, 6, "SET %d" % inp.cruise_set_kmh, pal["green"], filled=True)
        # speed limit + traffic light, top-right (the corner dot is the link LED)
        x_right = self.w - 20
        if inp.limit_kmh:
            over = inp.speed_kmh > inp.limit_kmh + 3
            seen = inp.limit_source != "default" and (inp.limit_conf is None or inp.limit_conf >= 0.35)
            self._draw_limit(s, x_right - 14, 16, inp.limit_kmh, over, seen)
            x_right -= 32
        if inp.light is not None and inp.light.state in self.LIGHT_COLORS:
            self._draw_signal(s, x_right - 12, 3, 3, inp.light, d.clock)
            x_right -= 16
            if inp.light.dist_m is not None:
                t = self.text("%d" % int(inp.light.dist_m + 0.5), 14, pal["text"])
                pygame.draw.rect(s, (0, 0, 0), (x_right - t.get_width() - 1, 8, t.get_width() + 2, 14))
                s.blit(t, (x_right - t.get_width(), 8))
                x_right -= t.get_width() + 4
        if tune.show_speed:
            self._draw_sign_row(s, d, self.SIGN_ROW_Y, self.w - 3)
        else:
            # the top line belongs to the signs: warnings, rules, the limit
            self._draw_sign_row(s, d, self.TOP_ROW_Y, x_right - 2)
        self._draw_lane_indicator(s, d)
        # bottom-left: lamps (tuning.lamp_chips), above them the perception status
        x = 4
        y = self.h - 20
        if tune.lamp_chips != "none":
            every = tune.lamp_chips == "all"
            for on, label, col in ((every and inp.tail and not inp.head and not inp.high, "POS", pal["green"]),
                                   (every and inp.head and not inp.high, "LOW", pal["warn"]),
                                   (inp.high, "HIGH", (66, 165, 245)),
                                   (every and inp.fog, "FOG", pal["green"])):
                if on:
                    x += self._chip(s, x, y, label, col) + 2
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
            self._chip(s, 4, y - 19 if x > 4 else y, tag[0], tag[1])

    def _draw_hud_parked(self, s: pygame.Surface, d: SceneDirector, inp: SceneInputs) -> None:
        pal = self.pal
        self._draw_gear(s, 5, 4, inp.gear)
        x = 24
        if inp.ready:
            x += self._chip(s, x, 5, "READY", pal["green"], filled=True) + 3
        elif inp.acc_on:
            x += self._chip(s, x, 5, "ACC", pal["value"]) + 3
        else:
            x += self._chip(s, x, 5, "OFF", pal["dim"]) + 3
        if inp.ice_running:
            x += self._chip(s, x, 5, "ICE", pal["orange"]) + 3
        if inp.speed_kmh >= 1.0:
            t = self.text("%d" % int(inp.speed_kmh + 0.5), 16, pal["text"])
            s.blit(t, (x + 3, 5))
            s.blit(self.text("km/h", 8, pal["label"], mono=False), (x + 5 + t.get_width(), 11))
        # lamps that are on: second row, right-aligned
        x = self.w - 4
        for on, label, col in ((inp.fog, "FOG", pal["green"]), (inp.high, "HIGH", (66, 165, 245)),
                               (inp.head, "LOW", pal["warn"]), (inp.tail, "TAIL", pal["red"]),
                               (inp.brake, "BRAKE", pal["red"])):
            if on:
                t = self.text(label, 14, col)
                x -= t.get_width() + 7 + 2
                self._chip(s, x, 26, label, col)
        # doors, bottom
        opened = [DOOR_LABELS[i] for i, v in enumerate(inp.doors) if v]
        if inp.rear_group:
            opened.append("REAR")           # one shared switch bit: a rear door or the back door
        if not opened and inp.door_any:
            opened.append("DOOR")
        y = self.h - 20
        if opened:
            w = self._chip(s, 4, y, "OPEN", pal["warn"], filled=True)
            s.blit(self.text(" ".join(opened), 14, pal["warn"]), (4 + w + 5, y + 2))
        elif inp.doors_known:
            s.blit(self.text("DOORS CLOSED", 14, pal["label"]), (5, y + 2))
