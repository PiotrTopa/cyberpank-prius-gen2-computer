# TODO: Implement Solicited OBD2 Queries

## Overview

Several important vehicle parameters are **not available** in unsolicited CAN broadcast messages.
They require **solicited OBD2 queries** to specific ECUs. This document tracks the implementation
of these features.

---

## Priority 1: Inverter/Motor Temperatures

### Current Status
- UI element exists (`_inv_temp_display` in main_screen.py)
- State field exists (`vehicle.inverter_temp` in app_state.py)
- Action exists (`SetInverterTempAction`)
- **NO DATA SOURCE** - 0x540 was incorrectly assumed to contain inverter temp

### Required Implementation
Send OBD2 query to **ECU 0x7E2** (Hybrid System) with **PID 21C3**

#### Request Format
```
CAN ID: 0x7E2
Data: [02, 21, C3, 00, 00, 00, 00, 00]
       └─ length  └─ mode  └─ PID
```

#### Response (multi-frame from 0x7EA)
| Byte | Parameter | Formula | Range |
|------|-----------|---------|-------|
| Y | MG1 Inverter Temp | `Y - 40` | -40 to 215 °C |
| Z | MG2 Inverter Temp | `Z - 40` | -40 to 215 °C |
| AA | Motor Temp No2 (MG2) | `AA - 40` | -40 to 215 °C |
| AB | Motor Temp No1 (MG1) | `AB - 40` | -40 to 215 °C |

#### Code Locations to Update
- `cyberpunk_computer/comm/can_decoder.py` - Add response parser for 0x7EA
- `cyberpunk_computer/comm/gateway_adapter.py` - Dispatch `SetInverterTempAction`
- `cyberpunk_computer/comm/gateway.py` - Add periodic query mechanism

---

## Priority 2: Delta SOC (Battery Cell Imbalance)

### Current Status
- UI chart exists (`_draw_voltage_chart` in energy_monitor.py, labeled "ΔSOC")
- State field exists (`energy.battery_delta_soc` in app_state.py)
- Action exists (`SetBatteryDeltaSOCAction`)
- **NO DATA SOURCE** - 0x3CB byte 2 was incorrectly assumed to be delta SOC

### Required Implementation
Send OBD2 query to **ECU 0x7E2** (Hybrid System) with **PID 21CF**

#### Request Format
```
CAN ID: 0x7E2
Data: [02, 21, CF, 00, 00, 00, 00, 00]
```

#### Response (multi-frame from 0x7EA)
| Byte | Parameter | Formula | Range |
|------|-----------|---------|-------|
| G | Delta SOC | `0.01 * G` | 0-60% |

**Interpretation:**
- 0-1%: Excellent battery health
- 1-2%: Good condition
- 2-3%: Fair, may have weak cells
- >3%: Poor, cells need attention

#### Code Locations to Update
- `cyberpunk_computer/comm/can_decoder.py` - Add response parser
- `cyberpunk_computer/comm/gateway_adapter.py` - Dispatch `SetBatteryDeltaSOCAction`
- `cyberpunk_computer/ui/widgets/energy_monitor.py` - Chart already implemented

---

## Priority 3: NiMH Volt Delta (Cell Voltage Difference)

### Current Status
- **NO UI element** - Could be added to energy_monitor chart
- **NO state field** - Needs to be added to `EnergyState`
- **NO action** - Needs `SetBatteryDeltaVoltAction`

### Required Implementation
Send OBD2 query to **ECU 0x7E2** (Hybrid System) with **PID 21D0**

#### Request Format
```
CAN ID: 0x7E2
Data: [02, 21, D0, 00, 00, 00, 00, 00]
```

#### Response (multi-frame from 0x7EA)
| Byte | Parameter | Formula | Range |
|------|-----------|---------|-------|
| J, N | NiMH Volt Delta | `(256*J + 0.01*N) - 327.68` | 0-3 V |

Also provides:
- Block # with Min/Max V
- The actual min/max voltage values
- Internal Resistance R01-R14: `0.001 * Byte` (0-10 Ohm)

---

## Priority 4: Individual Block Voltages (Blocks 01-14)

### Current Status
- **NO UI element**
- **NO state field**

### Required Implementation
Send OBD2 query to **ECU 0x7E2** with **PID 21CE**

#### Response contains:
| Parameter | Formula | Range |
|-----------|---------|-------|
| Block Voltages (1-14) | `(256*HighByte + LowByte)/100 - 327.68` | 0-18 V |
| HV Battery Current | `(256*B+C)/100 - 327.68` | -100 to 100 A |
| Battery Power | `(256*D+E)/100 - 327.68` | -27 to 27 kW |

---

## Implementation Strategy

### Phase 1: Gateway Protocol Extension
1. Add `obd2_query` command type to gateway protocol
2. Implement request/response correlation (sequence IDs)
3. Handle multi-frame ISO 15765-2 responses

### Phase 2: Periodic Queries
1. Create query scheduler (avoid bus congestion)
2. Suggested polling intervals:
   - Inverter temps: Every 1-2 seconds
   - Delta SOC: Every 5 seconds
   - Block voltages: Every 10 seconds (or on-demand)

### Phase 3: Response Parsing
1. Implement multi-frame message assembly
2. Parse each PID response format
3. Dispatch state actions

---

## References
- [docs/prius_can.md](prius_can.md) - Full PID documentation
- Section 6: "Solicited (CAN) - Hybrid/Specific (ECU 07E2)"
- Section 7: "Solicited (CAN) - HV Battery (ECU 07E2)"
