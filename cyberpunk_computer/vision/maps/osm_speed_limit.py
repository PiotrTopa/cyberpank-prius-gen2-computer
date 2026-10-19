"""Speed limit from an offline OpenStreetMap extract (roadmap 4.1, Phase A).

Pure Python, no numpy, no network: the extract is a (gzipped) JSON file made
by ``tools/osm_extract.py``; the bundled one covers ~15×15 km around Gdańsk
(:data:`DEFAULT_EXTRACT`).

Pipeline::

    Position(lat, lon, heading_deg, speed_kmh, t)
      → OsmSpeedMap.match(position)  -> MapMatch   (nearest way, heading, direction)
      → speed_limit(match)           -> SpeedLimit (value, basis, confidence)

**Matching** (:meth:`OsmSpeedMap.match`). The extract is projected to a local
metric plane (equirectangular around the box centre; < 0.1 % error over
15 km) and every segment is put in a uniform grid of ``cell_m`` (64 m)
buckets. A query looks at the buckets within ``radius_m`` (25 m) and scores
each segment by

    cost = distance_m + heading_weight · angle_deg            (0.25 m/°)
           + wrong_way_penalty   if the travel direction is forbidden (40 m)
           + service_penalty     for highway=service (5 m)

where ``angle`` is the difference between the GPS heading and the segment
direction (forward, or backward on a two-way road; on a oneway the backward
direction is "wrong way"). Heading is used only when known and the speed is
at least ``min_heading_speed_kmh`` (5 km/h; GPS course is noise when
standing). ``oneway=yes|-1``, ``junction=roundabout`` and ``highway=motorway``
(OSM implies oneway) are honoured. The lowest cost wins; the travel direction
on that way selects ``maxspeed:forward``/``maxspeed:backward``.

**Limit inference** (:func:`speed_limit`, :func:`implicit_limit`), Polish
rules (Prawo o ruchu drogowym art. 20):

* explicit: ``maxspeed[:forward|:backward]`` numeric (``"50"``, ``"30 mph"``)
  → basis ``explicit maxspeed``. Polish implicit-type values in ``maxspeed``
  (``PL:urban``, ``PL:rural``, ``PL:zone30``, ``PL:living_street``,
  ``PL:expressway``, ``PL:motorway``) are expanded; a zone value counts as
  explicit (it reflects a zone sign), the others as implicit.
* implicit, no ``maxspeed``: ``motorway(_link)`` 140; expressway
  (``motorroad=yes`` or ``PL:expressway``) 120 on a dual carriageway
  (oneway / ``dual_carriageway=yes``), else 100 — basis ``implicit road
  class``; ``living_street`` (strefa zamieszkania) 20 and any road inside a
  built-up area 50 — basis ``implicit built-up``; outside: 100 on a dual
  carriageway with ≥ 2 lanes (oneway + ``lanes>=2``), else 90 — basis
  ``implicit road class``.

**Built-up area — heuristic.** OSM in Poland has no polygons for the
"obszar zabudowany" signs (D-42/D-43); what exists is per-way tagging. Each
way gets ``built_up`` (True/False/None) with a basis, resolved at load time
in this order:

1. ``tag`` — ``source:maxspeed`` / ``maxspeed:type`` / ``zone:traffic`` /
   ``zone:maxspeed`` / ``maxspeed`` say ``PL:urban``, ``PL:zone*``,
   ``PL:living_street`` (True) or ``PL:rural`` (False). Reliable.
2. ``road class`` — ``highway=residential|living_street`` → True. Heuristic
   (a residential street outside a village sign exists, rarely).
3. ``neighbour`` — majority vote of the drivable ways sharing a vertex with
   this one (1 hop, then 2 hops), counting their tag/road-class state and
   numeric ``maxspeed=50`` (urban) / ``90`` (rural). Heuristic.
4. ``place`` — within a radius of a ``place=*`` node (city 8 km, town 3 km,
   suburb 1.5 km, village 0.8 km, quarter 1 km, neighbourhood 0.6 km,
   hamlet 0.3 km). Heuristic and coarse; in the Gdańsk box it marks
   everything built-up, which is right for the city and wrong for the
   few rural edges (those mostly carry explicit tags anyway).
5. ``default`` — not built-up (False). Low confidence.

Motorways and expressways ignore the built-up state (their limits come from
the class). Confidence (0..1, display/fusion only) is the basis confidence
times the match confidence. Not modelled (known gaps): time-dependent limits
(``maxspeed:conditional``, kept in the tags but ignored), vehicle-specific
limits, temporary/construction limits, zone polygons (``zone:maxspeed``
areas are only seen when the way itself is tagged), and the 2021+ rule
details for expressways inside built-up areas (always explicitly signed
around Gdańsk).
"""

