#!/usr/bin/env python3
"""
All-ECU diagnostics from the command line.

Dry run against the built-in ECU simulator (no hardware, nothing on any bus):

    python scripts/diag_cli.py --sim scan
    python scripts/diag_cli.py --sim --scenario clean scan --probe
    python scripts/diag_cli.py --sim clear engine --confirm

Against the running backend's REST API (the backend does the bus work through
the gateway; this tool only posts commands and reads the state):

    python scripts/diag_cli.py --url http://10.200.0.14:8080 identify
    python scripts/diag_cli.py --url http://10.200.0.14:8080 scan --probe
    python scripts/diag_cli.py --url http://10.200.0.14:8080 read hv 13
    python scripts/diag_cli.py --url http://10.200.0.14:8080 report
    python scripts/diag_cli.py --url http://10.200.0.14:8080 clear hv --confirm

Commands: scan [--ecus a,b] [--kinds stored,pending,...] [--probe], identify,
read <ecu> <item|hex>, clear <ecu> --confirm [--allow-moving], report, ecus,
autoclear (show), cancel. ``--report FILE`` saves the full JSON report,
``--trace`` prints every request/response.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── report printing ──────────────────────────────────────────────────────────

def _ts(ts: Optional[float]) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"


def print_report(rep: Dict[str, Any], trace: bool = False) -> None:
    scan = rep.get("scan") or {}
    print("Diagnostics report  %s" % _ts(rep.get("ts") or scan.get("finished_ts")))
    print("  VIN: %s    scan: %s (%s/%s ECUs)  kinds: %s" % (
        rep.get("vin") or (rep.get("vehicle") or {}).get("vin") or "not read",
        scan.get("state", "?"), scan.get("done", "?"), scan.get("total", "?"),
        ",".join(scan.get("kinds") or []) or "-"))
    if scan.get("report_file"):
        print("  report file: %s" % scan["report_file"])
    if scan.get("error"):
        print("  last error: %s" % scan["error"])
    print()
    total = 0
    for key, e in (rep.get("ecus") or {}).items():
        addr = e.get("address") or "-"
        if e.get("sub_address"):
            addr += "/" + e["sub_address"]
        status = e.get("status", "?")
        line = "%-13s %-44s %-10s %s" % (key, e.get("name", ""), addr, status.upper())
        if e.get("mil") is not None:
            line += "  MIL %s" % ("ON" if e["mil"] else "off")
        print(line)
        if e.get("error"):
            print("    ! %s" % e["error"])
        for d in e.get("dtcs") or []:
            total += 1
            print("    %-6s [%s] %s" % (d["code"], ",".join(d.get("status", [])),
                                       d.get("description") or "(not in the dictionary)"))
            extra = []
            if d.get("manual_page"):
                extra.append("manual %s" % d["manual_page"])
            if d.get("master_warning"):
                extra.append("master warning: %s of its detail codes" % d["master_warning"])
            if d.get("sources"):
                extra.append("via " + ", ".join(d["sources"]))
            if extra:
                print("           %s" % "; ".join(extra))
            if d.get("trouble_area"):
                print("           trouble area: %s" % ", ".join(d["trouble_area"]))
        ff = e.get("freeze_frame")
        if ff:
            vals = ", ".join("%s=%s%s" % (k, v["value"], v["unit"]) for k, v in ff.get("values", {}).items())
            print("    freeze frame for %s: %s" % (ff.get("dtc"), vals or "(no values)"))
        ident = {k: v for k, v in (e.get("ident") or {}).items() if k != "raw"}
        if ident:
            print("    ident: %s" % json.dumps(ident, ensure_ascii=False))
        if e.get("last_clear"):
            lc = e["last_clear"]
            print("    last clear %s: %s" % (_ts(lc.get("ts")), lc.get("result")))
        for line in e.get("log") or []:
            print("    . %s" % line)
    print()
    print("%d code(s) in total." % total)
    if trace:
        print("\nTrace:")
        for t in rep.get("trace") or []:
            print("  %-13s %s%s  ->  %s  (%s ms)%s" % (
                t.get("ecu"), t.get("tx"), "/" + t["sub"] if t.get("sub") else "", t.get("req"),
                t.get("ms"), "  " + (t.get("resp") or t.get("error") or "")))


def print_autoclear(ac: Optional[Dict[str, Any]]) -> None:
    if not ac:
        print("auto-clear: no state")
        return
    print("auto-clear: %s  trigger=%s  speed=%s  min interval %ss  max/day %s" % (
        "ON" if ac.get("enabled") else "off", ac.get("trigger"), ac.get("speed_policy"),
        ac.get("min_interval_s"), ac.get("max_per_day")))
    for c in ac.get("codes") or []:
        print("  %-10s %s %s" % (c["ecu"], c["code"], "" if c.get("enabled", True) else "(disabled)"))
    for k, b in (ac.get("blocked") or {}).items():
        print("  BLOCKED %s: %s" % (k, b.get("reason")))
    for k, w in (ac.get("waiting") or {}).items():
        print("  waiting %s: %s" % (k, w))
    print("  counters: %s" % json.dumps(ac.get("counters")))
    if ac.get("last_action"):
        a = ac["last_action"]
        print("  last action %s: %s %s -> %s" % (_ts(a.get("ts")), a.get("ecu"), a.get("codes"), a.get("result")))


# ── simulator backend ────────────────────────────────────────────────────────

def run_sim(args: argparse.Namespace) -> int:
    from cyberpunk_computer.diag.engine import DEFAULT_KINDS, DiagEngine
    from cyberpunk_computer.diag.sim import make_prius

    clock = [0.0]
    holder: Dict[str, Any] = {}
    gw = make_prius(lambda d: holder["eng"].on_gateway_message(d), scenario=args.scenario)
    eng = DiagEngine(gw.send, clock=lambda: clock[0], vehicle_speed=lambda: 0.0)
    holder["eng"] = eng

    def run() -> None:
        for _ in range(200000):
            eng.tick()
            gw.step()
            clock[0] += 0.01
            if not eng.busy:
                eng.tick()
                return
        raise SystemExit("simulator did not finish")

    cmd = args.command
    if cmd == "scan":
        kinds = list(args.kinds.split(",")) if args.kinds else list(DEFAULT_KINDS) + ["ident"]
        if args.probe and "probe" not in kinds:
            kinds.append("probe")
        ok, msg = eng.start_scan(args.ecus.split(",") if args.ecus else None, kinds)
    elif cmd == "identify":
        ok, msg = eng.start_identify(args.ecus.split(",") if args.ecus else None)
    elif cmd == "read":
        ok, msg = eng.start_read(args.ecu, args.item)
    elif cmd == "clear":
        ok, msg = eng.start_clear(args.ecu, confirm=args.confirm, allow_moving=args.allow_moving)
    elif cmd == "ecus":
        return print_ecus([_ecu_row(e) for e in eng.ecus.values()])
    else:
        raise SystemExit("'%s' needs the backend (--url)" % cmd)
    if not ok:
        print("refused: %s" % msg, file=sys.stderr)
        return 2
    run()
    rep = eng.report()
    if cmd == "read":
        print(json.dumps(eng.last_read, indent=1, ensure_ascii=False))
    else:
        print_report(rep, trace=args.trace)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, ensure_ascii=False, default=str)
    print("\n[simulator: %d request frames, scenario '%s']" % (len(gw.sent), args.scenario))
    return 0


def _ecu_row(e: Any) -> Dict[str, Any]:
    return {"key": e.key, "name": e.name, "address": "0x%03X" % e.tx_id if e.tx_id else None,
            "sub_address": None if e.sub_addr is None else "0x%02X" % e.sub_addr, "bus": e.bus,
            "reachable": e.reachable, "probe": e.probe, "confidence": e.confidence,
            "protocol": e.protocol_label}


def print_ecus(rows: List[Dict[str, Any]]) -> int:
    for e in rows:
        addr = (e.get("address") or "-") + ("/" + e["sub_address"] if e.get("sub_address") else "")
        how = "CAN" if e.get("reachable") else ("probe only" if e.get("probe") else "not on CAN")
        print("%-13s %-46s %-10s %-6s %-11s %-10s %s" % (
            e["key"], e["name"], addr, e.get("bus"), how, e.get("confidence"), e.get("protocol")))
    return 0


# ── REST backend ─────────────────────────────────────────────────────────────

class Rest:
    def __init__(self, url: str, token: Optional[str]) -> None:
        self.base = url.rstrip("/") + "/api/v1"
        self.token = token

    def _req(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise SystemExit("HTTP %d on %s: %s" % (e.code, path, detail))
        except urllib.error.URLError as e:
            raise SystemExit("cannot reach %s: %s" % (self.base, e.reason))

    def command(self, name: str, params: Dict[str, Any]) -> Any:
        return self._req("POST", "/commands/" + name, params)

    def diag(self) -> Dict[str, Any]:
        return self._req("GET", "/state")["state"]["diagnostics"]

    def get(self, path: str) -> Any:
        return self._req("GET", path)


def run_rest(args: argparse.Namespace) -> int:
    api = Rest(args.url, args.token or os.environ.get("PRIUS_API_TOKEN"))
    cmd = args.command
    if cmd == "report":
        print_report(api.get("/diag/report"), trace=args.trace)
        return 0
    if cmd == "ecus":
        return print_ecus(api.get("/diag/ecus")["ecus"])
    if cmd == "autoclear":
        print_autoclear(api.diag().get("autoclear"))
        return 0
    if cmd == "cancel":
        api.command("diag_cancel", {})
        return 0

    before = api.diag()
    if cmd in ("scan", "identify"):
        params: Dict[str, Any] = {}
        if args.ecus:
            params["ecus"] = args.ecus.split(",")
        if cmd == "scan":
            if args.kinds:
                params["kinds"] = args.kinds.split(",")
            if args.probe:
                params["probe"] = True
        prev = (before.get("scan") or {}).get("started_ts")
        api.command("diag_" + cmd, params)
        deadline = time.time() + args.timeout
        scan: Dict[str, Any] = {}
        while time.time() < deadline:
            time.sleep(1.0)
            scan = api.diag().get("scan") or {}
            if scan.get("started_ts") != prev and scan.get("state") in ("done", "cancelled"):
                break
            if scan.get("error") and scan.get("started_ts") == prev:
                print("refused: %s" % scan["error"], file=sys.stderr)
                return 2
            print("  ... %s %s/%s (%s)" % (scan.get("state"), scan.get("done"), scan.get("total"),
                                           scan.get("current")), file=sys.stderr)
        else:
            print("timed out waiting for the scan", file=sys.stderr)
            return 3
        try:
            rep = api.get("/diag/report")
        except SystemExit:
            d = api.diag()
            rep = {"ecus": d.get("ecus"), "scan": d.get("scan"), "vehicle": d.get("vehicle")}
        print_report(rep, trace=args.trace)
        if args.report:
            with open(args.report, "w", encoding="utf-8") as f:
                json.dump(rep, f, indent=1, ensure_ascii=False)
        return 0
    if cmd == "read":
        prev = (before.get("last_read") or {}).get("ts")
        api.command("diag_read", {"ecu": args.ecu, "item": args.item})
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            time.sleep(1.0)
            lr = api.diag().get("last_read") or {}
            if lr.get("ts") != prev:
                print(json.dumps(lr, indent=1, ensure_ascii=False))
                return 0
        print("timed out waiting for the read", file=sys.stderr)
        return 3
    if cmd == "clear":
        if not args.confirm:
            print("clear erases every code and the freeze frame of the ECU; add --confirm", file=sys.stderr)
            return 2
        prev = ((before.get("ecus") or {}).get(args.ecu) or {}).get("last_clear") or {}
        api.command("diag_clear", {"ecu": args.ecu, "confirm": True, "allow_moving": args.allow_moving})
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            time.sleep(1.0)
            lc = ((api.diag().get("ecus") or {}).get(args.ecu) or {}).get("last_clear") or {}
            if lc.get("ts") != prev.get("ts"):
                print("%s: %s" % ("CLEARED" if lc.get("ok") else "NOT CLEARED", lc.get("result")))
                print("cleared: %s  remaining: %s" % (lc.get("cleared"), lc.get("remaining")))
                return 0 if lc.get("ok") else 1
        print("timed out waiting for the clear", file=sys.stderr)
        return 3
    raise SystemExit("unknown command %r" % cmd)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sim", action="store_true", help="run against the built-in ECU simulator")
    p.add_argument("--scenario", default="buttons_unplugged", choices=["buttons_unplugged", "clean"])
    p.add_argument("--url", help="backend base URL, e.g. http://10.200.0.14:8080")
    p.add_argument("--token", help="API bearer token (or env PRIUS_API_TOKEN)")
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--trace", action="store_true", help="print every request/response")
    p.add_argument("--report", help="save the full JSON report to this file")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--ecus"); s.add_argument("--kinds"); s.add_argument("--probe", action="store_true")
    s = sub.add_parser("identify"); s.add_argument("--ecus")
    s = sub.add_parser("read"); s.add_argument("ecu"); s.add_argument("item")
    s = sub.add_parser("clear"); s.add_argument("ecu")
    s.add_argument("--confirm", action="store_true"); s.add_argument("--allow-moving", action="store_true")
    for name in ("report", "ecus", "autoclear", "cancel"):
        sub.add_parser(name)
    args = p.parse_args(argv)
    for attr, default in (("ecus", None), ("kinds", None), ("probe", False)):
        if not hasattr(args, attr):
            setattr(args, attr, default)
    if args.sim:
        return run_sim(args)
    if not args.url:
        p.error("give --sim or --url")
    return run_rest(args)


if __name__ == "__main__":
    sys.exit(main())
