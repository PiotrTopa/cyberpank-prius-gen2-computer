"""Frame sources: where the vision package gets its still images from.

A :class:`FrameSource` hands out one JPEG per :meth:`~FrameSource.grab` call
as a :class:`Frame` (file path + capture time, epoch seconds) or ``None`` when
no frame is available. Sources never raise on a missing camera.

* :class:`DashcamRtspSource` — the live 70mai stream
  (``rtsp://192.168.0.1/liveRTSP/av4``, docs/DASHCAM_70MAI.md). One RTSP
  session per grab: the camera serves one client at a time and refuses a new
  DESCRIBE for a few seconds after a teardown, hence the retries.
* :class:`DirectorySource` — offline replay of a folder of timestamped JPEGs
  as written by ``tools/dashcam_sampler.py`` (``YYYYmmdd_HHMMSS.jpg``, local
  time); files without a parsable name use their mtime.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Iterator, List, Optional, Protocol, runtime_checkable

from . import ffmpeg

DEFAULT_DASHCAM_URL = "rtsp://192.168.0.1/liveRTSP/av4"


@dataclass(frozen=True)
class Frame:
    path: str
    t: float          # capture time, epoch seconds (UTC based)


@runtime_checkable
class FrameSource(Protocol):
    def grab(self) -> Optional[Frame]:
        """One frame, or ``None`` if none is available right now."""
        ...


class DashcamRtspSource:
    """One JPEG per call from an RTSP stream via ffmpeg.

    The frame is (over)written to ``<out_dir>/latest.jpg`` (a private temp
    directory by default), so the returned path is valid until the next grab.
    ``retries`` extra attempts are made ``retry_delay_s`` apart (the 70mai
    refuses a new session for a few seconds after the previous one closed).
    """

    def __init__(self, url: str = DEFAULT_DASHCAM_URL, out_dir: Optional[str] = None,
                 retries: int = 2, retry_delay_s: float = 2.0, timeout_s: float = 15.0,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.url = url
        self.out_dir = out_dir or tempfile.mkdtemp(prefix="dashcam-")
        self.retries = retries
        self.retry_delay_s = retry_delay_s
        self.timeout_s = timeout_s
        self._clock = clock
        self._sleep = sleep
        self.failures = 0           # consecutive failed grabs (observability)

    @property
    def path(self) -> str:
        return os.path.join(self.out_dir, "latest.jpg")

    def grab(self) -> Optional[Frame]:
        try:
            os.makedirs(self.out_dir, exist_ok=True)
        except OSError:
            return None
        for attempt in range(1 + max(0, self.retries)):
            if attempt:
                self._sleep(self.retry_delay_s)
            t = self._clock()
            if ffmpeg.grab_jpeg(self.url, self.path, timeout=self.timeout_s):
                self.failures = 0
                return Frame(self.path, t)
        self.failures += 1
        return None


def parse_frame_time(name: str) -> Optional[float]:
    """``20261019_152214.jpg`` (local time) → epoch seconds, else ``None``."""
    stem = os.path.splitext(os.path.basename(name))[0]
    try:
        return time.mktime(time.strptime(stem[:15], "%Y%m%d_%H%M%S"))
    except ValueError:
        return None


class DirectorySource:
    """Replay a folder of JPEGs in name order, one per :meth:`grab`.

    Returns ``None`` once all files were handed out (or if the folder is
    missing). Iterable as well.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        try:
            names = sorted(n for n in os.listdir(path)
                           if n.lower().endswith((".jpg", ".jpeg")))
        except OSError:
            names = []
        self.files: List[str] = [os.path.join(path, n) for n in names]
        self._i = 0

    def grab(self) -> Optional[Frame]:
        if self._i >= len(self.files):
            return None
        f = self.files[self._i]
        self._i += 1
        t = parse_frame_time(f)
        if t is None:
            try:
                t = os.path.getmtime(f)
            except OSError:
                t = time.time()
        return Frame(f, t)

    def __iter__(self) -> Iterator[Frame]:
        while True:
            fr = self.grab()
            if fr is None:
                return
            yield fr
