"""Fresh zero-bias AVC-LAN analysis of comm_20260221_113214.ndjson"""
import json
import math
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
                    'm': d['m'],
                    's': d['s'],
                    'c': d['c'],
                    'd': d['d']
                })
        except:
            pass

print(f'Total AVC-LAN messages: {len(msgs)}')
print(f'Time range: {msgs[0]["ts"]:.1f}s - {msgs[-1]["ts"]:.1f}s')
print()

# Group by address pair
pairs = defaultdict(list)
for m in msgs:
    key = f'{m["m"]}->{m["s"]}'
    pairs[key].append(m)

# Classify each pair
print('=== CLASSIFICATION: PERIODIC vs EVENT-DRIVEN ===')
print(f'{"Pair":<12} {"Count":>5} {"AvgGap":>8} {"StdGap":>8} {"MinGap":>8} {"MaxGap":>8} {"UniqueD":>7} {"Type":<10}')
print('-' * 80)

for key in sorted(pairs.keys(), key=lambda k: len(pairs[k]), reverse=True):
    msgs_list = pairs[key]
    count = len(msgs_list)
    if count < 2:
        continue

    gaps = [msgs_list[i+1]['ts'] - msgs_list[i]['ts'] for i in range(len(msgs_list)-1)]
    avg_gap = sum(gaps)/len(gaps)
    std_gap = math.sqrt(sum((g-avg_gap)**2 for g in gaps)/len(gaps)) if len(gaps) > 1 else 0
    min_gap = min(gaps)
    max_gap = max(gaps)
    unique_d = len(set(str(m['d']) for m in msgs_list))
    cv = std_gap / avg_gap if avg_gap > 0 else 999

    if cv < 0.5 and count > 10:
        typ = 'PERIODIC'
    elif cv < 1.0 and count > 5:
        typ = 'SEMI-REG'
    else:
        typ = 'SPORADIC'

    print(f'{key:<12} {count:>5} {avg_gap:>8.2f} {std_gap:>8.2f} {min_gap:>8.2f} {max_gap:>8.2f} {unique_d:>7} {typ:<10}')

# === PART 2: Find messages that appear ONLY during interaction ===
print('\n\n=== TEMPORAL DISTRIBUTION: When do messages appear? ===')
# Divide recording into 30-second bins
bin_size = 30  # 30 seconds
t_start = msgs[0]['ts']
t_end = msgs[-1]['ts']
n_bins = int((t_end - t_start) / bin_size) + 1

print(f'\nBins: {n_bins} x 30s')
print(f'{"Pair":<12}', end='')
for i in range(min(n_bins, 20)):
    print(f' {i*30:>4}s', end='')
print()

for key in sorted(pairs.keys(), key=lambda k: len(pairs[k]), reverse=True):
    msgs_list = pairs[key]
    if len(msgs_list) < 2:
        continue
    bins = [0] * n_bins
    for m in msgs_list:
        b = int((m['ts'] - t_start) / bin_size)
        if b < n_bins:
            bins[b] += 1
    print(f'{key:<12}', end='')
    for i in range(min(n_bins, 20)):
        v = bins[i]
        if v == 0:
            print(f'    .', end='')
        else:
            print(f' {v:>4}', end='')
    print(f'  tot={len(msgs_list)}')

# === PART 3: Are there messages that ONLY appear in bursts? ===
print('\n\n=== BURSTY PAIRS (appear in clusters, not steady) ===')
for key in sorted(pairs.keys(), key=lambda k: len(pairs[k]), reverse=True):
    msgs_list = pairs[key]
    count = len(msgs_list)
    if count < 3:
        continue

    # Check: how many 30s bins have this pair?
    bins_with_data = set()
    for m in msgs_list:
        b = int((m['ts'] - t_start) / bin_size)
        bins_with_data.add(b)

    coverage = len(bins_with_data) / n_bins
    # Bursty = appears in few bins relative to count
    if coverage < 0.5:  # present in less than half the recording
        timestamps = [m['ts'] for m in msgs_list]
        print(f'{key:<12} count={count:>3} bins={len(bins_with_data):>2}/{n_bins} coverage={coverage:.0%}  ts: {", ".join(f"{t:.1f}" for t in timestamps[:15])}')

# === PART 4: Show ALL single-occurrence pairs (potential garbled frames) ===
print(f'\n\n=== SINGLE OCCURRENCE PAIRS: {sum(1 for k in pairs if len(pairs[k])==1)} total ===')
singles = [(k, pairs[k][0]) for k in pairs if len(pairs[k]) == 1]
singles.sort(key=lambda x: x[1]['ts'])
for key, m in singles:
    print(f'  ts={m["ts"]:>8.1f}  {key:<12} c={m["c"]} d={m["d"]}')

# === PART 5: Focus on slave addresses that receive from MULTIPLE masters ===
print('\n\n=== SLAVE ADDRESSES WITH MULTIPLE MASTERS ===')
slave_masters = defaultdict(set)
slave_count = defaultdict(int)
for key in pairs:
    m_addr, s_addr = key.split('->')
    slave_masters[s_addr].add(m_addr)
    slave_count[s_addr] += len(pairs[key])

for s in sorted(slave_masters.keys(), key=lambda k: len(slave_masters[k]), reverse=True):
    if len(slave_masters[s]) > 1:
        masters = sorted(slave_masters[s])
        print(f'  Slave {s}: {len(slave_masters[s])} masters, {slave_count[s]} total msgs')
        for m_addr in masters:
            key = f'{m_addr}->{s}'
            print(f'    {key}: {len(pairs[key])} msgs, data samples: {[pairs[key][i]["d"] for i in range(min(3, len(pairs[key])))]}')
