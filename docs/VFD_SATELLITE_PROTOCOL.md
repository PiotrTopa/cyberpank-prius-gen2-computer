# VFD Satellite Protocol Specification

## Overview

The VFD Display satellite (device ID 110) receives pre-processed data from the main application and renders it on a 256×48 pixel VFD (Vacuum Fluorescent Display).

This document defines the protocol between the main CyberPunk Computer application and the VFD satellite.

## Design Philosophy

The protocol represents a **sweet spot** between raw vehicle data and fully pre-rendered content:

| Approach | Pros | Cons |
|----------|------|------|
| **Raw CAN/AVC data** | Simple host | VFD needs decoders, high bandwidth |
| **Pre-rendered pixels** | Simple VFD | ~1.5KB/frame, wasteful bandwidth |
| **Normalized values** ✅ | Efficient, clean separation | Both sides do their job |

**Our choice**: Send **normalized values and states** that allow the VFD to render independently:
- Values are 0.0-1.0 normalized (or appropriate enums)
- VFD handles animation, rendering, and timing
- Host computes the "what to show", VFD handles "how to show"

## Transport

- **Device ID**: `110` (VFD Display satellite)
- **Protocol**: NDJSON over RS485 (production) or UDP (development)
- **Direction**: Primarily Host → VFD (some status messages VFD → Host)
- **Update Rate**: 10-20 Hz target (every 50-100ms)

## Message Structure

### Host → VFD Messages

```json
{"id": 110, "d": {"t": "<msg_type>", ...payload}}
```

Message types:
- `"E"` - Energy data (power flows, fuel, battery)
- `"S"` - State flags (ICE running, gear, etc.)
- `"C"` - Configuration (time base, etc.)
- `"R"` - Reset/clear command

---

## Message Type: Energy Data (`"E"`)

Sent at 10-20 Hz with current vehicle energy state.

```json
{
  "id": 110,
  "d": {
    "t": "E",
    "mg": 0.35,      // MG power: -1.0 (regen) to +1.0 (assist)
    "fl": 0.25,      // Fuel flow: 0.0 to 1.0 (normalized, 0-8 L/h)
    "br": 0.0,       // Brake: 0.0 to 1.0 (pedal pressure normalized)
    "spd": 0.45,     // Speed: 0.0 to 1.0 (normalized, 0-120 km/h)
    "soc": 0.62,     // Battery SOC: 0.0 to 1.0
    "ptr": 25,       // Petrol level: liters (0-45)
    "lpg": 42,       // LPG level: liters (0-60)
    "ice": true      // ICE running flag
  }
}
```

### Field Descriptions

| Field | Type | Range | Description |
|-------|------|-------|-------------|
| `mg` | float | -1.0 to +1.0 | MG power normalized to ±30kW. Positive = motor (discharge), Negative = generator (charge/regen) |
| `fl` | float | 0.0 to 1.0 | Fuel flow rate normalized to 8 L/h max |
| `br` | float | 0.0 to 1.0 | Brake pedal pressure normalized to 127 max |
| `spd` | float | 0.0 to 1.0 | Vehicle speed normalized to 120 km/h |
| `soc` | float | 0.0 to 1.0 | HV Battery state of charge |
| `ptr` | int | 0-45 | Petrol tank level in liters |
| `lpg` | int | 0-60 | LPG tank level in liters |
| `ice` | bool | - | True if ICE is currently running |

### Power Flow Derivation (VFD-side)

The VFD calculates power flows from the normalized values:

```
BATT → WHEELS:  mg > 0.05 (battery discharging to motor)
WHEELS → BATT:  mg < -0.05 AND spd > 0.01 (regen braking)
ICE → BATT:     ice AND mg < -0.05 AND spd < 0.01 (ICE charging)
ICE → WHEELS:   ice AND spd > 0.01 (ICE driving)
```

---

## Message Type: State Flags (`"S"`)

Sent when state changes (or periodically every 1s as keepalive).

```json
{
  "id": 110,
  "d": {
    "t": "S",
    "fuel": "LPG",     // Active fuel: "OFF", "PTR", "LPG"
    "gear": "D",       // Gear: "P", "R", "N", "D", "B"
    "rdy": true        // READY mode active
  }
}
```

