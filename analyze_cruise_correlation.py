#!/usr/bin/env python3
"""
Cruise Control Correlation Analysis.

Analyzes NDJSON log files to find CAN bus bits that correlate with
cruise-control-engaged driving conditions:
  - No throttle pedal (0x244 byte[6] == 0)
  - Speed maintained / positive MG2 torque (from 0x7EA PID 21C3)
  - Braking events (0x030 byte[4] > 0) which cancel cruise

Strategy:
  1. Build a timeline of throttle, brake, speed, torque, drive_condition
  2. Classify each second as "likely cruise" or "not cruise"
     - Cruise = throttle==0, speed > 60 km/h, speed stable, MG2 torque > 0
     - Not cruise = throttle > 0 OR brake > 0 OR speed < 40 OR decelerating
  3. For every other CAN ID, track all byte/bit values and compute
     correlation (% of time each bit==1 during cruise vs non-cruise)
  4. Rank bits by differential correlation
"""

import json
import sys
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
import statistics


# ──────────────────────────────── Data structures ────────────────────────────

@dataclass
class TimeSlice:
    """State at a given second."""
    ts: float = 0.0
    speed_kmh: float = 0.0
    throttle: int = 0            # 0x244 byte[6], 0-200
    brake: int = 0               # 0x030 byte[4], 0-127
    mg2_torque: Optional[float] = None   # Nm from 21C3
    mg1_torque: Optional[float] = None
    regen_torque: Optional[float] = None
    master_cyl_torque: Optional[float] = None
    engine_load: Optional[int] = None    # PID 0104 (from 0x7E8)
    drive_condition: Optional[int] = None
    drive_state: Optional[int] = None
    ice_rpm_actual: Optional[int] = None
    # Classification
    likely_cruise: bool = False


def parse_log(filepath: str) -> Tuple[List[dict], Dict[str, List[Tuple[float, list]]]]:
    """
    Parse NDJSON log into:
      - decoded_events: list of {ts, type, values}
      - raw_can: dict of CAN_ID -> [(ts, data_bytes), ...]
    """
    decoded_events = []
    raw_can: Dict[str, List[Tuple[float, list]]] = defaultdict(list)

    with open(filepath, 'r') as f:
        for line in f:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            if msg.get('dir') != 'IN' or msg.get('id') != 1:
                continue

            d = msg.get('d', {})
            can_id = d.get('i')
            data = d.get('d')
            ts = msg.get('ts', 0)

            if can_id is None or data is None:
                continue

            # Store raw data for all unsolicited CAN IDs
            if not d.get('a'):  # unsolicited
                raw_can[can_id].append((ts, data))

            can_int = int(can_id, 16)

            # Throttle (0x244, byte[6])
            if can_int == 0x244 and len(data) >= 7:
                decoded_events.append({
                    'ts': ts, 'type': 'throttle', 'value': data[6]
                })

            # Brake (0x030, byte[4])
            elif can_int == 0x030 and len(data) >= 5:
                decoded_events.append({
                    'ts': ts, 'type': 'brake', 'value': data[4]
                })

            # Speed (0x0B3, rear wheel speed)
            elif can_int == 0x0B3 and len(data) >= 4:
                speed = (data[0] * 256 + data[1]) * 0.01
                decoded_events.append({
                    'ts': ts, 'type': 'speed', 'value': speed
                })

            # 0x5C8 cruise raw
            elif can_int == 0x5C8 and len(data) >= 3:
                decoded_events.append({
                    'ts': ts, 'type': '5c8_raw',
                    'data': data,
                    'cancel_bit': bool(data[2] & 0x10)
                })

            # Solicited 0x7EA PID 21C3 (multi-frame reassembled)
            elif can_int == 0x7EA and d.get('a') == 'sub':
                payload = data
                if len(payload) >= 38 and payload[0] == 0x61 and payload[1] == 0xC3:
                    # payload[0:2] = service+PID header (0x61 0xC3)
                    # Actual data starts at index 2, matching CAN decoder payload[0]=A
                    off = 2  # offset to align with CAN decoder indices
                    mg2_torque = ((payload[off+2] * 256) + payload[off+3]) / 8 - 500
                    regen_actual = 4 * payload[off+4]
                    regen_request = 4 * payload[off+5]
                    mg1_torque = ((payload[off+8] * 256) + payload[off+9]) / 8 - 500
                    ice_rpm_target = (payload[off+12] * 256) + payload[off+13]
                    ice_rpm_actual = (payload[off+14] * 256) + payload[off+15]
                    master_cyl = (4 * payload[off+17]) - 512
                    drive_condition = payload[off+21] if len(payload) > off+21 else None
                    drive_state = payload[off+35] if len(payload) > off+35 else None

                    decoded_events.append({
                        'ts': ts, 'type': 'hybrid_21c3',
                        'mg2_torque': mg2_torque,
                        'mg1_torque': mg1_torque,
                        'regen_actual': regen_actual,
                        'regen_request': regen_request,
                        'master_cyl': master_cyl,
                        'ice_rpm_target': ice_rpm_target,
                        'ice_rpm_actual': ice_rpm_actual,
                        'drive_condition': drive_condition,
                        'drive_state': drive_state,
                    })

            # Solicited 0x7E8 PID 0104 (engine load)
            elif can_int == 0x7E8 and d.get('a') == 'sub':
                if len(data) >= 3 and data[0] == 0x41 and data[1] == 0x04:
                    decoded_events.append({
                        'ts': ts, 'type': 'engine_load', 'value': data[2]
                    })

    return decoded_events, raw_can


