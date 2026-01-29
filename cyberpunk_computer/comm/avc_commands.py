"""
AVC-LAN Command Generator.

Generates AVC-LAN commands for sending to the vehicle via gateway.
Based on Toyota Prius Gen 2 AVC-LAN protocol analysis.

Reference: Flerchinger, J.J. "AN IN-DEPTH LOOK AT THE TOYOTA AUDIO & VIDEO BUS (AVC-LAN)" 2006

Message format: <broadcast> <master> <slave> <control> <data_length> <data>

Known Prius commands (from Flerchinger document):
- Beep:         <1> <110> <440> <F> <5> <0 5E 29 60 dd>          (dd=1-4 duration)
- Touch press:  <1> <110> <178> <F> <8> <0 21 24 78 xx yy xx yy> (xx,yy=0-FF)
- Balance:      <1> <190> <440> <F> <5> <00 25 74 91 bl>         (bl=09-17, center=10)
- Fade:         <1> <190> <440> <F> <5> <00 25 74 92 fd>         (fd=09-17, center=10)
- Bass:         <1> <190> <440> <F> <5> <00 25 74 93 bs>         (bs=0B-15, center=10)
- Mid:          <1> <190> <440> <F> <5> <00 25 74 94 md>         (md=0B-15, center=10)
- Treble:       <1> <190> <440> <F> <5> <00 25 74 95 tb>         (tb=0B-15, center=10)
- Volume up:    <1> <190> <440> <F> <5> <00 25 74 9C vu>         (vu=01-04 step)
- Volume down:  <1> <190> <440> <F> <5> <00 25 74 9D vd>         (vd=01-04 step)
"""

from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from enum import IntEnum
import logging

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Audio Parameter Constants (from Flerchinger document)
# ─────────────────────────────────────────────────────────────────────────────

class AudioParamCode(IntEnum):
    """Audio parameter command codes for DSP-Amp (0x440)."""
    BALANCE = 0x91      # Left (0x09) to Right (0x17), center = 0x10
    FADE = 0x92         # Front (0x09) to Rear (0x17), center = 0x10
    BASS = 0x93         # Min (0x0B) to Max (0x15), center = 0x10
    MID = 0x94          # Min (0x0B) to Max (0x15), center = 0x10
    TREBLE = 0x95       # Min (0x0B) to Max (0x15), center = 0x10
    VOLUME_UP = 0x9C    # Step value 0x01-0x04
    VOLUME_DOWN = 0x9D  # Step value 0x01-0x04


# Audio parameter value ranges
BALANCE_MIN = 0x09      # Full left
BALANCE_MAX = 0x17      # Full right
BALANCE_CENTER = 0x10   # Center

FADE_MIN = 0x09         # Full front
FADE_MAX = 0x17         # Full rear
FADE_CENTER = 0x10      # Center

# Bass, Mid, Treble use same range
TONE_MIN = 0x0B         # -5
TONE_MAX = 0x15         # +5
TONE_CENTER = 0x10      # 0 (flat)

VOLUME_STEP_MIN = 0x01
VOLUME_STEP_MAX = 0x04


class AudioCommand(IntEnum):
    """Audio control command codes (legacy, for button simulation)."""
    VOLUME_UP = 0x01
    VOLUME_DOWN = 0x02
    MUTE_TOGGLE = 0x03
    SOURCE_NEXT = 0x04
    SOURCE_PREV = 0x05


class ClimateCommand(IntEnum):
    """Climate control command codes."""
    TEMP_UP = 0x01
    TEMP_DOWN = 0x02
    FAN_UP = 0x03
    FAN_DOWN = 0x04
    AC_TOGGLE = 0x05
    AUTO_TOGGLE = 0x06
    RECIRC_TOGGLE = 0x07


@dataclass
class AVCLANCommand:
    """AVC-LAN command to send to gateway."""
    master: int          # Master device address (12-bit)
    slave: int           # Slave device address (12-bit)
    control: int         # Control byte (0x0F for data commands)
    data: List[int]      # Data bytes
    
    def to_gateway_format(self) -> dict:
        """
        Convert to gateway NDJSON format.
        
        Format per PROTOCOL.md:
        {"id": 2, "d": {"m": "190", "s": "440", "c": 15, "d": ["00", "25", ...]}}
        
        Returns:
            Dict ready for JSON serialization
        """
        return {
            "id": 2,  # AVC-LAN device ID
            "d": {
                "m": f"{self.master:03X}",  # Hex string, 3 digits
                "s": f"{self.slave:03X}",   # Hex string, 3 digits
                "c": self.control,
                "d": [f"{b:02X}" for b in self.data]  # Hex strings
            }
        }


