"""
Tunables of the main-screen scene: the one place to adjust how much it shows.

Defaults are the "quiet road" look: road and lead vehicle dominate, the
scenery is a faint band over the horizon, information appears when it
matters. Every name below is read where the thing is drawn, so changing a
value changes the picture; nothing else needs editing.

Overriding without touching the code: put a JSON object with any of these
names (lower case) into ``mfd_scene_tuning.json`` next to ``user_settings.json``
(or point the environment variable ``MFD_SCENE_TUNING`` at a file), e.g.

    {"signs_max_on": 6, "beam_style": "hatch", "scenery_brightness": 0.7}

Unknown names and values of the wrong type are ignored (logged once).
The file is read when the scene is built (UI start).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

FILE_NAME = "mfd_scene_tuning.json"
ENV_VAR = "MFD_SCENE_TUNING"

BEAM_STYLES = ("outline", "hatch_light", "hatch", "off")
LAMP_CHIP_MODES = ("none", "high", "all")
ROADSIDE_MODES = ("off", "sparse", "normal")
AMBIENT_PANELS = ("plain", "level")


class Tuning:
    """Named settings with their defaults (documented in docs/MFD_MAIN_SCREEN.md)."""

    # ── signs (one row under the speed / limit line) ──
    signs_max_on = 4            # rules in effect shown; the rest become "+n"
    signs_max_ahead = 2         # warning signs shown, nearest first
    warning_max_m = 400         # a warning is shown while its hazard is expected within this (m)
    sign_hold_s = 4.0           # keep the last rules / warnings this long after the feed stops
                                # carrying them (bridges gaps; 0 = drop at once)

    # ── lead vehicle read-out ──
    gap_show_below_s = 2.5      # time gap shown when shorter than this (99 = always, 0 = never)
    closing_show_kmh = 5.0      # closing / opening speed shown from this difference (0 = always,
                                # 999 = never); the arrow follows the same rule
    lead_brackets = True        # target brackets around the lead vehicle

    # ── scenery ──
    scenery_brightness = 0.40   # backdrop strip brightness, 0..1 (1 = full)
    scenery_band_px = 34        # rows of the backdrop kept above the horizon (0 = all of it)
    roadside = "sparse"         # off | sparse | normal: label-driven roadside (posts, trees, bays)
    grid_brightness = 0.45      # floor grid on the road
    pad_brightness = 0.55       # floor grid under the parked car
    edge_brightness = 0.5       # road edges
    enclosure_brightness = 0.6  # tunnel / car-park frames

    # ── weather ──
    weather_density = 0.5       # share of rain streaks / snow flakes kept, 0..1
    weather_brightness = 0.45   # their brightness, 0..1
    fog_band = True

    # ── headlamps ──
    beam_style = "outline"      # outline | hatch_light | hatch | off

    # ── HUD extras ──
    path_chevrons = False       # the model's planned path in the lane
    lamp_chips = "high"         # none | high (only the high-beam chip) | all
    cruise_chip = True          # "SET nn" under the speed while cruise is engaged

    # ── side panels ──
    side_ticks = True           # corner ticks on the six panels
    ambient_panel = "plain"     # plain (empty, as originally) | level (mode + level bar)

    _CHOICES = {"beam_style": BEAM_STYLES, "lamp_chips": LAMP_CHIP_MODES,
                "roadside": ROADSIDE_MODES, "ambient_panel": AMBIENT_PANELS}

    def __init__(self, overrides: Optional[Mapping[str, Any]] = None):
        if overrides:
            self.apply(overrides)

    @classmethod
    def names(cls):
        return [n for n, v in vars(cls).items()
                if not n.startswith("_") and not callable(v) and not isinstance(v, classmethod)]

    def apply(self, overrides: Mapping[str, Any]) -> Dict[str, Any]:
        """Set the given names; returns what was accepted."""
        done: Dict[str, Any] = {}
        known = set(self.names())
        for name, value in overrides.items():
            if name not in known:
                logger.warning("scene tuning: unknown name %r ignored", name)
                continue
            default = getattr(type(self), name)
            if isinstance(default, bool):
                ok = isinstance(value, bool)
            elif isinstance(default, (int, float)):
                ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            else:
                ok = isinstance(value, str) and value in self._CHOICES.get(name, (value,))
            if not ok:
                logger.warning("scene tuning: %s=%r ignored (expected like %r)", name, value, default)
                continue
            setattr(self, name, type(default)(value) if not isinstance(default, str) else value)
            done[name] = value
        return done

    def as_dict(self) -> Dict[str, Any]:
        return {n: getattr(self, n) for n in self.names()}


def load(path: Optional[str] = None) -> Tuning:
    """The tuning with the overrides of the JSON file, if there is one."""
    t = Tuning()
    if path is None:
        path = os.environ.get(ENV_VAR)
    if path is None:
        try:
            from ...persistence import get_settings
            path = os.path.join(str(get_settings().config_dir), FILE_NAME)
        except Exception:      # noqa: BLE001 - no settings manager (tests, tools)
            return t
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return t
    except (OSError, ValueError) as exc:
        logger.warning("scene tuning: %s not read (%s)", path, exc)
        return t
    if isinstance(data, dict):
        done = t.apply(data)
        if done:
            logger.info("scene tuning overrides from %s: %s", path, done)
    return t
