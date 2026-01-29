# AVC-LAN Protocol Analysis - Prius Gen 2

## Overview

AVC-LAN (Audio Visual Communication - Local Area Network) is Toyota's implementation of the IEBus (NEC Inter Equipment Bus) protocol. It's used for communication between various audio/visual and control devices in the vehicle.

**Reference:** Flerchinger, J.J. "AN IN-DEPTH LOOK AT THE TOYOTA AUDIO & VIDEO BUS (AVC-LAN)" 31-JULY-2006

### Key Characteristics
- **Speed:** 17.8 kbps (IEBus mode 2)
- **Data per command:** 0-32 bytes
- **Physical layer:** Shielded twisted pair wire
- **Voltage levels:** -0.5V to 6.0V
- **Logic 1:** Voltage differential ≤ 20mV
- **Logic 0:** Voltage differential ≥ 120mV
- **Termination:** 120 ohm resistors at bus ends
- **Device protection:** 180 ohm series resistors

### Message Format
Each message contains:
1. **Header:** Start bit + broadcast bit
2. **Master Address:** 12 bits + parity bit
3. **Slave Address:** 12 bits + parity bit + ACK bit
4. **Control Field:** 4 bits + parity + ACK
5. **Data Length:** 8 bits + parity + ACK (0x00=16 bytes, 0x01-0x0F=1-15 bytes)
6. **Data Fields:** 8 bits each + parity + ACK

## Gateway Data Format (NDJSON)

The gateway provides processed AVC-LAN messages in the following format:

```json
{
  "id": 2,           // Message type (2 = AVC-LAN frame)
  "ts": 311192,      // Timestamp in milliseconds
  "seq": 0,          // Sequence number
  "d": {
    "m": "080",      // Master device address (hex, 12-bit)
    "s": "BE3",      // Slave device address (hex, 12-bit)  
    "c": 7,          // Control field
    "d": ["00", ...],// Data bytes (hex strings)
    "cnt": 1         // Message count (for duplicates)
  }
}
```

### Special Message Types

- `id: 0` - Gateway status messages (e.g., `GATEWAY_READY`)
- `id: 2` - AVC-LAN frames

## Known Device Addresses

Based on Flerchinger document and log analysis:

| Address | Device Name          | Description                           |
|---------|---------------------|---------------------------------------|
| 110     | EMV / MFD           | Multi-Function Display               |
| 112     | EMV (secondary)     | Multi-Display alternate              |
| 120     | AVX                 | Audio/Video system                   |
| 128     | 1DIN_TV             | 1-DIN TV                             |
| 140     | AVN                 | Audio/Video/Navigation               |
| 144     | G-Book              | G-BOOK Navigation                    |
| 160     | AUDIO H/U           | Audio Head Unit (Corolla)            |
| 178     | NAVI                | Navigation ECU                       |
| 17C     | MONET               | MONET Navigation                     |
| 17D     | TEL                 | Telephone                            |
| 180     | RR-TV               | Rear TV                              |
| 190     | AUDIO H/U           | Audio Head Unit (Prius - JBL system) |
| 1A0     | DVD-P               | DVD Player                           |
| 1AC     | CAMERA-C            | Camera Controller                    |
| 1C0     | RR-CONT             | Rear Controller                      |
| 1C2     | TV-TUNER2           | TV Tuner 2                           |
| 1C4     | PANEL               | Control Panel                        |
| 1C6     | G/W                 | Gateway ECU                          |
| 1C8     | FM-M-LCD            | FM Multi LCD                         |
| 1CC     | ST.WHEEL            | Steering Wheel Controls              |
| 1D6     | CLOCK               | Clock                                |
| 1D8     | CONT-SW             | Control Switch                       |
| 1EC     | BODY                | Body ECU                             |
| 1F0     | RADIO TUNER         | Radio Tuner                          |
| 1F1     | XM                  | XM Radio                             |
| 1F2     | SIRIUS              | Sirius Radio                         |
| 1F4     | RSA                 | RSA                                  |
| 1F6     | RSE                 | RSE                                  |
| 230     | TV-TUNER            | TV Tuner                             |
| 240     | CD-CH2              | CD Changer 2                         |
| 250     | DVD-CH              | DVD Changer                          |
| 280     | CAMERA              | Backup Camera                        |
| 360     | CD-CH1              | CD Changer 1                         |
| 3A0     | MD-CH               | MiniDisc Changer                     |
| 440     | DSP-AMP             | DSP Amplifier (JBL)                  |
| 480     | AMP                 | Standard Amplifier                   |
| 530     | ETC                 | ETC                                  |
| 5C8     | MAYDAY              | Mayday Emergency System              |
| FFF     | BROADCAST           | Broadcast address                    |

