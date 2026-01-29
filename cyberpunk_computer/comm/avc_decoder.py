"""
AVC-LAN Protocol Decoder for Toyota Prius Gen 2.

This module provides decoding and interpretation of AVC-LAN messages
received from the gateway in NDJSON format.

AVC-LAN is Toyota's implementation of the IEBus (NEC Inter Equipment Bus).
"""

from dataclasses import dataclass, field
from enum import IntEnum, Enum, auto
from typing import Optional, Tuple


class DeviceType(Enum):
    """Known AVC-LAN device types."""
    
    EMV = auto()           # Multi-Function Display
    AUDIO_HU = auto()      # Audio Head Unit
    DSP_AMP = auto()       # DSP Amplifier
    AMP = auto()           # Amplifier
    NAVI = auto()          # Navigation ECU
    TUNER = auto()         # Radio Tuner
    CD_CHANGER = auto()    # CD Changer
    CLIMATE = auto()       # Climate Control
    PANEL = auto()         # Control Panel
    STEERING = auto()      # Steering Wheel Controls
    CAMERA = auto()        # Backup Camera
    UNKNOWN = auto()       # Unknown device


class LogicDeviceID(IntEnum):
    """
    Logic device IDs used in AVC-LAN protocol.
    
    These are used in the data field of AVC-LAN messages to identify
    the logical function being addressed. Based on Toyota Prius Gen 2
    analysis and Flerchinger document.
    """
    
    COMM_CTRL = 0x01       # Communication control
    COMMUNICATION = 0x12   # Communication
    SWITCH = 0x21          # Switch
    SWITCH_NAME = 0x23     # Switch with name
    SW_CONVERT = 0x24      # Switch converting (touch screen coordinate magic)
    CMD_SWITCH = 0x25      # Command switch (audio parameter commands)
    BEEP_HU = 0x28         # Beep device in Head Unit
    BEEP_SPEAKER = 0x29    # Beep via speakers (DSP-Amp beep)
    CLIMATE_DRAW = 0x5D    # Climate control drawing
    AUDIO_DRAW = 0x5E      # Audio drawing
    TRIP_DRAW = 0x5F       # Trip info drawing
    TUNER = 0x60           # Tuner
    TAPE = 0x61            # Tape deck
    CD = 0x62              # CD player
    CD_CHANGER = 0x63      # CD changer
    AUDIO_AMP = 0x74       # Audio amplifier (volume, bass, treble, balance, fade)
    CLIMATE = 0xE0         # Climate control device


# ─────────────────────────────────────────────────────────────────────────────
# Button Codes from AVC-LAN (040 → 200 messages)
# ─────────────────────────────────────────────────────────────────────────────

class ButtonCode(IntEnum):
    """
    Known button codes from AVC-LAN button press messages.
    
    Button codes are 16-bit values from bytes 2-3 of 040→200 messages.
    The suffix byte (byte 4) typically indicates the device context:
    - 0x62: CD/Audio mode
    - 0x60: Tuner/Radio mode
    - 0x22: Menu/System
    - 0x02: General button release
    
    These codes are observed patterns and may need refinement
    as more button presses are analyzed.
    """
    # Status/Heartbeat (most frequent - may not be actual button)
    STATUS_6044 = 0x6044       # 60 44 - Status update (CD mode)
    STATUS_6024 = 0x6024       # 60 24 - Status update variant
    
    # Audio buttons
    AUDIO_6184 = 0x6184        # 61 84 - Possibly Track/Disc
    AUDIO_C104 = 0xC104        # C1 04 - Audio mode button
    AUDIO_30A4 = 0x30A4        # 30 A4 - Possibly seek/scan
    
    # Menu/Info buttons
    MENU_0005 = 0x0005         # 00 05 - Menu/Info button
    
    # Unknown (need more analysis)
    UNKNOWN = 0x0000


