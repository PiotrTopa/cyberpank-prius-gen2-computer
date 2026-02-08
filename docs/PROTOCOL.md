# Gateway Host Protocol (NDJSON)

This document describes the communication protocol between the host application and the Gateway firmware.  
**Protocol Version:** 2.9.0  
**Format:** NDJSON (Newline-Delimited JSON) over USB Serial at 1,000,000 baud.

> **Note:** This is a reference copy. The authoritative version is maintained in the Gateway repository.

---

## 1. Overview

The Gateway acts as a bridge between:
- **Host Application** (PC, Raspberry Pi, etc.) ↔ USB Serial
- **CAN Bus** (via MCP2515)
- **AVC-LAN** (Toyota/Lexus proprietary bus)
- **RS485** (for distributed sensors/modules)

### Device IDs

| ID | Device | Description |
|:---|:-------|:------------|
| 0 | Gateway | System messages, configuration, errors |
| 1 | CAN Bus | OBD-II, diagnostic, and custom CAN frames |
| 2 | AVC-LAN | Toyota audio/multimedia bus |
| 6+ | RS485 | Remote modules (forwarded via RS485) |

---

## 2. CAN Bus Communication

### 2.1 Operating Modes

The MCP2515 CAN controller supports two operating modes:

| Mode | Description | Use Case |
|:-----|:------------|:---------|
| **Listen-Only** (default) | Passive sniffing, no ACKs sent, no TX | Safe bus monitoring |
| **Normal** | Full participation, ACKs frames, can transmit | OBD-II queries, diagnostics |

> ⚠️ **WARNING:** Switching to Normal mode makes the gateway an active participant on the CAN bus. In a vehicle, this means it will ACK frames and could influence error recovery. Use with caution.

### 2.2 Actions

All CAN commands use `id: 1` and include an `"a"` (action) field:

| Action | Description |
|:-------|:------------|
| `tx` | Send a raw CAN frame (legacy, default if `a` omitted) |
| `req` | Single request-response query (OBD-II style) |
| `sub` | Subscribe to periodic polling |
| `unsub` | Unsubscribe from a slot |
| `subs` | List active subscriptions |
| `mode` | Switch CAN operating mode |

---

## 3. Command Reference

### 3.1 Single Request-Response Query (`req`)

Sends a CAN request and waits for a matching response. Ideal for **on-demand data retrieval**.
Supports ISO-TP (ISO 15765-2) multi-frame response reassembly for large payloads.

**Request:**
```json
{"id":1,"d":{"a":"req","i":"0x7DF","d":[2,1,12],"r":["0x7E8"],"t":100}}
```

| Field | Type | Required | Description |
|:------|:-----|:---------|:------------|
| `a` | string | Yes | Action: `"req"` |
| `i` | string/int | Yes | Request CAN ID (hex string or integer) |
| `d` | array | Yes | Request data bytes (max 8) |
| `r` | array | No | Expected response CAN IDs (default: OBD-II ECU range 0x7E8-0x7EF) |
| `t` | int | No | Timeout in milliseconds (default: 100, use 300+ for multi-frame) |
| `e` | bool | No | Extended CAN ID flag (default: false) |
| `isotp` | bool | No | Enable ISO-TP multi-frame reassembly (auto-enabled if t >= 300) |

**Response (Success):**
```json
{"id":1,"ts":12345,"seq":42,"d":{"a":"resp","i":"0x7E8","d":[4,65,12,25,128]}}
```

**Response (Timeout):**
```json
{"id":1,"d":{"a":"resp","err":"TIMEOUT"}}
```

#### OBD-II Example: Read Engine RPM (PID 0x0C)

```json
{"id":1,"d":{"a":"req","i":"0x7DF","d":[2,1,12]}}
```

- `0x7DF` = OBD-II broadcast request ID
- `[2, 1, 12]` = 2 bytes follow, Mode 01, PID 0x0C (RPM)
- Response on `0x7E8`: `[4, 65, 12, A, B]` → RPM = ((A×256)+B)/4

#### ISO-TP Multi-Frame Example: Read HV Inverter Temperature (PID 21C3)

