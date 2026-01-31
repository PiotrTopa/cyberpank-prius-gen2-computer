#!/usr/bin/env python3
"""
AVC-LAN and CAN Energy Data Correlator.

Analyzes simultaneous AVC-LAN and CAN recordings to find correlations
between energy flow data (battery power, SOC, ICE status) visible on CAN bus
and potential AVC-LAN messages carrying same data to the MFD display.

Usage:
    python -m cyberpunk_computer.comm.correlate_energy assets/data/full.ndjson
"""

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional, List, Dict, Tuple

from .avc_decoder import AVCDecoder, AVCMessage
from .can_decoder import CANDecoder, CANMessage


class EnergyCorrelator:
    """Correlates CAN and AVC-LAN energy data."""
    
    def __init__(self):
        self.avc_decoder = AVCDecoder()
        self.can_decoder = CANDecoder()
        self.avc_messages: List[Tuple[int, AVCMessage]] = []  # (timestamp, msg)
        self.can_energy_timeline: List[Tuple[int, Dict]] = []  # (timestamp, energy_data)
        
    def load_messages(self, path: Path) -> None:
        """Load both CAN and AVC-LAN messages from NDJSON file."""
        print(f"Loading {path}...")
        avc_count = 0
        can_count = 0
        
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('>>>') or line.startswith('MPY:'):
                    continue
                if line.startswith('('):  # Comments
                    continue
                    
                try:
                    raw_msg = json.loads(line)
                    msg_id = raw_msg.get("id")
                    timestamp = raw_msg.get("ts", 0)
                    
                    # AVC-LAN message (id=2)
                    if msg_id == 2:
                        avc_msg = self.avc_decoder.decode_message(raw_msg)
                        if avc_msg:
                            self.avc_messages.append((timestamp, avc_msg))
                            avc_count += 1
                    
                    # CAN message (id=1)
                    elif msg_id == 1:
                        can_msg = self.can_decoder.decode(raw_msg)
                        if can_msg:
                            # Extract energy-related data
                            energy_data = self._extract_energy_data(can_msg)
                            if energy_data:
                                self.can_energy_timeline.append((timestamp, energy_data))
                            can_count += 1
                            
                except json.JSONDecodeError:
                    continue
        
        print(f"Loaded {avc_count} AVC-LAN messages, {can_count} CAN messages")
        print(f"Extracted {len(self.can_energy_timeline)} CAN energy data points")
    
    def _extract_energy_data(self, can_msg: CANMessage) -> Optional[Dict]:
        """Extract energy-relevant data from CAN message."""
        data = {}
        
        # Battery SOC (0x3CB or 0x3C8)
        if can_msg.can_id in [0x3CB, 0x3C8]:
            if 'soc' in can_msg.values:
                data['soc'] = can_msg.values['soc']
        
        # Battery Power (0x3CB)
        if can_msg.can_id == 0x3CB:
            if 'battery_power_kw' in can_msg.values:
                data['battery_power_kw'] = can_msg.values['battery_power_kw']
        
        # ICE Status (0x030)
        if can_msg.can_id == 0x030:
            if 'ice_running' in can_msg.values:
                data['ice_running'] = can_msg.values['ice_running']
        
        # Engine RPM (0x038)
        if can_msg.can_id == 0x038:
            if 'rpm' in can_msg.values:
                data['ice_rpm'] = can_msg.values['rpm']
        
        # Fuel consumption (0x520)
        if can_msg.can_id == 0x520:
            if 'injector_time' in can_msg.values:
                data['fuel_injector_time'] = can_msg.values['injector_time']
        
        return data if data else None
    
    def find_correlations(self, time_window_ms: int = 500) -> None:
        """
        Find AVC-LAN messages that correlate with CAN energy data changes.
        
        Args:
            time_window_ms: Time window to consider messages "simultaneous"
        """
        print("\n" + "=" * 70)
        print("CORRELATION ANALYSIS: CAN Energy Data vs AVC-LAN Messages")
        print("=" * 70)
        
        # Track significant CAN energy changes
        significant_events = []
        prev_energy = {}
        
        for ts, energy_data in self.can_energy_timeline:
            # Check for significant changes
            changed = False
            changes = []
            
            if 'soc' in energy_data:
                if 'soc' not in prev_energy or abs(energy_data['soc'] - prev_energy['soc']) >= 1.0:
                    changes.append(f"SOC: {prev_energy.get('soc', '?')} -> {energy_data['soc']:.1f}%")
                    changed = True
            
            if 'battery_power_kw' in energy_data:
                if 'battery_power_kw' not in prev_energy:
                    changes.append(f"BattPwr: {energy_data['battery_power_kw']:.1f} kW")
                    changed = True
                elif abs(energy_data['battery_power_kw'] - prev_energy['battery_power_kw']) > 5.0:
                    changes.append(f"BattPwr: {prev_energy['battery_power_kw']:.1f} -> {energy_data['battery_power_kw']:.1f} kW")
                    changed = True
            
            if 'ice_running' in energy_data:
                if 'ice_running' not in prev_energy or energy_data['ice_running'] != prev_energy['ice_running']:
                    changes.append(f"ICE: {'ON' if energy_data['ice_running'] else 'OFF'}")
                    changed = True
            
            if changed:
                significant_events.append((ts, energy_data, changes))
            
            prev_energy = energy_data.copy()
        
        print(f"\nFound {len(significant_events)} significant CAN energy events")
        
        # Filter for ICE status changes (most visible correlation)
        ice_events = [(ts, data, chg) for ts, data, chg in significant_events 
                      if any('ICE:' in c for c in chg)]
        
        print(f"\nFiltered to {len(ice_events)} ICE status change events")
        print(f"\nSearching for correlated AVC-LAN messages (±{time_window_ms}ms window):\n")
        
        # Show all ICE events + first 30 other events
        events_to_show = ice_events + [e for e in significant_events if e not in ice_events][:30]
        
        for event_ts, event_data, changes in events_to_show:
            print(f"\n{'─' * 70}")
            print(f"CAN Event @ {event_ts}ms: {', '.join(changes)}")
            
            # Find AVC-LAN messages within time window
            nearby_avc = []
            for avc_ts, avc_msg in self.avc_messages:
                if abs(avc_ts - event_ts) <= time_window_ms:
                    nearby_avc.append((avc_ts, avc_msg))
            
            if nearby_avc:
                print(f"  → {len(nearby_avc)} AVC-LAN messages nearby:")
                # Group by address pair
                by_pair = defaultdict(list)
                for avc_ts, avc_msg in nearby_avc:
                    key = (avc_msg.master_addr, avc_msg.slave_addr)
                    by_pair[key].append((avc_ts, avc_msg))
                
                for (master, slave), msgs in sorted(by_pair.items()):
                    master_name = self.avc_decoder._get_device_name(master)
                    slave_name = self.avc_decoder._get_device_name(slave)
                    print(f"    {master_name:15} → {slave_name:15} ({len(msgs)}x)")
                    
                    # Show first message data
                    _, first_msg = msgs[0]
                    print(f"      Data: {first_msg.data_hex()[:60]}")
    
    def analyze_candidate_messages(self) -> None:
        """Analyze the most promising AVC-LAN message candidates for energy data."""
        print("\n" + "=" * 70)
        print("CANDIDATE MESSAGE ANALYSIS")
        print("=" * 70)
        
        candidates = [
            (0xA00, 0x258),  # Broadcast -> Display
            (0x210, 0x490),  # Known ICE status
            (0x110, 0x490),  # MFD -> System
        ]
        
        for master, slave in candidates:
            master_name = self.avc_decoder._get_device_name(master)
            slave_name = self.avc_decoder._get_device_name(slave)
            print(f"\n{master_name} (0x{master:03X}) → {slave_name} (0x{slave:03X})")
            print("─" * 70)
            
            # Find all messages for this pair
            messages = []
            for ts, msg in self.avc_messages:
                if msg.master_addr == master and msg.slave_addr == slave:
                    messages.append((ts, msg))
            
            print(f"Total messages: {len(messages)}")
            
            if not messages:
                continue
            
            # Analyze data byte variability
            if messages:
                byte_variability = [set() for _ in range(32)]  # Max 32 bytes
                
                for ts, msg in messages:
                    for i, byte_val in enumerate(msg.data):
                        if i < len(byte_variability):
                            byte_variability[i].add(byte_val)
                
                # Show which bytes vary
                print(f"\nByte variability (unique values per byte):")
                for i, unique_vals in enumerate(byte_variability):
                    if unique_vals:
                        count = len(unique_vals)
                        if count > 1:  # Only show varying bytes
                            vals_str = ", ".join(f"{v:02X}" for v in sorted(unique_vals)[:10])
                            if count > 10:
                                vals_str += f"... ({count} total)"
                            print(f"  Byte[{i:2d}]: {count:3d} unique values [{vals_str}]")
            
            # Show a few example messages with timestamps
            print(f"\nExample messages (first 5):")
            for ts, msg in messages[:5]:
                print(f"  {ts:7d}ms: {msg.data_hex()[:60]}")


def main() -> None:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python -m cyberpunk_computer.comm.correlate_energy <log_file.ndjson>")
        sys.exit(1)
    
    log_path = Path(sys.argv[1])
    if not log_path.exists():
        print(f"Error: File not found: {log_path}")
        sys.exit(1)
    
    correlator = EnergyCorrelator()
    correlator.load_messages(log_path)
    correlator.find_correlations(time_window_ms=500)
    correlator.analyze_candidate_messages()
    
    print("\n" + "=" * 70)
    print("Analysis complete!")
    print("=" * 70)


if __name__ == "__main__":
    main()
