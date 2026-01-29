"""
Extract all temporally varying bytes from AVC-LAN messages.

This script analyzes all AVC-LAN messages, finds bytes that change over time,
and outputs them as time series data for visualization.

Output: CSV file with columns for each varying byte, indexed by timestamp.
"""

import json
import csv
from collections import defaultdict
from pathlib import Path


def main():
    # Load AVC-LAN messages
    msgs = []
    with open('assets/data/avc_lan_messages.ndjson', 'r') as f:
        for line in f:
            if line.strip():
                msgs.append(json.loads(line))
    
    print(f"Loaded {len(msgs)} AVC-LAN messages")
    
    # Sort by sequence number (chronological order)
    msgs.sort(key=lambda x: x.get('seq', 0))
    
    # Group messages by address pair
    by_pair = defaultdict(list)
    for msg in msgs:
        d = msg.get('d', {})
        m = d.get('m', '')
        s = d.get('s', '')
        pair = f"{m}_{s}"
        
        data_hex = d.get('d', [])
        data_int = [int(b, 16) for b in data_hex]
        
        by_pair[pair].append({
            'ts': msg.get('ts', 0),
            'seq': msg.get('seq', 0),
            'data': data_int
        })
    
    print(f"Found {len(by_pair)} unique address pairs")
    
    # Analyze each address pair for varying bytes
    varying_series = {}  # key: "PAIR_byteN" -> list of (ts, value)
    
    for pair, pair_msgs in by_pair.items():
        if len(pair_msgs) < 3:
            # Need at least 3 messages to see variation
            continue
        
        # Find max data length for this pair
        max_len = max(len(m['data']) for m in pair_msgs)
        
        # For each byte position, check if it varies
        for byte_idx in range(max_len):
            values = []
            for m in pair_msgs:
                if byte_idx < len(m['data']):
                    values.append(m['data'][byte_idx])
            
            # Check if this byte varies
            unique_values = set(values)
            if len(unique_values) > 1:
                # This byte varies - extract time series
                series_key = f"{pair}_b{byte_idx}"
                series_data = []
                for m in pair_msgs:
                    if byte_idx < len(m['data']):
                        series_data.append({
                            'ts': m['ts'],
                            'seq': m['seq'],
                            'value': m['data'][byte_idx]
                        })
                varying_series[series_key] = {
                    'data': series_data,
                    'unique_count': len(unique_values),
                    'min': min(values),
                    'max': max(values),
                    'pair': pair,
                    'byte_idx': byte_idx
                }
    
    print(f"\nFound {len(varying_series)} varying byte series")
    print()
    
    # Print summary of all varying bytes
    print("=" * 80)
    print("VARYING BYTES SUMMARY")
    print("=" * 80)
    print(f"{'Series':<20} {'Count':>6} {'Unique':>6} {'Min':>5} {'Max':>5}  Sample Values")
    print("-" * 80)
    
    for key in sorted(varying_series.keys()):
        info = varying_series[key]
        sample_values = [d['value'] for d in info['data'][:10]]
        sample_str = ', '.join(f"{v:02X}" for v in sample_values)
        print(f"{key:<20} {len(info['data']):>6} {info['unique_count']:>6} "
              f"{info['min']:>5} {info['max']:>5}  {sample_str}")
    
    # Export to CSV for easy visualization
    output_file = 'assets/data/avc_varying_bytes.csv'
    
    # Collect all unique timestamps
    all_timestamps = set()
    for key, info in varying_series.items():
        for d in info['data']:
            all_timestamps.add((d['ts'], d['seq']))
    
    all_timestamps = sorted(all_timestamps, key=lambda x: x[1])  # Sort by seq
    
    # Create CSV with all series as columns
    with open(output_file, 'w', newline='') as f:
        writer = csv.writer(f)
        
        # Header
        header = ['timestamp', 'seq'] + sorted(varying_series.keys())
        writer.writerow(header)
        
        # Create lookup for fast access
        series_lookup = {}
        for key, info in varying_series.items():
            series_lookup[key] = {(d['ts'], d['seq']): d['value'] for d in info['data']}
        
        # Write rows
        for ts, seq in all_timestamps:
            row = [ts, seq]
            for key in sorted(varying_series.keys()):
                value = series_lookup[key].get((ts, seq), '')
                row.append(value)
            writer.writerow(row)
    
    print()
    print(f"Exported time series to: {output_file}")
    
    # Also create a more detailed analysis file with decoded interpretations
    analysis_file = 'assets/data/avc_varying_analysis.txt'
    with open(analysis_file, 'w', encoding='utf-8') as f:
        f.write("AVC-LAN VARYING BYTES ANALYSIS\n")
        f.write("=" * 80 + "\n\n")
        f.write(f"Total messages: {len(msgs)}\n")
        f.write(f"Address pairs: {len(by_pair)}\n")
        f.write(f"Varying byte series: {len(varying_series)}\n\n")
        
        # Group by address pair for better readability
        pairs_with_varying = defaultdict(list)
        for key, info in varying_series.items():
            pairs_with_varying[info['pair']].append((info['byte_idx'], key, info))
        
        for pair in sorted(pairs_with_varying.keys()):
            master, slave = pair.split('_')
            f.write(f"\n{'='*80}\n")
            f.write(f"Address Pair: {master} → {slave}\n")
            f.write(f"{'='*80}\n\n")
            
            for byte_idx, key, info in sorted(pairs_with_varying[pair]):
                f.write(f"  Byte {byte_idx}: {info['unique_count']} unique values, "
                       f"range [{info['min']}, {info['max']}]\n")
                
                # Show progression over time
                data = info['data']
                f.write(f"    Time progression (first 20, last 10):\n")
                f.write(f"    First: ")
                for d in data[:20]:
                    f.write(f"{d['value']:02X} ")
                f.write("\n")
                f.write(f"    Last:  ")
                for d in data[-10:]:
                    f.write(f"{d['value']:02X} ")
                f.write("\n")
                
                # If values look like temperature (reasonable range), show decoded
                if 0 <= info['min'] <= 100 and 0 <= info['max'] <= 100:
                    f.write(f"    Possible temp decodings:\n")
                    first_val = data[0]['value']
                    last_val = data[-1]['value']
                    f.write(f"      raw:       first={first_val:3d}, last={last_val:3d}\n")
                    f.write(f"      /2:        first={first_val/2:.1f}C, last={last_val/2:.1f}C\n")
                    f.write(f"      -40:       first={first_val-40}C, last={last_val-40}C\n")
                    f.write(f"      (x-18)/2:  first={(first_val-18)/2:.1f}C, last={(last_val-18)/2:.1f}C\n")
                    f.write(f"      (x-40)/2:  first={(first_val-40)/2:.1f}C, last={(last_val-40)/2:.1f}C\n")
                f.write("\n")
    
    print(f"Detailed analysis saved to: {analysis_file}")
    
    # Print candidates for temperature (bytes that show gradual change in reasonable range)
    print()
    print("=" * 80)
    print("TEMPERATURE CANDIDATES (gradual change, reasonable range)")
    print("=" * 80)
    
    for key in sorted(varying_series.keys()):
        info = varying_series[key]
        
        # Skip if too few or too many unique values (noise)
        if info['unique_count'] < 2 or info['unique_count'] > 30:
            continue
        
        # Check if range is temperature-like (0-100 raw, which could be -40 to +60C)
        if info['min'] > 100 or info['max'] > 150:
            continue
        
        # Get first and last values to see trend
        first_vals = [d['value'] for d in info['data'][:5]]
        last_vals = [d['value'] for d in info['data'][-5:]]
        first_avg = sum(first_vals) / len(first_vals)
        last_avg = sum(last_vals) / len(last_vals)
        
        # Calculate different temperature interpretations
        print(f"\n{key}:")
        print(f"  Range: {info['min']}-{info['max']} ({info['unique_count']} unique values)")
        print(f"  First avg: {first_avg:.1f}, Last avg: {last_avg:.1f}")
        print(f"  Decoded as temp (first → last):")
        print(f"    raw:       {first_avg:.1f} → {last_avg:.1f}")
        print(f"    /2:        {first_avg/2:.1f}C → {last_avg/2:.1f}C")
        print(f"    -40:       {first_avg-40:.1f}C → {last_avg-40:.1f}C")
        print(f"    (x-18)/2:  {(first_avg-18)/2:.1f}C → {(last_avg-18)/2:.1f}C")
        print(f"    (x-40)/2:  {(first_avg-40)/2:.1f}C → {(last_avg-40)/2:.1f}C")


if __name__ == '__main__':
    main()
