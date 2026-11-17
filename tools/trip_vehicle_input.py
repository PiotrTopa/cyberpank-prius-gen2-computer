#!/usr/bin/env python3
"""Trip log -> the vehicle input stream the feeder would have sent to roadsense.

Runs the broadcast CAN frames of recorded trip files (``BACKEND_RECORD``,
``logs/trips/trip_*.ndjson[.gz]``) through the real feeder code
(``backend/position_feeder.py``: ``CanMotionTap`` + ``PositionFeeder``) on the
log's own clock and writes every message it sends as one JSON line: the input
of roadsense ``tools/vehicle_replay.py`` (offline replay through mapd's engine)
and a way to check the signal mapping on a real drive without the car.

    tools/trip_vehicle_input.py trip_A.ndjson.gz trip_B.ndjson.gz -o drive.vehicle.ndjson
    tools/trip_vehicle_input.py trip.ndjson --yaw wheels,gyro --hz 20 --stats

``ts_mono_ns`` in the output is the log's wall clock in ns (the trip files
carry no CLOCK_MONOTONIC); roadsense's replay tool aligns a recorder session
by its ``ts_real_ns``. Trip logs hold frames that changed (the ingress
de-duplicates a constant frame to one per 0.25 s), so a signal that does not
change is refreshed 4 times a second, not at the bus rate. GNSS is not in the
trip files unless ``BACKEND_RECORD_INCLUDE`` has ``position`` (``POS`` lines
are passed through as ``gnss`` messages when present).
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cyberpunk_computer.backend.position_feeder import (  # noqa: E402
    CanMotionTap, NoGnssSource, PositionFeeder, PositionFeederConfig)

V11 = ["gnss", "motion", "reset", "controls", "body", "vehicle", "objects"]


class CaptureFeed:
    """Stands in for mapd: accepts everything, keeps the messages."""

    def __init__(self, out, accepts):
        self.out, self.accepts = out, accepts
        self.path = "(capture)"
        self.sent = self.dropped = self.errors_total = 0
        self.connects = 1
        self.errors = []
        self.counts = collections.Counter()

    connected = True

    def poll(self):
        pass

    def send(self, m):
        self.out.write(json.dumps(m, separators=(",", ":")) + "\n")
        self.counts[m["type"]] += 1
        self.sent += 1
        return True

    def close(self):
        pass


def frames(paths):
    """(t_wall, can_id, data bytes) of the panda broadcast frames, and ("POS", fix) lines, in file order."""
    for path in paths:
        op = gzip.open if path.endswith(".gz") else open
        with op(path, "rt", errors="replace") as f:
            for line in f:
                line = line.strip("\x00\r\n ")
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                d = m.get("d")
                if not isinstance(d, dict):
                    continue
                if m.get("dir") == "POS":
                    yield m.get("ts"), "POS", d
                elif m.get("id") == 1 and "a" not in d and isinstance(d.get("i"), str) and isinstance(d.get("d"), list):
                    ts = m.get("ts")
                    if isinstance(ts, (int, float)) and ts > 1e9:
                        try:
                            yield float(ts), int(d["i"], 16), bytes(d["d"])
                        except ValueError:
                            continue


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("trips", nargs="+")
    ap.add_argument("-o", "--out", default="-")
    ap.add_argument("--yaw", default="wheels", help="BACKEND_POSITION_YAW (wheels, steer, gyro)")
    ap.add_argument("--hz", type=float, default=10.0, help="BACKEND_POSITION_MOTION_HZ")
    ap.add_argument("--v1", action="store_true", help="as against an older roadsense (position input API v1)")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    out = sys.stdout if a.out == "-" else open(a.out, "w")
    cfg = PositionFeederConfig(enabled=True, source="none", motion_hz=a.hz,
                               yaw_inputs=tuple(t for t in a.yaw.split(",") if t))
    now = [0.0]
    feed = CaptureFeed(out, ["gnss", "motion", "reset"] if a.v1 else V11)
    tap = CanMotionTap()
    feeder = PositionFeeder(cfg, NoGnssSource(), tap=tap, feed=feed, clock=lambda: now[0])
    period = 1.0 / a.hz
    nxt = None
    ids = collections.Counter()
    t0 = t1 = None
    for t, can_id, data in frames(sorted(a.trips)):
        if can_id == "POS":
            out.write(json.dumps(dict(data, type="gnss"), separators=(",", ":")) + "\n")
            continue
        if nxt is None:
            nxt, t0 = t, t
        while t >= nxt:                       # the feeder thread's cycles up to this frame
            now[0] = nxt
            feeder.step(nxt)
            nxt += period
            if t - nxt > 5.0:                 # a hole in the log (car off between trips)
                nxt = t
        ids[can_id] += 1
        t1 = t
        tap.on_frame(can_id, data, 0, t, t)
    if out is not sys.stdout:
        out.close()
    if a.stats and t0 is not None:
        span = t1 - t0
        print("span %.0f s; messages: %s" % (span, dict(feed.counts)), file=sys.stderr)
        print("rates per s: " + ", ".join("%s %.1f" % (k, v / span) for k, v in feed.counts.items()), file=sys.stderr)
        print("frames per s in the log: " + ", ".join("0x%03X %.1f" % (i, ids[i] / span) for i in sorted(tap.IDS)),
              file=sys.stderr)


if __name__ == "__main__":
    main()