```json
{"id":1,"d":{"a":"req","i":"0x7E2","d":[3,0x21,0xC3],"r":["0x7EA"],"t":500,"isotp":true}}
```

- `0x7E2` = HV ECU request ID
- `[3, 0x21, 0xC3]` = 3 bytes follow, Service 21 (Read Data), PID 0xC3
- Response contains 31+ bytes of data reassembled from multiple CAN frames
- Response on `0x7EA`: `[97, 195, ...]` → Full payload with MG1/MG2 inverter temps at bytes 24-25

---

### 3.2 Subscription (Periodic Polling) (`sub`)

Creates a **subscription** that polls a CAN request at regular intervals. The gateway automatically sends requests and streams responses to the host.
Supports ISO-TP multi-frame reassembly for PIDs that return more than 7 bytes of data.

**Request:**
```json
{"id":1,"d":{"a":"sub","slot":0,"i":"0x7DF","d":[2,1,12],"r":["0x7E8"],"int":500,"t":100}}
```

| Field | Type | Required | Description |
|:------|:-----|:---------|:------------|
| `a` | string | Yes | Action: `"sub"` |
| `slot` | int | Yes | Slot ID (0-15), used to manage/cancel subscription |
| `i` | string/int | Yes | Request CAN ID |
| `d` | array | Yes | Request data bytes |
| `r` | array | No | Expected response CAN IDs |
| `int` | int | No | Polling interval in ms (default: 1000) |
| `t` | int | No | Response timeout in ms (default: 100, use 300+ for multi-frame) |
| `e` | bool | No | Extended CAN ID flag |
| `isotp` | bool | No | Enable ISO-TP multi-frame reassembly (auto-enabled if t >= 300) |

**Confirmation:**
```json
{"id":0,"d":{"msg":"SUB_OK","slot":0}}
```

**Periodic Response Stream:**
```json
{"id":1,"ts":12345,"seq":42,"d":{"a":"sub","slot":0,"i":"0x7E8","d":[4,65,12,25,128]}}
```

> 💡 Subscription responses include `"slot"` so the host can identify which subscription the data belongs to.

#### ISO-TP Subscription Example: HV Inverter Temperature

```json
{"id":1,"d":{"a":"sub","slot":5,"i":"0x7E2","d":[3,0x21,0xC3],"r":["0x7EA"],"int":1000,"t":500,"isotp":true}}
```

This creates a subscription that polls the HV ECU every second for inverter temperatures.
The response will contain the complete 31-byte payload with:
- Byte 24: MG1 Inverter Temperature
- Byte 25: MG2 Inverter Temperature
- Bytes 26-27: Motor temperatures

---

### 3.3 Unsubscribe (`unsub`)

Removes a subscription by slot ID.

**Unsubscribe Single Slot:**
```json
{"id":1,"d":{"a":"unsub","slot":0}}
```

**Unsubscribe All:**
```json
{"id":1,"d":{"a":"unsub","slot":"all"}}
```

**Confirmation:**
```json
{"id":0,"d":{"msg":"UNSUB_OK","slot":0}}
```
or
```json
{"id":0,"d":{"msg":"UNSUB_ALL"}}
```

---

### 3.4 List Subscriptions (`subs`)

Returns a list of all active subscriptions.

**Request:**
```json
{"id":1,"d":{"a":"subs"}}
```

**Response:**
```json
{"id":0,"d":{"subs":[{"slot":0,"i":"0x7DF","int":500},{"slot":1,"i":"0x7DF","int":1000}]}}
```

---

### 3.5 Switch CAN Mode (`mode`)

Manually switch between Listen-Only and Normal mode.

**Switch to Normal (TX enabled):**
```json
{"id":1,"d":{"a":"mode","m":"normal"}}
```

**Switch to Listen-Only (passive):**
```json
{"id":1,"d":{"a":"mode","m":"listen"}}
```

**Confirmation:**
```json
{"id":0,"d":{"msg":"CAN_MODE","m":"NORMAL"}}
```

> ⚠️ Switching to `listen` mode clears all active subscriptions.

