"""Backfill the battery store from existing recordings.

* ``backfill_trips``: trip recordings (``trip_*.ndjson``, the trip recorder's
  format, optionally gzip) run through the same decoder and collector as the
  live path, giving full rides (source ``trip``).
* ``backfill_metrics``: the backend's metrics database (``metrics_1min`` tier,
  kept 90 days) gives every rest's 12 V series (the powerbox signals, one row
  per minute: the same resolution the live collector stores) and "lite" rides
  without block data (source ``metrics1m``, grade ``L``).

Both are idempotent: a record that exists from an equal or richer source is
left alone (store.SOURCE_RANK).
"""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
from typing import Callable, Dict, Iterable, List, Optional

from . import analysis
from .collector import MIN_COLS, Collector
from .frames import FrameDecoder
from .model import ANALYSIS_VERSION, NONE16, SCHEMA_VERSION, clamp16

_KEEP = ('"0x03B"', '"0x3CB"', '"0x529"', '"0x0B4"', '"a":"sub"', '"a":"resp"',
         '"id":201', '"id":202')


def _open(path: str):
    return gzip.open(path, "rt", errors="replace") if path.endswith(".gz") else open(path, "r", errors="replace")


def feed_trip_file(path: str, decoder: FrameDecoder, collector: Collector, state: dict) -> int:
    """Feed one recording. Gateway lines carry device-domain timestamps
    (seconds since its boot); they take the wall time of the last line that
    has one (the panda frames arrive at > 100 Hz)."""
    n = 0
    wall = state.get("wall")
    with _open(path) as fh:
        for line in fh:
            if not any(k in line for k in _KEEP):
                continue
            try:
                m = json.loads(line.replace("\0", ""))   # NUL tail of a hard reset
            except ValueError:
                continue
            if not isinstance(m, dict):
                continue
            ts = m.get("ts")
            if isinstance(ts, (int, float)) and ts > 1e9:
                if wall is None or ts >= wall:
                    wall = ts
            if wall is None:
                continue
            d = m.get("d")
            if isinstance(d, dict):
                decoder.feed(wall, m.get("id"), d)
                n += 1
    state["wall"] = wall
    return n


def trip_files(paths: Iterable[str]) -> List[str]:
    out: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            out += [os.path.join(p, f) for f in os.listdir(p)
                    if f.startswith("trip_") and (f.endswith(".ndjson") or f.endswith(".ndjson.gz"))]
        else:
            out.append(p)
    return sorted(out, key=os.path.basename)


def backfill_trips(store, paths: Iterable[str], source: str = "trip", force: bool = False,
                   log: Callable[[str], None] = lambda s: None) -> Dict[str, int]:
    res = {"files": 0, "messages": 0, "rides": 0, "rides_new": 0, "rests_new": 0}

    def on_ride(rec: dict) -> None:
        res["rides"] += 1
        rid = store.put_ride(rec, force=force)
        if rid is not None:
            res["rides_new"] += 1
        f = analysis.analyse_ride(rec)
        log("  ride %s  %.0f s  grade %s  %s" % (
            _ts(rec["meta"]["start_ts"]), rec["meta"]["end_ts"] - rec["meta"]["start_ts"],
            f["quality"]["grade"], "stored" if rid is not None else "kept existing"))

    def on_rest(rec: dict) -> None:
        if rec["meta"].get("start_known") and store.put_rest(rec, force=force) is not None:
            res["rests_new"] += 1

    col = Collector(on_ride, on_rest, source=source)
    dec = FrameDecoder(col)
    state: dict = {}
    for path in trip_files(paths):
        log("reading %s" % path)
        res["messages"] += feed_trip_file(path, dec, col, state)
        res["files"] += 1
    col.finish()
    res["counts"] = dict(dec.counts)
    return res


def _ts(t: float) -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


# ── metrics database (1-min tier) ───────────────────────────────────────────

_SIGNALS = ("hv_current", "hv_voltage", "battery_soc", "battery_temp", "delta_v", "aux_voltage",
            "speed", "ambient_temp", "powerbox_voltage", "powerbox_current", "powerbox_energy",
            "powerbox_bmp2_t", "powerbox_acc", "inverter_temp")


