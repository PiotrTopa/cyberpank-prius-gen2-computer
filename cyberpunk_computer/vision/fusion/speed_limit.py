"""Speed-limit fusion: map prior + camera sightings → one current limit.

Roadmap 4.2 "Fusion". Pure and thread-free like :mod:`..auto_lights`. Two
inputs, both carrying their own timestamps:

* :meth:`SpeedLimitFusion.update_map` — every GPS fix: the map's
  :class:`~cyberpunk_computer.vision.maps.SpeedLimit` for the matched way
  (or None), the odometer in metres and whether we are at a junction;
* :meth:`SpeedLimitFusion.add_sighting` — a confirmed camera sighting from
  the sign detector (``vision/detectors/``, a track over 3+ frames):
  :class:`Sighting` ``(value_kmh, score, side, t, kind)``.

Rules, in priority order (the first that applies gives the limit):

1. **Camera override** — a sighting whose value *disagrees* with the
   current limit is a candidate only if it is on our side (``right`` or
   ``overhead``; ``left`` counts on a oneway road), ``score >= min_score``
   and we are not at a junction (the sign may belong to the crossing road).
   ``confirm_count`` (2) consistent candidates (same value, within
   ``confirm_window_s``) set the override (source ``camera``, confidence
   0.6 + 0.3·mean score). A different value restarts the count.
2. **End-of-limit sign** (``kind="end_limit"``, B-34) — override with the
   *default* limit of the current road (``urban_default_kmh`` /
   ``rural_default_kmh`` of the map limit, by the built-up state), source
   ``default``.
3. **Map explicit** ``maxspeed`` (source ``map``).
4. **Built-up sign** (``built_up_begin`` D-42 / ``built_up_end`` D-43) —
   replaces the map's built-up guess for the implicit default: 50 inside,
   the road-class default outside; source ``camera``. Also clears an
   override. Lasts ``built_up_max_distance_m`` (20 km) or until the
   opposite sign.
5. **Map implicit** (source ``map``).

Overrides (1, 2) **expire** when we pass a junction more than
``junction_grace_m`` (50 m) after the sign — the Polish rule: a B-33 limit
ends at the next intersection unless repeated — or after
``override_max_distance_m`` (3 km) / ``override_max_age_s`` (10 min),
whichever comes first; then the map prior is back. A sighting that *agrees*
with the current limit raises its confidence by ``agree_boost``·score
(up to 1.0), refreshes an override's expiry, clears pending candidates and
turns source ``map`` into ``map+camera``. Without a map match the last map
limit is held for ``map_hold_s`` (10 s), then the map part is None. A new map value is taken
after ``map_confirm_fixes`` (2) consecutive fixes, so a one-fix match on a
crossing street or a link does not flip the limit.

``confidence`` is a heuristic 0..1 for display and for the future UI
policy; nothing here acts on the car. Replay a track offline::

    python -m cyberpunk_computer.vision.fusion.speed_limit --track drive.csv
    python -m cyberpunk_computer.vision.fusion.speed_limit --track drive.gpx \\
        --sightings signs.csv --map extract.json.gz --all

Track CSV: ``t,lat,lon[,heading_deg,speed_kmh]`` (heading/speed derived
from consecutive points when missing; ``#`` lines are comments); GPX:
``<trkpt lat lon><time>``. Sightings CSV: ``t,value_kmh,score,side[,kind]``.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from ..maps.osm_speed_limit import (
    BASIS_BUILT_UP, BASIS_CLASS, PL_BUILT_UP, PL_RURAL, Position, SpeedLimit,
)

KINDS = ("limit", "end_limit", "built_up_begin", "built_up_end")


@dataclass(frozen=True)
class Sighting:
    """One confirmed sign from the detector lane.

    ``value_kmh`` is the number on a limit sign (None for the other kinds);
    ``side``: ``right`` | ``left`` | ``overhead`` | ``unknown``;
    ``kind``: ``limit`` | ``end_limit`` | ``built_up_begin`` | ``built_up_end``.
    """
    value_kmh: Optional[int]
    score: float
    side: str
    t: float
    kind: str = "limit"


@dataclass(frozen=True)
class FusionParams:
    confirm_count: int = 2
    confirm_window_s: float = 30.0
    min_score: float = 0.5
    agree_boost: float = 0.25
    junction_grace_m: float = 50.0
    expire_at_junction: bool = True
    override_max_distance_m: float = 3000.0
    override_max_age_s: float = 600.0
    built_up_max_distance_m: float = 20000.0
    map_hold_s: float = 10.0
    map_confirm_fixes: int = 2     # consecutive fixes before a new map value is taken


@dataclass(frozen=True)
class FusedLimit:
    t: float
    value_kmh: Optional[int]
    source: str                 # map | map+camera | camera | default | none
    confidence: float
    basis: str = ""
    road_name: Optional[str] = None
    note: str = ""              # what the last input did (for the replay log)
    changed: bool = False


@dataclass
class _Override:
    value: int
    source: str                 # camera | default
    basis: str
    confidence: float
    t: float
    odo: float


class SpeedLimitFusion:
    """Stateful, pure (no threads, no I/O) current-limit keeper."""

    def __init__(self, params: Optional[FusionParams] = None) -> None:
        self.p = params or FusionParams()
        self.reset()

    def reset(self) -> None:
        self.map_limit: Optional[SpeedLimit] = None
        self._map_t: Optional[float] = None
        self._map_cand = 0
        self._map_cand_v: Optional[int] = None
        self.override: Optional[_Override] = None
        self.built_up_sign: Optional[Tuple[bool, float]] = None   # (inside, odo)
        self._pending: List[Sighting] = []
        self._agree_value: Optional[int] = None
        self._agree_bonus = 0.0
        self.t = 0.0
        self.odo = 0.0
        self.at_junction = False
        self.oneway = False
        self.last: Optional[FusedLimit] = None

    # ── inputs ────────────────────────────────────────────────────────────

    def update_map(self, t: float, limit: Optional[SpeedLimit], odo_m: float,
                   at_junction: bool = False, oneway: bool = False) -> FusedLimit:
        """One GPS step with the map's limit for the matched way (or None)."""
        self.t, self.odo = t, odo_m
        self.at_junction, self.oneway = at_junction, oneway
        if limit is not None and limit.value_kmh is not None:
            cur = self.map_limit
            if cur is None or limit.value_kmh == cur.value_kmh:
                self.map_limit, self._map_t, self._map_cand = limit, t, 0
            else:
                # debounce: a one-fix match on a crossing/link way does not flip the limit
                self._map_cand = self._map_cand + 1 if self._map_cand_v == limit.value_kmh else 1
                self._map_cand_v = limit.value_kmh
                if self._map_cand >= self.p.map_confirm_fixes:
                    self.map_limit, self._map_t, self._map_cand = limit, t, 0
        elif self._map_t is not None and t - self._map_t > self.p.map_hold_s:
            self.map_limit = None
        note = self._expire()
        return self._emit(note)

    def add_sighting(self, s: Sighting) -> FusedLimit:
        """A camera sighting at the current position/context."""
        self.t = max(self.t, s.t)
        cur = self._resolve()
        ours = s.side in ("right", "overhead") or (s.side == "left" and self.oneway)
        if s.kind == "built_up_begin" or s.kind == "built_up_end":
            if not ours:
                return self._emit(f"ignored {s.kind} ({s.side})")
            self.built_up_sign = (s.kind == "built_up_begin", self.odo)
            self.override = None
            self._pending.clear()
            return self._emit(s.kind)
        if s.kind == "end_limit":
            if not ours or s.score < self.p.min_score:
                return self._emit(f"ignored end_limit ({s.side}, {s.score:.2f})")
            v, basis = self._default()
            self._pending.clear()
            if v is None:
                self.override = None
                return self._emit("end_limit (no default)")
            self.override = _Override(v, "default", basis, 0.6, s.t, self.odo)
            return self._emit("end_limit")
        if s.kind != "limit" or s.value_kmh is None:
            return self._emit(f"ignored kind {s.kind!r}")

        if cur.value_kmh == s.value_kmh:
            self._pending.clear()
            if self._agree_value != s.value_kmh:
                self._agree_value, self._agree_bonus = s.value_kmh, 0.0
            self._agree_bonus = min(1.0, self._agree_bonus + self.p.agree_boost * s.score)
            if self.override is not None:
                self.override.t, self.override.odo = s.t, self.odo   # repeated sign
            return self._emit("agree")
        if not ours:
            return self._emit(f"ignored {s.value_kmh} ({s.side} side)")
        if self.at_junction:
            return self._emit(f"ignored {s.value_kmh} (junction)")
        if s.score < self.p.min_score:
            return self._emit(f"ignored {s.value_kmh} (score {s.score:.2f})")
        if self._pending and (self._pending[0].value_kmh != s.value_kmh
                              or s.t - self._pending[0].t > self.p.confirm_window_s):
            self._pending.clear()
        self._pending.append(s)
        if len(self._pending) < self.p.confirm_count:
            return self._emit(f"pending {s.value_kmh} ({len(self._pending)}/{self.p.confirm_count})")
        mean = sum(x.score for x in self._pending) / len(self._pending)
        self.override = _Override(s.value_kmh, "camera", "sign", round(0.6 + 0.3 * mean, 3),
                                  s.t, self.odo)
        self._pending.clear()
        return self._emit(f"override {s.value_kmh}")

    # ── internals ─────────────────────────────────────────────────────────

    def _built_up(self) -> Optional[bool]:
        if self.built_up_sign is not None:
            return self.built_up_sign[0]
        return self.map_limit.built_up if self.map_limit is not None else None

    def _default(self) -> Tuple[Optional[int], str]:
        """Implicit limit of the current road, honouring a built-up sign."""
        inside = self._built_up()
        m = self.map_limit
        if m is not None:
            v = m.urban_default_kmh if inside else m.rural_default_kmh
            if v is not None:
                return v, BASIS_BUILT_UP if inside and v <= PL_BUILT_UP else BASIS_CLASS
        if inside is None:
            return None, ""
        return (PL_BUILT_UP, BASIS_BUILT_UP) if inside else (PL_RURAL, BASIS_CLASS)

    def _expire(self) -> str:
        p, o = self.p, self.override
        note = ""
        if o is not None:
            dist = self.odo - o.odo
            if p.expire_at_junction and self.at_junction and dist > p.junction_grace_m:
                note = f"{o.source} {o.value} expired (junction)"
            elif dist > p.override_max_distance_m:
                note = f"{o.source} {o.value} expired (distance)"
            elif self.t - o.t > p.override_max_age_s:
                note = f"{o.source} {o.value} expired (age)"
            if note:
                self.override = None
        if self.built_up_sign is not None and \
                self.odo - self.built_up_sign[1] > p.built_up_max_distance_m:
            self.built_up_sign = None
            note = (note + "; " if note else "") + "built-up sign expired"
        if self._pending and self.t - self._pending[-1].t > p.confirm_window_s:
            self._pending.clear()
        return note

    def _resolve(self) -> FusedLimit:
        m = self.map_limit
        name = m.way_name if m is not None else None
        if self.override is not None:
            o = self.override
            v, src, basis, conf = o.value, o.source, o.basis, o.confidence
        elif m is not None and m.basis == "explicit maxspeed":
            v, src, basis, conf = m.value_kmh, "map", m.basis, m.confidence
        elif self.built_up_sign is not None:
            v, basis = self._default()
            src, conf = "camera", 0.7
        elif m is not None:
            v, src, basis, conf = m.value_kmh, "map", m.basis, m.confidence
        else:
            return FusedLimit(self.t, None, "none", 0.0, road_name=name)
        if v is not None and v == self._agree_value and self._agree_bonus > 0:
            conf = min(1.0, conf + self._agree_bonus)
            if src == "map":
                src = "map+camera"
        return FusedLimit(self.t, v, src, round(conf, 3), basis, name)

    def _emit(self, note: str = "") -> FusedLimit:
        r = self._resolve()
        if self._agree_value is not None and r.value_kmh != self._agree_value:
            self._agree_value, self._agree_bonus = None, 0.0
            r = self._resolve()
        changed = self.last is None or r.value_kmh != self.last.value_kmh
        out = FusedLimit(r.t, r.value_kmh, r.source, r.confidence, r.basis, r.road_name,
                         note, changed)
        self.last = out
        return out