def build_timeline(decoded_events: List[dict],
                   bin_seconds: float = 1.0) -> List[TimeSlice]:
    """
    Build second-by-second timeline from decoded events.
    Uses forward-fill for sparse values (torques from solicited ~3s interval).
    """
    if not decoded_events:
        return []

    min_ts = min(e['ts'] for e in decoded_events)
    max_ts = max(e['ts'] for e in decoded_events)

    n_bins = int((max_ts - min_ts) / bin_seconds) + 1
    slices = [TimeSlice(ts=min_ts + i * bin_seconds) for i in range(n_bins)]

    cur_throttle = 0
    cur_brake = 0
    cur_speed = 0.0
    cur_mg2_torque = None
    cur_mg1_torque = None
    cur_regen = None
    cur_master_cyl = None
    cur_engine_load = None
    cur_drive_condition = None
    cur_drive_state = None
    cur_ice_rpm = None

    events_sorted = sorted(decoded_events, key=lambda e: e['ts'])

    event_idx = 0
    for i, sl in enumerate(slices):
        t_start = sl.ts
        t_end = t_start + bin_seconds

        while event_idx < len(events_sorted) and events_sorted[event_idx]['ts'] < t_end:
            e = events_sorted[event_idx]
            if e['type'] == 'throttle':
                cur_throttle = e['value']
            elif e['type'] == 'brake':
                cur_brake = e['value']
            elif e['type'] == 'speed':
                cur_speed = e['value']
            elif e['type'] == 'hybrid_21c3':
                cur_mg2_torque = e['mg2_torque']
                cur_mg1_torque = e['mg1_torque']
                cur_regen = e['regen_actual']
                cur_master_cyl = e['master_cyl']
                cur_drive_condition = e['drive_condition']
                cur_drive_state = e['drive_state']
                cur_ice_rpm = e['ice_rpm_actual']
            elif e['type'] == 'engine_load':
                cur_engine_load = e['value']
            event_idx += 1

        sl.speed_kmh = cur_speed
        sl.throttle = cur_throttle
        sl.brake = cur_brake
        sl.mg2_torque = cur_mg2_torque
        sl.mg1_torque = cur_mg1_torque
        sl.regen_torque = cur_regen
        sl.master_cyl_torque = cur_master_cyl
        sl.engine_load = cur_engine_load
        sl.drive_condition = cur_drive_condition
        sl.drive_state = cur_drive_state
        sl.ice_rpm_actual = cur_ice_rpm

    return slices


