"""
The road picture beyond the camera: drawing of the fused road geometry (pygame).

roadsense's ``roadgeom`` topic joins the camera's lanes with the map's road in
the car's frame (docs/MFD_MAIN_SCREEN.md 6.7, 7.3). ``mapinfo.MapDirector``
holds it as ``d.map.geo`` when every gate of the map layer is open; here it
becomes part of the main road drawing, not a separate element:

    the ego lane continues as a ribbon from where the camera's lanes end
    (``geo.v``) along the map's path, up to the horizon;
    side roads leave the road as ribbons at their junction;
    a roundabout is a ring on the ground with its island and its arms, and
    the way through it is the ego lane's ribbon.

Ground mapping (chase view, fully open only). A ground point (x m ahead of the
camera, y m to the left) is drawn like every lane point of ``render.py``: its
row follows from x alone (logarithmic depth scale up to 100 m, then
logarithmic in x up to the horizon), its column is the road centre at that
distance in true perspective plus the offset from the centre in the displayed
scale. The road centre is the camera's lane centre up to ``geo.v`` and the
map's path beyond, for as long as the path runs forward (``geo.steer``); a
ring and everything behind a turn are drawn rigidly from there on.

Two calls per redraw: ``prepare`` (projects everything once; fills are drawn
under the floor lines like the ego lane's) and ``lines``.
"""

from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence, Tuple

import pygame

RGB = Tuple[int, int, int]
Pt = Tuple[float, float]

FAR_FROM_M = 100.0
FAR_END_UP = 3                  # the picture ends this many rows under the horizon
MIN_CONF = 0.5                  # the path is drawn while its confidence stays above this
RING_SEG = 16                   # polygon segments of a ring
X_BACK = -5.0                   # ground points are clamped to this far behind the camera
ARM_FILL = (8, 14, 30)          # the surface of a road that is not the car's way (render.pal["lane_side"])
ISLAND = (0, 0, 0)
ISLAND_MARK = (40, 70, 50)


def _k(c: RGB, k: float) -> RGB:
    return (int(c[0] * k), int(c[1] * k), int(c[2] * k))


