"""
Actions - Commands that modify state.

Actions are simple data objects that describe what happened.
They are processed by the Store to create new state.

Action types:
- From Gateway: Data received from vehicle (AVC-LAN, CAN)
- From UI: User interaction (button press, value change)
- Internal: Application logic (timeouts, calculations)
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, List, Optional


class ActionType(Enum):
    """All possible action types."""
    
    # Audio actions
    SET_VOLUME = auto()
    SET_BASS = auto()
    SET_MID = auto()
    SET_TREBLE = auto()
    SET_BALANCE = auto()
    SET_FADER = auto()
    SET_MUTE = auto()
    SET_AUDIO_SOURCE = auto()
    
    # Climate actions
    SET_TARGET_TEMP = auto()
    SET_FAN_SPEED = auto()
    SET_AC = auto()
    SET_AUTO_MODE = auto()
    SET_RECIRCULATION = auto()
    SET_DEFROST = auto()
    SET_INSIDE_TEMP = auto()
    SET_OUTSIDE_TEMP = auto()
    
    # Vehicle actions
    SET_READY_MODE = auto()
    SET_PARK_MODE = auto()
    SET_GEAR = auto()
    SET_SPEED = auto()
    SET_ICE_RUNNING = auto()
    SET_EV_MODE = auto()
    SET_RPM = auto()
    SET_ICE_COOLANT_TEMP = auto()
    SET_INVERTER_TEMP = auto()
    SET_THROTTLE_POSITION = auto()
    SET_BRAKE_PRESSED = auto()
    SET_FUEL_LEVEL = auto()
    SET_FUEL_FLOW = auto() # Instant fuel flow rate (L/h)
    SET_INSTANT_CONSUMPTION = auto() # Calculated instant consumption
    
    # Energy actions
    SET_BATTERY_SOC = auto()
    SET_CHARGING_STATE = auto()
    SET_POWER_FLOW = auto()  # General power flow KW values
    SET_ENERGY_FLOW_FLAGS = auto() # New: 0x3B6 directional arrows
    SET_REGEN_STATE = auto()
    SET_BATTERY_VOLTAGE = auto()
    SET_BATTERY_CURRENT = auto()
    SET_BATTERY_TEMP = auto()
    SET_BATTERY_MAX_TEMP = auto() # New: Byte 5 of 0x3CB
    SET_BATTERY_DELTA_SOC = auto()  # Delta between min/max cell blocks
    
    # Connection actions
    SET_CONNECTION_STATE = auto()
    GATEWAY_READY = auto()
    GATEWAY_DISCONNECTED = auto()
    
    # UI actions
    SET_SCREEN_BRIGHTNESS = auto()
    SET_AMBIENT_COLOR = auto()
    
    # AVC Input actions (buttons and touch)
    AVC_BUTTON_PRESS = auto()
    AVC_BUTTON_RELEASE = auto()
    AVC_TOUCH_EVENT = auto()
    
    # Debug/Analysis actions
    UPDATE_DEBUG_INFO = auto()
    
    # Batch action
    BATCH = auto()


class ActionSource(Enum):
    """Source of action - for middleware routing."""
    GATEWAY = auto()   # From vehicle (don't echo back)
    UI = auto()        # From user (send to vehicle)
    INTERNAL = auto()  # From app logic


@dataclass
class Action:
    """
    Base action class.
    
    All actions have a type and optional source.
    Source determines if action should be sent to Gateway.
    """
    type: ActionType
    source: ActionSource = ActionSource.INTERNAL
    
    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(type={self.type.name})"


# ─────────────────────────────────────────────────────────────────────────────
# Audio Actions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SetVolumeAction(Action):
    """Set audio volume (0-63)."""
    volume: int = 0
    
    def __init__(self, volume: int, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_VOLUME, source)
        self.volume = volume


@dataclass
class SetBassAction(Action):
    """Set bass level (-5 to +5)."""
    bass: int = 0
    
    def __init__(self, bass: int, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_BASS, source)
        self.bass = bass


@dataclass
class SetMidAction(Action):
    """Set mid level (-5 to +5)."""
    mid: int = 0
    
    def __init__(self, mid: int, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_MID, source)
        self.mid = mid


@dataclass
class SetTrebleAction(Action):
    """Set treble level (-5 to +5)."""
    treble: int = 0
    
    def __init__(self, treble: int, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_TREBLE, source)
        self.treble = treble


@dataclass
class SetBalanceAction(Action):
    """Set balance (-7 to +7, negative=left)."""
    balance: int = 0
    
    def __init__(self, balance: int, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_BALANCE, source)
        self.balance = balance


@dataclass
class SetFaderAction(Action):
    """Set fader (-7 to +7, negative=rear)."""
    fader: int = 0
    
    def __init__(self, fader: int, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_FADER, source)
        self.fader = fader


@dataclass
class SetMuteAction(Action):
    """Set mute state."""
    muted: bool = False
    
    def __init__(self, muted: bool, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_MUTE, source)
        self.muted = muted


# ─────────────────────────────────────────────────────────────────────────────
# Climate Actions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SetTargetTempAction(Action):
    """Set target temperature (18-28°C)."""
    temp: float = 22.0
    
    def __init__(self, temp: float, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_TARGET_TEMP, source)
        self.temp = temp


@dataclass
class SetFanSpeedAction(Action):
    """Set fan speed (0-7)."""
    speed: int = 0
    
    def __init__(self, speed: int, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_FAN_SPEED, source)
        self.speed = speed


@dataclass
class SetACAction(Action):
    """Set AC on/off."""
    ac_on: bool = False
    
    def __init__(self, ac_on: bool, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_AC, source)
        self.ac_on = ac_on


@dataclass
class SetAutoModeAction(Action):
    """Set climate auto mode."""
    auto_mode: bool = False
    
    def __init__(self, auto_mode: bool, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_AUTO_MODE, source)
        self.auto_mode = auto_mode


@dataclass
class SetRecirculationAction(Action):
    """Set air recirculation on/off."""
    recirculation: bool = False
    
    def __init__(self, recirculation: bool, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_RECIRCULATION, source)
        self.recirculation = recirculation


@dataclass
class SetAirDirectionAction(Action):
    """Set air direction (0=FACE, 1=FACE+FEET, 2=FEET, 3=DEFROST)."""
    direction: int = 0
    
    def __init__(self, direction: int, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_DEFROST, source)  # Reuse defrost type for now
        self.direction = direction


@dataclass
class SetOutsideTempAction(Action):
    """Set outside temperature (from AVC-LAN climate messages)."""
    temp: float = 0.0
    
    def __init__(self, temp: float, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_OUTSIDE_TEMP, source)
        self.temp = temp


# ─────────────────────────────────────────────────────────────────────────────
# Vehicle Actions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SetReadyModeAction(Action):
    """Set vehicle READY mode state."""
    ready: bool = False
    
    def __init__(self, ready: bool, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_READY_MODE, source)
        self.ready = ready


@dataclass
class SetParkModeAction(Action):
    """Set vehicle park mode (gear position)."""
    parked: bool = True
    
    def __init__(self, parked: bool, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_PARK_MODE, source)
        self.parked = parked
        
@dataclass
class SetGearAction(Action):
    """Set vehicle gear position."""
    from .app_state import GearPosition
    gear: GearPosition = GearPosition.PARK
    
    def __init__(self, gear: GearPosition, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_GEAR, source)
        self.gear = gear

@dataclass
class SetICERunningAction(Action):
    """
    Set ICE (Internal Combustion Engine) running state.
    
    Detected from AVC-LAN messages 110->490 or 210->490:
    - Pattern 'C8' in byte[2] indicates ICE running
    - Pattern 'C1' in byte[2] indicates ICE stopped
    
    Note: For precise ICE status, CAN bus data is preferred.
    """
    running: bool = False
    
    def __init__(self, running: bool, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_ICE_RUNNING, source)
        self.running = running


# ─────────────────────────────────────────────────────────────────────────────
# Energy Actions
@dataclass
class SetThrottlePositionAction(Action):
    """Set throttle pedal position (0-100%)."""
    position: int = 0
    
    def __init__(self, position: int, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_THROTTLE_POSITION, source)
        self.position = position


@dataclass
class SetBrakePressedAction(Action):
    """Set brake pedal pressure (0-127)."""
    pressure: int = 0
    
    def __init__(self, pressure: int, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_BRAKE_PRESSED, source)
        self.pressure = pressure


@dataclass
class SetFuelLevelAction(Action):
    """Set fuel level in liters."""
    liters: int = 0
    
    def __init__(self, liters: int, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_FUEL_LEVEL, source)
        self.liters = liters


# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SetFuelFlowAction(Action):
    """Set instant fuel flow rate (L/h)."""
    flow_rate: float = 0.0
    
    def __init__(self, flow_rate: float, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_FUEL_FLOW, source)
        self.flow_rate = flow_rate


@dataclass
class SetInstantConsumptionAction(Action):
    """Set calculated instant consumption."""
    value: float = 0.0
    unit: str = "L/h"
    
    def __init__(self, value: float, unit: str = "L/h", source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_INSTANT_CONSUMPTION, source)
        self.value = value
        self.unit = unit


@dataclass
class SetEnergyFlowFlagsAction(Action):
    """Set energy flow direction flags (0x3B6)."""
    engine_to_wheels: bool = False
    battery_to_motor: bool = False
    motor_to_battery: bool = False
    engine_to_battery: bool = False
    battery_to_wheels: bool = False
    
    def __init__(self, 
                 engine_to_wheels: bool = False,
                 battery_to_motor: bool = False,
                 motor_to_battery: bool = False,
                 engine_to_battery: bool = False,
                 battery_to_wheels: bool = False,
                 source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_ENERGY_FLOW_FLAGS, source)
        self.engine_to_wheels = engine_to_wheels
        self.battery_to_motor = battery_to_motor
        self.motor_to_battery = motor_to_battery
        self.engine_to_battery = engine_to_battery
        self.battery_to_wheels = battery_to_wheels

@dataclass
class SetBatteryMaxTempAction(Action):
    """Set battery max temperature."""
    temp: float = 0.0
    
    def __init__(self, temp: float, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_BATTERY_MAX_TEMP, source)
        self.temp = temp

@dataclass
class SetBatterySOCAction(Action):
    """Set battery state of charge (0.0-1.0)."""
    soc: float = 0.6
    
    def __init__(self, soc: float, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_BATTERY_SOC, source)
        self.soc = soc


@dataclass
class SetChargingStateAction(Action):
    """Set charging/discharging state."""
    charging: bool = False
    discharging: bool = False
    
    def __init__(self, charging: bool = False, discharging: bool = False,
                 source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_CHARGING_STATE, source)
        self.charging = charging
        self.discharging = discharging


@dataclass
class SetBatteryVoltageAction(Action):
    """Set HV battery voltage."""
    voltage: float = 0.0
    
    def __init__(self, voltage: float, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_BATTERY_VOLTAGE, source)
        self.voltage = voltage


@dataclass
class SetBatteryCurrentAction(Action):
    """Set HV battery current (positive = discharge, negative = charge)."""
    current: float = 0.0
    
    def __init__(self, current: float, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_BATTERY_CURRENT, source)
        self.current = current


@dataclass
class SetBatteryTempAction(Action):
    """Set battery temperature."""
    temp: float = 0.0
    
    def __init__(self, temp: float, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_BATTERY_TEMP, source)
        self.temp = temp


@dataclass
class SetBatteryDeltaSOCAction(Action):
    """Set battery delta SOC (difference between min/max cell blocks in %)."""
    delta_soc: float = 0.0
    
    def __init__(self, delta_soc: float, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_BATTERY_DELTA_SOC, source)
        self.delta_soc = delta_soc


@dataclass
class SetRPMAction(Action):
    """Set ICE RPM."""
    rpm: int = 0
    
    def __init__(self, rpm: int, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_RPM, source)
        self.rpm = rpm


@dataclass
class SetSpeedAction(Action):
    """Set vehicle speed in km/h."""
    speed_kmh: float = 0.0
    
    def __init__(self, speed_kmh: float, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_SPEED, source)
        self.speed_kmh = speed_kmh


@dataclass
class SetICECoolantTempAction(Action):
    """Set ICE coolant temperature."""
    temp: float = 0.0
    
    def __init__(self, temp: float, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_ICE_COOLANT_TEMP, source)
        self.temp = temp


@dataclass
class SetInverterTempAction(Action):
    """Set inverter/motor temperature."""
    temp: float = 0.0
    
    def __init__(self, temp: float, source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_INVERTER_TEMP, source)
        self.temp = temp


# ─────────────────────────────────────────────────────────────────────────────
# Connection Actions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SetConnectionStateAction(Action):
    """Set gateway connection state."""
    connected: bool = False
    gateway_version: Optional[str] = None
    
    def __init__(self, connected: bool, gateway_version: Optional[str] = None,
                 source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_CONNECTION_STATE, source)
        self.connected = connected
        self.gateway_version = gateway_version


# ─────────────────────────────────────────────────────────────────────────────
# Debug Actions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class UpdateDebugInfoAction(Action):
    """Update debug/analysis information."""
    last_avc_input: Optional[str] = None
    last_can_message: Optional[str] = None
    
    def __init__(
        self, 
        last_avc_input: Optional[str] = None,
        last_can_message: Optional[str] = None,
        source: ActionSource = ActionSource.INTERNAL
    ):
        super().__init__(ActionType.UPDATE_DEBUG_INFO, source)
        self.last_avc_input = last_avc_input
        self.last_can_message = last_can_message


# ─────────────────────────────────────────────────────────────────────────────
# AVC Input Actions (Buttons and Touch)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AVCButtonPressAction(Action):
    """
    Physical button press detected from AVC-LAN (040 → 200).
    
    Button press messages contain 5 bytes:
    - byte[0]: Command type (0x28 = press, 0x2A = release)
    - byte[1]: Modifier (usually 0x00)
    - byte[2-3]: Button code (varies by button)
    - byte[4]: Suffix/device ID
    
    Common button codes observed:
    - 60 44 62: Most frequent (likely status/heartbeat)
    - C1 04 62: Climate/AC related
    - 00 05 22: Menu/Info buttons
    - 61 84 62: Audio related
    - 30 A4 62: Navigation/Map
    """
    button_code: int = 0           # Combined code (byte2 << 8 | byte3)
    modifier: int = 0              # Modifier byte
    suffix: int = 0                # Suffix byte
    is_press: bool = True          # True=press, False=release
    raw_data: List[int] = None     # Raw bytes for analysis
    button_name: str = ""          # Human-readable name (if known)
    
    def __init__(
        self, 
        button_code: int,
        modifier: int = 0,
        suffix: int = 0,
        is_press: bool = True,
        raw_data: List[int] = None,
        button_name: str = "",
        source: ActionSource = ActionSource.GATEWAY
    ):
        super().__init__(ActionType.AVC_BUTTON_PRESS, source)
        self.button_code = button_code
        self.modifier = modifier
        self.suffix = suffix
        self.is_press = is_press
        self.raw_data = raw_data or []
        self.button_name = button_name


@dataclass
class AVCTouchEventAction(Action):
    """
    Touch screen event detected from AVC-LAN (000 → 114).
    
    Touch messages contain coordinate and event type data.
    The exact format varies but common patterns:
    - Short messages (2-4 bytes): Status/tap events
    - Long messages (8+ bytes): Coordinate data
    
    Touch coordinate system (estimated from analysis):
    - X: 0-255 (left to right)
    - Y: 0-255 (top to bottom)
    """
    x: int = 0                     # X coordinate (0-255)
    y: int = 0                     # Y coordinate (0-255)
    touch_type: str = "unknown"    # "press", "release", "drag", "unknown"
    raw_data: List[int] = None     # Raw bytes for analysis
    
    def __init__(
        self,
        x: int = 0,
        y: int = 0,
        touch_type: str = "unknown",
        raw_data: List[int] = None,
        source: ActionSource = ActionSource.GATEWAY
    ):
        super().__init__(ActionType.AVC_TOUCH_EVENT, source)
        self.x = x
        self.y = y
        self.touch_type = touch_type
        self.raw_data = raw_data or []
    
    @property
    def normalized_x(self) -> float:
        """Get X as 0.0-1.0 value."""
        return self.x / 255.0
    
    @property
    def normalized_y(self) -> float:
        """Get Y as 0.0-1.0 value."""
        return self.y / 255.0


# ─────────────────────────────────────────────────────────────────────────────
# Batch Action
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BatchAction(Action):
    """
    Batch multiple actions together.
    
    Useful for atomic updates from a single gateway message
    that affects multiple state slices.
    """
    actions: List[Action] = None
    
    def __init__(self, actions: List[Action], source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.BATCH, source)
        self.actions = actions or []
