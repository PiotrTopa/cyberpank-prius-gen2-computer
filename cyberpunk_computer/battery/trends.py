"""Robust long-run trends: covariate normalisation, Theil-Sen slope with
Mann-Kendall significance, Pettitt change point. Pure Python.

A point is ``(ts, y, x)``: time (unix s), metric value, optional covariate
(e.g. a temperature term). Methods:

* covariate coefficient: median of pairwise slopes dy/dx over pairs that are
  close in time (so ageing between them is negligible) and far apart in x;
  shrunk towards a prior while few pairs exist;
* trend: Theil-Sen slope of the normalised values over time; confidence
  interval from the Mann-Kendall variance (Sen 1968); two-sided p-value;
* change point: Pettitt's test on the normalised values.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from . import stats as S

DAY = 86400.0
MAX_POINTS = 300            # more points are reduced to time-bin medians first


def reduce_points(pts: Sequence[tuple], max_points: int = MAX_POINTS) -> List[tuple]:
    pts = sorted(pts)
    if len(pts) <= max_points:
        return list(pts)
    t0, t1 = pts[0][0], pts[-1][0]
    width = (t1 - t0) / max_points or 1.0
    bins: dict = {}
    for p in pts:
        bins.setdefault(int((p[0] - t0) / width), []).append(p)
    out = []
    for k in sorted(bins):
        b = bins[k]
        out.append(tuple(S.median([p[j] for p in b]) if b[0][j] is not None else None
                         for j in range(len(b[0]))))
    return out


def covariate_coef(pts: Sequence[tuple], min_dx: float, max_dt_days: float = 45.0,
                   prior: float = 0.0, prior_weight: int = 30) -> dict:
    """dy/dx from time-local pairs, shrunk to ``prior``."""
    slopes: List[float] = []
    p = [q for q in sorted(pts) if q[2] is not None]
    n = len(p)
    lim = max_dt_days * DAY
    for a in range(n):
        ta, ya, xa = p[a]
        for b in range(a + 1, n):
            tb, yb, xb = p[b]
            if tb - ta > lim:
                break
            dx = xb - xa
            if abs(dx) >= min_dx:
                slopes.append((yb - ya) / dx)
    if len(slopes) < 6:
        return {"value": prior, "source": "prior", "pairs": len(slopes), "learned": None}
    learned = S.median(slopes)
    w = len(slopes) / (len(slopes) + float(prior_weight))
    return {"value": w * learned + (1 - w) * prior,
            "source": "learned" if w > 0.8 else "prior+learned",
            "pairs": len(slopes), "learned": learned}


def theil_sen(ts: Sequence[float], ys: Sequence[float], alpha: float = 0.05) -> Optional[dict]:
    """Slope per second with a (1 - alpha) interval and the Mann-Kendall p."""
    n = len(ts)
    if n < 4 or ts[-1] - ts[0] <= 0:
        return None
    slopes = []
    s_stat = 0
    for a in range(n):
        for b in range(a + 1, n):
            dt = ts[b] - ts[a]
            dy = ys[b] - ys[a]
            if dt > 0:
                slopes.append(dy / dt)
            s_stat += (dy > 0) - (dy < 0)
    if not slopes:
        return None
    slopes.sort()
    m = len(slopes)
    slope = S.median(slopes)
    # ties in y
    counts: dict = {}
    for y in ys:
        counts[y] = counts.get(y, 0) + 1
    tie = sum(c * (c - 1) * (2 * c + 5) for c in counts.values() if c > 1)
    var = (n * (n - 1) * (2 * n + 5) - tie) / 18.0
    if var <= 0:
        return {"slope": slope, "lo": slope, "hi": slope, "p": 1.0, "n": n,
                "intercept": S.median([y - slope * t for t, y in zip(ts, ys)])}
    z = (s_stat - (1 if s_stat > 0 else -1 if s_stat < 0 else 0)) / math.sqrt(var)
    pval = 2.0 * (1.0 - S.norm_cdf(abs(z)))
    c = S.norm_ppf(1 - alpha / 2) * math.sqrt(var)
    lo_i = max(0, int(round((m - c) / 2.0)) - 1)
    hi_i = min(m - 1, int(round((m + c) / 2.0)))
    return {"slope": slope, "lo": slopes[lo_i], "hi": slopes[hi_i], "p": pval, "n": n,
            "intercept": S.median([y - slope * t for t, y in zip(ts, ys)])}


def pettitt(ys: Sequence[float]) -> Optional[Tuple[int, float]]:
    """-> (index of the first point after the change, approximate p-value)."""
    n = len(ys)
    if n < 10:
        return None
    # ranks (average for ties)
    order = sorted(range(n), key=lambda k: ys[k])
    rank = [0.0] * n
    k = 0
    while k < n:
        j = k
        while j + 1 < n and ys[order[j + 1]] == ys[order[k]]:
            j += 1
        r = 0.5 * (k + j) + 1.0
        for q in range(k, j + 1):
            rank[order[q]] = r
        k = j + 1
    best, best_k, acc = 0.0, 0, 0.0
    for t in range(n - 1):
        acc += rank[t]
        u = abs(2.0 * acc - (t + 1) * (n + 1))
        if u > best:
            best, best_k = u, t + 1
    p = 2.0 * math.exp(-6.0 * best * best / (n ** 3 + n ** 2))
    return best_k, min(1.0, p)


def analyse_trend(pts: Sequence[tuple], *, min_dx: float = 0.0, prior: float = 0.0,
                  normalise: bool = True, log: bool = False, min_points: int = 8,
                  min_span_days: float = 21.0, series_max: int = 240) -> dict:
    """Trend of a metric. ``pts`` = (ts, y, x or None).

    With ``log`` the model is ln y = a + c*x + b*t (multiplicative covariate,
    e.g. Arrhenius) and slopes are reported as a fraction per 30 days;
    otherwise in the metric's unit per 30 days. ``value`` / ``baseline`` are in
    the metric's unit, normalised to x = 0.
    """
    raw = [(t, y, x) for (t, y, x) in pts if y is not None and (not log or y > 0)]
    raw.sort()
    out: dict = {"n": len(raw), "span_days": None, "value": None, "value_sigma": None,
                 "baseline": None, "slope_30d": None, "lo_30d": None, "hi_30d": None,
                 "p": None, "significant": False, "change": None, "coef": None,
                 "log": log, "series": [], "status": "no data" if not raw else "ok"}
    if not raw:
        return out
    use_x = normalise and all(p[2] is not None for p in raw)
    work = [(t, math.log(y) if log else y, x if use_x else None) for (t, y, x) in raw]
    coef = None
    if use_x:
        coef = covariate_coef(work, min_dx=min_dx, prior=prior)
        work = [(t, y - coef["value"] * x, x) for (t, y, x) in work]
    out["coef"] = coef
    back = (lambda v: math.exp(v)) if log else (lambda v: v)
    norm = [(t, back(y)) for (t, y, _) in work]
    last = [v for _, v in norm[-5:]]
    out["value"] = S.median(last)
    sig = S.mad_sigma([v for _, v in norm[-15:]])
    out["value_sigma"] = sig / math.sqrt(min(len(norm), 15)) if sig is not None and len(norm) >= 3 else None
    out["baseline"] = S.median([v for _, v in norm[:max(3, min(30, len(norm) // 2))]])
    span = (raw[-1][0] - raw[0][0]) / DAY
    out["span_days"] = round(span, 1)
    step = max(1, len(raw) // series_max)
    out["series"] = [[round(raw[k][0]), S.rnd(raw[k][1], 4), S.rnd(norm[k][1], 4)]
                     for k in range(0, len(raw), step)]
    if len(raw) < min_points or span < min_span_days:
        out["status"] = "history too short for a trend (%d points over %.0f days; need %d over %.0f)" % (
            len(raw), span, min_points, min_span_days)
        return out
    red = reduce_points(work)
    ts_ = [p[0] for p in red]
    ys_ = [p[1] for p in red]
    ts_fit = theil_sen(ts_, ys_)
    if ts_fit:
        k = 30 * DAY
        out.update(slope_30d=ts_fit["slope"] * k, lo_30d=ts_fit["lo"] * k, hi_30d=ts_fit["hi"] * k,
                   p=ts_fit["p"],
                   significant=bool(ts_fit["p"] < 0.05 and ts_fit["lo"] * ts_fit["hi"] > 0),
                   intercept=ts_fit["intercept"])
    cp = pettitt(ys_)
    if cp and cp[1] < 0.05:
        k = cp[0]
        before, after = S.median(ys_[:k]), S.median(ys_[k:])
        out["change"] = {"ts": round(ts_[k]), "p": S.rnd(cp[1], 4),
                         "before": S.rnd(back(before), 4), "after": S.rnd(back(after), 4)}
    for key in ("value", "value_sigma", "baseline", "slope_30d", "lo_30d", "hi_30d", "p", "intercept"):
        out[key] = S.rnd(out.get(key), 6)
    if coef:
        coef["value"] = S.rnd(coef["value"], 4)
        coef["learned"] = S.rnd(coef["learned"], 4)
    return out
