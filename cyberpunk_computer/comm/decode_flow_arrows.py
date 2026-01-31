#!/usr/bin/env python3
"""
AVC-LAN Energy Flow Arrow Decoder.

Maps discrete energy flow states (ICE→Battery, Battery→MG, MG→Battery)
from CAN data to AVC-LAN byte patterns. Since the MFD displays the same
flow arrows as our VFD, the AVC-LAN must contain these discrete states.

Usage:
    python -m cyberpunk_computer.comm.decode_flow_arrows assets/data/full.ndjson
"""

import json
import sys
from collections import defaultdict, Counter
from pathlib import Path
from typing import List, Dict, Tuple, Optional

from .avc_decoder import AVCDecoder
from .can_decoder import CANDecoder


class FlowState:
    """Energy flow state calculated from CAN data (matching MFD display states)."""
    
    def __init__(self):
        self.ice_to_battery: bool = False   # Engine charging battery
        self.ice_to_wheels: bool = False    # Engine powering wheels directly
        self.wheels_to_battery: bool = False  # Regenerative braking
        self.battery_to_wheels: bool = False  # Battery assist
        self.ice_running: bool = False
        self.soc: float = 0.0
    
    def __str__(self) -> str:
        """Human-readable flow state."""
        arrows = []
        if self.ice_to_battery:
            arrows.append("ICE→BATT")
        if self.ice_to_wheels:
            arrows.append("ICE→WHLS")
        if self.battery_to_wheels:
            arrows.append("BATT→WHLS")
        if self.wheels_to_battery:
            arrows.append("WHLS→BATT")
        
        if not arrows:
            return f"[SOC:{self.soc:.1f}% ICE:{'ON' if self.ice_running else 'OFF'}] NO_FLOW"
        
        return f"[SOC:{self.soc:.1f}% ICE:{'ON' if self.ice_running else 'OFF'}] {' + '.join(arrows)}"
    
    def to_tuple(self) -> Tuple[bool, bool, bool, bool]:
        """Return as hashable tuple for grouping."""
        return (self.ice_to_battery, self.ice_to_wheels, self.battery_to_wheels, self.wheels_to_battery)
    
    @staticmethod
    def from_can_data(
        ice_power_kw: float,
        mg_power_kw: float,
        brake_regen_kw: float,
        ice_running: bool,
        speed_kmh: float,
        soc: float
    ) -> 'FlowState':
        """
        Calculate flow state from CAN data matching MFD display logic.
        
        MFD Flow States:
        - ICE → Battery: Engine charging battery (stopped/parked or low power demand)
        - ICE → Wheels: Engine driving wheels directly (cruising, no battery assist)
        - Battery → Wheels: Battery assisting motor (acceleration, high demand)
        - Wheels → Battery: Regenerative braking (deceleration)
        """
        state = FlowState()
        state.ice_running = ice_running
        state.soc = soc
        
        # Wheels → Battery: Regen braking (only when moving)
        if (brake_regen_kw > 0 or mg_power_kw < -1) and speed_kmh > 1.0:
            state.wheels_to_battery = True
        
        # Battery → Wheels: Battery assist (positive MG power = motor drawing from battery)
        if mg_power_kw > 1.0:  # Threshold to avoid noise
            state.battery_to_wheels = True
        
        # ICE flows (only when engine running)
        if ice_running and ice_power_kw > 0:
            # ICE → Battery: Charging when stopped or very low assist
            # (engine on but car not moving much, or explicitly charging)
            if mg_power_kw < -1:  # Battery charging (negative = charging)
                state.ice_to_battery = True
            elif speed_kmh < 5.0:  # Stopped/creeping with engine on
                state.ice_to_battery = True
            
            # ICE → Wheels: Engine driving, not charging, minimal battery assist
            # This is the "cruising" state
            if not state.ice_to_battery and not state.battery_to_wheels:
                if speed_kmh > 5.0:  # Actually moving
                    state.ice_to_wheels = True
        
        return state


