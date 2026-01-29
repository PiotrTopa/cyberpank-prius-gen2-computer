"""
Extract AVC-LAN messages (id:2) from full.ndjson for analysis.

AVC-LAN messages contain data from the Toyota multimedia bus,
which may include climate control, audio, and temperature information.
"""

import json
import sys
from pathlib import Path
from collections import Counter

def extract_avc_lan_messages(input_path: str, output_path: str):
    """
    Extract all id:2 (AVC-LAN) messages from NDJSON file.
    
    Args:
        input_path: Path to input NDJSON file
        output_path: Path to output file for AVC-LAN messages
    """
    input_file = Path(input_path)
    output_file = Path(output_path)
    
    if not input_file.exists():
        print(f"Error: Input file not found: {input_file}")
        return
    
    print(f"Reading from: {input_file}")
    print(f"File size: {input_file.stat().st_size / 1024 / 1024:.2f} MB")
    
    avc_messages = []
    total_lines = 0
    avc_count = 0
    can_count = 0
    other_count = 0
    
    # Message type counter
    msg_types = Counter()
    
    with open(input_file, 'r', encoding='utf-8') as f:
        for line in f:
            total_lines += 1
            if total_lines % 10000 == 0:
                print(f"  Processed {total_lines} lines, found {avc_count} AVC-LAN messages...")
            
            try:
                msg = json.loads(line.strip())
                msg_id = msg.get("id")
                
                if msg_id == 2:  # AVC-LAN message
                    avc_count += 1
                    avc_messages.append(msg)
                elif msg_id == 1:  # CAN message
                    can_count += 1
                else:
                    other_count += 1
                
                msg_types[msg_id] += 1
                
            except json.JSONDecodeError as e:
                print(f"  Warning: Failed to parse line {total_lines}: {e}")
                continue
    
    print(f"\n=== Summary ===")
    print(f"Total lines: {total_lines}")
    print(f"Message type distribution:")
    for msg_id, count in sorted(msg_types.items()):
        pct = count / total_lines * 100
        print(f"  id={msg_id}: {count} ({pct:.1f}%)")
    
    print(f"\nAVC-LAN messages: {avc_count}")
    print(f"CAN messages: {can_count}")
    print(f"Other messages: {other_count}")
    
    # Write AVC-LAN messages to output file
    print(f"\nWriting {avc_count} AVC-LAN messages to: {output_file}")
    
    with open(output_file, 'w', encoding='utf-8') as f:
        for msg in avc_messages:
            f.write(json.dumps(msg) + '\n')
    
    output_size = output_file.stat().st_size / 1024
    print(f"Output file size: {output_size:.2f} KB")
    
    return avc_messages