def classify_cruise(slices: List[TimeSlice],
                    min_speed: float = 60.0,
                    speed_window: int = 10,
                    max_speed_std: float = 5.0) -> List[TimeSlice]:
    """
    Classify each time slice as likely-cruise or not.

    Cruise indicators:
      - Throttle == 0 (foot off gas)
      - Speed > min_speed
      - Speed relatively stable (low std over window)
      - MG2 provides positive torque (maintaining speed electrically) OR ICE running
      - Brake == 0
    """
    for i, sl in enumerate(slices):
        if sl.speed_kmh < min_speed:
            sl.likely_cruise = False
            continue
        if sl.brake > 5:
            sl.likely_cruise = False
            continue
        if sl.throttle > 5:
            sl.likely_cruise = False
            continue

        # Speed stability check
        window_start = max(0, i - speed_window)
        window_end = min(len(slices), i + speed_window + 1)
        speeds = [s.speed_kmh for s in slices[window_start:window_end] if s.speed_kmh > 0]
        if len(speeds) < 3:
            sl.likely_cruise = False
            continue

        speed_std = statistics.stdev(speeds) if len(speeds) > 1 else 0
        if speed_std > max_speed_std:
            sl.likely_cruise = False
            continue

        # Torque check: MG2 positive or engine providing power
        has_propulsion = False
        if sl.mg2_torque is not None and sl.mg2_torque > 5:
            has_propulsion = True
        if sl.engine_load is not None and sl.engine_load > 10:
            has_propulsion = True
        if sl.ice_rpm_actual is not None and sl.ice_rpm_actual > 500:
            has_propulsion = True

        # Even without torque data (sparse), if throttle=0 + speed stable = likely cruise
        # Torque data comes every ~3s, so we allow gaps
        if not has_propulsion and sl.mg2_torque is not None and sl.mg2_torque <= 0:
            sl.likely_cruise = False
            continue

        sl.likely_cruise = True

    return slices


def correlate_bits(slices: List[TimeSlice],
                   raw_can: Dict[str, List[Tuple[float, list]]],
                   bin_seconds: float = 1.0) -> List[dict]:
    """
    For every CAN ID and every byte:bit, compute:
      - % of time bit==1 during cruise windows
      - % of time bit==1 during non-cruise windows
      - Differential (cruise% - non-cruise%)
    """
    if not slices:
        return []

    min_ts = slices[0].ts
    results = []
    skip_ids = {'0x7E8', '0x7EA', '0x7EB'}

    for can_id, messages in raw_can.items():
        if can_id in skip_ids:
            continue
        if not messages:
            continue

        max_bytes = max(len(m[1]) for m in messages[:100])

        for byte_idx in range(max_bytes):
            for bit_idx in range(8):
                cruise_ones = 0
                cruise_total = 0
                non_cruise_ones = 0
                non_cruise_total = 0

                for ts, data in messages:
                    if byte_idx >= len(data):
                        continue
                    slice_idx = int((ts - min_ts) / bin_seconds)
                    if slice_idx < 0 or slice_idx >= len(slices):
                        continue

                    bit_val = (data[byte_idx] >> bit_idx) & 1

                    if slices[slice_idx].likely_cruise:
                        cruise_ones += bit_val
                        cruise_total += 1
                    else:
                        non_cruise_ones += bit_val
                        non_cruise_total += 1

                if cruise_total < 10 or non_cruise_total < 10:
                    continue

                cruise_pct = cruise_ones / cruise_total * 100
                non_cruise_pct = non_cruise_ones / non_cruise_total * 100
                diff = cruise_pct - non_cruise_pct

                results.append({
                    'can_id': can_id,
                    'byte': byte_idx,
                    'bit': bit_idx,
                    'cruise_pct': cruise_pct,
                    'non_cruise_pct': non_cruise_pct,
                    'diff': diff,
                    'cruise_n': cruise_total,
                    'non_cruise_n': non_cruise_total,
                })

    results.sort(key=lambda r: abs(r['diff']), reverse=True)
    return results


