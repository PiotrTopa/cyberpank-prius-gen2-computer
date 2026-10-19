"""SpeedLimitService: GPS fix → map match → fusion → ``SetVisionStateAction``.

No thread and no GPS here: the backend has no GPS section yet.

**GPS INGEST HOOK.** The future GPS ingest builds a
:class:`~cyberpunk_computer.vision.maps.Position` per fix and calls
:meth:`SpeedLimitService.update`; the returned action goes to the store on the
engine thread, like ``DashcamLightIngest.drain_actions`` does. The sign
detector (``vision/detectors/``) feeds :meth:`SpeedLimitService.add_sighting`
the same way. Both are cheap (map lookup well under 1 ms on the 15 km box).

Published fields (``state.vision``): ``speed_limit_kmh``,
``speed_limit_source`` (``map`` | ``map+camera`` | ``camera`` | ``default`` |
``none``), ``speed_limit_confidence`` (0..1), ``road_name`` and
``position_ok`` (the fix is valid and inside the extract's box).
"""

from __future__ import annotations

import math
from typing import Optional

from ...state.actions import SetVisionStateAction
from ..maps.osm_speed_limit import DEFAULT_EXTRACT, MapMatch, OsmSpeedMap, Position, speed_limit
from .speed_limit import FusedLimit, FusionParams, Sighting, SpeedLimitFusion, haversine_m

MAX_STEP_M = 2000.0      # a jump longer than this (GPS glitch, ferry) is not driven distance


class SpeedLimitService:
    def __init__(self, osm_map: Optional[OsmSpeedMap] = None, path: str = DEFAULT_EXTRACT,
                 params: Optional[FusionParams] = None) -> None:
        self.map = osm_map if osm_map is not None else OsmSpeedMap.load(path)
        self.fusion = SpeedLimitFusion(params)
        self.odo_m = 0.0
        self._last_pos: Optional[Position] = None
        self.last_match: Optional[MapMatch] = None
        self.position_ok = False

    def _inside(self, pos: Position) -> bool:
        if not (isinstance(pos.lat, (int, float)) and isinstance(pos.lon, (int, float))
                and math.isfinite(pos.lat) and math.isfinite(pos.lon)):
            return False
        bbox = self.map.meta.get("bbox")
        if not bbox:
            return True
        s, w, n, e = bbox
        return s <= pos.lat <= n and w <= pos.lon <= e

    def update(self, position: Optional[Position]) -> SetVisionStateAction:
        """GPS INGEST HOOK: one fix (None = no fix) → one state action."""
        ok = position is not None and self._inside(position)
        self.position_ok = ok
        if not ok:
            self.last_match = None
            fused = self.fusion.update_map(position.t if position else self.fusion.t, None,
                                           self.odo_m)
            return self._action(fused, None)
        if self._last_pos is not None:
            d = haversine_m(self._last_pos.lat, self._last_pos.lon, position.lat, position.lon)
            if d <= MAX_STEP_M:
                self.odo_m += d
        self._last_pos = position
        m = self.map.match(position)
        self.last_match = m
        fused = self.fusion.update_map(position.t, speed_limit(m), self.odo_m,
                                       at_junction=m.at_junction if m else False,
                                       oneway=bool(m and m.way.oneway))
        return self._action(fused, m)

    def add_sighting(self, sighting: Sighting) -> SetVisionStateAction:
        """Detector hook: one confirmed sign sighting → one state action."""
        return self._action(self.fusion.add_sighting(sighting), self.last_match)

    def _action(self, f: FusedLimit, m: Optional[MapMatch]) -> SetVisionStateAction:
        return SetVisionStateAction(
            speed_limit_kmh=f.value_kmh, speed_limit_source=f.source,
            speed_limit_confidence=round(f.confidence, 3),
            road_name=m.way.name if m is not None else None,
            position_ok=self.position_ok)
