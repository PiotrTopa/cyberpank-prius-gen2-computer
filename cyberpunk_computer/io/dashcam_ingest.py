"""Dashcam ingest: 70mai frames → auto-light model → ``state.vision``.

Optional and off by default. Enabled when ``BACKEND_DASHCAM_URL`` is set
(e.g. ``rtsp://192.168.0.1/liveRTSP/av4``); empty or unset = disabled, and
then nothing is started and the state's ``vision`` section stays at its
defaults (``enabled=False``).

When enabled, one background thread (``dashcam-lights``) runs every
``interval_s`` (5 s, ``BACKEND_DASHCAM_INTERVAL_S``):

    DashcamRtspSource.grab() → light_features.extract() → sun elevation
    → AutoLightModel.update()

and stores the result. The engine thread (the only one that dispatches)
calls :meth:`DashcamLightIngest.drain_actions` on each tick and dispatches
the returned :class:`SetVisionStateAction`. A missing ffmpeg, a sleeping
camera or the hotspot being out of range only make the frame ``None``: the
model then holds its state (or falls back to the sun), ``camera_ok`` goes
False, and nothing raises out of the thread.

Position for the sun elevation: the state has no GPS fix yet. The position
callback (:func:`position_from_state`) looks for a ``gps``/``location``
section with ``lat``/``lon`` (or ``latitude``/``longitude``) on the state,
so a future GPS ingest is picked up automatically; until then the default
from ``BACKEND_HOME_LATLON`` (``"54.35,18.65"``, Gdańsk) is used.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, List, Optional, Tuple

from ..state.actions import Action, SetVisionStateAction
from ..vision.auto_lights import AutoLightModel, LightThresholds
from ..vision.frames import DashcamRtspSource, FrameSource
from ..vision.light_features import LightFeatures, extract
from ..vision.sun import solar_elevation

logger = logging.getLogger(__name__)

DEFAULT_HOME_LATLON: Tuple[float, float] = (54.35, 18.65)   # Gdańsk

LatLon = Tuple[float, float]
PositionFn = Callable[[], Optional[LatLon]]


def parse_latlon(text: Optional[str], default: LatLon = DEFAULT_HOME_LATLON) -> LatLon:
    """``"54.35,18.65"`` → ``(54.35, 18.65)``; ``default`` on anything invalid."""
    if not text:
        return default
    try:
        lat_s, lon_s = text.split(",", 1)
        lat, lon = float(lat_s), float(lon_s)
    except (ValueError, AttributeError):
        logger.warning("BACKEND_HOME_LATLON=%r invalid, using %s", text, default)
        return default
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return default
    return lat, lon


def position_from_state(state) -> Optional[LatLon]:
    """Last GPS fix from the state, if the state has one (it does not yet)."""
    for section in ("gps", "location", "position"):
        s = getattr(state, section, None)
        if s is None:
            continue
        lat = getattr(s, "lat", getattr(s, "latitude", None))
        lon = getattr(s, "lon", getattr(s, "longitude", None))
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return float(lat), float(lon)
    return None


class DashcamLightIngest:
    """Source + model on a worker thread; actions drained on the engine thread."""

    def __init__(self, source: FrameSource, model: Optional[AutoLightModel] = None,
                 interval_s: float = 5.0, position: Optional[PositionFn] = None,
                 default_latlon: LatLon = DEFAULT_HOME_LATLON,
                 extractor: Callable[..., Optional[LightFeatures]] = extract,
                 clock: Callable[[], float] = time.time) -> None:
        self.source = source
        self.model = model or AutoLightModel(LightThresholds())
        self.interval_s = interval_s
        self.position = position
        self.default_latlon = default_latlon
        self._extract = extractor
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: Optional[dict] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._announced = False
        self.polls = 0

    # ── configuration ─────────────────────────────────────────────────────

    @classmethod
    def from_env(cls, position: Optional[PositionFn] = None,
                 env=os.environ) -> Optional["DashcamLightIngest"]:
        """``None`` unless ``BACKEND_DASHCAM_URL`` is set (non-empty)."""
        url = (env.get("BACKEND_DASHCAM_URL") or "").strip()
        if not url:
            return None
        try:
            interval = float(env.get("BACKEND_DASHCAM_INTERVAL_S") or 5.0)
        except ValueError:
            interval = 5.0
        return cls(DashcamRtspSource(url), interval_s=max(1.0, interval),
                   position=position,
                   default_latlon=parse_latlon(env.get("BACKEND_HOME_LATLON")))

    # ── one step (synchronous, testable) ──────────────────────────────────

    def _latlon(self) -> LatLon:
        if self.position is not None:
            try:
                p = self.position()
            except Exception:
                p = None
            if p is not None:
                return p
        return self.default_latlon

    def poll_once(self) -> dict:
        """Grab, extract, decide; store and return the vision fields."""
        self.polls += 1
        now = self._clock()
        features: Optional[LightFeatures] = None
        try:
            frame = self.source.grab()
            if frame is not None:
                features = self._extract(frame.path, t=frame.t)
        except Exception:
            logger.exception("dashcam: frame/feature step failed")
            features = None
        sun: Optional[float]
        try:
            lat, lon = self._latlon()
            sun = solar_elevation(now, lat, lon)
        except Exception:
            sun = None
        d = self.model.update(features, sun, t=features.t if features is not None else now)
        f = d.features
        fields = dict(
            enabled=True, camera_ok=features is not None,
            lights_recommended=d.lights, reason=d.reason, confidence=d.confidence,
            sky_y=_r(f.get("sky_y")), sky_chroma=_r(f.get("sky_chroma")),
            sky_clip=_r(f.get("sky_clip"), 3), sun_elevation=_r(sun, 2),
            updated_at=now,
        )
        if d.changed:
            logger.info("dashcam: lights %s (%s)", "ON" if d.lights else "off", d.reason)
        with self._lock:
            self._pending = fields
        return fields

    def drain_actions(self) -> List[Action]:
        """Engine thread: the newest result as one action (or nothing)."""
        out: List[Action] = []
        if not self._announced:
            self._announced = True
            out.append(SetVisionStateAction(enabled=True))
        with self._lock:
            fields, self._pending = self._pending, None
        if fields is not None:
            out.append(SetVisionStateAction(**fields))
        return out

    # ── thread ────────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.poll_once()
            except Exception:
                logger.exception("dashcam: poll failed")
            wait = self.interval_s - (time.monotonic() - t0)
            self._stop.wait(max(0.1, wait))

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dashcam-lights", daemon=True)
        self._thread.start()
        logger.info("dashcam light ingest started (%s, every %.0f s)",
                    getattr(self.source, "url", self.source), self.interval_s)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None


def _r(v: Optional[float], nd: int = 1) -> Optional[float]:
    return None if v is None else round(float(v), nd)