### Field Descriptions

| Field | Type | Values | Description |
|-------|------|--------|-------------|
| `fuel` | string | `"OFF"`, `"PTR"`, `"LPG"` | Currently active fuel type |
| `gear` | string | `"P"`, `"R"`, `"N"`, `"D"`, `"B"` | Current gear position |
| `rdy` | bool | - | Vehicle READY mode (hybrid system active) |

---

## Message Type: Configuration (`"C"`)

Sent on startup and when user changes settings.

```json
{
  "id": 110,
  "d": {
    "t": "C",
    "tb": 60,          // Time base: 15, 60, 300, 900, 3600 seconds
    "bri": 100         // Brightness: 0-100%
  }
}
```

### Field Descriptions

| Field | Type | Values | Description |
|-------|------|--------|-------------|
| `tb` | int | 15, 60, 300, 900, 3600 | Power chart time base in seconds |
| `bri` | int | 0-100 | Display brightness percentage |

---

## Message Type: Reset (`"R"`)

Clear display state (on startup, mode change, etc.)

```json
{
  "id": 110,
  "d": {
    "t": "R",
    "hist": true       // Clear history buffer
  }
}
```

---

## VFD → Host Messages

The VFD can send status/acknowledgment messages back.

### Ready Message

Sent when VFD initializes:

```json
{
  "id": 110,
  "d": {
    "msg": "VFD_READY",
    "ver": "1.0.0",
    "res": "256x48"
  }
}
```

### Error Message

```json
{
  "id": 110,
  "d": {
    "err": "BUFFER_OVERFLOW"
  }
}
```

---

## Bandwidth Analysis

Typical message sizes:
- Energy message (`E`): ~90 bytes
- State message (`S`): ~50 bytes
- Config message (`C`): ~40 bytes

At 20 Hz update rate:
- Energy only: 90 × 20 = **1,800 bytes/sec** (~15 kbit/s)
- With state keepalive: +50 bytes/sec

RS485 at 115200 baud: ~10,000 bytes/sec available → **Plenty of headroom**

---

## Timing and Animation

The VFD satellite handles all animation timing:
- Power flow arrows: 60 FPS animation based on flow direction
- Power bars: EMA smoothing applied locally
- Energy graph: Time-based column advancement using `tb` setting

The host only sends data updates; VFD interpolates between updates for smooth display.

---

## Example Session

```
# Host sends config on connect
{"id":110,"d":{"t":"C","tb":60,"bri":100}}

# VFD acknowledges
{"id":110,"d":{"msg":"VFD_READY","ver":"1.0.0"}}

# Host sends state
{"id":110,"d":{"t":"S","fuel":"PTR","gear":"P","rdy":false}}

# Vehicle starts - READY mode
{"id":110,"d":{"t":"S","fuel":"PTR","gear":"P","rdy":true}}

# Driving - energy updates at 20Hz
{"id":110,"d":{"t":"E","mg":0.15,"fl":0.3,"br":0,"spd":0.25,"soc":0.58,"ptr":25,"lpg":42,"ice":true}}
{"id":110,"d":{"t":"E","mg":0.22,"fl":0.35,"br":0,"spd":0.33,"soc":0.57,"ptr":25,"lpg":42,"ice":true}}

# Braking - regen
{"id":110,"d":{"t":"E","mg":-0.45,"fl":0,"br":0.6,"spd":0.2,"soc":0.58,"ptr":25,"lpg":42,"ice":false}}

# Gear change
{"id":110,"d":{"t":"S","fuel":"PTR","gear":"D","rdy":true}}
```

---

## Implementation Notes

### Host (Main Application)

1. Create `VfdDisplayRule` in rules engine that:
   - Watches `VEHICLE`, `ENERGY` state slices
   - Computes normalized values from raw state
   - Updates `VfdDisplayState` with computed data

2. Register egress handler for device 110:
   - Watches `VfdDisplayState` changes
   - Builds and sends `E` messages at configured rate
   - Sends `S` messages on state change

### VFD Satellite

1. NDJSON receiver (serial or UDP)
2. State machine for current values
3. Animation engine (power flow arrows, bars)
4. Framebuffer renderer
5. Display output (pygame for dev, SPI for hardware)
