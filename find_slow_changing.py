"""
Find slowly changing (high inertia) values in AVC-LAN varying bytes.

Temperature-like values should:
1. Change slowly between consecutive readings
2. Have low standard deviation of differences
3. Not jump around wildly

This script filters to find candidates for temperature data.
"""

import json
from collections import defaultdict
import math


def calculate_smoothness(values):
    """
    Calculate how "smooth" a series is.
    Returns tuple: (avg_abs_change, max_change, change_stddev)
    Lower values = smoother = more like temperature.
    """
    if len(values) < 2:
        return (999, 999, 999)
    
    changes = []
    for i in range(1, len(values)):
        changes.append(abs(values[i] - values[i-1]))
    
    avg_change = sum(changes) / len(changes)
    max_change = max(changes)
    
    # Standard deviation of changes
    if len(changes) > 1:
        mean = sum(changes) / len(changes)
        variance = sum((c - mean) ** 2 for c in changes) / len(changes)
        stddev = math.sqrt(variance)
    else:
        stddev = 0
    
    return (avg_change, max_change, stddev)


def main():
    # Load AVC-LAN messages
    msgs = []
    with open('assets/data/avc_lan_messages.ndjson', 'r') as f:
        for line in f:
            if line.strip():
                msgs.append(json.loads(line))
    
    print(f"Loaded {len(msgs)} AVC-LAN messages")
    msgs.sort(key=lambda x: x.get('seq', 0))
    
    # Group by address pair and extract time series for each byte
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
    
    # Analyze each byte series for smoothness
    smooth_series = []
    
    for pair, pair_msgs in by_pair.items():
        if len(pair_msgs) < 5:  # Need enough data points
            continue
        
        max_len = max(len(m['data']) for m in pair_msgs)
        
        for byte_idx in range(max_len):
            # Extract values for this byte
            values = []
            timestamps = []
            for m in pair_msgs:
                if byte_idx < len(m['data']):
                    values.append(m['data'][byte_idx])
                    timestamps.append(m['seq'])
            
            if len(values) < 5:
                continue
            
            # Check if it varies (constant values not interesting)
            unique = set(values)
            if len(unique) < 2:
                continue
            
            # Skip if range is too large (likely not temperature)
            val_range = max(values) - min(values)
            if val_range > 100:  # Temperature unlikely to span > 100 raw units
                continue
            
            # Calculate smoothness metrics
            avg_change, max_change, stddev = calculate_smoothness(values)
            
            # Filter for smooth series (low average change, low max jump)
            # Temperature should change by small amounts between readings
            if avg_change < 5 and max_change < 20:
                smooth_series.append({
                    'pair': pair,
                    'byte_idx': byte_idx,
                    'key': f"{pair}_b{byte_idx}",
                    'count': len(values),
                    'unique': len(unique),
                    'min': min(values),
                    'max': max(values),
                    'range': val_range,
                    'avg_change': avg_change,
                    'max_change': max_change,
                    'stddev': stddev,
                    'values': values,
                    'timestamps': timestamps
                })
    
    # Sort by smoothness (lower avg_change = smoother)
    smooth_series.sort(key=lambda x: (x['avg_change'], x['max_change']))
    
    print(f"\n{'='*90}")
    print("SLOWLY CHANGING (HIGH INERTIA) BYTE SERIES")
    print(f"{'='*90}")
    print(f"Filtered to: avg_change < 5, max_change < 20, range < 100")
    print(f"Found {len(smooth_series)} smooth series")
    print()
    print(f"{'Key':<22} {'N':>4} {'Uniq':>4} {'Min':>4} {'Max':>4} {'Range':>5} "
          f"{'AvgChg':>6} {'MaxChg':>6}  Progression")
    print("-" * 90)
    
    for s in smooth_series[:30]:  # Top 30 smoothest
        # Show value progression
        vals = s['values']
        if len(vals) > 12:
            progression = vals[:6] + ['...'] + vals[-3:]
        else:
            progression = vals
        prog_str = ', '.join(str(v) for v in progression)
        
        print(f"{s['key']:<22} {s['count']:>4} {s['unique']:>4} {s['min']:>4} {s['max']:>4} "
              f"{s['range']:>5} {s['avg_change']:>6.2f} {s['max_change']:>6} "
              f" {prog_str}")
    
    # Now show temperature interpretations for the smoothest candidates
    print()
    print(f"{'='*90}")
    print("TEMPERATURE CANDIDATE ANALYSIS")
    print(f"{'='*90}")
    
    for s in smooth_series[:15]:
        vals = s['values']
        first_avg = sum(vals[:3]) / min(3, len(vals))
        last_avg = sum(vals[-3:]) / min(3, len(vals))
        
        print(f"\n{s['key']}:")
        print(f"  Count: {s['count']}, Range: {s['min']}-{s['max']}")
        print(f"  Smoothness: avg_change={s['avg_change']:.2f}, max_change={s['max_change']}")
        print(f"  First 3 avg: {first_avg:.1f}, Last 3 avg: {last_avg:.1f}")
        print(f"  Decodings (first -> last):")
        print(f"    raw:        {first_avg:.1f} -> {last_avg:.1f}")
        print(f"    /2:         {first_avg/2:.1f}C -> {last_avg/2:.1f}C")
        print(f"    (x-18)/2:   {(first_avg-18)/2:.1f}C -> {(last_avg-18)/2:.1f}C")
        print(f"    (x-40):     {first_avg-40:.1f}C -> {last_avg-40:.1f}C")
        print(f"    (x-50)/2:   {(first_avg-50)/2:.1f}C -> {(last_avg-50)/2:.1f}C")
        
        # Show full progression
        print(f"  Values: ", end='')
        for i, v in enumerate(vals):
            if i > 0 and vals[i] != vals[i-1]:
                print(f"[{v}]", end=' ')  # Highlight changes
            else:
                print(f"{v}", end=' ')
        print()
    
    # Export smooth series to CSV
    output_file = 'assets/data/avc_smooth_bytes.csv'
    with open(output_file, 'w') as f:
        # Header
        f.write("key,count,unique,min,max,range,avg_change,max_change,first_val,last_val\n")
        for s in smooth_series:
            f.write(f"{s['key']},{s['count']},{s['unique']},{s['min']},{s['max']},"
                   f"{s['range']},{s['avg_change']:.3f},{s['max_change']},"
                   f"{s['values'][0]},{s['values'][-1]}\n")
    
    print(f"\nExported {len(smooth_series)} smooth series to: {output_file}")


if __name__ == '__main__':
    main()