# Button suffix device contexts
class ButtonContext(IntEnum):
    """Button context suffix byte values."""
    CD_AUDIO = 0x62            # CD/Audio context
    TUNER_RADIO = 0x60         # Tuner/Radio context
    MENU_SYSTEM = 0x22         # Menu/System context
    RELEASE = 0x02             # Button release / general


# Button command types
class ButtonCommandType(IntEnum):
    """Button command type prefix byte."""
    PRESS = 0x28               # Button press
    RELEASE = 0x2A             # Button release


# Known button name mappings
BUTTON_NAMES: dict[int, str] = {
    0x6044: "STATUS",          # Status heartbeat
    0x6024: "STATUS_ALT",
    0x6184: "AUDIO_1",
    0xC104: "AUDIO_2", 
    0x30A4: "SEEK",
    0x0005: "MENU",
}


def get_button_name(code: int) -> str:
    """Get human-readable button name from code."""
    return BUTTON_NAMES.get(code, f"BTN_{code:04X}")


@dataclass(frozen=True)
class DeviceInfo:
    """Information about an AVC-LAN device."""
    
    address: int
    name: str
    device_type: DeviceType
    description: str = ""


# Device address lookup table (12-bit addresses)
# Based on Flerchinger document "AN IN-DEPTH LOOK AT THE TOYOTA AUDIO & VIDEO BUS"
# Note: 0x190 is Audio H/U on Prius, 0x160 is Audio H/U on Corolla
DEVICE_ADDRESSES: dict[int, DeviceInfo] = {
    0x110: DeviceInfo(0x110, "EMV", DeviceType.EMV, "Multi-Function Display (MFD)"),
    0x112: DeviceInfo(0x112, "EMV2", DeviceType.EMV, "MFD Secondary"),
    0x120: DeviceInfo(0x120, "AVX", DeviceType.AUDIO_HU, "Audio/Video System"),
    0x128: DeviceInfo(0x128, "1DIN_TV", DeviceType.EMV, "1-DIN TV"),
    0x140: DeviceInfo(0x140, "AVN", DeviceType.NAVI, "Audio/Video/Navigation"),
    0x144: DeviceInfo(0x144, "GBOOK", DeviceType.NAVI, "G-BOOK"),
    0x160: DeviceInfo(0x160, "AUDIO_HU", DeviceType.AUDIO_HU, "Audio Head Unit (Corolla)"),
    0x178: DeviceInfo(0x178, "NAVI", DeviceType.NAVI, "Navigation ECU"),
    0x17C: DeviceInfo(0x17C, "MONET", DeviceType.NAVI, "MONET"),
    0x17D: DeviceInfo(0x17D, "TEL", DeviceType.UNKNOWN, "Telephone"),
    0x180: DeviceInfo(0x180, "RR_TV", DeviceType.EMV, "Rear TV"),
    0x190: DeviceInfo(0x190, "AUDIO_HU", DeviceType.AUDIO_HU, "Audio Head Unit (Prius)"),
    0x1A0: DeviceInfo(0x1A0, "DVD_P", DeviceType.CD_CHANGER, "DVD Player"),
    0x1AC: DeviceInfo(0x1AC, "CAMERA_C", DeviceType.CAMERA, "Camera Controller"),
    0x1C0: DeviceInfo(0x1C0, "RR_CONT", DeviceType.PANEL, "Rear Controller"),
    0x1C2: DeviceInfo(0x1C2, "TV_TUNER2", DeviceType.TUNER, "TV Tuner 2"),
    0x1C4: DeviceInfo(0x1C4, "PANEL", DeviceType.PANEL, "Control Panel"),
    0x1C6: DeviceInfo(0x1C6, "GW", DeviceType.UNKNOWN, "Gateway ECU"),
    0x1C8: DeviceInfo(0x1C8, "FM_M_LCD", DeviceType.EMV, "FM Multi LCD"),
    0x1CC: DeviceInfo(0x1CC, "ST_WHEEL", DeviceType.STEERING, "Steering Wheel Controls"),
    0x1D6: DeviceInfo(0x1D6, "CLOCK", DeviceType.UNKNOWN, "Clock"),
    0x1D8: DeviceInfo(0x1D8, "CONT_SW", DeviceType.PANEL, "Control Switch (CONT-SW)"),
    0x1EC: DeviceInfo(0x1EC, "BODY", DeviceType.UNKNOWN, "Body ECU"),
    0x1F0: DeviceInfo(0x1F0, "TUNER", DeviceType.TUNER, "Radio Tuner"),
    0x1F1: DeviceInfo(0x1F1, "XM", DeviceType.TUNER, "XM Radio"),
    0x1F2: DeviceInfo(0x1F2, "SIRIUS", DeviceType.TUNER, "Sirius Radio"),
    0x1F4: DeviceInfo(0x1F4, "RSA", DeviceType.UNKNOWN, "RSA"),
    0x1F6: DeviceInfo(0x1F6, "RSE", DeviceType.UNKNOWN, "RSE"),
    0x230: DeviceInfo(0x230, "TV_TUNER", DeviceType.TUNER, "TV Tuner"),
    0x240: DeviceInfo(0x240, "CD_CH2", DeviceType.CD_CHANGER, "CD Changer 2"),
    0x250: DeviceInfo(0x250, "DVD_CH", DeviceType.CD_CHANGER, "DVD Changer"),
    0x280: DeviceInfo(0x280, "CAMERA", DeviceType.CAMERA, "Camera"),
    0x360: DeviceInfo(0x360, "CD_CH1", DeviceType.CD_CHANGER, "CD Changer 1"),
    0x3A0: DeviceInfo(0x3A0, "MD_CH", DeviceType.CD_CHANGER, "MiniDisc Changer"),
    0x440: DeviceInfo(0x440, "DSP_AMP", DeviceType.DSP_AMP, "DSP Amplifier (JBL)"),
    0x480: DeviceInfo(0x480, "AMP", DeviceType.AMP, "Amplifier"),
    0x530: DeviceInfo(0x530, "ETC", DeviceType.UNKNOWN, "ETC"),
    0x5C8: DeviceInfo(0x5C8, "MAYDAY", DeviceType.UNKNOWN, "Mayday System"),
}

