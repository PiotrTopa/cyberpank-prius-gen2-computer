"""
Example Rule: DRL Control
"""

from typing import Optional, Set
from enum import Enum, auto

from .engine import StateRule, RulePriority
from ..store import Store, StateSlice
from ..app_state import AppState, GearPosition
from ..actions import Action, ActionSource

# These types would be added to app_state.py when implementing DRL feature

class DRLUserMode(Enum):
    """User-selected DRL mode."""
    OFF = auto()
    ON = auto()
    AUTO = auto()


# This action would be added to actions.py
class SetDRLOutputAction(Action):
    """Set the computed DRL output state."""
    def __init__(self, active: bool, source: ActionSource = ActionSource.RULE):
        from ..actions import ActionType
        # Would need to add ActionType.SET_DRL_OUTPUT to ActionType enum
        super().__init__(type=None, source=source)  # placeholder
        self.active = active


class DRLControlRule(StateRule):
    """
    Rule: Compute DRL output based on user mode, gear, and sensor data.
    
    Logic:
    - OFF mode: Always off
    - ON mode: On if not in PARK
    - AUTO mode: On if daytime AND not raining AND not in PARK
    
    State dependencies:
    - lights.drl_user_mode: User's selected mode (OFF/ON/AUTO)
    - lights.is_daytime: From light sensor satellite
    - lights.is_raining: From rain sensor satellite
    - vehicle.gear: Current gear position
    
    Output:
    - lights.drl_output_active: Computed status to send to DRL satellite
    """
    
    @property
    def name(self) -> str:
        return "DRLControlRule"
    
    @property
    def watches(self) -> Set[StateSlice]:
        # Would need to add StateSlice.LIGHTS and StateSlice.SENSORS
        return {StateSlice.VEHICLE}  # Plus LIGHTS, SENSORS when implemented
    
    @property
    def priority(self) -> RulePriority:
        return RulePriority.NORMAL
    
    def evaluate(
        self, 
        old_state: Optional[AppState], 
        new_state: AppState, 
        store: Store
    ) -> None:
        """
        Evaluate DRL output state.
        
        NOTE: This is example code showing the pattern. In actual
        implementation, AppState would need LightsState with the
        required fields.
        """
        # Example implementation (would need actual state fields):
        #
        # lights = new_state.lights
        # vehicle = new_state.vehicle
        #
        pass
