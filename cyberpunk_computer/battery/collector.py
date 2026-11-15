"""Event stream -> compact per-ride and per-rest raw records.

Unit of collection:

* **ride** = one period in which the HV battery ECU is on the bus (IG-ON to
  IG-OFF): opened by the first battery frame, closed after ``RIDE_GAP_S``
  without one;
* **rest** = the time between two rides (12 V telemetry from the powerbox, one
  row per minute).

Time ``t`` is seconds on one continuous clock (wall-clock anchored, see
service.py / backfill.py); it must not run backwards inside a ride.

A raw record is ``{"meta": {...}, "tables": {name: {column: [int, ...]}}}`` and
is what the store keeps (model.encode_record); features are derived from it in
analysis.py and can be recomputed at any time.

Tables of a ride:

    blk    one row per 21CE answer: t (0.1 s), soc2 (0.5 %), i (0.01 A), v1..v14 (0.01 V)
    sec    one row per second with 0x03B data: t (s), i (0.1 A mean), imin, imax, v (0.1 V mean)
    slow   one row per 10 s: SOC, temperatures, limits, speed, fan, 12 V, flags
    wake   12 V around the start: t (0.1 s, relative to the ride start), v (mV), ma (0.1 mA)

Tables of a rest:

    min    one row per minute: t (min), v (mV mean), vmin (mV), ma (0.1 mA mean),
           mah (0.1 mAh, powerbox counter), temp (0.01 degC), acc (0/1)
"""

from __future__ import annotations

from collections import deque
from typing import Callable, Dict, List, Optional

from .model import (
    FAST_LAGS_S, FAST_MIN_DI, HIST_EDGES, N_BLOCKS, NONE16, RIDE_GAP_S,
    SCHEMA_VERSION, SLOW_PERIOD_S, WAKE_POST_S, WAKE_PRE_S, clamp16, opt16,
)

SLOW_COLS = (
    "t", "soc2", "t1", "t2", "ccl", "cdl", "speed", "intake", "fan_v", "fan",
    "tb1", "tb2", "tb3", "chg_kw", "dis_kw", "dsoc", "aux_obd", "aux_cf",
    "pb_v", "pb_ma", "amb", "pb_t", "ready", "ice", "inv1", "inv2",
)
BLK_COLS = ("t", "soc2", "i") + tuple("v%d" % (k + 1) for k in range(N_BLOCKS))
SEC_COLS = ("t", "i", "imin", "imax", "v")
WAKE_COLS = ("t", "v", "ma")
MIN_COLS = ("t", "v", "vmin", "ma", "mah", "temp", "acc")


def _table(cols) -> Dict[str, List[int]]:
    return {c: [] for c in cols}


class _FastPairs:
    """Through-origin regression sums of (dI, dV) over a fixed lag on the
    0x03B stream, evaluated on a fixed time grid (one pair per lag, so pairs do
    not overlap and are not selected by where a ramp starts). R = -sxy / sxx."""

    __slots__ = ("lag", "n", "sxx", "sxy", "syy", "_next")

    def __init__(self, lag: float) -> None:
        self.lag = lag
        self.n = 0
        self.sxx = self.sxy = self.syy = 0.0
        self._next = 0.0

    def add(self, t: float, v: float, i: float, ring: deque) -> None:
        if t < self._next:
            return
        self._next = t + self.lag
        target = t - self.lag
        ref = None
        for s in ring:                      # oldest first
            if s[0] <= target:
                ref = s
            else:
                break
        # an older reference is accepted up to 0.3 s: identical frames are
        # dropped by the ingress dedupe (0.25 s), i.e. the value was unchanged
        if ref is None or target - ref[0] > 0.3:
            return
        di = i - ref[2]
        if abs(di) < FAST_MIN_DI:
            return
        dv = v - ref[1]
        self.n += 1
        self.sxx += di * di
        self.sxy += di * dv
        self.syy += dv * dv

    def to_meta(self) -> dict:
        return {"lag": self.lag, "n": self.n, "sxx": round(self.sxx, 2),
                "sxy": round(self.sxy, 2), "syy": round(self.syy, 2)}


