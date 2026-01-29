#!/usr/bin/env python3
"""
AVC-LAN Energy Monitor Data Analysis.

Analyzes AVC-LAN messages to find patterns related to energy monitoring data.
The MFD (Multi Function Display) shows:
- Battery SOC percentage
- Power flow diagram (Engine, Battery, Wheels)
- Fuel consumption
- Regeneration indicators

Since MFD is connected only via AVC-LAN (not CAN), this data must be transmitted
through AVC-LAN protocol.

Known related addresses:
- 110/112: EMV (Multi-Display MFD)
- 210: Possibly hybrid system ECU
- 490: System status recipient
"""

import json
import sys
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Set


def load_avc_messages(filepath: Path) -> List[dict]:
    """Load AVC-LAN messages from NDJSON file."""
    messages = []
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get('id') == 2:  # AVC-LAN messages only
                    messages.append(obj)
            except json.JSONDecodeError:
                continue
    return messages


def analyze_addresses(messages: List[dict]) -> Dict[str, Dict]:
    """Analyze message patterns by address pairs."""
    patterns = defaultdict(lambda: {
        'count': 0,
        'data_lengths': set(),
        'sample_data': [],
        'control_codes': set()
    })
    
    for msg in messages:
        d = msg.get('d', {})
        master = d.get('m', '???')
        slave = d.get('s', '???')
        data = d.get('d', [])
        control = d.get('c', 0)
        
        key = f"{master}->{slave}"
        patterns[key]['count'] += 1
        patterns[key]['data_lengths'].add(len(data))
        patterns[key]['control_codes'].add(control)
        
        # Keep up to 10 samples
        if len(patterns[key]['sample_data']) < 10:
            patterns[key]['sample_data'].append(data)
    
    return dict(patterns)


def find_variable_data(messages: List[dict], master: str, slave: str) -> List[dict]:
    """Find messages between specific addresses and track data changes."""
    matches = []
    for msg in messages:
        d = msg.get('d', {})
        if d.get('m') == master and d.get('s') == slave:
            matches.append({
                'seq': msg.get('seq'),
                'ts': msg.get('ts'),
                'data': d.get('d', [])
            })
    return matches


def analyze_data_variation(matches: List[dict]) -> Dict:
    """Analyze which bytes in the data vary between messages."""
    if not matches:
        return {}
    
    # Find the most common data length
    lengths = defaultdict(int)
    for m in matches:
        lengths[len(m['data'])] += 1
    
    most_common_len = max(lengths.keys(), key=lambda x: lengths[x])
    
    # Filter to only messages with that length
    filtered = [m for m in matches if len(m['data']) == most_common_len]
    
    if len(filtered) < 2:
        return {'length': most_common_len, 'samples': len(filtered)}
    
    # Track values per byte position
    byte_values = defaultdict(set)
    for m in filtered:
        for i, b in enumerate(m['data']):
            # Convert hex string to int if needed
            if isinstance(b, str):
                byte_values[i].add(int(b, 16))
            else:
                byte_values[i].add(b)
    
    # Identify variable bytes
    variable_bytes = {}
    constant_bytes = {}
    
    for pos, values in byte_values.items():
        if len(values) > 1:
            variable_bytes[pos] = {
                'unique_values': len(values),
                'min': min(values),
                'max': max(values),
                'range': max(values) - min(values)
            }
        else:
            constant_bytes[pos] = list(values)[0]
    
    return {
        'length': most_common_len,
        'samples': len(filtered),
        'variable_bytes': variable_bytes,
        'constant_bytes': constant_bytes
    }


def hex_data(data: List) -> str:
    """Convert data list to hex string."""
    result = []
    for b in data:
        if isinstance(b, str):
            result.append(b.upper())
        else:
            result.append(f"{b:02X}")
    return ' '.join(result)


