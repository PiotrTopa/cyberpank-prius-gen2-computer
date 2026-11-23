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
NEXT_LIMIT_MODES = ("off", "ahead", "beside")
ROAD_LABEL_MODES = ("off", "ref", "name")


class Tuning:
    """Named settings with their defaults (documented in docs/MFD_MAIN_SCREEN.md)."""

    # ── top line ──
    show_speed = False          # speed and gear in the scene's top-left corner (the car has a
                                # speedometer; False gives the top line to the signs)

    # ── signs (the top line; a row under it when show_speed is on) ──
    signs_max_on = 6            # rules in effect shown; the rest become "+n"
    signs_max_ahead = 3         # warning signs shown, nearest first
    warning_max_m = 400         # a warning is shown while its hazard is expected within this (m)
    sign_hold_s = 4.0           # keep the last rules / warnings this long after the feed stops
                                # carrying them (bridges gaps; 0 = drop at once)

    # ── lead vehicle read-out ──
    gap_show_below_s = 2.5      # time gap shown when shorter than this (99 = always, 0 = never)
    closing_show_kmh = 5.0      # closing / opening speed shown from this difference (0 = always,
                                # 999 = never); the arrow follows the same rule
    lead_brackets = True        # target brackets around the lead vehicle

    # ── lanes ──
    lane_neighbours = True      # draw the neighbouring lane(s) when their lines are seen
    lane_neighbour_min_prob = 0.5   # an outer lane line counts from this probability
    lane_indicator = True       # "which lane of N" boxes (only with a neighbouring lane)

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
    map_limit = True            # limit sign from the map layer's fused value when the map is
                                # usable (camera sign held by the map, else the map's limit);
                                # False = the camera-only limit as before

    # ── traffic light (roadsense's lane assignment of the ego light) ──
    light_uncertain = True      # "?" beside the signal when roadsense marks its choice ambiguous
    light_alt = True            # the other signal group (e.g. the left-turn light) as a small dim
                                # head on its side, while two groups show different states

    # ── map layer: ambient information from roadsense's map (docs/MFD_MAIN_SCREEN.md 6.5).
    #    Needs a usable map (as map_limit); without one the scene is the vision-only picture ──
    map_mark = True             # a small pin under the link LED while the map is in use
                                # (amber while it dead-reckons)
    map_next_limit = "ahead"    # the next speed-limit change with its distance: off | ahead (first
                                # in the things-ahead list, top-left) | beside (left of the limit sign)
    map_next_limit_road = True  # ... and as a sign at the roadside once within camera range
    map_far_road = True         # the road beyond camera range (100..600 m) up to the horizon
    map_tunnel = True           # distance to the tunnel's exit, the portal within 100 m
    map_lanes = True            # lane boxes from the map's lane count (own direction >= 2 lanes),
                                # an oncoming neighbour lane in its own tint
    map_road_label = "off"      # the road's number plate for a while after it changed:
                                # off | ref | name (number and street name)
    map_road_label_s = 8.0      # ... this long
    road_geo = True             # the road picture itself continues on the map: the lane beyond
                                # the camera's reach, side roads, a roundabout as a ring (the
                                # camera's lanes then end where roadsense says they do); needs
                                # roadsense's fused road geometry and a usable map, else nothing
    road_geo_way = True         # ... a side road chosen by the turn signal drawn as the way on
    map_junction = True         # the junction / roundabout ahead as a schematic at the left edge
                                # (arms, the most likely way on, the exit number). On since
                                # 2026-11-18 (lane L20: off by default, no junction had ever
                                # reached the glass on the recorded drives)
    map_junction_m = 250        # ... from this distance (m); a roundabout stays while on its ring
    map_junction_way = True     # ... the most likely way on bright with an arrow head (it is the
                                # map's guess, not a route); False = all arms alike, no exit number
    map_junction_name = True    # ... with the number or name of the road the way on takes
    map_ahead_s = 12.0          # the next limit appears this many seconds of driving ahead,
    map_ahead_min_m = 150       # ... but not later than this (m)
    map_ahead_max_m = 400       # ... and not earlier than this (m)
    map_min_match = 0.55        # map elements need this match confidence, averaged over ~3 s
                                # (roadsense uses the map from 0.5; they go 0.15 below). Was 0.7:
                                # on the recorded drives the average stayed under it half the time.
                                # roadsense >= L20 reports the confidence of the road, not of the
                                # edge, so it no longer dips at every side road
    map_hold_s = 2.5            # ... and stay this long when the map drops out (a message or two
                                # at side roads), distances advanced by the distance driven
    map_min_conf = 0.5          # a fact about the road ahead / the lanes is shown from this
                                # confidence (path confidence up to it x its own)
    map_path_min_conf = 0.6     # the far road ends at the first real junction below this
    map_max_hacc_m = 20.0       # distances are shown while the position is this accurate (m)
    map_junction_hacc_share = 0.4   # the junction schematic also while the position error is at
                                # most this share of its distance (drawn as approximate: amber
                                # frame, "~" before the distance); dead reckoning is approximate too
    map_far_disagree_m = 8.0    # the far road hides when camera and map differ by more than
                                # this in the road's lateral run over 100 m
    map_far_max_m = 600         # reach of the far road (m)

    # ── radar view (radar.py): ATC-style plan view of the tracked road users ──
    radar_view = True           # the RDR / ROAD button in the bottom-right corner of the road view
    radar_fallback_s = 5.0      # with the radar view chosen, the road view comes back after this long
                                # without radar data (and the radar view when the data returns)
    radar_centre = "centre"     # centre: the car in the middle of the scope | ahead: at the two larger
                                # ranges the centre moves down (more picture ahead, less behind)
    radar_trail = True          # history dots behind moving targets (where they were over the ground)
    radar_sweep = False         # the stepped sweep line with its afterglow (docs 6.8: its cost)

    # ── side panels ──
    side_ticks = True           # corner ticks on the six panels
    ambient_panel = "plain"     # plain (empty, as originally) | level (mode + level bar)

    # ── frame pacing (ui/scene/pacing.py; the UI loop renders only when needed) ──
    fps_scene = 20.0            # scene redraws/s while it animates (driving, camera move, doors);
                                # also the loop rate meanwhile. Between road events (<= 10/s) the
                                # lanes, edges, path and lead ease towards the newest data and
                                # the floor scrolls with the distance, so each frame is new
    fps_loop = 10.0             # loop passes/s while nothing moves and nobody touches the screen
                                # (input and link polling; never above fps_scene)
    fps_touch = 20.0            # frames/s while the screen is used (touch, buttons, drags)
    fps_pulse = 5.0             # frames/s while a panel only pulses (focus shown, edit mode)
    fps_idle = 1.0              # refresh when nothing changed (catches anything time-based)
    touch_hold_s = 2.0          # the touch rate holds this long after the last input

    _CHOICES = {"beam_style": BEAM_STYLES, "lamp_chips": LAMP_CHIP_MODES,
                "roadside": ROADSIDE_MODES, "ambient_panel": AMBIENT_PANELS,
                "map_next_limit": NEXT_LIMIT_MODES, "map_road_label": ROAD_LABEL_MODES}

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
