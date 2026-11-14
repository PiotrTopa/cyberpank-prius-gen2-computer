"""
Scenery of the road view, chosen by roadsense's environment labels.

Labels (roadsense envd, ``models/envd-classes.json`` / ``tools/envd/prompts.py``):

    place    highway, city_street, residential, rural_road, forest_road,
             tunnel, parking_lot, underground_parking, gas_station, bridge
    light    day, dusk_dawn, night
    weather  clear, overcast, rain, snow, fog

Everything here is decoration that follows a label: it says "roadsense calls
this a city street at night in the rain", it never stands for a detected
object. An unknown or missing label draws the neutral variant (a bare ridge,
no sun or moon, no weather).

Cost model: each (place, light, weather) backdrop is one strip rendered once
and cached; per redraw it is a single blit, panned sideways by the road's
curvature. Rain and snow are colour-keyed tiles blitted with a moving offset;
fog is a dithered band. The enclosed places (tunnel, underground parking) are
a dozen lines drawn from the road's own row table.
"""

from __future__ import annotations

import math
from typing import Callable, Dict, Optional, Tuple

import pygame

RGB = Tuple[int, int, int]

PLACES = ("highway", "city_street", "residential", "rural_road", "forest_road", "tunnel",
          "parking_lot", "underground_parking", "gas_station", "bridge")
ENCLOSED = ("tunnel", "underground_parking")
LIGHTS = ("day", "dusk_dawn", "night")
WEATHERS = ("clear", "overcast", "rain", "snow", "fog")
CLOUDY = ("overcast", "rain", "snow", "fog")

PAN = 40                       # the backdrop is this much wider on each side
KEY = (255, 0, 255)

AMBER = (255, 194, 122)
WARM = (255, 235, 153)
CYAN = (0, 168, 150)
NEON = (214, 72, 176)
RED = (255, 36, 36)


class _Rng:
    """Tiny deterministic generator (the art must not change between runs)."""

    def __init__(self, seed: int):
        self.s = seed & 0x7FFFFFFF

    def n(self, lo: int, hi: int) -> int:
        self.s = (self.s * 1103515245 + 12345) & 0x7FFFFFFF
        return lo + (self.s >> 8) % (hi - lo + 1)

    def chance(self, p: float) -> bool:
        return self.n(0, 999) < p * 1000


def _line_colour(light: Optional[str]) -> RGB:
    """Outline colour of the skyline for a light condition."""
    if light == "day":
        return (62, 112, 122)
    if light == "night":
        return (58, 46, 116)
    return (96, 66, 160)           # dusk / unknown: the violet of the ridge


def conifer(s: pygame.Surface, x: float, base: float, tall: float, half: float,
            line: RGB, fill: RGB, tiers: int = 3) -> None:
    """A tiered conifer: stacked triangles widening downwards, on a trunk."""
    trunk = max(1.0, tall * 0.12)
    top = base - tall
    body = tall - trunk
    pts_l, pts_r = [], []
    for i in range(tiers):
        t0 = i / float(tiers)
        t1 = (i + 1) / float(tiers)
        w0 = half * (0.18 + 0.55 * t0) * (0.55 if i else 0.0)
        w1 = half * (0.45 + 0.55 * t1)
        y0 = top + body * t0
        y1 = top + body * t1
        pts_l += [(x - w0, y0), (x - w1, y1)]
        pts_r += [(x + w0, y0), (x + w1, y1)]
    pygame.draw.polygon(s, fill, pts_l + pts_r[::-1])
    pygame.draw.lines(s, line, False, pts_l)
    pygame.draw.lines(s, line, False, pts_r)
    pygame.draw.line(s, line, (x, base - trunk), (x, base))


