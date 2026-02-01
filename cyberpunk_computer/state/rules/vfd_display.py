"""
Rule: VFD Satellite Display Communication.

This rule computes the VFD satellite display data from vehicle state
and prepares it for transmission via egress controller.

Device ID: 110

The rule:
1. Watches VEHICLE, ENERGY, and DISPLAY state slices
2. Normalizes raw values to protocol-specified ranges
3. Updates VFD_SATELLITE state with computed values
4. The egress controller then sends this data to the satellite
"""

import time
import logging
from typing import Set, Optional
from dataclasses import replace

from .engine import StateRule, RulePriority
from ..store import Store, StateSlice
from ..app_state import AppState, GearPosition, FuelType
from ..actions import UpdateVFDSatelliteAction, ActionSource

logger = logging.getLogger(__name__)


# Device ID for VFD satellite
VFD_DEVICE_ID = 110

# Update rates (seconds)
ENERGY_UPDATE_INTERVAL = 0.05   # 20 Hz for energy data
STATE_UPDATE_INTERVAL = 1.0     # 1 Hz for state flags (or on change)

# Normalization constants
MAX_MG_POWER_KW = 30.0      # Max MG power for normalization
MAX_SPEED_KMH = 120.0       # Max speed for normalization
MAX_FUEL_FLOW_LH = 8.0      # Max fuel flow for normalization
MAX_BRAKE_PRESSURE = 127.0  # Max brake pressure


class VFDDisplayRule(StateRule):
    """
    Rule: Compute VFD satellite display data from vehicle state.
    
    This rule transforms raw vehicle state into normalized values
    suitable for transmission to the VFD satellite display.
    
    State dependencies:
    - vehicle.speed_kmh, ice_running, brake_pressed, fuel_flow_rate
    - vehicle.fuel_level, lpg_level, active_fuel, gear, ready_mode
    - energy.battery_power_kw, battery_soc
    - display.power_chart_time_base
    
    Output:
    - vfd_satellite.* (all normalized values)
    """
    
    def __init__(self):
        self._last_energy_time = 0.0
        self._last_state_time = 0.0
        self._last_state_hash = None  # For change detection
    
    @property
    def name(self) -> str:
        return "VFDDisplayRule"
    
    @property
    def watches(self) -> Set[StateSlice]:
        return {StateSlice.VEHICLE, StateSlice.ENERGY, StateSlice.DISPLAY}
    
    @property
    def priority(self) -> RulePriority:
        return RulePriority.LOW  # Run after other rules compute derived state
    
    def evaluate(
        self, 
        old_state: Optional[AppState], 
        new_state: AppState, 
        store: Store
    ) -> None:
        """
        Evaluate VFD display state.
        
        Computes normalized values and updates VFD satellite state.
        """
        current_time = time.time()
        vehicle = new_state.vehicle
        energy = new_state.energy
        display = new_state.display
        
        # Compute normalized values
        # Use battery_power_kw (calculated from V*I) instead of motor_power_kw
        battery_power = energy.battery_power_kw or 0.0
        mg_power = self._normalize_mg_power(battery_power)
        fuel_flow = self._normalize_fuel_flow(vehicle.fuel_flow_rate)
        brake = self._normalize_brake(vehicle.brake_pressed)
        speed = self._normalize_speed(vehicle.speed_kmh)
        
        # State flags
        active_fuel = self._map_fuel_type(vehicle.active_fuel)
        gear = self._map_gear(vehicle.gear)
        
        # Check if state flags changed
        state_hash = (active_fuel, gear, vehicle.ready_mode)
        state_changed = state_hash != self._last_state_hash
        
        # Check if enough time has passed for energy update
        energy_due = (current_time - self._last_energy_time) >= ENERGY_UPDATE_INTERVAL
        state_due = (current_time - self._last_state_time) >= STATE_UPDATE_INTERVAL
        
        # Check if config changed
        config_changed = (
            old_state is not None and 
            old_state.display.power_chart_time_base != display.power_chart_time_base
        )
        
        # Build update action
        kwargs = {}
        
        # Always update energy values (they'll be batched by egress)
        if energy_due:
            kwargs.update({
                'mg_power': mg_power,
                'fuel_flow': fuel_flow,
                'brake': brake,
                'speed': speed,
                'battery_soc': energy.battery_soc,
                'petrol_level': vehicle.fuel_level,
                'lpg_level': vehicle.lpg_level,
                'ice_running': vehicle.ice_running,
                'last_energy_send_time': current_time,
            })
            self._last_energy_time = current_time
        
        # Update state flags if changed or periodic
        if state_changed or state_due:
            kwargs.update({
                'active_fuel': active_fuel,
                'gear': gear,
                'ready_mode': vehicle.ready_mode,
                'last_state_send_time': current_time,
            })
            self._last_state_time = current_time
            self._last_state_hash = state_hash
        
        # Update config if changed
        if config_changed:
            kwargs.update({
                'time_base': display.power_chart_time_base,
                'needs_config_send': True,
            })
        
        # Dispatch update if we have changes
        if kwargs:
            logger.debug(f"VFDDisplayRule dispatching update: {list(kwargs.keys())}")
            store.dispatch(UpdateVFDSatelliteAction(
                source=ActionSource.INTERNAL,
                **kwargs
            ))
    
    def _normalize_mg_power(self, power_kw: float) -> float:
        """Normalize MG power to -1.0 to +1.0 range."""
        return max(-1.0, min(1.0, power_kw / MAX_MG_POWER_KW))
    
    def _normalize_fuel_flow(self, flow_lh: float) -> float:
        """Normalize fuel flow to 0.0 to 1.0 range."""
        return max(0.0, min(1.0, flow_lh / MAX_FUEL_FLOW_LH))
    
    def _normalize_brake(self, pressure: int) -> float:
        """Normalize brake pressure to 0.0 to 1.0 range."""
        return max(0.0, min(1.0, pressure / MAX_BRAKE_PRESSURE))
    
    def _normalize_speed(self, speed_kmh: float) -> float:
        """Normalize speed to 0.0 to 1.0 range."""
        return max(0.0, min(1.0, speed_kmh / MAX_SPEED_KMH))
    
    def _map_fuel_type(self, fuel: FuelType) -> str:
        """Map FuelType enum to protocol string."""
        mapping = {
            FuelType.OFF: "OFF",
            FuelType.PETROL: "PTR",
            FuelType.LPG: "LPG",
        }
        return mapping.get(fuel, "OFF")
    
    def _map_gear(self, gear: GearPosition) -> str:
        """Map GearPosition enum to protocol string."""
        mapping = {
            GearPosition.PARK: "P",
            GearPosition.REVERSE: "R",
            GearPosition.NEUTRAL: "N",
            GearPosition.DRIVE: "D",
            GearPosition.B: "B",
        }
        return mapping.get(gear, "P")