class Projection:
    """Ground -> screen for one redraw (see the module docstring)."""

    def __init__(self, r: Any, d: Any, geo: Any) -> None:
        from .render import CAM_X, CHASE_F, CHASE_POS, compress
        self.r = r
        self.cx = r.w / 2.0
        self.compress = compress
        self.cam_x = CAM_X
        self.back = CHASE_POS[0]
        y100, k100 = r._row(compress(CAM_X + FAR_FROM_M))
        self.y100 = y100
        self.k100 = k100
        self.y_end = float(r.horizon_y + FAR_END_UP)
        self.span = math.log(max(1.5, float(getattr(r.tune, "map_far_max_m", 600)) / FAR_FROM_M))
        shown100 = compress(CAM_X + FAR_FROM_M) - CHASE_POS[0]
        # scale of the road centre: true perspective, continued beyond 100 m from its value there
        self.kc100 = k100 * shown100 / (CAM_X + FAR_FROM_M - CHASE_POS[0])
        self.depth0 = CAM_X - CHASE_POS[0]
        # the road centre by distance: the camera's (render._cen) up to geo.v, the path beyond
        xs = list(d.xs)
        cen = list(r._cen[:len(xs)])
        gx: List[float] = []
        gc: List[float] = []
        v = geo.v
        for x, c in zip(xs, cen):
            if x <= v + 1e-6:
                gx.append(x)
                gc.append(c)
        path = geo.path
        x0, y0 = path[0][0], path[0][1]
        cam_at = r._interp(xs, cen, x0) if cen else 0.0
        # the map's part is hung on the camera's lane centre where the two meet
        self.shift = shift = (cam_at - y0) if gx else 0.0
        for x, y, _ in path:
            if x > geo.steer + 1e-6:
                break
            if not gx or x > gx[-1] + 0.5:
                gx.append(x)
                gc.append(y + shift)
        if not gx:
            gx, gc = [0.0], [0.0]
        self.gx, self.gc = gx, gc

    def centre(self, x: float) -> float:
        gx, gc = self.gx, self.gc
        if x <= gx[0]:
            return gc[0]
        n = len(gx)
        if x >= gx[n - 1]:
            return gc[n - 1]
        lo, hi = 0, n - 1
        while hi - lo > 1:
            mid = (lo + hi) >> 1
            if gx[mid] <= x:
                lo = mid
            else:
                hi = mid
        t = (x - gx[lo]) / ((gx[hi] - gx[lo]) or 1.0)
        return gc[lo] + (gc[hi] - gc[lo]) * t

    def many(self, pts: Sequence[Pt]) -> List[Pt]:
        """Screen positions of ground points of the MAP's geometry (x ahead, y
        left). One loop with everything inline: this runs some 50-150 times
        per redraw on a Pi Zero (render.compress and SceneRenderer._row are
        repeated here, see there)."""
        from . import render as R
        r = self.r
        cx, shift = self.cx, self.shift
        gx, gc = self.gx, self.gc
        n = len(gx)
        g_last_x, g_last_c, g_first_x, g_first_c = gx[n - 1], gc[n - 1], gx[0], gc[0]
        cam_x, back = self.cam_x, self.back
        x0c, dn_i = R.COMP_X0, 1.0 / R._COMP_DN
        dslope = (dn_i - 1.0 / R.COMP_DFAR) / R._COMP_LN
        cs = R.COMP_S
        f0, fz, u0, uz, cy, F = r._row_f0, r._row_fz, r._row_u0, r._row_uz, r._chase_cy, R.CHASE_F
        y100, y_end, span, k100, kc100, depth0 = self.y100, self.y_end, self.span, self.k100, self.kc100, self.depth0
        log = math.log
        out: List[Pt] = []
        for x, y in pts:
            y += shift
            if x < X_BACK:
                x = X_BACK
            if x >= g_last_x:
                c = g_last_c
            elif x <= g_first_x:
                c = g_first_c
            else:
                lo, hi = 0, n - 1
                while hi - lo > 1:
                    mid = (lo + hi) >> 1
                    if gx[mid] <= x:
                        lo = mid
                    else:
                        hi = mid
                c = gc[lo] + (gc[hi] - gc[lo]) * (x - gx[lo]) / ((gx[hi] - gx[lo]) or 1.0)
            if x <= FAR_FROM_M:
                wx = cam_x + x
                rr = wx - x0c
                if rr <= 0.0:
                    dx = wx - back
                    ratio = 1.0
                else:
                    dx = 1.0 / (dn_i - dslope * log(1.0 + rr / cs))
                    ratio = dx / (wx - back)
                k = F / (dx * f0 + fz)
                sy = cy - k * (dx * u0 + uz)
                sx = cx - k * ((y - c) + c * ratio)
            else:
                t = log(x / FAR_FROM_M) / span
                if t > 1.0:
                    t = 1.0
                sy = y100 - (y100 - y_end) * t
                sx = cx - (k100 * FAR_FROM_M / x * (y - c) + kc100 * (FAR_FROM_M + depth0) / (x + depth0) * c)
            if sx < -3000.0:
                sx = -3000.0
            elif sx > 3000.0:
                sx = 3000.0
            out.append((sx, sy))
        return out


def _ribbon(pr: Projection, pts: Sequence[Pt], half: float) -> Tuple[List[Pt], List[Pt]]:
    """Left and right edge (screen) of a road of half-width ``half`` along a
    ground polyline (map geometry)."""
    g: List[Pt] = []
    n = len(pts)
    for i in range(n):
        ax, ay = pts[i - 1] if i > 0 else pts[i]
        bx, by = pts[i + 1] if i < n - 1 else pts[i]
        dx, dy = bx - ax, by - ay
        ln = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / ln * half, dx / ln * half        # to the left of the direction of travel
        x, y = pts[i]
        g.append((x + nx, y + ny))
        g.append((x - nx, y - ny))
    sc = pr.many(g)
    return sc[0::2], sc[1::2]