class _Ride:
    def __init__(self, t0: float, source: str) -> None:
        self.t0 = t0
        self.source = source
        self.last_hv = t0
        self.last_t = t0
        self.blk = _table(BLK_COLS)
        self.sec = _table(SEC_COLS)
        self.slow = _table(SLOW_COLS)
        self.wake = _table(WAKE_COLS)
        # pack integration (full rate)
        self.p_prev: Optional[tuple] = None        # (t, v, i)
        self.ring: deque = deque()
        self.fast = [_FastPairs(lag) for lag in FAST_LAGS_S]
        self.ah_dis = self.ah_chg = self.wh_dis = self.wh_chg = 0.0
        self.i2s = 0.0                             # integral of I^2 dt
        self.pack_s = 0.0                          # seconds covered by pack data
        self.pack_gap_s = 0.0
        self.hist = [0.0] * (len(HIST_EDGES) + 1)
        self.near_limit_s = 0.0
        self.i_max = self.i_min = 0.0
        self.p_max = self.p_min = 0.0
        self.n_pack = 0
        # current second bucket
        self.sb = -1
        self.sb_n = 0
        self.sb_i = self.sb_v = 0.0
        self.sb_imin = self.sb_imax = 0.0
        # latest slow values
        self.cur: Dict[str, Optional[float]] = {}
        self.next_slow = t0
        self.ready_s = 0.0
        self.dist_km = 0.0
        self.sp_prev: Optional[tuple] = None
        # 21D0
        self.d0_n = 0
        self.d0_r_sum = [0] * N_BLOCKS
        self.d0_last: Optional[dict] = None
        self.d0_first: Optional[dict] = None
        self.counts: Dict[str, int] = {}


