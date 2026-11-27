"""
Map layer of the main-screen scene: drawing (pygame).

Draws what ``mapinfo.MapDirector`` (``d.map``) holds; ``render.py`` calls one
function per place on the screen. Nothing here decides anything: thresholds,
confidence gates and hysteresis are the director's. With no usable map the
director holds nothing and every function returns without drawing.

    far_road        the road beyond camera range, 100 m .. the horizon
    world_items     the next limit as a sign at the roadside (within 100 m)
    next_limit_top  the next limit with its distance in the top line
    lane_boxes      lane boxes from the map's lane count
    corner          bottom-left: tunnel exit distance, road number plate
    mark            the map pin under the link LED
    junction        the junction / roundabout ahead as a schematic, left edge
    portal          the tunnel's end within 100 m

1-bit like the rest of the scene: no anti-aliasing, cached sprites and text.
"""

from __future__ import annotations

import math
from typing import Any, Tuple

import pygame

from .mapinfo import FAR_FROM_M, nav_active

RGB = Tuple[int, int, int]

ONCOMING_FILL = (26, 8, 30)        # an oncoming neighbour lane (own direction: pal["lane_side"])
ONCOMING_BOX = (128, 56, 150)
PLATE_RED = (196, 30, 40)
PLATE_YELLOW = (236, 200, 40)
PORTAL_FILL = (22, 38, 44)
FAR_END_UP = 3                     # the far road ends this many rows under the horizon
NEXT_R = 12                        # radius of the next-limit ring in the top line
PIN = ((1, 0), (2, 0), (3, 0), (0, 1), (4, 1), (0, 2), (4, 2), (0, 3), (4, 3), (1, 4), (3, 4), (2, 5), (2, 6))


def _k(c: RGB, k: float) -> RGB:
    return (int(c[0] * k), int(c[1] * k), int(c[2] * k))


def _cache(r: Any) -> dict:
    c = r.__dict__.get("_map_sprites")
    if c is None:
        c = r.__dict__["_map_sprites"] = {}
    return c