def correlate_byte_values(slices: List[TimeSlice],
                          raw_can: Dict[str, List[Tuple[float, list]]],
                          bin_seconds: float = 1.0) -> List[dict]:
    """
    For each CAN ID byte, compute average value during cruise vs non-cruise.
    Useful for finding non-binary indicators.
    """
    if not slices:
        return []

    min_ts = slices[0].ts
    results = []
    skip_ids = {'0x7E8', '0x7EA', '0x7EB'}

    for can_id, messages in raw_can.items():
        if can_id in skip_ids:
            continue
        if not messages:
            continue

        max_bytes = max(len(m[1]) for m in messages[:100])

        for byte_idx in range(max_bytes):
            cruise_vals = []
            non_cruise_vals = []

            for ts, data in messages:
                if byte_idx >= len(data):
                    continue
                slice_idx = int((ts - min_ts) / bin_seconds)
                if slice_idx < 0 or slice_idx >= len(slices):
                    continue

                val = data[byte_idx]
                if slices[slice_idx].likely_cruise:
                    cruise_vals.append(val)
                else:
                    non_cruise_vals.append(val)

            if len(cruise_vals) < 10 or len(non_cruise_vals) < 10:
                continue

            c_mean = statistics.mean(cruise_vals)
            nc_mean = statistics.mean(non_cruise_vals)
            c_unique = len(set(cruise_vals))
            nc_unique = len(set(non_cruise_vals))

            results.append({
                'can_id': can_id,
                'byte': byte_idx,
                'cruise_mean': c_mean,
                'non_cruise_mean': nc_mean,
                'diff_mean': c_mean - nc_mean,
                'cruise_unique': c_unique,
                'non_cruise_unique': nc_unique,
                'cruise_n': len(cruise_vals),
                'non_cruise_n': len(non_cruise_vals),
            })

    results.sort(key=lambda r: abs(r['diff_mean']), reverse=True)
    return results


def print_drive_condition_analysis(slices: List[TimeSlice]):
    """Analyze drive_condition and drive_state values during cruise vs non-cruise."""
    print("\n" + "=" * 80)
    print("DRIVE CONDITION / STATE ANALYSIS (from PID 21C3)")
    print("=" * 80)

    cruise_dc = defaultdict(int)
    non_cruise_dc = defaultdict(int)
    cruise_ds = defaultdict(int)
    non_cruise_ds = defaultdict(int)

    for sl in slices:
        if sl.drive_condition is not None:
            if sl.likely_cruise:
                cruise_dc[sl.drive_condition] += 1
            else:
                non_cruise_dc[sl.drive_condition] += 1
        if sl.drive_state is not None:
            if sl.likely_cruise:
                cruise_ds[sl.drive_state] += 1
            else:
                non_cruise_ds[sl.drive_state] += 1

    print("\n  drive_condition distribution:")
    all_dc = sorted(set(list(cruise_dc.keys()) + list(non_cruise_dc.keys())))
    for dc in all_dc:
        c = cruise_dc.get(dc, 0)
        nc = non_cruise_dc.get(dc, 0)
        total = c + nc
        c_pct = c / total * 100 if total > 0 else 0
        print(f"    condition={dc}: cruise={c} ({c_pct:.0f}%), non-cruise={nc} ({100-c_pct:.0f}%)")

    print("\n  drive_state distribution:")
    all_ds = sorted(set(list(cruise_ds.keys()) + list(non_cruise_ds.keys())))
    for ds in all_ds:
        c = cruise_ds.get(ds, 0)
        nc = non_cruise_ds.get(ds, 0)
        total = c + nc
        c_pct = c / total * 100 if total > 0 else 0
        print(f"    state={ds}: cruise={c} ({c_pct:.0f}%), non-cruise={nc} ({100-c_pct:.0f}%)")


