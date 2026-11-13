#!/usr/bin/env python3
"""Compare our BEAN captures with the published bean_tools NHW20 (JP) dumps.

For every DST:MES (FE:AB / FE:C2 per node) prints, side by side, the PRI values and the set of
payloads seen in each of our captures and in each JP dump (acc_off_key_ejected,
acc_off_key_inserted, acc_off_power_on = power OFF; acc_ign_on_park_n_park_p = IG-ON;
ready = READY), and flags:
  ONLY-JP   message never seen on our car (all our captures are power OFF)
  ONLY-OURS message never seen in the JP dumps
  DIFF      present in both but the power-OFF payload sets do not overlap
  DST       same MES-ID with a different DST-ID (e.g. odometer 52:3F vs FE:3F)
It also prints, for JP-only messages, the time of first appearance after the wake in the
IG-ON and READY dumps.

Usage:
  bean_compare.py OUR_CAPTURE [OUR_CAPTURE ...] --fixture tests/fixtures/bean_nhw20_frames.txt.gz
"""
from __future__ import annotations

import argparse
from collections import defaultdict

from beanlib import load_fixture, load_ndjson, node_key

JP_OFF = ("acc_off_key_ejected.dat", "acc_off_key_inserted.dat", "acc_off_power_on.dat")
JP_ORDER = JP_OFF + ("acc_ign_on_park_n_park_p.dat", "ready.dat")


def summarise(frames):
    d = defaultdict(lambda: {"pri": set(), "payloads": set(), "first": None})
    t0 = frames[0].t if frames else 0
    for f in frames:
        e = d[node_key(f)]
        e["pri"].add(f.pri)
        e["payloads"].add(f.hex)
        if e["first"] is None:
            e["first"] = f.t - t0
    return d


def fmt(e, maxp=4):
    if not e:
        return "-"
    p = sorted(e["payloads"])
    s = ",".join(p[:maxp]) + (f",+{len(p) - maxp}" if len(p) > maxp else "")
    return f"p{'/'.join(map(str, sorted(e['pri'])))} {s}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ours", nargs="+")
    ap.add_argument("--fixture", required=True)
    a = ap.parse_args(argv)
    ours = {p.split("/")[-1].split(".")[0]: summarise(load_ndjson(p).frames) for p in a.ours}
    jp_caps = load_fixture(a.fixture)
    jp = {n: summarise(jp_caps[n].frames) for n in JP_ORDER if n in jp_caps}
    keys = sorted(set().union(*[set(d) for d in ours.values()], *[set(d) for d in jp.values()]))
    our_mes = defaultdict(set)
    jp_mes = defaultdict(set)
    for d in ours.values():
        for k in d:
            our_mes[k.split(":")[1][:2]].add(k.split(":")[0])
    for d in jp.values():
        for k in d:
            jp_mes[k.split(":")[1][:2]].add(k.split(":")[0])
    for k in keys:
        in_ours = any(k in d for d in ours.values())
        in_jp = any(k in d for d in jp.values())
        our_off = set().union(*[d[k]["payloads"] for d in ours.values() if k in d]) if in_ours else set()
        jp_off = set().union(*[jp[n][k]["payloads"] for n in JP_OFF if n in jp and k in jp[n]])
        flags = []
        if in_jp and not in_ours:
            flags.append("ONLY-JP")
        if in_ours and not in_jp:
            flags.append("ONLY-OURS")
        if our_off and jp_off and not (our_off & jp_off):
            flags.append("DIFF")
        mes = k.split(":")[1][:2]
        if our_mes[mes] and jp_mes[mes] and our_mes[mes] != jp_mes[mes]:
            flags.append(f"DST ours {sorted(our_mes[mes])} jp {sorted(jp_mes[mes])}")
        print(f"{k:<11} {' '.join(flags)}")
        for n, d in ours.items():
            if k in d:
                print(f"    ours {n:<40} {fmt(d[k])}")
        for n, d in jp.items():
            if k in d:
                extra = f"  first at +{d[k]['first']:.2f}s" if n not in JP_OFF else ""
                print(f"    jp   {n:<40} {fmt(d[k])}{extra}")


if __name__ == "__main__":
    main()
