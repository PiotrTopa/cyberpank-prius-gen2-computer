"""
Low-poly vector model of the car (Toyota Prius NHW20 liftback) and the camera.

Pure Python, no pygame: geometry, part transforms (doors, hatch) and the
pinhole projection used by ``render.py``. Original art: the body is lofted
from a table of cross-sections measured off the car's published dimensions
(4450 x 1725 x 1490 mm, wheelbase 2700 mm).

Frame: metres, x forward, y LEFT, z up, origin on the ground under the car's
centre. (roadsense uses y RIGHT: ``inputs.py`` flips the sign.)
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

Vec3 = Tuple[float, float, float]

CAR_LENGTH = 4.45
CAR_HALF_LEN = CAR_LENGTH / 2.0
WHEEL_R = 0.31
AXLE_FRONT_S = 0.90     # distance from the nose
AXLE_REAR_S = 3.60

# Cross-sections, nose -> tail. s = distance from the nose (m); each section
# is the left half, bottom -> top:
#   (w_bot, z_bot)   lower body edge
#   (w_mid, z_mid)   widest point of the body side
#   (w_belt, z_belt) belt line (bottom of the glass / hood edge)
#   (w_roof, z_roof) roof edge (top of the side glass)
#   z_top            centre line
_SECTIONS: Tuple[Tuple[float, ...], ...] = (
    # s     wb    zb    wm     zm    wbelt zbelt wroof zroof ztop
    (0.00, 0.46, 0.30, 0.54, 0.42, 0.52, 0.51, 0.40, 0.535, 0.545),
    (0.14, 0.69, 0.23, 0.765, 0.46, 0.745, 0.645, 0.58, 0.69, 0.71),
    (0.55, 0.84, 0.21, 0.86, 0.50, 0.83, 0.77, 0.67, 0.80, 0.825),
    (0.93, 0.86, 0.20, 0.865, 0.55, 0.84, 0.90, 0.74, 0.935, 0.96),
    (1.45, 0.86, 0.20, 0.865, 0.58, 0.84, 0.94, 0.69, 1.235, 1.26),
    (1.87, 0.86, 0.20, 0.865, 0.60, 0.84, 0.965, 0.62, 1.435, 1.46),
    (2.45, 0.86, 0.20, 0.865, 0.61, 0.84, 0.985, 0.60, 1.465, 1.49),
    (3.30, 0.86, 0.20, 0.865, 0.63, 0.84, 1.015, 0.585, 1.385, 1.41),
    (3.55, 0.855, 0.21, 0.865, 0.65, 0.835, 1.025, 0.58, 1.34, 1.365),
    (3.90, 0.84, 0.23, 0.855, 0.67, 0.82, 1.04, 0.57, 1.255, 1.275),
    (4.20, 0.80, 0.27, 0.82, 0.68, 0.78, 1.04, 0.56, 1.15, 1.165),
    (4.39, 0.75, 0.30, 0.78, 0.66, 0.745, 0.93, 0.66, 0.955, 0.965),
    (4.45, 0.66, 0.33, 0.72, 0.55, 0.70, 0.71, 0.62, 0.725, 0.73),
)

S_COWL = 0.93          # windshield base
S_DOOR_F = 1.45        # front door leading edge
S_WS_TOP = 1.87        # windshield top
S_BPILLAR = 2.45       # front/rear door split
S_CPILLAR = 3.30       # rear door trailing edge
S_HATCH = 3.55         # hatch hinge / top of the rear glass
S_QGLASS = 3.90        # end of the rear quarter glass
S_SPOILER = 4.20       # spoiler lip (upper/lower rear glass split)
S_TAIL = 4.39

RING = 9               # points per section: L0 L1 L2 L3 C R3 R2 R1 R0

# Face kinds (render.py maps them to colours).
BODY, GLASS, HEAD, TAIL, WHEEL, HUB, INNER = (
    "body", "glass", "head", "tail", "wheel", "hub", "inner")

PARTS = ("body", "door_fl", "door_fr", "door_rl", "door_rr", "hatch")

# Line tiers: 0 none, 1 dim, 2 mid, 3 bright.


class Face:
    __slots__ = ("idx", "kind", "part", "normal", "centroid", "edges")

    def __init__(self, idx: Tuple[int, ...], kind: str, part: str):
        self.idx = idx
        self.kind = kind
        self.part = part
        self.normal: Vec3 = (0.0, 0.0, 1.0)
        self.centroid: Vec3 = (0.0, 0.0, 0.0)
        self.edges: List[int] = []


class Edge:
    __slots__ = ("a", "b", "tier", "faces")

    def __init__(self, a: int, b: int):
        self.a = a
        self.b = b
        self.tier = 0
        self.faces: List[int] = []


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _norm(a: Vec3) -> Vec3:
    n = math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2]) or 1.0
    return (a[0] / n, a[1] / n, a[2] / n)


def face_normal(pts: Sequence[Vec3]) -> Vec3:
    """Newell normal of a (possibly non-planar) polygon."""
    nx = ny = nz = 0.0
    n = len(pts)
    for i in range(n):
        a = pts[i]
        b = pts[(i + 1) % n]
        nx += (a[1] - b[1]) * (a[2] + b[2])
        ny += (a[2] - b[2]) * (a[0] + b[0])
        nz += (a[0] - b[0]) * (a[1] + b[1])
    return _norm((nx, ny, nz))


def centroid(pts: Sequence[Vec3]) -> Vec3:
    n = float(len(pts))
    return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n,
            sum(p[2] for p in pts) / n)


class CarModel:
    """The mesh: vertices, faces (with part and kind), edges with line tiers."""

    def __init__(self) -> None:
        self.verts: List[Vec3] = []
        self.faces: List[Face] = []
        self.edges: List[Edge] = []
        self._edge_ix: Dict[Tuple[int, int], int] = {}
        self.part_faces: Dict[str, List[int]] = {p: [] for p in PARTS}
        self.wheels: List[Tuple[List[int], List[int]]] = []   # (rim idx, hub idx)
        self.wheel_centres: List[Vec3] = []
        # Lamp anchor points (for glow sprites): name -> vertex-less 3D point
        self.anchors: Dict[str, Vec3] = {}
        self._build()

    # -- construction --------------------------------------------------------

    def _v(self, p: Vec3) -> int:
        self.verts.append(p)
        return len(self.verts) - 1

    def _edge(self, a: int, b: int) -> Edge:
        key = (a, b) if a < b else (b, a)
        ix = self._edge_ix.get(key)
        if ix is None:
            ix = len(self.edges)
            self._edge_ix[key] = ix
            self.edges.append(Edge(key[0], key[1]))
        return self.edges[ix]

    def _face(self, idx: Sequence[int], kind: str, part: str,
              outward_from: Optional[Vec3] = None) -> Face:
        idx = tuple(idx)
        pts = [self.verts[i] for i in idx]
        n = face_normal(pts)
        c = centroid(pts)
        if outward_from is not None:
            d = _sub(c, outward_from)
            if n[0] * d[0] + n[1] * d[1] + n[2] * d[2] < 0:
                idx = tuple(reversed(idx))
                n = (-n[0], -n[1], -n[2])
        f = Face(idx, kind, part)
        f.normal = n
        f.centroid = c
        fi = len(self.faces)
        self.faces.append(f)
        self.part_faces[part].append(fi)
        for i in range(len(idx)):
            e = self._edge(idx[i], idx[(i + 1) % len(idx)])
            e.faces.append(fi)
            f.edges.append(self._edge_ix[(e.a, e.b)])
        return f

    def _tier(self, a: int, b: int, tier: int) -> None:
        e = self._edge(a, b)
        if tier > e.tier:
            e.tier = tier

    def _build(self) -> None:
        secs = _SECTIONS
        ring: List[List[int]] = []
        for (s, wb, zb, wm, zm, wl, zl, wr, zr, zt) in secs:
            x = CAR_HALF_LEN - s
            pts = [(x, wb, zb), (x, wm, zm), (x, wl, zl), (x, wr, zr), (x, 0.0, zt),
                   (x, -wr, zr), (x, -wl, zl), (x, -wm, zm), (x, -wb, zb)]
            ring.append([self._v(p) for p in pts])

        def near(a: float, b: float) -> bool:
            return abs(a - b) < 1e-6

        for i in range(len(secs) - 1):
            s0, s1 = secs[i][0], secs[i + 1][0]
            mid_s = 0.5 * (s0 + s1)
            axis = (CAR_HALF_LEN - mid_s, 0.0, 0.62)
            for j in range(RING - 1):
                left = j < 4
                strip = j if left else 7 - j        # 0 A, 1 B, 2 C, 3 D
                kind = BODY
                part = "body"
                if strip == 3:
                    if S_COWL - 1e-6 <= s0 and s1 <= S_WS_TOP + 1e-6:
                        kind = GLASS                      # windshield
                    elif s0 >= S_HATCH - 1e-6 and s1 <= S_TAIL + 1e-6:
                        kind = GLASS                      # rear glass (upper + lower)
                    if s0 >= S_HATCH - 1e-6:
                        part = "hatch"
                elif strip == 2:
                    if S_COWL - 1e-6 <= s0 and s1 <= S_QGLASS + 1e-6:
                        kind = GLASS                      # side glass
                    elif s1 <= 0.55 + 1e-6 and s0 >= 0.14 - 1e-6:
                        kind = HEAD
                    elif s0 >= S_SPOILER - 1e-6 and s1 <= S_TAIL + 1e-6:
                        kind = TAIL
                if strip <= 2:
                    if S_DOOR_F - 1e-6 <= s0 and s1 <= S_BPILLAR + 1e-6:
                        part = "door_fl" if left else "door_fr"
                    elif S_BPILLAR - 1e-6 <= s0 and s1 <= S_CPILLAR + 1e-6:
                        part = "door_rl" if left else "door_rr"
                a, b = ring[i][j], ring[i][j + 1]
                c, d = ring[i + 1][j + 1], ring[i + 1][j]
                self._face((a, b, c, d), kind, part, outward_from=axis)

        # End caps
        nose = self._face(ring[0], BODY, "body", outward_from=(0.0, 0.0, 0.6))
        tail = self._face(ring[-1], BODY, "body", outward_from=(0.0, 0.0, 0.6))
        for f in (nose, tail):
            for i in range(len(f.idx)):
                self._tier(f.idx[i], f.idx[(i + 1) % len(f.idx)], 2)

        # ---- line tiers ----
        n = len(secs)
        for i in range(n - 1):
            s0, s1 = secs[i][0], secs[i + 1][0]
            for k in (0, 8):                                   # sill
                self._tier(ring[i][k], ring[i + 1][k], 2)
            for k in (2, 6):                                   # belt
                t = 3 if (s0 >= S_COWL - 1e-6 and s1 <= S_QGLASS + 1e-6) else 2
                self._tier(ring[i][k], ring[i + 1][k], t)
            for k in (3, 5):                                   # roof edge
                t = 3 if (s0 >= S_COWL - 1e-6 and s1 <= S_SPOILER + 1e-6) else 1
                if s0 >= S_SPOILER - 1e-6:
                    t = 2
                self._tier(ring[i][k], ring[i + 1][k], t)
        for i in range(n):
            s = secs[i][0]
            r = ring[i]
            side = ((0, 1), (1, 2), (7, 8), (6, 7))            # strips A, B
            pillar = ((2, 3), (5, 6))                          # strip C
            top = ((3, 4), (4, 5))                             # strip D
            if near(s, S_DOOR_F) or near(s, S_BPILLAR) or near(s, S_CPILLAR):
                for a, b in side + pillar:
                    self._tier(r[a], r[b], 2)
            if near(s, S_COWL):
                for a, b in top:
                    self._tier(r[a], r[b], 3)
            if near(s, S_WS_TOP) or near(s, S_HATCH) or near(s, S_TAIL):
                for a, b in top:
                    self._tier(r[a], r[b], 2)
            if near(s, S_SPOILER):
                for a, b in top:
                    self._tier(r[a], r[b], 3)
                for a, b in pillar:
                    self._tier(r[a], r[b], 2)
            if near(s, S_QGLASS) or near(s, S_TAIL) or near(s, 0.14) or near(s, 0.55):
                for a, b in pillar:
                    self._tier(r[a], r[b], 2)

        # ---- wheels: rim 12-gon + hub 6-gon on the outer face ----
        for s in (AXLE_FRONT_S, AXLE_REAR_S):
            for side in (1.0, -1.0):
                cx = CAR_HALF_LEN - s
                y = 0.885 * side
                rim = [self._v((cx + WHEEL_R * math.cos(t * math.pi / 6.0), y,
                                WHEEL_R + WHEEL_R * math.sin(t * math.pi / 6.0)))
                       for t in range(12)]
                # hub: 5 spokes, stored as (inner, outer) point pairs
                hub = []
                for t in range(5):
                    a = t * math.pi / 2.5 + 0.3
                    hub.append(self._v((cx + 0.07 * math.cos(a), y, WHEEL_R + 0.07 * math.sin(a))))
                    hub.append(self._v((cx + 0.22 * math.cos(a), y, WHEEL_R + 0.22 * math.sin(a))))
                self.wheels.append((rim, hub))
                self.wheel_centres.append((cx, y, WHEEL_R))

        # ---- decals: lamp lenses that extend below the belt line, and the
        # wheel arches. (points, kind, face whose facing they follow) ----
        self.decals: List[Tuple[List[Vec3], str, int]] = []
        self.arches: List[Tuple[List[Vec3], float]] = []

        def lerp3(a: Vec3, b: Vec3, t: float) -> Vec3:
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t,
                    a[2] + (b[2] - a[2]) * t)

        def face_of(i: int, j: int) -> int:
            return i * (RING - 1) + j

        vv = self.verts
        for belt, mid_k, strip_j in ((2, 1, 1), (6, 7, 6)):
            # headlamp: upper part of the fender side, swept back
            pts = [vv[ring[1][belt]], vv[ring[2][belt]],
                   lerp3(vv[ring[2][belt]], vv[ring[2][mid_k]], 0.30),
                   lerp3(vv[ring[1][belt]], vv[ring[1][mid_k]], 0.75)]
            self.decals.append((pts, HEAD, face_of(1, strip_j)))
            # tail lamp: tall lens on the rear corner
            pts = [vv[ring[10][belt]], vv[ring[11][belt]],
                   lerp3(vv[ring[11][belt]], vv[ring[11][mid_k]], 0.55),
                   lerp3(vv[ring[10][belt]], vv[ring[10][mid_k]], 0.75),
                   lerp3(lerp3(vv[ring[9][belt]], vv[ring[10][belt]], 0.55),
                         lerp3(vv[ring[9][mid_k]], vv[ring[10][mid_k]], 0.55), 0.35)]
            self.decals.append((pts, TAIL, face_of(10, strip_j)))
        # tail lamp lenses on the rear face, flanking the hatch
        cap = ring[-1]
        prev = ring[-2]
        for belt, mid_k, roof in ((2, 1, 3), (6, 7, 5)):
            pts = [vv[prev[belt]], vv[cap[belt]],
                   lerp3(vv[cap[belt]], vv[cap[mid_k]], 0.7),
                   lerp3(vv[prev[belt]], vv[prev[mid_k]], 0.55)]
            self.decals.append((pts, TAIL, face_of(11, 1 if belt == 2 else 6)))
        for s in (AXLE_FRONT_S, AXLE_REAR_S):
            for side in (1.0, -1.0):
                cx = CAR_HALF_LEN - s
                arc = [(cx + 0.385 * math.cos(math.radians(a)), 0.868 * side,
                        WHEEL_R + 0.385 * math.sin(math.radians(a)))
                       for a in range(-8, 189, 28)]
                self.arches.append((arc, side))

        # ---- anchors for lamp glows ----
        def mid(i0: int, k0: int, i1: int, k1: int) -> Vec3:
            a = self.verts[ring[i0][k0]]
            b = self.verts[ring[i1][k1]]
            return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2)

        self.anchors["head_l"] = mid(1, 2, 2, 3)
        self.anchors["head_r"] = mid(1, 6, 2, 5)
        self.anchors["tail_l"] = mid(10, 2, 11, 3)
        self.anchors["tail_r"] = mid(10, 6, 11, 5)
        sp = self.verts[ring[10][4]]
        self.anchors["stop_c"] = sp
        self.stop_lamp = (ring[10][3], ring[10][4], ring[10][5])
        self.ring = ring

        # Hinges
        self.hinges: Dict[str, Tuple[str, Vec3]] = {
            "door_fl": ("z", (CAR_HALF_LEN - S_DOOR_F, 0.865, 0.0)),
            "door_fr": ("z", (CAR_HALF_LEN - S_DOOR_F, -0.865, 0.0)),
            "door_rl": ("z", (CAR_HALF_LEN - S_BPILLAR, 0.865, 0.0)),
            "door_rr": ("z", (CAR_HALF_LEN - S_BPILLAR, -0.865, 0.0)),
            "hatch": ("y", (CAR_HALF_LEN - S_HATCH, 0.0, 1.365)),
        }
        self.part_verts: Dict[str, List[int]] = {}
        for part in PARTS:
            seen: List[int] = []
            for fi in self.part_faces[part]:
                for v in self.faces[fi].idx:
                    if v not in seen:
                        seen.append(v)
            self.part_verts[part] = seen
        # Outline of every moving part: (a, b, on_boundary, tier)
        self.part_edges: Dict[str, List[Tuple[int, int, bool, int]]] = {}
        for part in PARTS:
            if part == "body":
                continue
            fset = set(self.part_faces[part])
            out: List[Tuple[int, int, bool, int]] = []
            done = set()
            for fi in self.part_faces[part]:
                for ei in self.faces[fi].edges:
                    if ei in done:
                        continue
                    done.add(ei)
                    e = self.edges[ei]
                    inside = sum(1 for f in e.faces if f in fset)
                    out.append((e.a, e.b, inside < 2, e.tier))
            self.part_edges[part] = out

    # -- part transforms -----------------------------------------------------

    MAX_ANGLE = {"door_fl": 62.0, "door_fr": 62.0, "door_rl": 62.0,
                 "door_rr": 62.0, "hatch": 72.0}

    def part_points(self, part: str, open_frac: float) -> Dict[int, Vec3]:
        """Vertices of a moving part at ``open_frac`` (0 closed .. 1 open)."""
        axis, h = self.hinges[part]
        ang = math.radians(self.MAX_ANGLE[part] * max(0.0, min(1.0, open_frac)))
        out: Dict[int, Vec3] = {}
        if axis == "z":
            # left doors swing the trailing edge to +y, right doors to -y
            sgn = -1.0 if part.endswith("l") else 1.0
            c, s = math.cos(sgn * ang), math.sin(sgn * ang)
            for vi in self.part_verts[part]:
                x, y, z = self.verts[vi]
                dx, dy = x - h[0], y - h[1]
                out[vi] = (h[0] + dx * c - dy * s, h[1] + dx * s + dy * c, z)
        else:
            c, s = math.cos(ang), math.sin(ang)
            for vi in self.part_verts[part]:
                x, y, z = self.verts[vi]
                dx, dz = x - h[0], z - h[2]
                # tail (dx < 0) lifts up
                out[vi] = (h[0] + dx * c + dz * s, y, h[2] - dx * s + dz * c)
        return out


class Camera:
    """Look-at pinhole camera projecting to scene pixels."""

    __slots__ = ("pos", "target", "f", "cx", "cy", "_r", "_u", "_f")

    def __init__(self, pos: Vec3, target: Vec3, f: float, cx: float, cy: float):
        self.pos = pos
        self.target = target
        self.f = f
        self.cx = cx
        self.cy = cy
        fwd = _norm(_sub(target, pos))
        right = _norm(_cross(fwd, (0.0, 0.0, 1.0)))
        up = _cross(right, fwd)
        self._f, self._r, self._u = fwd, right, up

    def project(self, p: Vec3) -> Tuple[float, float, float]:
        """-> (screen x, screen y, depth). Depth <= 0.05 is clamped."""
        px, py, pz = self.pos
        dx, dy, dz = p[0] - px, p[1] - py, p[2] - pz
        r, u, f = self._r, self._u, self._f
        zc = dx * f[0] + dy * f[1] + dz * f[2]
        if zc < 0.05:
            zc = 0.05
        k = self.f / zc
        return (self.cx + k * (dx * r[0] + dy * r[1] + dz * r[2]),
                self.cy - k * (dx * u[0] + dy * u[1] + dz * u[2]), zc)

    def horizon_y(self) -> float:
        """Screen y of the horizon (ground plane at infinity)."""
        f = self._f
        horiz = math.hypot(f[0], f[1]) or 1e-6
        return self.cy + self.f * (f[2] / horiz)

    @staticmethod
    def lerp(a: "Camera", b: "Camera", t: float) -> "Camera":
        def mix(p: Vec3, q: Vec3) -> Vec3:
            return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t,
                    p[2] + (q[2] - p[2]) * t)
        # orbit: interpolate the camera position in cylindrical coordinates
        # around the target so the move reads as a swing, not a dolly
        # the target leaves the car late, so the car stays in frame mid-move
        tt = t * t * t
        ta = (a.target[0] + (b.target[0] - a.target[0]) * tt,
              a.target[1] + (b.target[1] - a.target[1]) * tt,
              a.target[2] + (b.target[2] - a.target[2]) * tt)
        ra = math.hypot(a.pos[0] - a.target[0], a.pos[1] - a.target[1])
        rb = math.hypot(b.pos[0] - b.target[0], b.pos[1] - b.target[1])
        aa = math.atan2(a.pos[1] - a.target[1], a.pos[0] - a.target[0])
        ab = math.atan2(b.pos[1] - b.target[1], b.pos[0] - b.target[0])
        d = (ab - aa + math.pi) % (2 * math.pi) - math.pi
        ang = aa + d * t
        rad = ra + (rb - ra) * t
        pos = (ta[0] + rad * math.cos(ang), ta[1] + rad * math.sin(ang),
               a.pos[2] + (b.pos[2] - a.pos[2]) * t)
        return Camera(pos, ta, a.f + (b.f - a.f) * t,
                      a.cx + (b.cx - a.cx) * t, a.cy + (b.cy - a.cy) * t)


_MODEL: Optional[CarModel] = None


def get_model() -> CarModel:
    """The shared mesh (built on first use: ~1 ms on a desktop)."""
    global _MODEL
    if _MODEL is None:
        _MODEL = CarModel()
    return _MODEL
