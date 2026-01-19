# Communication Protocol Specification

This document defines the **NDJSON (Newline Delimited JSON)** protocol used for communication between the Host (PC/Raspberry Pi) and the Gateway (RP2040).

> **Note:** This is a reference copy. The authoritative version is maintained in the [Gateway repository](../../Gateway/PROTOCOL.md).

## 1. General Architecture

- **Transport:** USB CDC (Serial)
- **Baudrate:** 1,000,000 (Recommended)
- **Format:** NDJSON - Each line is a valid JSON object
- **Direction:** Bidirectional asynchronous

### Root Object Structure

To maximize throughput, property names are minimized.

```json
{
  "id": <int>,      // Device ID / Channel
  "ts": <int>,      // Timestamp (ms), optional in TX
  "seq": <int>,     // Sequence Counter (0-65535), optional RX only
  "d":  <any>       // Payload Data (Object, Array, or Value)
}
```

| Key | Description | Type | Notes |
|:----|:------------|:-----|:------|
| `id` | **Device ID** | `int` | Routing channel (see Device Map) |
| `ts` | **Timestamp** | `int` | Gateway uptime in milliseconds |
| `seq`| **Sequence** | `int` | (Optional) Cyclic counter (0-65535) for checking data continuity |
| `d` | **Data** | `any` | Protocol-specific payload |

---

## 2. Device Map

| ID | Name | Description | Routing Logic |
|:---|:-----|:------------|:--------------|
| `0` | **SYSTEM** | Gateway Status & Control | Processed internally by RP2040 |
| `1` | **CAN** | Vehicle CAN Bus | Wrapped & Forwarded to/from CAN Controller |
| `2` | **AVC-LAN** | Multimedia Bus | Wrapped & Forwarded to/from PIO State Machines |
| `6-255` | **SAT** | RS485 Satellites | Wrapped & Forwarded to/from RS485 Bus |

---

## 3. Payload Definitions

### ID 0: SYSTEM (Gateway Status)

Used for lifecycle events, errors, and configuration.

**RX (Gateway -> Host):**
```json
{"id":0, "d": {"msg": "GATEWAY_READY", "ver": "2.6.0", "can": "CAN_READY", "rs485": "READY"}}
{"id":0, "ts":105, "d": {"err": "RX_OVERFLOW"}}
{"id":0, "ts":110, "d": {"ack": true}}
{"id":0, "d": {"msg": "CFG_UPDATED", "seq": true}}
```

**TX (Host -> Gateway) - Configuration:**
```json
{"id":0, "d": {"seq": true}}  // Enable Sequence Counter
```

### ID 1: CAN (Vehicle Bus)

Transparent bridge to the vehicle's Controller Area Network.

**Payload Structure:**
```json
{
  "i": <int|hex_str>, // CAN ID (11-bit or 29-bit)
  "d": <[int]>,       // Data Bytes (0-8 integers)
  "e": <bool>         // Extended Frame (Optional, default false)
}
```

**Example:**
```json
// RX: Odometer data
{"id":1, "ts":2200, "d": {"i": "0x2C4", "d": [0, 0, 12, 55]}}

// TX: Unlock Doors
{"id":1, "d": {"i": "0x5A0", "d": [128, 1]}}
```

### ID 2: AVC-LAN (Multimedia)

Bridge for the NEC IEBus-based AVC-LAN.

**Payload Structure:**
```json
{
  "m": <hex_str>,     // Master Address (12-bit)
  "s": <hex_str>,     // Slave Address (12-bit)
  "c": <int>,         // Control Flag (4-bit)
  "d": <[hex_str]>,   // Data Bytes (Array of Hex Strings)
  "cnt": <int>        // Burst Count (RX only, for aggregation)
}
```

**Example:**
```json
// RX: Volume Up
{"id":2, "ts":3500, "d": {"m":"190", "s":"110", "c":0, "d":["01","FF"], "cnt":1}}

// TX: Change Mode
{"id":2, "d": {"m":"190", "s":"110", "c":0, "d":["02"]}}
```

### ID > 5: SATELLITES (RS485)

Transparent tunnel to distributed modules. The `id` corresponds to the target satellite address on the RS485 bus.

**Payload Structure:**
Defined by the specific satellite implementation. The Gateway treats `d` as a transparent payload.

**Example (Window Controller at ID 6):**
```json
// TX: Open Window
{"id":6, "d": {"cmd": "OPEN", "val": 100}}

// RX: Status Report
{"id":6, "ts":9999, "d": {"temp": 24.5, "state": "IDLE"}}
```

---

## 4. Implementation Notes for This Application

### Message Parsing

```python
import json

def parse_message(line: str) -> dict | None:
    """Parse a single NDJSON line from the Gateway."""
    try:
        return json.loads(line.strip())
    except json.JSONDecodeError:
        return None
```

### Message Sending

```python
def send_message(serial_port, device_id: int, data: dict) -> None:
    """Send a message to the Gateway."""
    message = {"id": device_id, "d": data}
    serial_port.write((json.dumps(message) + "\n").encode())
```

### Recommended Serial Configuration

```python
import serial

gateway = serial.Serial(
    port='/dev/ttyACM0',  # Or COM port on Windows
    baudrate=1_000_000,
    timeout=0.1
)
```
