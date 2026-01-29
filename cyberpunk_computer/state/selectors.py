"""
Selectors - Functions to extract data from state.

Selectors provide a clean interface to access state slices.
They can also compute derived values.
"""

from typing import Optional
from .app_state import (
    AppState, AudioState, ClimateState, VehicleState,
    EnergyState, ConnectionState
)


def select_audio(state: AppState) -> AudioState:
    """Select audio state slice."""
    return state.audio


def select_climate(state: AppState) -> ClimateState:
    """Select climate state slice."""
    return state.climate


def select_vehicle(state: AppState) -> VehicleState:
    """Select vehicle state slice."""
    return state.vehicle


def select_energy(state: AppState) -> EnergyState:
    """Select energy state slice."""
    return state.energy


def select_connection(state: AppState) -> ConnectionState:
    """Select connection state slice."""
    return state.connection


# ─────────────────────────────────────────────────────────────────────────────
# Derived selectors (computed values)
# ─────────────────────────────────────────────────────────────────────────────

def select_volume_percent(state: AppState) -> float:
    """Get volume as percentage (0-100)."""
    return (state.audio.volume / 63.0) * 100


def select_battery_percent(state: AppState) -> int:
    """Get battery SOC as percentage (0-100)."""
    return int(state.energy.battery_soc * 100)


def select_is_charging(state: AppState) -> bool:
    """Check if battery is charging (regen or external)."""
    return state.energy.charging or state.energy.regen_active


def select_can_drive(state: AppState) -> bool:
    """Check if vehicle is ready to drive."""
    return state.vehicle.ready_mode and state.connection.connected


def select_display_temp(state: AppState) -> str:
    """Get formatted temperature display string."""
    return f"{state.climate.target_temp:.0f}°C"


def select_display_volume(state: AppState) -> str:
    """Get formatted volume display string."""
    if state.audio.muted:
        return "MUTE"
    return str(state.audio.volume)


def select_power_flow_direction(state: AppState) -> str:
    """
    Get power flow direction indicator.
    
    Returns: "CHARGE", "DISCHARGE", "IDLE"
    """
    if state.energy.charging or state.energy.regen_active:
        return "CHARGE"
    elif state.energy.discharging:
        return "DISCHARGE"
    return "IDLE"
