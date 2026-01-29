
import json
import sys
import os
from collections import defaultdict

# Add the project root to the path so we can import modules
sys.path.append(os.getcwd())

from cyberpunk_computer.comm.avc_decoder import AVCMessage, AVCDecoder, parse_button_event, parse_touch_event

def analyze_file(filename):
    print(f"Analyzing {filename}...")
    
    decoder = AVCDecoder()
    
    # Track patterns by length
    patterns_by_length = defaultdict(list)
    
    with open(filename, 'r') as f:
        lines = f.readlines()
        
    print(f"Read {len(lines)} lines.")
    print("\n=== TOUCH EVENTS ===\n")
    
    for i, line in enumerate(lines):
        try:
            entry = json.loads(line)
            
            decoded = decoder.decode_message(entry)
            if not decoded:
                continue

            classification = decoder.classify_message(decoded)
            
            # Check for Buttons
            if classification == 'button_press':
                btn = parse_button_event(decoded.data)
                print(f"Line {i} (Seq {entry.get('seq')}): BUTTON {btn}")

            # Check for Touch
            if classification == 'touch_event':
                touch = parse_touch_event(decoded.data)
                data_hex = ' '.join([f'{x:02X}' for x in decoded.data])
                print(f"Line {i} (Seq {entry.get('seq')}): TOUCH {decoded.master_addr:03X}->{decoded.slave_addr:03X}")
                print(f"    Raw Hex ({len(decoded.data)} bytes): {data_hex}")
                if touch:
                    print(f"    Parsed: type={touch.touch_type}, x={touch.x}, y={touch.y}, conf={touch.confidence}")
                
                # Save pattern for analysis
                patterns_by_length[len(decoded.data)].append({
                    'seq': entry.get('seq'),
                    'data': decoded.data,
                    'hex': data_hex
                })


        except json.JSONDecodeError:
            continue
        except Exception as e:
            print(f"Error on line {i}: {e}")
    
    # Print pattern summary
    print("\n=== PATTERN SUMMARY BY LENGTH ===\n")
    for length in sorted(patterns_by_length.keys()):
        patterns = patterns_by_length[length]
        print(f"{length}-byte messages ({len(patterns)} occurrences):")
        # Show first few examples
        for p in patterns[:3]:
            print(f"    Seq {p['seq']}: {p['hex']}")
        if len(patterns) > 3:
            print(f"    ... and {len(patterns) - 3} more")
        print()

if __name__ == "__main__":
    analyze_file("assets/data/avc_analysis_2.ndjson")
