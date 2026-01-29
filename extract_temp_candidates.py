"""
Extract time series data for the 4 temperature candidate bytes.

Creates individual CSV files for each candidate with raw values and decoded temperatures.
"""

import json
from collections import defaultdict


def main():
    # Load AVC-LAN messages
    msgs = []
    with open('assets/data/avc_lan_messages.ndjson', 'r') as f:
        for line in f:
            if line.strip():
                msgs.append(json.loads(line))
    
    msgs.sort(key=lambda x: x.get('seq', 0))
    print(f"Loaded {len(msgs)} AVC-LAN messages")
    
    # Extract time series for each candidate
    candidates = {
        '10C_310': {'master': '10C', 'slave': '310', 'byte': 4, 'filter_b6': 0x90},
        '112_060': {'master': '112', 'slave': '060', 'byte': 3, 'filter_b6': None},
        'D10_490': {'master': 'D10', 'slave': '490', 'byte': 1, 'filter_b6': None},
        'B10_490': {'master': 'B10', 'slave': '490', 'byte': 1, 'filter_b6': None},
    }
    
    for name, config in candidates.items():
        data_points = []
        
        for msg in msgs:
            d = msg.get('d', {})
            if d.get('m') == config['master'] and d.get('s') == config['slave']:
                data_hex = d.get('d', [])
                byte_idx = config['byte']
                
                if len(data_hex) <= byte_idx:
                    continue
                
                # Apply filter if specified
                if config['filter_b6'] is not None:
                    if len(data_hex) < 7:
                        continue
                    b6 = int(data_hex[6], 16)
                    if b6 != config['filter_b6']:
                        continue
                
                raw_val = int(data_hex[byte_idx], 16)
                ts = msg.get('ts', 0)
                seq = msg.get('seq', 0)
                full_data = ' '.join(data_hex)
                
                data_points.append({
                    'ts': ts,
                    'seq': seq,
                    'raw': raw_val,
                    'data': full_data
                })
        
        # Write CSV file
        output_file = f'assets/data/temp_candidate_{name}.csv'
        with open(output_file, 'w') as f:
            f.write('timestamp,seq,raw_value,div2,minus18_div2,minus40,full_data\n')
            for dp in data_points:
                raw = dp['raw']
                div2 = raw / 2.0
                minus18_div2 = (raw - 18) / 2.0
                minus40 = raw - 40
                f.write(f"{dp['ts']},{dp['seq']},{raw},{div2:.1f},{minus18_div2:.1f},{minus40},{dp['data']}\n")
        
        print(f"\n{name} (byte {config['byte']}):")
        print(f"  Saved {len(data_points)} data points to {output_file}")
        
        if data_points:
            raws = [dp['raw'] for dp in data_points]
            print(f"  Raw values: min={min(raws)}, max={max(raws)}, unique={len(set(raws))}")
            print(f"  Sample progression: {raws[:10]}{'...' if len(raws) > 10 else ''}")
    
    # Also create a combined file for easy comparison
    print("\n" + "="*60)
    print("Creating combined time series file...")
    
    # Collect all data by sequence number
    all_data = defaultdict(dict)
    
    for name, config in candidates.items():
        for msg in msgs:
            d = msg.get('d', {})
            if d.get('m') == config['master'] and d.get('s') == config['slave']:
                data_hex = d.get('d', [])
                byte_idx = config['byte']
                
                if len(data_hex) <= byte_idx:
                    continue
                
                # Apply filter if specified
                if config['filter_b6'] is not None:
                    if len(data_hex) < 7:
                        continue
                    b6 = int(data_hex[6], 16)
                    if b6 != config['filter_b6']:
                        continue
                
                raw_val = int(data_hex[byte_idx], 16)
                ts = msg.get('ts', 0)
                seq = msg.get('seq', 0)
                
                all_data[seq]['ts'] = ts
                all_data[seq][f'{name}_raw'] = raw_val
                all_data[seq][f'{name}_temp'] = (raw_val - 18) / 2.0
    
    # Write combined CSV
    combined_file = 'assets/data/temp_candidates_combined.csv'
    with open(combined_file, 'w') as f:
        headers = ['seq', 'timestamp']
        for name in candidates.keys():
            headers.extend([f'{name}_raw', f'{name}_temp_C'])
        f.write(','.join(headers) + '\n')
        
        for seq in sorted(all_data.keys()):
            row = [str(seq), str(all_data[seq].get('ts', ''))]
            for name in candidates.keys():
                raw = all_data[seq].get(f'{name}_raw', '')
                temp = all_data[seq].get(f'{name}_temp', '')
                row.append(str(raw) if raw != '' else '')
                row.append(f'{temp:.1f}' if temp != '' else '')
            f.write(','.join(row) + '\n')
    
    print(f"Saved combined data to {combined_file}")
    print(f"Total rows: {len(all_data)}")


if __name__ == '__main__':
    main()
