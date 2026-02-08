# Solicited OBD2 Queries - Implementation Guide

## Overview

Several important vehicle parameters are **not available** in unsolicited CAN broadcast messages.
They require **solicited OBD2 queries** to specific ECUs. This document tracks the implementation
of these features.

**Status: ✅ IMPLEMENTED** (Gateway Protocol v2.8.0)

---

## Implementation Summary

The solicited CAN mode is now fully implemented:

| Component | File | Status |
|-----------|------|--------|
| Protocol Documentation | [docs/PROTOCOL.md](PROTOCOL.md) | ✅ Updated |
| Protocol Functions | `cyberpunk_computer/comm/protocol.py` | ✅ Added `create_can_request`, `create_can_subscription`, etc. |
| Gateway Connection | `cyberpunk_computer/comm/gateway.py` | ✅ Added `can_switch_mode`, `can_subscribe`, `obd2_request`, etc. |
| CAN Decoder | `cyberpunk_computer/comm/can_decoder.py` | ✅ Added solicited response parsing |
| PID Manager | `cyberpunk_computer/comm/solicited_can.py` | ✅ New module with PID definitions |

---

## Priority 1: Inverter/Motor Temperatures ✅

### Current Status
- UI element exists (`_inv_temp_display` in main_screen.py)
- State field exists (`vehicle.inverter_temp` in app_state.py)
- Action exists (`SetInverterTempAction`)
- **✅ DATA SOURCE IMPLEMENTED** - PID 21C3 from ECU 0x7E2

### Implementation
Send OBD2 query to **ECU 0x7E2** (Hybrid System) with **PID 21C3**

#### Request Format
```python
# Using new gateway methods:
gateway.obd2_subscribe(slot=0, mode=0x21, pid=0xC3, interval_ms=500, ecu=0x7E2)

# Or raw:
gateway.can_subscribe(
    slot=0,
    can_id=0x7E2,
    data=[0x02, 0x21, 0xC3, 0x00, 0x00, 0x00, 0x00, 0x00],
    interval_ms=500,
    response_ids=["0x7EA"]
)
```

#### Response (parsed by CANDecoder)
| Value | Key in msg.values | Formula | Range |
|-------|-------------------|---------|-------|
| MG1 Inverter Temp | `mg1_inverter_temp` | `Y - 40` | -40 to 215 °C |
| MG2 Inverter Temp | `mg2_inverter_temp` | `Z - 40` | -40 to 215 °C |
| Motor Temp No2 (MG2) | `mg2_motor_temp` | `AA - 40` | -40 to 215 °C |
| Motor Temp No1 (MG1) | `mg1_motor_temp` | `AB - 40` | -40 to 215 °C |
| Primary inverter_temp | `inverter_temp` | Uses MG2 value | -40 to 215 °C |

---

## Priority 2: Delta SOC (Battery Cell Imbalance) ✅

### Current Status
- UI chart exists (`_draw_voltage_chart` in energy_monitor.py, labeled "ΔSOC")
- State field exists (`energy.battery_delta_soc` in app_state.py)
- Action exists (`SetBatteryDeltaSOCAction`)
- **✅ DATA SOURCE IMPLEMENTED** - PID 21CF from ECU 0x7E3

### Implementation
Send OBD2 query to **ECU 0x7E3** (HV Battery) with **PID 21CF**

#### Request Format
```python
gateway.obd2_subscribe(slot=1, mode=0x21, pid=0xCF, interval_ms=2000, ecu=0x7E3)
```

#### Response (parsed by CANDecoder)
| Value | Key in msg.values | Formula | Range |
|-------|-------------------|---------|-------|
| Delta SOC | `delta_soc` | `0.01 * G` | 0-60% |
| Battery Air Temp | `battery_air_intake_temp` | `(256*A+B)/100 - 327.68` | °C |
| Charge Limit | `charge_limit_kw` | `E - 64` | kW |
| Discharge Limit | `discharge_limit_kw` | `F - 64` | kW |
| Fan Speed | `battery_fan_speed` | `H` | 0-6 |

**Interpretation:**
- 0-1%: Excellent battery health
- 1-2%: Good condition
- 2-3%: Fair, may have weak cells
- >3%: Poor, cells need attention

---

## Priority 3: Individual Block Voltages (Blocks 01-14) ✅

### Current Status
- **✅ DATA SOURCE IMPLEMENTED** - PID 21CE from ECU 0x7E3

### Implementation
Send OBD2 query to **ECU 0x7E3** with **PID 21CE**

