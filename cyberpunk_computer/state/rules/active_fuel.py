"""
Rule: Active Fuel Type determination.

Logic:
- Engine OFF: active_fuel = OFF
- Engine ON (ice_running): active_fuel = PETROL (default when engine starts)
- LPG system ON message received: active_fuel = LPG

When engine starts, we always assume PETROL until we get an LPG message.
"""

from typing import Set, Optional
from .engine import StateRule, RulePriority
from ..store import Store, StateSlice
from ..app_state import AppState, FuelType
from ..actions import SetActiveFuelAction, ActionSource


class ActiveFuelRule(StateRule):
    """
    Determines active fuel type based on engine state and LPG messages.
    
    When engine starts (ice_running transitions from False to True),
    we set fuel type to PETROL. If LPG system sends ON message,
    we switch to LPG. When engine stops, we set to OFF.
    """
    
    def __init__(self):
        self._lpg_active = False  # Track if LPG system has signaled ON
    
    @property
    def name(self) -> str:
        return "ActiveFuelRule"
    
    @property
    def watches(self) -> Set[StateSlice]:
        return {StateSlice.VEHICLE}
        
    @property
    def priority(self) -> RulePriority:
        return RulePriority.NORMAL
    
    def set_lpg_active(self, active: bool) -> None:
        """Called when LPG system signals ON/OFF."""
        self._lpg_active = active
    
    def evaluate(self, old_state: Optional[AppState], new_state: AppState, store: Store) -> None:
        vehicle = new_state.vehicle
        ice_running = vehicle.ice_running
        current_fuel = vehicle.active_fuel
        
        # Determine what the fuel type should be
        if not ice_running:
            # Engine is off
            target_fuel = FuelType.OFF
        elif self._lpg_active:
            # Engine on and LPG system is active
            target_fuel = FuelType.LPG
        else:
            # Engine on, no LPG signal - use petrol
            target_fuel = FuelType.PETROL
        
        # Check if engine just started (transition from not running to running)
        if old_state is not None:
            was_running = old_state.vehicle.ice_running
            if not was_running and ice_running:
                # Engine just started - reset LPG flag and assume petrol
                self._lpg_active = False
                target_fuel = FuelType.PETROL
        
        # Dispatch if changed
        if current_fuel != target_fuel:
            store.dispatch(SetActiveFuelAction(
                fuel_type=target_fuel.name,
                source=ActionSource.INTERNAL
            ))
