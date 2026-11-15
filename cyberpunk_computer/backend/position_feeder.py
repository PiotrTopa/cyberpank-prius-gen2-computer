"""
Prius position feeder: GNSS fixes and vehicle motion -> roadsense ``mapd``.

roadsense owns all position filtering, dead reckoning and map matching
(roadsense docs/ARCHITECTURE.md "mapd", position input API v1). This module
only delivers raw inputs to ``<rundir>/position.sock``:

    panda reader thread ──on_frame──▶ CanMotionTap (latest 0x0B4/0x120/0x0B1/0x0B3/0x025)
    head unit ──POST /api/v1/position/gnss──▶ HeadunitGnssSource (queue)
                                                   │
                         PositionFeeder thread (motion_hz) ──PositionFeed──▶ position.sock

Rules:

* Off unless ``BACKEND_POSITION_FEEDER=1``. While on, it only runs while the
  roadsense stack is meant to be up (backend loop -> :meth:`PositionFeeder.set_active`);
  with mapd absent every message is dropped without a log line and a
  reconnect is tried at most every 2 s (a failed ``connect`` on a missing
  socket path is a few microseconds).
* Motion comes from broadcast CAN only (panda, SAFETY_SILENT). BEAN is never
  used: its copies lag the bus by 45..110 ms and more (docs/prius_can.md 0x120).
  ``ts_mono_ns`` is the panda transfer's CLOCK_MONOTONIC receipt time of the
  0x0B4 frame the speed came from (the bus-to-transfer delay is a few ms, at
  most the 20 ms read timeout), never the send time.
* Nothing stale goes out: no motion while the newest 0x0B4 is older than
  ``stale_s`` (car off, CAN dark, panda gone); a yaw field rides along only
  when its frames are within ``pair_s`` of the speed frame.
* GNSS sources: ``headunit`` (real; pushed by the head unit, see
  docs/POSITION_FEEDER.md for the exact contract), ``synthetic`` (test/demo
  only, never the default, suspends itself while the real car moves),
  ``none`` (motion only). Without a usable fix the feeder sends
  ``{"type":"gnss","fix":"none"}`` (on the change and every ``none_repeat_s``)
  so mapd stays without position and roadsense runs on vision alone.

Signals (decode, verification state, sign conventions): docs/POSITION_FEEDER.md
section 3. Summary:

* ``speed_mps``  = CAN 0x0B4 (256F + G) x 0.01 km/h / 3.6, the true vehicle
  speed (verified on this car; the speedometer reads 12 % higher).
* ``gear``       = CAN 0x120 byte F low nibble 0 P, 1 R, 2 N, 3 D, 4 B
  (P/R/N/D verified, B not observed).
* ``wheel_speeds_mps`` [FL, FR, RL, RR] = CAN 0x0B1 (front) / 0x0B3 (rear),
  right = bytes A,B, left = bytes C,D, x 0.01 km/h (unit verified against
  0x0B4 on the 2026-11-13 drive; left/right labels third-party, consistent
  with the steering sign).
* ``steering_wheel_deg`` = CAN 0x025 12-bit signed (low nibble of A, B) x 1.5,
  positive = left (scale from the full-lock range; sign third-party Toyota
  convention, consistent with the wheel-speed difference). Decoder shared with
  the state's ``dynamics.steering_angle`` (``comm/can_decoder.py``).
* CAN 0x03A is NOT a yaw rate (correlation 0.01 with the wheel-speed yaw on
  the 2026-11-13 drive); no yaw-rate sensor is decoded.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..comm.can_decoder import decode_steering_deg  # noqa: F401 (re-exported)
from .position_client import PositionFeed

logger = logging.getLogger(__name__)

SOURCES = ("headunit", "synthetic", "none")
YAW_INPUTS = ("wheels", "steer")
# Default: wheel speeds only. mapd prefers them anyway; steering_wheel_deg is
# used by mapd only without wheel speeds, and its kinematic model (ratio 19.1,
# wheelbase 2.70 m) over-predicts this car's yaw by ~10 % (all speeds) to ~50 %
# (above 8 m/s) on the 2026-11-13 drive, so it is opt-in (BACKEND_POSITION_YAW).
DEFAULT_YAW_INPUTS = ("wheels",)
FIXES = ("none", "2d", "3d", "dgps", "rtk", "dr")
GEARS = ("P", "R", "N", "D", "B")

MOTION_TAG = "prius-can"
SYNTHETIC_TAG = "synthetic"
HEADUNIT_TAG = "headunit"

DEFAULT_ROUTE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                             "position_route_opole.json")

# Prius NHW20 nominal geometry (the same nominal values mapd uses by default).
TRACK_M = 1.48
WHEELBASE_M = 2.70
STEERING_RATIO = 19.1


@dataclass
class PositionFeederConfig:
    """Knobs of the position feeder (BACKEND_POSITION_*, backend.__main__)."""
    enabled: bool = False
    source: str = "headunit"               # headunit | synthetic | none
    rundir: str = "/run/roadsense"         # mapd's position.sock lives here
    motion_hz: float = 10.0                # motion messages per second (contract 5..50, 10..20 recommended)
    yaw_inputs: Tuple[str, ...] = DEFAULT_YAW_INPUTS   # yaw fields with motion: wheels, steer
    stale_s: float = 0.3                   # 0x0B4 older than this -> no motion (CAN dark)
    pair_s: float = 0.1                    # yaw frames must be this close to the speed frame
    gear_stale_s: float = 1.0              # 0x120 older than this -> gear left out
    fix_timeout_s: float = 3.0             # no fix for this long -> "no fix" (mapd's own fix_timeout_s is 3 s)
    none_repeat_s: float = 5.0             # repeat fix "none" while there is no fix
    max_fix_age_s: float = 2.5             # head-unit fixes older than this on arrival are rejected
    link_delay_s: float = 0.02             # head unit -> backend HTTP transit, subtracted from the fix time
    max_batch: int = 20                    # fixes per POST
    status_s: float = 2.0                  # status mirror cadence (on change)
    retry_s: float = 2.0                   # mapd reconnect period
    # synthetic source (test/demo only)
    synthetic_route: str = ""              # route file; "" = built-in Opole loop
    synthetic_dropouts: Tuple[Tuple[float, float], ...] = ()   # (t0, t1) s since start: fix lost
    synthetic_seed: int = 1
    synthetic_gnss_hz: float = 1.0
    synthetic_gnss_sigma_m: float = 2.5
    synthetic_hacc_m: float = 5.0
    synthetic_start_m: float = 0.0         # start this far along the route
    synthetic_moving_mps: float = 0.5      # real CAN speed above this suspends the synthetic drive


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


# ── CAN decode (pure; docs/POSITION_FEEDER.md section 3) ─────────────────────

def decode_speed_kmh(data: bytes) -> Optional[float]:
    """0x0B4: (256F + G) x 0.01 km/h, the true vehicle speed."""
    if len(data) < 7:
        return None
    return ((data[5] << 8) | data[6]) * 0.01


def decode_gear(data: bytes) -> Optional[str]:
    """0x120: byte F low nibble 0 P, 1 R, 2 N, 3 D, 4 B; anything else None."""
    if len(data) < 6:
        return None
    g = data[5] & 0x0F
    return GEARS[g] if g < len(GEARS) else None


def decode_wheel_pair_kmh(data: bytes) -> Optional[Tuple[float, float]]:
    """0x0B1 / 0x0B3: (right, left) = (256A + B, 256C + D) x 0.01 km/h."""
    if len(data) < 4:
        return None
    return ((data[0] << 8) | data[1]) * 0.01, ((data[2] << 8) | data[3]) * 0.01


class CanMotionTap:
    """Latest motion-relevant broadcast frames with their receipt times.

    ``on_frame(can_id, data, bus, t_wall, t_mono)`` has the panda callback's
    signature and runs on the panda reader thread: a set lookup for every
    other id, a few integer operations for the five used ones. Each field is
    replaced by one tuple assignment, so readers on other threads never see a
    torn value.
    """

    IDS = frozenset((0x0B4, 0x120, 0x0B1, 0x0B3, 0x025))

    def __init__(self) -> None:
        self.speed: Optional[Tuple[float, float]] = None          # (km/h, t_mono)
        self.gear: Optional[Tuple[str, float]] = None
        self.front: Optional[Tuple[float, float, float]] = None   # (right, left km/h, t_mono)
        self.rear: Optional[Tuple[float, float, float]] = None
        self.steer: Optional[Tuple[float, float]] = None          # (deg, t_mono)
        self.frames = 0

    def on_frame(self, can_id: int, data: bytes, bus: int = 0,
                 t_wall: Optional[float] = None, t_mono: Optional[float] = None) -> None:
        if can_id not in self.IDS:
            return
        t = t_mono if t_mono is not None else time.monotonic()
        if can_id == 0x0B4:
            v = decode_speed_kmh(data)
            if v is not None and v < 300.0:
                self.speed = (v, t)
        elif can_id == 0x0B3:
            p = decode_wheel_pair_kmh(data)
            if p is not None:
                self.rear = (p[0], p[1], t)
        elif can_id == 0x0B1:
            p = decode_wheel_pair_kmh(data)
            if p is not None:
                self.front = (p[0], p[1], t)
        elif can_id == 0x025:
            s = decode_steering_deg(data)
            if s is not None:
                self.steer = (s, t)
        else:                               # 0x120
            g = decode_gear(data)
            if g is not None:
                self.gear = (g, t)
        self.frames += 1

    def speed_mps(self, now: float, stale_s: float) -> Optional[float]:
        sp = self.speed
        if sp is None or now - sp[1] > stale_s:
            return None
        return sp[0] / 3.6

    def sample(self, now: float, cfg: PositionFeederConfig) -> Optional[Dict[str, Any]]:
        """The motion message for the newest speed frame, or None (stale / none)."""
        sp = self.speed
        if sp is None or now - sp[1] > cfg.stale_s:
            return None
        kmh, t = sp
        m: Dict[str, Any] = {"type": "motion", "ts_mono_ns": int(t * 1e9),
                             "speed_mps": round(kmh / 3.6, 3)}
        g = self.gear
        if g is not None and now - g[1] <= cfg.gear_stale_s:
            m["gear"] = g[0]
        if "wheels" in cfg.yaw_inputs:
            fr, rr = self.front, self.rear
            if fr is not None and rr is not None and abs(fr[2] - t) <= cfg.pair_s \
                    and abs(rr[2] - t) <= cfg.pair_s:
                m["wheel_speeds_mps"] = [round(fr[1] / 3.6, 3), round(fr[0] / 3.6, 3),
                                         round(rr[1] / 3.6, 3), round(rr[0] / 3.6, 3)]
        if "steer" in cfg.yaw_inputs:
            st = self.steer
            if st is not None and abs(st[1] - t) <= cfg.pair_s:
                m["steering_wheel_deg"] = round(st[0], 1)
        m["source"] = MOTION_TAG
        return m


# ── GNSS sources ─────────────────────────────────────────────────────────────

class GnssSource:
    """Interface of a GNSS source.

    ``drain(now)`` returns the position-API ``gnss`` messages that became
    available since the last call (``ts_mono_ns`` set to the fix time);
    ``has_fix(now)`` says whether a usable fix exists now (the feeder sends
    ``fix: none`` when it does not); ``status(now)`` is the source's part of
    the feeder status. ``provides_motion`` sources also produce the motion
    (``motion(now)``) instead of the car's CAN.
    """

    name = "none"
    tag = ""
    reports_fix = False
    provides_motion = False

    def drain(self, now: float) -> List[Dict[str, Any]]:
        return []

    def has_fix(self, now: float) -> bool:
        return False

    def motion(self, now: float) -> Optional[Dict[str, Any]]:
        return None

    def status(self, now: float) -> Dict[str, Any]:
        return {}


class NoGnssSource(GnssSource):
    """Motion only: no ``gnss`` message at all (mapd never makes a position)."""
    name = "none"


class HeadunitGnssSource(GnssSource):
    """Raw fixes pushed by the Android head unit (``POST /api/v1/position/gnss``).

    Thread-safe: :meth:`ingest` runs on the API thread, :meth:`drain` on the
    feeder thread. Fix time -> phone CLOCK_MONOTONIC, best first:

    1. ``age_ms`` = the head unit's ``SystemClock.elapsedRealtimeNanos() -
       Location.getElapsedRealtimeNanos()`` at send time (needs no clock sync):
       t = arrival - age - link_delay;
    2. ``utc_ms`` (``Location.getTime()``) against this phone's wall clock, used
       only when the difference is plausible (-0.5 .. max_fix_age_s);
    3. otherwise the arrival time minus link_delay (counted as ``time_arrival``).
    """

    name = "headunit"
    tag = HEADUNIT_TAG
    reports_fix = True

    def __init__(self, cfg: PositionFeederConfig, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time):
        self.cfg = cfg
        self._clock, self._wall = clock, wall
        self._lock = threading.Lock()
        self._queue: deque = deque(maxlen=16)
        self._last_t: Optional[float] = None       # mono time of the newest accepted fix
        self._lost_t: Optional[float] = None       # mono arrival of the newest explicit fix "none"
        self._last_meta: Dict[str, Any] = {}
        self.counts = {"received": 0, "accepted": 0, "rejected": 0, "none": 0,
                       "time_age": 0, "time_utc": 0, "time_arrival": 0}
        self.last_reject: Optional[str] = None

    # -- API thread --
    def ingest(self, payload: Any) -> Dict[str, Any]:
        now, wall = self._clock(), self._wall()
        items = payload if isinstance(payload, list) else [payload]
        if len(items) > self.cfg.max_batch:
            raise ValueError("at most %d fixes per request" % self.cfg.max_batch)
        accepted, rejected = 0, []
        with self._lock:
            for i, item in enumerate(items):
                self.counts["received"] += 1
                try:
                    self._ingest_one(item, now, wall)
                    accepted += 1
                    self.counts["accepted"] += 1
                except ValueError as exc:
                    self.counts["rejected"] += 1
                    self.last_reject = str(exc)
                    rejected.append({"index": i, "reason": str(exc)})
            fix = self._has_fix_locked(now)
        return {"accepted": accepted, "rejected": rejected, "has_fix": fix}

    def _ingest_one(self, g: Any, now: float, wall: float) -> None:
        if not isinstance(g, dict):
            raise ValueError("a fix must be a JSON object")
        fix = g.get("fix", "3d")
        if fix not in FIXES:
            raise ValueError("fix must be one of " + " ".join(FIXES))
        if fix == "none":
            self._lost_t = now
            self.counts["none"] += 1
            return
        lat, lon = _num(g.get("lat")), _num(g.get("lon"))
        if lat is None or lon is None or not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
            raise ValueError("lat/lon missing or out of range")
        if abs(lat) < 1e-6 and abs(lon) < 1e-6:
            raise ValueError("lat/lon 0,0 (no fix)")
        msg: Dict[str, Any] = {"type": "gnss", "fix": fix, "lat": round(lat, 7), "lon": round(lon, 7)}
        for key, lo, hi in (("hacc_m", 0.0, 1e5), ("speed_mps", 0.0, 120.0), ("speed_acc_mps", 0.0, 100.0),
                            ("course_deg", 0.0, 360.0), ("course_acc_deg", 0.0, 360.0),
                            ("alt_m", -1000.0, 20000.0), ("vacc_m", 0.0, 1e5), ("hdop", 0.0, 100.0)):
            if key in g and g[key] is not None:
                v = _num(g[key])
                if v is None or not lo <= v <= hi:
                    raise ValueError("%s out of range" % key)
                if key == "course_deg" and v >= 360.0:
                    v = 0.0
                msg[key] = round(v, 3)
        sats = g.get("sats")
        if sats is not None:
            if isinstance(sats, bool) or not isinstance(sats, int) or not 0 <= sats <= 200:
                raise ValueError("sats must be an integer 0..200")
            msg["sats"] = sats
        utc_ms = g.get("utc_ms")
        if utc_ms is not None:
            if isinstance(utc_ms, bool) or not isinstance(utc_ms, int) or utc_ms < 1_500_000_000_000:
                raise ValueError("utc_ms must be Unix milliseconds (integer)")
            msg["utc_ms"] = utc_ms
        age_ms = g.get("age_ms")
        if age_ms is not None:
            a = _num(age_ms)
            if a is None or a < 0:
                raise ValueError("age_ms must be a number >= 0")
            t = now - a / 1000.0 - self.cfg.link_delay_s
            self.counts["time_age"] += 1
        elif utc_ms is not None and -0.5 <= wall - utc_ms / 1000.0 <= self.cfg.max_fix_age_s:
            t = now - max(0.0, wall - utc_ms / 1000.0)
            self.counts["time_utc"] += 1
        else:
            t = now - self.cfg.link_delay_s
            self.counts["time_arrival"] += 1
        if now - t > self.cfg.max_fix_age_s:
            raise ValueError("fix too old (%.1f s)" % (now - t))
        if self._last_t is not None and t <= self._last_t:
            raise ValueError("fix not newer than the previous one")
        msg["ts_mono_ns"] = int(t * 1e9)
        msg["source"] = HEADUNIT_TAG
        self._queue.append(msg)
        self._last_t = t
        self._last_meta = {"fix": fix, "hacc_m": msg.get("hacc_m"), "sats": msg.get("sats")}

    # -- feeder thread --
    def drain(self, now: float) -> List[Dict[str, Any]]:
        with self._lock:
            out = list(self._queue)
            self._queue.clear()
        return out

    def _has_fix_locked(self, now: float) -> bool:
        if self._last_t is None or now - self._last_t > self.cfg.fix_timeout_s:
            return False
        return self._lost_t is None or self._lost_t < self._last_t

    def has_fix(self, now: float) -> bool:
        with self._lock:
            return self._has_fix_locked(now)

    def status(self, now: float) -> Dict[str, Any]:
        with self._lock:
            age = None if self._last_t is None else round(now - self._last_t, 1)
            st = {"has_fix": self._has_fix_locked(now), "last_fix_age_s": age,
                  "last_reject": self.last_reject}
            st.update(self.counts)
            st.update({k: v for k, v in self._last_meta.items() if v is not None})
        return st


# ── synthetic source (test / demo only) ──────────────────────────────────────

def load_route(path: str) -> List[Tuple[float, float, float]]:
    """Route file: JSON ``{"points": [[lat, lon, v_target_mps], ...]}`` or a
    bare list of ``[lat, lon]`` / ``[lat, lon, v]`` (v defaults to 12.5 m/s)."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    pts = data.get("points") if isinstance(data, dict) else data
    out = []
    for p in pts or ():
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            continue
        lat, lon = _num(p[0]), _num(p[1])
        v = _num(p[2]) if len(p) > 2 else 12.5
        if lat is None or lon is None:
            continue
        out.append((lat, lon, v if v and v > 0 else 12.5))
    if len(out) < 2:
        raise ValueError("route %s has fewer than 2 points" % path)
    return out