def _circle(pr: Projection, cx: float, cy: float, rad: float) -> Optional[List[Pt]]:
    """A circle on the ground as a screen polygon, cut off behind the car
    (ground x >= X_BACK); None when nothing of it is ahead."""
    g: List[Pt] = []
    for i in range(RING_SEG):
        a = 2.0 * math.pi * i / RING_SEG
        g.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
    if min(q[0] for q in g) < X_BACK:
        cut: List[Pt] = []
        for i in range(RING_SEG):
            a, b = g[i - 1], g[i]
            ina, inb = a[0] >= X_BACK, b[0] >= X_BACK
            if ina != inb:
                t = (X_BACK - a[0]) / (b[0] - a[0])
                cut.append((X_BACK, a[1] + (b[1] - a[1]) * t))
            if inb:
                cut.append(b)
        g = cut
    if len(g) < 3:
        return None
    return pr.many(g)


class Prepared:
    __slots__ = ("way", "way_split", "way_end", "arms", "ring_out", "ring_in", "end")


def prepare(r: Any, d: Any) -> Optional[Prepared]:
    """Project the geometry held by the director; None when there is none (or
    the view is not the open chase view)."""
    geo = d.map.geo
    if geo is None or not r._fast or d.vision != "live":
        return None
    cen = r._cen
    cache = r.__dict__.get("_geo_cache")
    if cache is not None and cache[0] is geo and cache[3] == bool(d.lanes[1] or d.lanes[2]):
        # the same geometry as at the last redraw (the scene comes 10 times a second, the
        # picture is redrawn up to 20 times): the projection is kept; the camera's lanes have
        # moved by a fraction of their 0.3 s low-pass since
        for i, c in cache[2]:
            if i < len(cen):
                cen[i] = c
        return cache[1]
    pr = Projection(r, d, geo)
    # beyond the camera's reach everything that stands on the road (the lead, objects, the
    # roadside) follows the fused centre line
    tail = []
    for i, x in enumerate(d.xs):
        if x > geo.v and i < len(cen):
            cen[i] = pr.centre(x)
            tail.append((i, cen[i]))
    out = Prepared()
    r.__dict__["_geo_cache"] = (geo, out, tail, bool(d.lanes[1] or d.lanes[2]))
    # the way on: the path while it is known well enough
    pts: List[Pt] = []
    if not (d.lanes[1] or d.lanes[2]) and geo.path[0][0] > 3.0:
        # the camera has no lane line (in a junction, on a ring): the ribbon starts at the car,
        # along the centre the camera still gives (its planned path), in the map's style
        x0 = geo.path[0][0]
        for x in (0.0, 4.0, 8.0, 12.0, 16.0, 20.0, 30.0, 40.0, 50.0, 65.0, 80.0):
            if x >= x0 - 2.0:
                break
            pts.append((x, pr.centre(x) - pr.shift))
    for x, y, q in geo.path:
        if q < MIN_CONF and len(pts) >= 2:
            break
        pts.append((x, y))
    cut_short = len(pts) < len(geo.path)
    out.way = _ribbon(pr, pts, geo.width * 0.5) if len(pts) >= 2 else None
    out.way_split = next((i for i, p in enumerate(pts) if p[0] > FAR_FROM_M), len(pts))
    out.end = "" if cut_short else geo.end
    out.way_end = pr.many(pts[-1:])[0] if pts else None
    arms = []
    for b in geo.branches:
        if b.minor and b.pts[0][0] > 60.0:
            continue
        arms.append((b, _ribbon(pr, b.pts, b.width * 0.5)))
    out.arms = arms
    out.ring_out = out.ring_in = None
    if geo.ring is not None:
        cx, cy, rad, w, _on = geo.ring
        ro, ri = rad + w * 0.5, max(1.0, rad - w * 0.5)
        out.ring_out = _circle(pr, cx, cy, ro)
        out.ring_in = _circle(pr, cx, cy, ri)
    return out