class AVCCommandGenerator:
    """
    Generates AVC-LAN commands for vehicle control.
    
    Device addresses (from Flerchinger document):
    - 0x110: EMV / Multi-Function Display
    - 0x178: Navigation ECU
    - 0x190: Audio Head Unit (Prius - NOT 0x160 which is Corolla)
    - 0x440: DSP Amplifier (JBL) - receives audio control commands
    - 0x480: Standard Amplifier
    
    Control byte:
    - 0x0F (15): Standard data command with 5+ bytes
    
    Audio commands to DSP-Amp use format:
    <190> -> <440>: [00 25 74 XX YY]
    Where XX is the parameter code and YY is the value.
    """
    
    # Device addresses (Prius Gen 2)
    ADDR_EMV = 0x110              # Multi-Function Display
    ADDR_NAVI = 0x178             # Navigation ECU
    ADDR_AUDIO_HU = 0x190         # Audio Head Unit (Prius)
    ADDR_AUDIO_HU_COROLLA = 0x160 # Audio Head Unit (Corolla)
    ADDR_DSP_AMP = 0x440          # DSP Amplifier (JBL)
    ADDR_AMP = 0x480              # Standard Amplifier
    ADDR_CLIMATE = 0x310          # Climate controller
    ADDR_BROADCAST = 0xFFF        # Broadcast address
    
    # Control bytes
    CTRL_DATA_CMD = 0x0F          # Data command (5+ bytes)
    CTRL_COMMAND = 0x01           # Simple command
    CTRL_REQUEST = 0x00           # Request
    CTRL_RESPONSE = 0x03          # Response
    
    # Logic device prefixes (for data field)
    LOGIC_AUDIO_DRAW = 0x5E       # Audio drawing
    LOGIC_BEEP = 0x29             # Beep via speaker
    LOGIC_SW_CONVERT = 0x24       # Touch coordinate conversion
    LOGIC_CMD_SWITCH = 0x25       # Command switch (audio params)
    LOGIC_AUDIO_AMP = 0x74        # Audio amplifier parameters
    
    def __init__(self):
        """Initialize command generator."""
        self._sequence = 0
        
    def _next_seq(self) -> int:
        """Get next sequence number."""
        self._sequence = (self._sequence + 1) & 0xFF
        return self._sequence
    
    # ─────────────────────────────────────────────────────────────────────────
    # Audio Commands (DSP-Amp control via 190 → 440)
    # Based on Flerchinger document format: [00 25 74 XX YY]
    # ─────────────────────────────────────────────────────────────────────────
    
    def _audio_param_command(self, param_code: int, value: int) -> AVCLANCommand:
        """
        Generate an audio parameter command to DSP-Amp.
        
        Format: <190> → <440>: [00 25 74 param value]
        
        Args:
            param_code: AudioParamCode value (0x91-0x9D)
            value: Parameter value
        """
        return AVCLANCommand(
            master=self.ADDR_AUDIO_HU,      # 0x190 for Prius
            slave=self.ADDR_DSP_AMP,        # 0x440 (JBL DSP-Amp)
            control=self.CTRL_DATA_CMD,     # 0x0F
            data=[0x00, self.LOGIC_CMD_SWITCH, self.LOGIC_AUDIO_AMP, 
                  param_code, value]
        )
    
    def volume_up(self, step: int = 1) -> AVCLANCommand:
        """
        Generate volume up command.
        
        Format: <190> → <440>: [00 25 74 9C step]
        
        Args:
            step: Volume step (1-4, higher = faster increase)
        """
        step = max(VOLUME_STEP_MIN, min(VOLUME_STEP_MAX, step))
        return self._audio_param_command(AudioParamCode.VOLUME_UP, step)
    
    def volume_down(self, step: int = 1) -> AVCLANCommand:
        """
        Generate volume down command.
        
        Format: <190> → <440>: [00 25 74 9D step]
        
        Args:
            step: Volume step (1-4, higher = faster decrease)
        """
        step = max(VOLUME_STEP_MIN, min(VOLUME_STEP_MAX, step))
        return self._audio_param_command(AudioParamCode.VOLUME_DOWN, step)
    
    def set_volume(self, level: int) -> AVCLANCommand:
        """
        Generate direct volume set command.
        
        Note: Direct volume set may not be supported - use volume_up/volume_down
        for incremental changes. This is a best-effort implementation.
        
        Args:
            level: Volume level (0-63)
        """
        level = max(0, min(63, level))
        # Try direct volume set - may need button simulation instead
        return AVCLANCommand(
            master=self.ADDR_AUDIO_HU,
            slave=self.ADDR_DSP_AMP,
            control=self.CTRL_DATA_CMD,
            data=[0x00, self.LOGIC_CMD_SWITCH, self.LOGIC_AUDIO_AMP, 0x90, level]
        )
    
    def mute_toggle(self) -> AVCLANCommand:
        """Generate mute toggle command."""
        # Mute may use a different command structure
        return AVCLANCommand(
            master=self.ADDR_AUDIO_HU,
            slave=self.ADDR_DSP_AMP,
            control=self.CTRL_DATA_CMD,
            data=[0x00, self.LOGIC_CMD_SWITCH, self.LOGIC_AUDIO_AMP, 0x9E, 0x01]
        )
    
    def set_bass(self, level: int) -> AVCLANCommand:
        """
        Generate bass level command.
        
        Format: <190> → <440>: [00 25 74 93 value]
        
        Args:
            level: Bass level (-5 to +5)
            
        Value encoding:
            -5 = 0x0B, -4 = 0x0C, ..., 0 = 0x10, ..., +4 = 0x14, +5 = 0x15
        """
        # Convert from -5..+5 to 0x0B..0x15
        value = TONE_CENTER + level
        value = max(TONE_MIN, min(TONE_MAX, value))
        return self._audio_param_command(AudioParamCode.BASS, value)
    
    def set_mid(self, level: int) -> AVCLANCommand:
        """
        Generate mid level command.
        
        Format: <190> → <440>: [00 25 74 94 value]
        
        Args:
            level: Mid level (-5 to +5)
        """
        value = TONE_CENTER + level
        value = max(TONE_MIN, min(TONE_MAX, value))
        return self._audio_param_command(AudioParamCode.MID, value)
    
    def set_treble(self, level: int) -> AVCLANCommand:
        """
        Generate treble level command.
        
        Format: <190> → <440>: [00 25 74 95 value]
        
        Args:
            level: Treble level (-5 to +5)
        """
        value = TONE_CENTER + level
        value = max(TONE_MIN, min(TONE_MAX, value))
        return self._audio_param_command(AudioParamCode.TREBLE, value)
    
    def set_balance(self, level: int) -> AVCLANCommand:
        """
        Generate balance command.
        
        Format: <190> → <440>: [00 25 74 91 value]
        
        Args:
            level: Balance (-7 = full left, +7 = full right, 0 = center)
            
        Value encoding:
            -7 = 0x09 (left), 0 = 0x10 (center), +7 = 0x17 (right)
        """
        value = BALANCE_CENTER + level
        value = max(BALANCE_MIN, min(BALANCE_MAX, value))
        return self._audio_param_command(AudioParamCode.BALANCE, value)
    
    def set_fader(self, level: int) -> AVCLANCommand:
        """
        Generate fader command.
        
        Format: <190> → <440>: [00 25 74 92 value]
        
        Args:
            level: Fader (-7 = front, +7 = rear, 0 = center)
            
        Value encoding:
            -7 = 0x09 (front), 0 = 0x10 (center), +7 = 0x17 (rear)
        """
        value = FADE_CENTER + level
        value = max(FADE_MIN, min(FADE_MAX, value))
        return self._audio_param_command(AudioParamCode.FADE, value)
    # ─────────────────────────────────────────────────────────────────────────
    # Climate Commands (MFD 110 → A/C 130)
    # ─────────────────────────────────────────────────────────────────────────

    def set_target_temp(self, temp_c: float) -> AVCLANCommand:
        """
        Generate set temperature command.
        
        Master: 110 (MFD), Slave: 130 (A/C Amp)
        
        Encoding according to docs:
        LO = 0x00
        18°C (65°F) = 0x22
        Increments 1 hex per 1°F step.
        HI = 0x37 or 0xFF
        
        Args:
            temp_c: Target temperature in Celsius
        """
        if temp_c < 18.0:
            # LO
            hex_val = 0x00
        elif temp_c > 30.0:
             # HI (approx > 85F)
             hex_val = 0x37 # or 0xFF
        else:
            # Calculate offset from 18C (65F)
            # 18C = 65F = 0x22 (34)
            # 1F step = 0.55C step
            
            # Using F logic:
            # temp_f = temp_c * 1.8 + 32
            # delta_f = temp_f - 65
            # hex = 0x22 + delta_f
            
            temp_f = temp_c * 1.8 + 32
            delta_f = temp_f - 65
            hex_val = 0x22 + int(round(delta_f))
            
            # Clamp
            hex_val = max(0x10, min(0x36, hex_val))

        # Command structure derived from docs: 110 130 00 03 <val> ??
        # Or standard data packet:
        # 110 130 0F ...
        # Example in docs: 110 130 00 03 29 (Where 29 is 72F/22C)
        # 00 = control? 03 = length?
        # Wait, standard AVC-LAN header has control (4-bit) and length (8-bit) separate.
        # "110 130 00 03 29"
        # Master=110, Slave=130, Control=0 (Direct Command?), Length=something
        
        # If we use strict AVC-LAN structure: 
        # Control 0x00 might be specific type.
        
        # Let's assume standard structure:
        # Control 0 is likely "Direct Command" or similar.
        # But our `AVCLANCommand` builds the `d` object which includes `c` (control).
        
        return AVCLANCommand(
            master=self.ADDR_EMV,      # 0x110
            slave=0x130,               # A/C Amp
            control=0x00,              # Based on docs example "110 130 00 ..."
            data=[0x03, hex_val]       # "03" might be opcode for Temp Set, followed by value
        )
    
    # ─────────────────────────────────────────────────────────────────────────
    # Beep Commands (MFD → DSP-Amp)
    # Format: <110> → <440>: [0 5E 29 60 duration]
    # ─────────────────────────────────────────────────────────────────────────
    
    def beep(self, duration: int = 1) -> AVCLANCommand:
        """
        Generate beep command.
        
        Format: <110> → <440>: [0 5E 29 60 dd]
        
        Args:
            duration: Beep duration (1-4, longer = more beeps/longer duration)
        """
        duration = max(1, min(4, duration))
        return AVCLANCommand(
            master=self.ADDR_EMV,           # 0x110
            slave=self.ADDR_DSP_AMP,        # 0x440
            control=self.CTRL_DATA_CMD,     # 0x0F
            data=[0x00, self.LOGIC_AUDIO_DRAW, self.LOGIC_BEEP, 0x60, duration]
        )
    
    # ─────────────────────────────────────────────────────────────────────────
    # Touch/Navigation Commands (MFD → Navigation ECU)
    # Format: <110> → <178>: [0 21 24 78 xx yy xx yy]
    # ─────────────────────────────────────────────────────────────────────────
    
    def touch_press(self, x: int, y: int) -> AVCLANCommand:
        """
        Generate touch press command to navigation.
        
        Format: <110> → <178>: [0 21 24 78 xx yy xx yy]
        
        Args:
            x: X coordinate (0-255, 0=left, 255=right)
            y: Y coordinate (0-255, 0=top, 255=bottom)
        """
        x = max(0, min(255, x))
        y = max(0, min(255, y))
        return AVCLANCommand(
            master=self.ADDR_EMV,           # 0x110
            slave=self.ADDR_NAVI,           # 0x178
            control=self.CTRL_DATA_CMD,     # 0x0F
            # Format: 0 21 24 78 xx yy xx yy (coordinates repeated)
            data=[0x00, 0x21, self.LOGIC_SW_CONVERT, 0x78, x, y, x, y]
        )
    # ─────────────────────────────────────────────────────────────────────────
    # Climate Commands
    # ─────────────────────────────────────────────────────────────────────────
    
    def climate_temp_up(self) -> AVCLANCommand:
        """Generate temperature up command."""
        return AVCLANCommand(
            master=self.ADDR_EMV,
            slave=self.ADDR_CLIMATE,
            control=self.CTRL_COMMAND,
            data=[0x28, 0x00, 0x10, 0x01, 0x62]  # Temp up button
        )
    
    def climate_temp_down(self) -> AVCLANCommand:
        """Generate temperature down command."""
        return AVCLANCommand(
            master=self.ADDR_EMV,
            slave=self.ADDR_CLIMATE,
            control=self.CTRL_COMMAND,
            data=[0x28, 0x00, 0x10, 0x02, 0x62]  # Temp down button
        )
    
    def set_target_temp(self, temp_c: float) -> AVCLANCommand:
        """
        Generate set temperature command.
        
        Master: 110 (MFD), Slave: 130 (A/C Amp)
        
        Encoding according to docs:
        LO = 0x00
        18°C (65°F) = 0x22
        Increments 1 hex per 1°F step.
        HI = 0x37 or 0xFF
        
        Args:
            temp_c: Target temperature in Celsius
        """
        if temp_c < 18.0:
            # LO
            hex_val = 0x00
        elif temp_c > 30.0:
             # HI (approx > 85F)
             hex_val = 0x37 # or 0xFF
        else:
            # Calculate offset from 18C (65F)
            # 18C = 65F = 0x22 (34)
            # 1F step = 0.55C step
            
            # Using F logic:
            # temp_f = temp_c * 1.8 + 32
            # delta_f = temp_f - 65
            # hex = 0x22 + int(round(delta_f))
            
            temp_f = temp_c * 1.8 + 32
            delta_f = temp_f - 65
            hex_val = 0x22 + int(round(delta_f))
            
            # Clamp
            hex_val = max(0x10, min(0x36, hex_val))

        # Command structure derived from docs: 110 130 00 03 <val> ??
        
        return AVCLANCommand(
            master=self.ADDR_EMV,      # 0x110
            slave=0x130,               # A/C Amp
            control=0x00,              # Based on docs example "110 130 00 ..."
            data=[0x03, hex_val]       # "03" might be opcode for Temp Set, followed by value
        )
    
    def set_fan_speed(self, speed: int) -> AVCLANCommand:
        """
        Generate fan speed command.
        
        Args:
            speed: Fan speed (0-7)
        """
        speed = max(0, min(7, speed))
        return AVCLANCommand(
            master=self.ADDR_EMV,
            slave=self.ADDR_CLIMATE,
            control=self.CTRL_COMMAND,
            data=[0x22, speed]  # Fan speed set
        )
    
    def climate_ac_toggle(self) -> AVCLANCommand:
        """Generate A/C toggle command."""
        return AVCLANCommand(
            master=0x040,
            slave=self.ADDR_CLIMATE,
            control=self.CTRL_COMMAND,
            data=[0x28, 0x00, 0x10, 0x05, 0x62]  # A/C button
        )
    
    def climate_auto_toggle(self) -> AVCLANCommand:
        """Generate AUTO mode toggle command."""
        return AVCLANCommand(
            master=0x040,
            slave=self.ADDR_CLIMATE,
            control=self.CTRL_COMMAND,
            data=[0x28, 0x00, 0x10, 0x06, 0x62]  # AUTO button
        )
    
    def climate_recirc_toggle(self) -> AVCLANCommand:
        """Generate recirculation toggle command."""
        return AVCLANCommand(
            master=0x040,
            slave=self.ADDR_CLIMATE,
            control=self.CTRL_COMMAND,
            data=[0x28, 0x00, 0x10, 0x07, 0x62]  # Recirc button
        )
    
    def set_air_direction(self, direction: int) -> AVCLANCommand:
        """
        Generate air direction command.
        
        Args:
            direction: 0=FACE, 1=FACE+FEET, 2=FEET, 3=DEFROST
        """
        direction = max(0, min(3, direction))
        return AVCLANCommand(
            master=self.ADDR_EMV,
            slave=self.ADDR_CLIMATE,
            control=self.CTRL_COMMAND,
            data=[0x23, direction]  # Air direction set
        )
    
    # ─────────────────────────────────────────────────────────────────────────
    # Display Commands
    # ─────────────────────────────────────────────────────────────────────────
    
    def request_status(self) -> AVCLANCommand:
        """Request current status from vehicle."""
        return AVCLANCommand(
            master=self.ADDR_EMV,
            slave=self.ADDR_STATUS,
            control=self.CTRL_REQUEST,
            data=[0x00]  # Status request
        )


