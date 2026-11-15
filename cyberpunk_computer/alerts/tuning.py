"""
Every tunable of the alert functions, in one place.

Override without editing code: a JSON object ``{"<section>": {"<name>": value}}``
in ``alerts_tuning.json`` in the alerts directory (or the file named by
``BACKEND_ALERTS_TUNING``), read at backend start. Unknown names and values
of the wrong type are ignored and logged. Speeds are TRUE km/h (CAN 0x0B4),
never the speedometer's (+12 %).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SpeedLimitTuning:
    tolerance_kmh: float = 6.0          # over the limit by more than this ...
    tolerance_pct_above_100: float = 0.10   # ... or this share of the limit when the limit is above 100
    delay_s: float = 5.0                # ... for this long -> one notice
    min_conf: float = 0.5               # strong sources (sign, sign_held, map) below this are weak
    weak_policy: str = "high_bar"       # weak sources (map_implicit, vision_default, default): "off" | "high_bar"
    weak_tolerance_kmh: float = 15.0
    weak_delay_s: float = 10.0
    weak_min_conf: float = 0.6
    suppress_after_change_m: float = 150.0   # no warning in the first metres after the limit changed (slip roads)
    rearm_below_kmh: float = 0.0        # re-arm when the true speed drops to limit + this ...
    rearm_hold_s: float = 3.0           # ... for this long (or when the limit value changes)
    min_speed_kmh: float = 20.0
    real_speed_chip: str = "warning"    # "off" | "warning" (chip while the warning shows) | "near_limit"
    chip_over_display_kmh: float = 3.0  # near_limit: the speedometer reads more than limit + this


@dataclass
class StopGoTuning:
    stop_kmh: float = 0.5               # ego counts as stopped below this (true speed)
    stopped_min_s: float = 2.0          # ... for this long before anything can fire
    lead_close_m: float = 10.0          # the lead was within this distance while stopped
    lead_min_prob: float = 0.6          # vision lead probability for the fallback logic
    lead_still_mps: float = 0.6         # |v_rel| below this = the lead stands
    lead_still_s: float = 2.0
    moved_m: float = 3.5                # fallback: the lead is this much farther than at the stop ...
    moved_s: float = 1.0                # ... for this long
    use_tracked: bool = True            # use roadsense's tracked lead + departed cue when present
    green_enabled: bool = True          # evaluate the green-light half (it cannot go "on", see FEATURES.md)
    green_hold_s: float = 2.0           # green and nothing within green_no_lead_m for this long
    green_no_lead_m: float = 15.0
    green_after_red_s: float = 30.0     # the ego light was red at most this long ago


@dataclass
class HeadwayTuning:
    gap_s: float = 1.0                  # coach: time gap below this ...
    gap_hold_s: float = 5.0             # ... for this long
    gap_min_kmh: float = 50.0
    gap_cooldown_s: float = 120.0
    gap_max_per_drive: int = 6
    ttc_s: float = 2.0                  # warning: time to collision below this ...
    ttc_closing_kmh: float = 15.0       # ... with at least this closing speed
    ttc_min_kmh: float = 30.0
    ttc_max_kmh: float = 150.0
    ttc_debounce_s: float = 0.3
    ttc_cooldown_s: float = 10.0
    lead_min_prob: float = 0.8          # vision lead probability (tracked lead: always trusted as a track)
    lead_max_y_m: float = 1.0           # lead within this lateral offset of the camera axis
    coach_max_y_m: float = 1.3
    max_steer_deg: float = 25.0         # suppress in curves (steering wheel angle)
    suppress_when_braking: bool = True
    max_scene_age_s: float = 0.6


@dataclass
class CurveTuning:
    a_lat: float = 2.5                  # comfortable lateral acceleration, m/s2
    a_lat_wet: float = 2.0              # when envd says rain / snow / fog
    min_match_conf: float = 0.9
    lead_time_min_s: float = 3.0        # notice when the curve starts within this ...
    lead_time_max_s: float = 9.0        # ... and this many seconds at the current speed
    max_radius_m: float = 400.0
    min_speed_kmh: float = 40.0
    margin_kmh: float = 5.0             # fire only above the comfortable speed + this
    skip_built_up: bool = True
    junction_window_m: float = 40.0     # a junction this close to the curve start = a turn, not a bend


@dataclass
class TunnelTuning:
    entry_m: float = 120.0              # map: press when the portal is this close (match >= min_match_conf)
    min_match_conf: float = 0.8
    vision_stable_s: float = 3.0        # vision only: "in tunnel" held this long
    map_in_stable_s: float = 1.0
    exit_stable_s: float = 5.0          # out of the tunnel this long = exited
    restore_after_s: float = 20.0       # restore fresh air this long after the exit
    max_recirc_s: float = 600.0         # never keep our recirculation longer than this
    min_press_interval_s: float = 30.0  # rate limit between our presses
    max_presses_per_hour: int = 12
    feedback_timeout_s: float = 6.0     # the climate status must show our press within this
    touch_grace_s: float = 3.0          # a climate change within this of our press is ours
    skip_if_defrost: bool = True
    skip_below_c: float = 3.0           # cold and A/C off: fogging risk -> skip
    min_speed_kmh: float = 5.0


@dataclass
class EcoTuning:
    coast_decel: float = 0.7            # m/s2, lift-off glide + light regen of the NHW20
    reaction_s: float = 1.5
    window_s: float = 3.0               # hint while the target is within [d_coast, d_coast + v * window]
    min_delta_kmh: float = 15.0         # only when at least this much faster than the target
    min_speed_kmh: float = 35.0
    throttle_min_pct: float = 5.0       # only while the accelerator is pressed
    target_roundabout_kmh: float = 30.0
    target_stop_kmh: float = 0.0
    target_give_way_kmh: float = 20.0
    target_built_up_kmh: float = 50.0
    use_vision_signs: bool = True       # camera warning signs (roundabout / stop ahead) without the map
    stop_from_kmh: float = 20.0         # braking score: a stop is a slowdown from above this ...
    stop_to_kmh: float = 3.0            # ... to below this
    keep_stops: int = 50


@dataclass
class WinterTuning:
    ice_c: float = 3.0                  # ice risk at or below (outside temperature, whole degrees)
    rearm_c: float = 5.0                # hysteresis: back above this re-arms
    settle_s: float = 30.0              # ignore the first seconds after READY (sensor heat soak)
    hold_s: float = 60.0                # while driving: below ice_c this long
    defog_hint: bool = True
    defog_hint_window_s: float = 300.0  # rear-defogger hint only in the first minutes of a drive


@dataclass
class ServiceItem:
    id: str = ""
    name: str = ""
    every_km: Optional[float] = None
    every_days: Optional[float] = None


def _default_services() -> List[Dict[str, Any]]:
    return [
        {"id": "oil", "name": "OIL + FILTER", "every_km": 10000, "every_days": 365},
        {"id": "air_filter", "name": "AIR FILTER", "every_km": 30000, "every_days": None},
        {"id": "cabin_filter", "name": "CABIN FILTER", "every_km": 15000, "every_days": 365},
        {"id": "hv_intake_filter", "name": "HV BATT FILTER", "every_km": 20000, "every_days": None},
        {"id": "inverter_coolant", "name": "INVERTER COOLANT", "every_km": 160000, "every_days": None},
        {"id": "lpg_service", "name": "LPG SERVICE", "every_km": 15000, "every_days": 365},
    ]


@dataclass
class MaintenanceTuning:
    services: List[Dict[str, Any]] = field(default_factory=_default_services)
    warn_km: float = 500.0
    warn_days: float = 14.0
    range_warn_km: float = 60.0
    range_window_km: float = 300.0      # consumption over the last this many km (all drives)
    range_min_km: float = 20.0          # need this much history before an estimate
    dtc_notice: bool = True


@dataclass
class GuardianTuning:
    unlocked_after_s: float = 300.0
    door_open_after_s: float = 120.0
    lamps_after_s: float = 60.0
    low_12v_v: float = 12.2
    low_12v_rest_s: float = 1800.0      # resting voltage: this long after power-off ...
    low_12v_hold_s: float = 600.0       # ... and below the threshold this long
    intrusion_unlock_window_s: float = 60.0   # a door opening this long after an unlock is the owner


@dataclass
class LaneTuning:
    min_speed_kmh: float = 60.0
    min_prob: float = 0.6
    half_width_m: float = 0.86          # NHW20 1725 mm wide; camera on the centre line
    margin_m: float = 0.15              # wheel within this of the line ...
    min_rate_mps: float = 0.25          # ... while moving towards it at least this fast
    debounce_s: float = 0.3
    max_steer_deg: float = 30.0


@dataclass
class AlertTuning:
    speed_limit: SpeedLimitTuning = field(default_factory=SpeedLimitTuning)
    stop_go: StopGoTuning = field(default_factory=StopGoTuning)
    headway: HeadwayTuning = field(default_factory=HeadwayTuning)
    curve: CurveTuning = field(default_factory=CurveTuning)
    tunnel: TunnelTuning = field(default_factory=TunnelTuning)
    eco: EcoTuning = field(default_factory=EcoTuning)
    winter: WinterTuning = field(default_factory=WinterTuning)
    maintenance: MaintenanceTuning = field(default_factory=MaintenanceTuning)
    guardian: GuardianTuning = field(default_factory=GuardianTuning)
    lane: LaneTuning = field(default_factory=LaneTuning)

    def as_dict(self) -> Dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


def apply_overrides(t: AlertTuning, raw: Dict[str, Any]) -> List[str]:
    """Apply a ``{"section": {"name": value}}`` mapping; returns the problems."""
    problems: List[str] = []
    for sec_name, values in (raw or {}).items():
        sec = getattr(t, sec_name, None)
        if sec is None or not is_dataclass(sec) or not isinstance(values, dict):
            problems.append("unknown section %r" % sec_name)
            continue
        names = {f.name: f for f in fields(sec)}
        for k, v in values.items():
            if k not in names:
                problems.append("%s.%s unknown" % (sec_name, k))
                continue
            cur = getattr(sec, k)
            ok = (cur is None or isinstance(v, type(cur))
                  or (isinstance(cur, float) and isinstance(v, int) and not isinstance(v, bool))
                  or (cur is None and v is not None))
            if isinstance(cur, bool) != isinstance(v, bool):
                ok = False
            if not ok:
                problems.append("%s.%s: wrong type %s" % (sec_name, k, type(v).__name__))
                continue
            setattr(sec, k, float(v) if isinstance(cur, float) and not isinstance(v, bool) else v)
    return problems


def load(directory: Optional[str] = None) -> AlertTuning:
    t = AlertTuning()
    path = os.environ.get("BACKEND_ALERTS_TUNING") or (
        os.path.join(directory, "alerts_tuning.json") if directory else None)
    if not path or not os.path.exists(path):
        return t
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        logger.exception("alert tuning %s unreadable; defaults used", path)
        return t
    for p in apply_overrides(t, raw if isinstance(raw, dict) else {}):
        logger.warning("alert tuning %s: %s", path, p)
    return t
