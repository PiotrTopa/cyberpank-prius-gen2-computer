"""
``GET /api/v1/summary/power`` body: what the dashboard home view shows about
the computer's consumption, computed on the backend from the metrics tiers.

    stack / poco    {"avg_1m", "avg_5m", "avg_1h", "avg_24h"} in W, signed:
                    stack > 0 = drawn from the 12 V battery, < 0 = net charge
                    into it (the solar charger delivers more than the load)
    consumed_24h_wh stack energy drawn over the last 24 h (positive minutes)
    charged_24h_wh  energy put back over the last 24 h (negative minutes)
    energy_24h_wh   net = consumed - charged (1-minute buckets x 60 s; minutes
                    with no samples count as zero: the computer was off)
    power_24h       [[t, avg W], ...] signed stack power, 15-minute buckets
    voltage_24h     [[t, avg], ...] 12 V supply, 15-minute buckets, last 24 h
    voltage_min_24h lowest 1-minute minimum of the 12 V supply in the last 24 h
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

SPARK_BUCKET_S = 900


def _r(v: Optional[float], nd: int = 2) -> Optional[float]:
    return None if v is None else round(v, nd)


def _avg(points, since: float, key=lambda p: p.avg) -> Optional[float]:
    sel = [p for p in points if p.bucket >= since]
    n = sum(p.count for p in sel)
    if n <= 0:
        return None
    return sum(key(p) * p.count for p in sel) / n


def _series_avgs(db, conn, signal: str, now: float) -> Dict[str, Any]:
    raw = db.query_series(conn, signal, now - 300, now, "raw")
    mins = db.query_series(conn, signal, now - 86400, now, "1m")
    return {
        "avg_1m": _r(_avg(raw, now - 60)),
        "avg_5m": _r(_avg(raw, now - 300)),
        "avg_1h": _r(_avg(mins, now - 3600)),
        "avg_24h": _r(_avg(mins, now - 86400)),
        "_mins": mins,
    }


def compute_power_summary(db, conn, now: float) -> Dict[str, Any]:
    stack = _series_avgs(db, conn, "powerbox_power", now)
    poco = _series_avgs(db, conn, "poco_power", now)
    stack_mins = stack.pop("_mins")
    poco.pop("_mins")
    consumed = sum(max(p.avg, 0.0) for p in stack_mins) * 60.0 / 3600.0 if stack_mins else None
    charged = sum(max(-p.avg, 0.0) for p in stack_mins) * 60.0 / 3600.0 if stack_mins else None
    net = None if consumed is None or charged is None else consumed - charged

    volt = db.query_series(conn, "powerbox_voltage", now - 86400, now, "1m")
    vmin = min((p.min for p in volt), default=None)
    return {
        "ts": now,
        "stack": stack,
        "poco": poco,
        "energy_24h_wh": _r(net, 1),
        "consumed_24h_wh": _r(consumed, 1),
        "charged_24h_wh": _r(charged, 1),
        "power_24h": _buckets(stack_mins, 2),
        "voltage_24h": _buckets(volt, 3),
        "voltage_min_24h": _r(vmin),
    }


def _buckets(points, nd: int) -> List[List[float]]:
    """Count-weighted averages of 1-minute points in SPARK_BUCKET_S buckets."""
    acc: Dict[int, List[float]] = {}
    for p in points:
        b = int(p.bucket // SPARK_BUCKET_S * SPARK_BUCKET_S)
        a = acc.setdefault(b, [0.0, 0])
        a[0] += p.avg * p.count
        a[1] += p.count
    return [[b, round(s / n, nd)] for b, (s, n) in sorted(acc.items()) if n]