def print_torque_analysis(slices: List[TimeSlice]):
    """Show torque behavior during cruise vs non-cruise."""
    print("\n" + "=" * 80)
    print("TORQUE / PEDAL ANALYSIS DURING CLASSIFIED WINDOWS")
    print("=" * 80)

    cruise_slices = [s for s in slices if s.likely_cruise]
    non_cruise_driving = [s for s in slices if not s.likely_cruise and s.speed_kmh > 30]

    for label, group in [("CRUISE (likely)", cruise_slices),
                         ("NON-CRUISE (speed>30)", non_cruise_driving)]:
        print(f"\n  [{label}] — {len(group)} seconds")
        vals = {
            'throttle': [s.throttle for s in group],
            'brake': [s.brake for s in group],
            'speed': [s.speed_kmh for s in group if s.speed_kmh > 0],
            'mg2_torque': [s.mg2_torque for s in group if s.mg2_torque is not None],
            'mg1_torque': [s.mg1_torque for s in group if s.mg1_torque is not None],
            'regen_torque': [s.regen_torque for s in group if s.regen_torque is not None],
            'master_cyl': [s.master_cyl_torque for s in group if s.master_cyl_torque is not None],
            'engine_load': [s.engine_load for s in group if s.engine_load is not None],
            'ice_rpm_actual': [s.ice_rpm_actual for s in group if s.ice_rpm_actual is not None],
        }
        for name, v in vals.items():
            if not v:
                print(f"    {name:20s}: no data")
                continue
            mn = min(v)
            mx = max(v)
            avg = statistics.mean(v)
            print(f"    {name:20s}: min={mn:8.1f}  avg={avg:8.1f}  max={mx:8.1f}  (n={len(v)})")


def print_5c8_timeline(decoded_events: List[dict], slices: List[TimeSlice],
                       bin_seconds: float = 1.0):
    """Show 0x5C8 messages annotated with cruise classification."""
    print("\n" + "=" * 80)
    print("0x5C8 MESSAGES + CRUISE CLASSIFICATION")
    print("=" * 80)

    if not slices:
        return
    min_ts = slices[0].ts

    for e in decoded_events:
        if e['type'] != '5c8_raw':
            continue
        ts = e['ts']
        data = e['data']
        cancel = e['cancel_bit']

        slice_idx = int((ts - min_ts) / bin_seconds)
        if 0 <= slice_idx < len(slices):
            sl = slices[slice_idx]
            cruise_label = "CRUISE" if sl.likely_cruise else "not-cruise"
            hex_str = ' '.join(f'{b:02X}' for b in data)
            bits_2 = f'{data[2]:08b}' if len(data) > 2 else '--------'
            print(f"  ts={ts:8.1f}s  [{hex_str}]  byte2={bits_2}  cancel={cancel}  "
                  f"spd={sl.speed_kmh:.0f}  thr={sl.throttle}  brk={sl.brake}  "
                  f"mg2T={sl.mg2_torque}  [{cruise_label}]")


def print_brake_cruise_events(slices: List[TimeSlice]):
    """Show transitions where braking happens during or near cruise windows."""
    print("\n" + "=" * 80)
    print("BRAKING / CRUISE TRANSITION EVENTS")
    print("=" * 80)

    in_cruise = False
    for i, sl in enumerate(slices):
        prev_cruise = in_cruise
        in_cruise = sl.likely_cruise

        if prev_cruise and not in_cruise and sl.brake > 5:
            ctx_start = max(0, i - 3)
            ctx_end = min(len(slices), i + 5)
            print(f"\n  BRAKE cancels cruise at ts={sl.ts:.0f}s:")
            for j in range(ctx_start, ctx_end):
                s = slices[j]
                marker = " ***" if j == i else ""
                print(f"    ts={s.ts:7.0f}  spd={s.speed_kmh:5.0f}  thr={s.throttle:3d}  "
                      f"brk={s.brake:3d}  mg2T={s.mg2_torque}  dc={s.drive_condition}  "
                      f"{'CRS' if s.likely_cruise else '   '}{marker}")

        if in_cruise and not prev_cruise:
            ctx_start = max(0, i - 3)
            ctx_end = min(len(slices), i + 3)
            print(f"\n  CRUISE entry at ts={sl.ts:.0f}s:")
            for j in range(ctx_start, ctx_end):
                s = slices[j]
                marker = " ***" if j == i else ""
                print(f"    ts={s.ts:7.0f}  spd={s.speed_kmh:5.0f}  thr={s.throttle:3d}  "
                      f"brk={s.brake:3d}  mg2T={s.mg2_torque}  dc={s.drive_condition}  "
                      f"{'CRS' if s.likely_cruise else '   '}{marker}")


