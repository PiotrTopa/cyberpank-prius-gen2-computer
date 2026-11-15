"""Small robust statistics (pure Python)."""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple


def mean(xs: Sequence[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def median(xs: Sequence[float]) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def quantile(xs: Sequence[float], q: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def mad_sigma(xs: Sequence[float]) -> Optional[float]:
    """Robust standard deviation: 1.4826 * median absolute deviation."""
    m = median(xs)
    if m is None:
        return None
    return 1.4826 * median([abs(x - m) for x in xs])


def rnd(x: Optional[float], n: int = 3) -> Optional[float]:
    return None if x is None or x != x or x in (math.inf, -math.inf) else round(x, n)


def solve(a: List[List[float]], b: List[float]) -> Optional[List[float]]:
    """Gaussian elimination with partial pivoting; None when singular."""
    n = len(b)
    m = [row[:] + [b[k]] for k, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(c + 1, n):
            f = m[r][c] / m[c][c]
            if f:
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))) / m[r][r]
    return x


def ols(xcols: Sequence[Sequence[float]], y: Sequence[float]) -> Optional[Tuple[List[float], List[float], float]]:
    """Least squares y ~ b0 + sum b_k x_k. -> (coefficients, standard errors, residual sd)."""
    n = len(y)
    k = len(xcols) + 1
    if n <= k:
        return None
    cols = [[1.0] * n] + [list(c) for c in xcols]
    xtx = [[sum(a * b for a, b in zip(cols[i], cols[j])) for j in range(k)] for i in range(k)]
    xty = [sum(a * b for a, b in zip(cols[i], y)) for i in range(k)]
    beta = solve(xtx, xty)
    if beta is None:
        return None
    rss = 0.0
    for r in range(n):
        pred = sum(beta[j] * cols[j][r] for j in range(k))
        rss += (y[r] - pred) ** 2
    s2 = rss / (n - k)
    se = []
    for j in range(k):
        e = [1.0 if i == j else 0.0 for i in range(k)]
        col = solve(xtx, e)
        se.append(math.sqrt(max(0.0, s2 * col[j])) if col else float("nan"))
    return beta, se, math.sqrt(s2)


def origin_slope(dx: Sequence[float], dy: Sequence[float]) -> Optional[Tuple[float, float]]:
    """Through-origin slope dy = s*dx and its standard error."""
    n = len(dx)
    sxx = sum(x * x for x in dx)
    if n < 2 or sxx <= 0:
        return None
    sxy = sum(x * y for x, y in zip(dx, dy))
    s = sxy / sxx
    rss = sum((y - s * x) ** 2 for x, y in zip(dx, dy))
    return s, math.sqrt(max(0.0, rss / ((n - 1) * sxx)))


def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse normal CDF (Acklam's rational approximation)."""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > 1 - 0.02425:
        return -norm_ppf(1 - p)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
