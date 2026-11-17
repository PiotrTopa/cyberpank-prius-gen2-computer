"""
Run the alert set over a recorded trip and print what would have fired.

    python -m cyberpunk_computer.alerts.replay TRIP [--roadsense WS.jsonl[.gz]] [--rs-offset S]
                                               [--modes configured|shadow] [--settings DIR]
                                               [--tuning FILE] [--json OUT.jsonl] [--hz 10]

TRIP is either

* a backend trip recording (``trips/*.ndjson[.gz]``, io/trip_recorder.py:
  ``{"ts": ms, "dir": "IN", "id": device, "d": data}`` per line), or
* a decoded-frame CSV as in tests/fixtures/fuel_drive_2026-11-13.csv.gz
  (``t,bus,id,data``; bus C = CAN id hex, B = BEAN DST:MES).

Frames go through the real ingress (CAN decoder, BEAN ingest, powerbox
parsers) into a Store, so the alert functions see exactly the state fields the
backend has. ``--roadsense`` adds a recorded roadsense ``/ws`` stream (JSON
lines with ``ts_mono_ns``): it goes through the backend's own SceneBuilder, so
the functions get the same scene mapping as on the car; ``--rs-offset`` shifts
it (seconds, roadsense time 0 = trip time offset).

By default every type is evaluated as if in shadow mode (also the ones that are
off), so the owner can review all of them; ``--modes configured`` uses the
modes in ``--settings`` (alerts_settings.json). Nothing is sent anywhere.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from typing import Iterator, List, Optional, Tuple


def _open(path: str):
    return (gzip.open(path, "rt", errors="replace") if path.endswith(".gz")
            else open(path, "r", errors="replace"))


def trip_events(path: str) -> Iterator[Tuple[float, str, object]]:
    """(t s, kind, payload): kind "raw" (device, data) or "can"/"bean" from a CSV."""
    with _open(path) as fh:
        first = None
        for line in fh:
            line = line.replace("\0", "").strip()      # NUL tail of a hard reset
            if not line or line.startswith("#"):
                continue
            if line.startswith("{"):
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("dir", "IN") != "IN" or "d" not in e:
                    continue
                ts = float(e.get("ts") or 0) / 1000.0
                if first is None:
                    first = ts
                yield ts - first, "raw", (int(e.get("id", 0)), e["d"])
            else:
                parts = line.split(",")
                if len(parts) != 4:
                    continue
                t, bus, ident, data = parts
                yield float(t), "can" if bus == "C" else "bean", (ident, data)


def roadsense_lines(path: str) -> List[Tuple[float, str]]:
    out = []
    t0 = None
    with _open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ns = json.loads(line).get("ts_mono_ns")
            except ValueError:
                continue
            if not isinstance(ns, (int, float)):
                continue
            if t0 is None:
                t0 = ns
            out.append(((ns - t0) / 1e9, line))
    return out


class Replay:
    def __init__(self, modes: str = "shadow", settings_dir: Optional[str] = None, tuning_file: Optional[str] = None,
                 hz: float = 10.0):
        import os
        from ..io.ingress import IngressController
        from ..io.mock_io import MockInputPort
        from ..io.powerbox import register_powerbox_ingress
        from ..state.rules import RulesEngine
        from ..state.rules.fuel_consumption import FuelConsumptionRule
        from ..state.rules.trip_fuel import TripFuelConsumptionRule
        from ..state.store import Store
        from .service import AlertService
        from .tuning import load as load_tuning
        from ..backend.roadsense_scene import SceneBuilder, SceneFeedConfig
        self.t = 0.0
        clock = lambda: self.t  # noqa: E731
        self.store = Store()
        self.ing = IngressController(self.store, MockInputPort())
        register_powerbox_ingress(self.ing)
        for attr in ("_sources", "_bean"):
            obj = getattr(self.ing, attr, None)
            if obj is not None and hasattr(obj, "_clock"):
                obj._clock = clock
        rules = RulesEngine(self.store)
        for r in (FuelConsumptionRule(), TripFuelConsumptionRule(clock=clock)):
            rules.register(r)
        if tuning_file:
            os.environ["BACKEND_ALERTS_TUNING"] = tuning_file
        self.svc = AlertService(self.store, settings_dir if modes == "configured" else None,
                                tuning=load_tuning(None), hz=hz, clock=clock, wall=clock)
        self.svc.attach()
        if modes != "configured":
            for aid in self.svc.manager.specs:
                self.svc.settings.set_mode(aid, "shadow")
            self.svc.manager.mode = self._mode_shadow
        self.scenes = SceneBuilder(SceneFeedConfig(road_max_age_s=2.0))
        self.period = 1.0 / hz

    def _mode_shadow(self, alert_id: str) -> str:
        return "shadow" if alert_id in self.svc.manager.specs else "off"

    def feed(self, kind: str, payload) -> None:
        from ..io.ports import RawMessage
        if kind == "raw":
            dev, data = payload
            self.ing._process_message(RawMessage(device_id=dev, timestamp=int(self.t * 1000), data=data))
        elif kind == "can":
            ident, data = payload
            b = bytes.fromhex(data)
            self.ing._process_message(RawMessage(device_id=1, timestamp=int(self.t * 1000),
                                                 data={"i": "0x%03X" % int(ident, 16), "d": list(b)}))
        elif kind == "bean":
            ident, data = payload
            dst, mes = ident.split(":")
            rec = [int(self.t * 1000), 6, dst, mes, data, 1, 0]
            self.ing._process_message(RawMessage(device_id=203, timestamp=0,
                                                 data={"t": int(self.t * 1000), "bf": [rec]}))

    def run(self, trip: str, roadsense: Optional[str] = None, rs_offset: float = 0.0) -> List[dict]:
        rs = roadsense_lines(roadsense) if roadsense else []
        ri = 0
        next_tick = 0.0
        for t, kind, payload in trip_events(trip):
            while next_tick <= t:
                self.t = next_tick
                while ri < len(rs) and rs[ri][0] + rs_offset <= self.t:
                    self.scenes.feed(rs[ri][1], self.t)
                    ri += 1
                sc = self.scenes.build(self.t)
                if sc is not None:
                    self.svc.on_scene(sc)
                self.ing.update()
                self.svc.tick(force=True)
                next_tick += self.period
            self.t = t
            try:
                self.feed(kind, payload)
            except Exception:  # noqa: BLE001 - a bad line never stops a replay
                pass
        return [e for e in reversed(self.svc.log.recent_entries(500))
                if e.get("event") in ("fire", "action", "note", "stat")]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("trip")
    ap.add_argument("--roadsense")
    ap.add_argument("--rs-offset", type=float, default=0.0)
    ap.add_argument("--modes", choices=("shadow", "configured"), default="shadow")
    ap.add_argument("--settings", help="alerts directory (alerts_settings.json) for --modes configured")
    ap.add_argument("--tuning", help="alerts_tuning.json")
    ap.add_argument("--json", help="write the entries as JSON lines here")
    ap.add_argument("--hz", type=float, default=10.0)
    a = ap.parse_args(argv)
    import logging
    logging.basicConfig(level=logging.WARNING)
    rp = Replay(a.modes, a.settings, a.tuning, a.hz)
    entries = rp.run(a.trip, a.roadsense, a.rs_offset)
    counts = {}
    for e in entries:
        what = e.get("text") or e.get("action") or e.get("what") or e.get("id")
        ev = e.get("evidence") or e.get("data") or {}
        print("t=%8.1fs  %-7s %-18s %s  %s" % (e.get("ts", 0), e.get("event"), e.get("id"), what,
                                                json.dumps(ev, separators=(",", ":"), default=str)[:160]))
        if e.get("event") in ("fire", "action"):
            counts[e.get("id")] = counts.get(e.get("id"), 0) + 1
    print("-- %.0f s replayed; would have fired: %s" % (rp.t, ", ".join("%s x%d" % kv for kv in sorted(counts.items())) or "nothing"))
    fn = rp.svc.manager.functions
    for f in fn:
        st = f.status()
        if f.name == "eco_coach":
            print("-- braking score: %s" % json.dumps(st.get("drive")))
    if a.json:
        with open(a.json, "w") as fh:
            for e in entries:
                fh.write(json.dumps(e, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
