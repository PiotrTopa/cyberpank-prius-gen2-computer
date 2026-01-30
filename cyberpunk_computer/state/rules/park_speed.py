"""
Safety rule: Speed must be 0 when in PARK.
"""

from typing import Set, Optional
from .engine import StateRule
from ..store import Store, StateSlice
from ..app_state import AppState, GearPosition
from ..actions import SetSpeedAction, ActionSource

class ParkSpeedRule(StateRule):
    """
    Safety rule: Speed must be 0 when in PARK.
    """
    
    @property
    def name(self) -> str:
        return "ParkSpeedRule"
    
    @property
    def watches(self) -> Set[StateSlice]:
        return {StateSlice.VEHICLE}
    
    def evaluate(self, old_state: Optional[AppState], new_state: AppState, store: Store) -> None:
        # If in PARK and speed is not 0, force it to 0
        if new_state.vehicle.gear == GearPosition.PARK and new_state.vehicle.speed_kmh != 0:
            # Important: Use ActionSource.INTERNAL to avoid sending this back to the vehicle/gateway
            # and to ensure the reducer processes it correctly locally.
            store.dispatch(SetSpeedAction(
                speed_kmh=0.0,
                source=ActionSource.INTERNAL
            ))