# ── tracks + replay ───────────────────────────────────────────────────────

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def complete_track(points: Sequence[Position]) -> List[Position]:
    """Fill missing heading/speed from consecutive points."""
    out: List[Position] = []
    for i, p in enumerate(points):
        h, v = p.heading_deg, p.speed_kmh
        if h is None or v is None:
            q, r = (points[i - 1], p) if i > 0 else ((p, points[1]) if len(points) > 1 else (p, p))
            d = haversine_m(q.lat, q.lon, r.lat, r.lon)
            dt = r.t - q.t
            if h is None and d > 0.5:
                h = bearing_deg(q.lat, q.lon, r.lat, r.lon)
            if v is None and dt > 0:
                v = d / dt * 3.6
        out.append(Position(p.lat, p.lon, h, v, p.t))
    return out


def _num(row: dict, *keys: str) -> Optional[float]:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return float(v)
    return None


def load_track(path: str) -> List[Position]:
    """Track from CSV (``t,lat,lon[,heading_deg,speed_kmh]``) or GPX."""
    if path.lower().endswith(".gpx"):
        import xml.etree.ElementTree as ET
        from datetime import datetime
        pts = []
        for el in ET.parse(path).getroot().iter():
            if not el.tag.endswith("trkpt"):
                continue
            t = 0.0
            for c in el:
                if c.tag.endswith("time") and c.text:
                    t = datetime.fromisoformat(c.text.strip().replace("Z", "+00:00")).timestamp()
            pts.append(Position(float(el.get("lat")), float(el.get("lon")), None, None, t))
        return complete_track(pts)
    with open(path, newline="") as f:
        rows = list(csv.DictReader(line for line in f if not line.startswith("#")))
    pts = [Position(float(r["lat"]), float(r["lon"]), _num(r, "heading_deg", "heading"),
                    _num(r, "speed_kmh", "speed"), float(r["t"])) for r in rows]
    return complete_track(pts)


