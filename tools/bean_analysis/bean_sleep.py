#!/usr/bin/env python3
"""Wake/sleep analysis of BEAN captures.

Per bus-awake period prints:
  * the waker (first FE:C2, normally not ACKed) and the order of the wake burst
  * duration, the time of the last payload change (other than FE:AB), and for every node the
    time its FE:AB status byte last dropped to bit7 = 0 ("may sleep")
  * the time from the gateway's (node 0x52) last FE:AB → 0x00 to the last frame
Then summary statistics over all periods.

Usage:
  bean_sleep.py CAPTURE [CAPTURE ...] [--hour 16]
"""
from __future__ import annotations

import argparse
import statistics

from beanlib import load_any, node_key, owner_clock, wake_segments


def analyse(seg):
    t0 = seg[0].t
    burst = [f for f in seg if f.key == "FE:C2" and f.t - t0 < 0.1]
    waker = burst[0] if burst else None
    last_change = t0
    last = {}
    ab_drop = {}
    ab_state = {}
    for f in seg:
        k = node_key(f)
        if k.startswith("FE:AB"):
            node = f.data[0]
            awake = bool(f.data[1] & 0x80)
            if ab_state.get(node, True) and not awake:
                ab_drop[node] = f.t - t0
            if awake:
                ab_drop.pop(node, None)
            ab_state[node] = awake
            continue
        if k.startswith("FE:C2"):
            continue
        if k in last and last[k] != f.hex:
            last_change = f.t
        last[k] = f.hex
    end = seg[-1].t
    gw = ab_drop.get(0x52)
    return {
        "t0": t0,
        "dur": end - t0,
        "waker": f"{waker.data.hex().upper()} rsp{waker.rsp}" if waker else "-",
        "burst": " ".join(f.data.hex().upper() for f in burst),
        "last_change": last_change - t0,
        "ab_drop": {f"{n:02X}": round(v, 2) for n, v in sorted(ab_drop.items())},
        "gw_to_end": (end - t0 - gw) if gw is not None else None,
        "last_frames": [f"{node_key(f)} {f.hex}" for f in seg[-4:]],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("captures", nargs="+")
    ap.add_argument("--hour", type=int, default=16)
    a = ap.parse_args(argv)
    rows = []
    for path in a.captures:
        for cap in load_any(path):
            fixture = not cap.name.endswith((".ndjson", ".ndjson.gz"))
            print(f"##### {cap.name}")
            for seg in wake_segments(cap.frames):
                r = analyse(seg)
                rows.append(r)
                start = f"{r['t0']:.2f}" if fixture else owner_clock(r["t0"], a.hour)
                print(f"{start}  dur {r['dur']:5.1f}s  waker {r['waker']:<10} last change +{r['last_change']:5.1f}s  "
                      f"gw00->end {r['gw_to_end'] if r['gw_to_end'] is None else round(r['gw_to_end'], 2)}  "
                      f"AB->00 at {r['ab_drop']}")
                print(f"{'':8}burst: {r['burst']}   last frames: {' | '.join(r['last_frames'])}")
    g = [r["gw_to_end"] for r in rows if r["gw_to_end"] is not None]
    d = [r["dur"] - r["last_change"] for r in rows]
    if g:
        print(f"\ngateway AB->00 to last frame: median {statistics.median(g):.2f}s min {min(g):.2f} max {max(g):.2f} (n={len(g)})")
    print(f"last payload change to last frame: median {statistics.median(d):.2f}s min {min(d):.2f} max {max(d):.2f}")
    short = [r["dur"] for r in rows if r["last_change"] < 0.5]
    if short:
        print(f"wakes with no payload change after the first 0.5 s: n={len(short)}, duration median {statistics.median(short):.2f}s")


if __name__ == "__main__":
    main()