def analyze_file(filepath: str):
    """Full analysis of a single log file."""
    basename = os.path.basename(filepath)
    print(f"\n{'#' * 80}")
    print(f"# ANALYZING: {basename}")
    print(f"{'#' * 80}")

    print(f"\nParsing {filepath}...")
    decoded_events, raw_can = parse_log(filepath)
    print(f"  Decoded events: {len(decoded_events)}")
    print(f"  CAN IDs with raw data: {len(raw_can)}")
    for cid in sorted(raw_can.keys(), key=lambda x: int(x, 16)):
        print(f"    {cid}: {len(raw_can[cid])} messages")

    print("\nBuilding timeline...")
    slices = build_timeline(decoded_events)
    if not slices:
        print("  No data!")
        return None, [], []
    print(f"  Timeline: {len(slices)} seconds ({slices[0].ts:.0f}s - {slices[-1].ts:.0f}s)")

    print("\nClassifying cruise windows...")
    slices = classify_cruise(slices)
    cruise_seconds = sum(1 for s in slices if s.likely_cruise)
    non_cruise_seconds = len(slices) - cruise_seconds
    print(f"  Cruise: {cruise_seconds}s ({cruise_seconds/len(slices)*100:.1f}%)")
    print(f"  Non-cruise: {non_cruise_seconds}s ({non_cruise_seconds/len(slices)*100:.1f}%)")

    if cruise_seconds < 5:
        print("\n  WARNING: Very few cruise seconds detected. Lowering speed threshold to 40...")
        slices = classify_cruise(slices, min_speed=40.0, max_speed_std=8.0)
        cruise_seconds = sum(1 for s in slices if s.likely_cruise)
        non_cruise_seconds = len(slices) - cruise_seconds
        print(f"  Cruise: {cruise_seconds}s ({cruise_seconds/len(slices)*100:.1f}%)")

    # Print analyses
    print_torque_analysis(slices)
    print_drive_condition_analysis(slices)
    print_5c8_timeline(decoded_events, slices)
    print_brake_cruise_events(slices)

    # Bit correlation
    print("\n" + "=" * 80)
    print("BIT CORRELATION ANALYSIS (top 40 by |differential|)")
    print("=" * 80)
    header = (f"  {'CAN_ID':>8s}  {'byte':>4s}  {'bit':>3s}  "
              f"{'cruise%':>8s}  {'other%':>8s}  {'diff':>8s}  "
              f"{'n_crs':>6s}  {'n_oth':>6s}")
    print(header)
    print(f"  {'─'*8}  {'─'*4}  {'─'*3}  {'─'*8}  {'─'*8}  {'─'*8}  {'─'*6}  {'─'*6}")

    bit_results = correlate_bits(slices, raw_can)
    for r in bit_results[:40]:
        indicator = ""
        if r['diff'] > 80:
            indicator = " ★★★ STRONG cruise=ON"
        elif r['diff'] > 50:
            indicator = " ★★ moderate cruise=ON"
        elif r['diff'] < -80:
            indicator = " ★★★ STRONG cruise=OFF"
        elif r['diff'] < -50:
            indicator = " ★★ moderate cruise=OFF"

        print(f"  {r['can_id']:>8s}  [{r['byte']:>2d}]  b{r['bit']}  "
              f"{r['cruise_pct']:7.1f}%  {r['non_cruise_pct']:7.1f}%  "
              f"{r['diff']:+7.1f}%  {r['cruise_n']:>6d}  {r['non_cruise_n']:>6d}"
              f"{indicator}")

    # Byte-value correlation
    print("\n" + "=" * 80)
    print("BYTE VALUE CORRELATION (top 20 by |mean difference|)")
    print("=" * 80)
    print(f"  {'CAN_ID':>8s}  {'byte':>4s}  {'crs_mean':>8s}  {'oth_mean':>8s}  "
          f"{'diff':>8s}  {'crs_uniq':>8s}  {'oth_uniq':>8s}")
    print(f"  {'─'*8}  {'─'*4}  {'─'*8}  {'─'*8}  {'─'*8}  {'─'*8}  {'─'*8}")

    byte_results = correlate_byte_values(slices, raw_can)
    for r in byte_results[:20]:
        print(f"  {r['can_id']:>8s}  [{r['byte']:>2d}]  "
              f"{r['cruise_mean']:8.1f}  {r['non_cruise_mean']:8.1f}  "
              f"{r['diff_mean']:+8.1f}  {r['cruise_unique']:>8d}  {r['non_cruise_unique']:>8d}")

    return slices, bit_results, byte_results


