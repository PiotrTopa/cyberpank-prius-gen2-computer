"""Offline tooling: ``python -m cyberpunk_computer.battery <command>``.

    backfill --db battery.db [--trips DIR|FILE ...] [--metrics metrics.db]
    reanalyse --db battery.db [--force]
    summary --db battery.db [--json out.json]     text report (or the UI document)
    export --db battery.db --out DIR              rides.csv, blocks.csv, rests.csv
    ride --db battery.db ID [--csv out.csv]       one ride's features / raw block series
    synth --db demo.db [--days N --weak-block K --leaky-block K ...]   synthetic history
    serve --db battery.db [--port 8099]           the battery API alone (UI development)

Same code as the backend; standard library only (``serve`` needs fastapi).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import analysis, backfill, export, summary, synth
from .collector import Collector
from .store import BatteryStore


def _fmt_ts(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "-"


def cmd_backfill(a) -> int:
    store = BatteryStore(a.db)
    if a.metrics:
        print("metrics:", backfill.backfill_metrics(store, a.metrics, log=print))
    if a.trips:
        res = backfill.backfill_trips(store, a.trips, force=a.force, log=print)
        print("trips:", res)
    store.set_cache("summary", summary.build_summary(store))
    print("store:", store.stats())
    return 0


def cmd_reanalyse(a) -> int:
    store = BatteryStore(a.db)
    print(store.reanalyse(force=a.force))
    store.set_cache("summary", summary.build_summary(store))
    return 0


def text_report(s: dict) -> str:
    out = ["Battery health summary  (generated %s, %d rides, %d rests)" % (
        _fmt_ts(s["generated_ts"]), s["quality"]["rides"], s["quality"]["rests"]),
        "overall: HV %s, 12 V %s" % (s["overall"]["hv"], s["overall"]["aux"]), ""]
    for i in s["indicators"]:
        tr = s["trends"].get(i.get("trend") or "", {})
        slope = ""
        if tr.get("slope_30d") is not None:
            slope = "  trend %+.3g%s/30d [%+.3g, %+.3g]%s" % (
                tr["slope_30d"] * (100 if tr.get("log") else 1), " %" if tr.get("log") else "",
                tr["lo_30d"] * (100 if tr.get("log") else 1), tr["hi_30d"] * (100 if tr.get("log") else 1),
                " *" if tr.get("significant") else "")
        elif tr.get("status") and tr["status"] != "ok":
            slope = "  (%s)" % tr["status"]
        out.append("%-7s %-32s %10s %-7s n=%-4s%s" % (
            i["level"].upper(), i["title"], "-" if i["value"] is None else i["value"], i["unit"], i["n"], slope))
    b = s["blocks"].get("latest")
    if b:
        out += ["", "latest graded ride %s (battery %.1f degC)" % (_fmt_ts(b["ts"]), b["t_mean"] or float("nan")),
                "block      " + " ".join("%6d" % (k + 1) for k in range(14)),
                "dev@50A mV " + " ".join("%6.0f" % x for x in b["dev25_mv"]),
                "dev@0A  mV " + " ".join("%6.0f" % x for x in b["dev0_mv"])]
        if b.get("r_mohm"):
            out.append("R mOhm     " + " ".join("%6.1f" % x for x in b["r_mohm"]))
        if b.get("ecu_mohm"):
            out.append("R ECU mOhm " + " ".join("%6.0f" % x for x in b["ecu_mohm"]))
    q = s["quality"]
    out += ["", "quality: grades %s  flags %s" % (q["grades"], q["flags"]),
            "storage: %s bytes in %s" % (s["stats"]["db_bytes"], s["stats"]["path"])]
    return "\n".join(out)


def cmd_summary(a) -> int:
    store = BatteryStore(a.db)
    s = summary.build_summary(store)
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(s, fh)
    print(text_report(s))
    return 0


def cmd_export(a) -> int:
    store = BatteryStore(a.db)
    os.makedirs(a.out, exist_ok=True)
    for name, fn in export.EXPORTS.items():
        path = os.path.join(a.out, "battery_%s.csv" % name)
        with open(path, "w") as fh:
            fh.write(fn(store))
        print("wrote", path)
    return 0


def cmd_ride(a) -> int:
    store = BatteryStore(a.db)
    row = store.get("rides", a.id)
    if row is None:
        print("no such ride", file=sys.stderr)
        return 1
    rec = store.record(row)
    if a.csv and rec:
        with open(a.csv, "w") as fh:
            fh.write(export.ride_series_csv(rec))
    print(json.dumps(row["features"], indent=1))
    return 0


def cmd_synth(a) -> int:
    if os.path.exists(a.db):
        print("refusing to write synthetic data into an existing database: %s" % a.db, file=sys.stderr)
        return 1
    store = BatteryStore(a.db)
    cfg = synth.SynthConfig(seed=a.seed, days=a.days, start_ts=time.time() - a.days * 86400.0, weak_block=a.weak_block, weak_start_day=a.weak_start_day,
                            leaky_block=a.leaky_block, leak_soc_per_day=a.leak, pack_r_growth_per_year=a.r_growth,
                            aux_fade_per_year=a.aux_fade, full_21cf=not a.first_frame_cf)
    col = Collector(lambda r: store.put_ride(r), lambda r: store.put_rest(r), source="synth")
    t0 = time.time()
    synth.simulate(cfg, col)
    store.set_cache("summary", summary.build_summary(store))
    print("synthetic history: %s (%.1f s)" % (store.stats(), time.time() - t0))
    return 0


def cmd_serve(a) -> int:
    import uvicorn
    from fastapi import FastAPI
    from . import api, service
    svc = service.BatteryHealthService(a.db)
    svc.store.set_cache("summary", summary.build_summary(svc.store))
    service.set_active(svc)
    app = FastAPI()
    api.register(app, "/api/v1/battery", lambda: None)
    if a.static and os.path.isdir(a.static):
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=a.static, html=True))
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m cyberpunk_computer.battery", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("backfill"); s.add_argument("--db", required=True)
    s.add_argument("--trips", nargs="*", default=[]); s.add_argument("--metrics")
    s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_backfill)
    s = sub.add_parser("reanalyse"); s.add_argument("--db", required=True)
    s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_reanalyse)
    s = sub.add_parser("summary"); s.add_argument("--db", required=True); s.add_argument("--json")
    s.set_defaults(fn=cmd_summary)
    s = sub.add_parser("export"); s.add_argument("--db", required=True); s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("ride"); s.add_argument("--db", required=True); s.add_argument("id", type=int)
    s.add_argument("--csv"); s.set_defaults(fn=cmd_ride)
    s = sub.add_parser("synth"); s.add_argument("--db", required=True)
    s.add_argument("--days", type=int, default=120); s.add_argument("--seed", type=int, default=1)
    s.add_argument("--weak-block", type=int); s.add_argument("--weak-start-day", type=float, default=0.0)
    s.add_argument("--leaky-block", type=int); s.add_argument("--leak", type=float, default=0.25)
    s.add_argument("--r-growth", type=float, default=0.0)
    s.add_argument("--aux-fade", type=float, default=0.0); s.add_argument("--first-frame-cf", action="store_true")
    s.set_defaults(fn=cmd_synth)
    s = sub.add_parser("serve"); s.add_argument("--db", required=True)
    s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8099)
    s.add_argument("--static"); s.set_defaults(fn=cmd_serve)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