class Scenery:
    def __init__(self, width: int, sky_h: int, horizon_colour: RGB,
                 brightness: float = 1.0, band_px: int = 0,
                 weather_density: float = 1.0, weather_brightness: float = 1.0):
        # brightness / band_px: how loud the backdrop is (tuning.py);
        # weather_density / weather_brightness: the same for rain and snow
        self.brightness = max(0.0, min(1.0, brightness))
        self.band_px = int(band_px)
        self.weather_density = max(0.0, min(1.0, weather_density))
        self.weather_brightness = max(0.0, min(1.0, weather_brightness))
        self.w = width
        self.h = sky_h
        self.horizon = horizon_colour
        self._strips: Dict[tuple, pygame.Surface] = {}
        self._tiles: Dict[str, pygame.Surface] = {}

    # ── backdrop ─────────────────────────────────────────────────────────────

    def backdrop(self, place: Optional[str], light: Optional[str],
                 weather: Optional[str]) -> pygame.Surface:
        """Sky + skyline strip, ``w + 2 * PAN`` wide, its last row the horizon."""
        if place not in PLACES:
            place = None
        if light not in LIGHTS:
            light = None
        if weather not in WEATHERS:
            weather = None
        key = (place, light, weather)
        strip = self._strips.get(key)
        if strip is None:
            if len(self._strips) >= 6:
                self._strips.clear()
            strip = pygame.Surface((self.w + 2 * PAN, self.h))
            strip.fill((0, 0, 0))
            self._sky(strip, light, weather)
            self._skyline(strip, place, light)
            if self.brightness < 0.999:
                f = int(255 * self.brightness)
                strip.fill((f, f, f), special_flags=pygame.BLEND_RGB_MULT)
            if 0 < self.band_px < self.h:
                # only a band over the horizon is kept
                pygame.draw.rect(strip, (0, 0, 0), (0, 0, strip.get_width(), self.h - self.band_px))
            pygame.draw.line(strip, self.horizon, (0, self.h - 1), (strip.get_width(), self.h - 1))
            self._strips[key] = strip
        return strip

    def _sky(self, s: pygame.Surface, light: Optional[str], weather: Optional[str]) -> None:
        w, h = s.get_size()
        cx = w // 2
        cloudy = weather in CLOUDY
        # glow over the horizon: scan lines thinning out upwards
        glow = {"day": ((0, 52, 66), (0, 30, 40)), "dusk_dawn": ((84, 30, 70), (48, 18, 52))}.get(light or "")
        if glow and not cloudy:
            for i, y in enumerate((h - 3, h - 5, h - 7, h - 10, h - 13, h - 17, h - 22, h - 28)):
                pygame.draw.line(s, glow[0] if i < 4 else glow[1], (0, y), (w, y))
        if cloudy:
            # cloud deck: broken bands, heavier towards the top
            col = (44, 44, 78) if light == "night" else (70, 74, 106)
            rng = _Rng(77)
            for row, y in enumerate(range(3, 40, 6)):
                x = -rng.n(0, 30)
                while x < w:
                    ln = rng.n(18, 60) - row * 3
                    pygame.draw.line(s, col, (x, y), (x + max(6, ln), y))
                    if row < 3:
                        pygame.draw.line(s, col, (x + 3, y + 1), (x + max(6, ln) - 4, y + 1))
                    x += max(6, ln) + rng.n(6, 22) + row * 4
            return
        if light == "night":
            rng = _Rng(12345)
            for _ in range(70):
                x, y = rng.n(0, w - 1), rng.n(0, h - 16)
                s.set_at((x, y), (255, 255, 255) if rng.chance(0.3) else (53, 93, 104))
            mx, my = cx + 8, 13
            pygame.draw.circle(s, WARM, (mx, my), 7)
            pygame.draw.circle(s, (0, 0, 0), (mx + 4, my - 2), 7)
        elif light == "dusk_dawn":
            # striped sun on the horizon under thin bands of afterglow
            r = 26
            cy = h - 3
            top, bot = WARM, (232, 96, 96)
            for yy in range(cy - r, h):
                dy = yy - cy
                half = int(math.sqrt(max(0, r * r - dy * dy)))
                t = (yy - (cy - r)) / float(2 * r)
                band = (yy - (cy - r)) % 5
                if t > 0.26 and band >= 5 - int(1 + t * 3.4):
                    continue
                col = (int(top[0] + (bot[0] - top[0]) * t), int(top[1] + (bot[1] - top[1]) * t),
                       int(top[2] + (bot[2] - top[2]) * t))
                pygame.draw.line(s, col, (cx - half, yy), (cx + half, yy))
            for i, y in enumerate((h - 44, h - 52, h - 62)):
                span = 86 - i * 22
                pygame.draw.line(s, (120, 52, 150) if i else (150, 64, 180),
                                 (cx - span, y), (cx - 34 + i * 6, y))
                pygame.draw.line(s, (120, 52, 150) if i else (150, 64, 180),
                                 (cx + 34 - i * 6, y), (cx + span, y))
        elif light == "day":
            # sun with rays, and flat-bottomed vector clouds
            sx, sy = cx + 40, 20
            pygame.draw.circle(s, WARM, (sx, sy), 8)
            for a in range(0, 360, 45):
                dx, dy = math.cos(math.radians(a)), math.sin(math.radians(a))
                pygame.draw.line(s, AMBER, (sx + dx * 11, sy + dy * 11), (sx + dx * 14, sy + dy * 14))
            col = (86, 128, 140)
            for x, y, ln in ((cx - 104, 40, 30), (cx - 30, 16, 22), (cx + 112, 46, 26)):
                pygame.draw.lines(s, col, False, [
                    (x, y), (x + 3, y - 4), (x + ln // 3, y - 5), (x + ln // 2, y - 8),
                    (x + 2 * ln // 3, y - 5), (x + ln - 3, y - 4), (x + ln, y), (x, y)])

    def _skyline(self, s: pygame.Surface, place: Optional[str], light: Optional[str]) -> None:
        draw = getattr(self, "_place_" + (place or "none"), None)
        if draw is None or place in ENCLOSED:
            draw = self._place_none
        draw(s, _line_colour(light), light)

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _solid(s: pygame.Surface, pts, col: RGB, closed: bool = False) -> None:
        """Black-filled silhouette with a coloured outline (hides the sky)."""
        w, h = s.get_size()
        poly = [(pts[0][0], h)] + list(pts) + [(pts[-1][0], h)]
        pygame.draw.polygon(s, (0, 0, 0), poly)
        pygame.draw.lines(s, col, closed, pts)

    def _ridge(self, s: pygame.Surface, col: RGB, heights, base: int = 0) -> None:
        w, h = s.get_size()
        step = w / float(len(heights) - 1)
        pts = [(int(i * step), h - 2 - base - hh) for i, hh in enumerate(heights)]
        self._solid(s, pts, col)

    # -- one skyline per place ------------------------------------------------

    def _place_none(self, s, col, light) -> None:
        self._ridge(s, col, (7, 12, 6, 15, 9, 4, 3, 5, 3, 4, 8, 14, 7, 11, 5, 9, 6))

    def _place_rural_road(self, s, col, light) -> None:
        far = (col[0] * 2 // 3, col[1] * 2 // 3, col[2] * 2 // 3)
        self._ridge(s, far, (14, 26, 18, 30, 22, 12, 9, 13, 20, 31, 24, 15, 19, 28, 16, 10, 14))
        self._ridge(s, col, (5, 9, 6, 11, 7, 3, 2, 3, 2, 3, 6, 10, 5, 8, 4, 6, 4))
        w, h = s.get_size()
        rng = _Rng(31)
        green = (0, 120, 74) if light != "night" else (0, 78, 60)
        for _ in range(7):
            x = rng.n(6, w - 6)
            if abs(x - w // 2) < 30:
                continue
            y = h - 2
            pygame.draw.line(s, green, (x, y), (x, y - 4))
            pygame.draw.circle(s, (0, 0, 0), (x, y - 7), 4)
            pygame.draw.circle(s, green, (x, y - 7), 4, 1)

    def _place_forest_road(self, s, col, light) -> None:
        """Three depth layers: a solid far tree line, then two rows of tiered
        conifers, opening over the road."""
        w, h = s.get_size()
        night = light == "night"
        rng = _Rng(5)
        pts = [(0, h)]
        x = 0
        while x <= w:
            gap = abs(x - w // 2)
            top = rng.n(20, 30) if gap > 30 else 8 + gap // 3
            pts.append((x, h - 2 - top))
            x += rng.n(3, 6)
        pts.append((w, h))
        pygame.draw.polygon(s, (0, 20, 18), pts)
        pygame.draw.lines(s, (0, 52, 46) if night else (0, 70, 58), False, pts[1:-1])
        for line, fill, lo, hi, step, seed in (((0, 86, 62), (0, 26, 22), 22, 36, 17, 3),
                                              ((0, 150, 92), (0, 36, 28), 30, 52, 23, 9)):
            if night:
                line = (0, int(line[1] * 0.7), int(line[2] * 0.7))
            rng = _Rng(seed)
            x = -6 + rng.n(0, 8)
            while x < w + 8:
                if abs(x - w // 2) > 34:
                    tall = rng.n(lo, hi)
                    conifer(s, x, h - 2, tall, tall * 0.26, line, fill, 3 if tall > 30 else 2)
                x += step + rng.n(-3, 5)

    def _place_highway(self, s, col, light) -> None:
        w, h = s.get_size()
        self._ridge(s, col, (3, 4, 3, 5, 4, 2, 2, 2, 2, 2, 3, 5, 4, 3, 4, 3, 2))
        # a power line marching off: lattice pylons with sagging wires
        tops = []
        for i, x in enumerate((22, 96, w - 96, w - 22)):
            tall = 38 if i in (0, 3) else 30
            y0, y1 = h - 2, h - 2 - tall
            pygame.draw.line(s, col, (x - 4, y0), (x - 1, y1))
            pygame.draw.line(s, col, (x + 4, y0), (x + 1, y1))
            for f in (0.35, 0.62):
                yy = int(y0 - tall * f)
                pygame.draw.line(s, col, (x - 3, yy), (x + 3, yy))
            arm = 9 if tall > 32 else 7
            for f in (0.8, 0.95):
                yy = int(y0 - tall * f)
                pygame.draw.line(s, col, (x - arm, yy), (x + arm, yy))
            tops.append((x, int(y0 - tall * 0.95), arm))
        for (xa, ya, aa), (xb, yb, ab) in ((tops[0], tops[1]), (tops[2], tops[3])):
            for sgn in (-1, 1):
                x0, x1 = xa + sgn * aa, xb + sgn * ab
                pygame.draw.lines(s, col, False, [(x0, ya), ((x0 + x1) // 2, max(ya, yb) + 7), (x1, yb)])
        if light == "night":
            for x, y, _ in tops:
                s.set_at((x, y - 3), RED)

    def _place_city_street(self, s, col, light) -> None:
        w, h = s.get_size()
        rng = _Rng(2019)
        lit = {"night": 0.42, "dusk_dawn": 0.2}.get(light or "", 0.0)
        # far layer: a stepped ziggurat and slabs, outline only
        far = (col[0] * 2 // 3, col[1] * 2 // 3, col[2] * 2 // 3)
        zx = w // 2 + 58
        pts = [(zx - 40, h - 2)]
        for i in range(5):
            pts += [(zx - 40 + i * 7, h - 2 - (i + 1) * 9), (zx - 33 + i * 7, h - 2 - (i + 1) * 9)]
        pts += [(zx + 2, h - 2 - 45), (zx + 9, h - 2 - 45)]
        for i in range(4, -1, -1):
            pts += [(zx + 9 + (4 - i) * 7, h - 2 - (i + 1) * 9), (zx + 16 + (4 - i) * 7, h - 2 - i * 9)]
        self._solid(s, pts, far)
        x = -6
        while x < w:
            bw = rng.n(11, 24)
            tall = rng.n(16, 58)
            # keep the street canyon open over the vanishing point
            mid = abs(x + bw // 2 - w // 2)
            if mid < 34:
                tall = min(tall, 10 + mid // 2)
            top = h - 2 - tall
            pygame.draw.rect(s, (0, 0, 0), (x, top, bw, tall + 2))
            pygame.draw.lines(s, col, False, [(x, h - 2), (x, top), (x + bw - 1, top), (x + bw - 1, h - 2)])
            if tall > 40 and rng.chance(0.6):
                pygame.draw.line(s, col, (x + bw // 2, top), (x + bw // 2, top - 9))
                if light in ("night", "dusk_dawn"):
                    s.set_at((x + bw // 2, top - 10), RED)
            if lit:
                for wy in range(top + 3, h - 4, 4):
                    for wx in range(x + 3, x + bw - 3, 4):
                        if rng.chance(lit):
                            c = CYAN if rng.chance(0.18) else AMBER
                            s.set_at((wx, wy), c)
                            s.set_at((wx + 1, wy), c)
                if tall > 30 and bw > 16 and rng.chance(0.35):
                    pygame.draw.rect(s, NEON, (x + 3, top + 6, bw - 7, 5), 1)
            else:
                for wy in range(top + 4, h - 5, 6):
                    pygame.draw.line(s, far, (x + 3, wy), (x + bw - 4, wy))
            x += bw + rng.n(-1, 3)

    def _place_residential(self, s, col, light) -> None:
        w, h = s.get_size()
        rng = _Rng(404)
        green = (0, 120, 74) if light != "night" else (0, 78, 60)
        x = -4
        while x < w:
            if abs(x - w // 2) < 22:
                x += 14
                continue
            if rng.chance(0.3):
                pygame.draw.line(s, green, (x + 4, h - 2), (x + 4, h - 7))
                pygame.draw.circle(s, (0, 0, 0), (x + 4, h - 11), 5)
                pygame.draw.circle(s, green, (x + 4, h - 11), 5, 1)
                x += 12
                continue
            bw = rng.n(14, 22)
            wall = rng.n(7, 10)
            top = h - 2 - wall
            pts = [(x, h - 2), (x, top), (x + bw // 2, top - rng.n(5, 8)), (x + bw, top), (x + bw, h - 2)]
            pygame.draw.polygon(s, (0, 0, 0), pts)
            pygame.draw.lines(s, col, False, pts)
            pygame.draw.line(s, col, (x, top), (x + bw, top))
            if light in ("night", "dusk_dawn"):
                for wx in (x + 3, x + bw - 6):
                    if rng.chance(0.55):
                        pygame.draw.rect(s, AMBER, (wx, top + 3, 3, 3))
            else:
                pygame.draw.rect(s, col, (x + 3, top + 3, 3, 3), 1)
            x += bw + rng.n(3, 9)

    def _place_bridge(self, s, col, light) -> None:
        w, h = s.get_size()
        deck = h - 2
        towers = (w // 2 - 86, w // 2 + 86)
        tall = h - 16
        # main cables first, then hangers down to the deck
        for a, b in ((-40, towers[0]), (towers[0], towers[1]), (towers[1], w + 40)):
            n = 14
            prev = None
            for i in range(n + 1):
                t = i / float(n)
                x = a + (b - a) * t
                y = deck - tall + (4 * (tall - 12)) * t * (1 - t)
                if prev is not None:
                    pygame.draw.line(s, col, prev, (x, y))
                pygame.draw.line(s, col, (x, y), (x, deck))
                prev = (x, y)
        for x in towers:
            pygame.draw.rect(s, (0, 0, 0), (x - 4, deck - tall - 4, 9, tall + 4))
            pygame.draw.line(s, col, (x - 4, deck), (x - 4, deck - tall - 4))
            pygame.draw.line(s, col, (x + 4, deck), (x + 4, deck - tall - 4))
            for f in (0.3, 0.62, 1.0):
                yy = int(deck - (tall + 4) * f)
                pygame.draw.line(s, col, (x - 4, yy), (x + 4, yy))
            if light in ("night", "dusk_dawn"):
                s.set_at((x, deck - tall - 6), RED)
                s.set_at((x, deck - tall - 7), RED)

    def _place_parking_lot(self, s, col, light) -> None:
        w, h = s.get_size()
        rng = _Rng(88)
        self._ridge(s, (col[0] * 2 // 3, col[1] * 2 // 3, col[2] * 2 // 3),
                    (4, 6, 5, 7, 5, 4, 4, 5, 4, 4, 5, 7, 6, 5, 6, 5, 4))
        for x in range(8, w, 12):          # parked cars along the far edge
            if rng.chance(0.7) and abs(x - w // 2) > 16:
                pygame.draw.rect(s, (0, 0, 0), (x, h - 6, 8, 4))
                pygame.draw.lines(s, col, False, [(x, h - 2), (x, h - 5), (x + 2, h - 7),
                                                  (x + 6, h - 7), (x + 8, h - 5), (x + 8, h - 2)])
        for x in range(26, w, 52):         # light masts, two heads each
            pygame.draw.line(s, col, (x, h - 2), (x, h - 34))
            pygame.draw.line(s, col, (x - 6, h - 34), (x + 6, h - 34))
            for hx in (x - 6, x + 6):
                if light in ("night", "dusk_dawn"):
                    pygame.draw.rect(s, WARM, (hx - 1, h - 34, 3, 2))
                else:
                    pygame.draw.rect(s, col, (hx - 1, h - 34, 3, 2), 1)

    def _place_gas_station(self, s, col, light) -> None:
        w, h = s.get_size()
        self._ridge(s, (col[0] * 2 // 3, col[1] * 2 // 3, col[2] * 2 // 3),
                    (5, 8, 6, 9, 6, 3, 2, 3, 2, 3, 5, 4, 3, 4, 5, 6, 4))
        on = light in ("night", "dusk_dawn")
        # canopy to the right of the road, price pylon beside it
        x0, x1 = w // 2 + 30, w // 2 + 118
        roof = h - 34
        pygame.draw.rect(s, (0, 0, 0), (x0, roof, x1 - x0, 7))
        pygame.draw.rect(s, WARM if on else col, (x0, roof, x1 - x0, 7), 1)
        pygame.draw.line(s, NEON if on else col, (x0 + 2, roof + 3), (x1 - 3, roof + 3))
        for px in (x0 + 12, x1 - 13):
            pygame.draw.rect(s, (0, 0, 0), (px - 1, roof + 7, 3, 26))
            pygame.draw.line(s, col, (px - 1, roof + 7), (px - 1, h - 2))
            pygame.draw.line(s, col, (px + 1, roof + 7), (px + 1, h - 2))
        for px in range(x0 + 8, x1 - 6, 10):       # lamps under the canopy
            if on:
                s.set_at((px, roof + 8), WARM)
                s.set_at((px + 1, roof + 8), WARM)
        for px in (x0 + 34, x0 + 56):               # pumps
            pygame.draw.rect(s, (0, 0, 0), (px, h - 12, 6, 10))
            pygame.draw.rect(s, col, (px, h - 12, 6, 10), 1)
        tx = w // 2 - 62
        pygame.draw.rect(s, (0, 0, 0), (tx, h - 40, 12, 38))
        pygame.draw.rect(s, col, (tx, h - 40, 12, 38), 1)
        for i, yy in enumerate(range(h - 36, h - 12, 7)):
            c = (AMBER, CYAN, WARM, AMBER)[i % 4] if on else col
            pygame.draw.line(s, c, (tx + 3, yy), (tx + 8, yy))
            pygame.draw.line(s, c, (tx + 3, yy + 2), (tx + 8, yy + 2))

    # ── weather ──────────────────────────────────────────────────────────────

    def _tile(self, kind: str, size: Tuple[int, int]) -> pygame.Surface:
        t = self._tiles.get(kind)
        if t is not None and t.get_size() == size:
            return t
        w, h = size
        t = pygame.Surface(size)
        t.fill(KEY)
        t.set_colorkey(KEY, pygame.RLEACCEL)
        rng = _Rng(9 if kind == "rain" else 17)
        dens, br = self.weather_density, self.weather_brightness

        def tone(c: RGB) -> RGB:
            return (int(c[0] * br), int(c[1] * br), int(c[2] * br))

        if kind == "rain":
            for _ in range(w * h // 420):
                x, y = rng.n(0, w - 1), rng.n(0, h - 1)
                ln = rng.n(3, 6)
                c = (64, 112, 132) if rng.chance(0.7) else (110, 160, 176)
                if rng.chance(dens):
                    pygame.draw.line(t, tone(c), (x, y), (x - ln // 2, y + ln))
        else:
            for _ in range(w * h // 520):
                x, y = rng.n(0, w - 1), rng.n(0, h - 1)
                bigf = rng.chance(0.3)
                if not rng.chance(dens):
                    continue
                if bigf:
                    pygame.draw.rect(t, tone((210, 220, 228)), (x, y, 2, 2))
                else:
                    t.set_at((x, y), tone((130, 146, 158)))
        self._tiles[kind] = t
        return t

    def draw_precipitation(self, s: pygame.Surface, kind: str, clock: float, top: int = 0) -> None:
        """Rain or snow over the scene: one tile twice the scene's height
        (its halves are equal), one blit with a moving offset."""
        w, h = s.get_size()
        tile = self._tiles.get(kind + "2")
        if tile is None:
            half = self._tile(kind, (w, h))
            tile = pygame.Surface((w, 2 * h))
            tile.fill(KEY)
            tile.blit(half, (0, 0))
            tile.blit(half, (0, h))
            tile.set_colorkey(KEY, pygame.RLEACCEL)
            self._tiles[kind + "2"] = tile
        oy = int(clock * (150 if kind == "rain" else 26)) % h
        s.blit(tile, (0, oy - h))

    def draw_fog(self, s: pygame.Surface, horizon_y: int) -> None:
        """Dithered band over the far road and the skyline's foot."""
        band = self._tiles.get("fog")
        if band is None:
            bh = 50
            cell = pygame.Surface((8, bh))
            cell.fill(KEY)
            col = (62, 74, 86)
            for y in range(bh):
                d = abs(y - bh * 0.42) / (bh * 0.58)        # 0 at the horizon row
                for x in range(8):
                    if d < 0.3:
                        on = (x + y) % 2 == 0
                    elif d < 0.65:
                        on = (x + 2 * y) % 4 == 0
                    else:
                        on = (x + 3 * y) % 8 == 0
                    if on:
                        cell.set_at((x, y), col)
            band = pygame.Surface((self.w, bh))
            band.fill(KEY)
            for x in range(0, self.w, 8):
                band.blit(cell, (x, 0))
            band.set_colorkey(KEY, pygame.RLEACCEL)
            self._tiles["fog"] = band
        s.blit(band, (0, horizon_y - int(band.get_height() * 0.42)))

    # ── enclosed places ──────────────────────────────────────────────────────

    def draw_enclosure(self, s: pygame.Surface, place: str, row: Callable[[float], Tuple[float, float]],
                       shown: Callable[[float], float], odo: float, grid: RGB) -> None:
        """Tunnel / underground car park: frames marching towards the far end.

        ``row(world x) -> (floor row, px per metre)`` is the chase view's row
        function, ``shown(x)`` its depth mapping.
        """
        w = s.get_width()
        cx = w / 2.0
        tunnel = place == "tunnel"
        half = 5.2 if tunnel else 9.0           # wall distance from the road centre (m)
        tall = 5.6 if tunnel else 2.7           # ceiling height (m)
        pitch = 10.0
        lamp = WARM if tunnel else (150, 230, 220)
        line = pygame.draw.line
        x = -(odo % pitch) - pitch
        frames = []
        while x <= 100.0:
            if x > -6.5:
                fy, k = row(shown(x) if x > 0 else x)
                frames.append((fy, k))
            x += pitch
        prev = None
        for fy, k in frames:
            xl, xr = cx - k * half, cx + k * half
            top = fy - k * tall
            if tunnel:
                sh = k * 1.5                     # chamfered arch
                pts = [(xl, fy), (xl, top + sh), (xl + sh, top), (xr - sh, top), (xr, top + sh), (xr, fy)]
                pygame.draw.lines(s, grid, False, pts)
                ly = fy - k * 3.6                # a lamp on each wall
                r = 1 if k < 14 else 2
                pygame.draw.rect(s, lamp, (int(xl) - r + 1, int(ly), r + 1, r + 1))
                pygame.draw.rect(s, lamp, (int(xr) - 1, int(ly), r + 1, r + 1))
            else:
                # beam across, pillars, a strip light on the ceiling
                line(s, grid, (xl, top), (xr, top))
                for px in (-half, -half / 3.0, half / 3.0, half):
                    xx = cx + k * px
                    line(s, grid, (xx, top), (xx, fy))
                ln = max(2.0, k * 1.2)
                for px in (-half * 0.66, 0.0, half * 0.66):
                    xx = cx + k * px
                    line(s, lamp, (xx - ln, top + 1), (xx + ln, top + 1))
            if prev is not None:
                pfy, pk = prev
                ptop = pfy - pk * tall
                # ceiling edges, the foot of the walls and (tunnel) the lamp
                # rows run frame to frame towards the far end
                for sg in (-1.0, 1.0):
                    inset = 1.5 if tunnel else 0.0
                    line(s, grid, (cx + sg * pk * (half - inset), ptop),
                         (cx + sg * k * (half - inset), top))
                    line(s, grid, (cx + sg * pk * half, pfy), (cx + sg * k * half, fy))
                    if tunnel:
                        line(s, (96, 86, 48), (cx + sg * pk * half, pfy - pk * 3.6),
                             (cx + sg * k * half, fy - k * 3.6))
            prev = (fy, k)
