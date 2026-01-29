#!/usr/bin/env python3
"""
Analyze specific CAN messages from NDJSON recording file.
Focus on 0x03B (battery current/voltage) and 0x3CB (SOC/temps) to verify decoding.
"""

import json
import sys
from collections import defaultdict


def analyze_can_message(file_path: str, target_ids: list[int] = None):
    """
    Analyze CAN messages to verify byte interpretations.
    
    Args:
        file_path: Path to NDJSON recording file
        target_ids: List of CAN IDs to analyze (hex integers)
    """
    if target_ids is None:
        target_ids = [0x03B, 0x3CB, 0x3C8, 0x540, 0x348, 0x038]
    
    samples = defaultdict(list)
    count = defaultdict(int)
    
    print(f"Analyzing CAN messages from: {file_path}")
    print(f"Looking for CAN IDs: {[hex(x) for x in target_ids]}\n")
    
    with open(file_path, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            
            # Only CAN messages (id=1)
            if msg.get("id") != 1:
                continue
            
            d = msg.get("d", {})
            can_id_str = d.get("i", "")
            data = d.get("d", [])
            
            try:
                can_id = int(can_id_str, 16)
            except ValueError:
                continue
            
            if can_id not in target_ids:
                continue
            
            count[can_id] += 1
            
            # Save some samples (first 10 and every 100th after)
            if len(samples[can_id]) < 10 or count[can_id] % 100 == 0:
                # Store with timestamp for reference
                samples[can_id].append({
                    "line": line_num,
                    "ts": msg.get("ts"),
                    "data": data
                })
    
    # Analyze each CAN ID
    for can_id in target_ids:
        if can_id not in count:
            print(f"\n{'-'*60}")
            print(f"CAN ID 0x{can_id:03X}: NOT FOUND in recording")
            continue
        
        print(f"\n{'='*60}")
        print(f"CAN ID 0x{can_id:03X}: {count[can_id]} messages")
        print(f"{'='*60}")
        
        # Show some samples
        print(f"\nSample messages ({min(10, len(samples[can_id]))} shown):")
        for sample in samples[can_id][:10]:
            data = sample["data"]
            data_hex = " ".join(f"{b:02X}" for b in data)
            print(f"  [{sample['line']:6d}] {data_hex}")
            
            # Decode based on CAN ID
            if can_id == 0x03B and len(data) >= 5:
                # Current: bytes 0-1, 12-bit signed, 0.1A resolution
                current_raw = ((data[0] & 0x0F) << 8) | data[1]
                if current_raw > 0x7FF:
                    current_raw -= 0x1000
                current_amps = current_raw * 0.1
                
                # Voltage: bytes 2-3, 0.5V resolution
                voltage_raw = (data[2] << 8) | data[3]
                voltage = voltage_raw * 0.5
                
                power_kw = (voltage * current_amps) / 1000.0
                print(f"           -> Current: {current_amps:.1f}A, Voltage: {voltage:.1f}V, Power: {power_kw:.1f}kW")
            
            elif can_id == 0x3CB and len(data) >= 7:
                cdl = data[0]
                ccl = data[1]
                delta_soc = data[2] * 0.5
                soc = data[3] * 0.5
                temp1 = data[4] - 256 if data[4] > 127 else data[4]
                temp2 = data[5] - 256 if data[5] > 127 else data[5]
                print(f"           -> SOC: {soc:.1f}%, DeltaSOC: {delta_soc:.1f}%, CDL: {cdl}A, CCL: {ccl}A")
                print(f"           -> Temp1: {temp1}°C, Temp2: {temp2}°C")
            
            elif can_id == 0x3C8 and len(data) >= 5:
                # Try to decode alternative SOC
                print(f"           -> Byte1: {data[1]}, Byte2: {data[2]} (possible SOC candidates)")
            
            elif can_id == 0x540 and len(data) >= 4:
                temp = data[0] - 40
                print(f"           -> Inverter Temp: {temp}°C (raw: {data[0]})")
            
            elif can_id == 0x348 and len(data) >= 6:
                temp1 = data[1] - 40 if data[1] > 0 else None
                temp2 = data[2] - 40 if data[2] > 0 else None
                print(f"           -> Pack Temp1: {temp1}°C, Temp2: {temp2}°C")
            
            elif can_id == 0x038 and len(data) >= 7:
                ice_running = (data[0] & 0x40) > 0
                rpm_byte = data[1]
                rpm = rpm_byte * 32
                coolant_raw = data[5]
                # Try with offset 40
                coolant = coolant_raw - 40 if coolant_raw > 0 else None
                print(f"           -> ICE Running: {ice_running}, RPM~{rpm}, Coolant: {coolant}°C (raw: {coolant_raw})")


if __name__ == "__main__":
    file_path = "assets/data/full.ndjson"
    if len(sys.argv) > 1:
        file_path = sys.argv[1]
    
    analyze_can_message(file_path)