---

### 3.6 Send Raw CAN Frame (`tx`)

Legacy command to send a raw CAN frame without expecting a response.

**Request:**
```json
{"id":1,"d":{"i":"0x123","d":[1,2,3,4,5,6,7,8]}}
```

or explicitly:
```json
{"id":1,"d":{"a":"tx","i":"0x123","d":[1,2,3,4,5,6,7,8]}}
```

| Field | Type | Required | Description |
|:------|:-----|:---------|:------------|
| `a` | string | No | Action: `"tx"` (default if omitted) |
| `i` | string/int | Yes | CAN ID |
| `d` | array | Yes | Data bytes (max 8) |
| `e` | bool | No | Extended CAN ID flag |

---

## 4. Passive CAN RX (Broadcast Frames)

When the gateway receives CAN frames (either in Listen-Only or Normal mode), they are streamed to the host:

```json
{"id":1,"ts":12345,"seq":42,"d":{"i":"0x1A0","d":[0,100,0,50,0,0,0,0]}}
```

| Field | Description |
|:------|:------------|
| `id` | Always `1` for CAN |
| `ts` | Timestamp (milliseconds since boot) |
| `seq` | Sequence counter (if enabled) |
| `d.i` | CAN ID (hex string) |
| `d.d` | Data bytes array |

---

## 5. Design Patterns: When to Use What?

### On-Demand Query (`req`)

Use for **infrequent or one-time data retrieval**:
- Reading VIN (Vehicle Identification Number)
- Reading Diagnostic Trouble Codes (DTCs)
- Checking battery voltage at startup
- Manual diagnostic commands

```python
# Python example: Read VIN
import serial
import json

ser = serial.Serial('COM3', 1000000)

# Mode 09, PID 02 = VIN
ser.write(b'{"id":1,"d":{"a":"req","i":"0x7DF","d":[2,9,2]}}\n')
response = json.loads(ser.readline())
print("VIN Response:", response)
```

### Subscription (`sub`)

Use for **continuous monitoring** (dashboard, logging):
- Engine RPM
- Vehicle Speed
- Coolant Temperature
- Fuel Level

```python
# Python example: Dashboard with subscriptions
import serial
import json

ser = serial.Serial('COM3', 1000000)

# Subscribe to RPM (PID 0x0C) every 200ms
ser.write(b'{"id":1,"d":{"a":"sub","slot":0,"i":"0x7DF","d":[2,1,12],"int":200}}\n')

# Subscribe to Speed (PID 0x0D) every 200ms
ser.write(b'{"id":1,"d":{"a":"sub","slot":1,"i":"0x7DF","d":[2,1,13],"int":200}}\n')

# Subscribe to Coolant Temp (PID 0x05) every 1000ms
ser.write(b'{"id":1,"d":{"a":"sub","slot":2,"i":"0x7DF","d":[2,1,5],"int":1000}}\n')

# Read streaming data
while True:
    line = ser.readline()
    frame = json.loads(line)
    if frame.get('id') == 1 and frame.get('d', {}).get('a') == 'sub':
        slot = frame['d']['slot']
        data = frame['d']['d']
        if slot == 0:
            rpm = ((data[3] * 256) + data[4]) / 4
            print(f"RPM: {rpm}")
        elif slot == 1:
            speed = data[3]
            print(f"Speed: {speed} km/h")
        elif slot == 2:
            temp = data[3] - 40
            print(f"Coolant: {temp}°C")
```

---

## 6. OBD-II Quick Reference

### Common PIDs (Mode 01)

| PID | Description | Formula | Unit |
|:----|:------------|:--------|:-----|
| 0x05 | Coolant Temp | A - 40 | °C |
| 0x0C | Engine RPM | ((A×256)+B)/4 | RPM |
| 0x0D | Vehicle Speed | A | km/h |
| 0x0F | Intake Air Temp | A - 40 | °C |
| 0x10 | MAF Flow | ((A×256)+B)/100 | g/s |
| 0x11 | Throttle Position | A×100/255 | % |
| 0x2F | Fuel Level | A×100/255 | % |
| 0x46 | Ambient Air Temp | A - 40 | °C |

