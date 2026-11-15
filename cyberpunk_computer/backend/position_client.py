"""Client of the roadsense position input API v1 (``mapd``'s ``position.sock``).

Origin: ``PositionFeed`` is copied from roadsense ``tools/position_client.py``
(github.com/PiotrTopa/roadsense, MIT licence, copied 2026-11-15; contract:
roadsense docs/ARCHITECTURE.md "mapd" -> "Position input API v1"). Nothing
is imported from roadsense. Changes against the reference client:

* a short (partial) ``send`` on the non-blocking socket closes the connection
  and counts the message as dropped. The reference client counts it as sent
  and leaves half a JSON line in the stream, which corrupts the next message;
  mapd sees the connection end instead and drops the partial line.
* ``connected`` property and ``errors_total`` counter for the status mirror.
* the retry period after a failed connect is ``retry_s`` (default 2 s here).

Transport: Unix stream socket ``<rundir>/position.sock``, one JSON object per
line. The client never blocks the caller: while mapd is away every message is
dropped and a reconnect is tried at most every ``retry_s``.
"""

from __future__ import annotations

import json
import os
import socket
import time

API = 1


class PositionFeed:
    def __init__(self, rundir="/run/roadsense", name="feeder", retry_s=2.0):
        self.path = os.path.join(rundir, "position.sock")
        self.name, self.retry_s = name, retry_s
        self.sock = None
        self._next_try = 0.0
        self.sent = self.dropped = 0
        self.connects = 0
        self.errors = []                 # error replies from mapd (most recent last, at most 20)
        self.errors_total = 0

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def _connect(self):
        now = time.monotonic()
        if now < self._next_try:
            return False
        self._next_try = now + self.retry_s
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(self.path)
            s.setblocking(False)
            s.send((json.dumps({"type": "hello", "api": API, "name": self.name}) + "\n").encode())
        except OSError:
            s.close()
            return False
        self.sock = s
        self.connects += 1
        return True

    def _poll_replies(self):
        try:
            data = self.sock.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self.close()
            return
        for line in data.splitlines():
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("type") == "error":
                self.errors = (self.errors + [m])[-20:]
                self.errors_total += 1

    def send(self, msg):
        """Send one message (a dict with "type": "gnss" or "motion"). Returns False when it was dropped."""
        if "ts_mono_ns" not in msg:
            msg["ts_mono_ns"] = time.monotonic_ns()
        if self.sock is None and not self._connect():
            self.dropped += 1
            return False
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        try:
            n = self.sock.send(data)
        except BlockingIOError:              # mapd is not reading: drop rather than block the car
            self.dropped += 1
            return False
        except OSError:
            self.close()
            self.dropped += 1
            return False
        if n != len(data):                   # half a line in the stream: start over on a new connection
            self.close()
            self.dropped += 1
            return False
        self.sent += 1
        self._poll_replies()
        return True

    def gnss(self, lat, lon, hacc_m=None, speed_mps=None, course_deg=None, fix="3d", ts_mono_ns=None, **extra):
        m = {"type": "gnss", "fix": fix, "lat": lat, "lon": lon}
        for k, v in (("hacc_m", hacc_m), ("speed_mps", speed_mps), ("course_deg", course_deg),
                     ("ts_mono_ns", ts_mono_ns)):
            if v is not None:
                m[k] = v
        m.update(extra)
        return self.send(m)

    def no_fix(self, ts_mono_ns=None, **extra):
        m = {"type": "gnss", "fix": "none"}
        if ts_mono_ns is not None:
            m["ts_mono_ns"] = ts_mono_ns
        m.update(extra)
        return self.send(m)

    def motion(self, speed_mps, yaw_rate_rps=None, steering_wheel_deg=None, wheel_speeds_mps=None, gear=None,
               reverse=None, ts_mono_ns=None, **extra):
        m = {"type": "motion", "speed_mps": speed_mps}
        for k, v in (("yaw_rate_rps", yaw_rate_rps), ("steering_wheel_deg", steering_wheel_deg),
                     ("wheel_speeds_mps", wheel_speeds_mps), ("gear", gear), ("reverse", reverse),
                     ("ts_mono_ns", ts_mono_ns)):
            if v is not None:
                m[k] = v
        m.update(extra)
        return self.send(m)

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
