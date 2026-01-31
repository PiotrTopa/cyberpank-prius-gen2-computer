"""
Application State - Single Source of Truth.

This module defines the complete application state structure.
All state is immutable - changes create new state objects.
"""

from dataclasses import dataclass, field, replace
from typing import Optional
from enum import Enum, auto


class AudioSource(Enum):
    """Audio source types."""
    UNKNOWN = auto()
    AM = auto()
    FM = auto()
    CD = auto()
    AUX = auto()
    BLUETOOTH = auto()
    USB = auto()


class ClimateMode(Enum):
    """Climate control mode."""
    OFF = auto()
    AUTO = auto()
    MANUAL = auto()
    DEFROST = auto()


class GearPosition(Enum):
    """Gear/shift position."""
    PARK = auto()
    REVERSE = auto()
    NEUTRAL = auto()
    DRIVE = auto()
    B = auto()  # Engine braking mode


class FuelType(Enum):
    """Active fuel type."""
    OFF = auto()
    PETROL = auto()
    LPG = auto()


@dataclass(frozen=True)
class AudioState:
    """
    Audio system state.
    
    Based on Toyota Prius Gen 2 audio specifications (Flerchinger document):
    - Volume: 0-63 (6-bit)
    - Bass/Mid/Treble: -5 to +5 (protocol: 0x0B-0x15, center=0x10)
    - Balance/Fader: -7 to +7 (protocol: 0x09-0x17, center=0x10)
    """
    volume: int = 25
    muted: bool = False
    bass: int = 0
    mid: int = 0
    treble: int = 0
    balance: int = 0  # Negative = Left, Positive = Right
    fader: int = 0    # Negative = Rear, Positive = Front
    source: AudioSource = AudioSource.UNKNOWN
    
    def with_volume(self, volume: int) -> "AudioState":
        """Return new state with updated volume."""
        return replace(self, volume=max(0, min(63, volume)))
    
    def with_bass(self, bass: int) -> "AudioState":
        """Return new state with updated bass."""
        return replace(self, bass=max(-5, min(5, bass)))
    
    def with_mid(self, mid: int) -> "AudioState":
        """Return new state with updated mid."""
        return replace(self, mid=max(-5, min(5, mid)))
    
    def with_treble(self, treble: int) -> "AudioState":
        """Return new state with updated treble."""
        return replace(self, treble=max(-5, min(5, treble)))
    
    def with_balance(self, balance: int) -> "AudioState":
        """Return new state with updated balance."""
        return replace(self, balance=max(-7, min(7, balance)))
    
    def with_fader(self, fader: int) -> "AudioState":
        """Return new state with updated fader."""
        return replace(self, fader=max(-7, min(7, fader)))


@dataclass(frozen=True)
class ClimateState:
    """
    Climate control state.
    
    Based on Toyota Prius Gen 2 climate specifications:
    - Temperature: 18-28°C (0.5° steps in some modes)
    - Fan speed: 0-7 (0 = off)
    """
    target_temp: float = 22.0
    inside_temp: Optional[float] = None
    outside_temp: Optional[float] = None
    fan_speed: int = 0  # 0-7
    ac_on: bool = False
    auto_mode: bool = False
    recirculation: bool = False
    defrost: bool = False
    mode: ClimateMode = ClimateMode.OFF
    
    def with_target_temp(self, temp: float) -> "ClimateState":
        """Return new state with updated target temperature."""
        return replace(self, target_temp=max(18.0, min(28.0, temp)))
    
    def with_fan_speed(self, speed: int) -> "ClimateState":
        """Return new state with updated fan speed."""
        return replace(self, fan_speed=max(0, min(7, speed)))


@dataclass(frozen=True)
class VehicleState:
    """
    Vehicle state (power, gear, etc.).
    
    Represents the hybrid system and drive state.
    """
    ready_mode: bool = False      # READY indicator on
    acc_on: bool = False          # ACC power on
    ig_on: bool = False           # IG-ON power on
    ice_running: bool = False     # Internal combustion engine running
    ev_mode: bool = False         # EV mode active
    gear: GearPosition = GearPosition.PARK
    speed_kmh: float = 0.0
    rpm: int = 0                  # ICE RPM
    
    # Pedals & Fuel
    throttle_position: int = 0    # 0-100% (approx) or raw 0-255
    brake_pressed: int = 0        # 0-127 (measure of pressure)
    fuel_level: int = 30          # Petrol liters (approx 0-45) - default for testing
    lpg_level: int = 45           # LPG liters (approx 0-60) - default for testing
    active_fuel: FuelType = FuelType.OFF  # Currently active fuel type
    fuel_flow_rate: float = 0.0   # L/h
    
    # Consumption
    instant_consumption: float = 0.0
    consumption_unit: str = "L/h" # "L/h" or "L/100km"
    
    # Temperatures
    ice_coolant_temp: Optional[float] = None  # Engine coolant temp (C)
    inverter_temp: Optional[float] = None     # Inverter/motor temp (C)
    
    @property
    def is_parked(self) -> bool:
        """Check if vehicle is in PARK."""
        return self.gear == GearPosition.PARK
    
    @property
    def is_driving(self) -> bool:
        """Check if vehicle is in a driving gear."""
        return self.gear in (GearPosition.DRIVE, GearPosition.REVERSE, GearPosition.B)


