"""
Rule: our per-drive fuel, distance and average; the meter cross-check.

* ``trip_fuel_consumed`` (L): our corrected flow (state.fuel_model) integrated
  over time, standstill idling included. The flow already is 0 while the
  engine is stopped (engine gate, 0x520 silence).
* ``drive_distance_km``: ``real_speed_kmh`` integrated (true distance).
* ``drive_avg_consumption_l100km``: litres / true distance x 100, None below
  :data:`DRIVE_AVG_MIN_KM`.
* A drive starts at IG-ON (BEAN FE:83 ig_on False -> True); without BEAN the
  totals run since application start.
* ``meter_trip_fuel_l``: the meter's own litres since its trip reset,
  52:A2 average x 52:76 trip distance / 100 (a cross-check; K is not
  calibrated from it automatically).

Integration is left-rectangle: the flow and speed held since the previous
evaluation times the time since then (the rule runs on every vehicle change).
A held flow cannot run on: the ingress drops it to 0 when 0x520 / 62:20 go
silent for 2.5 s (state.fuel_model.FLOW_STALE_S), which is itself a change.
"""

import time
from typing import Callable, Optional, Set

from .engine import RulePriority, StateRule
from ..actions import ActionSource, SetTripFuelConsumedAction
from ..app_state import AppState
from ..store import StateSlice, Store

#: Below this true distance the drive average is not meaningful (None).
DRIVE_AVG_MIN_KM = 0.2

def meter_trip_fuel_l(avg_l100km: Optional[float], trip_km: Optional[float]) -> Optional[float]:
    if avg_l100km is None or trip_km is None:
        return None
    return round(avg_l100km * trip_km / 100.0, 3)


class TripFuelConsumptionRule(StateRule):
    def __init__(self, clock: Optional[Callable[[], float]] = None):
        # looked up at call time so a replay may patch the module's ``time``
        self._clock = clock or (lambda: time.monotonic())
        self._last_time: Optional[float] = None
        self._flow: float = 0.0           # L/h held since _last_time
        self._speed: float = 0.0          # km/h held since _last_time
        self._accumulated: float = 0.0    # liters this drive
        self._distance: float = 0.0       # km this drive
        self._last_dispatched: Optional[tuple] = None

    @property
    def name(self) -> str:
        return "TripFuelConsumptionRule"

    @property
    def watches(self) -> Set[StateSlice]:
        return {StateSlice.VEHICLE}

    @property
    def priority(self) -> RulePriority:
        return RulePriority.LOW  # Run after FuelConsumptionRule

    def reset(self) -> None:
        self._accumulated = 0.0
        self._distance = 0.0

    def evaluate(self, old_state: Optional[AppState], new_state: AppState, store: Store) -> None:
        now = self._clock()   # monotonic: a wall-clock step must not integrate hours of fuel
        vehicle = new_state.vehicle

        if self._last_time is not None:
            dt = now - self._last_time
            if dt > 0.0:
                self._accumulated += self._flow * dt / 3600.0
                self._distance += self._speed * dt / 3600.0
        self._last_time = now

        if old_state is not None and vehicle.ig_on and not old_state.vehicle.ig_on:
            self.reset()                  # IG-ON: a new drive

        flow = vehicle.fuel_flow_rate or 0.0
        self._flow = flow if vehicle.ice_running else 0.0
        self._speed = max(0.0, vehicle.real_speed_kmh or 0.0)

        liters = round(self._accumulated, 4)
        dist = round(self._distance, 3)
        avg = round(self._accumulated / self._distance * 100.0, 2) \
            if self._distance >= DRIVE_AVG_MIN_KM else None
        meter_l = meter_trip_fuel_l(vehicle.meter_avg_consumption_l100km, vehicle.meter_trip_km)

        last = self._last_dispatched
        if (last is None or abs(liters - last[0]) > 0.001 or abs(dist - last[1]) >= 0.01
                or (avg is None) != (last[2] is None)
                or (avg is not None and abs(avg - last[2]) >= 0.05)
                or meter_l != last[3] or liters < last[0]):
            if last is None and liters == 0.0 and dist == 0.0 and meter_l is None \
                    and vehicle.trip_fuel_consumed == 0.0:
                return
            self._last_dispatched = (liters, dist, avg, meter_l)
            store.dispatch(SetTripFuelConsumedAction(
                liters=liters, source=ActionSource.INTERNAL,
                drive_distance_km=dist, drive_avg_l100km=avg, meter_trip_fuel_l=meter_l,
            ))
