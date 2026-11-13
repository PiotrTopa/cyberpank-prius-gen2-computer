#!/usr/bin/env python3
"""Per-message statistics for BEAN captures.

For every DST:MES (FE:AB and FE:C2 split per node byte) prints: frame count, PRI values,
data length, RSP values, the periodic interval (median, p10-p90, computed only between
consecutive frames inside one bus-awake period and only on frames with the base priority,
i.e. PRI with bit 3 clear), the event-copy ratio (PRI bit 3 set), distinct payloads, and a
per-byte classification:

  const      one value only
  flags      few values, changes are bit flips (lists the bits that toggle)
  counter    mostly +1 steps
  rolling    a new unrelated value at (almost) every change, high entropy -> crypto/random
  analog     many values, small steps
  enum       few values that are not single-bit changes

Usage:
  bean_stats.py CAPTURE [CAPTURE ...] [--json]
CAPTURE is an NDJSON(.gz) capture or the bean_tools fixture (*.txt.gz, one table per .dat file).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter

from beanlib import Frame, group, load_any, node_key, wake_segments


def classify_byte(vals: list[int]) -> tuple[str, str]:
    distinct = sorted(set(vals))
    if len(distinct) == 1:
        return "const", f"{distinct[0]:02X}"
    changes = [(a, b) for a, b in zip(vals, vals[1:]) if a != b]
    toggled = 0
    for a, b in changes:
        toggled |= a ^ b
    single_bit = sum(1 for a, b in changes if bin(a ^ b).count("1") == 1)
    plus1 = sum(1 for a, b in changes if (b - a) & 0xFF == 1)
    small = sum(1 for a, b in changes if min((b - a) & 0xFF, (a - b) & 0xFF) <= 3)
    vals_s = " ".join(f"{v:02X}" for v in distinct[:12]) + (" ..." if len(distinct) > 12 else "")
    if changes and plus1 / len(changes) > 0.8:
        return "counter", vals_s
    if len(distinct) >= 6 and changes and small / len(changes) > 0.7:
        return "analog", f"{distinct[0]:02X}..{distinct[-1]:02X} ({len(distinct)} values)"
    if len(distinct) <= 6 and changes and (single_bit / len(changes) > 0.5 or bin(toggled).count("1") <= 3):
        return "flags", f"bits {toggled:08b} toggle; values {vals_s}"
    if len(distinct) >= 4 and len(distinct) >= 0.6 * (len(changes) + 1):
        return "rolling", f"{len(distinct)} values in {len(changes)} changes: {vals_s}"
    return "enum", vals_s


def per_message(frames: list[Frame]) -> list[dict]:
    segs = wake_segments(frames)
    seg_of = {}
    for i, s in enumerate(segs):
        for f in s:
            seg_of[id(f)] = i
    groups: dict[str, list[Frame]] = {}
    for f in frames:
        groups.setdefault(node_key(f), []).append(f)
    out = []
    for k, fr in sorted(groups.items()):
        pri = Counter(f.pri for f in fr)
        base = [f for f in fr if not f.pri & 8]
        iv = []
        for a, b in zip(base, base[1:]):
            if seg_of[id(a)] == seg_of[id(b)]:
                iv.append(b.t - a.t)
        iv.sort()
        lens = Counter(len(f.data) for f in fr)
        payloads = Counter(f.hex for f in fr)
        nbytes = max(lens)
        bytes_cls = []
        for i in range(nbytes):
            vals = [f.data[i] for f in fr if len(f.data) > i]
            bytes_cls.append(classify_byte(vals))
        # event copies: PRI bit3 set; check whether they coincide with a value change
        ev = [f for f in fr if f.pri & 8]
        prev = None
        ev_change = 0
        for f in fr:
            if f.pri & 8 and prev is not None and f.hex != prev:
                ev_change += 1
            prev = f.hex
        out.append({
            "key": k,
            "n": len(fr),
            "segments": len({seg_of[id(f)] for f in fr}),
            "pri": dict(pri),
            "len": dict(lens),
            "rsp": dict(Counter(f.rsp for f in fr)),
            "period_med_s": round(statistics.median(iv), 3) if iv else None,
            "period_p10_s": round(iv[len(iv) // 10], 3) if iv else None,
            "period_p90_s": round(iv[(9 * len(iv)) // 10], 3) if iv else None,
            "n_event_copies": len(ev),
            "event_copies_with_change": ev_change,
            "distinct_payloads": len(payloads),
            "top_payloads": payloads.most_common(6),
            "bytes": bytes_cls,
        })
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("captures", nargs="+")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    allres = {}
    for path in a.captures:
        for cap in load_any(path):
            res = per_message(cap.frames)
            allres[cap.name] = res
            if a.json:
                continue
            segs = wake_segments(cap.frames)
            print(f"### {cap.name}: {len(cap.frames)} frames, {len(segs)} awake periods")
            for r in res:
                per = (f"{r['period_med_s']:.3f}s [{r['period_p10_s']:.3f}-{r['period_p90_s']:.3f}]"
                       if r["period_med_s"] is not None else "-")
                print(f"{r['key']:<12} n={r['n']:<5} seg={r['segments']:<3} pri={r['pri']} len={r['len']} "
                      f"rsp={r['rsp']} period={per} ev={r['n_event_copies']}({r['event_copies_with_change']} on change)")
                print(f"{'':12} payloads({r['distinct_payloads']}): "
                      + ", ".join(f"{p}x{c}" for p, c in r["top_payloads"]))
                for i, (cls, desc) in enumerate(r["bytes"]):
                    print(f"{'':12}   b{i}: {cls:<8} {desc}")
            print()
    if a.json:
        json.dump(allres, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
