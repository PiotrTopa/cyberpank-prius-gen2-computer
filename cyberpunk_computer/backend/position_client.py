"""Client of the roadsense position input API v1 (``mapd``'s ``position.sock``).

Origin: ``PositionFeed`` is copied from roadsense ``tools/position_client.py``
(github.com/PiotrTopa/roadsense, MIT licence, copied 2026-11-15; contract:
roadsense docs/ARCHITECTURE.md "mapd" -> "Position input API v1"). Nothing
is imported from roadsense. Changes against the reference client:

* a short (partial) ``send`` on the non-blocking socket closes the connection
  and counts the message as dropped. The reference client counts it as sent
  and leaves half a JSON line in the stream, which corrupts the next message;
  mapd sees the connection end instead and drops the partial line.
* the hello carries ``provides`` (what this feeder will send) and the reply's
  ``accepts`` is forgotten on every reconnect, so the feeder decides per
  connection whether the roadsense at the other end takes the vehicle input
  API 1.1 messages (``controls``, ``body``, ``vehicle``, ``objects``;
  roadsense docs/VEHICLE_API.md, 2026-11-17) or only v1.
* ``connected`` property and ``errors_total`` counter for the status mirror.
* the retry period after a failed connect is ``retry_s`` (default 2 s here).
* mapd's ``hello`` reply is kept (``server``, ``accepts``: None until it has
  been read on this connection), so a caller can check that mapd accepts
  ``reset`` (listed since roadsense 2026-11-15) before sending it; ``poll()``
  reads pending replies without sending. ``reset()`` as in the reference
  client (roadsense f51b678).

* ``rundir`` may name several directories separated by ``:`` (lane L44,
  2026-11-20): the connect tries ``<dir>/position.sock`` of each in order and
  keeps the first that accepts. The default ``/run/chris:/run/roadsense``
  reaches chris-integrator (the vehicle input API's home since L44; it serves
  ``position.sock`` as an alias of its ``feed.sock``) and, when that is not
  there (an old roadsense after a rollback), legacy mapd. ``path`` is the
  socket of the current connection (the first candidate while disconnected).

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
DEFAULT_RUNDIR = "/run/chris:/run/roadsense"     # chris-integrator first, legacy mapd as the fallback


class PositionFeed:
    def __init__(self, rundir=DEFAULT_RUNDIR, name="feeder", retry_s=2.0, provides=None):
        self.paths = [os.path.join(d, "position.sock") for d in str(rundir).split(":") if d.strip()]
        if not self.paths:
            self.paths = [os.path.join(d, "position.sock") for d in DEFAULT_RUNDIR.split(":")]
        self.path = self.paths[0]
        self.name, self.retry_s = name, retry_s
        self.provides = provides         # what this feeder will send, declared in the hello (vehicle input API 1.1)
        self.sock = None
        self._next_try = 0.0
        self.sent = self.dropped = 0
        self.connects = 0
        self.errors = []                 # error replies from mapd (most recent last, at most 20)
        self.errors_total = 0
        self.server = None               # mapd's hello reply on this connection
        self.accepts = None              # its "accepts" list; None until the reply was read

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def _connect(self):
        now = time.monotonic()
        if now < self._next_try:
            return False
        self._next_try = now + self.retry_s
        s = None
        for path in self.paths:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect(path)
                s.setblocking(False)
                hello = {"type": "hello", "api": API, "name": self.name}
                if self.provides:
                    hello["provides"] = self.provides
                s.send((json.dumps(hello) + "\n").encode())
            except OSError:
                s.close()
                s = None
                continue
            self.path = path
            break
        if s is None:
            return False
        self.sock = s
        self.server = self.accepts = None    # of this connection: unknown until its hello reply is read
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
            if m.get("type") == "hello":
                self.server = m
                acc = m.get("accepts")
                self.accepts = [str(a) for a in acc] if isinstance(acc, list) else []
            elif m.get("type") == "error":
                self.errors = (self.errors + [m])[-20:]
                self.errors_total += 1

    def poll(self):
        """Read pending replies (hello, errors) without sending anything."""
        if self.sock is not None:
            self._poll_replies()

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

    def reset(self, reason=None, ts_mono_ns=None):
        """Make mapd forget the position (filter, fix, match): the map is `no_source` until a new fix
        arrives. Only for a mapd whose hello lists "reset" in `accepts` (the caller checks)."""
        m = {"type": "reset"}
        if reason is not None:
            m["reason"] = str(reason)
        if ts_mono_ns is not None:
            m["ts_mono_ns"] = ts_mono_ns
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
        self.server = None
        self.accepts = None