class Collector:
    """Turns battery events into ride / rest records.

    ``on_ride(record)`` / ``on_rest(record)`` are called with closed records;
    ``snapshot()`` returns the open ones for spooling. Single-threaded.
    """

    def __init__(self, on_ride: Callable[[dict], None], on_rest: Callable[[dict], None],
                 source: str = "live") -> None:
        self._on_ride = on_ride
        self._on_rest = on_rest
        self.source = source
        self.ride: Optional[_Ride] = None
        self.now = 0.0
        # rest state
        self.rest_t0: Optional[float] = None
        self.rest_start_known = False
        self.rest_min = _table(MIN_COLS)
        self._mb = -1                 # current minute bucket
        self._mb_n = 0
        self._mb_v = self._mb_ma = self._mb_temp = 0.0
        self._mb_nma = self._mb_ntemp = 0
        self._mb_vmin = 0.0
        self._mb_mah: Optional[float] = None
        self._acc: Optional[bool] = None
        self._pb_ring: deque = deque()      # (t, v, ma) before a ride starts
        self._ccl: Optional[float] = None
        self._cdl: Optional[float] = None

    # ── restore (service restart) ────────────────────────────────────────────

    def restore_rest(self, record: dict) -> None:
        """Continue an open rest record read back from the store."""
        m = record["meta"]
        self.rest_t0 = m["start_ts"]
        self.rest_start_known = bool(m.get("start_known", True))
        self.rest_min = {c: list(record["tables"]["min"].get(c, [])) for c in MIN_COLS}

    # ── HV events ────────────────────────────────────────────────────────────

    def _hv(self, t: float) -> _Ride:
        r = self.ride
        if r is not None and t - r.last_hv > RIDE_GAP_S:
            self._close_ride(r.last_hv)
            r = None
        if r is None:
            r = self._open_ride(t)
        r.last_hv = t
        if t > r.last_t:
            r.last_t = t
        self.now = max(self.now, t)
        return r

    def pack(self, t: float, v: float, i: float) -> None:
        r = self._hv(t)
        r.n_pack += 1
        prev = r.p_prev
        if prev is not None:
            dt = t - prev[0]
            if 0.0 < dt <= 1.0:
                # hold the previous value over the interval (identical frames
                # are dropped by the ingress dedupe, so a gap means "unchanged")
                pi, pv = prev[2], prev[1]
                q = pi * dt / 3600.0
                if q >= 0:
                    r.ah_dis += q
                    r.wh_dis += q * pv
                else:
                    r.ah_chg -= q
                    r.wh_chg -= q * pv
                r.i2s += pi * pi * dt
                r.pack_s += dt
                k = 0
                for e in HIST_EDGES:
                    if pi < e:
                        break
                    k += 1
                r.hist[k] += dt
                lim = self._cdl if pi > 0 else self._ccl
                if lim and abs(pi) >= 0.9 * lim:
                    r.near_limit_s += dt
            elif dt > 1.0:
                r.pack_gap_s += dt
        r.p_prev = (t, v, i)
        if i > r.i_max:
            r.i_max = i
        if i < r.i_min:
            r.i_min = i
        p = v * i / 1000.0
        if p > r.p_max:
            r.p_max = p
        if p < r.p_min:
            r.p_min = p
        ring = r.ring
        for f in r.fast:
            f.add(t, v, i, ring)
        ring.append((t, v, i))
        while ring and t - ring[0][0] > 1.4:
            ring.popleft()
        sb = int(t - r.t0)
        if sb != r.sb:
            self._flush_sec(r)
            r.sb = sb
            r.sb_n = 0
            r.sb_i = r.sb_v = 0.0
            r.sb_imin = r.sb_imax = i
        r.sb_n += 1
        r.sb_i += i
        r.sb_v += v
        if i < r.sb_imin:
            r.sb_imin = i
        if i > r.sb_imax:
            r.sb_imax = i

    @staticmethod
    def _flush_sec(r: _Ride) -> None:
        if r.sb_n:
            s = r.sec
            s["t"].append(r.sb)
            s["i"].append(clamp16(r.sb_i / r.sb_n * 10))
            s["imin"].append(clamp16(r.sb_imin * 10))
            s["imax"].append(clamp16(r.sb_imax * 10))
            s["v"].append(clamp16(r.sb_v / r.sb_n * 10))
            r.sb_n = 0

    def ecu(self, t: float, soc: float, ccl: float, cdl: float, t1: float, t2: float) -> None:
        r = self._hv(t)
        c = r.cur
        c["soc"] = soc
        c["ccl"] = ccl
        c["cdl"] = cdl
        c["t1"] = t1
        c["t2"] = t2
        self._ccl, self._cdl = ccl, cdl
        r.counts["ecu"] = r.counts.get("ecu", 0) + 1
        self._slow(r, t)

    def blocks(self, t: float, soc: float, i: float, volts: List[float]) -> None:
        r = self._hv(t)
        b = r.blk
        b["t"].append(int(round((t - r.t0) * 10)))
        b["soc2"].append(int(round(soc * 2)))
        b["i"].append(clamp16(i * 100))
        for k in range(N_BLOCKS):
            b["v%d" % (k + 1)].append(clamp16(volts[k] * 100))
        if "soc" not in r.cur:
            r.cur["soc"] = soc
        self._slow(r, t)

    def cf(self, t: float, d: dict) -> None:
        r = self.ride
        if r is None:
            return
        c = r.cur
        for k_src, k_dst in (("intake", "intake"), ("fan_v", "fan_v"), ("aux_v", "aux_cf"),
                             ("chg_kw", "chg_kw"), ("dis_kw", "dis_kw"), ("dsoc", "dsoc"),
                             ("fan", "fan"), ("tb1", "tb1"), ("tb2", "tb2"), ("tb3", "tb3")):
            if k_src in d:
                c[k_dst] = d[k_src]
        key = "cf_full" if "tb1" in d else "cf_first"
        r.counts[key] = r.counts.get(key, 0) + 1

    def d0(self, t: float, d: dict) -> None:
        r = self.ride
        if r is None:
            return
        r.d0_n += 1
        for k in range(N_BLOCKS):
            r.d0_r_sum[k] += d["r"][k]
        if r.d0_first is None:
            r.d0_first = d
        r.d0_last = d

    def obd(self, t: float, key: str, value: float) -> None:
        r = self.ride
        if r is None:
            return
        if key == "aux_v":
            r.cur["aux_obd"] = value
        elif key == "rpm":
            r.cur["ice"] = 1.0 if value > 400 else 0.0
        elif key == "ambient":
            r.cur["amb"] = value
        elif key in ("inv1", "inv2"):
            r.cur[key] = value

    def speed(self, t: float, kmh: float) -> None:
        r = self.ride
        if r is None:
            return
        prev = r.sp_prev
        if prev is not None:
            dt = t - prev[0]
            if 0.0 < dt < 3.0:
                r.dist_km += prev[1] * dt / 3600.0
        r.sp_prev = (t, kmh)
        r.cur["speed"] = kmh

    def ready(self, t: float, flag: bool) -> None:
        r = self.ride
        if r is None:
            return
        prev = r.cur.get("ready_t")
        if r.cur.get("ready") and prev is not None and 0.0 < t - prev < 5.0:
            r.ready_s += t - prev
        r.cur["ready"] = 1.0 if flag else 0.0
        r.cur["ready_t"] = t

    # ── 12 V events ──────────────────────────────────────────────────────────

    def acc(self, t: float, on: bool) -> None:
        self._acc = on
        self.now = max(self.now, t)

    def powerbox(self, t: float, v: float, ma: Optional[float], mah: Optional[float],
                 temp: Optional[float]) -> None:
        self.now = max(self.now, t)
        r = self.ride
        if r is not None:
            c = r.cur
            c["pb_v"] = v
            if ma is not None:
                c["pb_ma"] = ma
            if temp is not None:
                c["pb_t"] = temp
            if t - r.t0 <= WAKE_POST_S:
                w = r.wake
                w["t"].append(int(round((t - r.t0) * 10)))
                w["v"].append(clamp16(v * 1000))
                w["ma"].append(int(round((ma or 0.0) * 10)))
            return
        ring = self._pb_ring
        ring.append((t, v, ma))
        while ring and t - ring[0][0] > WAKE_PRE_S:
            ring.popleft()
        if self.rest_t0 is None:
            self.rest_t0 = t
            self.rest_start_known = False
        mb = int((t - self.rest_t0) // 60)
        if mb != self._mb:
            self._flush_min()
            self._mb = mb
            self._mb_vmin = v
        self._mb_n += 1
        self._mb_v += v
        if v < self._mb_vmin:
            self._mb_vmin = v
        if ma is not None:
            self._mb_ma += ma
            self._mb_nma += 1
        if temp is not None:
            self._mb_temp += temp
            self._mb_ntemp += 1
        if mah is not None:
            self._mb_mah = mah

    def _flush_min(self) -> None:
        if not self._mb_n:
            return
        m = self.rest_min
        m["t"].append(self._mb)
        m["v"].append(clamp16(self._mb_v / self._mb_n * 1000))
        m["vmin"].append(clamp16(self._mb_vmin * 1000))
        m["ma"].append(int(round(self._mb_ma / self._mb_nma * 10)) if self._mb_nma else -1)
        m["mah"].append(int(round(self._mb_mah * 10)) if self._mb_mah is not None else -1)
        m["temp"].append(clamp16(self._mb_temp / self._mb_ntemp * 100) if self._mb_ntemp else NONE16)
        m["acc"].append(1 if self._acc else 0)
        self._mb_n = self._mb_nma = self._mb_ntemp = 0
        self._mb_v = self._mb_ma = self._mb_temp = 0.0
        self._mb_mah = None

    # ── time ─────────────────────────────────────────────────────────────────

    def tick(self, t: float) -> None:
        """Advance the clock without data (closes a ride after the HV gap)."""
        self.now = max(self.now, t)
        r = self.ride
        if r is not None and t - r.last_hv > RIDE_GAP_S:
            self._close_ride(r.last_hv)

    def finish(self, t: Optional[float] = None) -> None:
        """End of input (backfill, shutdown): close the open ride; the open
        rest stays open (see ``snapshot``)."""
        if self.ride is not None:
            self._close_ride(self.ride.last_hv)

    # ── internals ────────────────────────────────────────────────────────────

    def _open_ride(self, t: float) -> _Ride:
        # the rest that ends here
        if self.rest_t0 is not None:
            self._flush_min()
            rec = self._rest_record(t, closed=True)
            self.rest_t0 = None
            self.rest_min = _table(MIN_COLS)
            self._mb = -1
            if rec["tables"]["min"]["t"]:
                self._on_rest(rec)
        r = _Ride(t, self.source)
        for (pt, pv, pma) in self._pb_ring:
            r.wake["t"].append(int(round((pt - t) * 10)))
            r.wake["v"].append(clamp16(pv * 1000))
            r.wake["ma"].append(int(round((pma or 0.0) * 10)))
        self._pb_ring.clear()
        self.ride = r
        return r

    def _slow(self, r: _Ride, t: float) -> None:
        if t < r.next_slow:
            return
        r.next_slow = t + SLOW_PERIOD_S
        c = r.cur
        g = c.get
        s = r.slow
        s["t"].append(int(t - r.t0))
        s["soc2"].append(opt16(g("soc"), 2))
        s["t1"].append(opt16(g("t1")))
        s["t2"].append(opt16(g("t2")))
        s["ccl"].append(opt16(g("ccl")))
        s["cdl"].append(opt16(g("cdl")))
        s["speed"].append(opt16(g("speed")))
        s["intake"].append(opt16(g("intake"), 100))
        s["fan_v"].append(opt16(g("fan_v"), 10))
        s["fan"].append(opt16(g("fan")))
        s["tb1"].append(opt16(g("tb1"), 100))
        s["tb2"].append(opt16(g("tb2"), 100))
        s["tb3"].append(opt16(g("tb3"), 100))
        s["chg_kw"].append(opt16(g("chg_kw")))
        s["dis_kw"].append(opt16(g("dis_kw")))
        s["dsoc"].append(opt16(g("dsoc"), 100))
        s["aux_obd"].append(opt16(g("aux_obd"), 1000))
        s["aux_cf"].append(opt16(g("aux_cf"), 10))
        s["pb_v"].append(opt16(g("pb_v"), 1000))
        s["pb_ma"].append(opt16(g("pb_ma")))
        s["amb"].append(opt16(g("amb"), 10))
        s["pb_t"].append(opt16(g("pb_t"), 100))
        s["ready"].append(opt16(g("ready")))
        s["ice"].append(opt16(g("ice")))
        s["inv1"].append(opt16(g("inv1")))
        s["inv2"].append(opt16(g("inv2")))

    def _ride_record(self, r: _Ride, end: float, closed: bool) -> dict:
        meta = {
            "kind": "ride", "schema": SCHEMA_VERSION, "source": r.source,
            "start_ts": round(r.t0, 3), "end_ts": round(end, 3), "closed": closed,
            "n_pack": r.n_pack, "pack_s": round(r.pack_s, 2), "pack_gap_s": round(r.pack_gap_s, 1),
            "ah_dis": round(r.ah_dis, 5), "ah_chg": round(r.ah_chg, 5),
            "wh_dis": round(r.wh_dis, 3), "wh_chg": round(r.wh_chg, 3),
            "i2s": round(r.i2s, 1), "hist_s": [round(x, 2) for x in r.hist],
            "near_limit_s": round(r.near_limit_s, 2),
            "i_max": round(r.i_max, 1), "i_min": round(r.i_min, 1),
            "p_max_kw": round(r.p_max, 2), "p_min_kw": round(r.p_min, 2),
            "fast": [f.to_meta() for f in r.fast],
            "ready_s": round(r.ready_s, 1), "dist_km": round(r.dist_km, 3),
            "counts": dict(r.counts),
        }
        if r.d0_n:
            meta["d0"] = {
                "n": r.d0_n, "r_mean": [round(x / r.d0_n, 2) for x in r.d0_r_sum],
                "first": {k: r.d0_first[k] for k in ("t_low", "t_dcinh", "t_high", "t_hot")},
                "last": {k: r.d0_last[k] for k in ("t_low", "t_dcinh", "t_high", "t_hot")},
            }
        return {"meta": meta, "tables": {"blk": r.blk, "sec": r.sec, "slow": r.slow, "wake": r.wake}}

    def _rest_record(self, end: float, closed: bool) -> dict:
        meta = {"kind": "rest", "schema": SCHEMA_VERSION, "source": self.source,
                "start_ts": round(self.rest_t0 or end, 3), "end_ts": round(end, 3),
                "closed": closed, "start_known": self.rest_start_known}
        return {"meta": meta, "tables": {"min": self.rest_min}}

    def _close_ride(self, end: float) -> None:
        r = self.ride
        if r is None:
            return
        self.ride = None
        self._flush_sec(r)
        rec = self._ride_record(r, end, closed=True)
        # the rest that starts here
        self.rest_t0 = end
        self.rest_start_known = True
        self.rest_min = _table(MIN_COLS)
        self._mb = -1
        self._on_ride(rec)

    def snapshot(self) -> dict:
        """Open records for the spool: {"ride": record|None, "rest": record|None}."""
        out = {"ride": None, "rest": None}
        r = self.ride
        if r is not None:
            out["ride"] = self._ride_record(r, r.last_t, closed=False)
        elif self.rest_t0 is not None and self.rest_min["t"]:
            out["rest"] = self._rest_record(self.now, closed=False)
        return out