### Toyota Extended PIDs (Mode 21)

| PID | ECU | Description | Response Size |
|:----|:----|:------------|:--------------|
| 0xC3 | 0x7E2 (Hybrid) | Inverter temps, MG RPM/torque, HV voltage | 31 bytes |
| 0xC4 | 0x7E2 (Hybrid) | Accelerator, boost voltage, converter temp | 20+ bytes |
| 0xCE | 0x7E3 (HV Battery) | Battery SOC, voltage | 10+ bytes |
| 0xCF | 0x7E3 (HV Battery) | Delta SOC, intake temp, block temps | 15+ bytes |
| 0xD0 | 0x7E3 (HV Battery) | Individual block voltages | 20+ bytes |

### Request Format
```
[Length, Mode, PID]
```

Example for RPM: `[2, 1, 12]` = 2 bytes, Mode 01, PID 0x0C

### Response Format
```
[Length, Mode+0x40, PID, Data...]
```

Example RPM response: `[4, 65, 12, A, B]` → Mode 01+0x40=65, PID 12, data bytes A,B

---

## 7. Error Codes

| Error | Description |
|:------|:------------|
| `CAN_OFFLINE` | CAN controller not initialized |
| `CAN_TX_FULL` | TX buffer full, message not sent |
| `CAN_MODE_SWITCH_FAIL` | Failed to switch operating mode |
| `TIMEOUT` | No response within timeout period |
| `INVALID_SLOT` | Subscription slot out of range (0-15) |
| `SLOT_NOT_FOUND` | Attempted to unsubscribe non-existent slot |
| `UNKNOWN_ACTION` | Invalid action specified |
| `JSON_PARSE` | Malformed JSON command |

---

## 8. Best Practices

### Bus Load Management

- **Don't over-subscribe:** Each subscription adds bus traffic. On OBD-II port, keep total request rate under 20 requests/second.
- **Use appropriate intervals:** Fast PIDs (RPM, speed) = 100-200ms. Slow PIDs (temps) = 1000ms.
- **Unsubscribe when done:** Clear subscriptions when the dashboard/logging session ends.

### Timeout Tuning

- **OBD-II:** 100ms is typically sufficient for most ECUs.
- **Manufacturer diagnostics:** May need longer timeouts (200-500ms) for complex queries.
- **Multi-frame responses:** UDS/ISO-TP multi-frame responses need 300-500ms+.

### Error Handling

- Check for `TIMEOUT` responses - the ECU may not support that PID.
- Monitor `seq` counter for frame continuity (detect dropped messages).
- On persistent timeouts, check CAN termination and wiring.

---

## 9. Limitations

| Item | Limit |
|:-----|:------|
| Max subscriptions | 16 slots |
| Min polling interval | ~50ms (practical limit due to processing) |
| CAN data bytes | 8 max (CAN 2.0B), up to 64 bytes with ISO-TP reassembly |
| Response IDs per query | ~10 (memory limited) |
| USB baud rate | 1,000,000 bps |
| ISO-TP max payload | 64 bytes (single ISO-TP message) |

---

## 10. Changelog

### v2.9.0
- **ISO-TP Support (ISO 15765-2):** Added multi-frame response reassembly
  - Automatically handles First Frame (FF), Flow Control (FC), and Consecutive Frames (CF)
  - Reassembles payloads up to 64 bytes from multiple CAN frames
  - Enabled via `"isotp": true` flag or automatically when timeout ≥ 300ms
- Added `isotp` parameter to `req` and `sub` actions
- Supports Toyota/Lexus diagnostic PIDs that return >7 bytes (e.g., PID 21C3 for inverter temps)

### v2.8.0
- Added solicited CAN mode with request-response support
- Added subscription manager for periodic polling
- Added `req`, `sub`, `unsub`, `subs`, `mode` actions
- Automatic mode switching from Listen-Only to Normal when TX needed
- Comprehensive OBD-II support

### v2.7.0
- Initial CAN bus support (listen-only)
- RS485 forwarding
- AVC-LAN bidirectional communication
