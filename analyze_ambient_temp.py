"""
Analyze AVC-LAN messages for ambient (outside) temperature data.

The user drove in -2 to -4°C conditions, but the display shows 15°C.
This script analyzes the extracted AVC-LAN messages to find the correct
encoding for outside temperature.

Known sources:
- CAN 0x4CE: Battery compartment air temp (constant 15°C, NOT ambient)
- AVC-LAN 10C→310: Climate ECU to Display (byte 5 = outside temp, documented as /2)
"""

import json
from collections import Counter


def main():
    # Load extracted AVC-LAN messages
    msgs = []
    with open('assets/data/avc_lan_messages.ndjson', 'r') as f:
        for line in f:
            if line.strip():
                msgs.append(json.loads(line))
    
    print(f"Loaded {len(msgs)} AVC-LAN messages")
    print()
    
    # Find 10C -> 310 messages (Climate ECU to Display)
    climate_msgs = []
    for msg in msgs:
        data = msg.get('parsed', {})
        if data.get('master_addr') == 0x10C and data.get('slave_addr') == 0x310:
            climate_msgs.append(msg)
    
    print(f"Found {len(climate_msgs)} climate messages (10C → 310)")
    print()
    
    if not climate_msgs:
        print("No climate messages found! Checking all address pairs...")
        pairs = Counter()
        for msg in msgs:
            data = msg.get('parsed', {})
            m = data.get('master_addr')
            s = data.get('slave_addr')
            if m is not None and s is not None:
                pairs[(m, s)] += 1
        
        print("Top address pairs:")
        for (m, s), count in pairs.most_common(20):
            print(f"  {m:03X} → {s:03X}: {count:4d}x")
        return
    
    # Analyze byte patterns
    byte5_values = Counter()
    for msg in climate_msgs:
        data = msg.get('parsed', {}).get('data', [])
        if len(data) >= 6:
            byte5_values[data[5]] += 1
    
    print("Byte 5 values (documented as outside temp * 2):")
    print("-" * 70)
    for val, count in sorted(byte5_values.items()):
        # Different decoding attempts
        decoded_div2 = val / 2.0
        decoded_offset40 = val - 40  # Common automotive offset
        decoded_signed = (val - 256) if val > 127 else val
        
        print(f"  0x{val:02X} ({val:3d}): {count:4d}x  "
              f"-> /2={decoded_div2:6.1f}°C | "
              f"-40={decoded_offset40:4d}°C | "
              f"signed={decoded_signed:4d}")
    
    print()
    print("Sample message data:")
    print("-" * 70)
    for i, msg in enumerate(climate_msgs[:15]):
        data = msg.get('parsed', {}).get('data', [])
        hex_data = ' '.join(f'{b:02X}' for b in data)
        byte5 = data[5] if len(data) >= 6 else None
        
        if byte5 is not None:
            # The user drove in -2 to -4°C
            # If byte5 / 2 = temp, then for -4°C we need byte5 = -8
            # If temp = byte5 - 40, then for -4°C we need byte5 = 36
            # If temp = (byte5 - 80) / 2, then for -4°C we need byte5 = 72
            temp_div2 = byte5 / 2.0
            temp_minus40 = byte5 - 40
            temp_formula = (byte5 - 80) / 2  # Alternative: offset before divide
            
            print(f"  {hex_data}")
            print(f"      Byte5=0x{byte5:02X}({byte5}): "
                  f"/2={temp_div2:.1f}°C, -40={temp_minus40}°C, (b-80)/2={temp_formula:.1f}°C")
    
    # Check other potential climate-related addresses
    print()
    print("=" * 70)
    print("Checking other potential temperature sources...")
    print("=" * 70)
    
    # Find all messages with similar addressing patterns
    other_climate = []
    for msg in msgs:
        data = msg.get('parsed', {})
        m = data.get('master_addr')
        s = data.get('slave_addr')
        # Look for messages from ECUs to display (0x310) or from climate (0x10C)
        if m == 0x10C or s == 0x310:
            other_climate.append(msg)
    
    # Group by address pair
    by_pair = {}
    for msg in other_climate:
        data = msg.get('parsed', {})
        m = data.get('master_addr')
        s = data.get('slave_addr')
        pair = (m, s)
        if pair not in by_pair:
            by_pair[pair] = []
        by_pair[pair].append(msg)
    
    for (m, s), pair_msgs in sorted(by_pair.items()):
        if len(pair_msgs) < 5:
            continue
        print(f"\n{m:03X} → {s:03X}: {len(pair_msgs)} messages")
        
        # Analyze data patterns
        for i, msg in enumerate(pair_msgs[:5]):
            data = msg.get('parsed', {}).get('data', [])
            hex_data = ' '.join(f'{b:02X}' for b in data)
            print(f"  {hex_data}")


if __name__ == '__main__':
    main()
