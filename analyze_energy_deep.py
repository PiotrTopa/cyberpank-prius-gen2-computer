#!/usr/bin/env python3
"""
Deep AVC-LAN Energy Data Analysis.

Focuses on finding actual energy data (SOC, power flow) in AVC-LAN messages.
Looking at larger packets that might contain display data for MFD.

Key findings from initial analysis:
- 110/210/B10/D10 -> 490: Status messages with patterns like 00 46 C1/C8
- 002 -> 660: Large 32-byte display packets
- A00 -> 258: 32-byte system controller packets
- 806 -> C48: Large display data packets
"""

import json
import sys
from pathlib import Path
from collections import defaultdict


def load_all_avc_messages(data_dir: Path) -> list:
    """Load all AVC-LAN messages from all files."""
    files = [
        "avc_lan.ndjson",
        "avc_analysis.ndjson",
        "avc_analysis_2.ndjson",
        "avc_lan_extended.ndjson"
    ]
    
    all_msgs = []
    for filename in files:
        filepath = data_dir / filename
        if filepath.exists():
            with open(filepath, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if obj.get('id') == 2:
                            all_msgs.append(obj)
                    except json.JSONDecodeError:
                        continue
    return all_msgs


def hex_data(data: list) -> str:
    """Convert data to hex string."""
    return ' '.join(b if isinstance(b, str) else f"{b:02X}" for b in data)


def byte_val(b) -> int:
    """Convert byte (string or int) to int."""
    if isinstance(b, str):
        return int(b, 16)
    return b


def main():
    data_dir = Path(__file__).parent / "assets/data"
    all_msgs = load_all_avc_messages(data_dir)
    print(f"Loaded {len(all_msgs)} AVC-LAN messages\n")
    
    # =========================================================================
    # ANALYZE LARGE PACKETS (32 bytes) - likely contain rich data
    # =========================================================================
    print("=" * 80)
    print("LARGE PACKET ANALYSIS (32 bytes) - Potential Energy/Display Data")
    print("=" * 80)
    
    large_packets = defaultdict(list)
    
    for msg in all_msgs:
        d = msg.get('d', {})
        data = d.get('d', [])
        if len(data) >= 20:  # Large packets
            key = f"{d.get('m')}->{d.get('s')}"
            large_packets[key].append({
                'seq': msg.get('seq'),
                'ts': msg.get('ts'),
                'data': data
            })
    
    print("\nLarge packet sources:")
    for addr, msgs in sorted(large_packets.items(), key=lambda x: -len(x[1])):
        print(f"  {addr}: {len(msgs)} packets, len={len(msgs[0]['data'])}")
    
    # =========================================================================
    # DEEP DIVE: A00 -> 258 (System Controller)
    # =========================================================================
    print("\n" + "=" * 80)
    print("A00 -> 258 (System Controller) - 32 byte packets")
    print("=" * 80)
    
    for msg in large_packets.get("A00->258", []):
        data = msg['data']
        print(f"\n[{msg['seq']:>4}] ts={msg['ts']}")
        
        # Print in 8-byte rows with analysis
        for row in range(0, len(data), 8):
            chunk = data[row:row+8]
            hex_str = ' '.join(f"{byte_val(b):02X}" for b in chunk)
            
            # Try to interpret
            vals = [byte_val(b) for b in chunk]
            dec_str = ' '.join(f"{v:3}" for v in vals)
            
            print(f"  [{row:2}-{row+7:2}]: {hex_str}  |  {dec_str}")
    
    # =========================================================================
    # DEEP DIVE: 002 -> 660 (Display Control)
    # =========================================================================
    print("\n" + "=" * 80)
    print("002 -> 660 (Display Control) - Variable length packets")
    print("=" * 80)
    
    for msg in all_msgs:
        d = msg.get('d', {})
        if d.get('m') == '002' and d.get('s') == '660':
            data = d.get('d', [])
            if len(data) >= 16:
                print(f"\n[{msg.get('seq'):>4}] len={len(data)}")
                
                for row in range(0, min(len(data), 32), 8):
                    chunk = data[row:row+8]
                    hex_str = ' '.join(f"{byte_val(b):02X}" for b in chunk)
                    print(f"  [{row:2}-{row+7:2}]: {hex_str}")
    
    # =========================================================================
    # DEEP DIVE: 806 -> C48 and C10 -> 028 (Likely hybrid display data)
    # =========================================================================
    print("\n" + "=" * 80)
    print("806 -> C48 (Large Display Data)")
    print("=" * 80)
    
    for msg in large_packets.get("806->C48", []):
        data = msg['data']
        print(f"\n[{msg['seq']:>4}] ts={msg['ts']}")
        
        for row in range(0, len(data), 8):
            chunk = data[row:row+8]
            hex_str = ' '.join(f"{byte_val(b):02X}" for b in chunk)
            vals = [byte_val(b) for b in chunk]
            dec_str = ' '.join(f"{v:3}" for v in vals)
            print(f"  [{row:2}-{row+7:2}]: {hex_str}  |  {dec_str}")
    
    # =========================================================================
    # ICE STATUS TRACKING: C1 vs C8 pattern
    # =========================================================================
    print("\n" + "=" * 80)
    print("ICE STATUS TRACKING (C1=OFF, C8=ON pattern)")
    print("=" * 80)
    
    for msg in all_msgs:
        d = msg.get('d', {})
        data = d.get('d', [])
        
        if len(data) >= 3:
            b2 = byte_val(data[2]) if len(data) > 2 else 0
            
            # Look for C8 (ICE running) pattern
            if b2 == 0xC8:
                print(f"[{msg.get('seq'):>4}] {d.get('m')}->{d.get('s')}: {hex_data(data)} <- ICE RUNNING")
            # Also look for C1 around the same addresses
            elif b2 == 0xC1 and d.get('s') == '490':
                master = d.get('m')
                if master in ['110', '210', 'B10', 'D10']:
                    # Only print a few
                    if msg.get('seq', 0) < 300:
                        print(f"[{msg.get('seq'):>4}] {master}->{d.get('s')}: {hex_data(data)} <- ICE OFF")
    
    # =========================================================================
    # SEARCH FOR NUMERIC PATTERNS (potential SOC/Power values)
    # =========================================================================
    print("\n" + "=" * 80)
    print("NUMERIC PATTERN SEARCH")
    print("=" * 80)
    
    print("\nLooking for bytes that could represent percentages (0-100) or power values...")
    
    # Track values in specific positions across multiple messages
    position_values = defaultdict(lambda: defaultdict(set))
    
    for msg in all_msgs:
        d = msg.get('d', {})
        data = d.get('d', [])
        addr = f"{d.get('m')}->{d.get('s')}"
        
        for i, b in enumerate(data):
            val = byte_val(b)
            position_values[addr][(i, len(data))].add(val)
    
    print("\nPositions with varied values (potential data fields):")
    
    for addr in ["110->490", "210->490", "112->060"]:
        print(f"\n{addr}:")
        for (pos, length), values in sorted(position_values[addr].items()):
            if len(values) > 2 and len(values) < 50:  # Variable but not too random
                min_v = min(values)
                max_v = max(values)
                if max_v - min_v > 5:  # Meaningful range
                    print(f"  Byte[{pos}] (len={length}): {len(values)} unique, "
                          f"range {min_v}-{max_v} (0x{min_v:02X}-0x{max_v:02X})")
                    print(f"    Values: {sorted(values)[:10]}{'...' if len(values) > 10 else ''}")


if __name__ == "__main__":
    main()
