"""Fusion: temporal voting + state rules per topic (roadmap 4.2).

* :mod:`.speed_limit` — ``SpeedLimitFusion`` (map prior + camera sightings),
  track replay and CLI;
* :mod:`.service`     — ``SpeedLimitService``: GPS fix → map match → fusion →
  ``SetVisionStateAction`` (the hook the future GPS ingest calls).
"""

__all__ = ["FusedLimit", "FusionParams", "Sighting", "SpeedLimitFusion", "SpeedLimitService"]


def __getattr__(name):
    # Lazy, so ``python -m ...fusion.speed_limit`` does not import the
    # module twice (same pattern as vision/__init__.py).
    if name == "SpeedLimitService":
        from .service import SpeedLimitService
        return SpeedLimitService
    if name in ("FusedLimit", "FusionParams", "Sighting", "SpeedLimitFusion"):
        from . import speed_limit
        return getattr(speed_limit, name)
    raise AttributeError(name)
