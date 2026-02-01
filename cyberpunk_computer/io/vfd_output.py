"""
VFD Satellite Output Handler.

Handles sending state updates to the VFD satellite display (device 110)
using the VFD Satellite Protocol (see docs/VFD_SATELLITE_PROTOCOL.md).

The handler sends three message types:
- "E": Energy data at 20Hz (mg_power, fuel_flow, brake, speed, battery_soc, petrol_level, lpg_level)
- "S": State flags on-change (ice, gear, fuel, ready)
- "C": Configuration on-change (brightness)
"""

import time
import logging
from typing import Optional, Set, List
from dataclasses import dataclass

from .ports import OutgoingCommand, DEVICE_VFD
from .egress import OutputHandler
from ..state.store import StateSlice
from ..state.app_state import AppState, VFDSatelliteState

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# VFD Message Builders
# ─────────────────────────────────────────────────────────────────────────────

def build_energy_message(state: VFDSatelliteState) -> dict:
    """
    Build energy message payload (type "E").
    
    All values are normalized as per VFD_SATELLITE_PROTOCOL.md:
    - mg: MG power, -1.0 (regen) to +1.0 (motor), normalized to ±30kW
    - fl: Fuel flow, 0.0 to 1.0, normalized to 8L/h
    - br: Brake position, 0.0 to 1.0
    - spd: Speed, 0.0 to 1.0, normalized to 120km/h
    - soc: Battery SOC, 0.0 to 1.0
    - ptr: Petrol level in liters (0-45)
    - lpg: LPG level in liters (0-60)
    - ice: ICE running flag
    """
    return {
        "t": "E",
        "mg": round(state.mg_power, 3),
        "fl": round(state.fuel_flow, 3),
        "br": round(state.brake, 3),
        "spd": round(state.speed, 3),
        "soc": round(state.battery_soc, 3),
        "ptr": state.petrol_level,
        "lpg": state.lpg_level,
        "ice": state.ice_running,
    }


def build_state_message(state: VFDSatelliteState) -> dict:
    """
    Build state message payload (type "S").
    
    - fuel: Active fuel type (PTR/LPG/OFF)
    - gear: Gear position (P/R/N/D/B)
    - rdy: Ready mode flag
    """
    return {
        "t": "S",
        "fuel": state.active_fuel,
        "gear": state.gear,
        "rdy": state.ready_mode,
    }


def build_config_message(state: VFDSatelliteState) -> dict:
    """
    Build config message payload (type "C").
    
    - tb: Power chart time base in seconds (15, 60, 300, 900, 3600)
    - bri: Display brightness, 0-100%
    """
    return {
        "t": "C",
        "tb": state.time_base,
        "bri": state.brightness,
    }


# ─────────────────────────────────────────────────────────────────────────────
# State Change Detection
# ─────────────────────────────────────────────────────────────────────────────

def energy_changed(
    old: Optional[VFDSatelliteState], 
    new: VFDSatelliteState,
    threshold: float = 0.001
) -> bool:
    """Check if energy values changed significantly."""
    if old is None:
        return True
    
    return (
        abs(old.mg_power - new.mg_power) > threshold or
        abs(old.fuel_flow - new.fuel_flow) > threshold or
        abs(old.brake - new.brake) > threshold or
        abs(old.speed - new.speed) > threshold or
        abs(old.battery_soc - new.battery_soc) > threshold or
        abs(old.petrol_level - new.petrol_level) > threshold or
        abs(old.lpg_level - new.lpg_level) > threshold
    )


def state_flags_changed(
    old: Optional[VFDSatelliteState], 
    new: VFDSatelliteState
) -> bool:
    """Check if state flags changed."""
    if old is None:
        return True
    
    return (
        old.ice_running != new.ice_running or
        old.gear != new.gear or
        old.active_fuel != new.active_fuel or
        old.ready_mode != new.ready_mode
    )