def dist_text(m: float) -> Tuple[str, str]:
    """(number, unit) of a distance ahead: 10 m steps, kilometres from 1000 m."""
    if m >= 995.0:
        return "%.1f" % (m / 1000.0), "km"
    return "%d" % (int((m + 5.0) // 10.0) * 10), "m"


# ── far road ─────────────────────────────────────────────────────────────────

def far_road(r: Any, s: pygame.Surface, d: Any) -> None:
    """The map's road from where the camera's lanes end (100 m) to the horizon.

    Rows: logarithmic in the distance between the 100 m row and a few rows
    under the horizon. Columns: true perspective of the lateral offset, the
    scale the scene uses for the road's centre line, anchored on the far end
    of the camera's centre line, so the ribbon continues the ego lane. Width:
    the ego lane's at 100 m, falling with 1 / distance."""
    m = d.map
    far = m.far
    if far is None or m.far_alpha <= 0.0 or not r._cen:
        return
    from .render import CAM_X, CHASE_F, CHASE_POS, compress
    geo = _cache(r).get("far_geo")
    if geo is None:
        y100, k100 = r._row(compress(CAM_X + FAR_FROM_M))
        geo = _cache(r)["far_geo"] = (y100, k100 * 1.75, float(r.horizon_y + FAR_END_UP))
    y100, hw100, y_end = geo
    span = math.log(max(1.5, m.far_max_m / FAR_FROM_M))
    adv = m.adv
    cx = r.w / 2.0
    w = r.w
    depth0 = CAM_X - CHASE_POS[0]
    anchor = None
    prev = None
    left = []
    right = []
    log = math.log
    for x, y in far.pts:
        f = x - adv
        if f >= FAR_FROM_M:
            if anchor is None:
                if prev is None:
                    return
                pf, py = prev
                y_at = py + (y - py) * (FAR_FROM_M - pf) / ((f - pf) or 1.0)
                anchor = r._cen[-1] - y_at
                px = cx - CHASE_F / (FAR_FROM_M + depth0) * r._cen[-1]
                left.append((px - hw100, y100))
                right.append((px + hw100, y100))
                last_f = FAR_FROM_M
            if f <= last_f + 4.0:
                break                              # the road turns back towards the car
            last_f = f
            t = log(f / FAR_FROM_M) / span
            if t > 1.0:
                break
            sy = y100 - (y100 - y_end) * t
            px = cx - CHASE_F / (f + depth0) * (y + anchor)
            hw = hw100 * FAR_FROM_M / f
            left.append((px - hw, sy))
            right.append((px + hw, sy))
            if px < -30.0 or px > w + 30.0:
                break
        prev = (f, y)
    if len(left) < 2:
        return
    a = m.far_alpha
    pal = r.pal
    pygame.draw.polygon(s, _k(pal["lane_fill"], 0.8 * a), left + right[::-1])
    col = _k(pal["lane_dim"], a)
    pygame.draw.lines(s, col, False, left)
    pygame.draw.lines(s, col, False, right)
    ex = (left[-1][0] + right[-1][0]) * 0.5
    ey = left[-1][1]
    if far.end == "roundabout":
        rx = max(4, int(right[-1][0] - left[-1][0]) // 2 + 3)
        pygame.draw.ellipse(s, (0, 0, 0), (int(ex) - rx, int(ey) - 3, 2 * rx + 1, 5))
        pygame.draw.ellipse(s, _k(pal["lane_mid"], a), (int(ex) - rx, int(ey) - 3, 2 * rx + 1, 5), 1)
    elif far.end == "dead_end":
        hw = max(3, int(right[-1][0] - left[-1][0]) // 2 + 2)
        pygame.draw.line(s, _k(pal["lane_mid"], a), (ex - hw, ey), (ex + hw, ey))


# ── next speed limit ─────────────────────────────────────────────────────────

def _next_ring(r: Any, kmh: int, weak: bool) -> pygame.Surface:
    """The next limit: a dashed ring (not in effect yet), dim for a limit that
    only follows from a rule."""
    key = ("next", kmh, weak)
    spr = _cache(r).get(key)
    if spr is None:
        n = NEXT_R
        size = 2 * n + 1
        spr = pygame.Surface((size, size))
        spr.fill((0, 0, 0))
        col = (150, 30, 34) if weak else r.pal["red"]
        for i in range(720):
            ang = i * math.pi / 360.0 + math.pi / 16.0
            if int(i / 45.0) % 2:
                continue
            for rr in ((n,) if weak else (n, n - 1)):
                spr.set_at((int(round(n + rr * math.cos(ang))), int(round(n + rr * math.sin(ang)))), col)
        txt = r.text(str(kmh), 14, r.pal["dim"] if weak else r.pal["text"])
        spr.blit(txt, (n - txt.get_width() // 2 + (1 if kmh < 100 else 0), n - txt.get_height() // 2))
        _cache(r)[key] = spr
    return spr


def next_limit_top(r: Any, s: pygame.Surface, d: Any, y: int, x_left: int, x_right: int) -> Tuple[int, int]:
    """The next limit and its distance in the top line (``y`` = the sign
    row's top). Returns the row's bounds left for the signs."""
    nl = d.map.next_limit
    mode = d.map.next_mode
    if nl is None or mode == "off":
        return x_left, x_right
    spr = _next_ring(r, nl.kmh, nl.src != "maxspeed")
    num, _unit = dist_text(nl.m)
    t = r.text(num, 14, r.pal["text"])
    ry = y + 9 - NEXT_R
    if mode == "beside":
        x = x_right - spr.get_width()
        s.blit(spr, (x, ry))
        x -= t.get_width() + 3
        pygame.draw.rect(s, (0, 0, 0), (x - 1, y + 3, t.get_width() + 2, 14))
        s.blit(t, (x, y + 3))
        return x_left, x - 5
    s.blit(spr, (x_left, ry))
    x = x_left + spr.get_width() + 2
    pygame.draw.rect(s, (0, 0, 0), (x - 1, y + 3, t.get_width() + 2, 14))
    s.blit(t, (x, y + 3))
    return x + t.get_width() + 7, x_right


def world_items(r: Any, s: pygame.Surface, d: Any, items: list) -> None:
    """Adds the map's world objects to the far-to-near draw list: the next
    limit as a sign on a post at the right road edge, within camera range."""
    nl = d.map.next_limit
    if nl is None or not r.tune.map_next_limit_road or not r._fast or not 3.0 <= nl.m <= 100.0:
        return
    items.append((nl.m, lambda: _road_sign(r, s, d, nl)))


def _road_sign(r: Any, s: pygame.Surface, d: Any, nl: Any) -> None:
    x = nl.m
    edge, line = d.edges[1], d.lanes[2]
    if edge:
        y = r._interp(d.xs, edge, x) - 1.0
    elif line:
        y = r._interp(d.xs, line, x) - 2.2
    else:
        y = -4.5
    g = r._place(None, x, y, 1.0)
    if g is None:
        return
    cx, by, ppm = g
    pal = r.pal
    mast = max(9.0, 2.4 * ppm)
    rad = max(3, min(9, int(0.6 * ppm)))
    pygame.draw.line(s, pal["lane_dim"], (cx, by), (cx, by - mast))
    c = (int(cx), int(by - mast) - rad)
    pygame.draw.circle(s, (0, 0, 0), c, rad)
    weak = nl.src != "maxspeed"
    pygame.draw.circle(s, (150, 30, 34) if weak else pal["red"], c, rad, 2 if (rad >= 6 and not weak) else 1)
    if rad >= 7:
        t = r.text(str(nl.kmh), 8, pal["text"], mono=False)
        if t.get_width() <= 2 * rad - 3:
            s.blit(t, (c[0] - t.get_width() // 2 + 1, c[1] - t.get_height() // 2))


# ── lanes ────────────────────────────────────────────────────────────────────

def _lanes_ok(r: Any, d: Any):
    """The map's lane layout unless the camera contradicts it (it sees a lane
    where the map has none): (boxes, camera neighbour left, right) or None."""
    lb = d.map.lanes
    if lb is None:
        return None
    nl, nr = r.lane_neighbours(d)
    if lb.ego >= 0 and ((nl and lb.ego == 0 and lb.back == 0) or (nr and lb.ego >= lb.fwd - 1)):
        return None
    return lb, nl, nr


def left_is_oncoming(r: Any, d: Any) -> bool:
    """The neighbour lane seen on the left carries oncoming traffic."""
    return d.map.onc_left and _lanes_ok(r, d) is not None


def _lane_arrow(s: pygame.Surface, col: RGB, x: int, y: int, codes: str) -> None:
    """The painted arrows of one lane inside its box (7 x 11 px at x, y), pixel by pixel: a stem from the
    bottom with a head pointing up for "t" (straight on), a branch with a head to the left / right for "l" /
    "r" ("u", turning back, is drawn as left; "m", a merge, has no picture)."""
    left, right, up = ("l" in codes or "u" in codes), "r" in codes, "t" in codes
    if not (left or right or up):
        return
    px = set()
    if up or (left and right):
        cx = 3
        px |= {(cx, r) for r in range(1 if up else 5, 11)}
        if up:
            px |= {(3, 0), (2, 1), (4, 1), (1, 2), (2, 2), (4, 2), (5, 2)}
        for on, sg in ((left, -1), (right, 1)):
            if on:                                   # a branch up and out at 45 degrees, its head a corner at the tip
                px |= {(cx + sg, 7), (cx + 2 * sg, 6), (cx + 3 * sg, 5),
                       (cx + 2 * sg, 5), (cx + sg, 5), (cx + 3 * sg, 6), (cx + 3 * sg, 7)}
    else:
        sg = -1 if left else 1                       # a single turn: the stem on the far side, a full head
        cx = 3 - sg
        px |= {(cx, r) for r in range(4, 11)}
        px |= {(cx + sg * k, 4) for k in range(1, 5)}
        px |= {(cx + 3 * sg, 3), (cx + 3 * sg, 5), (cx + 2 * sg, 2), (cx + 2 * sg, 3), (cx + 2 * sg, 5), (cx + 2 * sg, 6)}
    for dx, dy in px:
        s.set_at((x + dx, y + dy), col)


def lane_boxes(r: Any, s: pygame.Surface, d: Any) -> bool:
    """Lane boxes from the map: one per lane of the own direction (the own
    one lit) when it has two or more, oncoming lanes (at most two) as dim
    boxes with a pointer towards the car. Returns False when the map has
    nothing to say (the caller then draws the camera's boxes as before)."""
    if not nav_active(d.map):
        return False                # no navigation route: the map's boxes are not drawn (mapinfo.NAV_ONLY)
    ok = _lanes_ok(r, d)
    if ok is None:
        return False
    lb, nl, nr = ok
    if lb.fwd < 2 and not nl:
        return True                 # one lane each way, no lane seen beside: nothing, as before
    if not r.tune.lane_indicator:
        return True
    fwd = min(lb.fwd, 5)
    ego = min(lb.ego, fwd - 1)                  # -1: the own lane is not known (L20): no lane lit
    likely = min(getattr(lb, "likely", -1), fwd - 1)
    back = min(lb.back, 2 if fwd <= 4 else 1)
    bw, bh, gap, sep = 9, 14, 3, 3
    x = r.w - 5 - getattr(r, "right_inset", 0) - (fwd + back) * (bw + gap) + gap - (sep if back else 0)
    y = r.h - 19
    pal = r.pal
    for _ in range(back):
        pygame.draw.rect(s, (0, 0, 0), (x, y, bw, bh))
        pygame.draw.rect(s, ONCOMING_BOX, (x, y, bw, bh), 1)
        pygame.draw.polygon(s, ONCOMING_BOX, [(x + 2, y + 5), (x + 6, y + 5), (x + 4, y + 8)])
        x += bw + gap
    if back:
        x += sep
    turn = getattr(lb, "turn", ()) if len(getattr(lb, "turn", ())) == lb.fwd else ()
    tpath = getattr(lb, "path", ()) if len(getattr(lb, "path", ())) == lb.fwd else ()
    x_first = x
    for i in range(fwd):
        if i == ego:
            pygame.draw.rect(s, pal["lane_ego"], (x, y, bw, bh))
        elif ego < 0 and i == likely:
            # probably this one: an outline in the own-lane colour, not filled
            pygame.draw.rect(s, (0, 0, 0), (x, y, bw, bh))
            pygame.draw.rect(s, pal["lane_ego"], (x, y, bw, bh), 1)
            pygame.draw.rect(s, pal["lane_ego"], (x + 3, y + bh - 4, bw - 6, 2))
        else:
            pygame.draw.rect(s, (0, 0, 0), (x, y, bw, bh))
            pygame.draw.rect(s, pal["lane_mid"], (x, y, bw, bh), 1)
        x += bw + gap
    # painted turn arrows near a junction (roadsense view.road, from the map's turn lanes): black on the lit
    # lane, bright on the lanes that lead where the most likely path goes, dim on the others
    for i in range(fwd if turn else 0):
        col = (0, 0, 0) if i == ego else pal["lane_ego"] if (tpath and tpath[i]) else pal["lane_mid"]
        _lane_arrow(s, col, x_first + i * (bw + gap) + 1, y + 1, turn[i])
    return True


# ── bottom-left corner: tunnel exit, road plate ──────────────────────────────

def _plate(r: Any, label: Any) -> pygame.Surface:
    """A road number as the plate of a Polish road-number sign: white on red
    for motorways, expressways and national roads, black on yellow for
    voivodeship roads, an outline for the rest."""
    key = ("plate", label.ref, label.cls)
    spr = _cache(r).get(key)
    if spr is None:
        cls = label.cls.replace("_link", "")
        if cls in ("motorway", "trunk", "primary"):
            bg, fg = PLATE_RED, (255, 255, 255)
        elif cls == "secondary":
            bg, fg = PLATE_YELLOW, (0, 0, 0)
        else:
            bg, fg = None, r.pal["value"]
        t = r.text(label.ref[:8], 14, fg)
        spr = pygame.Surface((t.get_width() + 7, 17))
        spr.fill(bg or (0, 0, 0))
        if bg is None:
            pygame.draw.rect(spr, r.pal["lane_mid"], spr.get_rect(), 1)
        spr.blit(t, (4, 1))
        _cache(r)[key] = spr
    return spr


def corner(r: Any, s: pygame.Surface, d: Any, y: int) -> None:
    """Rows above the lamp chips / perception status (``y`` = top of the
    first free row, rows are 19 px): the tunnel's exit distance, the road
    plate."""
    m = d.map
    pal = r.pal
    if m.tunnel_exit_m is not None:
        num, unit = dist_text(m.tunnel_exit_m)
        t = r.text(num, 14, pal["text"])
        u = r.text(unit, 8, pal["label"], mono=False)
        wide = 20 + t.get_width() + 2 + u.get_width()
        pygame.draw.rect(s, (0, 0, 0), (3, y, wide + 3, 17))
        # the portal: an arch with the light at its end
        ax, ay = 4, y + 2
        pygame.draw.lines(s, pal["lane_mid"], False,
                          [(ax, ay + 13), (ax, ay + 3), (ax + 3, ay), (ax + 11, ay), (ax + 14, ay + 3), (ax + 14, ay + 13)])
        pygame.draw.polygon(s, pal["text"], [(ax + 5, ay + 13), (ax + 5, ay + 7), (ax + 7, ay + 5),
                                             (ax + 8, ay + 5), (ax + 10, ay + 7), (ax + 10, ay + 13)])
        s.blit(t, (24, y + 1))
        s.blit(u, (24 + t.get_width() + 2, y + 8))
        y -= 19
    lab = m.label
    if lab is not None:
        x = 4
        if lab.ref:
            spr = _plate(r, lab)
            s.blit(spr, (x, y))
            x += spr.get_width() + 5
        if lab.name:
            name = lab.name if len(lab.name) <= 18 else lab.name[:17].rstrip() + "."
            t = r.text(name, 14, pal["label"])
            pygame.draw.rect(s, (0, 0, 0), (x - 1, y + 1, t.get_width() + 2, 15))
            s.blit(t, (x, y + 1))


# ── map pin ──────────────────────────────────────────────────────────────────

def mark(r: Any, s: pygame.Surface, d: Any) -> None:
    """A 5 x 7 pin under the link LED: the map is in use (amber while the
    position is dead-reckoned). Nothing without a usable map."""
    st = d.map.mark
    if not st:
        return
    col = r.pal["warn"] if st == "dr" else r.pal["label"]
    x0, y0 = r.w - 12, 21
    for dx, dy in PIN:
        s.set_at((x0 + dx, y0 + dy), col)
    if st == "good":
        s.set_at((x0 + 2, y0 + 2), col)


# ── tunnel end ───────────────────────────────────────────────────────────────

def tunnel_end(d: Any) -> float:
    """Distance at which the tunnel's frames end (100 = beyond the picture)."""
    m = d.map.tunnel_exit_m
    return m if (m is not None and m < 100.0) else 100.0


def portal(r: Any, s: pygame.Surface, d: Any, end: float) -> None:
    """The tunnel's mouth at ``end`` metres: the last frame, bright, with the
    open air in it."""
    if end >= 100.0 or end < 1.0:
        return
    from .render import compress
    fy, k = r._row(compress(end))
    cx = r.w / 2.0
    half, tall, sh = 5.2 * k, 5.6 * k, 1.5 * k
    pts = [(cx - half, fy), (cx - half, fy - tall + sh), (cx - half + sh, fy - tall),
           (cx + half - sh, fy - tall), (cx + half, fy - tall + sh), (cx + half, fy)]
    pygame.draw.polygon(s, PORTAL_FILL, pts)
    pygame.draw.lines(s, r.pal["text"], False, pts)


# ── junction view ────────────────────────────────────────────────────────────

JV_X, JV_Y, JV_W, JV_H = 3, 62, 58, 58      # the panel (scene pixels); the distance stands under it
JV_ARM = 23                                 # arm length from the centre (px)
JV_RING = 10                                # roundabout ring radius (px)


_PLAIN = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")


def _dir(angle: float) -> Tuple[float, float]:
    """Screen direction of an arm angle (0 = up = straight on, positive = right)."""
    a = math.radians(angle)
    return math.sin(a), -math.cos(a)


def _thick(s: pygame.Surface, col: RGB, a: Tuple[float, float], b: Tuple[float, float], w: int) -> None:
    """A line of width w without anti-aliasing (w parallel 1 px lines)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy) or 1.0
    px, py = -dy / n, dx / n
    for k in range(w):
        o = k - (w - 1) / 2.0
        pygame.draw.line(s, col, (int(round(a[0] + px * o)), int(round(a[1] + py * o))),
                         (int(round(b[0] + px * o)), int(round(b[1] + py * o))))


def _dashed(s: pygame.Surface, col: RGB, a: Tuple[float, float], b: Tuple[float, float]) -> None:
    n = int(math.hypot(b[0] - a[0], b[1] - a[1]))
    for i in range(0, n, 4):
        t0, t1 = i / n, min(1.0, (i + 2) / n)
        pygame.draw.line(s, col, (int(a[0] + (b[0] - a[0]) * t0), int(a[1] + (b[1] - a[1]) * t0)),
                         (int(a[0] + (b[0] - a[0]) * t1), int(a[1] + (b[1] - a[1]) * t1)))


def _arrow(s: pygame.Surface, col: RGB, tip: Tuple[float, float], d: Tuple[float, float], size: int = 4) -> None:
    px, py = -d[1], d[0]
    bx, by = tip[0] - d[0] * size, tip[1] - d[1] * size
    pygame.draw.polygon(s, col, [(int(round(tip[0])), int(round(tip[1]))),
                                 (int(round(bx + px * size * 0.8)), int(round(by + py * size * 0.8))),
                                 (int(round(bx - px * size * 0.8)), int(round(by - py * size * 0.8)))])


def _arm_width(a: Any) -> int:
    return 3 if a.rank == 0 else 2 if a.rank == 1 else 1


def _control_icon(r: Any, s: pygame.Surface, x: int, y: int, ctl: str) -> None:
    pal = r.pal
    if ctl == "s":                          # a three-lamp head
        pygame.draw.rect(s, (0, 0, 0), (x + 1, y, 7, 15))
        pygame.draw.rect(s, pal["label"], (x + 1, y, 7, 15), 1)
        for i, c in enumerate((pal["red"], pal["warn"], pal["green"])):
            pygame.draw.rect(s, c, (x + 3, y + 2 + 4 * i, 3, 3))
    elif ctl == "g":                        # give way: a triangle standing on its tip
        pygame.draw.polygon(s, pal["warn"], [(x, y), (x + 8, y), (x + 4, y + 7)], 1)
    elif ctl == "t":                        # stop: a filled octagon
        pygame.draw.polygon(s, pal["red"], [(x + 2, y), (x + 6, y), (x + 8, y + 2), (x + 8, y + 6),
                                            (x + 6, y + 8), (x + 2, y + 8), (x, y + 6), (x, y + 2)])


def junction(r: Any, s: pygame.Surface, d: Any) -> None:
    """The junction or roundabout ahead on the most likely path, as a schematic
    seen from above with the car coming from below: every arm in its direction
    (wide = a main road, dashed = cannot be driven into), the most likely way
    on bright with an arrow head, what controls it in the corner, the distance
    under it. A roundabout: the ring, the way round it to the exit of the most
    likely path, that exit's number in the middle, the car as a dot once it is
    on the ring. Nothing without ``d.map.junction``."""
    jv = d.map.junction
    if jv is None or not nav_active(d.map):     # no navigation route: no schematic (mapinfo.NAV_ONLY)
        return
    if not getattr(r.tune, "map_junction_way", True):
        jv = jv._replace(arms=tuple(a._replace(path=False) for a in jv.arms), ring_path=0, to="")
    pal = r.pal
    x0, y0 = JV_X, JV_Y
    approx = bool(getattr(d.map, "junction_approx", False))
    pygame.draw.rect(s, (0, 0, 0), (x0, y0, JV_W, JV_H + 16))
    # an approximate position (dead reckoning, a weak receiver): amber frame, "~" before the distance
    pygame.draw.rect(s, pal["warn"] if approx else pal["lane_dim"], (x0, y0, JV_W, JV_H), 1)
    cx, cy = x0 + JV_W // 2, y0 + JV_H // 2 + (3 if jv.kind == "j" else 0)
    dim, mid, hot = pal["lane_dim"], _k(pal["lane_mid"], 0.62), pal["lane_ego"]
    clip = s.get_clip()
    s.set_clip((x0 + 1, y0 + 1, JV_W - 2, JV_H - 2))
    if jv.kind == "j":
        # arms first (dim), then the way in and the way on (bright) over them
        for a in jv.arms:
            if a.path:
                continue
            dx, dy = _dir(a.angle)
            end = (cx + dx * JV_ARM, cy + dy * JV_ARM)
            if not a.exit:
                _dashed(s, dim, (cx, cy), end)
            else:
                _thick(s, dim if a.minor else mid, (cx, cy), end, _arm_width(a))
        _thick(s, hot, (cx, cy + JV_ARM + 4), (cx, cy), 3)
        for a in jv.arms:
            if a.path:
                dx, dy = _dir(a.angle)
                end = (cx + dx * (JV_ARM - 3), cy + dy * (JV_ARM - 3))
                _thick(s, hot, (cx, cy), end, 3)
                _arrow(s, hot, (cx + dx * (JV_ARM + 2), cy + dy * (JV_ARM + 2)), (dx, dy), 5)
    else:
        rr = JV_RING
        for a in jv.arms:
            dx, dy = _dir(a.angle)
            p0 = (cx + dx * rr, cy + dy * rr)
            p1 = (cx + dx * (rr + 14), cy + dy * (rr + 14))
            if a.path or (a.approach and not jv.on_ring):
                continue
            if not a.exit and not a.approach:
                _dashed(s, dim, p0, p1)
            else:
                _thick(s, dim if a.minor else mid, p0, p1, min(2, _arm_width(a)))
        pygame.draw.circle(s, mid, (cx, cy), rr, 1)
        # the way: in from below, round the ring against the clock, out at the path's exit
        start = 180.0 if jv.car_angle is None or not jv.on_ring else jv.car_angle
        out_arm = next((a for a in jv.arms if a.path), None)
        if not jv.on_ring:
            _thick(s, hot, (cx, cy + rr + 16), (cx, cy + rr), 3)
        if out_arm is not None:
            sweep = (start - out_arm.angle) % 360.0 or 360.0
            for k in range(0, int(sweep) + 1, 4):
                dx, dy = _dir(start - k)
                pygame.draw.rect(s, hot, (int(round(cx + dx * rr)) - 1, int(round(cy + dy * rr)) - 1, 2, 2))
            dx, dy = _dir(out_arm.angle)
            _thick(s, hot, (cx + dx * rr, cy + dy * rr), (cx + dx * (rr + 9), cy + dy * (rr + 9)), 3)
            _arrow(s, hot, (cx + dx * (rr + 15), cy + dy * (rr + 15)), (dx, dy), 5)
        if jv.on_ring and jv.car_angle is not None:
            dx, dy = _dir(jv.car_angle)
            pygame.draw.circle(s, pal["text"], (int(round(cx + dx * rr)), int(round(cy + dy * rr))), 2)
        if jv.ring_path:
            t = r.text("%d" % jv.ring_path, 14, pal["text"])
            s.blit(t, (cx - t.get_width() // 2 + 1, cy - 7))
    s.set_clip(clip)
    if jv.control:
        _control_icon(r, s, x0 + JV_W - 11, y0 + 3, jv.control)
    # under the panel: the distance, and the road the way on takes
    if not jv.on_ring:
        num, unit = dist_text(jv.m)
        t = r.text(("~" if approx else "") + num, 14, pal["warn"] if approx else pal["text"])
        u = r.text(unit, 8, pal["label"], mono=False)
        s.blit(t, (x0 + 1, y0 + JV_H + 1))
        s.blit(u, (x0 + 3 + t.get_width(), y0 + JV_H + 8))
        tx = x0 + 3 + t.get_width() + u.get_width() + 4
    else:
        tx = x0 + 1
    if jv.to and getattr(r.tune, "map_junction_name", True):
        room = (JV_W + 40 - (tx - x0)) // 6
        to = jv.to.translate(_PLAIN)       # the 8 px font has no Polish letters
        name = to if len(to) <= room else to[:max(1, room - 1)].rstrip() + "."
        n = r.text(name, 8, pal["label"], mono=False)
        pygame.draw.rect(s, (0, 0, 0), (tx - 1, y0 + JV_H + 6, n.get_width() + 2, 10))
        s.blit(n, (tx, y0 + JV_H + 7))