**Note:** On Prius with JBL audio, the Audio H/U address is `0x190`, which sends commands to DSP-AMP at `0x440`. On Corolla, the Audio H/U is at `0x160`.

## Documented AVC-LAN Commands

Based on Flerchinger document (2004-2005 Prius):

### Touch Screen Press
Send touch coordinates to Navigation ECU:
```
Master: 110 (MFD)
Slave:  178 (Navigation)
Control: 0x0F
Data:   [00 21 24 78 XX YY XX YY]

XX = X coordinate (0x00=left, 0xFF=right)
YY = Y coordinate (0x00=top, 0xFF=bottom)
Coordinates are repeated twice
```

### Beep Command
Request speaker beep from DSP-Amp:
```
Master: 110 (MFD)
Slave:  440 (DSP-Amp)
Control: 0x0F
Data:   [00 5E 29 60 DD]

DD = Duration (1-4, higher = longer beep)
```

### Audio Parameter Commands
Audio H/U (0x190) sends to DSP-Amp (0x440):
```
Control: 0x0F
Data:   [00 25 74 PP VV]

PP = Parameter code:
  - 0x91: Balance
  - 0x92: Fade
  - 0x93: Bass
  - 0x94: Mid
  - 0x95: Treble
  - 0x9C: Volume Up (VV = step 1-4)
  - 0x9D: Volume Down (VV = step 1-4)

VV = Value:
  - Balance/Fade: 0x09 (left/front) to 0x17 (right/rear), center=0x10
  - Bass/Mid/Treble: 0x0B (min) to 0x15 (max), center=0x10
  - Volume step: 0x01 (slow) to 0x04 (fast)
```

### Value Encoding Tables

#### Balance (0x91)
| Display | Protocol Value |
|---------|---------------|
| L7 (left)  | 0x09 |
| L6      | 0x0A |
| L5      | 0x0B |
| L4      | 0x0C |
| L3      | 0x0D |
| L2      | 0x0E |
| L1      | 0x0F |
| C (center) | 0x10 |
| R1      | 0x11 |
| R2      | 0x12 |
| R3      | 0x13 |
| R4      | 0x14 |
| R5      | 0x15 |
| R6      | 0x16 |
| R7 (right) | 0x17 |

#### Fade (0x92)
| Display | Protocol Value |
|---------|---------------|
| F7 (front) | 0x09 |
| ... | ... |
| C (center) | 0x10 |
| ... | ... |
| R7 (rear)  | 0x17 |

#### Bass/Mid/Treble (0x93, 0x94, 0x95)
| Display | Protocol Value |
|---------|---------------|
| -5 (min) | 0x0B |
| -4      | 0x0C |
| -3      | 0x0D |
| -2      | 0x0E |
| -1      | 0x0F |
| 0 (flat)| 0x10 |
| +1      | 0x11 |
| +2      | 0x12 |
| +3      | 0x13 |
| +4      | 0x14 |
| +5 (max)| 0x15 |

## Logic Device IDs

| ID  | Function              |
|-----|----------------------|
| 01  | Communication ctrl   |
| 12  | Communication        |
| 21  | Switch               |
| 23  | Switch with name     |
| 24  | SW converting        |
| 25  | Command SW           |
| 28  | Beep dev in HU       |
| 29  | Beep via speakers    |
| 5D  | Climate ctrl drawing |
| 5E  | Audio drawing        |
| 5F  | Trip info drawing    |
| 60  | Tuner                |
| 61  | Tape deck            |
| 62  | CD                   |
| 63  | CD changer           |
| 74  | Audio amplifier      |
| E0  | Climate ctrl dev     |

## Log Analysis - Key Events

Based on the provided notes (row numbers from avc_lan.ndjson):

### Row 418 (seq 413): ICE Start

```json
{"m":"210","s":"490","c":8,"d":["00","46","C8","00"]}
```

**Analysis:**
- Master `210` sends to `490` (system status)
- `C8` in byte[2] differs from normal `C1` - indicates engine state change
- Byte pattern: `00 46 C8 00` vs normal `00 46 C1 80`

**Interpretation:**
| Byte | Value | Meaning |
|------|-------|---------|
| 0-1  | 00 46 | Status header |
| 2    | C8    | ICE running (vs C1=off) |
| 3    | 00    | No additional flags |

### Row 435 (seq 430): MFD Buttons