class FlowArrowDecoder:
    """Decoder for energy flow arrows in AVC-LAN."""
    
    def __init__(self):
        self.avc_decoder = AVCDecoder()
        self.can_decoder = CANDecoder()
        
        # Timeline: (timestamp, FlowState, {power values})
        self.flow_timeline: List[Tuple[int, FlowState, Dict]] = []
        
        # AVC-LAN timelines
        self.avc_a00_timeline: List[Tuple[int, List[int]]] = []  # (ts, 32-byte data)
        self.avc_110_timeline: List[Tuple[int, List[int]]] = []  # (ts, 8-byte data)
    
    def load_data(self, path: Path) -> None:
        """Load CAN and AVC-LAN data, calculating flow states."""
        print(f"Loading {path}...")
        
        # Accumulator for CAN data at each timestamp
        can_buffer: Dict[int, Dict] = defaultdict(dict)
        
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(('>>>', 'MPY:', '(')):
                    continue
                
                try:
                    raw_msg = json.loads(line)
                    msg_id = raw_msg.get("id")
                    timestamp = raw_msg.get("ts", 0)
                    
                    # AVC-LAN message (id=2)
                    if msg_id == 2:
                        avc_msg = self.avc_decoder.decode_message(raw_msg)
                        if not avc_msg:
                            continue
                        
                        # A00 -> 258 (32-byte energy data candidate)
                        if avc_msg.master_addr == 0xA00 and avc_msg.slave_addr == 0x258:
                            if len(avc_msg.data) >= 20:
                                self.avc_a00_timeline.append((timestamp, avc_msg.data))
                        
                        # 110 -> 490 (MFD status)
                        elif avc_msg.master_addr == 0x110 and avc_msg.slave_addr == 0x490:
                            if len(avc_msg.data) >= 4:
                                self.avc_110_timeline.append((timestamp, avc_msg.data[:8]))
                    
                    # CAN message (id=1)
                    elif msg_id == 1:
                        can_msg = self.can_decoder.decode(raw_msg)
                        if not can_msg:
                            continue
                        
                        # Accumulate all energy-related values at this timestamp
                        if can_msg.can_id == 0x03B:  # Battery power
                            if 'power_kw' in can_msg.values:
                                can_buffer[timestamp]['mg_power_kw'] = can_msg.values['power_kw']
                        
                        elif can_msg.can_id in [0x3C8, 0x3CB]:  # SOC
                            if 'soc' in can_msg.values:
                                can_buffer[timestamp]['soc'] = can_msg.values['soc']
                        
                        elif can_msg.can_id in [0x38, 0x39]:  # Engine RPM
                            if 'rpm' in can_msg.values:
                                can_buffer[timestamp]['ice_rpm'] = can_msg.values['rpm']
                            if 'ice_running' in can_msg.values:
                                can_buffer[timestamp]['ice_running'] = can_msg.values['ice_running']
                        
                        elif can_msg.can_id == 0x520:  # Fuel consumption
                            if 'injector_time' in can_msg.values:
                                # Estimate ICE power from fuel: ~2.5 kW per L/h
                                # injector_time * 0.0067 = L/h
                                fuel_lh = can_msg.values['injector_time'] * 0.0067
                                can_buffer[timestamp]['ice_power_kw'] = fuel_lh * 2.5
                        
                        elif can_msg.can_id in [0x3A, 0xB4]:  # Speed
                            if 'speed_kmh' in can_msg.values:
                                can_buffer[timestamp]['speed_kmh'] = can_msg.values['speed_kmh']
                        
                        elif can_msg.can_id == 0x030:  # Brake
                            if 'brake' in can_msg.values:
                                # Estimate brake regen from brake pressure
                                brake = can_msg.values['brake']
                                can_buffer[timestamp]['brake_regen_kw'] = (brake / 127.0) * 15.0 if brake > 5 else 0.0
                
                except (json.JSONDecodeError, KeyError):
                    continue
        
        # Process accumulated CAN data into flow states
        prev_data = {}
        for timestamp in sorted(can_buffer.keys()):
            data = can_buffer[timestamp]
            
            # Merge with previous values (carry forward)
            merged = {**prev_data, **data}
            
            # Need minimum data to calculate flow
            if 'mg_power_kw' in merged and 'ice_running' in merged and 'soc' in merged:
                flow_state = FlowState.from_can_data(
                    ice_power_kw=merged.get('ice_power_kw', 0.0),
                    mg_power_kw=merged['mg_power_kw'],
                    brake_regen_kw=merged.get('brake_regen_kw', 0.0),
                    ice_running=merged['ice_running'],
                    speed_kmh=merged.get('speed_kmh', 0.0),
                    soc=merged['soc']
                )
                
                self.flow_timeline.append((timestamp, flow_state, merged))
            
            prev_data = merged
        
        print(f"Loaded:")
        print(f"  Flow states: {len(self.flow_timeline)} timestamps")
        print(f"  AVC 0xA00→0x258: {len(self.avc_a00_timeline)} messages")
        print(f"  AVC 0x110→0x490: {len(self.avc_110_timeline)} messages")
    
    def analyze_flow_correlations(self) -> None:
        """Find AVC-LAN byte patterns that match flow arrow states."""
        print("\n" + "=" * 80)
        print("ENERGY FLOW ARROW CORRELATION")
        print("=" * 80)
        
        # Group flow states and find matching AVC messages
        flow_samples_a00: Dict[Tuple[bool, bool, bool], List[List[int]]] = defaultdict(list)
        flow_samples_110: Dict[Tuple[bool, bool, bool], List[List[int]]] = defaultdict(list)
        
        time_window_ms = 200
        
        for can_ts, flow_state, _ in self.flow_timeline:
            flow_key = flow_state.to_tuple()
            
            # Find matching A00 message
            for avc_ts, avc_data in self.avc_a00_timeline:
                if abs(avc_ts - can_ts) <= time_window_ms:
                    flow_samples_a00[flow_key].append(avc_data)
                    break
            
            # Find matching 110 message
            for avc_ts, avc_data in self.avc_110_timeline:
                if abs(avc_ts - can_ts) <= time_window_ms:
                    flow_samples_110[flow_key].append(avc_data)
                    break
        
        # Analyze 0xA00→0x258
        print("\n" + "-" * 80)
        print("0xA00→0x258 Flow Arrow Patterns:")
        print("-" * 80)
        
        self._analyze_flow_patterns(flow_samples_a00, max_bytes=32)
        
        # Analyze 0x110→0x490
        print("\n" + "-" * 80)
        print("0x110→0x490 Flow Arrow Patterns:")
        print("-" * 80)
        
        self._analyze_flow_patterns(flow_samples_110, max_bytes=8)
    
    def _analyze_flow_patterns(
        self,
        flow_samples: Dict[Tuple[bool, bool, bool, bool], List[List[int]]],
        max_bytes: int
    ) -> None:
        """Analyze byte patterns for each flow state."""
        
        if not flow_samples:
            print("  No samples found")
            return
        
        # Show statistics for each flow state
        print(f"\nFlow state distribution:")
        for flow_key, samples in sorted(flow_samples.items(), key=lambda x: len(x[1]), reverse=True):
            ice_to_batt, ice_to_whls, batt_to_whls, whls_to_batt = flow_key
            arrows = []
            if ice_to_batt: arrows.append("ICE→BATT")
            if ice_to_whls: arrows.append("ICE→WHLS")
            if batt_to_whls: arrows.append("BATT→WHLS")
            if whls_to_batt: arrows.append("WHLS→BATT")
            state_str = " + ".join(arrows) if arrows else "NO_FLOW"
            print(f"  {state_str:35} : {len(samples):4} samples")
        
        # Find bytes that discriminate between flow states
        print(f"\nByte patterns that distinguish flow states:")
        
        discriminating_bytes = []
        
        for byte_idx in range(max_bytes):
            # For each flow state, collect the byte values
            state_byte_values: Dict[Tuple[bool, bool, bool, bool], set] = defaultdict(set)
            
            for flow_key, samples in flow_samples.items():
                for avc_data in samples:
                    if byte_idx < len(avc_data):
                        state_byte_values[flow_key].add(avc_data[byte_idx])
            
            # Check if this byte helps discriminate states
            # Count unique value sets
            unique_patterns = set()
            for flow_key, byte_vals in state_byte_values.items():
                unique_patterns.add(frozenset(byte_vals))
            
            # If we have multiple distinct patterns, this byte is useful
            if len(unique_patterns) > 1:
                # Calculate how well it discriminates
                total_states = len(state_byte_values)
                states_per_value: Dict[int, List] = defaultdict(list)
                
                for flow_key, byte_vals in state_byte_values.items():
                    for val in byte_vals:
                        states_per_value[val].append(flow_key)
                
                # A good discriminator has values unique to specific states
                discrimination_score = 0
                for val, states in states_per_value.items():
                    if len(states) == 1:  # This value uniquely identifies a state
                        discrimination_score += 1
                
                discriminating_bytes.append((byte_idx, discrimination_score, state_byte_values))
        
        # Sort by discrimination score
        discriminating_bytes.sort(key=lambda x: x[1], reverse=True)
        
        # Show top discriminators
        print(f"\n{'Byte':>6} | {'Score':>6} | Flow State → Byte Values")
        print("-" * 80)
        
        for byte_idx, score, state_values in discriminating_bytes[:20]:
            if score == 0:
                continue
            
            print(f"  [{byte_idx:2d}]  | {score:5d}  |")
            
            for flow_key, byte_vals in sorted(state_values.items()):
                ice_to_batt, ice_to_whls, batt_to_whls, whls_to_batt = flow_key
                arrows = []
                if ice_to_batt: arrows.append("I→B")
                if ice_to_whls: arrows.append("I→W")
                if batt_to_whls: arrows.append("B→W")
                if whls_to_batt: arrows.append("W→B")
                state_str = " + ".join(arrows) if arrows else "NONE"
                
                vals_str = ", ".join(f"0x{v:02X}" for v in sorted(byte_vals)[:8])
                if len(byte_vals) > 8:
                    vals_str += f"... ({len(byte_vals)} total)"
                
                print(f"         |        | {state_str:20} → {vals_str}")
        
        # Show example complete messages for key states
        print(f"\nExample complete messages for key flow states:")
        print("-" * 80)
        
        key_states = [
            ((True, False, False, False), "ICE→BATT"),
            ((False, True, False, False), "ICE→WHLS"),
            ((False, False, True, False), "BATT→WHLS"),
            ((False, False, False, True), "WHLS→BATT"),
            ((False, False, False, False), "NO_FLOW"),
        ]
        
        for flow_key, label in key_states:
            if flow_key in flow_samples and flow_samples[flow_key]:
                sample = flow_samples[flow_key][0]
                hex_str = " ".join(f"{b:02X}" for b in sample[:max_bytes])
                print(f"  {label:20} : {hex_str}")


def main() -> None:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python -m cyberpunk_computer.comm.decode_flow_arrows <log_file.ndjson>")
        sys.exit(1)
    
    log_path = Path(sys.argv[1])
    if not log_path.exists():
        print(f"Error: File not found: {log_path}")
        sys.exit(1)
    
    decoder = FlowArrowDecoder()
    decoder.load_data(log_path)
    decoder.analyze_flow_correlations()
    
    print("\n" + "=" * 80)
    print("Analysis complete!")
    print("=" * 80)
    print("\nLook for bytes with high discrimination scores that uniquely map to flow states.")


if __name__ == "__main__":
    main()