def backfill_metrics(store, metrics_db: str, since: float = 0.0,
                     log: Callable[[str], None] = lambda s: None) -> Dict[str, int]:
    """Lite rides + full 12 V rests from ``metrics_1min``."""
    conn = sqlite3.connect("file:%s?mode=ro" % metrics_db, uri=True)
    data: Dict[str, Dict[int, tuple]] = {s: {} for s in _SIGNALS}
    q = ("SELECT signal, bucket, min, max, sum, count, last FROM metrics_1min "
         "WHERE bucket >= ? AND signal IN (%s)" % ",".join("?" * len(_SIGNALS)))
    for sig, b, mn, mx, sm, cnt, last in conn.execute(q, (int(since),) + _SIGNALS):
        data[sig][b] = (mn, mx, sm / cnt if cnt else None, last)
    conn.close()
    res = {"rides_new": 0, "rests_new": 0, "sessions": 0}
    hc = data["hv_current"]
    # a ride minute = the HV current varied within the minute (the state keeps
    # the last value after IG-OFF, so mere presence means nothing)
    live = sorted(b for b, v in hc.items() if v[0] != v[1])
    sessions: List[List[int]] = []
    for b in live:
        if sessions and b - sessions[-1][1] <= 180:
            sessions[-1][1] = b
        else:
            sessions.append([b, b])
    sessions = [s for s in sessions if s[1] - s[0] >= 60]
    res["sessions"] = len(sessions)

    def avg(sig: str, bs: List[int], idx: int = 2) -> List[float]:
        return [data[sig][b][idx] for b in bs if b in data[sig] and data[sig][b][idx] is not None]

    for s0, s1 in sessions:
        bs = list(range(s0, s1 + 60, 60))
        dur = float(s1 - s0 + 60)
        soc = avg("battery_soc", bs, 3)
        dv = avg("delta_v", bs)
        tb = avg("battery_temp", bs)
        aux = avg("aux_voltage", bs)
        sp = avg("speed", bs)
        f = {
            "v": ANALYSIS_VERSION, "dur_s": dur, "ready_s": None, "source": "metrics1m",
            "dist_km": round(sum(sp) / 60.0, 2) if sp else None, "n_blk": 0, "n_sec": 0, "blk_rate_hz": None,
            "temp": {"source": "metrics 1 min", "t_start": tb[0] if tb else None, "t_end": tb[-1] if tb else None,
                     "t_min": min(avg("battery_temp", bs, 0), default=None),
                     "t_max": max(avg("battery_temp", bs, 1), default=None),
                     "t_mean": round(sum(tb) / len(tb), 2) if tb else None,
                     "amb_mean": (lambda a: round(sum(a) / len(a), 1) if a else None)(avg("ambient_temp", bs)),
                     "inv_max": max(avg("inverter_temp", bs, 1), default=None)},
            "soc": {"start": soc[0] if soc else None, "end": soc[-1] if soc else None,
                    "min": min(avg("battery_soc", bs, 0), default=None),
                    "max": max(avg("battery_soc", bs, 1), default=None)},
            "stress": {"i_max": max(avg("hv_current", bs, 1), default=None),
                       "i_min": min(avg("hv_current", bs, 0), default=None)},
            "bal": {"spread_all_mean_mv": round(1000 * sum(dv) / len(dv), 1) if dv else None,
                    "spread_max_mv": round(1000 * max(avg("delta_v", bs, 1)), 0) if dv else None, "fit": None},
            "r": {"n_steps": 0}, "ocv": {"on": None, "off": None}, "ecu": {},
            "aux": {"ready_v_obd": round(sorted(aux)[len(aux) // 2], 3) if aux else None},
            "quality": {"grade": "L", "flags": ["lite_1min"]},
        }
        if f["soc"]["min"] is not None and f["soc"]["max"] is not None:
            f["soc"]["swing"] = round(f["soc"]["max"] - f["soc"]["min"], 1)
        if store.put_lite_ride(float(s0), float(s0) + dur, f) is not None:
            res["rides_new"] += 1
            log("  lite ride %s  %.0f min" % (_ts(s0), dur / 60))

    # rests between sessions (and after the last one, left open)
    pv = data["powerbox_voltage"]
    if not pv:
        return res
    edges = [(None, sessions[0][0])] if sessions else []
    for k, (s0, s1) in enumerate(sessions):
        nxt = sessions[k + 1][0] if k + 1 < len(sessions) else None
        edges.append((s1 + 60, nxt))
    all_b = sorted(pv)
    for start, end in edges:
        bs = [b for b in all_b if (start is None or b >= start) and (end is None or b < end)]
        if len(bs) < 2:
            continue
        t0 = start if start is not None else bs[0]
        tab = {c: [] for c in MIN_COLS}
        for b in bs:
            v = pv[b]
            tab["t"].append(int((b - t0) // 60))
            tab["v"].append(clamp16(v[2] * 1000))
            tab["vmin"].append(clamp16(v[0] * 1000))
            c = data["powerbox_current"].get(b)
            tab["ma"].append(int(round(c[2] * 10000)) if c and c[2] is not None else -1)
            e = data["powerbox_energy"].get(b)
            tab["mah"].append(int(round(e[3] * 10)) if e else -1)
            tt = data["powerbox_bmp2_t"].get(b)
            tab["temp"].append(clamp16(tt[2] * 100) if tt and tt[2] is not None else NONE16)
            a = data["powerbox_acc"].get(b)
            tab["acc"].append(1 if a and a[2] is not None and a[2] > 0.5 else 0)
        closed = end is not None
        rec = {"meta": {"kind": "rest", "schema": SCHEMA_VERSION, "source": "metrics1m",
                        "start_ts": float(t0), "end_ts": float(end if end is not None else bs[-1] + 60),
                        "closed": closed, "start_known": start is not None},
               "tables": {"min": tab}}
        if not closed:
            rec["meta"]["closed"] = True      # a backfill never leaves a spool row
        if store.put_rest(rec) is not None:
            res["rests_new"] += 1
    return res