from __future__ import annotations

import gzip
import json
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_EXTRACT = os.path.join(os.path.dirname(__file__), "data", "gdansk_15km.json.gz")

BASIS_EXPLICIT = "explicit maxspeed"
BASIS_BUILT_UP = "implicit built-up"
BASIS_CLASS = "implicit road class"

# Polish defaults (km/h)
PL_BUILT_UP = 50
PL_LIVING_STREET = 20
PL_RURAL = 90
PL_RURAL_DUAL = 100
PL_EXPRESSWAY_SINGLE = 100
PL_EXPRESSWAY = 120
PL_MOTORWAY = 140

PLACE_RADIUS_M = {"city": 8000.0, "town": 3000.0, "suburb": 1500.0, "quarter": 1000.0,
                  "village": 800.0, "neighbourhood": 600.0, "hamlet": 300.0}

BUILT_UP_CONFIDENCE = {"tag": 0.85, "road class": 0.75, "neighbour": 0.6,
                       "place": 0.5, "default": 0.3}

_URBAN_VALUES = ("PL:urban", "PL:living_street")
_RURAL_VALUES = ("PL:rural",)
_BUILT_UP_TAGS = ("source:maxspeed", "maxspeed:type", "zone:traffic", "zone:maxspeed", "maxspeed")


@dataclass(frozen=True)
class Position:
    """One GPS fix. ``heading_deg``: course over ground, 0 = north, clockwise.

    The backend has no GPS ingest yet; the future one builds these and calls
    :meth:`cyberpunk_computer.vision.fusion.service.SpeedLimitService.update`.
    """
    lat: float
    lon: float
    heading_deg: Optional[float] = None
    speed_kmh: Optional[float] = None
    t: float = 0.0


@dataclass
class Way:
    id: int
    tags: Dict[str, str]
    xy: List[Tuple[float, float]]
    oneway: int = 0                      # +1 forward only, -1 backward only, 0 both
    built_up: Optional[bool] = None
    built_up_basis: str = ""
    junctions: List[int] = field(default_factory=list)   # vertex indices shared with other ways

    @property
    def highway(self) -> str:
        return self.tags.get("highway", "")

    @property
    def name(self) -> Optional[str]:
        return self.tags.get("name") or self.tags.get("ref") or None


@dataclass(frozen=True)
class MapMatch:
    way: Way
    segment: int                 # index of the segment's first vertex
    distance_m: float
    direction: str               # "forward" | "backward" | "unknown" (no usable heading)
    angle_deg: Optional[float]   # heading vs travel direction on the way
    cost: float
    confidence: float            # 0..1
    junction_dist_m: Optional[float]   # to the nearest junction vertex of this way
    point: Tuple[float, float]   # projected point (local metres)

    @property
    def at_junction(self) -> bool:
        return self.junction_dist_m is not None and self.junction_dist_m < 30.0


@dataclass(frozen=True)
class SpeedLimit:
    value_kmh: Optional[int]
    source: str = "map"
    basis: str = ""
    way_name: Optional[str] = None
    confidence: float = 0.0
    way_id: Optional[int] = None
    highway: str = ""
    built_up: Optional[bool] = None
    urban_default_kmh: Optional[int] = None   # implicit limit if inside a built-up area
    rural_default_kmh: Optional[int] = None   # implicit limit if outside


# ── tag parsing ───────────────────────────────────────────────────────────

_NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(mph|km/h|kmh|kph)?\s*$", re.I)