@dataclass(frozen=True)
class EnergyState:
    """
    Hybrid energy system state.
    
    Represents battery, motor, and energy flow.
    """
    battery_soc: float = 0.6       # State of charge (0.0-1.0)
    battery_temp: Optional[float] = None  # Battery pack temp (C)
    hv_battery_voltage: Optional[float] = None  # HV battery voltage (V)
    hv_battery_current: Optional[float] = None  # HV battery current (A)
    
    # Battery cell data
    battery_delta_soc: Optional[float] = None  # Delta between min/max cell SOC
    battery_min_cell_temp: Optional[float] = None
    battery_max_cell_temp: Optional[float] = None
    
    # Power flow (positive = output/discharge, negative = input/charge)
    motor_power_kw: float = 0.0    # MG2 power
    generator_power_kw: float = 0.0  # MG1 power
    ice_power_kw: float = 0.0      # Engine power
    
    # 0x3B6 Energy Flow Flags
    flow_engine_to_wheels: bool = False
    flow_battery_to_motor: bool = False
    flow_motor_to_battery: bool = False
    flow_engine_to_battery: bool = False
    flow_battery_to_wheels: bool = False # Indirect
    
    # Derived states
    charging: bool = False         # Battery being charged
    discharging: bool = False      # Battery being discharged
    regen_active: bool = False     # Regenerative braking active
    
    @property
    def net_power_kw(self) -> float:
        """Net power to/from battery."""
        return self.motor_power_kw + self.generator_power_kw
    
    @property
    def battery_power_kw(self) -> Optional[float]:
        """Calculate battery power from voltage and current."""
        if self.hv_battery_voltage is not None and self.hv_battery_current is not None:
            return (self.hv_battery_voltage * self.hv_battery_current) / 1000.0
        return None


@dataclass(frozen=True)
class ConnectionState:
    """
    Gateway connection state.
    """
    connected: bool = False
    gateway_version: Optional[str] = None
    can_ready: bool = False
    avc_ready: bool = False
    last_message_time: Optional[float] = None


@dataclass(frozen=True)
class DebugState:
    """Debug and Analysis state."""
    last_avc_input: str = ""  # Last detected button/touch
    last_can_message: str = ""
    avc_input_count: int = 0


@dataclass(frozen=True)
class InputState:
    """
    AVC-LAN input state (buttons and touch).
    
    Tracks the last detected button press and touch event
    for debugging and UI visualization.
    """
    # Last button press
    last_button_code: int = 0
    last_button_name: str = ""
    last_button_time: float = 0.0
    button_press_count: int = 0
    
    # Last touch event
    last_touch_x: int = 0
    last_touch_y: int = 0
    last_touch_type: str = ""
    last_touch_time: float = 0.0
    touch_event_count: int = 0
    
    # Recent button history (for debugging)
    # Note: For immutable state, we track just the last few raw codes
    recent_buttons: tuple = ()  # Up to 5 recent button codes
    
    # AVC-LAN debug bytes (for manual correlation)
    last_avc_110_490_bytes: tuple = (0, 0, 0, 0, 0, 0, 0, 0)  # MFD status/flow arrows
    last_avc_a00_258_bytes: tuple = tuple([0] * 32)  # SOC/energy broadcast
    
    @property
    def touch_active(self) -> bool:
        """Check if touch is currently active (within last 500ms)."""
        import time
        return (time.time() - self.last_touch_time) < 0.5 if self.last_touch_time else False


@dataclass(frozen=True)
class DisplayState:
    """
    Display settings state.
    
    Controls VFD and other display-related settings.
    """
    # Power chart time base in seconds
    # Options: 15, 60, 300, 900, 3600 (15s, 1m, 5m, 15m, 1h)
    power_chart_time_base: int = 60
    
    def with_time_base(self, seconds: int) -> "DisplayState":
        """Return new state with updated time base."""
        valid_options = [15, 60, 300, 900, 3600]
        if seconds in valid_options:
            return replace(self, power_chart_time_base=seconds)
        return self


@dataclass(frozen=True)
class AppState:
    """
    Complete application state.
    
    This is the single source of truth for the entire application.
    All UI components should derive their display from this state.
    All state modifications go through the Store.
    """
    audio: AudioState = field(default_factory=AudioState)
    climate: ClimateState = field(default_factory=ClimateState)
    vehicle: VehicleState = field(default_factory=VehicleState)
    energy: EnergyState = field(default_factory=EnergyState)
    connection: ConnectionState = field(default_factory=ConnectionState)
    debug: DebugState = field(default_factory=DebugState)
    input: InputState = field(default_factory=InputState)
    display: DisplayState = field(default_factory=DisplayState)
    
    # UI-only state (not from vehicle)
    screen_brightness: int = 100
    ambient_hue: int = 180
    ambient_saturation: int = 100
    ambient_brightness: int = 50
    
    def __post_init__(self):
        """Ensure nested states are properly initialized."""
        # frozen=True handles immutability
        pass
