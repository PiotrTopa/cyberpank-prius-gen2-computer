"""
Rule: Calculate instant fuel consumption.
"""

from typing import Set, Optional
from .engine import StateRule, RulePriority
from ..store import Store, StateSlice
from ..app_state import AppState
from ..actions import SetInstantConsumptionAction, ActionSource

class FuelConsumptionRule(StateRule):
    """
    Calculates instant fuel consumption from flow rate and speed.
    
    If speed > 5 km/h: calculates L/100km.
    Else: returns flow rate as L/h.
    """
    
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
        flow_rate = vehicle.fuel_flow_rate
        speed = vehicle.speed_kmh
        ice_running = vehicle.ice_running
        
        # Default values
        consumption = 0.0
        unit = "L/h"
        
        # Logic from MainScreen:
        # Use flow_rate > 0.05 as threshold for "consuming"
        is_consuming = flow_rate is not None and flow_rate > 0.05
        
        # Only calculate if consuming and engine is running
        if is_consuming and ice_running:
            if speed > 5.0:
                 # L/100km = (L/h / km/h) * 100
                 l_100 = (flow_rate / speed) * 100
                 consumption = min(99.9, l_100)
                 unit = "L/100km"
            else:
                 # L/h
                 consumption = flow_rate
                 unit = "L/h"
        else:
            # Not consuming
            consumption = 0.0
            if speed > 5.0:
                unit = "L/100km"
            else:
                unit = "L/h"
        
        # Check against old state to avoid infinite loops and unnecessary dispatches
        # (Although ActionSource.INTERNAL should prevent loops in the engine if handled correctly, 
        # explicit check is safer).
        current_consumption = getattr(vehicle, 'instant_consumption', 0.0)
        current_unit = getattr(vehicle, 'consumption_unit', "L/h")
        
        if abs(current_consumption - consumption) > 0.01 or current_unit != unit:
            store.dispatch(SetInstantConsumptionAction(
                value=consumption,
                unit=unit,
                source=ActionSource.INTERNAL
            ))