#### Response (parsed by CANDecoder)
| Value | Key in msg.values | Formula |
|-------|-------------------|---------|
| SOC | `battery_soc_21ce` | `0.5 * A` |
| Battery Current | `battery_current_21ce` | `(256*B+C)/100 - 327.68` A |
| Battery Power | `battery_power_kw_21ce` | `(256*D+E)/100 - 327.68` kW |
| Block Voltages | `block_voltages` | Array of 14 values |
| Min Block Voltage | `block_voltage_min` | V |
| Max Block Voltage | `block_voltage_max` | V |
| Voltage Delta | `block_voltage_delta` | V |

---

## Quick Start: Enabling Solicited Mode

### 1. Switch to Normal CAN Mode
```python
from cyberpunk_computer.comm.gateway import GatewayConnection

gateway = GatewayConnection(config)
gateway.connect()

# Switch from listen-only to normal (active) mode
gateway.can_switch_mode("normal")
```

### 2. Subscribe to PIDs
```python
# Dashboard data - fast updates
gateway.obd2_subscribe(slot=0, mode=0x21, pid=0xC3, interval_ms=500, ecu=0x7E2)  # Hybrid

# Battery monitoring - slower updates
gateway.obd2_subscribe(slot=1, mode=0x21, pid=0xCF, interval_ms=2000, ecu=0x7E3)  # Delta SOC
gateway.obd2_subscribe(slot=2, mode=0x21, pid=0xCE, interval_ms=5000, ecu=0x7E3)  # Block voltages
```

### 3. Process Responses
Responses are automatically parsed by `CANDecoder`:
```python
from cyberpunk_computer.comm.can_decoder import CANDecoder, CANMessageType

decoder = CANDecoder()

def handle_can_message(message):
    if message.device_id == 1:  # CAN device
        msg = decoder.decode(message.data)
        if msg and msg.msg_type == CANMessageType.SOLICITED_HYBRID:
            inverter_temp = msg.values.get("inverter_temp")
            if inverter_temp is not None:
                dispatch(SetInverterTempAction(inverter_temp))
```

### 4. Using the High-Level Manager
```python
from cyberpunk_computer.comm.solicited_can import get_manager, PID_HYBRID_COMPREHENSIVE

manager = get_manager()
manager.set_send_callback(gateway.send)

# Apply a predefined profile
manager.apply_profile(manager.PROFILE_DASHBOARD)

# Or subscribe individually
manager.subscribe(slot=0, pid_def=PID_HYBRID_COMPREHENSIVE, interval_ms=500)
```

---

## Available Subscription Profiles

The `SolicitedCANManager` provides pre-defined profiles:

### PROFILE_DASHBOARD
For real-time dashboard display:
- Engine RPM @ 200ms
- Vehicle Speed @ 200ms
- Coolant Temp @ 1000ms
- Hybrid System (inverter temps) @ 500ms

### PROFILE_ENERGY_MONITOR
For energy flow monitoring:
- Hybrid System @ 500ms
- Battery Temps/Delta SOC @ 2000ms
- Battery Detail @ 5000ms

### PROFILE_BATTERY_HEALTH
For battery diagnostics:
- Battery Detail (block voltages) @ 2000ms
- Battery Temps (delta SOC) @ 2000ms

---

## Bus Load Considerations

⚠️ **Important:** Each subscription adds traffic to the CAN bus.

- Keep total request rate under 20 requests/second on OBD-II port
- Use appropriate intervals:
  - Fast PIDs (RPM, speed): 100-200ms
  - Medium PIDs (temps, hybrid data): 500-1000ms
  - Slow PIDs (battery blocks): 2000-5000ms
- Unsubscribe when not needed

---

## References
- [docs/PROTOCOL.md](PROTOCOL.md) - Gateway communication protocol
- [docs/prius_can.md](prius_can.md) - Full PID documentation
- Section 5: "Solicited (CAN) - Generic Engine (ECU 07E0)"
- Section 6: "Solicited (CAN) - Hybrid/Specific (ECU 07E2)"
- Section 7: "Solicited (CAN) - HV Battery (ECU 07E3)"
- Block # with Min/Max V
- The actual min/max voltage values
- Internal Resistance R01-R14: `0.001 * Byte` (0-10 Ohm)

---

## References
- [docs/PROTOCOL.md](PROTOCOL.md) - Gateway communication protocol
- [docs/prius_can.md](prius_can.md) - Full PID documentation
- Section 5: "Solicited (CAN) - Generic Engine (ECU 07E0)"
- Section 6: "Solicited (CAN) - Hybrid/Specific (ECU 07E2)"
- Section 7: "Solicited (CAN) - HV Battery (ECU 07E3)"
