"""
Rule: instantaneous fuel consumption.

The combination meter's own figure (BEAN 52:90, over TRUE speed, lags our
flow by 0-1.5 s) is primary while it is fresh; our computed value is the
fallback (docs/BEAN_SIGNALS.md, docs/prius_can.md):

  52:90 numeric   -> meter value, L/100 km (capped at 99.9 for display; the
                     raw value stays in vehicle.meter_consumption_l100km)
  52:90 0000      -> 0 (no fuel: ICE off / EV, or fuel cut)
  52:90 FFFE      -> stopped with the ICE running: the meter has no figure,
                     our corrected flow is shown in L/h
  52:90 stale     -> ours: flow / real speed x 100 above 5 km/h, else L/h
                     (BeanIngest.expire sets the meter fields to None)

Our own L/100 km (``computed_consumption_l100km``) is always computed from
the corrected flow (state.fuel_model) and ``real_speed_kmh``.
"""

from typing import Optional, Set

from .engine import RulePriority, StateRule
from ..actions import ActionSource, SetInstantConsumptionAction
from ..app_state import AppState
from ..fuel_model import DISPLAY_CAP_L100KM, MIN_SPEED_L100KM
from ..store import StateSlice, Store

#: Flow below this (L/h) counts as "not consuming".
MIN_FLOW_LH = 0.05

#: Clamp of our own L/100 km (the meter's 52:90 reached 196 at walking pace).
COMPUTED_CAP_L100KM = 300.0


def computed_l100km(flow_lh: float, speed_kmh: float, ice_running: bool) -> Optional[float]:
    """Our L/100 km over true speed; None at or below MIN_SPEED_L100KM."""
    if speed_kmh is None or speed_kmh <= MIN_SPEED_L100KM:
        return None
    if not ice_running or not flow_lh or flow_lh <= MIN_FLOW_LH:
        return 0.0
    return round(min(COMPUTED_CAP_L100KM, flow_lh / speed_kmh * 100.0), 2)


def instant_display(vehicle) -> tuple:
    """(value, unit, origin) for the displayed instantaneous consumption."""
    flow = vehicle.fuel_flow_rate or 0.0
    speed = vehicle.real_speed_kmh or 0.0
    running = vehicle.ice_running
    stopped = vehicle.meter_consumption_stopped
    meter = vehicle.meter_consumption_l100km
    unit_idle = "L/100km" if speed > MIN_SPEED_L100KM else "L/h"

    if stopped is True:                                   # FFFE
        value = flow if running and flow > MIN_FLOW_LH else 0.0
        return round(value, 2), "L/h", "computed"
    if stopped is False and meter is not None:
        if meter <= 0.0:                                  # 0000
            return 0.0, unit_idle, "meter"
        return min(DISPLAY_CAP_L100KM, meter), "L/100km", "meter"

    # meter stale / never heard: ours
    if running and flow > MIN_FLOW_LH:
        if speed > MIN_SPEED_L100KM:
            return min(DISPLAY_CAP_L100KM, computed_l100km(flow, speed, running)), "L/100km", "computed"
        return round(flow, 2), "L/h", "computed"
    return 0.0, unit_idle, "computed"


class FuelConsumptionRule(StateRule):
    """Displayed instantaneous consumption: meter (52:90) first, ours as
    fallback; also publishes our own L/100 km."""

    @property
    def name(self) -> str:
        return "FuelConsumptionRule"

    @property
    def watches(self) -> Set[StateSlice]:
        return {StateSlice.VEHICLE}

    @property
    def priority(self) -> RulePriority:
        return RulePriority.NORMAL

    def evaluate(self, old_state: Optional[AppState], new_state: AppState, store: Store) -> None:
        vehicle = new_state.vehicle
        value, unit, origin = instant_display(vehicle)
        computed = computed_l100km(vehicle.fuel_flow_rate or 0.0, vehicle.real_speed_kmh or 0.0,
                                   vehicle.ice_running)

        cur_value = getattr(vehicle, "instant_consumption", 0.0)
        cur_computed = getattr(vehicle, "computed_consumption_l100km", None)
        computed_changed = (computed is None) != (cur_computed is None) or (
            computed is not None and abs(computed - cur_computed) > 0.01)
        if (abs(cur_value - value) > 0.01 or vehicle.consumption_unit != unit
                or getattr(vehicle, "instant_consumption_source", None) != origin
                or computed_changed):
            store.dispatch(SetInstantConsumptionAction(
                value=value, unit=unit, source=ActionSource.INTERNAL,
                origin=origin, computed_l100km=computed,
            ))
