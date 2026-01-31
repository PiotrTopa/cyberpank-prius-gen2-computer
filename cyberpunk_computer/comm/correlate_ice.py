#!/usr/bin/env python3
"""
Quick ICE status and power flow correlation tool.

Focuses on ICE ON/OFF events and large battery power changes to find
AVC-LAN messages that carry this data to the MFD.

Usage:
    python -m cyberpunk_computer.comm.correlate_ice assets/data/full.ndjson
"""

import json
import sys
from collections import defaultdict, Counter
from pathlib import Path
from typing import List, Dict, Tuple

from .avc_decoder import AVCDecoder
from .can_decoder import CANDecoder


def main() -> None:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python -m cyberpunk_computer.comm.correlate_ice <log_file.ndjson>")
        sys.exit(1)
    
    log_path = Path(sys.argv[1])
    if not log_path.exists():
        print(f"Error: File not found: {log_path}")
        sys.exit(1)
    
    avc_decoder = AVCDecoder()
    can_decoder = CANDecoder()
    
    # Data storage
    ice_events: List[Tuple[int, bool]] = []  # (timestamp, is_running)
    avc_by_time: Dict[int, List] = defaultdict(list)  # timestamp -> [(master, slave, data)]
    
    print(f"Loading {log_path}...")
    
    with open(log_path, 'r', encoding='utf-8') as f:
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
                    avc_msg = avc_decoder.decode_message(raw_msg)
                    if avc_msg:
                        avc_by_time[timestamp].append((
                            avc_msg.master_addr,
                            avc_msg.slave_addr,
                            avc_msg.data[:8]  # First 8 bytes
                        ))
                
                # CAN message (id=1) - looking for ICE status
                elif msg_id == 1:
                    can_msg = can_decoder.decode(raw_msg)
                    if can_msg and can_msg.can_id == 0x030:  # ICE status message
                        if 'ice_running' in can_msg.values:
                            ice_running = can_msg.values['ice_running']
                            # Only record state changes
                            if not ice_events or ice_events[-1][1] != ice_running:
                                ice_events.append((timestamp, ice_running))
                            
            except json.JSONDecodeError:
                continue
    
    print(f"Found {len(ice_events)} ICE state changes")
    print(f"Loaded {len(avc_by_time)} AVC-LAN timestamps")
    
    # Analyze each ICE event
    print("\n" + "=" * 80)
    print("ICE STATUS CHANGES AND NEARBY AVC-LAN MESSAGES")
    print("=" * 80)
    
    time_window_ms = 1000  # 1 second window
    
    # Track which AVC-LAN message patterns appear near ICE events
    pattern_counter = Counter()
    
    for event_ts, ice_running in ice_events:
        status = "ON " if ice_running else "OFF"
        print(f"\n[{event_ts:7d}ms] ICE → {status}")
        
        # Find AVC-LAN messages within time window
        nearby = []
        for ts in range(event_ts - time_window_ms, event_ts + time_window_ms + 1):
            if ts in avc_by_time:
                for master, slave, data in avc_by_time[ts]:
                    dt = ts - event_ts
                    nearby.append((dt, master, slave, data))
        
        if not nearby:
            print("  (no AVC-LAN messages nearby)")
            continue
        
        # Group by address pair
        by_pair = defaultdict(list)
        for dt, master, slave, data in nearby:
            by_pair[(master, slave)].append((dt, data))
        
        # Show results
        for (master, slave), messages in sorted(by_pair.items()):
            master_name = avc_decoder._get_device_name(master)
            slave_name = avc_decoder._get_device_name(slave)
            
            print(f"  {master_name:15} → {slave_name:15} ({len(messages)}x within ±{time_window_ms}ms)")
            
            # Track patterns for frequency analysis
            pattern_key = (master, slave, status)
            pattern_counter[pattern_key] += 1
            
            # Show up to 3 examples with timing
            for dt, data in messages[:3]:
                hex_data = " ".join(f"{b:02X}" for b in data)
                timing = f"+{dt}ms" if dt >= 0 else f"{dt}ms"
                print(f"    [{timing:6}] {hex_data}")
    
    # Summary of most common correlations
    print("\n" + "=" * 80)
    print("MOST FREQUENT AVC-LAN PATTERNS NEAR ICE EVENTS")
    print("=" * 80)
    print("\nAddress pairs that consistently appear near ICE state changes:\n")
    
    for (master, slave, status), count in pattern_counter.most_common(15):
        master_name = avc_decoder._get_device_name(master)
        slave_name = avc_decoder._get_device_name(slave)
        ice_events_count = sum(1 for _, s in ice_events if (s and status == "ON ") or (not s and status == "OFF"))
        percent = (count / ice_events_count * 100) if ice_events_count > 0 else 0
        print(f"  {status} | {master_name:15} → {slave_name:15} : {count:3}x ({percent:5.1f}%)")


if __name__ == "__main__":
    main()
