"""
AVC-LAN State Manager for Cyberpunk Computer.

This module tracks the current state of various vehicle systems
based on decoded AVC-LAN messages and provides event-based updates
to the UI layer.
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Optional, Any
from collections import defaultdict
import time

from .avc_decoder import AVCMessage, AVCDecoder


class AVCEventType(Enum):
    """Types of events emitted by the state manager."""
    
    # Power events
    POWER_ON = auto()
    POWER_OFF = auto()
    ACC_ON = auto()
    ICE_START = auto()
    ICE_STOP = auto()
    
    # Audio events
    VOLUME_CHANGE = auto()
    MUTE_TOGGLE = auto()
    SOURCE_CHANGE = auto()
    AMP_ON = auto()
    AMP_OFF = auto()
    
    # Climate events
    CLIMATE_STATE = auto()
    CLIMATE_AUTO = auto()
    CLIMATE_OFF = auto()
    FAN_SPEED = auto()
    TEMPERATURE = auto()
    
    # Display events
    DISPLAY_MODE = auto()
    TOUCH_EVENT = auto()
    BUTTON_PRESS = auto()
    
    # System events
    CONNECTION_STATE = auto()
    HEARTBEAT = auto()
    ERROR = auto()


class AudioSource(Enum):
    """Audio source types."""
    
    OFF = auto()
    RADIO_FM = auto()
    RADIO_AM = auto()
    CD = auto()
    CD_CHANGER = auto()
    AUX = auto()
    BLUETOOTH = auto()
    USB = auto()
    UNKNOWN = auto()


class DisplayMode(Enum):
    """Display mode types."""
    
    OFF = auto()
    AUDIO = auto()
    CLIMATE = auto()
    ENERGY = auto()
    NAVIGATION = auto()
    INFO = auto()
    SETTINGS = auto()
    UNKNOWN = auto()


class ClimateMode(Enum):
    """Climate control mode types."""
    
    OFF = auto()
    AUTO = auto()
    MANUAL = auto()
    DEFROST = auto()


@dataclass
class AudioState:
    """
    Current audio system state.
    
    Value ranges based on Flerchinger document:
    - Volume: 0-63 (6-bit) or 0-255 for status display
    - Bass/Mid/Treble: 0x0B-0x15 (raw), displayed as -5 to +5
    - Balance/Fade: 0x09-0x17 (raw), displayed as -7 to +7
    
    Internal storage uses display values for convenience.
    Conversion to/from protocol values done at command/parse time.
    """
    
    volume: int = 0            # 0-63 (Toyota uses 6-bit volume)
    muted: bool = False
    source: AudioSource = AudioSource.OFF
    amp_on: bool = False
    
    # Tone controls: -5 to +5 (display value)
    # Protocol value = display + 0x10 (e.g., -5→0x0B, 0→0x10, +5→0x15)
    bass: int = 0              # -5 to +5
    mid: int = 0               # -5 to +5
    treble: int = 0            # -5 to +5
    
    # Balance/Fade: -7 to +7 (display value)
    # Protocol value = display + 0x10 (e.g., -7→0x09, 0→0x10, +7→0x17)
    balance: int = 0           # -7 (left) to +7 (right)
    fade: int = 0              # -7 (front) to +7 (rear)
    
    def volume_percent(self) -> int:
        """Get volume as percentage (based on 0-63 range)."""
        return int((self.volume / 63) * 100)
    
    def bass_to_protocol(self) -> int:
        """Convert bass display value to protocol value."""
        return max(0x0B, min(0x15, self.bass + 0x10))
    
    def treble_to_protocol(self) -> int:
        """Convert treble display value to protocol value."""
        return max(0x0B, min(0x15, self.treble + 0x10))
    
    def mid_to_protocol(self) -> int:
        """Convert mid display value to protocol value."""
        return max(0x0B, min(0x15, self.mid + 0x10))
    
    def balance_to_protocol(self) -> int:
        """Convert balance display value to protocol value."""
        return max(0x09, min(0x17, self.balance + 0x10))
    
    def fade_to_protocol(self) -> int:
        """Convert fade display value to protocol value."""
        return max(0x09, min(0x17, self.fade + 0x10))
    
    @staticmethod
    def protocol_to_tone(value: int) -> int:
        """Convert protocol tone value to display value (-5 to +5)."""
        return max(-5, min(5, value - 0x10))
    
    @staticmethod
    def protocol_to_balance_fade(value: int) -> int:
        """Convert protocol balance/fade value to display value (-7 to +7)."""
        return max(-7, min(7, value - 0x10))


@dataclass
class ClimateState:
    """Current climate control state."""
    
    mode: ClimateMode = ClimateMode.OFF
    fan_speed: int = 0         # 0-7
    temperature: float = 22.0  # Celsius
    ac_on: bool = False
    recirculate: bool = False
    defrost: bool = False
    rear_defrost: bool = False
    dual_zone: bool = False
    left_temp: float = 22.0
    right_temp: float = 22.0


@dataclass
class VehicleState:
    """Current vehicle power/status state."""
    
    acc_on: bool = False
    ice_running: bool = False
    ev_mode: bool = False
    park: bool = True
    ready: bool = False


@dataclass
class DisplayState:
    """Current display state."""
    
    mode: DisplayMode = DisplayMode.OFF
    brightness: int = 128      # 0-255
    last_touch_x: int = 0
    last_touch_y: int = 0
    last_touch_time: float = 0


EventCallback = Callable[[AVCEventType, Any], None]


class AVCStateManager:
    """
    Manages vehicle state based on AVC-LAN messages.
    
    Receives decoded AVC-LAN messages, updates internal state,
    and emits events to registered listeners.
    """
    
    def __init__(self) -> None:
        """Initialize the state manager."""
        self._decoder = AVCDecoder()
        
        # Current states
        self.audio = AudioState()
        self.climate = ClimateState()
        self.vehicle = VehicleState()
        self.display = DisplayState()
        
        # Event listeners
        self._listeners: dict[AVCEventType, list[EventCallback]] = defaultdict(list)
        self._global_listeners: list[EventCallback] = []
        
        # Message statistics
        self._msg_count = 0
        self._last_msg_time = 0.0
        self._connected = False
        
        # Pattern detection
        self._last_patterns: dict[str, list[int]] = {}
    
    @property
    def connected(self) -> bool:
        """Check if we're receiving messages."""
        return self._connected and (time.time() - self._last_msg_time) < 5.0
    
    def subscribe(
        self,
        event_type: Optional[AVCEventType],
        callback: EventCallback
    ) -> None:
        """
        Subscribe to events.
        
        Args:
            event_type: Specific event type, or None for all events
            callback: Function to call with (event_type, data)
        """
        if event_type is None:
            self._global_listeners.append(callback)
        else:
            self._listeners[event_type].append(callback)
    
    def unsubscribe(
        self,
        event_type: Optional[AVCEventType],
        callback: EventCallback
    ) -> None:
        """Unsubscribe from events."""
        if event_type is None:
            if callback in self._global_listeners:
                self._global_listeners.remove(callback)
        else:
            if callback in self._listeners[event_type]:
                self._listeners[event_type].remove(callback)
    
    def _emit(self, event_type: AVCEventType, data: Any = None) -> None:
        """Emit an event to all listeners."""
        # Specific listeners
        for callback in self._listeners[event_type]:
            try:
                callback(event_type, data)
            except Exception:
                pass  # Don't let callback errors break state manager
        
        # Global listeners
        for callback in self._global_listeners:
            try:
                callback(event_type, data)
            except Exception:
                pass
    
    def process_raw_message(self, raw: dict) -> Optional[AVCMessage]:
        """
        Process a raw gateway message.
        
        Args:
            raw: Raw message dict from gateway NDJSON
            
        Returns:
            Decoded message if valid, None otherwise
        """
        # Handle gateway status messages
        if raw.get("id") == 0:
            d = raw.get("d", {})
            if d.get("msg") == "GATEWAY_READY":
                self._connected = True
                self._emit(AVCEventType.CONNECTION_STATE, {"connected": True})
                return None
        
        # Decode AVC-LAN message
        msg = self._decoder.decode_message(raw)
        if msg is None:
            return None
        
        self._msg_count += 1
        self._last_msg_time = time.time()
        self._connected = True
        
        # Process message based on type
        self._process_message(msg)
        
        return msg
    
    def _process_message(self, msg: AVCMessage) -> None:
        """Process a decoded AVC-LAN message and update state."""
        classification = self._decoder.classify_message(msg)
        
        if classification == "power_status":
            self._handle_power_status(msg)
        elif classification == "climate_state":
            self._handle_climate_state(msg)
        elif classification == "button_press":
            self._handle_button_press(msg)
        elif classification == "system_status":
            self._handle_system_status(msg)
        elif classification == "emv_status":
            self._handle_emv_status(msg)
        elif classification == "audio_control":
            self._handle_audio_control(msg)
        elif classification == "touch_event":
            self._handle_touch_event(msg)
    
    def _handle_power_status(self, msg: AVCMessage) -> None:
        """Handle power status messages (400 → 020)."""
        if len(msg.data) >= 1:
            status_byte = msg.data[0]
            
            # 0x21 appears frequently - likely heartbeat/ready status
            if status_byte == 0x21:
                if not self.vehicle.ready:
                    self.vehicle.ready = True
                    self._emit(AVCEventType.POWER_ON, self.vehicle)
                self._emit(AVCEventType.HEARTBEAT)
            
            # 0x24 might indicate different power state
            elif status_byte == 0x24:
                self._emit(AVCEventType.HEARTBEAT)
    
    def _handle_climate_state(self, msg: AVCMessage) -> None:
        """Handle climate control messages (10C → 310)."""
        if len(msg.data) < 8:
            return
        
        old_mode = self.climate.mode
        
        # Parse climate bytes
        # Byte 4-7 seem to contain climate state
        b4, b5, b6, b7 = msg.data[4:8]
        
        # 0x0A 0x90 0x80 pattern seems to indicate active climate
        if b5 == 0x0A and b6 == 0x90:
            if self.climate.mode == ClimateMode.OFF:
                self.climate.mode = ClimateMode.AUTO
                self._emit(AVCEventType.CLIMATE_AUTO, self.climate)
        
        # 0x00 0x00 0x00 might indicate off
        elif b5 == 0x00 and b6 == 0x00 and b7 == 0x00:
            if self.climate.mode != ClimateMode.OFF:
                self.climate.mode = ClimateMode.OFF
                self._emit(AVCEventType.CLIMATE_OFF, self.climate)
        
        # Emit general climate state if changed
        if old_mode != self.climate.mode:
            self._emit(AVCEventType.CLIMATE_STATE, self.climate)
    
    def _handle_button_press(self, msg: AVCMessage) -> None:
        """Handle button press messages (040 → 200)."""
        if len(msg.data) < 5:
            return
        
        button_data = {
            "prefix": msg.data[0],
            "modifier": msg.data[1],
            "code1": msg.data[2],
            "code2": msg.data[3],
            "suffix": msg.data[4],
            "raw": msg.data,
        }
        
        # Common button codes observed:
        # 28 00 60 44 62 - appears frequently
        # 28 00 00 05 22 - different button
        # 28 00 C1 04 62 - another variant
        
        self._emit(AVCEventType.BUTTON_PRESS, button_data)
    
    def _handle_system_status(self, msg: AVCMessage) -> None:
        """Handle system status messages (→ 490)."""
        if len(msg.data) < 4:
            return
        
        # Common patterns:
        # 00 46 C1 80 - status heartbeat
        # 00 44 60 80 - audio status
        # 00 00 00 08 A4 04 02 00 - command status
        
        b0, b1, b2, b3 = msg.data[0:4]
        
        # 46 C1 pattern - system status
        if b1 == 0x46 and b2 == 0xC1:
            # Bit 0 of b3 might indicate state change
            pass
        
        # 44 60 pattern - audio/display status
        elif b1 == 0x44 and b2 == 0x60:
            # Bit patterns might indicate display mode
            if b3 & 0x08:
                # Some flag is set
                pass
        
        # A4 04 pattern - operation mode
        elif len(msg.data) >= 8:
            if msg.data[4] == 0xA4 and msg.data[5] == 0x04:
                mode_byte = msg.data[6]
                # 0x02 - normal
                # 0x03 - transition
                # 0x05 - active
                # 0x06 - config
    
    def _handle_emv_status(self, msg: AVCMessage) -> None:
        """Handle EMV/MFD status messages (110/112 → 490)."""
        # Similar to system status but from display
        pass
    
    def _handle_audio_control(self, msg: AVCMessage) -> None:
        """Handle audio control messages (→ 440/480)."""
        old_amp = self.audio.amp_on
        
        # Look for AMP state changes
        # Messages to 440 (DSP-AMP) or 480 (AMP)
        
        # If we see traffic to amp, it's probably on
        if not self.audio.amp_on:
            self.audio.amp_on = True
            self._emit(AVCEventType.AMP_ON, self.audio)
    
    def _handle_touch_event(self, msg: AVCMessage) -> None:
        """
        Handle touch events (110 → 178).
        
        Touch format from Flerchinger document:
        [0 21 24 78 xx yy xx yy]
        - byte[0]: 0x00 prefix
        - byte[1]: 0x21 (switch logic device)
        - byte[2]: 0x24 (SW_CONVERT)
        - byte[3]: 0x78 (touch press indicator)
        - byte[4-5]: X,Y coordinates
        - byte[6-7]: X,Y repeated
        
        Coordinates are 8-bit (0-255):
        - X: 0=left, 255=right
        - Y: 0=top, 255=bottom
        """
        if len(msg.data) >= 8:
            # Check for documented touch format
            if (msg.data[0] == 0x00 and msg.data[1] == 0x21 and 
                msg.data[2] == 0x24 and msg.data[3] == 0x78):
                x = msg.data[4]
                y = msg.data[5]
                
                self.display.last_touch_x = x
                self.display.last_touch_y = y
                self.display.last_touch_time = time.time()
                
                self._emit(AVCEventType.TOUCH_EVENT, {
                    "x": x,
                    "y": y,
                    "normalized_x": x / 255.0,
                    "normalized_y": y / 255.0,
                    "confidence": "high",
                    "format": "documented",
                })
                return
            
            # Fallback: try to extract from positions 4-5
            x = msg.data[4]
            y = msg.data[5]
            
            self.display.last_touch_x = x
            self.display.last_touch_y = y
            self.display.last_touch_time = time.time()
            
            self._emit(AVCEventType.TOUCH_EVENT, {
                "x": x,
                "y": y,
                "normalized_x": x / 255.0,
                "normalized_y": y / 255.0,
                "confidence": "medium",
                "format": "inferred",
            })
    
    def get_state_snapshot(self) -> dict:
        """Get a snapshot of all current states."""
        return {
            "audio": {
                "volume": self.audio.volume,
                "volume_percent": self.audio.volume_percent(),
                "muted": self.audio.muted,
                "source": self.audio.source.name,
                "amp_on": self.audio.amp_on,
            },
            "climate": {
                "mode": self.climate.mode.name,
                "fan_speed": self.climate.fan_speed,
                "temperature": self.climate.temperature,
                "ac_on": self.climate.ac_on,
            },
            "vehicle": {
                "acc_on": self.vehicle.acc_on,
                "ice_running": self.vehicle.ice_running,
                "ready": self.vehicle.ready,
                "park": self.vehicle.park,
            },
            "display": {
                "mode": self.display.mode.name,
                "brightness": self.display.brightness,
            },
            "connection": {
                "connected": self.connected,
                "message_count": self._msg_count,
            },
        }
