#!/usr/bin/env python3
"""
AVC-LAN Energy Data Deep Decoder.

Performs byte-level correlation analysis between CAN energy values
and AVC-LAN message bytes to reverse engineer the encoding scheme.

Usage:
    python -m cyberpunk_computer.comm.decode_avc_energy assets/data/full.ndjson
"""

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import List, Dict, Tuple, Optional
import struct

from .avc_decoder import AVCDecoder
from .can_decoder import CANDecoder


class EnergyDecoder:
    """Deep decoder for AVC-LAN energy data."""
    
    def __init__(self):
        self.avc_decoder = AVCDecoder()
        self.can_decoder = CANDecoder()
        
        # Timeline data
        self.can_timeline: List[Tuple[int, Dict]] = []  # (ts, {soc, power, rpm, ...})
        self.avc_a00_timeline: List[Tuple[int, List[int]]] = []  # (ts, 32-byte data)
        self.avc_110_timeline: List[Tuple[int, List[int]]] = []  # (ts, 8-byte data)
        
    def load_data(self, path: Path) -> None:
        """Load and organize CAN and AVC-LAN data."""
        print(f"Loading {path}...")
        
        can_count = 0
        avc_a00_count = 0
        avc_110_count = 0
        
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
                            if len(avc_msg.data) >= 20:  # Need substantial data
                                self.avc_a00_timeline.append((timestamp, avc_msg.data))
                                avc_a00_count += 1
                        
                        # 110 -> 490 (MFD status)
                        elif avc_msg.master_addr == 0x110 and avc_msg.slave_addr == 0x490:
                            if len(avc_msg.data) >= 4:
                                self.avc_110_timeline.append((timestamp, avc_msg.data[:8]))
                                avc_110_count += 1
                    
                    # CAN message (id=1)
                    elif msg_id == 1:
                        can_msg = self.can_decoder.decode(raw_msg)
                        if not can_msg:
                            continue
                        
                        # Collect energy data from various CAN messages
                        energy_data = {}
                        
                        # Battery SOC (0x3C8 or 0x3CB)
                        if can_msg.can_id in [0x3C8, 0x3CB]:
                            if 'soc' in can_msg.values:
                                energy_data['soc'] = can_msg.values['soc']
                        
                        # Battery Power (0x03B - calculated from current & voltage)
                        if can_msg.can_id == 0x03B:
                            if 'power_kw' in can_msg.values:
                                energy_data['battery_power_kw'] = can_msg.values['power_kw']
                            if 'current' in can_msg.values:
                                energy_data['battery_current_a'] = can_msg.values['current']
                            if 'voltage' in can_msg.values:
                                energy_data['battery_voltage_v'] = can_msg.values['voltage']
                        
                        # Engine RPM (0x38 or 0x39)
                        if can_msg.can_id in [0x38, 0x39]:
                            if 'rpm' in can_msg.values:
                                energy_data['ice_rpm'] = can_msg.values['rpm']
                        
                        # Fuel consumption (0x520)
                        if can_msg.can_id == 0x520:
                            if 'injector_time' in can_msg.values:
                                energy_data['fuel_injector'] = can_msg.values['injector_time']
                        
                        # Speed (0x3A or 0xB4)
                        if can_msg.can_id in [0x3A, 0xB4]:
                            if 'speed_kmh' in can_msg.values:
                                energy_data['speed_kmh'] = can_msg.values['speed_kmh']
                        
                        if energy_data:
                            # Merge with existing entry at this timestamp if exists
                            if self.can_timeline and self.can_timeline[-1][0] == timestamp:
                                self.can_timeline[-1][1].update(energy_data)
                            else:
                                self.can_timeline.append((timestamp, energy_data))
                            can_count += 1
                            
                except (json.JSONDecodeError, KeyError):
                    continue
        
        print(f"Loaded:")
        print(f"  CAN energy data: {len(self.can_timeline)} timestamps")
        print(f"  AVC 0xA00→0x258: {avc_a00_count} messages")
        print(f"  AVC 0x110→0x490: {avc_110_count} messages")
    
    def find_matching_avc_message(
        self,
        can_ts: int,
        avc_timeline: List[Tuple[int, List[int]]],
        time_window_ms: int = 200
    ) -> Optional[Tuple[int, List[int]]]:
        """Find AVC message closest to CAN timestamp within window."""
        best_match = None
        best_delta = time_window_ms + 1
        
        for avc_ts, avc_data in avc_timeline:
            delta = abs(avc_ts - can_ts)
            if delta < best_delta:
                best_delta = delta
                best_match = (avc_ts, avc_data)
        
        return best_match if best_delta <= time_window_ms else None
    
    def correlate_soc_encoding(self) -> None:
        """Correlate CAN SOC values with AVC-LAN bytes."""
        print("\n" + "=" * 80)
        print("SOC ENCODING CORRELATION (CAN vs AVC-LAN)")
        print("=" * 80)
        
        # Collect SOC correlation samples
        soc_samples_a00: List[Tuple[float, List[int]]] = []
        soc_samples_110: List[Tuple[float, List[int]]] = []
        
        for can_ts, can_data in self.can_timeline:
            if 'soc' not in can_data:
                continue
            
            soc = can_data['soc']
            
            # Find matching A00 message
            match_a00 = self.find_matching_avc_message(can_ts, self.avc_a00_timeline)
            if match_a00:
                _, avc_data = match_a00
                soc_samples_a00.append((soc, avc_data))
            
            # Find matching 110 message
            match_110 = self.find_matching_avc_message(can_ts, self.avc_110_timeline)
            if match_110:
                _, avc_data = match_110
                soc_samples_110.append((soc, avc_data))
        
        print(f"\nCollected {len(soc_samples_a00)} SOC correlation samples for A00→258")
        print(f"Collected {len(soc_samples_110)} SOC correlation samples for 110→490")
        
        # Analyze A00 bytes
        if soc_samples_a00:
            print("\n" + "-" * 80)
            print("Analyzing 0xA00→0x258 bytes for SOC correlation:")
            print("-" * 80)
            self._analyze_byte_correlation(soc_samples_a00, "SOC %", max_bytes=32)
        
        # Analyze 110 bytes
        if soc_samples_110:
            print("\n" + "-" * 80)
            print("Analyzing 0x110→0x490 bytes for SOC correlation:")
            print("-" * 80)
            self._analyze_byte_correlation(soc_samples_110, "SOC %", max_bytes=8)
    
    def correlate_power_encoding(self) -> None:
        """Correlate CAN battery power with AVC-LAN bytes."""
        print("\n" + "=" * 80)
        print("BATTERY POWER ENCODING CORRELATION (CAN vs AVC-LAN)")
        print("=" * 80)
        
        # Collect power correlation samples
        power_samples_a00: List[Tuple[float, List[int]]] = []
        power_samples_110: List[Tuple[float, List[int]]] = []
        
        for can_ts, can_data in self.can_timeline:
            if 'battery_power_kw' not in can_data:
                continue
            
            power = can_data['battery_power_kw']
            
            # Find matching A00 message
            match_a00 = self.find_matching_avc_message(can_ts, self.avc_a00_timeline)
            if match_a00:
                _, avc_data = match_a00
                power_samples_a00.append((power, avc_data))
            
            # Find matching 110 message
            match_110 = self.find_matching_avc_message(can_ts, self.avc_110_timeline)
            if match_110:
                _, avc_data = match_110
                power_samples_110.append((power, avc_data))
        
        print(f"\nCollected {len(power_samples_a00)} power samples for A00→258")
        print(f"Collected {len(power_samples_110)} power samples for 110→490")
        
        # Analyze A00 bytes
        if power_samples_a00:
            print("\n" + "-" * 80)
            print("Analyzing 0xA00→0x258 bytes for battery power correlation:")
            print("-" * 80)
            self._analyze_byte_correlation(power_samples_a00, "Battery Power (kW)", max_bytes=32)
        
        # Analyze 110 bytes
        if power_samples_110:
            print("\n" + "-" * 80)
            print("Analyzing 0x110→0x490 bytes for battery power correlation:")
            print("-" * 80)
            self._analyze_byte_correlation(power_samples_110, "Battery Power (kW)", max_bytes=8)
    
    def _analyze_byte_correlation(
        self,
        samples: List[Tuple[float, List[int]]],
        value_name: str,
        max_bytes: int = 32
    ) -> None:
        """Analyze correlation between a value and AVC bytes."""
        if len(samples) < 5:
            print(f"  Insufficient samples ({len(samples)})")
            return
        
        # Calculate correlation for each byte position
        correlations = []
        
        for byte_idx in range(max_bytes):
            # Extract byte values and corresponding CAN values
            pairs = []
            for can_value, avc_data in samples:
                if byte_idx < len(avc_data):
                    pairs.append((can_value, avc_data[byte_idx]))
            
            if len(pairs) < 5:
                continue
            
            # Check variability
            byte_values = [b for _, b in pairs]
            unique_bytes = len(set(byte_values))
            
            if unique_bytes < 2:  # Skip constant bytes
                continue
            
            # Calculate linear correlation coefficient
            can_vals = [c for c, _ in pairs]
            byte_vals = [b for _, b in pairs]
            
            correlation = self._pearson_correlation(can_vals, byte_vals)
            
            # Also check if byte changes when CAN value changes significantly
            change_correlation = self._change_correlation(pairs)
            
            correlations.append((byte_idx, correlation, change_correlation, unique_bytes))
        
        # Sort by absolute correlation
        correlations.sort(key=lambda x: abs(x[1]), reverse=True)
        
        # Show top candidates
        print(f"\nTop byte positions correlated with {value_name}:")
        print(f"{'Byte':>6} | {'Linear R':>9} | {'Change R':>9} | {'Unique':>7} | Notes")
        print("-" * 70)
        
        for byte_idx, corr, change_corr, unique in correlations[:15]:
            notes = []
            if abs(corr) > 0.7:
                notes.append("STRONG")
            elif abs(corr) > 0.5:
                notes.append("moderate")
            if abs(change_corr) > 0.6:
                notes.append("tracks changes")
            
            notes_str = ", ".join(notes) if notes else ""
            print(f"  [{byte_idx:2d}]  | {corr:+8.4f}  | {change_corr:+8.4f}  | {unique:6d}  | {notes_str}")
        
        # Show some example mappings for top candidate
        if correlations:
            best_byte, best_corr, _, _ = correlations[0]
            if abs(best_corr) > 0.3:
                print(f"\nExample {value_name} ↔ Byte[{best_byte}] mappings:")
                # Show diverse examples
                samples_sorted = sorted(samples, key=lambda x: x[0])
                step = max(1, len(samples_sorted) // 10)
                for i in range(0, len(samples_sorted), step):
                    can_val, avc_data = samples_sorted[i]
                    if best_byte < len(avc_data):
                        byte_val = avc_data[best_byte]
                        print(f"  {value_name} = {can_val:7.2f}  →  Byte[{best_byte}] = 0x{byte_val:02X} ({byte_val:3d})")
    
    def _pearson_correlation(self, x: List[float], y: List[int]) -> float:
        """Calculate Pearson correlation coefficient."""
        if len(x) != len(y) or len(x) < 2:
            return 0.0
        
        n = len(x)
        sum_x = sum(x)
        sum_y = sum(y)
        sum_xy = sum(xi * yi for xi, yi in zip(x, y))
        sum_x2 = sum(xi * xi for xi in x)
        sum_y2 = sum(yi * yi for yi in y)
        
        numerator = n * sum_xy - sum_x * sum_y
        denominator = ((n * sum_x2 - sum_x * sum_x) * (n * sum_y2 - sum_y * sum_y)) ** 0.5
        
        if denominator == 0:
            return 0.0
        
        return numerator / denominator
    
    def _change_correlation(self, pairs: List[Tuple[float, int]]) -> float:
        """Measure how well byte changes track CAN value changes."""
        if len(pairs) < 3:
            return 0.0
        
        # Count when both change vs when only one changes
        both_change = 0
        either_change = 0
        
        for i in range(1, len(pairs)):
            can_prev, byte_prev = pairs[i-1]
            can_curr, byte_curr = pairs[i]
            
            can_changed = abs(can_curr - can_prev) > 0.1
            byte_changed = byte_curr != byte_prev
            
            if can_changed or byte_changed:
                either_change += 1
                if can_changed and byte_changed:
                    both_change += 1
        
        return both_change / either_change if either_change > 0 else 0.0


def main() -> None:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python -m cyberpunk_computer.comm.decode_avc_energy <log_file.ndjson>")
        sys.exit(1)
    
    log_path = Path(sys.argv[1])
    if not log_path.exists():
        print(f"Error: File not found: {log_path}")
        sys.exit(1)
    
    decoder = EnergyDecoder()
    decoder.load_data(log_path)
    
    # Run correlation analyses
    decoder.correlate_soc_encoding()
    decoder.correlate_power_encoding()
    
    print("\n" + "=" * 80)
    print("Analysis complete!")
    print("=" * 80)
    print("\nLook for bytes with:")
    print("  - Linear R > 0.5 (moderate) or > 0.7 (strong)")
    print("  - Change R > 0.6 (tracks value changes)")
    print("  - High unique value count (many different states)")


if __name__ == "__main__":
    main()