def load_sightings(path: str) -> List[Sighting]:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(line for line in f if not line.startswith("#")))
    out = []
    for r in rows:
        v = r.get("value_kmh") or ""
        out.append(Sighting(int(float(v)) if v.strip() else None, float(r.get("score") or 1.0),
                            (r.get("side") or "right").strip(), float(r["t"]),
                            (r.get("kind") or "limit").strip()))
    return out


def replay(track: Sequence[Position], osm_map=None,
           sightings: Iterable[Sighting] = (), params: Optional[FusionParams] = None
           ) -> List[FusedLimit]:
    """Run a fresh :class:`SpeedLimitService` over a track (+ sightings, by time)."""
    from .service import SpeedLimitService
    svc = SpeedLimitService(osm_map=osm_map, params=params)
    events: List[Tuple[float, int, object]] = [(p.t, 0, p) for p in track]
    events += [(s.t, 1, s) for s in sightings]
    events.sort(key=lambda e: (e[0], e[1]))
    out: List[FusedLimit] = []
    for _, kind, ev in events:
        if kind == 0:
            svc.update(ev)
        else:
            svc.add_sighting(ev)
        out.append(svc.fusion.last)
    return out


def changes(timeline: Sequence[FusedLimit]) -> List[FusedLimit]:
    """First entry, every value change, and every non-trivial note."""
    keep = ("override", "expired", "end_limit", "built_up", "pending", "ignored", "agree")
    return [d for i, d in enumerate(timeline)
            if i == 0 or d.changed or any(k in d.note for k in keep)]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m cyberpunk_computer.vision.fusion.speed_limit",
        description="Replay map-matching + speed-limit fusion over a recorded track.")
    ap.add_argument("--track", required=True, help="CSV (t,lat,lon[,heading_deg,speed_kmh]) or GPX")
    ap.add_argument("--sightings", help="CSV t,value_kmh,score,side[,kind]")
    ap.add_argument("--map", help="extract from tools/osm_extract.py (default: bundled Gdańsk)")
    ap.add_argument("--all", action="store_true", help="print every step, not only changes")
    args = ap.parse_args(argv)

    from ..maps.osm_speed_limit import DEFAULT_EXTRACT, OsmSpeedMap
    t0 = time.perf_counter()
    m = OsmSpeedMap.load(args.map or DEFAULT_EXTRACT)
    load_s = time.perf_counter() - t0
    track = load_track(args.track)
    sightings = load_sightings(args.sightings) if args.sightings else []
    t0 = time.perf_counter()
    timeline = replay(track, m, sightings)
    run_s = time.perf_counter() - t0
    show = timeline if args.all else changes(timeline)
    print("time      limit source      conf  basis                 road / note")
    for d in show:
        lim = "  - " if d.value_kmh is None else f"{d.value_kmh:4d}"
        print(f"{time.strftime('%H:%M:%S', time.localtime(d.t))}  {lim}  {d.source:<10} "
              f"{d.confidence:4.2f}  {d.basis:<21} {d.road_name or '-'}"
              f"{'  [' + d.note + ']' if d.note else ''}")
    print(f"{len(track)} fixes, {len(sightings)} sightings, "
          f"{sum(1 for d in timeline if d.changed) - 1} limit changes; "
          f"map load {load_s:.2f} s, {run_s / max(1, len(timeline)) * 1000:.2f} ms/step")
    return 0


if __name__ == "__main__":
    sys.exit(main())
