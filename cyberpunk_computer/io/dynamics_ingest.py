"""
Broadcast-CAN vehicle dynamics and distance -> state (docs/prius_can.md §2,
decoded on the 2026-11-17 drive).

* **Yaw rate** (0x022, two redundant channels, ~0.0044 rad/s = 0.25 °/s per
  count, + = left; C that it is a yaw rate, scale P). The channels have
  different zero offsets (-2 and -7 counts at standstill on 2026-11-17), so
  each is re-estimated whenever the car stands still: speed 0 on 0x0B4 for
  :data:`STANDSTILL_SETTLE_S`, then an exponential mean of the raw counts.
  ``dynamics.yaw_rate_dps`` = mean of both channels minus their offsets.
* **Accelerations** (0x023, P): longitudinal and lateral m/s² straight from
  the decoder (they include road grade and camber; no offset is removed).
* **Distance** (0x230 A,B 16-bit counter, ~102.8 counts/m, P): differences
  of the counter (mod 2^16), each checked against the true speed; while
  0x230 is stale the true speed (0x0B4) is integrated instead.
  ``vehicle.distance_total_km`` runs since the backend started.
* **Own odometer** (:class:`OwnOdometer`): the meter's odometer (BEAN 52:3F)
  is frozen at 299,999 km on this car, so service intervals need our own:
  persisted km that grows with the distance above and never falls below
  52:3F.
* Flags: 0x03A E bit 4 accelerator pressed (P), 0x0B4 E bit 0 moving (P),
  0x038 B ICE power request (P).

Every value goes back to None (unknown) once its frame has been silent for
:data:`STALE_S` (car off, panda gone). The broadcast reader thread calls the
``on_*`` methods; the engine tick calls :meth:`expire`.

Read-only: the system informs the driver; nothing here touches the car.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..comm.can_decoder import (
    DISTANCE_COUNTS_PER_M, YAW_DEG_S_PER_COUNT, YAW_STANDSTILL_OFFSETS,
)
from ..state.actions import Action, SetPassiveSignalsAction

logger = logging.getLogger(__name__)

#: A broadcast value silent for this long is unknown (None).
STALE_S = 2.0
#: Speed 0 for this long before the yaw offsets are learnt (the car settles).
STANDSTILL_SETTLE_S = 1.0
#: Weight of one standstill sample in the offset mean (~10 samples/s reach
#: the ingress: time constant ~2 s).
OFFSET_ALPHA = 0.05
#: An offset estimate outside this range is rejected (sensor fault).
OFFSET_LIMIT = 40.0
#: 0x230 differences are used only between frames this close together.
DISTANCE_MAX_GAP_S = 2.0
#: Speed integration is used while 0x230 has been silent this long.
DISTANCE_FALLBACK_AFTER_S = 1.0
#: Odometer file writes: at most this often while the distance grows.
ODOMETER_SAVE_S = 30.0
#: The meter's odometer frozen value on this car (BEAN 52:3F 04 93 DF).
METER_ODOMETER_FROZEN_KM = 299999


class OwnOdometer:
    """Persistent km counter: ``km = max(meter 52:3F, last km + distance)``.

    The file holds ``{"km": float, "wall": epoch, "meter_km": int|None}``,
    written atomically (temp file + rename), at most every
    :data:`ODOMETER_SAVE_S` while the distance grows and on :meth:`flush`.
    Without a file (tests, replay) it works in memory only."""

    def __init__(self, path: Optional[str] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self.km: Optional[float] = None
        self.meter_km: Optional[int] = None
        self._dirty = False
        self._saved_t: Optional[float] = None
        if path:
            self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.exception("Odometer file %s unreadable; starting unknown", self.path)
            return
        km = raw.get("km") if isinstance(raw, dict) else None
        if isinstance(km, (int, float)) and 0 <= km < 2e6:
            self.km = float(km)
            m = raw.get("meter_km")
            self.meter_km = int(m) if isinstance(m, (int, float)) else None
            logger.info("Own odometer: %.1f km (file %s)", self.km, self.path)

    def set_path(self, path: Optional[str]) -> None:
        """Attach the persistence file (loads it; a value already counted in
        memory is kept when it is higher)."""
        held = self.km
        self.path = path
        if path:
            self._load()
            if held is not None and (self.km is None or held > self.km):
                self.km = held
                self._dirty = True

    def anchor_meter(self, meter_km: float) -> bool:
        """BEAN 52:3F value: the odometer never reads below the meter.
        Returns True when the value changed."""
        with self._lock:
            self.meter_km = int(meter_km)
            if self.km is None or meter_km > self.km:
                self.km = float(meter_km)
                self._dirty = True
                return True
            return False

    def add(self, km: float) -> bool:
        """Distance driven; True when the counter changed. Ignored while no
        anchor exists (no file, no 52:3F yet): the base is unknown."""
        if km <= 0.0:
            return False
        with self._lock:
            if self.km is None:
                return False
            self.km += km
            self._dirty = True
        now = self._clock()
        if self._saved_t is None or now - self._saved_t >= ODOMETER_SAVE_S:
            self.flush(now)
        return True

    def flush(self, now: Optional[float] = None) -> None:
        if not self.path or not self._dirty or self.km is None:
            return
        with self._lock:
            data = {"km": round(self.km, 3), "wall": time.time(), "meter_km": self.meter_km}
            self._dirty = False
        self._saved_t = self._clock() if now is None else now
        d = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".odometer.", dir=d)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError:
            logger.exception("Odometer file %s not written", self.path)
            self._dirty = True


class DynamicsIngest:
    """Stateful part of the broadcast decode (yaw offsets, distance,
    freshness). Methods return actions for the ingress to dispatch."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 odometer: Optional[OwnOdometer] = None) -> None:
        self._clock = clock
        self.odometer = odometer or OwnOdometer(clock=clock)
        self.offsets: List[float] = list(YAW_STANDSTILL_OFFSETS)
        self.offset_samples = 0
        self._still_since: Optional[float] = None
        self._speed: Optional[float] = None
        self._speed_t: Optional[float] = None
        self._counter: Optional[int] = None
        self._counter_t: Optional[float] = None
        self.distance_km = 0.0
        self._source: Optional[str] = None
        self._seen: Dict[str, float] = {}
        self._odo_shown: Optional[float] = None

    def _now(self, now: Optional[float]) -> float:
        return self._clock() if now is None else now

    def _fresh(self, key: str, now: float) -> None:
        self._seen[key] = now

    # ── speed (0x0B4): standstill detection + distance fallback ─────────────

    def on_speed(self, kmh: float, moving: Optional[bool] = None,
                 now: Optional[float] = None) -> List[Action]:
        now = self._now(now)
        out: Dict[str, Any] = {}
        prev, prev_t = self._speed, self._speed_t
        self._speed, self._speed_t = kmh, now
        if kmh <= 0.05 and not moving:
            if self._still_since is None:
                self._still_since = now
        else:
            self._still_since = None
        if moving is not None:
            self._fresh("moving", now)
            out["moving"] = bool(moving)
        # distance from the speed while 0x230 is silent
        stale230 = self._counter_t is None or now - self._counter_t > DISTANCE_FALLBACK_AFTER_S
        if stale230 and prev is not None and prev_t is not None and 0 < now - prev_t <= DISTANCE_MAX_GAP_S:
            km = max(0.0, prev) * (now - prev_t) / 3600.0
            out.update(self._add_distance(km, "speed"))
        return [SetPassiveSignalsAction(vehicle=out)] if out else []

    def _add_distance(self, km: float, source: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if source != self._source:
            self._source = source
            out["distance_source"] = source
        if km <= 0.0:
            return out
        self.distance_km += km
        out["distance_total_km"] = round(self.distance_km, 4)
        self.odometer.add(km)
        odo = self.odometer.km
        if odo is not None and (self._odo_shown is None or abs(odo - self._odo_shown) >= 0.01):
            self._odo_shown = odo
            out["odometer_km"] = round(odo, 2)
        return out

    # ── 0x230 distance counter ──────────────────────────────────────────────

    def on_distance_counter(self, counter: int, now: Optional[float] = None) -> List[Action]:
        now = self._now(now)
        prev, prev_t = self._counter, self._counter_t
        self._counter, self._counter_t = counter, now
        if prev is None or prev_t is None or now - prev_t > DISTANCE_MAX_GAP_S:
            return []                       # (re)anchor: never count across a gap
        delta = (counter - prev) & 0xFFFF
        if delta == 0:
            return []
        m = delta / DISTANCE_COUNTS_PER_M
        dt = now - prev_t
        # plausibility against the true speed (+50 % and 2 m slack); without
        # a fresh speed, against 70 m/s
        v = self._speed if (self._speed_t is not None and now - self._speed_t <= 1.0) else None
        limit = (max(v, 0.0) / 3.6 * dt * 1.5 + 2.0) if v is not None else (70.0 * dt + 2.0)
        if delta >= 0x8000 or m > limit:
            logger.debug("0x230 step of %d counts in %.2f s rejected (limit %.1f m)", delta, dt, limit)
            return []
        out = self._add_distance(m / 1000.0, "can230")
        return [SetPassiveSignalsAction(vehicle=out)] if out else []

    # ── 0x022 yaw rate ──────────────────────────────────────────────────────

    def on_yaw(self, raw1: int, raw2: int, now: Optional[float] = None) -> List[Action]:
        now = self._now(now)
        self._fresh("yaw", now)
        still = (self._still_since is not None and now - self._still_since >= STANDSTILL_SETTLE_S
                 and self._speed_t is not None and now - self._speed_t <= 1.0)
        if still:
            for i, raw in enumerate((raw1, raw2)):
                if abs(raw) <= OFFSET_LIMIT:
                    self.offsets[i] += OFFSET_ALPHA * (raw - self.offsets[i])
            self.offset_samples += 1
        c1 = raw1 - self.offsets[0]
        c2 = raw2 - self.offsets[1]
        dps = 0.0 if still else (c1 + c2) / 2.0 * YAW_DEG_S_PER_COUNT
        dyn = {"yaw_rate_dps": round(dps, 2), "yaw_rate_raw": raw1, "yaw_rate_raw_2": raw2,
               "yaw_offsets": (round(self.offsets[0], 1), round(self.offsets[1], 1))}
        return [SetPassiveSignalsAction(dynamics=dyn)]

    # ── 0x023 accelerations ─────────────────────────────────────────────────

    def on_accel(self, values: dict, now: Optional[float] = None) -> List[Action]:
        now = self._now(now)
        self._fresh("accel", now)
        p, q = values.get("accel_raw_p"), values.get("accel_raw_q")
        dyn = {"longitudinal_accel_ms2": values.get("longitudinal_accel_ms2"),
               "lateral_accel_ms2": values.get("lateral_accel_ms2")}
        if p is not None and q is not None:
            dyn["lateral_accel_raw"] = p + q
            dyn["longitudinal_accel_raw"] = p - q
        return [SetPassiveSignalsAction(dynamics=dyn)]

    # ── simple flags ────────────────────────────────────────────────────────

    def on_vehicle_value(self, field: str, value: Any, now: Optional[float] = None) -> List[Action]:
        """A broadcast vehicle field with expiry (accelerator_pressed,
        ice_power_request_kw)."""
        self._fresh(field, self._now(now))
        return [SetPassiveSignalsAction(vehicle={field: value})]

    def on_meter_odometer(self, km: float) -> List[Action]:
        """BEAN 52:3F (frozen at 299,999 km on this car): floor of ours."""
        if self.odometer.anchor_meter(km) or self._odo_shown is None:
            odo = self.odometer.km
            if odo is not None and odo != self._odo_shown:
                self._odo_shown = odo
                return [SetPassiveSignalsAction(vehicle={"odometer_km": round(odo, 2)})]
        return []

    def odometer_action(self) -> List[Action]:
        """The persisted odometer at start-up (before any distance)."""
        odo = self.odometer.km
        if odo is None:
            return []
        self._odo_shown = odo
        return [SetPassiveSignalsAction(vehicle={"odometer_km": round(odo, 2)})]

    # ── expiry ──────────────────────────────────────────────────────────────

    _EXPIRE = {
        "yaw": ("dynamics", ("yaw_rate_dps", "yaw_rate_raw", "yaw_rate_raw_2")),
        "accel": ("dynamics", ("longitudinal_accel_ms2", "lateral_accel_ms2",
                               "lateral_accel_raw", "longitudinal_accel_raw")),
        "moving": ("vehicle", ("moving",)),
        "accelerator_pressed": ("vehicle", ("accelerator_pressed",)),
        "ice_power_request_kw": ("vehicle", ("ice_power_request_kw",)),
    }

    def expire(self, now: Optional[float] = None) -> List[Action]:
        now = self._now(now)
        groups: Dict[str, Dict[str, Any]] = {"vehicle": {}, "dynamics": {}}
        for key, t in list(self._seen.items()):
            if now - t > STALE_S:
                del self._seen[key]
                grp, fields = self._EXPIRE.get(key, ("vehicle", (key,)))
                for f in fields:
                    groups[grp][f] = None
        if self._source is not None \
                and (self._counter_t is None or now - self._counter_t > STALE_S) \
                and (self._speed_t is None or now - self._speed_t > STALE_S):
            # car off: write the odometer now, not 30 s later
            self.odometer.flush(now)
            self._source = None
            groups["vehicle"]["distance_source"] = None
        if groups["vehicle"] or groups["dynamics"]:
            return [SetPassiveSignalsAction(vehicle=groups["vehicle"], dynamics=groups["dynamics"])]
        return []

    def status(self) -> Dict[str, Any]:
        return {"yaw_offsets": [round(o, 2) for o in self.offsets],
                "offset_samples": self.offset_samples,
                "distance_km": round(self.distance_km, 3), "distance_source": self._source,
                "odometer_km": self.odometer.km, "odometer_file": self.odometer.path}