# Common address patterns observed in Prius Gen 2 logs
PRIUS_GEN2_ADDRESSES: dict[int, str] = {
    0x002: "System Control",
    0x010: "Power Control",
    0x020: "Power Status",
    0x040: "Button Input",
    0x080: "Status",
    0x088: "Climate Status",
    0x092: "System Status",
    0x100: "Device Query",
    0x10C: "Climate Control",
    0x114: "ECU Data",
    0x182: "ECU Status",
    0x200: "Display/Touch",
    0x218: "Audio Control",
    0x228: "Audio Processing",
    0x258: "System Controller",
    0x310: "HVAC",
    0x400: "Power/Wake",
    0x430: "Climate Buttons",
    0x490: "System Status Sink",
    0x660: "Display Control",
    0x800: "Extended",
    0xA00: "Broadcast Source",
}


class AVCMessageType(Enum):
    """Types of AVC-LAN messages."""
    UNKNOWN = auto()
    BUTTON_PRESS = auto()
    AUDIO_CONTROL = auto()
    CLIMATE_STATUS = auto()


@dataclass
class AVCMessage:
    """Parsed AVC-LAN message from gateway."""
    
    timestamp: int
    sequence: int
    master_addr: int
    slave_addr: int
    control: int
    data: list[int]
    count: int = 1
    
    # Decoded information (populated by decoder)
    master_device: Optional[DeviceInfo] = None
    slave_device: Optional[DeviceInfo] = None
    master_name: str = ""
    slave_name: str = ""
    
    # Extracted values
    msg_type: AVCMessageType = AVCMessageType.UNKNOWN
    values: dict = field(default_factory=dict)
    
    @property
    def is_broadcast(self) -> bool:
        """Check if this is a broadcast message (slave = 0xFFF or 0x1FF)."""
        return self.slave_addr in (0xFFF, 0x1FF)
    
    @property
    def data_length(self) -> int:
        """Return the length of data payload."""
        return len(self.data)
    
    def data_hex(self) -> str:
        """Return data as hex string."""
        return " ".join(f"{b:02X}" for b in self.data)