def analyze_avc_messages(messages: list):
    """
    Analyze AVC-LAN messages for temperature-related data.
    
    AVC-LAN message structure:
    - "m": master address (sender)
    - "s": slave address (receiver)  
    - "c": command byte
    - "d": data bytes (hex strings)
    - "cnt": message count
    
    Looking for:
    - Ambient temperature (should be -2 to -4°C based on user info)
    - Cabin temperature
    - Set temperature (18-23°C based on user info)
    - Climate control messages
    """
    print("\n" + "=" * 60)
    print("=== AVC-LAN Message Analysis ===")
    print("=" * 60)
    
    # Extract message addresses and data
    address_pairs = Counter()  # (master, slave) pairs
    address_data = {}  # Store data samples by address pair
    master_addrs = Counter()
    slave_addrs = Counter()
    
    for msg in messages:
        data = msg.get("d", {})
        master = data.get("m")
        slave = data.get("s")
        cmd = data.get("c")
        msg_data = data.get("d", [])
        
        if master and slave:
            key = (master, slave, cmd)
            address_pairs[key] += 1
            master_addrs[master] += 1
            slave_addrs[slave] += 1
            
            # Store data samples
            if key not in address_data:
                address_data[key] = []
            if len(address_data[key]) < 30:  # Keep more samples for analysis
                # Convert hex strings to integers
                try:
                    int_data = [int(b, 16) for b in msg_data]
                    address_data[key].append(int_data)
                except (ValueError, TypeError):
                    pass
    
    print(f"\nMaster addresses found: {len(master_addrs)}")
    for addr, count in master_addrs.most_common(10):
        print(f"  {addr}: {count} messages")
    
    print(f"\nSlave addresses found: {len(slave_addrs)}")
    for addr, count in slave_addrs.most_common(10):
        print(f"  {addr}: {count} messages")
    
    print(f"\nAddress pairs (master->slave) found: {len(address_pairs)}")
    print("\nTop 20 address pairs by frequency:")
    for key, count in address_pairs.most_common(20):
        master, slave, cmd = key
        print(f"  {master} -> {slave} (cmd={cmd}): {count} messages")
    
    # Analyze climate-related addresses
    # Toyota Climate control ECU typically:
    # - Address 0x10C is Climate/AC control
    # - Address 0x310 is display/status
    # - 0x110 might be related to temperature sensors
    
    print("\n" + "=" * 60)
    print("=== Temperature Data Analysis ===")
    print("=" * 60)
    
    # Look for temperature data in the messages
    # Temperature values expected:
    # - Ambient: -2 to -4°C (actual driving conditions)
    # - Set temp: 18-23°C
    # - Battery temp: around 15°C (based on user report of wrong display)
    
    # Known climate-related address patterns
    climate_patterns = ['10C', '110', '310', '1C0', '1D0', '190']
    
    for key, samples in address_data.items():
        master, slave, cmd = key
        count = address_pairs[key]
        
        if count < 5 or not samples:
            continue
        
        # Check if this might be climate related
        is_climate_related = (master in climate_patterns or 
                             slave in climate_patterns or
                             'C' in master or 'C' in slave)
        
        # Analyze byte value ranges
        byte_ranges = {}
        for sample in samples:
            for i, val in enumerate(sample):
                if i not in byte_ranges:
                    byte_ranges[i] = {'min': val, 'max': val, 'values': set()}
                byte_ranges[i]['min'] = min(byte_ranges[i]['min'], val)
                byte_ranges[i]['max'] = max(byte_ranges[i]['max'], val)
                byte_ranges[i]['values'].add(val)
        
        # Check if any byte could be temperature
        temp_candidates = []
        for byte_idx, info in byte_ranges.items():
            min_val, max_val = info['min'], info['max']
            unique_vals = len(info['values'])
            
            # Raw temperature (0 to 50°C)
            if 0 <= min_val <= 50 and 0 <= max_val <= 50 and unique_vals > 1:
                temp_candidates.append((byte_idx, 'raw', info['values']))
            
            # byte - 40 encoding (-40 to 215°C standard)
            # Values 38-63 would be -2 to 23°C
            if 35 <= min_val <= 70 and 35 <= max_val <= 70:
                decoded = {v - 40 for v in info['values']}
                if any(-10 <= t <= 30 for t in decoded):
                    temp_candidates.append((byte_idx, 'minus40', decoded))
            
            # Half-degree encoding (val / 2)
            if 30 <= min_val <= 50 and 30 <= max_val <= 50:
                decoded = {v / 2 for v in info['values']}
                if any(15 <= t <= 25 for t in decoded):
                    temp_candidates.append((byte_idx, 'div2', decoded))
        
        # Report findings for climate-related or temp-containing messages
        if (is_climate_related or temp_candidates) and count >= 5:
            print(f"\n{master} -> {slave} (cmd={cmd}): {count} messages")
            print(f"  Sample data (first 5):")
            for sample in samples[:5]:
                hex_str = ' '.join(f'{b:02X}' for b in sample)
                print(f"    [{hex_str}]")
            
            if temp_candidates:
                print(f"  Potential temperature bytes:")
                for byte_idx, encoding, values in temp_candidates:
                    vals_str = ', '.join(f'{v}' for v in sorted(values)[:8])
                    if len(values) > 8:
                        vals_str += '...'
                    print(f"    Byte {byte_idx}: encoding={encoding}, values={{{vals_str}}}")
    
    print("\n" + "=" * 60)
    print("=== Detailed Climate Message Analysis (10C/310) ===")
    print("=" * 60)
    
    # Focus on 10C -> 310 messages (Climate ECU to Display)
    for key, samples in address_data.items():
        master, slave, cmd = key
        if master == '10C' and slave == '310':
            print(f"\n{master} -> {slave} (cmd={cmd}): {address_pairs[key]} messages")
            print(f"  All unique data patterns:")
            seen_patterns = set()
            for sample in samples:
                pattern = tuple(sample)
                if pattern not in seen_patterns:
                    seen_patterns.add(pattern)
                    hex_str = ' '.join(f'{b:02X}' for b in sample)
                    # Decode potential values
                    decoded = []
                    for i, val in enumerate(sample):
                        # Try common decodings
                        minus40 = val - 40
                        div2 = val / 2
                        decoded.append(f"b{i}:{val}(m40={minus40},/2={div2:.1f})")
                    print(f"    [{hex_str}]")
                    print(f"      {', '.join(decoded[:4])}")
                    if len(decoded) > 4:
                        print(f"      {', '.join(decoded[4:])}")


if __name__ == "__main__":
    input_path = "assets/data/full.ndjson"
    output_path = "assets/data/avc_lan_messages.ndjson"
    
    # Allow command line override
    if len(sys.argv) > 1:
        input_path = sys.argv[1]
    if len(sys.argv) > 2:
        output_path = sys.argv[2]
    
    messages = extract_avc_lan_messages(input_path, output_path)
    
    if messages:
        analyze_avc_messages(messages)