class SyntheticDrive:
    """A vehicle driving a polyline route, consistent in position, heading,
    speed and yaw. Closed routes (last point within 20 m of the first) loop;
    open ones are driven there and back. Stdlib only."""

    A_LAT = 2.0        # m/s2, curve speed limit
    ACC = 1.2          # m/s2
    DEC = 2.0          # m/s2

    def __init__(self, points: Sequence[Tuple[float, float, float]], start_m: float = 0.0):
        lat0, lon0 = points[0][0], points[0][1]
        self.lat0, self.lon0 = lat0, lon0
        self.mlat = 111132.954
        self.mlon = 111319.49 * math.cos(math.radians(lat0))
        pts = list(points)
        x0, y0 = self._xy(pts[0][0], pts[0][1])
        x1, y1 = self._xy(pts[-1][0], pts[-1][1])
        self.closed = math.hypot(x1 - x0, y1 - y0) < 20.0
        if not self.closed:
            pts = pts + pts[-2::-1]
        xs, ys, vt = [], [], []
        for lat, lon, v in pts:
            x, y = self._xy(lat, lon)
            if xs and math.hypot(x - xs[-1], y - ys[-1]) < 0.05:
                continue
            xs.append(x)
            ys.append(y)
            vt.append(v)
        self.x, self.y = xs, ys
        s = [0.0]
        for i in range(1, len(xs)):
            s.append(s[-1] + math.hypot(xs[i] - xs[i - 1], ys[i] - ys[i - 1]))
        self.s = s
        self.length = s[-1]
        # speed profile on a 1 m grid: target, curve limit, then accel / decel limits
        n = int(self.length) + 1
        prof = []
        j = 0
        for k in range(n):
            while j < len(s) - 2 and s[j + 1] <= k:
                j += 1
            kap = max(abs(self._curvature(float(k) + o)) for o in (-1.0, 0.0, 1.0))
            v = vt[j]
            if kap > 1e-4:
                v = min(v, math.sqrt(self.A_LAT / kap))
            prof.append(max(2.0, v))
        for _ in range(2 if self.closed else 1):
            for k in range(1, n):                   # accel limit forward
                prof[k] = min(prof[k], math.sqrt(prof[k - 1] ** 2 + 2 * self.ACC))
            for k in range(n - 2, -1, -1):          # decel limit backward
                prof[k] = min(prof[k], math.sqrt(prof[k + 1] ** 2 + 2 * self.DEC))
            if self.closed:
                prof[0] = min(prof[0], prof[-1])
                prof[-1] = prof[0]
        self.profile = prof
        self.pos_s = start_m % self.length
        self.v = 0.0
        self.heading = self.heading_at(self.pos_s)
        self.yaw_ccw = 0.0

    def _xy(self, lat: float, lon: float) -> Tuple[float, float]:
        return (lon - self.lon0) * self.mlon, (lat - self.lat0) * self.mlat

    def _wrap_s(self, s: float) -> float:
        return s % self.length

    def point(self, s: float) -> Tuple[float, float]:
        s = self._wrap_s(s)
        lo, hi = 0, len(self.s) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.s[mid] <= s:
                lo = mid
            else:
                hi = mid
        seg = self.s[hi] - self.s[lo]
        t = (s - self.s[lo]) / seg if seg > 0 else 0.0
        return (self.x[lo] + t * (self.x[hi] - self.x[lo]), self.y[lo] + t * (self.y[hi] - self.y[lo]))

    def heading_at(self, s: float) -> float:
        """Smoothed heading, radians clockwise from north."""
        xa, ya = self.point(s - 4.0)
        xb, yb = self.point(s + 4.0)
        return math.atan2(xb - xa, yb - ya)

    def _curvature(self, s: float) -> float:
        """1/m, positive = turning left (counter-clockwise)."""
        d = self.heading_at(s + 1.5) - self.heading_at(s - 1.5)
        d = (d + math.pi) % (2 * math.pi) - math.pi
        return -d / 3.0

    def latlon(self) -> Tuple[float, float]:
        x, y = self.point(self.pos_s)
        return self.lat0 + y / self.mlat, self.lon0 + x / self.mlon

    def step(self, dt: float) -> None:
        """Advance by dt seconds (sub-stepped at 50 ms)."""
        h0 = self.heading
        left = max(0.0, dt)
        while left > 1e-9:
            h = min(0.05, left)
            k = int(self.pos_s)
            vt = self.profile[min(k, len(self.profile) - 1)]
            # the profile is already decel-limited along the route: follow it down
            # exactly, accelerate towards it at ACC
            self.v = max(0.0, min(self.v + self.ACC * h, vt))
            self.pos_s = self._wrap_s(self.pos_s + self.v * h)
            left -= h
        self.heading = self.heading_at(self.pos_s)
        d = (self.heading - h0 + math.pi) % (2 * math.pi) - math.pi
        self.yaw_ccw = -d / dt if dt > 0 else 0.0


