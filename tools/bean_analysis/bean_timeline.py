#!/usr/bin/env python3
"""Change timeline of a BEAN capture, split into bus-awake periods.

For each awake period prints the first value of every DST:MES (only when it differs from the
usual car-OFF wake value, unless --all) and every later change, with the owner's clock
(mm:ss after HH:00 CET), PRI and RSP. FE:AB / FE:C2 are tracked per node byte.

Usage:
  bean_timeline.py CAPTURE [--hour 16] [--all] [--skip-ab] [--show-repeats B5:D9,C1:E2]
"""
from __future__ import annotations

import argparse

from beanlib import load_any, node_key, owner_clock, wake_segments

# value of each message right after a car-OFF wake with all doors closed and locked (session30)
OFF_BASELINE = {
    "FE:C2[40]": "4080", "FE:C2[52]": "5280", "FE:C2[E9]": "E990", "FE:C2[C1]": "C180",
    "FE:C2[B5]": "B590", "FE:C2[62]": "6280", "52:3F": "0493DF80", "40:59": "00C0",
    "FE:83": "00000000", "FE:7F": "001000000000", "FE:AB[52]": "528020200000C0048E0380",
    "FE:D8": "0000", "FE:62": "2000", "62:6E": "0000", "FE:DB": "00", "FE:AB[E9]": "E990",
    "FE:54": "57424C41454E", "FE:AB[C1]": "C180", "FE:E1": "000000", "FE:AB[B5]": "B590",
    "FE:53": "490000000000", "FE:B6": "0000", "FE:AB[62]": "6280", "FE:AB[40]": "4080",
}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture")
    ap.add_argument("--hour", type=int, default=16, help="owner clock base hour (CET)")
    ap.add_argument("--all", action="store_true", help="print every first value, not only non-baseline")
    ap.add_argument("--skip-ab", action="store_true", help="hide FE:AB status changes")
    ap.add_argument("--show-repeats", default="", help="comma list of keys to print on every frame")
    ap.add_argument("--gap", type=float, default=2.0, help="silence (s) that separates awake periods")
    a = ap.parse_args(argv)
    rep = set(filter(None, a.show_repeats.split(",")))
    for cap in load_any(a.capture):
        fixture = not cap.name.endswith((".ndjson", ".ndjson.gz"))
        clock = (lambda t: f"{t:8.3f}") if fixture else (lambda t: owner_clock(t, a.hour))
        print(f"##### {cap.name}")
        for seg in wake_segments(cap.frames, a.gap):
            print(f"==== awake {clock(seg[0].t)} - {clock(seg[-1].t)}  ({seg[-1].t - seg[0].t:.1f} s, {len(seg)} frames)")
            last: dict[str, str] = {}
            for f in seg:
                k = node_key(f)
                if a.skip_ab and k.startswith("FE:AB"):
                    continue
                v = f.hex
                if k in rep:
                    print(f"    {clock(f.t)} p{f.pri:<2} {k:<10} {v} rsp{f.rsp}")
                elif k not in last:
                    if a.all or OFF_BASELINE.get(k) != v:
                        print(f"    {clock(f.t)} p{f.pri:<2} {k:<10} {v} rsp{f.rsp}  first (baseline {OFF_BASELINE.get(k)})")
                elif last[k] != v:
                    print(f"    {clock(f.t)} p{f.pri:<2} {k:<10} {v} rsp{f.rsp}  (was {last[k]})")
                last[k] = v


if __name__ == "__main__":
    main()