def fills(r: Any, s: pygame.Surface, p: Optional[Prepared], d: Any, k: float) -> None:
    """Road surfaces, under the floor lines: side roads and the ring in the
    tint of a neighbour lane, the way on in the ego lane's."""
    if p is None:
        return
    polygon = pygame.draw.polygon
    pal = r.pal
    arm = _k(ARM_FILL, k)
    way_on = r.tune.road_geo_way
    for b, (left, right) in p.arms:
        if b.exit or b.ring:
            polygon(s, _k(pal["lane_fill"], k) if (b.way and way_on) else arm, left + right[::-1])
    if p.ring_out is not None:
        polygon(s, arm, p.ring_out)
    if p.way is not None:
        left, right = p.way
        polygon(s, _k(pal["lane_fill"], 0.9 * k), left + right[::-1])
    if p.ring_in is not None:
        polygon(s, ISLAND, p.ring_in)


def lines(r: Any, s: pygame.Surface, p: Optional[Prepared], d: Any, k: float) -> None:
    """Edges: side roads and the ring in the road-edge colour, the way on as
    lane lines (dimmer than the camera's: they are the map's)."""
    if p is None:
        return
    pal = r.pal
    draw_lines = pygame.draw.lines
    edge = _k(pal["edge"], 0.85 * k)
    edge_dim = _k(pal["edge"], 0.45 * k)
    way_on = r.tune.road_geo_way
    for b, (left, right) in p.arms:
        if b.way and way_on:
            col = _k(pal["lane_ego"], k)
        else:
            col = edge if (b.exit and not b.minor) else edge_dim
        draw_lines(s, col, False, left)
        draw_lines(s, col, False, right)
        if not b.exit and len(left) >= 2:
            # cannot be driven into: a bar across its mouth
            i = min(1, len(left) - 1)
            pygame.draw.line(s, _k(pal["red"], 0.8 * k), left[i], right[i])
    if p.ring_out is not None:
        draw_lines(s, edge, True, p.ring_out)
    if p.ring_in is not None:
        draw_lines(s, edge, True, p.ring_in)
        xs_ = [q[0] for q in p.ring_in]
        ys_ = [q[1] for q in p.ring_in]
        w_i, h_i = max(xs_) - min(xs_), max(ys_) - min(ys_)
        if w_i >= 12 and h_i >= 5:
            # the island: a mark in its middle so that it reads as a body, not a hole
            mx, my = (max(xs_) + min(xs_)) * 0.5, (max(ys_) + min(ys_)) * 0.5
            hw, hh = max(2.0, w_i * 0.22), max(1.0, h_i * 0.22)
            draw_lines(s, _k(ISLAND_MARK, k), True, [(mx - hw, my), (mx, my - hh), (mx + hw, my), (mx, my + hh)])
    if p.way is not None:
        left, right = p.way
        n = p.way_split
        near = _k(pal["lane_mid"], k)
        far = _k(pal["lane_dim"], k)
        if n >= 2:
            draw_lines(s, near, False, left[:n])
            draw_lines(s, near, False, right[:n])
        if len(left) - n >= 1:
            a = max(n - 1, 0)
            if len(left) - a >= 2:
                draw_lines(s, far, False, left[a:])
                draw_lines(s, far, False, right[a:])
        if p.end in ("d", "n") and p.way_end is not None:
            ex, ey = p.way_end
            hw = max(3.0, abs(right[-1][0] - left[-1][0]) * 0.5 + 2.0)
            pygame.draw.line(s, _k(pal["lane_mid"], k), (ex - hw, ey), (ex + hw, ey))
