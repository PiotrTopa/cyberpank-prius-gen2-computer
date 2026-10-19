"""Vision: dashcam frames → light features → automatic-headlight recommendation.

Dependency-free apart from the ffmpeg/ffprobe binaries (no numpy, PIL or
OpenCV). Modules:

* :mod:`.ffmpeg`          — the only place ffmpeg/ffprobe are invoked;
* :mod:`.frames`          — frame sources (live RTSP dashcam, folder replay);
* :mod:`.light_features`  — sky/road band luma, sky chroma, sky clipping;
* :mod:`.sun`             — solar elevation (NOAA approximation);
* :mod:`.auto_lights`     — the headlight state machine + offline replay CLI.

All of these are pure and thread-free; the backend thread lives in
:mod:`cyberpunk_computer.io.dashcam_ingest`. See docs/DASHCAM_70MAI.md.
"""

from .frames import DashcamRtspSource, DirectorySource, Frame, FrameSource
from .light_features import LightFeatures, extract, from_sample_row
from .sun import solar_elevation

__all__ = [
    "AutoLightModel", "LightDecision", "LightThresholds", "replay",
    "DashcamRtspSource", "DirectorySource", "Frame", "FrameSource",
    "LightFeatures", "extract", "from_sample_row", "solar_elevation",
]


def __getattr__(name):
    # auto_lights is imported lazily so ``python -m ...vision.auto_lights``
    # does not import the module twice.
    if name in ("AutoLightModel", "LightDecision", "LightThresholds", "replay"):
        from . import auto_lights
        return getattr(auto_lights, name)
    raise AttributeError(name)
