"""Scene features of one dashcam frame for the environment model.

A superset of :mod:`light_features`, computed in **one** ffprobe run (one
``signalstats`` pass per filtered branch, see :func:`ffmpeg.signalstats`).
The frame is first scaled to the 70mai's 640×360 (``scale=640:360``, a no-op
for our own frames) so that public datasets at 1280×720 give comparable
edge and clip shares.

Bands (rows of the 640×360 frame):

* ``sky_*``   — top third (the light_features "sky band");
* ``road_*``  — bottom half;
* ``b0..b4``  — five horizontal bands of height ih/5 (``bN_y`` = mean Y),
  the coarse vertical luma profile;
* ``sky_l_y`` / ``sky_r_y`` — left / right half of the sky band.

Per band: mean Y/U/V, ``_low`` / ``_high`` = signalstats' 10 % / 90 % Y
percentiles, ``_sat`` = mean saturation. Shares (0..1, from a thresholded
copy of the band; YAVG/255 is then the share of pixels past the threshold):

* ``sky_clip`` / ``full_clip`` — Y > 250 (sun, flare, lamps);
* ``full_dark`` / ``sky_dark`` — Y < 24 (night sky, unlit areas);
* ``edge_full`` / ``edge_sky`` — ``edgedetect`` pixel share (texture:
  wipers/raindrops/fog blur it, ceilings and night skies are flat).

Measured cost: ≈ 90 ms per 640×360 JPEG on the laptop (one ffprobe process,
≈ 40 ms of it process start); the Poco F1 figure is still to be measured.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields
from typing import Dict, Mapping, Optional

from . import ffmpeg
from .light_features import CLIP_LEVEL, LightFeatures, chroma

DARK_LEVEL = 24
PRE = "scale=640:360:flags=area"


def _thr(level: int, above: bool = True) -> str:
    op = "gt" if above else "lt"
    return f"lutyuv=y='if({op}(val\\,{level})\\,255\\,0)'"


_SKY = "crop=iw:ih/3:0:0"
_ROAD = "crop=iw:ih/2:0:ih/2"
_EDGE = "edgedetect=low=0.08:high=0.2"

BRANCHES: Dict[str, str] = {
    "full": "",
    "sky": _SKY,
    "road": _ROAD,
    "skyl": "crop=iw/2:ih/3:0:0",
    "skyr": "crop=iw/2:ih/3:iw/2:0",
    **{f"b{i}": f"crop=iw:ih/5:0:{i}*ih/5" for i in range(5)},
    "sky_clip": _SKY + "," + _thr(CLIP_LEVEL),
    "full_clip": _thr(CLIP_LEVEL),
    "full_dark": _thr(DARK_LEVEL, above=False),
    "sky_dark": _SKY + "," + _thr(DARK_LEVEL, above=False),
    "edge_full": _EDGE,
    "edge_sky": _SKY + "," + _EDGE,
}


@dataclass(frozen=True)
class SceneFeatures:
    t: float
    y_avg: float
    y_low: float
    y_high: float
    sat: float
    sky_y: float
    sky_u: float
    sky_v: float
    sky_chroma: float
    sky_low: float
    sky_high: float
    sky_sat: float
    sky_l_y: float
    sky_r_y: float
    road_y: float
    road_u: float
    road_v: float
    road_low: float
    road_high: float
    road_sat: float
    b0_y: float
    b1_y: float
    b2_y: float
    b3_y: float
    b4_y: float
    sky_clip: float
    full_clip: float
    full_dark: float
    sky_dark: float
    edge_full: float
    edge_sky: float

    def as_dict(self) -> dict:
        return asdict(self)

    def light(self) -> LightFeatures:
        """The :class:`LightFeatures` subset (for :class:`AutoLightModel`)."""
        return LightFeatures(t=self.t, sky_y=self.sky_y, sky_u=self.sky_u, sky_v=self.sky_v,
                             sky_chroma=self.sky_chroma, sky_clip=self.sky_clip,
                             road_y=self.road_y, y_avg=self.y_avg)


FIELDS = tuple(f.name for f in fields(SceneFeatures))


def _share(st: Mapping[str, Mapping[str, float]], name: str) -> float:
    return max(0.0, min(1.0, st[name]["y_avg"] / 255.0))


def from_stats(st: Mapping[str, Mapping[str, float]], t: float) -> SceneFeatures:
    """Build the features from a :func:`ffmpeg.signalstats` result (KeyError if incomplete)."""
    full, sky, road = st["full"], st["sky"], st["road"]
    return SceneFeatures(
        t=float(t),
        y_avg=full["y_avg"], y_low=full["y_low"], y_high=full["y_high"], sat=full["sat_avg"],
        sky_y=sky["y_avg"], sky_u=sky["u_avg"], sky_v=sky["v_avg"],
        sky_chroma=chroma(sky["u_avg"], sky["v_avg"]),
        sky_low=sky["y_low"], sky_high=sky["y_high"], sky_sat=sky["sat_avg"],
        sky_l_y=st["skyl"]["y_avg"], sky_r_y=st["skyr"]["y_avg"],
        road_y=road["y_avg"], road_u=road["u_avg"], road_v=road["v_avg"],
        road_low=road["y_low"], road_high=road["y_high"], road_sat=road["sat_avg"],
        b0_y=st["b0"]["y_avg"], b1_y=st["b1"]["y_avg"], b2_y=st["b2"]["y_avg"],
        b3_y=st["b3"]["y_avg"], b4_y=st["b4"]["y_avg"],
        sky_clip=_share(st, "sky_clip"), full_clip=_share(st, "full_clip"),
        full_dark=_share(st, "full_dark"), sky_dark=_share(st, "sky_dark"),
        edge_full=_share(st, "edge_full"), edge_sky=_share(st, "edge_sky"),
    )


def extract(path: str, t: Optional[float] = None,
            timeout: float = 20.0) -> Optional[SceneFeatures]:
    """Scene features of the image at ``path``; ``None`` (never raises) on failure.

    ``t`` defaults to the file's mtime.
    """
    st = ffmpeg.signalstats(path, BRANCHES, timeout=timeout, pre=PRE)
    if st is None:
        return None
    try:
        if t is None:
            t = os.path.getmtime(path)
        return from_stats(st, t)
    except (KeyError, OSError, TypeError):
        return None


def from_row(row: Mapping) -> Optional[SceneFeatures]:
    """Features from a CSV row (dict of strings) with the :data:`FIELDS` columns.

    ``t`` falls back to an ``epoch`` column, then 0. ``None`` when a value is
    missing or not a number.
    """
    vals = {}
    for name in FIELDS:
        v = row.get(name)
        if name == "t" and (v is None or v == ""):
            v = row.get("epoch") or 0.0
        try:
            vals[name] = float(v)
        except (TypeError, ValueError):
            return None
    return SceneFeatures(**vals)