def parse_maxspeed(value: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
    """``"50"`` → (50, None); ``"30 mph"`` → (48, None);
    ``"PL:urban"`` → (None, "PL:urban"); junk/``none``/``signals`` → (None, None).
    Multiple values (``"50;70"``) take the lowest."""
    if not value:
        return None, None
    value = value.strip()
    if ";" in value:
        nums = [parse_maxspeed(v)[0] for v in value.split(";")]
        nums = [n for n in nums if n is not None]
        return (min(nums) if nums else None), None
    m = _NUM_RE.match(value)
    if m:
        v = float(m.group(1))
        if (m.group(2) or "").lower() == "mph":
            v *= 1.609344
        return int(round(v)), None
    if value.upper().startswith("PL:"):
        return None, "PL:" + value[3:]
    if value.lower() == "walk":
        return 10, None
    return None, None


def _pl_value(tag_value: str) -> Optional[int]:
    """PL:zone30 → 30, PL:living_street → 20 (others are road-class dependent)."""
    v = tag_value.lower()
    if v.startswith("pl:zone"):
        d = re.sub(r"\D", "", v)
        return int(d) if d else None
    if v == "pl:living_street":
        return PL_LIVING_STREET
    return None


def _is_dual(tags: Dict[str, str], oneway: int) -> bool:
    return tags.get("dual_carriageway") == "yes" or oneway != 0


def _lanes(tags: Dict[str, str]) -> int:
    try:
        return int(str(tags.get("lanes", "1")).split(";")[0])
    except ValueError:
        return 1


def way_oneway(tags: Dict[str, str]) -> int:
    ow = tags.get("oneway")
    if ow in ("yes", "true", "1"):
        return 1
    if ow == "-1" or ow == "reverse":
        return -1
    if ow in ("no", "alternating", "reversible"):
        return 0
    if tags.get("junction") in ("roundabout", "circular"):
        return 1
    if tags.get("highway") in ("motorway", "motorway_link"):
        return 1
    return 0


def tag_built_up(tags: Dict[str, str]) -> Optional[bool]:
    """Built-up state from the way's own Polish tagging (rule 1), else None."""
    for k in _BUILT_UP_TAGS:
        v = tags.get(k)
        if not v:
            continue
        if v in _URBAN_VALUES or v.startswith("PL:zone") or (k == "zone:maxspeed" and v):
            return True
        if v in _RURAL_VALUES:
            return False
    return None


def _is_expressway(tags: Dict[str, str]) -> bool:
    return tags.get("motorroad") == "yes" or any(
        tags.get(k) == "PL:expressway" for k in ("source:maxspeed", "maxspeed:type", "maxspeed"))


def implicit_limit(tags: Dict[str, str], built_up: Optional[bool],
                   oneway: Optional[int] = None) -> Tuple[int, str]:
    """Polish default limit for a way without a usable ``maxspeed``."""
    hw = tags.get("highway", "")
    ow = way_oneway(tags) if oneway is None else oneway
    if hw in ("motorway", "motorway_link") or any(
            tags.get(k) == "PL:motorway" for k in ("source:maxspeed", "maxspeed:type", "maxspeed")):
        return PL_MOTORWAY, BASIS_CLASS
    if _is_expressway(tags):
        return (PL_EXPRESSWAY if _is_dual(tags, ow) else PL_EXPRESSWAY_SINGLE), BASIS_CLASS
    if hw == "living_street" or tags.get("source:maxspeed") == "PL:living_street":
        return PL_LIVING_STREET, BASIS_BUILT_UP
    if built_up:
        return PL_BUILT_UP, BASIS_BUILT_UP
    if hw in ("trunk", "trunk_link", "primary", "primary_link") and _is_dual(tags, ow) \
            and _lanes(tags) >= 2:
        return PL_RURAL_DUAL, BASIS_CLASS
    return PL_RURAL, BASIS_CLASS


def explicit_limit(tags: Dict[str, str], direction: str) -> Optional[int]:
    """Signed limit for the travel direction, or None.

    With ``direction == "unknown"`` and different forward/backward values the
    lower one is returned (conservative)."""
    keys = {"forward": ("maxspeed:forward", "maxspeed"),
            "backward": ("maxspeed:backward", "maxspeed")}
    if direction in keys:
        for k in keys[direction]:
            v, pl = parse_maxspeed(tags.get(k))
            if v is None and pl:
                v = _pl_value(pl)
            if v is not None:
                return v
        return None
    vals = [explicit_limit(tags, d) for d in ("forward", "backward")]
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


def infer_limit(way: Way, direction: str = "unknown", match_confidence: float = 1.0) -> SpeedLimit:
    """Speed limit on ``way`` in ``direction`` (see module doc)."""
    tags = way.tags
    urban, _ = implicit_limit(tags, True, way.oneway)
    rural, _ = implicit_limit(tags, False, way.oneway)
    common = dict(source="map", way_name=way.name, way_id=way.id, highway=way.highway,
                  built_up=way.built_up, urban_default_kmh=urban, rural_default_kmh=rural)
    v = explicit_limit(tags, direction)
    if v is not None:
        conf = 0.9 if direction != "unknown" or "maxspeed" in tags else 0.7
        return SpeedLimit(v, basis=BASIS_EXPLICIT, confidence=round(conf * match_confidence, 3),
                          **common)
    v, basis = implicit_limit(tags, way.built_up, way.oneway)
    if basis == BASIS_CLASS and v in (PL_MOTORWAY, PL_EXPRESSWAY, PL_EXPRESSWAY_SINGLE):
        conf = 0.7
    elif way.highway == "living_street":
        conf = 0.75
    else:
        conf = BUILT_UP_CONFIDENCE.get(way.built_up_basis, 0.3)
    if way.highway == "service":
        conf = min(conf, 0.3)        # parking aisles, driveways: limit is rarely meaningful
    if way.highway.endswith("_link"):
        conf = min(conf, 0.4)
    return SpeedLimit(v, basis=basis, confidence=round(conf * match_confidence, 3), **common)


def speed_limit(match: Optional[MapMatch]) -> Optional[SpeedLimit]:
    """The limit for a :class:`MapMatch` (None for no match)."""
    if match is None:
        return None
    return infer_limit(match.way, match.direction, match.confidence)


# ── geometry ──────────────────────────────────────────────────────────────

def _angle_diff(a: float, b: float) -> float:
    d = abs((a - b) % 360.0)
    return 360.0 - d if d > 180.0 else d


def _bearing(ax: float, ay: float, bx: float, by: float) -> float:
    return math.degrees(math.atan2(bx - ax, by - ay)) % 360.0


def _project_seg(px: float, py: float, ax: float, ay: float, bx: float, by: float
                 ) -> Tuple[float, float, float]:
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    u = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    qx, qy = ax + u * dx, ay + u * dy
    return math.hypot(px - qx, py - qy), qx, qy


# ── the map ───────────────────────────────────────────────────────────────

class OsmSpeedMap:
    """Ways + grid index; :meth:`match` and :meth:`lookup` are the API."""

    def __init__(self, extract: dict, cell_m: float = 64.0, radius_m: float = 25.0,
                 heading_weight: float = 0.25, wrong_way_penalty: float = 40.0,
                 service_penalty: float = 5.0, min_heading_speed_kmh: float = 5.0) -> None:
        if extract.get("format") != "osm-speed-extract":
            raise ValueError("not an osm-speed-extract file")
        self.meta = {k: v for k, v in extract.items() if k not in ("ways", "places")}
        self.cell_m = cell_m
        self.radius_m = radius_m
        self.heading_weight = heading_weight
        self.wrong_way_penalty = wrong_way_penalty
        self.service_penalty = service_penalty
        self.min_heading_speed_kmh = min_heading_speed_kmh
        scale = float(extract.get("scale", 100000))
        s, w, n, e = extract.get("bbox") or (0.0, 0.0, 0.0, 0.0)
        raw_ways = extract.get("ways", [])
        if not (s or n) and raw_ways:
            s = n = raw_ways[0]["c"][0] / scale
            w = e = raw_ways[0]["c"][1] / scale
        self.lat0, self.lon0 = (s + n) / 2.0, (w + e) / 2.0
        self._ky = 110574.0
        self._kx = 111320.0 * math.cos(math.radians(self.lat0))
        self.ways: List[Way] = []
        vertex_ways: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for rw in raw_ways:
            c = rw["c"]
            tags = dict(rw.get("t") or {})
            xy = [self.to_xy(c[i] / scale, c[i + 1] / scale) for i in range(0, len(c) - 1, 2)]
            if len(xy) < 2:
                continue
            wi = len(self.ways)
            self.ways.append(Way(id=rw["id"], tags=tags, xy=xy, oneway=way_oneway(tags)))
            for i in range(0, len(c) - 1, 2):
                vertex_ways[(c[i], c[i + 1])].append(wi)
        self._places = [(self.to_xy(p["c"][0] / scale, p["c"][1] / scale),
                         PLACE_RADIUS_M.get(p.get("place", ""), 0.0), p.get("name", ""))
                        for p in extract.get("places", [])]
        self._link(vertex_ways, raw_ways, scale)
        self._resolve_built_up()
        self._build_index()

    # ── construction ──────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: str = DEFAULT_EXTRACT, **kw) -> "OsmSpeedMap":
        with open(path, "rb") as f:
            raw = f.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return cls(json.loads(raw.decode("utf-8")), **kw)

    def to_xy(self, lat: float, lon: float) -> Tuple[float, float]:
        return (lon - self.lon0) * self._kx, (lat - self.lat0) * self._ky

    def to_latlon(self, x: float, y: float) -> Tuple[float, float]:
        return self.lat0 + y / self._ky, self.lon0 + x / self._kx

    def _link(self, vertex_ways, raw_ways, scale) -> None:
        """Junction vertices (shared by ≥ 2 non-service ways) and adjacency."""
        self._adj: List[set] = [set() for _ in self.ways]
        idx_of = {}
        k = 0
        for rw in raw_ways:
            if len(rw["c"]) >= 4:
                idx_of[id(rw)] = k
                k += 1
        for rw in raw_ways:
            wi = idx_of.get(id(rw))
            if wi is None:
                continue
            way = self.ways[wi]
            c = rw["c"]
            for vi, i in enumerate(range(0, len(c) - 1, 2)):
                others = [o for o in vertex_ways[(c[i], c[i + 1])] if o != wi]
                if not others:
                    continue
                self._adj[wi].update(others)
                if way.highway != "service" and any(
                        self.ways[o].highway != "service" for o in others):
                    way.junctions.append(vi)

    def _resolve_built_up(self) -> None:
        strong: List[Optional[bool]] = []
        for w in self.ways:
            b = tag_built_up(w.tags)
            if b is not None:
                w.built_up, w.built_up_basis = b, "tag"
            elif w.highway in ("residential", "living_street"):
                w.built_up, w.built_up_basis = True, "road class"
            elif w.highway in ("motorway", "motorway_link"):
                w.built_up, w.built_up_basis = False, "road class"   # limit is class-based
            strong.append(w.built_up)

        def vote(wi: int) -> Optional[bool]:
            w = self.ways[wi]
            if w.highway in ("motorway", "motorway_link"):
                return None                      # says nothing about the area
            if strong[wi] is not None:
                return strong[wi]
            v, _ = parse_maxspeed(w.tags.get("maxspeed"))
            if v == PL_BUILT_UP:
                return True
            if v == PL_RURAL:
                return False
            return None

        for wi, w in enumerate(self.ways):
            if w.built_up is not None:
                continue
            seen = {wi}
            frontier = {wi}
            decided = None
            for _hop in range(2):
                nxt = set()
                for f in frontier:
                    nxt.update(o for o in self._adj[f] if o not in seen)
                seen |= nxt
                votes = Counter(v for v in (vote(o) for o in nxt
                                            if self.ways[o].highway != "service") if v is not None)
                if votes:
                    decided = votes[True] >= votes[False]
                    break
                frontier = nxt
            if decided is not None:
                w.built_up, w.built_up_basis = decided, "neighbour"
                continue
            px, py = w.xy[len(w.xy) // 2]
            if any(math.hypot(px - x, py - y) <= r for (x, y), r, _ in self._places):
                w.built_up, w.built_up_basis = True, "place"
            else:
                w.built_up, w.built_up_basis = False, "default"

    def _build_index(self) -> None:
        cs = self.cell_m
        self._grid: Dict[Tuple[int, int], List[Tuple[int, int]]] = defaultdict(list)
        n = 0
        for wi, w in enumerate(self.ways):
            for si in range(len(w.xy) - 1):
                (ax, ay), (bx, by) = w.xy[si], w.xy[si + 1]
                for ix in range(int(math.floor(min(ax, bx) / cs)), int(math.floor(max(ax, bx) / cs)) + 1):
                    for iy in range(int(math.floor(min(ay, by) / cs)), int(math.floor(max(ay, by) / cs)) + 1):
                        self._grid[(ix, iy)].append((wi, si))
                n += 1
        self.segment_count = n

    # ── queries ───────────────────────────────────────────────────────────

    def candidates(self, x: float, y: float, r: float) -> Iterable[Tuple[int, int]]:
        cs = self.cell_m
        seen = set()
        for ix in range(int(math.floor((x - r) / cs)), int(math.floor((x + r) / cs)) + 1):
            for iy in range(int(math.floor((y - r) / cs)), int(math.floor((y + r) / cs)) + 1):
                for key in self._grid.get((ix, iy), ()):
                    if key not in seen:
                        seen.add(key)
                        yield key

    def match(self, pos: Position, radius_m: Optional[float] = None) -> Optional[MapMatch]:
        """Best way within ``radius_m`` of ``pos`` (None if nothing is near)."""
        r = self.radius_m if radius_m is None else radius_m
        px, py = self.to_xy(pos.lat, pos.lon)
        use_heading = pos.heading_deg is not None and (
            pos.speed_kmh is None or pos.speed_kmh >= self.min_heading_speed_kmh)
        best = None
        second_cost = None
        for wi, si in self.candidates(px, py, r):
            w = self.ways[wi]
            (ax, ay), (bx, by) = w.xy[si], w.xy[si + 1]
            d, qx, qy = _project_seg(px, py, ax, ay, bx, by)
            if d > r:
                continue
            cost = d + (self.service_penalty if w.highway == "service" else 0.0)
            direction, angle = "unknown", None
            if use_heading:
                seg_b = _bearing(ax, ay, bx, by)
                fwd = _angle_diff(pos.heading_deg, seg_b)
                bwd = 180.0 - fwd
                options = []
                if w.oneway >= 0:
                    options.append((fwd, "forward", 0.0))
                if w.oneway <= 0:
                    options.append((bwd, "backward", 0.0))
                if w.oneway == 1:
                    options.append((bwd, "backward", self.wrong_way_penalty))
                if w.oneway == -1:
                    options.append((fwd, "forward", self.wrong_way_penalty))
                angle, direction, pen = min(options, key=lambda o: o[0] * self.heading_weight + o[2])
                cost += angle * self.heading_weight + pen
            elif w.oneway == 1:
                direction = "forward"
            elif w.oneway == -1:
                direction = "backward"
            if best is None or cost < best[0]:
                if best is not None and best[1] != wi:
                    second_cost = best[0] if second_cost is None else min(second_cost, best[0])
                best = (cost, wi, si, d, direction, angle, qx, qy)
            elif wi != best[1] and (second_cost is None or cost < second_cost):
                second_cost = cost
        if best is None:
            return None
        cost, wi, si, d, direction, angle, qx, qy = best
        w = self.ways[wi]
        conf = max(0.0, 1.0 - d / (2.0 * r))
        if angle is not None:
            conf *= max(0.2, 1.0 - angle / 90.0)
        if second_cost is not None:
            gap = second_cost - cost             # metres of cost margin to another way
            conf *= 0.5 + 0.5 * min(1.0, gap / 10.0)
        jd = None
        for vi in w.junctions:
            jx, jy = w.xy[vi]
            dd = math.hypot(qx - jx, qy - jy)
            jd = dd if jd is None or dd < jd else jd
        return MapMatch(way=w, segment=si, distance_m=round(d, 2), direction=direction,
                        angle_deg=None if angle is None else round(angle, 1),
                        cost=round(cost, 2), confidence=round(conf, 3),
                        junction_dist_m=None if jd is None else round(jd, 1), point=(qx, qy))

    def lookup(self, pos: Position) -> Tuple[Optional[MapMatch], Optional[SpeedLimit]]:
        m = self.match(pos)
        return m, speed_limit(m)

    def stats(self) -> Dict[str, int]:
        explicit = sum(1 for w in self.ways if any(
            k in w.tags for k in ("maxspeed", "maxspeed:forward", "maxspeed:backward")))
        return {"ways": len(self.ways), "explicit_maxspeed": explicit,
                "segments": self.segment_count, "cells": len(self._grid),
                "places": len(self._places)}


def way_bearing(way: Way, segment: int = 0) -> float:
    (ax, ay), (bx, by) = way.xy[segment], way.xy[segment + 1]
    return _bearing(ax, ay, bx, by)