class CommandQueue:
    """
    Queue for outgoing AVC-LAN commands.
    
    Handles rate limiting and command prioritization.
    """
    
    def __init__(self, min_interval_ms: int = 50):
        """
        Initialize command queue.
        
        Args:
            min_interval_ms: Minimum interval between commands
        """
        self._queue: List[AVCLANCommand] = []
        self._min_interval = min_interval_ms / 1000.0
        self._last_send_time = 0.0
        
    def enqueue(self, command: AVCLANCommand, priority: int = 0) -> None:
        """
        Add command to queue.
        
        Args:
            command: Command to send
            priority: Higher priority = sent first
        """
        self._queue.append((priority, command))
        self._queue.sort(key=lambda x: -x[0])  # Sort by priority descending
        
    def get_next(self, current_time: float) -> Optional[AVCLANCommand]:
        """
        Get next command to send if timing allows.
        
        Args:
            current_time: Current time (from time.time())
            
        Returns:
            Command to send, or None if queue empty or rate limited
        """
        if not self._queue:
            return None
            
        if current_time - self._last_send_time < self._min_interval:
            return None
            
        self._last_send_time = current_time
        _, command = self._queue.pop(0)
        return command
        
    def clear(self) -> None:
        """Clear all pending commands."""
        self._queue.clear()
        
    @property
    def pending_count(self) -> int:
        """Get number of pending commands."""
        return len(self._queue)