def cross_file_consistency(results_list: List[Tuple[str, List[dict]]]):
    """Check which bits are consistently correlated across multiple log files."""
    if len(results_list) < 2:
        return

    print("\n" + "#" * 80)
    print("# CROSS-FILE CONSISTENCY CHECK")
    print("#" * 80)

    file_diffs = []
    for fname, bit_results in results_list:
        lookup = {}
        for r in bit_results:
            key = (r['can_id'], r['byte'], r['bit'])
            lookup[key] = r
        file_diffs.append((fname, lookup))

    all_keys = set()
    for _, lookup in file_diffs:
        top = sorted(lookup.values(), key=lambda r: abs(r['diff']), reverse=True)[:50]
        for r in top:
            all_keys.add((r['can_id'], r['byte'], r['bit']))

    consistent = []
    for key in all_keys:
        diffs = []
        for fname, lookup in file_diffs:
            if key in lookup:
                diffs.append(lookup[key]['diff'])

        if len(diffs) < 2:
            continue

        if all(d > 20 for d in diffs) or all(d < -20 for d in diffs):
            avg_diff = statistics.mean(diffs)
            consistent.append({
                'can_id': key[0], 'byte': key[1], 'bit': key[2],
                'avg_diff': avg_diff,
                'diffs': diffs,
            })

    consistent.sort(key=lambda r: abs(r['avg_diff']), reverse=True)

    print(f"\n  Bits consistently correlated across {len(results_list)} files:")
    print(f"  {'CAN_ID':>8s}  {'byte':>4s}  {'bit':>3s}  "
          f"{'avg_diff':>8s}  diffs_per_file")
    print(f"  {'─'*8}  {'─'*4}  {'─'*3}  {'─'*8}  {'─'*30}")

    for r in consistent[:30]:
        diffs_str = ', '.join(f'{d:+.1f}%' for d in r['diffs'])
        indicator = ""
        if abs(r['avg_diff']) > 70:
            indicator = " ← VERY LIKELY CRUISE INDICATOR"
        elif abs(r['avg_diff']) > 40:
            indicator = " ← possible cruise indicator"
        print(f"  {r['can_id']:>8s}  [{r['byte']:>2d}]  b{r['bit']}  "
              f"{r['avg_diff']:+7.1f}%  [{diffs_str}]{indicator}")


# ───────────────────────────────────── Main ──────────────────────────────────

def main():
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')

    log_files = [
        os.path.join(log_dir, 'comm_20260219_144720.ndjson'),
        os.path.join(log_dir, 'comm_20260219_202855.ndjson'),
    ]

    if len(sys.argv) > 1:
        log_files = sys.argv[1:]

    all_bit_results = []

    for filepath in log_files:
        if not os.path.exists(filepath):
            print(f"WARNING: File not found: {filepath}")
            continue
        result = analyze_file(filepath)
        if result[0] is not None:
            slices, bit_results, byte_results = result
            all_bit_results.append((os.path.basename(filepath), bit_results))

    if len(all_bit_results) >= 2:
        cross_file_consistency(all_bit_results)

    print("\n\nDone.")


if __name__ == '__main__':
    main()
