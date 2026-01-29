#!/usr/bin/env python3
"""
Deep analyze 0x038 and 0x039 to find coolant temperature byte.
Also check 0x03A for speed.
"""

import json
from collections import defaultdict


def analyze_byte_ranges(file_path: str, can_ids: list[int]):
    """Analyze byte ranges for specific CAN IDs to find patterns."""
    
    byte_ranges = defaultdict(lambda: defaultdict(lambda: {"min": 256, "max": -1, "values": set()}))
    msg_count = defaultdict(int)
    
    print(f"Analyzing byte ranges from: {file_path}")
    print(f"Looking for CAN IDs: {[hex(x) for x in can_ids]}\n")
    
    with open(file_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            
            if msg.get("id") != 1:
                continue
            
            d = msg.get("d", {})
            can_id_str = d.get("i", "")
            data = d.get("d", [])
            
            try:
                can_id = int(can_id_str, 16)
            except ValueError:
                continue
            
            if can_id not in can_ids:
                continue
            
            msg_count[can_id] += 1
            
            for i, byte_val in enumerate(data):
                byte_ranges[can_id][i]["min"] = min(byte_ranges[can_id][i]["min"], byte_val)
                byte_ranges[can_id][i]["max"] = max(byte_ranges[can_id][i]["max"], byte_val)
                byte_ranges[can_id][i]["values"].add(byte_val)
    
    # Print results
    for can_id in can_ids:
        if can_id not in msg_count:
            print(f"\n{'-'*60}")
            print(f"CAN ID 0x{can_id:03X}: NOT FOUND")
            continue
        
        print(f"\n{'='*60}")
        print(f"CAN ID 0x{can_id:03X}: {msg_count[can_id]} messages")
        print(f"{'='*60}")
        
        print("\nByte analysis:")
        print(f"{'Byte':>4} | {'Min':>3} | {'Max':>3} | {'Range':>5} | {'Unique':>6} | {'Min-40':>6} | {'Max-40':>6} | Interpretation")
        print("-" * 80)
        
        for byte_idx in sorted(byte_ranges[can_id].keys()):
            info = byte_ranges[can_id][byte_idx]
            min_val = info["min"]
            max_val = info["max"]
            val_range = max_val - min_val
            unique_count = len(info["values"])
            min_offset = min_val - 40
            max_offset = max_val - 40
            
            # Try to interpret
            interpretation = ""
            if val_range == 0:
                interpretation = f"Constant: 0x{min_val:02X}"
            elif min_offset >= -40 and max_offset <= 120 and val_range < 100:
                interpretation = f"Temp? ({min_offset} to {max_offset}°C)"
            elif val_range < 10:
                interpretation = f"Flag/Status ({unique_count} vals)"
            elif min_val == 0 and max_val > 200:
                interpretation = "Speed/RPM high byte?"
            elif unique_count > 100:
                interpretation = "Variable data"
            
            print(f"{byte_idx:4d} | {min_val:3d} | {max_val:3d} | {val_range:5d} | {unique_count:6d} | {min_offset:6d} | {max_offset:6d} | {interpretation}")
        
        # Show sample values for key bytes
        print("\nPossible temperature bytes (stable range 0-100):")
        for byte_idx in sorted(byte_ranges[can_id].keys()):
            info = byte_ranges[can_id][byte_idx]
            min_val = info["min"]
            max_val = info["max"]
            if min_val >= 0 and max_val <= 150 and (max_val - min_val) < 80:
                sorted_vals = sorted(info["values"])
                vals_preview = sorted_vals[:10] if len(sorted_vals) > 10 else sorted_vals
                print(f"  Byte {byte_idx}: {sorted_vals[:5]}...{sorted_vals[-3:]} (offset 40: {min_val-40} to {max_val-40}°C)")


if __name__ == "__main__":
    file_path = "assets/data/full.ndjson"
    
    # Analyze ICE related and speed messages
    analyze_byte_ranges(file_path, [0x038, 0x039, 0x03A, 0x03E])
