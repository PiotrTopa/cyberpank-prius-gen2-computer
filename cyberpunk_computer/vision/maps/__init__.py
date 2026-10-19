"""Offline map data for vision: speed limit from an OpenStreetMap extract.

* :mod:`.osm_speed_limit` — extract loader, grid index, map matching and the
  Polish speed-limit rules (``OsmSpeedMap``, ``Position``, ``speed_limit``).

The extract is produced by ``tools/osm_extract.py``; the bundled
``data/gdansk_15km.json.gz`` covers ~15×15 km around Gdańsk
(© OpenStreetMap contributors, ODbL 1.0).
"""

from .osm_speed_limit import (
    BASIS_BUILT_UP, BASIS_CLASS, BASIS_EXPLICIT, DEFAULT_EXTRACT, MapMatch, OsmSpeedMap,
    Position, SpeedLimit, Way, implicit_limit, infer_limit, parse_maxspeed, speed_limit,
)

__all__ = [
    "BASIS_BUILT_UP", "BASIS_CLASS", "BASIS_EXPLICIT", "DEFAULT_EXTRACT", "MapMatch",
    "OsmSpeedMap", "Position", "SpeedLimit", "Way", "implicit_limit", "infer_limit",
    "parse_maxspeed", "speed_limit",
]
