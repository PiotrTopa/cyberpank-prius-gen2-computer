"""
State Management Module.

Provides centralized state management with:
- Single source of truth (AppState)
- Actions for state modifications
- Subscriptions for reactive UI updates
- Middleware for side effects (Gateway communication)

Architecture:
    GATEWAY -> Actions -> Store -> Subscribers (UI)
    UI -> Actions -> Store -> Middleware -> Gateway
"""

from .app_state import (
    AppState,
    AudioState,
    ClimateState,
    VehicleState,
    EnergyState,
    ConnectionState,
    AudioSource,
    ClimateMode,
    GearPosition,
)
from .store import Store, StateSlice
from .actions import (
    Action,
    ActionType,
    ActionSource,
    # Audio actions
    SetVolumeAction,
    SetBassAction,
    SetMidAction,
    SetTrebleAction,
    SetBalanceAction,
    SetFaderAction,
    SetMuteAction,
    # Climate actions
    SetTargetTempAction,
    SetFanSpeedAction,
    SetACAction,
    SetAutoModeAction,
    # Vehicle actions
    SetReadyModeAction,
    SetParkModeAction,
    # Energy actions
    SetBatterySOCAction,
    SetChargingStateAction,
    # Connection actions
    SetConnectionStateAction,
    # Batch action
    BatchAction,
)
from .selectors import (
    select_audio,
    select_climate,
    select_vehicle,
    select_energy,
    select_connection,
    select_volume_percent,
    select_battery_percent,
    select_display_temp,
    select_display_volume,
)

__all__ = [
    # State classes
    "AppState",
    "AudioState",
    "ClimateState",
    "VehicleState",
    "EnergyState",
    "ConnectionState",
    "AudioSource",
    "ClimateMode",
    "GearPosition",
    # Store
    "Store",
    "StateSlice",
    # Actions
    "Action",
    "ActionType",
    "ActionSource",
    "SetVolumeAction",
    "SetBassAction",
    "SetMidAction",
    "SetTrebleAction",
    "SetBalanceAction",
    "SetFaderAction",
    "SetMuteAction",
    "SetTargetTempAction",
    "SetFanSpeedAction",
    "SetACAction",
    "SetAutoModeAction",
    "SetReadyModeAction",
    "SetParkModeAction",
    "SetBatterySOCAction",
    "SetChargingStateAction",
    "SetConnectionStateAction",
    "BatchAction",
    # Selectors
    "select_audio",
    "select_climate",
    "select_vehicle",
    "select_energy",
    "select_connection",
    "select_volume_percent",
    "select_battery_percent",
    "select_display_temp",
    "select_display_volume",
]