```json
{"m":"002","s":"660","c":4,"d":["00","02","0A","18","0A","90","80","A0","04","86","18","00","00","08","80","00","00","03","00","11","18","40","06","00","2F","00","12","A2","90","00","00","00"]}
```

Large 32-byte packet for MFD button/touch event.

### Row ~458-460: INFO Mode Selection

Mode change events through display control. Look for `112 → 060` messages.

### Row 513-518: AMP/Audio Control

```json
{"m":"400","s":"020","c":1,"d":["21"]}
```

**Analysis:**
- `400 → 020` with `21` appears 74 times in recording
- This is a keep-alive/acknowledgment signal
- Not specifically AMP toggle, but system heartbeat

### Row 549-550: AUDIO Settings / PARK Mode

```json
// Row 549
{"m":"002","s":"660","c":4,"d":["10","50","C4","52","82","02","00","22","30","C0","00","00","44","00","00","00"]}

// Row 550
{"m":"110","s":"490","c":8,"d":["8C","83","C1","00","00","00","16","00"]}
```

**Interpretation:**
- `8C 83` in 110→490 indicates display/mode configuration
- `16 00` at bytes 6-7 could be mode identifier

### Row 560-565: Auto Climate

```json
// Row 560
{"m":"A00","s":"258","c":2,"d":["02","01","05","00","20","31","02","00","04","00","00","00","04","52","82","02","00","23","60","A0",...]}

// Row 565
{"m":"110","s":"490","c":8,"d":["00","00","0A","01"]}
```

Climate AUTO mode activation visible in `A00 → 258` messages.

### Row 590-620: Outside Temperature Messages

```json
{"m":"10C","s":"310","c":8,"d":["00","00","00","00","08","0A","90","80"]}
```

**CORRECTED:** Byte[5] encoding: `temp_C = (byte5 - 18) / 2`
- `0A` = 10 → (10-18)/2 = **-4°C** (matches actual recorded outside temp of -2 to -4°C)
- `1C` = 28 → (28-18)/2 = **+5°C** (initial warmer reading at start of drive)

The original `/2` formula was incorrect. Empirical analysis shows an offset of 18 is required before dividing by 2.

### Row ~642-660: Volume/Audio Changes

```json
{"m":"040","s":"200","c":1,"d":["28","00","60","44","60"]}
```

**Pattern Analysis:**
- `040 → 200` with 5-byte data: `[28, 00, XX, YY, ZZ]`
- Byte 0: `28` (command type 40) or `2A` (command type 42)
- Bytes 2-3: State/mode flags
- Byte 4: Device ID (`62` = CD, `60` = Tuner, etc.)

### Row 650-665: Screen Touch/Input Events

Touch events observed from `000 → 114` (touch controller messages).

**NOTE:** The Flerchinger document describes `110 → 178` format for MFD-to-Navigation
touch commands, but observed live data shows raw touch controller messages
going from address `000` to `114` with different message structures.

#### Observed Message Formats (000 → 114)

**2-byte messages (coordinate pairs):**
```
[XX YY] - Raw X,Y coordinates (0-255 each)
Examples: [4A 11], [07 08], [04 04]
```

**13-byte Position Messages (Pattern A):**
```
[XX XX XX 00 00 01 21 XX YY YY 00 00 00]

bytes[5:7] = 01 21 → Touch position data marker
byte[7] = X coordinate  
byte[9] = Y coordinate

Examples:
  2C D1 C0 00 00 01 21 92 10 10 00 00 00 → x=0x92=146, y=0x10=16
  10 34 40 00 00 01 21 90 00 90 00 00 00 → x=0x90=144, y=0x90=144
```

**13-byte and 9-byte Status Messages (Pattern B):**
```
[XX XX XX 00 00 11 24 ...]

bytes[5:7] = 11 24 → Status/config message (similar to SW_CONVERT 0x24)
Not a touch position, used for touch panel configuration.

Examples:
  22 4C 00 00 00 11 24 0C 30 (9-byte)
  0A 68 40 00 00 11 24 0C 30 10 00 00 00 (13-byte)
```

**8-byte messages:**
```
[XX XX XX 00 00 PP 00 00]
byte[5] = Possible position or status value
Example: 94 42 80 00 00 4A 00 00 → x=0x4A=74
```

#### Documented Format (110 → 178) - Flerchinger

For MFD sending touch commands to Navigation ECU:
```json
{"m":"110","s":"178","c":15,"d":["00","21","24","78","XX","YY","XX","YY"]}

00 = prefix
21 = Switch logic device
24 = SW_CONVERT (coordinate conversion)
78 = Touch press indicator
XX = X coordinate (0x00-0xFF)
YY = Y coordinate (0x00-0xFF)
```