def main():
    # Analyze all available AVC-LAN files
    data_dir = Path(__file__).parent / "assets/data"
    
    files_to_analyze = [
        "avc_lan.ndjson",
        "avc_analysis.ndjson", 
        "avc_analysis_2.ndjson",
        "avc_lan_extended.ndjson"
    ]
    
    all_messages = []
    
    for filename in files_to_analyze:
        filepath = data_dir / filename
        if filepath.exists():
            msgs = load_avc_messages(filepath)
            print(f"Loaded {len(msgs)} messages from {filename}")
            all_messages.extend(msgs)
    
    print(f"\nTotal: {len(all_messages)} AVC-LAN messages\n")
    
    # Analyze address patterns
    print("=" * 70)
    print("ADDRESS PATTERN ANALYSIS")
    print("=" * 70)
    
    patterns = analyze_addresses(all_messages)
    
    # Sort by count
    sorted_patterns = sorted(patterns.items(), key=lambda x: -x[1]['count'])
    
    # Show top patterns (potential energy data candidates)
    print("\nTop 30 message patterns:\n")
    for addr, info in sorted_patterns[:30]:
        print(f"  {addr:15} : {info['count']:>5} msgs, "
              f"len={sorted(info['data_lengths'])}, "
              f"ctrl={sorted(info['control_codes'])}")
    
    # Focus on likely energy-related addresses
    print("\n" + "=" * 70)
    print("ENERGY MONITOR CANDIDATE ANALYSIS")
    print("=" * 70)
    
    # These addresses are likely to contain energy data:
    # 110->490: MFD to system status (very frequent)
    # 210->490: Possibly hybrid ECU to system
    # 112->060: MFD alternate
    
    candidates = [
        ("110", "490"),  # MFD to system - most common
        ("210", "490"),  # Possibly hybrid system
        ("112", "060"),  # MFD alternate channel
        ("B10", "490"),  # Unknown but to 490
        ("D10", "490"),  # Unknown but to 490
    ]
    
    for master, slave in candidates:
        print(f"\n--- {master} -> {slave} ---")
        
        matches = find_variable_data(all_messages, master, slave)
        if not matches:
            print("  No messages found")
            continue
        
        print(f"  Total messages: {len(matches)}")
        
        # Group by data pattern signature (first 2-3 bytes as signature)
        by_signature = defaultdict(list)
        for m in matches:
            if len(m['data']) >= 2:
                sig = tuple(m['data'][:2])
            else:
                sig = tuple(m['data'])
            by_signature[sig].append(m)
        
        print(f"  Unique signatures (first 2 bytes): {len(by_signature)}")
        
        for sig, sig_msgs in sorted(by_signature.items(), key=lambda x: -len(x[1]))[:10]:
            sig_hex = ' '.join(b if isinstance(b, str) else f"{b:02X}" for b in sig)
            print(f"\n  Signature [{sig_hex}] - {len(sig_msgs)} messages:")
            
            # Analyze variation
            analysis = analyze_data_variation(sig_msgs)
            
            if analysis.get('variable_bytes'):
                print(f"    Length: {analysis['length']} bytes")
                print(f"    Variable positions:")
                for pos, info in sorted(analysis['variable_bytes'].items()):
                    print(f"      Byte {pos}: range {info['min']:02X}-{info['max']:02X} "
                          f"({info['unique_values']} unique, delta={info['range']})")
                
                # Show a few samples
                print(f"    Samples:")
                for m in sig_msgs[:5]:
                    print(f"      [{m['seq']:>4}] {hex_data(m['data'])}")
    
    # Special focus on 210->490 which may contain ICE/battery state
    print("\n" + "=" * 70)
    print("DEEP ANALYSIS: 210 -> 490 (Likely Hybrid System)")
    print("=" * 70)
    
    matches_210 = find_variable_data(all_messages, "210", "490")
    
    if matches_210:
        print(f"\nAll {len(matches_210)} unique data patterns:\n")
        
        seen_patterns = set()
        for m in matches_210:
            pattern = hex_data(m['data'])
            if pattern not in seen_patterns:
                seen_patterns.add(pattern)
                print(f"  [{m['seq']:>4}] ts={m['ts']:>8} : {pattern}")
    
    # Look for messages that might contain SOC or power flow
    print("\n" + "=" * 70)
    print("LOOKING FOR SOC/POWER DATA PATTERNS")
    print("=" * 70)
    
    # SOC is typically 0-100 or 0-255 scaled
    # Look for bytes that could represent percentage values
    
    print("\nSearching for bytes in 40-100 range (potential SOC %)...")
    
    for master, slave in candidates:
        matches = find_variable_data(all_messages, master, slave)
        
        for m in matches[:50]:  # Check first 50 messages
            data = m['data']
            for i, b in enumerate(data):
                if isinstance(b, str):
                    val = int(b, 16)
                else:
                    val = b
                
                # SOC typically 40-80% for normal operation
                if 40 <= val <= 100:
                    print(f"  {master}->{slave} [{m['seq']:>4}] byte[{i}]={val:3} (0x{val:02X}) : {hex_data(data)}")
                    break  # Just show first match per message


if __name__ == "__main__":
    main()