class AVCDecoder:
    """
    Decoder for AVC-LAN messages.
    
    Parses raw gateway messages and provides decoded information
    including device identification and message classification.
    """
    
    def __init__(self) -> None:
        """Initialize the decoder."""
        self._device_cache: dict[int, DeviceInfo] = {}
    
    def decode_message(self, raw: dict) -> Optional[AVCMessage]:
        """
        Decode a raw gateway message dict into an AVCMessage.
        
        Args:
            raw: Raw message dict - can be either:
                 - Full message: {"id": 2, "d": {"m": "10C", "s": "310", ...}}
                 - Data only: {"m": "10C", "s": "310", ...}
            
        Returns:
            Decoded AVCMessage or None if not an AVC frame
        """
        # Handle both full message format and data-only format
        # Full message has "id" and "d" fields
        if "id" in raw:
            # Full message format
            if raw.get("id") != 2:
                return None
            data = raw.get("d", {})
            timestamp = raw.get("ts", 0)
            sequence = raw.get("seq", 0)
        elif "m" in raw and "s" in raw:
            # Data-only format (already extracted "d" field)
            data = raw
            timestamp = 0
            sequence = 0
        else:
            return None
        
        if not data:
            return None
        
        try:
            master_addr = int(data.get("m", "0"), 16)
            slave_addr = int(data.get("s", "0"), 16)
            control = data.get("c", 0)
            raw_data = [int(b, 16) for b in data.get("d", [])]
            
            msg = AVCMessage(
                timestamp=timestamp,
                sequence=sequence,
                master_addr=master_addr,
                slave_addr=slave_addr,
                control=control,
                data=raw_data,
                count=data.get("cnt", 1),
            )
            
            # Look up device info
            msg.master_device = self._get_device_info(master_addr)
            msg.slave_device = self._get_device_info(slave_addr)
            msg.master_name = self._get_device_name(master_addr)
            msg.slave_name = self._get_device_name(slave_addr)
            
            # -----------------------------------------------------------------
            # Specific Decoding Logic
            # -----------------------------------------------------------------
            
            # Climate Status (A/C Amp 0x130 broadcasting)
            if master_addr == 0x130:
                self._decode_climate_status(msg)
                
            return msg
            
        except (ValueError, KeyError, TypeError):
            return None
    
    def _decode_climate_status(self, msg: AVCMessage) -> None:
        """
        Decode Climate Control status from A/C Amplifier (0x130).
        
        Based on research:
        - Ambient Temp: typically byte 3 or 4, Val-40=C
        - Mode: Bitmasks in status byte
        """
        data = msg.data
        if not data:
            return
            
        msg.msg_type = AVCMessageType.CLIMATE_STATUS
        
        # Heuristics based on "Status Broadcast"
        # We look for a message that is likely the periodic status
        # Example: 80 02 13 00 2c ...
        # If payload starts with 0x80, it might be the status telegram
        
        # Ambient Temp candidate:
        # We'll extract a few bytes that look like temperature (around 50-70 raw for 10-30C)
        # 20C = 60 (0x3C). 
        # If we see a byte in range 40-100 (0-60C), it's a candidate.
        
        # Docs say "Byte 3 or 4". Let's try to extract if length permits.
        if len(data) >= 5:
            # Check byte 3 and 4
            b3 = data[3]
            b4 = data[4]
            
            # Store raw for state to filter/decide
            msg.values["raw_byte_3"] = b3
            msg.values["raw_byte_4"] = b4
            
            # If standard Toyota offset -40C applies:
            if 0 <= (b3 - 40) <= 50:
                msg.values["ambient_temp_c"] = b3 - 40
            elif 0 <= (b4 - 40) <= 50:
                 msg.values["ambient_temp_c"] = b4 - 40
                 
            # AC Modes (Bitmasks)
            # Docs say "second byte" in trace `80 02...` which would be data[1]
            status_byte = data[1]
            msg.values["raw_mode_byte"] = status_byte
            
            msg.values["face"] = bool(status_byte & 0x01)
            msg.values["feet"] = bool(status_byte & 0x02) # Note: Bi-level is 0x02 (Face+Feet). Wait.
            # Docs: Face=0x01, Bi-Level=0x02, Feet=0x03, Mix=0x04, Defrost=0x05
            # These are VALUES, not BITMASKS per se (though they look like it)
            # Actually "Bitmask Analysis" header but then values `0x01`, `0x02`...
            # 0x03 (Feet) has 1 and 2 set? No. 3 is 11 binary.
            # If 0x01 is Face, and 0x02 is Bi-Level...
            # Maybe it is an Enum value.
            msg.values["mode_enum"] = status_byte
            
            # Recirculation
            # "Fresh Air 0x00, Recirculate 0x10" -> Bit 4
            msg.values["recirc"] = bool(status_byte & 0x10)

    def _get_device_info(self, addr: int) -> Optional[DeviceInfo]:
        """Get device info for an address."""
        if addr in self._device_cache:
            return self._device_cache[addr]
        
        info = DEVICE_ADDRESSES.get(addr)
        if info:
            self._device_cache[addr] = info
        return info
    
    def _get_device_name(self, addr: int) -> str:
        """Get a human-readable name for a device address."""
        info = self._get_device_info(addr)
        if info:
            return info.name
        
        # Check Prius-specific addresses
        name = PRIUS_GEN2_ADDRESSES.get(addr)
        if name:
            return name
        
        return f"0x{addr:03X}"
    
    def classify_message(self, msg: AVCMessage) -> str:
        """
        Classify the message type based on patterns.
        
        Returns a string describing the likely message purpose.
        """
        m, s = msg.master_addr, msg.slave_addr
        d = msg.data
        
        # Power/Wake messages
        if m == 0x400 and s == 0x020:
            return "power_status"
        
        # Climate control
        if m == 0x10C and s == 0x310:
            return "climate_state"
        
        # Button press (040 → 200)
        if m == 0x040 and s == 0x200 and len(d) >= 5:
            return "button_press"
        
        # Display control
        if s == 0x660:
            return "display_control"
        
        # System status (→ 490)
        if s == 0x490:
            return "system_status"
        
        # EMV messages
        if m in (0x110, 0x112):
            if s == 0x490:
                return "emv_status"
            elif s == 0x178:
                return "touch_event"
        
        # Touch Screen (000 -> 114, 000 -> 178)
        if m == 0x000 and s in (0x114, 0x178):
            return "touch_event"
        
        # Audio control
        if s == 0x440 or s == 0x480:
            return "audio_control"
        
        # Broadcast
        if msg.is_broadcast:
            return "broadcast"
        
        return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Button Event Parsing
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ButtonEvent:
    """Parsed button press event from AVC-LAN."""
    is_press: bool            # True = press, False = release
    button_code: int          # Combined code (byte2 << 8 | byte3)
    modifier: int             # Modifier byte (byte1)
    suffix: int               # Context suffix (byte4)
    button_name: str          # Human-readable name
    raw_data: list[int]       # Original data bytes


