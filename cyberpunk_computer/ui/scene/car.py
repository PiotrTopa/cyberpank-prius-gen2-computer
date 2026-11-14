"""
Vector renderer for the car model (pygame).

Hidden surfaces are handled without a depth buffer: the closed body is close
to convex, so back-face culling is enough for it; wheels and open parts are
ordered around it by which side of the car the camera is on. Faces are flat
fills in near-black tints and the lines carry the picture ("vector scan with
hidden-line removal"). Nothing is anti-aliased (analog MFD: 1-bit edges only).

A full render costs ~130 polygon fills and ~150 lines; callers cache the
result (see ``render.CarCache``) and only re-render while something moves.
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import pygame

from .model import (BODY, GLASS, HEAD, INNER, TAIL, Camera,
                    CarModel, face_normal, get_model)

RGB = Tuple[int, int, int]


def car_palette(colors: Mapping[str, RGB]) -> Dict[str, RGB]:
    """Colours of the car drawing, derived from the active UI theme."""
    return {
        "line3": colors["cyan_bright"],
        "line2": (0, 140, 128),
        "line1": (30, 78, 86),
        "body": (0, 34, 40),
        "glass": (26, 22, 56),
        "inner": (8, 8, 14),
        "tyre": (10, 12, 16),
        "head_off": (30, 44, 52),
        "head_on": colors["amber"],
        "tail_off": (70, 10, 14),
        "tail_on": (200, 24, 30),
        "brake": (255, 48, 40),
        "rim": colors["cyan_dim"],
        "open": colors["amber"],          # outline of an open door / hatch
    }


class CarRenderer:
    """Draws the car for a camera, door/hatch openings and lamp states."""

    TYRE_W = 0.21

    def __init__(self, palette: Mapping[str, RGB], model: Optional[CarModel] = None):
        self.model = model or get_model()
        self.pal = dict(palette)

    def draw(self, surface: pygame.Surface, cam: Camera,
             openings: Optional[Mapping[str, float]] = None,
             lamps: Optional[Mapping[str, bool]] = None,
             offset: Tuple[float, float, float] = (0.0, 0.0, 0.0)) -> Tuple[int, int, int, int]:
        """Render the car. ``openings``: part -> 0..1; ``lamps``: head, tail,
        brake -> bool. ``offset`` moves the car in the world (x, y, z).
        Returns the bounding box drawn (x0, y0, x1, y1)."""
        self._bounds: List[Sequence[Tuple[float, float]]] = []
        m = self.model
        pal = self.pal
        lamps = lamps or {}
        openings = {p: f for p, f in (openings or {}).items()
                    if f > 0.01 and p in m.hinges}
        ox, oy, oz = offset
        proj = cam.project
        if ox or oy or oz:
            pts = [proj((x + ox, y + oy, z + oz)) for (x, y, z) in m.verts]
        else:
            pts = [proj(v) for v in m.verts]
        cpos = (cam.pos[0] - ox, cam.pos[1] - oy, cam.pos[2] - oz)
        polygon = pygame.draw.polygon
        line = pygame.draw.line

        fill = {
            BODY: pal["body"], GLASS: pal["glass"], INNER: pal["inner"],
            HEAD: pal["head_on"] if lamps.get("head") else pal["head_off"],
            TAIL: (pal["brake"] if lamps.get("brake")
                   else pal["tail_on"] if lamps.get("tail") else pal["tail_off"]),
        }
        tiers = (None, pal["line1"], pal["line2"], pal["line3"])

        # ---- facing of every face in its closed position ----
        faces = m.faces
        front = [False] * len(faces)
        for i, f in enumerate(faces):
            n, c = f.normal, f.centroid
            front[i] = (n[0] * (cpos[0] - c[0]) + n[1] * (cpos[1] - c[1])
                        + n[2] * (cpos[2] - c[2])) > 0.0

        cam_left = cpos[1] > 0.0
        before: List[str] = []
        after: List[str] = []
        for part in openings:
            if part == "hatch":
                (before if cpos[0] > 0.6 else after).append(part)
            elif part.endswith("l") == cam_left:
                after.append(part)
            else:
                before.append(part)
        # far -> near along the car so overlapping doors stack correctly
        order = (lambda p: -m.hinges[p][1][0]) if cpos[0] > 0 else (lambda p: m.hinges[p][1][0])
        before.sort(key=order)
        after.sort(key=order)

        for part in before:
            self._draw_part(surface, cam, part, openings[part], fill, tiers, offset)

        # ---- wheels behind / under the body ----
        late_wheels = []
        for wi, (rim, hub) in enumerate(m.wheels):
            cx, cy, cz = m.wheel_centres[wi]
            side = 1.0 if cy > 0 else -1.0
            vx, vy, vz = cpos[0] - cx, cpos[1] - cy, cpos[2] - cz
            dist = (vx * vx + vy * vy + vz * vz) ** 0.5 or 1.0
            facing = side * vy / dist
            if facing > 0.12:
                late_wheels.append((wi, True))
            else:
                self._draw_wheel(surface, cam, wi, False, offset)

        # ---- body ----
        open_faces = set()
        for part in openings:
            open_faces.update(m.part_faces[part])
        for i, f in enumerate(faces):
            if not front[i]:
                continue
            col = fill[INNER] if i in open_faces else fill[f.kind]
            polygon(surface, col, [pts[v][:2] for v in f.idx])
        for e in m.edges:
            fs = e.faces
            if len(fs) == 2:
                fa, fb = front[fs[0]], front[fs[1]]
                if not (fa or fb):
                    continue
                tier = 3 if fa != fb else e.tier
            else:
                if not front[fs[0]]:
                    continue
                tier = max(e.tier, 2)
            if tier:
                a, b = pts[e.a], pts[e.b]
                line(surface, tiers[tier], (a[0], a[1]), (b[0], b[1]))

        # lamp lenses and wheel arches lying on the body surface
        for dpts, kind, fi in m.decals:
            if front[fi] and fi not in open_faces:
                poly = [proj((x + ox, y + oy, z + oz))[:2] for (x, y, z) in dpts]
                polygon(surface, fill[kind], poly)
                polygon(surface, tiers[2], poly, 1)
        for arc, side in m.arches:
            if (cpos[1] - side * 0.87) * side > 0.3:
                ap = [proj((x + ox, y + oy, z + oz))[:2] for (x, y, z) in arc]
                pygame.draw.lines(surface, tiers[2], False, ap)

        # high-mounted stop lamp on the spoiler lip
        if lamps.get("brake"):
            a, b, c = (pts[i] for i in m.stop_lamp)
            mx1, my1 = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            mx2, my2 = (c[0] + b[0]) / 2, (c[1] + b[1]) / 2
            line(surface, pal["brake"], (mx1, my1), (mx2, my2), 2)

        for wi, _ in late_wheels:
            self._draw_wheel(surface, cam, wi, True, offset)

        for part in after:
            self._draw_part(surface, cam, part, openings[part], fill, tiers, offset)

        x0 = y0 = 1e9
        x1 = y1 = -1e9
        for lst in [[(p[0], p[1]) for p in pts]] + self._bounds:
            for (x, y) in lst:
                if x < x0:
                    x0 = x
                if x > x1:
                    x1 = x
                if y < y0:
                    y0 = y
                if y > y1:
                    y1 = y
        return (int(x0) - 2, int(y0) - 2, int(x1) + 3, int(y1) + 3)

    # -- pieces ---------------------------------------------------------------

    def _draw_wheel(self, surface, cam: Camera, wi: int, face_on: bool,
                    offset: Tuple[float, float, float]) -> None:
        """Tyre = the rim outline swept across the tyre width. The inner
        outline is the outer one shifted by the projected axle offset (exact
        enough at this size, and it halves the projections)."""
        m = self.model
        pal = self.pal
        rim, hub = m.wheels[wi]
        ox, oy, oz = offset
        cx, cy, cz = m.wheel_centres[wi]
        side = 1.0 if cy > 0 else -1.0
        project = cam.project
        outer = [project((x + ox, y + oy, z + oz))[:2] for (x, y, z) in (m.verts[i] for i in rim)]
        c0 = project((cx + ox, cy + oy, cz + oz))
        c1 = project((cx + ox, cy + oy - side * self.TYRE_W, cz + oz))
        tx, ty = c1[0] - c0[0], c1[1] - c0[1]
        inner = [(x + tx, y + ty) for (x, y) in outer]
        self._bounds.append(outer)
        self._bounds.append(inner)
        polygon = pygame.draw.polygon
        tyre = pal["tyre"]
        polygon(surface, tyre, inner)
        polygon(surface, pal["line1"], inner, 1)
        if tx * tx + ty * ty > 0.5:
            # the two silhouette points: extremes across the sweep direction
            a = max(outer, key=lambda p: p[0] * ty - p[1] * tx)
            b = min(outer, key=lambda p: p[0] * ty - p[1] * tx)
            polygon(surface, tyre, [a, b, (b[0] + tx, b[1] + ty), (a[0] + tx, a[1] + ty)])
            pygame.draw.line(surface, pal["line1"], a, (a[0] + tx, a[1] + ty))
            pygame.draw.line(surface, pal["line1"], b, (b[0] + tx, b[1] + ty))
        polygon(surface, tyre, outer)
        if face_on:
            polygon(surface, pal["line2"], outer, 1)
            hp = [project((x + ox, y + oy, z + oz))[:2] for (x, y, z) in (m.verts[i] for i in hub)]
            for i in range(0, len(hp), 2):
                pygame.draw.line(surface, pal["rim"], hp[i], hp[i + 1])
        else:
            polygon(surface, pal["line1"], outer, 1)

    def _draw_part(self, surface, cam: Camera, part: str, frac: float,
                   fill: Mapping[str, RGB], tiers, offset) -> None:
        m = self.model
        ox, oy, oz = offset
        world = m.part_points(part, frac)
        scr = {vi: cam.project((p[0] + ox, p[1] + oy, p[2] + oz))
               for vi, p in world.items()}
        self._bounds.append([(p[0], p[1]) for p in scr.values()])
        cpos = (cam.pos[0] - ox, cam.pos[1] - oy, cam.pos[2] - oz)
        items = []
        for fi in m.part_faces[part]:
            f = m.faces[fi]
            w = [world[v] for v in f.idx]
            n = face_normal(w)
            cx = sum(p[0] for p in w) / len(w)
            cy = sum(p[1] for p in w) / len(w)
            cz = sum(p[2] for p in w) / len(w)
            vx, vy, vz = cpos[0] - cx, cpos[1] - cy, cpos[2] - cz
            facing = n[0] * vx + n[1] * vy + n[2] * vz > 0
            items.append((vx * vx + vy * vy + vz * vz, f, facing))
        items.sort(key=lambda t: -t[0])
        for _, f, facing in items:
            poly = [scr[v][:2] for v in f.idx]
            if facing:
                col = fill[f.kind]
            else:
                col = fill[GLASS] if f.kind == GLASS else fill[INNER]
            pygame.draw.polygon(surface, col, poly)
        hot = self.pal.get("open", tiers[3])
        for a, b, boundary, tier in m.part_edges[part]:
            if boundary:
                col = hot
            elif tier >= 3:
                col = tiers[2]
            else:
                continue
            pa, pb = scr[a], scr[b]
            pygame.draw.line(surface, col, (pa[0], pa[1]), (pb[0], pb[1]))

    # -- anchors ----------------------------------------------------------------

    def lamp_points(self, cam: Camera, offset=(0.0, 0.0, 0.0)) -> Dict[str, Tuple[int, int]]:
        """Screen positions of the lamps that face the camera (for glows)."""
        m = self.model
        ox, oy, oz = offset
        cpos = (cam.pos[0] - ox, cam.pos[1] - oy, cam.pos[2] - oz)
        out: Dict[str, Tuple[int, int]] = {}
        for name, p in m.anchors.items():
            if name.startswith("head") and cpos[0] < p[0] - 0.2:
                continue
            if (name.startswith("tail") or name == "stop_c") and cpos[0] > p[0] + 0.2:
                continue
            if name.endswith("_l") and cpos[1] < -2.5:
                continue
            if name.endswith("_r") and cpos[1] > 2.5:
                continue
            s = cam.project((p[0] + ox, p[1] + oy, p[2] + oz))
            out[name] = (int(s[0]), int(s[1]))
        return out
