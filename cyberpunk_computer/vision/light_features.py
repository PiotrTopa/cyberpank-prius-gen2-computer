"""Light features of one dashcam frame (docs/DASHCAM_70MAI.md, first drive).

The camera's auto exposure flattens the frame average (whole-frame Y sits at
112–123 in every outdoor segment), so the decisions use the *distribution*:

* ``sky_y``      — mean Y of the top third ("sky band"), 0..255;
* ``sky_u/v``    — mean U/V of the sky band; ``sky_chroma`` is their distance
                   from neutral, ``hypot(U-128, V-128)`` (grey ceiling ≈ 0–6,
                   blue sky 10–30);
* ``sky_clip``   — share (0..1) of sky-band pixels with Y > 250 (sun disc,
                   flare). Implemented exactly, not by proxy: the sky crop is
                   thresholded with ``lutyuv=y='if(gt(val,250),255,0)'`` and the
                   thresholded crop's ``signalstats`` YAVG / 255 is the share;
* ``road_y``     — mean Y of the bottom half (secondary);
* ``y_avg``      — whole-frame mean Y (logged only).

All values come from one ffprobe run (:func:`ffmpeg.signalstats` with four
branches). The sampler CSV (``tools/dashcam_sampler.py``) has no clip column,
so :func:`from_sample_row` sets ``sky_clip=None``.
"""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass
from typing import Mapping, Optional

from . import ffmpeg

#: Y above which a sky pixel counts as clipped.
CLIP_LEVEL = 250

SKY_CROP = "crop=iw:ih/3:0:0"
ROAD_CROP = "crop=iw:ih/2:0:ih/2"
BRANCHES = {
    "full": "",
    "sky": SKY_CROP,
    "road": ROAD_CROP,
    # filtergraph-escaped commas inside the lutyuv expression
    "clip": SKY_CROP + f",lutyuv=y='if(gt(val\\,{CLIP_LEVEL})\\,255\\,0)'",
}


def chroma(u: float, v: float) -> float:
    """Distance of (U, V) from neutral grey (128, 128), 8-bit units."""
    return math.hypot(u - 128.0, v - 128.0)


@dataclass(frozen=True)
class LightFeatures:
    t: float                        # capture time, epoch seconds
    sky_y: float
    sky_u: float
    sky_v: float
    sky_chroma: float
    sky_clip: Optional[float]       # 0..1, None when unknown (CSV rows)
    road_y: Optional[float] = None
    y_avg: Optional[float] = None

    def as_dict(self) -> dict:
        return asdict(self)


def extract(path: str, t: Optional[float] = None,
            timeout: float = 20.0) -> Optional[LightFeatures]:
    """Features of the JPEG at ``path`` (one ffprobe run).

    ``t`` defaults to the file's mtime. Returns ``None`` (never raises) when
    ffprobe is missing or cannot read the file.
    """
    st = ffmpeg.signalstats(path, BRANCHES, timeout=timeout)
    if st is None:
        return None
    try:
        sky, road, full, clip = st["sky"], st["road"], st["full"], st["clip"]
        if t is None:
            t = os.path.getmtime(path)
        return LightFeatures(
            t=float(t),
            sky_y=sky["y_avg"], sky_u=sky["u_avg"], sky_v=sky["v_avg"],
            sky_chroma=chroma(sky["u_avg"], sky["v_avg"]),
            sky_clip=max(0.0, min(1.0, clip["y_avg"] / 255.0)),
            road_y=road.get("y_avg"), y_avg=full.get("y_avg"),
        )
    except (KeyError, OSError, TypeError):
        return None


def _num(row: Mapping, key: str) -> Optional[float]:
    v = row.get(key)
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f < 0 else f     # the sampler writes -1 for a failed stat


def from_sample_row(row: Mapping) -> Optional[LightFeatures]:
    """Features from one ``tools/dashcam_sampler.py`` CSV row (dict).

    ``t`` is the ``epoch`` column; ``sky_clip`` is unknown (``None``) unless
    the row carries a ``sky_clip`` column. ``None`` if the sky values or the
    time are missing.
    """
    t = _num(row, "epoch")
    sky_y, sky_u, sky_v = _num(row, "sky_y"), _num(row, "sky_u"), _num(row, "sky_v")
    if t is None or sky_y is None or sky_u is None or sky_v is None:
        return None
    return LightFeatures(
        t=t, sky_y=sky_y, sky_u=sky_u, sky_v=sky_v,
        sky_chroma=chroma(sky_u, sky_v),
        sky_clip=_num(row, "sky_clip"),
        road_y=_num(row, "road_y"), y_avg=_num(row, "y_avg"),
    )
