"""Deep analysis: what happens around 040->200 timestamps?
Also check for Hamming-distance-1 garbled versions of 040->200."""
import json
from collections import defaultdict

# Load all AVC-LAN messages
msgs = []
with open('logs/comm_20260221_113214.ndjson') as f:
    for line in f:
        try:
            obj = json.loads(line.strip())
            if obj.get('id') == 2:
                d = obj['d']
                msgs.append({
                    'ts': obj['ts'],
                    'm': d['m'], 's': d['s'],
                    'c': d['c'], 'd': d['d']
                })
        except:
            pass

# Timestamps where 040->200 appears
event_ts = [271.1, 326.9, 362.1, 416.5]

# Show EVERYTHING within ±5 seconds of each event
print("=== MESSAGES WITHIN ±5 SECONDS OF 040->200 EVENTS ===\n")
for et in event_ts:
    print(f"--- Event at ts={et:.1f}s ---")
    window = [m for m in msgs if abs(m['ts'] - et) < 5.0]
    for m in window:
        d_hex = ' '.join(m['d'])
        marker = " <<<" if m['m'] == '040' and m['s'] == '200' else ""
        print(f"  ts={m['ts']:>7.1f}  {m['m']}->{m['s']} c={m['c']} [{len(m['d'])}] {d_hex}{marker}")
    print()

# Hamming distance between two hex address strings
def hamming_12bit(a, b):
    va, vb = int(a, 16), int(b, 16)
    diff = va ^ vb
    return bin(diff).count('1')

# Find ALL messages to slave 200 or from master 040
print("\n=== ALL MESSAGES TO SLAVE 200 (any master) ===")
to_200 = [m for m in msgs if m['s'] == '200']
for m in to_200:
    d_hex = ' '.join(m['d'])
    dist = hamming_12bit(m['m'], '040')
    print(f"  ts={m['ts']:>7.1f}  {m['m']}->{m['s']} c={m['c']} [{len(m['d'])}] hmg_040={dist} {d_hex}")

print("\n=== ALL MESSAGES FROM MASTER 040 (any slave) ===")
from_040 = [m for m in msgs if m['m'] == '040']
for m in from_040:
    d_hex = ' '.join(m['d'])
    dist = hamming_12bit(m['s'], '200')
    print(f"  ts={m['ts']:>7.1f}  {m['m']}->{m['s']} c={m['c']} [{len(m['d'])}] hmg_200={dist} {d_hex}")

# Find messages with Hamming ≤2 from 040 (master) AND Hamming ≤2 from 200 (slave)
print("\n=== MESSAGES WITH GARBLED 040->200 (Hamming ≤ 2 on both) ===")
for m in msgs:
    dm = hamming_12bit(m['m'], '040')
    ds = hamming_12bit(m['s'], '200')
    if dm <= 2 and ds <= 2 and not (dm == 0 and ds == 0):
        d_hex = ' '.join(m['d'])
        print(f"  ts={m['ts']:>7.1f}  {m['m']}->{m['s']} hm={dm} hs={ds} c={m['c']} [{len(m['d'])}] {d_hex}")

# Look at the 040->200 data bytes more carefully
print("\n=== 040->200 DATA BYTE ANALYSIS ===")
for m in [m for m in msgs if m['m']=='040' and m['s']=='200']:
    d = [int(x, 16) for x in m['d']]
    print(f"  ts={m['ts']:>7.1f} raw={m['d']}")
    print(f"    byte0={d[0]:08b} byte1={d[1]:08b} byte2={d[2]:08b} byte3={d[3]:08b} byte4={d[4]:08b}")
    # Try as nibbles
    print(f"    nibbles: {d[0]>>4:X}.{d[0]&0xF:X} {d[1]>>4:X}.{d[1]&0xF:X} {d[2]>>4:X}.{d[2]&0xF:X} {d[3]>>4:X}.{d[3]&0xF:X} {d[4]>>4:X}.{d[4]&0xF:X}")

# Search for ANY 5-byte messages (matching the 040->200 pattern)
print("\n=== ALL 5-BYTE MESSAGES IN RECORDING ===")
five_byte = [m for m in msgs if len(m['d']) == 5]
for m in five_byte:
    d_hex = ' '.join(m['d'])
    print(f"  ts={m['ts']:>7.1f}  {m['m']}->{m['s']} c={m['c']} {d_hex}")

# Check: do timestamps of 040->200 events correlate with any OTHER recurring pair?
print("\n=== PAIRS THAT APPEAR AT SAME TIMESTAMPS AS 040->200 ===")
for et in event_ts:
    same_ts = [m for m in msgs if abs(m['ts'] - et) < 0.1]
    print(f"  @{et:.1f}s: {len(same_ts)} msgs: {[(m['m']+'->'+m['s']) for m in same_ts]}")
