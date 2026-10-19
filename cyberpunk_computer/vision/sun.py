"""Solar elevation from time and position (pure Python, NOAA approximation).

The NOAA Solar Calculator equations (after Meeus, "Astronomical Algorithms"):
geometric mean longitude/anomaly, equation of centre, apparent longitude,
obliquity, declination, equation of time, hour angle. Accuracy is a few
hundredths of a degree for current dates — far inside what the headlight
rule needs (civil twilight at −6°).

The returned elevation is *geometric* (no atmospheric refraction): civil
twilight is defined on the geometric elevation, and refraction only matters
within ~1° of the horizon.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Union

When = Union[float, int, datetime]


def _epoch(when: When) -> float:
    if isinstance(when, datetime):
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)   # naive = UTC
        return when.timestamp()
    return float(when)


def solar_elevation(when: When, lat: float, lon: float) -> float:
    """Sun elevation above the horizon in degrees.

    ``when``: epoch seconds or a ``datetime`` (naive = UTC); ``lat``/``lon``
    in degrees, north and east positive.
    """
    t = _epoch(when)
    jd = t / 86400.0 + 2440587.5
    jc = (jd - 2451545.0) / 36525.0

    l0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360.0
    m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    mr = math.radians(m)
    c = (math.sin(mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
         + math.sin(2 * mr) * (0.019993 - 0.000101 * jc)
         + math.sin(3 * mr) * 0.000289)
    true_long = l0 + c
    omega = math.radians(125.04 - 1934.136 * jc)
    app_long = true_long - 0.00569 - 0.00478 * math.sin(omega)
    mean_obliq = 23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
    obliq = math.radians(mean_obliq + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(obliq) * math.sin(math.radians(app_long)))

    y = math.tan(obliq / 2.0) ** 2
    l0r = math.radians(l0)
    eq_time = 4.0 * math.degrees(
        y * math.sin(2 * l0r) - 2 * e * math.sin(mr)
        + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
        - 0.5 * y * y * math.sin(4 * l0r) - 1.25 * e * e * math.sin(2 * mr))

    minutes_utc = (t % 86400.0) / 60.0
    true_solar = (minutes_utc + eq_time + 4.0 * lon) % 1440.0
    hour_angle = true_solar / 4.0 - 180.0
    latr = math.radians(lat)
    cos_zen = (math.sin(latr) * math.sin(decl)
               + math.cos(latr) * math.cos(decl) * math.cos(math.radians(hour_angle)))
    cos_zen = max(-1.0, min(1.0, cos_zen))
    return 90.0 - math.degrees(math.acos(cos_zen))
