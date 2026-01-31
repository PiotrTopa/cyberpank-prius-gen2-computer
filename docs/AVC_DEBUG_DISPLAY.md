# AVC-LAN Debug Display

## Overview
Real-time debug overlay showing raw AVC-LAN message bytes for manual correlation with driving states.

## Purpose
Helps reverse-engineer AVC-LAN energy flow messages by:
- Displaying raw bytes in real-time during driving/replay
- Highlighting discriminating bytes identified by analysis
- Allowing manual visual correlation between byte patterns and vehicle behavior

## Usage

### Enable Debug Display
Run the application in development mode:
```bash
python -m cyberpunk_computer --dev --scale 2
```

Or with replay:
```bash
python -m cyberpunk_computer --replay assets/data/full.ndjson --dev --scale 2
```

The debug overlay appears automatically in the **top-right corner** of the center area when dev mode is active.

### Display Contents

#### 110→490 (MFD Status - Flow Arrows)
- **All 8 bytes**: Raw message data in hex
- **Key bytes highlighted**: [1], [2], [3], [5] - identified as best discriminators
- **Update rate**: ~1 Hz (every MFD status message)

**Example:**
```
110→490: 00 46 C1 80 00 00 04 85
         [1]=46 [2]=C1 [3]=80 [5]=00
```

#### A00→258 (SOC/Energy Broadcast)
- **First 8 bytes**: Header and control data
- **SOC bytes highlighted**: [17], [21], [23] - track battery state of charge
- **Update rate**: ~0.05 Hz (every 20 seconds)

**Example:**
```
A00→258: 02 01 05 00 20 30 06 00
         [17]=3C [21]=42 [23]=FF
```

## Known Byte Patterns

### 110→490 Flow States
Based on analysis of 20-minute recording (see `decode_flow_arrows.py`):

| Flow State | Byte[1] | Byte[2] | Byte[3] | Byte[5] |
|------------|---------|---------|---------|---------|
| **ICE→BATT** | 00 | 00 | 80 | 00 |
| **BATT→WHEELS** | 46 | C1 | 80 | 00 |
| **NO_FLOW** | 00 | 00 | 08 | 04 |
| **ICE→WHEELS** | ? | ? | ? | ? |
| **WHEELS→BATT** | ? | ? | ? | ? |

**Missing states** (ICE→WHEELS, WHEELS→BATT) need capture during:
- Highway cruising (ICE→WHEELS)
- Regenerative braking (WHEELS→BATT)

### A00→258 SOC Encoding
Based on Pearson correlation analysis (see `decode_avc_energy.py`):

| Byte | Correlation | Purpose |
|------|-------------|---------|
| 17 | +0.92 | Primary SOC indicator (tracks changes perfectly) |
| 21 | +0.83 | Secondary SOC or cell balance |
| 23 | +0.70 | SOC-related state |
| 25 | +0.66 | SOC-related state |

## Manual Correlation Method

### During Driving
1. Enable dev mode display
2. Watch byte values during different states:
   - **Accelerating**: ICE running, battery discharging → ICE→BATT, BATT→WHEELS
   - **Cruising**: ICE running, light load → ICE→WHEELS (unknown pattern)
   - **Braking**: MG1/MG2 regenerating → WHEELS→BATT (unknown pattern)
   - **Stopped**: Engine charging battery → ICE→BATT

3. Note byte patterns for each state
4. Update correlation table above

### Pattern Recognition Tips
- **Byte[1,2]**: `46 C1` strongly indicates battery discharging to wheels
- **Byte[3]**: `80` appears during active power flow
- **Byte[5]**: Changes between flow states (best discriminator)
- **Multiple arrows**: Values can combine (ICE→BATT + BATT→WHEELS)

## Architecture

### Data Flow
```
AVC-LAN Message (0x110→0x490 or 0xA00→0x258)
    ↓
ingress.py: Create AVCDebugBytesAction
    ↓
store.py: Update InputState.last_avc_*_bytes
    ↓
main_screen.py: Render debug overlay
```

### Files Modified
- **state/app_state.py**: Added `last_avc_110_490_bytes`, `last_avc_a00_258_bytes` to `InputState`
- **state/actions.py**: Added `AVCDebugBytesAction` and `AVC_DEBUG_BYTES` action type
- **state/store.py**: Reducer for `AVC_DEBUG_BYTES` action
- **io/ingress.py**: Capture and dispatch debug bytes for target messages
- **ui/screens/main_screen.py**: Render `_render_avc_lan_debug()` overlay

## Analysis Tools
Complementary tools for offline analysis:
- **correlate_energy.py**: Find AVC messages correlated with CAN energy events
- **decode_avc_energy.py**: Statistical correlation (Pearson R, change correlation)
- **decode_flow_arrows.py**: Discrete flow state pattern matching

## Future Work
- [ ] Capture ICE→WHEELS pattern during highway driving
- [ ] Capture WHEELS→BATT pattern during regen braking
- [ ] Decode power magnitude encoding in A00→258
- [ ] Add interactive byte inspection (click to highlight)
- [ ] Log correlation table (byte pattern → observed state)
