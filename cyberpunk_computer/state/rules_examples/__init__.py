"""
Example Rules - Demonstrating the State Rules Engine.

This module contains example rules that demonstrate how to implement
reactive business logic using the Rules Engine.

NOTE: These rules require state extensions (LightsState, SensorsState)
that are not yet implemented in the core. They serve as documentation
and templates for future implementation.
"""

from typing import Optional, Set
from enum import Enum, auto

from ..rules import StateRule, RulePriority
from ..store import Store, StateSlice
from ..app_state import AppState, GearPosition
from ..actions import Action, ActionSource


# ─────────────────────────────────────────────────────────────────────────────
# Example: DRL Control Rule
# ─────────────────────────────────────────────────────────────────────────────

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
    
    Example flow:
    1. Light sensor satellite sends brightness data → ingress updates is_daytime
    2. This rule evaluates and computes new drl_output_active
    3. Rule dispatches SetDRLOutputAction(active=True/False)
    4. Store updates lights.drl_output_active
    5. Egress controller sees change, sends command to DRL satellite
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
        # # Compute what DRL output should be
        # should_be_active = self._compute_drl_output(
        #     user_mode=lights.drl_user_mode,
        #     gear=vehicle.gear,
        #     is_daytime=lights.is_daytime,
        #     is_raining=lights.is_raining
        # )
        #
        # # Only dispatch if changed
        # if should_be_active != lights.drl_output_active:
        #     store.dispatch(SetDRLOutputAction(
        #         active=should_be_active,
        #         source=ActionSource.RULE
        #     ))
        pass
    
    def _compute_drl_output(
        self,
        user_mode: DRLUserMode,
        gear: GearPosition,
        is_daytime: bool,
        is_raining: bool
    ) -> bool:
        """
        Pure function: compute DRL output state.
        
        This is a pure function with no side effects, making it
        easy to test independently.
        """
        # Never on in PARK
        if gear == GearPosition.PARK:
            return False
        
        if user_mode == DRLUserMode.OFF:
            return False
        elif user_mode == DRLUserMode.ON:
            return True
        else:  # AUTO
            return is_daytime and not is_raining


# ─────────────────────────────────────────────────────────────────────────────
# Example: Auto AC Rule
# ─────────────────────────────────────────────────────────────────────────────

class AutoACRule(StateRule):
    """
    Example rule: Automatically manage AC based on temperatures.
    
    Logic:
    - If inside temp > target temp + 2°C: AC should be on
    - If inside temp < target temp - 1°C: AC should be off
    - Hysteresis prevents rapid cycling
    
    This demonstrates a rule with hysteresis logic.
    """
    
    HYSTERESIS_HIGH = 2.0  # °C above target to turn on
    HYSTERESIS_LOW = 1.0   # °C below target to turn off
    
    @property
    def name(self) -> str:
        return "AutoACRule"
    
    @property
    def watches(self) -> Set[StateSlice]:
        return {StateSlice.CLIMATE}
    
    def evaluate(
        self, 
        old_state: Optional[AppState], 
        new_state: AppState, 
        store: Store
    ) -> None:
        """Evaluate AC state based on temperature difference."""
        climate = new_state.climate
        
        # Skip if not in auto mode
        if not climate.auto_mode:
            return
        
        # Skip if we don't have inside temperature
        if climate.inside_temp is None:
            return
        
        target = climate.target_temp
        inside = climate.inside_temp
        current_ac = climate.ac_on
        
        # Compute desired state with hysteresis
        if inside > target + self.HYSTERESIS_HIGH:
            should_be_on = True
        elif inside < target - self.HYSTERESIS_LOW:
            should_be_on = False
        else:
            should_be_on = current_ac  # Keep current state in hysteresis band
        
        # Only dispatch if changed
        # NOTE: Would need SetACAction with RULE source
        # if should_be_on != current_ac:
        #     from ..actions import SetACAction
        #     store.dispatch(SetACAction(should_be_on, ActionSource.RULE))
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Example: Screen Brightness Rule
# ─────────────────────────────────────────────────────────────────────────────

class AutoBrightnessRule(StateRule):
    """
    Example rule: Adjust screen brightness based on ambient light.
    
    This demonstrates a rule that uses sensor input to adjust a UI setting.
    """
    
    @property
    def name(self) -> str:
        return "AutoBrightnessRule"
    
    @property
    def watches(self) -> Set[StateSlice]:
        # Would watch SENSORS slice
        return set()
    
    @property
    def priority(self) -> RulePriority:
        # Low priority - runs after sensor processing
        return RulePriority.LOW
    
    def evaluate(
        self, 
        old_state: Optional[AppState], 
        new_state: AppState, 
        store: Store
    ) -> None:
        """Adjust brightness based on ambient light."""
        # Example implementation:
        #
        # sensors = new_state.sensors
        # light_level = sensors.ambient_light_level  # 0-100
        #
        # # Map light level to brightness (inverse relationship)
        # # Bright outside → dim screen, dark outside → bright screen
        # if light_level > 80:
        #     target_brightness = 30  # Dim screen in bright conditions
        # elif light_level < 20:
        #     target_brightness = 100  # Bright screen in dark conditions
        # else:
        #     # Linear interpolation
        #     target_brightness = 100 - int((light_level - 20) * (70 / 60))
        #
        # if target_brightness != new_state.screen_brightness:
        #     store.dispatch(SetScreenBrightnessAction(target_brightness))
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Registration helper
# ─────────────────────────────────────────────────────────────────────────────

def register_example_rules(engine) -> None:
    """
    Register all example rules with the rules engine.
    
    Call this function to enable the example rules:
    
        from cyberpunk_computer.state.rules_examples import register_example_rules
        register_example_rules(rules_engine)
    """
    # These are commented out because they require state extensions
    # engine.register(DRLControlRule())
    # engine.register(AutoACRule())
    # engine.register(AutoBrightnessRule())
    pass
