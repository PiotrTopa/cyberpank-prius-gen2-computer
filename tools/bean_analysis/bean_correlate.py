#!/usr/bin/env python3
"""Bit-level correlation of BEAN payload bits against timed action marks.

Marks are intervals in the owner's clock (mm:ss after HH:00 CET), given either
  * in a CSV/TSV file: one per line "mm:ss[-mm:ss],label" (a single time = a 2 s window), or
  * --builtin session30: the door intervals from docs/BEAN_SIGNALS.md §10.
Marks with the same label are pooled.

For every DST:MES / byte / bit the script samples the bit on a 50 ms grid over the time the
bus was awake (the bit keeps the value of the last frame of that message) and scores it
against each label: precision = P(mark | bit set), recall = P(bit set | mark), F1.
It also lists every bit toggle (with the owner clock) so that event-like bits that are not
level-coded can be matched by eye.

Usage:
  bean_correlate.py CAPTURE (--marks FILE | --builtin session30) [--hour 16] [--top 5] [--toggles]
"""
from __future__ import annotations

import argparse
from collections import defaultdict

from beanlib import load_any, node_key, owner_clock, parse_owner_clock, wake_segments

BUILTIN = {
    # from docs/BEAN_SIGNALS.md §10 (owner's action list, session30, car OFF)
    "session30": [
        ("31:21", "31:26", "driver door open"),
        ("32:01", "32:11", "driver door open"),
        ("34:01", "34:11", "passenger door open"),
        ("35:40", "35:52", "rear/back door open"),
        ("36:01", "36:16", "rear/back door open"),
        ("36:31", "36:41", "rear/back door open"),
    ],
}

STEP = 0.05


def load_marks(path):
    out = []
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sep = "\t" if "\t" in line else ","
        when, label = line.split(sep, 1)
        if "-" in when.strip()[1:]:
            a, b = when.split("-", 1)
        else:
            a = b = when
        out.append((a.strip(), b.strip(), label.strip()))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture")
    ap.add_argument("--marks")
    ap.add_argument("--builtin", choices=sorted(BUILTIN))
    ap.add_argument("--hour", type=int, default=16)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--toggles", action="store_true", help="also list every bit toggle")
    a = ap.parse_args(argv)
    marks = load_marks(a.marks) if a.marks else BUILTIN[a.builtin]
    iv = defaultdict(list)
    for s, e, label in marks:
        ts, te = parse_owner_clock(s, a.hour), parse_owner_clock(e, a.hour)
        if te - ts < 2:
            te = ts + 2
        iv[label].append((ts, te))

    cap = load_any(a.capture)[0]
    segs = wake_segments(cap.frames)
    # sample grid over awake time
    grid = []
    for s in segs:
        t = s[0].t
        while t <= s[-1].t:
            grid.append(t)
            t += STEP
    in_label = {lab: [any(a0 <= t <= b0 for a0, b0 in ivs) for t in grid] for lab, ivs in iv.items()}

    # per key: frames sorted; bit value at grid points
    by_key = defaultdict(list)
    for f in cap.frames:
        by_key[node_key(f)].append(f)
    results = []
    toggles = []
    for k, fr in by_key.items():
        nbytes = max(len(f.data) for f in fr)
        for bi in range(nbytes):
            for bit in range(8):
                vals = [((f.data[bi] >> bit) & 1) if len(f.data) > bi else 0 for f in fr]
                if len(set(vals)) < 2:
                    continue
                prev = None
                for f, v in zip(fr, vals):
                    if prev is not None and v != prev:
                        toggles.append((f.t, k, bi, bit, v))
                    prev = v
                # sample
                j = 0
                cur = None
                series = []
                for t in grid:
                    while j < len(fr) and fr[j].t <= t:
                        cur = vals[j]
                        j += 1
                    series.append(cur or 0)
                for lab, inl in in_label.items():
                    both = sum(1 for x, y in zip(series, inl) if x and y)
                    nset = sum(series)
                    nlab = sum(inl)
                    if not nset or not nlab:
                        continue
                    p, r = both / nset, both / nlab
                    f1 = 2 * p * r / (p + r) if p + r else 0
                    results.append((lab, f1, p, r, k, bi, bit))
    for lab in iv:
        rs = sorted((x for x in results if x[0] == lab), key=lambda x: -x[1])[: a.top]
        print(f"== {lab}  ({len(iv[lab])} intervals)")
        for _, f1, p, r, k, bi, bit in rs:
            print(f"   {k:<10} byte{bi} bit{bit} (0x{1 << bit:02X})  F1 {f1:.2f}  precision {p:.2f}  recall {r:.2f}")
    if a.toggles:
        print("\n== all bit toggles (owner clock, key, byte.bit, new value)")
        for t, k, bi, bit, v in sorted(toggles):
            print(f"   {owner_clock(t, a.hour)} {k:<10} b{bi}.{bit} -> {v}")


if __name__ == "__main__":
    main()