class SyntheticGnssSource(GnssSource):
    """A synthetic drive over a route: GNSS (1 Hz, correlated noise) and the
    matching motion (speed, gear D, wheel speeds, steering angle). Every
    message is tagged ``source: "synthetic"``, so mapd's ``map.input`` and the
    backend status show it. Test/demo only: selected by name, never a
    default; the feeder suspends it while the real car moves."""

    name = "synthetic"
    tag = SYNTHETIC_TAG
    reports_fix = True
    provides_motion = True

    def __init__(self, cfg: PositionFeederConfig, wall: Callable[[], float] = time.time):
        self.cfg = cfg
        self._wall = wall
        path = cfg.synthetic_route or DEFAULT_ROUTE
        self.route_file = path
        self.drive = SyntheticDrive(load_route(path), start_m=cfg.synthetic_start_m)
        self._rnd = random.Random(cfg.synthetic_seed)
        self._t0: Optional[float] = None
        self._t_last: Optional[float] = None
        self._next_fix = 0.0
        self._last_fix_t: Optional[float] = None
        self._noise = [0.0, 0.0]
        self.fixes = 0
        self.laps = 0

    def elapsed(self, now: float) -> float:
        return 0.0 if self._t0 is None else now - self._t0

    def _in_dropout(self, now: float) -> bool:
        e = self.elapsed(now)
        return any(t0 <= e < t1 for t0, t1 in self.cfg.synthetic_dropouts)

    def advance(self, now: float) -> None:
        if self._t0 is None:
            self._t0 = self._t_last = now
            self._next_fix = now
            return
        dt = now - self._t_last
        if dt <= 0:
            return
        if dt > 1.0:                 # paused (suspended / inactive): resume at standstill, no jump
            dt = 0.0
            self.drive.v = 0.0
        before = self.drive.pos_s
        if dt > 0:
            self.drive.step(dt)
        if self.drive.pos_s < before - 1.0:
            self.laps += 1
        self._t_last = now

    def motion(self, now: float) -> Optional[Dict[str, Any]]:
        self.advance(now)
        d = self.drive
        v, yaw = d.v, d.yaw_ccw
        half = yaw * TRACK_M / 2.0
        left, right = max(0.0, v - half), max(0.0, v + half)
        kappa = yaw / v if v > 0.5 else 0.0
        steer = math.degrees(math.atan(WHEELBASE_M * kappa)) * STEERING_RATIO
        return {"type": "motion", "ts_mono_ns": int(now * 1e9), "speed_mps": round(v, 3), "gear": "D",
                "wheel_speeds_mps": [round(left, 3), round(right, 3), round(left, 3), round(right, 3)],
                "steering_wheel_deg": round(steer, 1), "source": SYNTHETIC_TAG}

    def drain(self, now: float) -> List[Dict[str, Any]]:
        self.advance(now)
        if now < self._next_fix:
            return []
        period = 1.0 / max(0.1, self.cfg.synthetic_gnss_hz)
        self._next_fix = max(self._next_fix + period, now)
        # correlated noise (first-order Gauss-Markov, tau 20 s) + white 0.5 m
        a = math.exp(-period / 20.0)
        sig = self.cfg.synthetic_gnss_sigma_m
        for i in (0, 1):
            self._noise[i] = a * self._noise[i] + math.sqrt(1 - a * a) * self._rnd.gauss(0.0, sig)
        if self._in_dropout(now):
            return []
        d = self.drive
        lat, lon = d.latlon()
        e = self._noise[0] + self._rnd.gauss(0.0, 0.5)
        n = self._noise[1] + self._rnd.gauss(0.0, 0.5)
        lat += n / d.mlat
        lon += e / d.mlon
        msg: Dict[str, Any] = {"type": "gnss", "ts_mono_ns": int(now * 1e9), "fix": "3d",
                               "lat": round(lat, 7), "lon": round(lon, 7),
                               "hacc_m": self.cfg.synthetic_hacc_m,
                               "speed_mps": round(max(0.0, d.v + self._rnd.gauss(0.0, 0.1)), 2),
                               "sats": 12, "hdop": 0.9, "utc_ms": int(self._wall() * 1000),
                               "source": SYNTHETIC_TAG}
        if d.v >= 1.0:
            msg["course_deg"] = round(math.degrees(d.heading) % 360.0, 1)
        self.fixes += 1
        self._last_fix_t = now
        return [msg]

    def has_fix(self, now: float) -> bool:
        return (self._last_fix_t is not None and now - self._last_fix_t <= self.cfg.fix_timeout_s
                and not self._in_dropout(now))

    def status(self, now: float) -> Dict[str, Any]:
        age = None if self._last_fix_t is None else round(now - self._last_fix_t, 1)
        return {"has_fix": self.has_fix(now), "last_fix_age_s": age, "fixes": self.fixes,
                "route": os.path.basename(self.route_file), "route_m": round(self.drive.length),
                "along_m": round(self.drive.pos_s), "laps": self.laps,
                "speed_mps": round(self.drive.v, 1), "dropout": self._in_dropout(now)}


