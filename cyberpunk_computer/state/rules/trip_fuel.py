"""
Rule: Cumulative trip fuel consumption.

Integrates fuel_flow_rate over time to compute total fuel consumed
since application start. Runs as part of the VirtualTwin rules engine.
"""

import time
from typing import Set, Optional

from .engine import StateRule, RulePriority
from ..store import Store, StateSlice
from ..app_state import AppState
from ..actions import SetTripFuelConsumedAction, ActionSource


class TripFuelConsumptionRule(StateRule):
    """
    Integrates fuel flow rate over time to track trip fuel consumed.

    Updates at most once per second to avoid excessive dispatches.
    Only counts fuel when ICE is actually running.
    """

    def __init__(self):
        self._last_time: float = 0.0
        self._accumulated: float = 0.0  # liters
        self._last_dispatched: float = 0.0  # last value dispatched

    @property
    def name(self) -> str:
        return "TripFuelConsumptionRule"

    @property
    def watches(self) -> Set[StateSlice]:
        return {StateSlice.VEHICLE}

    @property
    def priority(self) -> RulePriority:
        return RulePriority.LOW  # Run after FuelConsumptionRule

    def evaluate(
        self,
        old_state: Optional[AppState],
        new_state: AppState,
        store: Store,
    ) -> None:
        now = time.time()
        vehicle = new_state.vehicle

        flow_rate = vehicle.fuel_flow_rate if vehicle.fuel_flow_rate is not None else 0.0
        # Only count when ICE is running
        if not vehicle.ice_running:
            flow_rate = 0.0

        # Integrate
        if self._last_time > 0.0:
            dt_hours = (now - self._last_time) / 3600.0
            self._accumulated += flow_rate * dt_hours
        self._last_time = now

        # Dispatch at most once per second and only when changed meaningfully
        if abs(self._accumulated - self._last_dispatched) > 0.001:
            self._last_dispatched = self._accumulated
            store.dispatch(
                SetTripFuelConsumedAction(
                    liters=round(self._accumulated, 4),
                    source=ActionSource.INTERNAL,
                )
            )