def parse_button_event(data: list[int]) -> Optional[ButtonEvent]:
    """
    Parse a button press/release event from 040 → 200 message data.
    
    Message format (5 bytes):
    - byte[0]: Command type (0x28 = press, 0x2A = release)
    - byte[1]: Modifier (usually 0x00)
    - byte[2]: Button code high byte
    - byte[3]: Button code low byte
    - byte[4]: Context suffix (0x62=CD, 0x60=Radio, etc.)
    
    Args:
        data: Data bytes from the message
        
    Returns:
        ButtonEvent with parsed information, or None if invalid
    """
    if len(data) < 5:
        return None
    
    cmd_type = data[0]
    
    # Check for valid command type
    if cmd_type not in (ButtonCommandType.PRESS, ButtonCommandType.RELEASE):
        return None
    
    is_press = (cmd_type == ButtonCommandType.PRESS)
    modifier = data[1]
    button_code = (data[2] << 8) | data[3]
    suffix = data[4]
    button_name = get_button_name(button_code)
    
    return ButtonEvent(
        is_press=is_press,
        button_code=button_code,
        modifier=modifier,
        suffix=suffix,
        button_name=button_name,
        raw_data=list(data)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Touch Event Parsing
# Based on Flerchinger document: <110> → <178>: [0 21 24 78 xx yy xx yy]
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TouchEvent:
    """
    Parsed touch screen event from AVC-LAN.
    
    Touch events are sent from MFD (0x110) to Navigation ECU (0x178).
    Format: [0 21 24 78 xx yy xx yy]
    
    Coordinates are 8-bit values (0x00-0xFF = 0-255):
    - x: 0 = left edge, 255 = right edge
    - y: 0 = top edge, 255 = bottom edge
    
    The coordinates are repeated twice in the message (bytes 4-5 and 6-7).
    """
    touch_type: str           # "press", "release", "drag", "unknown"
    x: int                    # X coordinate (0-255)
    y: int                    # Y coordinate (0-255)
    normalized_x: float       # X as 0.0-1.0
    normalized_y: float       # Y as 0.0-1.0
    raw_data: list[int]       # Original data bytes
    confidence: str           # "high", "medium", "low"


def parse_touch_event(data: list[int]) -> Optional[TouchEvent]:
    """
    Parse a touch screen event from AVC-LAN message data.
    
    Known touch event formats:
    
    1. Navigation touch command (110 → 178): [00 21 24 78 xx yy xx yy]
       - byte[0]: 0x00 (prefix)
       - byte[1]: 0x21 (switch logic device)
       - byte[2]: 0x24 (SW_CONVERT - coordinate conversion)
       - byte[3]: 0x78 (touch press indicator)
       - byte[4-5]: X,Y coordinates (first pair)
       - byte[6-7]: X,Y coordinates (repeated)
       
    2. Touch controller raw (000 → 114): Variable formats
       - 2 bytes: [xx yy] - raw coordinates or status
       - 3 bytes: [xx yy zz] - coords with status
       - 4+ bytes: Various status/position messages
       - 13 bytes with [01 21] pattern: Likely position data
       
    3. The 0x114 destination is likely the touch panel controller.
       These are lower-level touch events.
    
    Args:
        data: Data bytes from the message
        
    Returns:
        TouchEvent with parsed information, or None if invalid
    """
    if not data:
        return None
    
    raw_data = list(data)
    
    # High confidence: Navigation touch format [00 21 24 78 xx yy xx yy]
    # This is the documented format from Flerchinger for 110→178
    if len(data) >= 8:
        if (data[0] == 0x00 and data[1] == 0x21 and 
            data[2] == 0x24 and data[3] == 0x78):
            x = data[4]
            y = data[5]
            
            return TouchEvent(
                touch_type="press",
                x=x,
                y=y,
                normalized_x=x / 255.0,
                normalized_y=y / 255.0,
                raw_data=raw_data,
                confidence="high"
            )
    
    # 13-byte messages from touch controller (000→114)
    # Two observed patterns:
    # 
    # Pattern A: [XX XX XX 00 00 01 21 XX XX XX 00 00 00]
    #   - bytes[5:7] = 0x01 0x21 → touch position data
    #   - byte[7] = X coordinate, byte[9] = Y coordinate (byte[8] might be status)
    #   - Examples:
    #     - 2C D1 C0 00 00 01 21 92 10 10 00 00 00 → x=0x92=146, y=0x10=16
    #     - 10 34 40 00 00 01 21 90 00 90 00 00 00 → x=0x90=144, y=0x90=144
    #
    # Pattern B: [XX XX XX 00 00 11 24 XX XX XX 00 00 00]
    #   - bytes[5:7] = 0x11 0x24 → possibly status/config message
    #   - Similar to Flerchinger's SW_CONVERT (0x24) command
    #
    if len(data) == 13:
        if data[5] == 0x01 and data[6] == 0x21:
            # Touch position message
            x = data[7]
            y = data[9]  # Y appears to be at byte 9, not 8
            return TouchEvent(
                touch_type="press",
                x=x,
                y=y,
                normalized_x=x / 255.0,
                normalized_y=y / 255.0,
                raw_data=raw_data,
                confidence="medium"
            )
        elif data[5] == 0x11 and data[6] == 0x24:
            # Status/config message - not a touch position
            return TouchEvent(
                touch_type="status",
                x=0,
                y=0,
                normalized_x=0.0,
                normalized_y=0.0,
                raw_data=raw_data,
                confidence="low"
            )
    
    # 9-byte messages - likely status/config similar to 13-byte Pattern B
    if len(data) == 9:
        if data[5] == 0x11 and data[6] == 0x24:
            return TouchEvent(
                touch_type="status",
                x=0,
                y=0,
                normalized_x=0.0,
                normalized_y=0.0,
                raw_data=raw_data,
                confidence="low"
            )
        # Fallback: try first two bytes as coords
        return TouchEvent(
            touch_type="unknown",
            x=data[0],
            y=data[1],
            normalized_x=data[0] / 255.0,
            normalized_y=data[1] / 255.0,
            raw_data=raw_data,
            confidence="low"
        )
    
    # 8-byte messages - may have coordinates at positions 5-6
    if len(data) == 8:
        # Pattern: [XX XX XX 00 00 XX 00 00]
        # Position might be at byte 5
        x = data[5] if data[5] != 0 else data[0]
        y = data[6] if len(data) > 6 else 0
        
        return TouchEvent(
            touch_type="press",
            x=x,
            y=y,
            normalized_x=x / 255.0,
            normalized_y=y / 255.0,
            raw_data=raw_data,
            confidence="low"
        )
    
    # 5-byte messages
    if len(data) == 5:
        # First two bytes often look like coordinates
        x = data[0]
        y = data[1]
        return TouchEvent(
            touch_type="press",
            x=x,
            y=y,
            normalized_x=x / 255.0,
            normalized_y=y / 255.0,
            raw_data=raw_data,
            confidence="low"
        )
    
    # 4-byte messages
    if len(data) == 4:
        # [XX XX XX 00] pattern - first two bytes are likely position
        x = data[0]
        y = data[1]
        return TouchEvent(
            touch_type="unknown",
            x=x,
            y=y,
            normalized_x=x / 255.0,
            normalized_y=y / 255.0,
            raw_data=raw_data,
            confidence="low"
        )
    
    # 3-byte messages
    if len(data) == 3:
        x = data[0]
        y = data[1]
        return TouchEvent(
            touch_type="unknown",
            x=x,
            y=y,
            normalized_x=x / 255.0,
            normalized_y=y / 255.0,
            raw_data=raw_data,
            confidence="low"
        )
    
    # 2-byte messages - likely raw coordinate pair
    if len(data) == 2:
        return TouchEvent(
            touch_type="tap",
            x=data[0],
            y=data[1],
            normalized_x=data[0] / 255.0,
            normalized_y=data[1] / 255.0,
            raw_data=raw_data,
            confidence="medium"  # 2-byte is often coordinates
        )
    
    # 1-byte or empty
    return TouchEvent(
        touch_type="unknown",
        x=data[0] if len(data) > 0 else 0,
        y=0,
        normalized_x=(data[0] if len(data) > 0 else 0) / 255.0,
        normalized_y=0.0,
        raw_data=raw_data,
        confidence="low"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Audio Status Parsing
# Based on Flerchinger: audio commands use [00 25 74 XX YY] format
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AudioParamStatus:
    """Parsed audio parameter status from AVC-LAN."""
    param_type: str           # "volume", "bass", "treble", "balance", "fade", "mid"
    raw_value: int            # Raw protocol value
    display_value: int        # User-friendly value
    raw_data: list[int]


def parse_audio_status(data: list[int]) -> Optional[AudioParamStatus]:
    """
    Extract audio parameter from status messages.
    
    Audio param format: [00 25 74 XX YY]
    Where XX is param code and YY is value.
    
    Param codes:
    - 0x91: Balance (0x09-0x17, center=0x10) → display: -7 to +7
    - 0x92: Fade (0x09-0x17, center=0x10) → display: -7 to +7  
    - 0x93: Bass (0x0B-0x15, center=0x10) → display: -5 to +5
    - 0x94: Mid (0x0B-0x15, center=0x10) → display: -5 to +5
    - 0x95: Treble (0x0B-0x15, center=0x10) → display: -5 to +5
    - 0x9C/0x9D: Volume up/down step
    
    Returns:
        AudioParamStatus or None
    """
    # Look for audio param pattern
    for i in range(len(data) - 4):
        if data[i] == 0x00 and data[i+1] == 0x25 and data[i+2] == 0x74:
            param_code = data[i+3]
            value = data[i+4] if i+4 < len(data) else 0
            
            if param_code == 0x91:
                return AudioParamStatus(
                    param_type="balance",
                    raw_value=value,
                    display_value=value - 0x10,  # Convert to -7..+7
                    raw_data=list(data)
                )
            elif param_code == 0x92:
                return AudioParamStatus(
                    param_type="fade",
                    raw_value=value,
                    display_value=value - 0x10,
                    raw_data=list(data)
                )
            elif param_code == 0x93:
                return AudioParamStatus(
                    param_type="bass",
                    raw_value=value,
                    display_value=value - 0x10,
                    raw_data=list(data)
                )
            elif param_code == 0x94:
                return AudioParamStatus(
                    param_type="mid",
                    raw_value=value,
                    display_value=value - 0x10,
                    raw_data=list(data)
                )
            elif param_code == 0x95:
                return AudioParamStatus(
                    param_type="treble",
                    raw_value=value,
                    display_value=value - 0x10,
                    raw_data=list(data)
                )
    
    return None


def parse_volume_status(data: list[int]) -> Optional[int]:
    """
    Extract volume level from status messages.
    
    Volume messages often contain 0x74 0x31 0xF1 0x90 [volume] pattern
    or other volume indicators.
    
    Returns:
        Volume level (0-255) or None
    """
    # Look for volume pattern in data
    for i in range(len(data) - 1):
        if data[i] == 0x90 and i + 1 < len(data):
            return data[i + 1]
    return None


def parse_climate_state(data: list[int]) -> Optional[dict]:
    """
    Parse climate control state from 10C → 310 messages.
    
    Args:
        data: Data bytes from the message
        
    Returns:
        Dict with climate state or None
    """
    if len(data) < 8:
        return None
    
    return {
        "raw": data,
        "byte4": data[4] if len(data) > 4 else 0,
        "byte5": data[5] if len(data) > 5 else 0,
        "byte6": data[6] if len(data) > 6 else 0,
        "byte7": data[7] if len(data) > 7 else 0,
    }
