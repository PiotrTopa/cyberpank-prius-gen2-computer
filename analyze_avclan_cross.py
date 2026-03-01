"""Cross-reference two recordings to find CONSISTENT AVC-LAN patterns.
Recording 1: avclan_20260219_202855.json (2615 frames, older)
Recording 2: comm_20260221_113214.ndjson (669 AVC frames, newer)
"""
import json
from collections import defaultdict, Counter

# Load recording 1 (already extracted AVC-LAN JSON)
with open('logs/avclan_20260219_202855.json') as f:
    raw = json.load(f)
    rec1 = []
    for fr in raw['frames']:
        rec1.append({
            'ts': fr['ts'],
            'm': fr['master'], 's': fr['slave'],
            'c': fr['control'], 'd': fr['data_hex']
        })

# Load recording 2
rec2 = []
with open('logs/comm_20260221_113214.ndjson') as f:
    for line in f:
        try:
            obj = json.loads(line.strip())
            if obj.get('id') == 2:
                d = obj['d']
                rec2.append({
                    'ts': obj['ts'],
                    'm': d['m'], 's': d['s'],
                    'c': d['c'], 'd': d['d']
                })
        except:
            pass

print(f"Recording 1: {len(rec1)} frames")
print(f"Recording 2: {len(rec2)} frames")

# Group by pair
def group_by_pair(msgs):
    pairs = defaultdict(list)
    for m in msgs:
        key = f"{m['m']}->{m['s']}"
        pairs[key].append(m)
    return pairs

p1 = group_by_pair(rec1)
p2 = group_by_pair(rec2)

# Find pairs present in BOTH recordings
common = set(p1.keys()) & set(p2.keys())
only1 = set(p1.keys()) - set(p2.keys())
only2 = set(p2.keys()) - set(p1.keys())

print(f"\nCommon pairs: {len(common)}")
print(f"Only in rec1: {len(only1)}")
print(f"Only in rec2: {len(only2)}")

print("\n=== COMMON PAIRS (present in both recordings) ===")
print(f"{'Pair':<12} {'R1':>5} {'R2':>5} {'R1_uniq':>7} {'R2_uniq':>7} {'c_vals':>12}")
print('-' * 60)

for key in sorted(common, key=lambda k: len(p1[k])+len(p2[k]), reverse=True):
    n1 = len(p1[key])
    n2 = len(p2[key])
    u1 = len(set(str(m['d']) for m in p1[key]))
    u2 = len(set(str(m['d']) for m in p2[key]))
    c1 = set(m['c'] for m in p1[key])
    c2 = set(m['c'] for m in p2[key])
    c_all = sorted(c1 | c2)
    print(f"{key:<12} {n1:>5} {n2:>5} {u1:>7} {u2:>7} {str(c_all):>12}")

# For 040->200 specifically, compare data across recordings
print("\n=== 040->200 COMPARISON ===")
if '040->200' in p1:
    print(f"Recording 1 ({len(p1['040->200'])} msgs):")
    for m in p1['040->200']:
        print(f"  ts={m['ts']:>7.1f} c={m['c']} d={' '.join(m['d'])}")
else:
    print("040->200 NOT in recording 1")

if '040->200' in p2:
    print(f"Recording 2 ({len(p2['040->200'])} msgs):")
    for m in p2['040->200']:
        print(f"  ts={m['ts']:>7.1f} c={m['c']} d={' '.join(m['d'])}")

# Check ALL messages to slave 200 in recording 1
print("\n=== ALL MESSAGES TO SLAVE 200 IN RECORDING 1 ===")
to_200_r1 = [(k, m) for k, ms in p1.items() for m in ms if m['s'] == '200']
to_200_r1.sort(key=lambda x: x[1]['ts'])
for key, m in to_200_r1[:30]:
    print(f"  ts={m['ts']:>7.1f} {key:<12} c={m['c']} [{len(m['d'])}] {' '.join(m['d'][:10])}")
print(f"  ... total: {len(to_200_r1)} messages to slave 200")

# Look at the top pairs in rec1 to see if they match rec2's top pairs
print("\n=== TOP PAIRS IN RECORDING 1 ===")
for key in sorted(p1.keys(), key=lambda k: len(p1[k]), reverse=True)[:20]:
    n = len(p1[key])
    u = len(set(str(m['d']) for m in p1[key]))
    c_vals = sorted(set(m['c'] for m in p1[key]))
    lens = Counter(len(m['d']) for m in p1[key])
    print(f"  {key:<12} n={n:>4} uniq={u:>4} c={c_vals} lens={dict(lens)}")

# Check 110->490 data consistency across recordings
print("\n=== 110->490 DATA COMPARISON (top 10 payloads each) ===")
for label, pair_data in [("Rec1", p1.get('110->490', [])), ("Rec2", p2.get('110->490', []))]:
    counter = Counter(str(m['d']) for m in pair_data)
    print(f"\n{label} ({len(pair_data)} msgs):")
    for payload, count in counter.most_common(10):
        print(f"  {count:>4}x {payload}")

# Also check 400->020
print("\n=== 400->020 DATA COMPARISON ===")
for label, pair_data in [("Rec1", p1.get('400->020', [])), ("Rec2", p2.get('400->020', []))]:
    counter = Counter(str(m['d']) for m in pair_data)
    print(f"\n{label} ({len(pair_data)} msgs):")
    for payload, count in counter.most_common(10):
        print(f"  {count:>4}x {payload}")

# Find 5-byte messages in recording 1
print("\n=== 5-BYTE MESSAGES IN RECORDING 1 ===")
five_byte_r1 = [(k,m) for k, ms in p1.items() for m in ms if len(m['d']) == 5]
five_byte_r1.sort(key=lambda x: x[1]['ts'])
for key, m in five_byte_r1:
    print(f"  ts={m['ts']:>7.1f} {key:<12} c={m['c']} {' '.join(m['d'])}")
print(f"  Total: {len(five_byte_r1)} five-byte messages")
