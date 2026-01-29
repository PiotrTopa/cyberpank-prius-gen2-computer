"""
Deep analysis of temperature-related CAN and AVC-LAN data.

Focus areas:
1. CAN 0x3CB battery temperature (bytes 4-5)
2. CAN 0x348 battery pack temperature
3. AVC-LAN 10C->310 climate messages
4. CAN ambient temperature (0x07E0 PID 0146 = byte - 40)
"""

import json
from pathlib import Path
from collections import Counter, defaultdict
import statistics


def analyze_can_battery_temp(input_path: str):
    """Analyze CAN battery temperature data from 0x3CB and 0x348."""
    print("=" * 60)
    print("=== CAN Battery Temperature Analysis ===")
    print("=" * 60)
    
    input_file = Path(input_path)
    
    # Track temperature readings from different sources
    temp_0x3CB_b4 = []  # 0x3CB byte 4 (temp1 - average/lowest)
    temp_0x3CB_b5 = []  # 0x3CB byte 5 (temp2 - intake/highest)
    temp_0x348 = []     # 0x348 battery pack temp
    voltages = []       # HV voltage
    
    # Track raw data patterns
    raw_0x3CB = []
    raw_0x348 = []
    
    total_can = 0
    
    with open(input_file, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                msg = json.loads(line.strip())
                if msg.get("id") != 1:  # CAN messages are id:1
                    continue
                
                total_can += 1
                data = msg.get("d", {})
                can_id_str = data.get("i")
                msg_data = data.get("d", [])
                
                if not can_id_str or not msg_data:
                    continue
                
                # Convert data to integers
                if isinstance(msg_data[0], str):
                    try:
                        msg_data = [int(b, 16) for b in msg_data]
                    except (ValueError, TypeError):
                        continue
                
                can_id = int(can_id_str, 16)
                
                # 0x3CB: SOC, Current Limits, Temperatures
                # Format: [CDL, CCL, SOC_Hi?, SOC, Temp1, Temp2, Checksum]
                if can_id == 0x3CB and len(msg_data) >= 7:
                    raw_0x3CB.append(msg_data)
                    
                    # Temperature 1: byte 4, signed [°C]
                    temp1_raw = msg_data[4]
                    temp1 = temp1_raw if temp1_raw <= 127 else temp1_raw - 256
                    temp_0x3CB_b4.append(temp1)
                    
                    # Temperature 2: byte 5, signed [°C]
                    temp2_raw = msg_data[5]
                    temp2 = temp2_raw if temp2_raw <= 127 else temp2_raw - 256
                    temp_0x3CB_b5.append(temp2)
                
                # 0x348: Battery Pack Temperature/Status
                if can_id == 0x348 and len(msg_data) >= 4:
                    raw_0x348.append(msg_data)
                    # Need to determine which byte contains temp
                
                # 0x03B: Battery voltage (byte 3)
                if can_id == 0x03B and len(msg_data) >= 4:
                    voltages.append(msg_data[3])
                    
            except json.JSONDecodeError:
                continue
    
    print(f"\nTotal CAN messages analyzed: {total_can}")
    
    # Analyze 0x3CB temperatures
    print(f"\n--- 0x3CB Battery Temperatures ---")
    print(f"Messages: {len(temp_0x3CB_b4)}")
    
    if temp_0x3CB_b4:
        print(f"\nByte 4 (Temp1 - supposedly average/lowest):")
        print(f"  Range: {min(temp_0x3CB_b4)}°C to {max(temp_0x3CB_b4)}°C")
        print(f"  Mean: {statistics.mean(temp_0x3CB_b4):.1f}°C")
        print(f"  Median: {statistics.median(temp_0x3CB_b4):.1f}°C")
        print(f"  Std Dev: {statistics.stdev(temp_0x3CB_b4):.1f}°C")
        
        # Value distribution
        value_counts = Counter(temp_0x3CB_b4)
        print(f"  Value distribution (top 10):")
        for val, count in value_counts.most_common(10):
            pct = count / len(temp_0x3CB_b4) * 100
            print(f"    {val}°C: {count} ({pct:.1f}%)")
    
    if temp_0x3CB_b5:
        print(f"\nByte 5 (Temp2 - supposedly intake/highest):")
        print(f"  Range: {min(temp_0x3CB_b5)}°C to {max(temp_0x3CB_b5)}°C")
        print(f"  Mean: {statistics.mean(temp_0x3CB_b5):.1f}°C")
        print(f"  Median: {statistics.median(temp_0x3CB_b5):.1f}°C")
        print(f"  Std Dev: {statistics.stdev(temp_0x3CB_b5):.1f}°C")
        
        value_counts = Counter(temp_0x3CB_b5)
        print(f"  Value distribution (top 10):")
        for val, count in value_counts.most_common(10):
            pct = count / len(temp_0x3CB_b5) * 100
            print(f"    {val}°C: {count} ({pct:.1f}%)")
    
    # Show raw data samples
    print(f"\n--- Sample Raw 0x3CB Messages ---")
    seen_patterns = set()
    for data in raw_0x3CB[:50]:
        pattern = tuple(data)
        if pattern not in seen_patterns:
            seen_patterns.add(pattern)
            hex_str = ' '.join(f'{b:02X}' for b in data)
            temp1_raw = data[4]
            temp1 = temp1_raw if temp1_raw <= 127 else temp1_raw - 256
            temp2_raw = data[5]
            temp2 = temp2_raw if temp2_raw <= 127 else temp2_raw - 256
            soc = data[3] * 0.5
            print(f"  [{hex_str}] -> SOC={soc}%, Temp1={temp1}°C, Temp2={temp2}°C")
    
    # Analyze 0x348 if present
    if raw_0x348:
        print(f"\n--- 0x348 Battery Pack Temperature ---")
        print(f"Messages: {len(raw_0x348)}")
        print(f"Sample raw data:")
        seen_patterns = set()
        for data in raw_0x348[:20]:
            pattern = tuple(data)
            if pattern not in seen_patterns:
                seen_patterns.add(pattern)
                hex_str = ' '.join(f'{b:02X}' for b in data)
                print(f"  [{hex_str}]")
    
    # Voltage analysis
    if voltages:
        print(f"\n--- HV Battery Voltage (0x03B byte 3) ---")
        print(f"Messages: {len(voltages)}")
        print(f"  Range: {min(voltages)}V to {max(voltages)}V")
        print(f"  Mean: {statistics.mean(voltages):.1f}V")
        
    return temp_0x3CB_b4, temp_0x3CB_b5


def analyze_temperature_stability(temps: list, name: str):
    """
    Analyze if temperature readings are stable or jumping around.
    
    User reports seeing: 42, 35, -3 which suggests:
    1. Different CAN IDs sending conflicting temps
    2. Wrong byte interpretation
    3. Some other source mixing in
    """
    if not temps or len(temps) < 10:
        return
    
    print(f"\n--- Temperature Stability Analysis: {name} ---")
    
    # Calculate consecutive differences
    diffs = [abs(temps[i] - temps[i-1]) for i in range(1, len(temps))]
    
    print(f"  Total readings: {len(temps)}")
    print(f"  Range: {min(temps)} to {max(temps)}")
    
    # How often does temp jump by more than 5 degrees?
    big_jumps = sum(1 for d in diffs if d > 5)
    jump_pct = big_jumps / len(diffs) * 100
    print(f"  Jumps > 5°C: {big_jumps} ({jump_pct:.1f}%)")
    
    # How often does temp jump by more than 10 degrees?
    huge_jumps = sum(1 for d in diffs if d > 10)
    huge_jump_pct = huge_jumps / len(diffs) * 100
    print(f"  Jumps > 10°C: {huge_jumps} ({huge_jump_pct:.1f}%)")
    
    if huge_jump_pct > 5:
        print(f"  WARNING: High temperature instability detected!")
        print(f"  This suggests data from multiple sources or wrong byte interpretation")


def analyze_avc_climate_temps(input_path: str):
    """Analyze AVC-LAN 10C->310 climate messages for temperature data."""
    print("\n" + "=" * 60)
    print("=== AVC-LAN Climate Temperature Analysis ===")
    print("=" * 60)
    
    input_file = Path(input_path)
    
    climate_messages = []
    
    with open(input_file, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                msg = json.loads(line.strip())
                if msg.get("id") != 2:
                    continue
                
                data = msg.get("d", {})
                master = data.get("m")
                slave = data.get("s")
                
                # Climate ECU messages
                if master == "10C" and slave == "310":
                    msg_data = data.get("d", [])
                    if msg_data:
                        try:
                            int_data = [int(b, 16) for b in msg_data]
                            climate_messages.append({
                                'ts': msg.get('ts'),
                                'data': int_data
                            })
                        except (ValueError, TypeError):
                            pass
                            
            except json.JSONDecodeError:
                continue
    
    print(f"\n10C -> 310 Climate Messages: {len(climate_messages)}")
    
    if not climate_messages:
        print("  No climate messages found")
        return
    
    # Analyze each byte position
    print("\nByte-by-byte analysis:")
    
    for byte_idx in range(8):
        values = []
        for msg in climate_messages:
            if len(msg['data']) > byte_idx:
                values.append(msg['data'][byte_idx])
        
        if not values:
            continue
        
        unique_vals = set(values)
        print(f"\n  Byte {byte_idx}:")
        print(f"    Unique values: {len(unique_vals)}")
        
        # Show all unique values with potential decodings
        for val in sorted(unique_vals):
            count = values.count(val)
            pct = count / len(values) * 100
            # Potential temperature decodings
            raw = val
            minus40 = val - 40
            div2 = val / 2
            signed = val if val <= 127 else val - 256
            
            # Check if any decoding gives realistic temp
            decodings = []
            if -10 <= raw <= 40:
                decodings.append(f"raw={raw}°C")
            if -10 <= minus40 <= 40:
                decodings.append(f"-40={minus40}°C")
            if 15 <= div2 <= 30:
                decodings.append(f"/2={div2:.0f}°C")
            if -10 <= signed <= 40 and signed != raw:
                decodings.append(f"signed={signed}°C")
            
            dec_str = ', '.join(decodings) if decodings else 'no temp match'
            print(f"      0x{val:02X} ({val}): {count} ({pct:.0f}%) - {dec_str}")


if __name__ == "__main__":
    input_path = "assets/data/full.ndjson"
    
    # Analyze CAN battery temperatures
    temp_b4, temp_b5 = analyze_can_battery_temp(input_path)
    
    # Check stability
    analyze_temperature_stability(temp_b4, "0x3CB Byte 4 (Temp1)")
    analyze_temperature_stability(temp_b5, "0x3CB Byte 5 (Temp2)")
    
    # Analyze AVC-LAN climate
    analyze_avc_climate_temps(input_path)
