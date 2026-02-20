"""
Rule: Continuous chart data collection.

Samples time-series data into ChartDataStore on every state change,
regardless of which UI screen is active. This ensures charts always
have history data even when navigating between screens.
"""

from typing import Set, Optional

from .engine import StateRule, RulePriority
from ..store import Store, StateSlice
from ..app_state import AppState
from ..chart_data import ChartDataStore


class ChartDataRule(StateRule):
    """
    Collects time-series samples for chart visualizations.

    Watches VEHICLE and ENERGY slices and feeds samples into
    the Store's ChartDataStore at 1-second intervals.

    Collected series:
      - Battery delta-V        (from block_voltages)
      - Fuel flow rate          (from fuel_flow_rate + ice_running)
      - ICE coolant temperature (from ice_coolant_temp)
      - Inverter temperature    (from inverter_temp)
    """

    @property
    def name(self) -> str:
        return "ChartDataRule"

    @property
    def watches(self) -> Set[StateSlice]:
        return {StateSlice.VEHICLE, StateSlice.ENERGY}

    @property
    def priority(self) -> RulePriority:
        return RulePriority.LOW  # Run after sensor processing rules

    def evaluate(
        self,
        old_state: Optional[AppState],
        new_state: AppState,
        store: Store,
    ) -> None:
        chart: ChartDataStore = store.chart_data

        # ── Battery delta-V ──
        chart.record_delta_v(new_state.energy.block_voltages)

        # ── Fuel flow (accumulate; flush happens on 1-s boundary) ──
        flow = new_state.vehicle.fuel_flow_rate or 0.0
        chart.accumulate_fuel_flow(flow, new_state.vehicle.ice_running)
        chart.flush_fuel_flow()

        # ── ICE coolant temperature ──
        chart.record_ice_temp(new_state.vehicle.ice_coolant_temp)

        # ── Inverter temperature ──
        chart.record_inverter_temp(new_state.vehicle.inverter_temp)
