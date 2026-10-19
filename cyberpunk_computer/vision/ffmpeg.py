"""The one place where the vision package runs ffmpeg / ffprobe.

ffmpeg is the only external dependency of :mod:`cyberpunk_computer.vision`
(no numpy, PIL or OpenCV). Every call goes through :func:`run`, which never
raises: a missing binary, a timeout or a non-zero exit all come back as
``None`` so a missing camera or a missing ffmpeg can never crash the backend.

Two operations are offered:

* :func:`grab_jpeg` — one JPEG from an RTSP stream (RTSP over TCP, one session
  per call, so the 70mai's single-client server is never held open);
* :func:`signalstats` — ``signalstats`` averages of several crops of one image
  in a single ffprobe run (``split`` into N branches, one output per branch).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

FFMPEG = os.environ.get("VISION_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("VISION_FFPROBE", "ffprobe")


def available(binary: str = FFPROBE) -> bool:
    """Is ``binary`` (default: ffprobe) on the PATH?"""
    return shutil.which(binary) is not None


def run(cmd: Sequence[str], timeout: float) -> Optional[subprocess.CompletedProcess]:
    """Run ``cmd``; ``None`` on a missing binary, timeout or non-zero exit."""
    try:
        r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("vision: %s failed: %s", cmd[0], e)
        return None
    if r.returncode != 0:
        logger.debug("vision: %s exit %s: %s", cmd[0], r.returncode, r.stderr.strip()[:200])
        return None
    return r


def grab_jpeg(url: str, path: str, timeout: float = 20.0, quality: int = 3) -> bool:
    """Grab one frame of ``url`` into ``path`` (JPEG). True on success.

    The frame is written to ``path + '.part.jpg'`` first and renamed, so a reader
    never sees a half-written file.
    """
    tmp = path + ".part.jpg"
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
           "-rtsp_transport", "tcp", "-i", url, "-frames:v", "1",
           "-q:v", str(quality), "-y", tmp]
    ok = run(cmd, timeout) is not None and os.path.exists(tmp) and os.path.getsize(tmp) > 0
    if ok:
        os.replace(tmp, path)
    else:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return ok


def _escape_movie_path(path: str) -> str:
    """Escape a file name for ``movie=<path>`` inside a filtergraph.

    Two levels (ffmpeg-filters "Notes on filtergraph escaping"): the option
    value (``\\ ' :``), then the filtergraph (``\\ ' [ ] , ;``).
    """
    v = path.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    for ch in "\\'[],;":
        v = v.replace(ch, "\\" + ch)
    return v


_TAGS = {"YAVG": "y_avg", "YMIN": "y_min", "YMAX": "y_max", "UAVG": "u_avg", "VAVG": "v_avg",
         "YLOW": "y_low", "YHIGH": "y_high", "SATAVG": "sat_avg"}


def signalstats(path: str, branches: Mapping[str, str],
                timeout: float = 20.0, pre: str = "") -> Optional[Dict[str, Dict[str, float]]]:
    """``signalstats`` of several filtered views of one image, one ffprobe run.

    ``branches`` maps a name to a filter chain applied before ``signalstats``
    (``""`` = the whole frame), e.g. ``{"sky": "crop=iw:ih/3:0:0"}``. Returns
    ``{name: {"y_avg", "y_min", "y_max", "u_avg", "v_avg", "y_low", "y_high",
    "sat_avg"}}`` (8-bit values; ``y_low``/``y_high`` are signalstats' 10 % /
    90 % percentiles), or ``None`` if ffprobe is missing or fails. ``pre`` is
    a filter chain applied once before the split (e.g. a ``scale``).
    """
    names: List[str] = list(branches)
    if not names or not os.path.exists(path):
        return None
    n = len(names)
    labels = "".join(f"[s{i}]" for i in range(n))
    src = f"movie={_escape_movie_path(path)}" + (f",{pre}" if pre else "")
    parts = [f"{src},split={n}{labels}" if n > 1 else f"{src}[s0]"]
    for i, name in enumerate(names):
        chain = branches[name]
        parts.append(f"[s{i}]" + (chain + "," if chain else "") + f"signalstats[out{i}]")
    graph = ";".join(parts)
    cmd = [FFPROBE, "-v", "error", "-f", "lavfi", "-i", graph,
           "-show_entries", "frame=stream_index:frame_tags=" + ",".join(
               f"lavfi.signalstats.{t}" for t in _TAGS),
           "-of", "compact"]
    r = run(cmd, timeout)
    if r is None:
        return None
    out: Dict[str, Dict[str, float]] = {}
    # lines: frame|stream_index=1|tag:lavfi.signalstats.YAVG=180.97|...
    for line in r.stdout.splitlines():
        fields = line.strip().split("|")
        if not fields or fields[0] != "frame":
            continue
        idx: Optional[int] = None
        vals: Dict[str, float] = {}
        for f in fields[1:]:
            if "=" not in f:
                continue
            key, val = f.split("=", 1)
            if key == "stream_index":
                try:
                    idx = int(val)
                except ValueError:
                    idx = None
            elif "lavfi.signalstats." in key:
                tag = key.rsplit(".", 1)[-1]
                if tag in _TAGS:
                    try:
                        vals[_TAGS[tag]] = float(val)
                    except ValueError:
                        pass
        if idx is None and n == 1:
            idx = 0
        if idx is not None and 0 <= idx < n and names[idx] not in out:
            out[names[idx]] = vals
    return out if len(out) == n else None
