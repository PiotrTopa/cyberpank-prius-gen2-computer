#!/usr/bin/env python3
"""
Analyze CAN IDs from recording file.
Scans the full.ndjson file and reports all CAN IDs with sample data.
"""

import json
from collections import defaultdict
from pathlib import Path

def analyze_can_file(filepath: str) -> dict:
    """Analyze CAN messages in the file."""
    
    # Stats per CAN ID
    stats = defaultdict(lambda: {
        "count": 0,
        "samples": [],  # First few samples
        "data_lengths": set(),
        "min_values": {},
        "max_values": {},
        "all_bytes": defaultdict(set)  # byte_pos -> set of seen values
    })
    
    total_lines = 0
    can_messages = 0
    avc_messages = 0
    errors = 0
    
    with open(filepath, 'r') as f:
        for line_num, line in enumerate(f, 1):
            total_lines += 1
            line = line.strip()
            if not line:
                continue
            
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                errors += 1
                continue
            
            device_id = obj.get("id", 0)
            d = obj.get("d", {})
            
            # Device ID 1 = CAN, Device ID 2 = AVC-LAN
            if device_id == 1:
                can_messages += 1
                
                # Get CAN ID
                can_id_str = d.get("i", "")
                data = d.get("d", [])
                
                if not can_id_str:
                    continue
                
                # Parse CAN ID
                try:
                    can_id = int(can_id_str, 16)
                except ValueError:
                    continue
                
                # Convert data to integers
                if data and isinstance(data[0], str):
                    try:
                        data = [int(b, 16) for b in data]
                    except ValueError:
                        continue
                
                # Update stats
                info = stats[can_id]
                info["count"] += 1
                info["data_lengths"].add(len(data))
                
                # Store first 3 samples
                if len(info["samples"]) < 3:
                    info["samples"].append(data)
                
                # Track byte values
                for i, val in enumerate(data):
                    info["all_bytes"][i].add(val)
                    
                    # Track min/max for each byte position
                    if i not in info["min_values"]:
                        info["min_values"][i] = val
                        info["max_values"][i] = val
                    else:
                        info["min_values"][i] = min(info["min_values"][i], val)
                        info["max_values"][i] = max(info["max_values"][i], val)
            
            elif device_id == 2:
                avc_messages += 1
    
    return {
        "total_lines": total_lines,
        "can_messages": can_messages,
        "avc_messages": avc_messages,
        "errors": errors,
        "can_ids": dict(stats)
    }


def format_byte_range(min_val: int, max_val: int) -> str:
    """Format byte range for display."""
    if min_val == max_val:
        return f"{min_val:02X}"
    else:
        return f"{min_val:02X}-{max_val:02X}"


def main():
    filepath = Path("assets/data/full.ndjson")
    
    print(f"Analyzing {filepath}...")
    print("=" * 80)
    
    result = analyze_can_file(filepath)
    
    print(f"\nFile Statistics:")
    print(f"  Total lines:    {result['total_lines']:,}")
    print(f"  CAN messages:   {result['can_messages']:,}")
    print(f"  AVC-LAN msgs:   {result['avc_messages']:,}")
    print(f"  Parse errors:   {result['errors']:,}")
    print(f"  Unique CAN IDs: {len(result['can_ids'])}")
    
    print("\n" + "=" * 80)
    print("CAN ID Analysis (sorted by ID):")
    print("=" * 80)
    
    # Sort by CAN ID
    sorted_ids = sorted(result["can_ids"].keys())
    
    for can_id in sorted_ids:
        info = result["can_ids"][can_id]
        count = info["count"]
        lengths = info["data_lengths"]
        samples = info["samples"]
        
        print(f"\n0x{can_id:03X} ({can_id:4d}) - Count: {count:>6,}  Data lengths: {sorted(lengths)}")
        
        # Print byte ranges
        if info["min_values"]:
            ranges = []
            for i in range(max(info["min_values"].keys()) + 1):
                if i in info["min_values"]:
                    ranges.append(format_byte_range(info["min_values"][i], info["max_values"][i]))
            print(f"  Byte ranges: [{' '.join(ranges)}]")
        
        # Print samples
        for i, sample in enumerate(samples):
            hex_data = " ".join(f"{b:02X}" for b in sample)
            print(f"  Sample {i+1}: [{hex_data}]")
    
    # Print frequency table
    print("\n" + "=" * 80)
    print("CAN ID Frequency (sorted by count, top 30):")
    print("=" * 80)
    
    by_count = sorted(result["can_ids"].items(), key=lambda x: x[1]["count"], reverse=True)
    
    print(f"\n{'CAN ID':>10} | {'Count':>10} | {'Pct':>6} | Description")
    print("-" * 60)
    
    for can_id, info in by_count[:30]:
        count = info["count"]
        pct = count / result["can_messages"] * 100
        print(f"0x{can_id:03X} ({can_id:4d}) | {count:>10,} | {pct:>5.1f}% | ")
    
    print("\n" + "=" * 80)
    print("Known Prius Gen 2 CAN IDs (from research):")
    print("=" * 80)
    
    known_ids = {
        0x030: "Engine/ICE status",
        0x038: "ICE RPM, Coolant temp",
        0x039: "ICE RPM (alternative)",
        0x03A: "Vehicle speed",
        0x03B: "Battery voltage/current",
        0x03C: "Unknown",
        0x03E: "Battery SOC",
        0x0B1: "Unknown status",
        0x0B3: "Unknown status", 
        0x0B4: "Vehicle speed (alternative)",
        0x120: "Gear position, speed",
        0x23:  "Throttle/pedal",
        0x262: "Unknown",
        0x348: "Battery pack temp/status",
        0x3C8: "HV Battery SOC",
        0x3C9: "HV Battery cell voltages",
        0x3CA: "HV Battery status",
        0x3CB: "HV Battery power",
        0x3CC: "HV Battery status",
        0x3CD: "HV Battery current",
        0x3CE: "HV Battery status",
        0x3CF: "HV Battery voltage",
        0x540: "Inverter temp",
    }
    
    for can_id, description in sorted(known_ids.items()):
        found = "FOUND" if can_id in result["can_ids"] else "NOT FOUND"
        count = result["can_ids"].get(can_id, {}).get("count", 0)
        print(f"  0x{can_id:03X}: {description:30s} - {found:10s} ({count:,} messages)")


if __name__ == "__main__":
    main()