def make_source(cfg: PositionFeederConfig) -> GnssSource:
    if cfg.source == "headunit":
        return HeadunitGnssSource(cfg)
    if cfg.source == "synthetic":
        return SyntheticGnssSource(cfg)
    if cfg.source == "none":
        return NoGnssSource()
    raise ValueError("position source must be one of " + ", ".join(SOURCES))


# ── the feeder ───────────────────────────────────────────────────────────────

class PositionFeeder:
    """Sends motion and GNSS to mapd from its own thread while active.

    :meth:`step` does one cycle and is what the thread calls at ``motion_hz``
    (tests call it directly with a fake clock). ``publish_status(dict)`` is
    called with the status on change, at most every ``status_s``.
    """

    def __init__(self, cfg: PositionFeederConfig, source: GnssSource,
                 tap: Optional[CanMotionTap] = None, feed: Optional[PositionFeed] = None,
                 clock: Callable[[], float] = time.monotonic,
                 publish_status: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.cfg = cfg
        self.source = source
        self.tap = tap
        self.feed = feed or PositionFeed(cfg.rundir, name="prius-backend", retry_s=cfg.retry_s)
        self._clock = clock
        self._publish_status = publish_status
        self._active = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._fix_ok: Optional[bool] = None
        self._last_none = -1e9
        self._last_motion_ts = 0
        self._last_status_t = -1e9
        self._last_status: Optional[Dict[str, Any]] = None
        self._was_connected = False
        self.suspended: Optional[str] = None
        self.counts = {"motion": 0, "gnss": 0, "none": 0}
        self.yaw_input: Optional[str] = None
        if source.name == "synthetic":
            logger.warning("position feeder: SYNTHETIC GNSS + motion source (test/demo only; "
                           "tagged 'synthetic', suspended while the real car moves)")

    # ── control ──────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._active

    def set_active(self, on: bool) -> None:
        on = bool(on)
        if on != self._active:
            logger.info("position feeder %s (source %s)", "active" if on else "idle", self.source.name)
            self._active = on
            self._wake.set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="position-feeder", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self.feed.close()

    # ── one cycle ────────────────────────────────────────────────────────

    def _send(self, msg: Dict[str, Any], kind: str) -> bool:
        ok = self.feed.send(msg)
        if ok:
            self.counts[kind] += 1
        return ok

    def _motion(self, now: float) -> None:
        src = self.source
        if src.provides_motion:
            real = self.tap.speed_mps(now, self.cfg.stale_s) if self.tap is not None else None
            if real is not None and real > self.cfg.synthetic_moving_mps:
                if self.suspended is None:
                    logger.warning("position feeder: synthetic drive suspended, the real car moves "
                                   "(%.1f m/s on CAN)", real)
                self.suspended = "car_moving"
                return
            if self.suspended is not None:
                logger.info("position feeder: synthetic drive resumed")
                self.suspended = None
            m = src.motion(now)
        elif self.tap is not None:
            m = self.tap.sample(now, self.cfg)
        else:
            m = None
        if m is None:
            self.yaw_input = None
            return
        if m["ts_mono_ns"] <= self._last_motion_ts:       # no new speed frame since the last send
            return
        self._last_motion_ts = m["ts_mono_ns"]
        yaw = [k for k, f in (("wheels", "wheel_speeds_mps"), ("steer", "steering_wheel_deg")) if f in m]
        self.yaw_input = "+".join(yaw) or None
        self._send(m, "motion")

    def _gnss(self, now: float) -> None:
        src = self.source
        if self.suspended is not None:
            return
        for g in src.drain(now):
            self._send(g, "gnss")
        if not src.reports_fix:
            return
        ok = src.has_fix(now)
        if not ok and (self._fix_ok is not False or now - self._last_none >= self.cfg.none_repeat_s):
            self._last_none = now
            self._send({"type": "gnss", "fix": "none", "ts_mono_ns": int(now * 1e9),
                        "source": src.tag}, "none")
        if ok != self._fix_ok and self._fix_ok is not None:
            logger.info("position feeder: %s fix %s", src.name, "acquired" if ok else "lost")
        self._fix_ok = ok

    def step(self, now: Optional[float] = None) -> None:
        now = self._clock() if now is None else now
        self._motion(now)
        self._gnss(now)
        conn = self.feed.connected
        if conn != self._was_connected:
            logger.info("position feeder: %s %s", "connected to" if conn else "lost", self.feed.path)
            self._was_connected = conn
        self._status(now)

    # ── status ───────────────────────────────────────────────────────────

    def status(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = self._clock() if now is None else now
        f = self.feed
        motion: Dict[str, Any] = {"yaw_input": self.yaw_input}
        if self.tap is not None:
            sp = self.tap.speed
            motion["can_age_s"] = None if sp is None else round(now - sp[1], 1)
            motion["can_frames"] = self.tap.frames
        last_err = f.errors[-1].get("reason") if f.errors else None
        return {"enabled": True, "source": self.source.name, "synthetic": self.source.name == "synthetic",
                "active": self._active, "suspended": self.suspended,
                "connected": f.connected, "socket": f.path,
                "sent": f.sent, "dropped": f.dropped, "connects": f.connects,
                "motion_sent": self.counts["motion"], "gnss_sent": self.counts["gnss"],
                "none_sent": self.counts["none"], "errors": f.errors_total, "last_error": last_err,
                "motion": motion, "gnss": self.source.status(now)}

    def _status(self, now: float, force: bool = False) -> None:
        if self._publish_status is None:
            return
        if not force and now - self._last_status_t < self.cfg.status_s:
            return
        self._last_status_t = now
        st = self.status(now)
        if st != self._last_status:
            self._last_status = st
            try:
                self._publish_status(st)
            except Exception:  # noqa: BLE001 - never kill the feeder
                logger.exception("position feeder status publish failed")

    # ── thread ───────────────────────────────────────────────────────────

    def _run(self) -> None:
        period = 1.0 / max(1.0, min(50.0, self.cfg.motion_hz))
        nxt = self._clock()
        while not self._stop.is_set():
            if not self._active:
                if self.feed.connected:
                    self.feed.close()
                self._fix_ok = None
                self._status(self._clock(), force=True)
                self._wake.wait(1.0)
                self._wake.clear()
                nxt = self._clock()
                continue
            try:
                self.step()
            except Exception:  # noqa: BLE001 - never kill the feeder
                logger.exception("position feeder step failed")
            nxt += period
            now = self._clock()
            if nxt < now:
                nxt = now + period
            self._stop.wait(nxt - now)
        self.feed.close()
