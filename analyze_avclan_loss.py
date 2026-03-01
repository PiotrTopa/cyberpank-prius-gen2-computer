"""Analyze frame loss and check for alternative steering wheel addresses."""
import json
from collections import defaultdict, Counter

# Load both recordings
def load_rec2():
    msgs = []
    with open('logs/comm_20260221_113214.ndjson') as f:
        for line in f:
            try:
                obj = json.loads(line.strip())
                if obj.get('id') == 2:
                    d = obj['d']
                    msgs.append({
                        'ts': obj['ts'], 'm': d['m'], 's': d['s'],
                        'c': d['c'], 'd': d['d']
                    })
            except: pass
    return msgs

def load_rec1():
    with open('logs/avclan_20260219_202855.json') as f:
        raw = json.load(f)
    return [{'ts': fr['ts'], 'm': fr['master'], 's': fr['slave'],
             'c': fr['control'], 'd': fr['data_hex']} for fr in raw['frames']]

rec1 = load_rec1()
rec2 = load_rec2()

# === Check for Flerchinger ST_WHEEL address 0x1CC ===
print("=== SEARCH FOR FLERCHINGER ADDRESSES ===")
flerchinger = {
    '1CC': 'ST_WHEEL',
    '190': 'AUDIO_HU (Prius)',
    '178': 'NAVI',
    '110': 'EMV/MFD',
    '360': 'CD_CH1',
    '1C4': 'PANEL',
    '1D8': 'CONT_SW',
}

for addr, name in flerchinger.items():
    # Search as master
    as_master_r1 = sum(1 for m in rec1 if m['m'] == addr)
    as_slave_r1 = sum(1 for m in rec1 if m['s'] == addr)
    as_master_r2 = sum(1 for m in rec2 if m['m'] == addr)
    as_slave_r2 = sum(1 for m in rec2 if m['s'] == addr)
    if as_master_r1 + as_slave_r1 + as_master_r2 + as_slave_r2 > 0:
        print(f"  {addr} ({name}): R1 m={as_master_r1} s={as_slave_r1}, R2 m={as_master_r2} s={as_slave_r2}")

# Check Hamming-distance-1 from 1CC
print("\n=== HAMMING ≤2 FROM 1CC (steering wheel physical addr) ===")
def hamming(a, b):
    va, vb = int(a, 16), int(b, 16)
    return bin(va ^ vb).count('1')

# Collect all address pairs from both recordings
all_addrs = set()
for m in rec1 + rec2:
    all_addrs.add(m['m'])
    all_addrs.add(m['s'])

near_1cc = sorted([a for a in all_addrs if hamming(a, '1CC') <= 2])
print(f"  Addresses near 0x1CC: {near_1cc}")
for a in near_1cc:
    r1m = sum(1 for m in rec1 if m['m'] == a)
    r1s = sum(1 for m in rec1 if m['s'] == a)
    r2m = sum(1 for m in rec2 if m['m'] == a)
    r2s = sum(1 for m in rec2 if m['s'] == a)
    print(f"    {a} (hamming={hamming(a, '1CC')}): R1 m={r1m} s={r1s}, R2 m={r2m} s={r2s}")

# === Quantify frame loss in rec2 ===
print("\n=== FRAME LOSS ANALYSIS (Recording 2) ===")
# Count all messages (AVC + CAN)
total_all = 0
total_avc = 0
total_can = 0
with open('logs/comm_20260221_113214.ndjson') as f:
    for line in f:
        try:
            obj = json.loads(line.strip())
            total_all += 1
            if obj.get('id') == 1: total_can += 1
            elif obj.get('id') == 2: total_avc += 1
        except: pass

duration = rec2[-1]['ts'] - rec2[0]['ts']
print(f"  Total messages: {total_all}")
print(f"  CAN messages: {total_can}")
print(f"  AVC-LAN messages: {total_avc}")
print(f"  AVC-LAN rate: {total_avc/duration:.2f} msgs/sec over {duration:.0f}s")
print(f"  Expected AVC-LAN rate on busy bus: 10-50 msgs/sec")
print(f"  Estimated capture rate: ~{total_avc/duration / 30 * 100:.0f}% (vs 30/s estimated)")

# === Look at decode batches (same timestamp = processed together) ===
print("\n=== DECODE BATCH ANALYSIS ===")
ts_groups = defaultdict(list)
for m in rec2:
    ts_groups[m['ts']].append(m)

batch_sizes = [len(msgs) for msgs in ts_groups.values()]
print(f"  Unique timestamps: {len(ts_groups)}")
print(f"  Batch size distribution:")
size_counts = Counter(batch_sizes)
for size in sorted(size_counts.keys()):
    print(f"    {size} frame(s): {size_counts[size]} batches")
print(f"  Max batch: {max(batch_sizes)} frames at ts={max(ts_groups.keys(), key=lambda t: len(ts_groups[t])):.1f}")

# Average gap between decode events
ts_list = sorted(ts_groups.keys())
if len(ts_list) > 1:
    gaps = [ts_list[i+1] - ts_list[i] for i in range(len(ts_list)-1)]
    avg_gap = sum(gaps) / len(gaps)
    min_gap = min(gaps)
    max_gap = max(gaps)
    print(f"  Gap between decode events: avg={avg_gap:.1f}s min={min_gap:.1f}s max={max_gap:.1f}s")

# === 040->200 payload analysis across both recordings ===
print("\n=== 040->200 COMPLETE PAYLOAD CATALOG ===")
all_040_200 = []
for m in rec1 + rec2:
    if m['m'] == '040' and m['s'] == '200':
        all_040_200.append(m)

payload_counter = Counter(' '.join(m['d']) for m in all_040_200)
print(f"  Total 040->200 messages: {len(all_040_200)}")
print(f"  Unique payloads: {len(payload_counter)}")
for payload, count in payload_counter.most_common():
    d = payload.split()
    btn_code = f"0x{d[2]}{d[3]}" if len(d) >= 4 else "?"
    cmd_type = "PRESS" if d[0] == '28' else "RELEASE" if d[0] == '2A' else f"ALT({d[0]})"
    suffix = d[4] if len(d) >= 5 else "?"
    print(f"  {count:>3}x {payload:<20} cmd={cmd_type:<8} code={btn_code} suffix=0x{suffix}")

# === Which CAN subscription gaps correlate with AVC-LAN capture? ===
print("\n=== CAN SUBSCRIPTION BLOCKING ANALYSIS ===")
# Load CAN subscription responses
can_subs = []
with open('logs/comm_20260221_113214.ndjson') as f:
    for line in f:
        try:
            obj = json.loads(line.strip())
            if obj.get('id') == 1:
                d = obj.get('d', {})
                if d.get('a') == 'sub':
                    can_subs.append(obj['ts'])
        except: pass

if can_subs:
    print(f"  CAN subscription responses: {len(can_subs)}")
    # Find periods with NO CAN subs (gateway not blocking)
    can_gaps = [can_subs[i+1] - can_subs[i] for i in range(len(can_subs)-1)]
    print(f"  CAN sub response gap: avg={sum(can_gaps)/len(can_gaps):.2f}s min={min(can_gaps):.3f}s max={max(can_gaps):.2f}s")
else:
    print("  No CAN subscription responses found")