## Frequently Observed Message Patterns

### Status/Heartbeat Messages

```json
// Very frequent - system status
{"m":"110","s":"490","c":8,"d":["00","46","C1","80"]}
{"m":"110","s":"490","c":8,"d":["00","44","60","80"]}
{"m":"110","s":"490","c":8,"d":["00","00","00","08","A4","04","02","00"]}

// Acknowledgment
{"m":"400","s":"020","c":1,"d":["21"]}

// Climate status
{"m":"10C","s":"310","c":8,"d":["00","00","00","00","08","0A","90","80"]}
```

### Display Control Messages

```json
// Common pattern from 002 → 660
{"m":"002","s":"660","c":4,"d":["10","50","C4","52","82","01","00","11","18","60",...]}
```

### Button Press Pattern

```json
// 040 → 200 with 5-byte data
{"m":"040","s":"200","c":1,"d":["28","00","XX","YY","62"]}
// XX YY encode button identity
```

## Data Interpretation

### Byte 4 of 110→490 Messages

Observed patterns suggest:
- `46 C1 80/81/00` - General status flags
- `44 60 80/88/00` - Audio/Display state
- `00 00 00 08...` - Command/control data

### A4 04 XX 00 Pattern

Common in status messages:
- `A4 04 02 00` - Normal operation
- `A4 04 03 00` - Transition state
- `A4 04 05 00` - Active/playing
- `A4 04 06 00` - Configuration mode

## Next Steps for Implementation

### 1. State Machine Module
Create `cyberpunk_computer/comm/avc_state.py`:
- Parse incoming AVC-LAN messages
- Track device states (AMP on/off, volume, source)
- Emit events for UI updates

### 2. Message Decoder
Create `cyberpunk_computer/comm/avc_decoder.py`:
- Device address lookup
- Logic device ID interpretation
- Command type classification

### 3. Event Types to Support

```python
class AVCEvent(Enum):
    POWER_STATE = "power_state"      # ICE/EV/Accessory mode
    VOLUME_CHANGE = "volume_change"  # Volume level
    SOURCE_CHANGE = "source_change"  # Audio source (Radio/CD/AUX)
    BUTTON_PRESS = "button_press"    # Physical button
    TOUCH_EVENT = "touch_event"      # Screen touch
    CLIMATE_STATE = "climate_state"  # HVAC status
    AMP_STATE = "amp_state"          # Amplifier on/off
    DISPLAY_MODE = "display_mode"    # Current screen mode
```

### 4. Integration Points

- `gateway.py` - Already receiving data, needs to route to AVC parser
- `protocol.py` - Add AVC-LAN message type handling
- UI screens - Subscribe to relevant events

## Raw Data Statistics

From `avc_lan.ndjson` (725 records):

### Most Common Masters:
- `110` (EMV/Display) - ~40% of traffic
- `002` - ~15%
- `400` - ~10%
- `10C` (Climate) - ~8%

### Most Common Slaves:
- `490` - ~45% (system status sink)
- `660` - ~15%
- `020` - ~10%
- `200` - ~8%

---

## Implementation Status

### Completed Modules

| Module | Location | Description |
|--------|----------|-------------|
| `avc_decoder.py` | `comm/` | Decodes raw AVC-LAN messages, device lookup |
| `avc_state.py` | `comm/` | State management with event subscriptions |
| `avc_commands.py` | `comm/` | Command generation for sending to vehicle |
| `avc_integration.py` | `ui/` | Bridge between state manager and UI |
| `energy_monitor.py` | `ui/widgets/` | Energy Monitor visualization widget |
| `vehicle_status.py` | `ui/widgets/` | Status indicator widgets |
| `touch.py` | `input/` | Touch screen event handling |
| `can_decoder.py` | `comm/` | CAN bus message decoder |

### UI Integration

- **Audio Screen**: Volume 0-63, Bass/Treble ±5, Balance/Fader ±7
- **Climate Screen**: Temp 18-28°C, Fan 0-7, AC, Auto mode
- **Main Screen**: Energy Monitor and status widgets in center area

### Replay Mode Usage

```bash
# Run with log replay (AVC-LAN or CAN)
python -m cyberpunk_computer --replay assets/data/avc_lan.ndjson --dev

# Keyboard shortcuts in replay mode:
# P     - Play/Pause
# R     - Restart from beginning
# S     - Print message statistics
# J     - Jump to row
# [/]   - Step backward/forward 1 message
# -/+   - Step backward/forward 10 messages
# V     - Toggle verbose logging
# ESC   - Exit
```