def config_changed(
    old: Optional[VFDSatelliteState], 
    new: VFDSatelliteState
) -> bool:
    """Check if config changed."""
    if old is None:
        return True
    
    return (
        abs(old.brightness - new.brightness) > 0.01 or
        old.time_base != new.time_base
    )


# ─────────────────────────────────────────────────────────────────────────────
# Output Handlers
# ─────────────────────────────────────────────────────────────────────────────

def create_vfd_energy_handler() -> OutputHandler:
    """
    Create output handler for VFD energy data.
    
    Sends energy message (type "E") when energy values change.
    Rate limited to ~20Hz by the VFDDisplayRule.
    """
    def should_send(old: Optional[AppState], new: AppState) -> bool:
        if old is None:
            result = new.vfd_satellite is not None
            logger.debug(f"VFD energy handler: old=None, result={result}")
            return result
        if new.vfd_satellite is None:
            logger.debug("VFD energy handler: new.vfd_satellite is None")
            return False
        result = energy_changed(old.vfd_satellite, new.vfd_satellite)
        if result:
            logger.debug(f"VFD energy handler: energy changed, sending")
        return result
    
    def build_command(state: AppState) -> OutgoingCommand:
        vfd = state.vfd_satellite
        return OutgoingCommand(
            device_id=DEVICE_VFD,
            command_type="E",
            payload=build_energy_message(vfd)
        )
    
    return OutputHandler(
        name="vfd_energy",
        watched_slices={StateSlice.VFD_SATELLITE},
        should_send=should_send,
        build_command=build_command
    )


def create_vfd_state_handler() -> OutputHandler:
    """
    Create output handler for VFD state flags.
    
    Sends state message (type "S") when state flags change.
    """
    def should_send(old: Optional[AppState], new: AppState) -> bool:
        if old is None:
            return new.vfd_satellite is not None
        if new.vfd_satellite is None:
            return False
        return state_flags_changed(old.vfd_satellite, new.vfd_satellite)
    
    def build_command(state: AppState) -> OutgoingCommand:
        vfd = state.vfd_satellite
        return OutgoingCommand(
            device_id=DEVICE_VFD,
            command_type="S",
            payload=build_state_message(vfd)
        )
    
    return OutputHandler(
        name="vfd_state",
        watched_slices={StateSlice.VFD_SATELLITE},
        should_send=should_send,
        build_command=build_command
    )


def create_vfd_config_handler() -> OutputHandler:
    """
    Create output handler for VFD configuration.
    
    Sends config message (type "C") when config changes.
    """
    def should_send(old: Optional[AppState], new: AppState) -> bool:
        if old is None:
            return new.vfd_satellite is not None
        if new.vfd_satellite is None:
            return False
        return config_changed(old.vfd_satellite, new.vfd_satellite)
    
    def build_command(state: AppState) -> OutgoingCommand:
        vfd = state.vfd_satellite
        return OutgoingCommand(
            device_id=DEVICE_VFD,
            command_type="C",
            payload=build_config_message(vfd)
        )
    
    return OutputHandler(
        name="vfd_config",
        watched_slices={StateSlice.VFD_SATELLITE},
        should_send=should_send,
        build_command=build_command
    )


def create_all_vfd_handlers() -> List[OutputHandler]:
    """
    Create all VFD output handlers.
    
    Returns:
        List of handlers for energy, state, and config messages.
    """
    return [
        create_vfd_energy_handler(),
        create_vfd_state_handler(),
        create_vfd_config_handler(),
    ]


def register_vfd_handlers(egress) -> None:
    """
    Register all VFD handlers with the egress controller.
    
    Args:
        egress: EgressController instance
    """
    handlers = create_all_vfd_handlers()
    logger.info(f"Registering {len(handlers)} VFD output handlers")
    for handler in handlers:
        egress.register_output_handler(
            name=handler.name,
            watched_slices=handler.watched_slices,
            should_send=handler.should_send,
            build_command=handler.build_command
        )
    logger.info("Registered VFD satellite handlers with egress controller")
