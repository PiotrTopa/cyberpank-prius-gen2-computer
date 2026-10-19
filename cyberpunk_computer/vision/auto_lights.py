"""Automatic-headlight model: sun elevation + dashcam sky features, no ML.

Rule (docs/DASHCAM_70MAI.md, "Cheap auto-light rule, first proposal"), fed
with one :class:`LightFeatures` sample every ~5 s and, optionally, the sun
elevation for the same instant:

Each step first appends the sample to a sliding window (``window_s`` = 30 s)
and takes the **median** of ``sky_y``, ``sky_chroma``, ``sky_clip`` and
``road_y`` over it. Then, in this order:

1. **Twilight** — sun elevation < ``twilight_deg`` (−6°, civil twilight):
   lights ON at once, camera ignored. reason ``twilight``.
2. **No camera** — no sample in the window: keep the current state
   (``no-camera-hold``); if there is none yet, sun known → OFF
   (``sun-only``), sun unknown → ON (``no-data``, the safe side).
3. **Clipped sky** — median ``sky_clip`` > ``clip_hold`` (5 %): the sun or
   strong flare is in the picture and auto exposure darkens the rest, so a
   *dark* reading is not trusted: the ON candidate is suppressed and the
   state is held (``clip-hold``). The OFF direction stays open by default
   (``clip_blocks_off=False``): on the 2026-10-19 frames the open sunny sky
   is 10–40 % clipped almost all the time, so a full hold (the doc's literal
   rule, ``clip_blocks_off=True``) kept the garage's ON state for the whole
   sunny drive. With no state yet, a clipped sky bootstraps to OFF.
4. **Hysteresis** on the medians:

   * ON candidate:  ``sky_y < sky_y_on`` (120) **and** ``sky_chroma <
     chroma_on`` (8) — garage, tunnel, night, dark overcast;
   * OFF candidate: ``sky_y > sky_y_off`` (150) **or** ``sky_chroma >
     chroma_off`` (12) — daylight;
   * neither (the 120–150 / 8–12 dead band): keep the state, reset timer.

   A candidate opposite to the current state must hold continuously for
   ``on_persist_s`` (5 s) / ``off_persist_s`` (20 s) — measured between
   sample timestamps — before the state switches (reasons ``dark-sky`` /
   ``daylight``; while waiting ``pending-on`` / ``pending-off``). A
   candidate equal to the state just confirms it.
   **Bootstrap**: the very first camera decision takes the candidate at once
   (nothing to protect against flapping yet); in the dead band it starts ON.

Persistence timers reset whenever rules 1–3 take over or the candidate
disappears. ``confidence`` is a heuristic 0..1: 1.0 for twilight, 0.5 for
holds/pending, and 0.5–1.0 for camera decisions scaled by how far the
medians are past the thresholds (30 Y units or the chroma threshold itself
= full margin). It is for display, not for the decision.

Thresholds are placeholders from one sunny drive; dusk, night, rain and
overcast drives will move them (see the doc). Evaluate a drive offline::

    python -m cyberpunk_computer.vision.auto_lights --csv samples.csv
    python -m cyberpunk_computer.vision.auto_lights --frames DIR   # needs ffprobe

Extension point (scene understanding — signs, lights of other cars, rain):
add further feature producers next to :mod:`light_features` and feed their
verdicts in as extra inputs of :meth:`AutoLightModel.update`; this model
stays the arbiter for the headlight decision.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Iterable, List, Optional, Sequence, Union

from .light_features import LightFeatures, from_sample_row
from .sun import solar_elevation

DEFAULT_LATLON = (54.35, 18.65)   # Gdańsk


@dataclass(frozen=True)
class LightThresholds:
    """Placeholders from the 2026-10-19 drive (docs/DASHCAM_70MAI.md)."""
    sky_y_on: float = 120.0        # lights on below (with chroma_on)
    sky_y_off: float = 150.0       # lights off above (or chroma_off)
    chroma_on: float = 8.0
    chroma_off: float = 12.0
    clip_hold: float = 0.05        # sky share > 250 above which the state is held
    clip_blocks_off: bool = False  # True = also hold against switching OFF (doc's literal rule)
    on_persist_s: float = 5.0
    off_persist_s: float = 20.0
    twilight_deg: float = -6.0     # civil twilight
    window_s: float = 30.0         # median window


@dataclass(frozen=True)
class LightDecision:
    t: float
    lights: bool
    reason: str
    confidence: float
    features: Dict[str, Optional[float]] = field(default_factory=dict)
    sun_elevation: Optional[float] = None
    changed: bool = False          # state differs from the previous step's


def _median(vals: List[float]) -> Optional[float]:
    return statistics.median(vals) if vals else None


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


class AutoLightModel:
    """Stateful, pure (no threads, no I/O) headlight recommendation."""

    def __init__(self, thresholds: Optional[LightThresholds] = None) -> None:
        self.th = thresholds or LightThresholds()
        self.lights: Optional[bool] = None
        self._window: Deque[LightFeatures] = deque()
        self._pending: Optional[bool] = None       # target state waiting to persist
        self._pending_since: Optional[float] = None
        self.last: Optional[LightDecision] = None

    # ── internals ─────────────────────────────────────────────────────────

    def _medians(self) -> Optional[Dict[str, Optional[float]]]:
        if not self._window:
            return None
        clips = [s.sky_clip for s in self._window if s.sky_clip is not None]
        roads = [s.road_y for s in self._window if s.road_y is not None]
        return {
            "sky_y": _median([s.sky_y for s in self._window]),
            "sky_chroma": _median([s.sky_chroma for s in self._window]),
            "sky_clip": _median(clips),
            "road_y": _median(roads),
            "n": float(len(self._window)),
        }

    def _reset_pending(self) -> None:
        self._pending = None
        self._pending_since = None

    def _decide(self, t: float, lights: bool, reason: str, conf: float,
                med: Optional[dict], sun: Optional[float]) -> LightDecision:
        changed = self.lights is not None and lights != self.lights
        self.lights = lights
        d = LightDecision(t=t, lights=lights, reason=reason, confidence=round(conf, 3),
                          features=dict(med or {}), sun_elevation=sun, changed=changed)
        self.last = d
        return d

    # ── public API ────────────────────────────────────────────────────────

    def reset(self) -> None:
        self.lights = None
        self._window.clear()
        self._reset_pending()
        self.last = None

    def update(self, features: Optional[LightFeatures] = None,
               sun_elevation: Optional[float] = None,
               t: Optional[float] = None) -> LightDecision:
        """One step. ``t`` defaults to the sample time (or now without one)."""
        th = self.th
        if t is None:
            t = features.t if features is not None else time.time()
        if features is not None:
            self._window.append(features)
        while self._window and self._window[0].t <= t - th.window_s:
            self._window.popleft()
        med = self._medians()
        sun = sun_elevation

        # 1. twilight
        if sun is not None and sun < th.twilight_deg:
            self._reset_pending()
            return self._decide(t, True, "twilight", 1.0, med, sun)

        # 2. no camera data in the window
        if med is None:
            self._reset_pending()
            if self.lights is not None:
                return self._decide(t, self.lights, "no-camera-hold", 0.3, med, sun)
            if sun is not None:
                return self._decide(t, False, "sun-only", 0.3, med, sun)
            return self._decide(t, True, "no-data", 0.0, med, sun)

        sky_y, chroma, clip = med["sky_y"], med["sky_chroma"], med["sky_clip"]

        # 3. sun / flare in the sky band
        clipped = clip is not None and clip > th.clip_hold
        if clipped and (self.lights is None or th.clip_blocks_off or not self.lights):
            self._reset_pending()
            if self.lights is None:
                return self._decide(t, False, "clip-bootstrap", 0.5, med, sun)
            return self._decide(t, self.lights, "clip-hold", 0.5, med, sun)

        # 4. hysteresis (a clipped sky never yields an ON candidate)
        on_cand = sky_y < th.sky_y_on and chroma < th.chroma_on and not clipped
        off_cand = sky_y > th.sky_y_off or chroma > th.chroma_off
        on_margin = min((th.sky_y_on - sky_y) / 30.0, (th.chroma_on - chroma) / th.chroma_on)
        off_margin = max((sky_y - th.sky_y_off) / 30.0, (chroma - th.chroma_off) / th.chroma_off)

        if self.lights is None:      # bootstrap: nothing to protect yet
            self._reset_pending()
            if off_cand:
                return self._decide(t, False, "bootstrap-daylight",
                                    0.5 + 0.5 * _clamp01(off_margin), med, sun)
            if on_cand:
                return self._decide(t, True, "bootstrap-dark-sky",
                                    0.5 + 0.5 * _clamp01(on_margin), med, sun)
            return self._decide(t, True, "bootstrap-dead-band", 0.5, med, sun)

        if not on_cand and not off_cand:
            self._reset_pending()
            return self._decide(t, self.lights, "clip-hold" if clipped else "dead-band-hold",
                                0.5, med, sun)

        target = on_cand            # True = lights on wanted
        if target == self.lights:
            self._reset_pending()
            reason = "dark-sky" if target else "daylight"
            margin = on_margin if target else off_margin
            return self._decide(t, target, reason, 0.5 + 0.5 * _clamp01(margin), med, sun)

        if self._pending != target:
            self._pending, self._pending_since = target, t
        persist = th.on_persist_s if target else th.off_persist_s
        if t - self._pending_since >= persist:
            self._reset_pending()
            reason = "dark-sky" if target else "daylight"
            margin = on_margin if target else off_margin
            return self._decide(t, target, reason, 0.5 + 0.5 * _clamp01(margin), med, sun)
        return self._decide(t, self.lights, "pending-on" if target else "pending-off",
                            0.5, med, sun)


SunFn = Callable[[float], Optional[float]]


def sun_at(lat: float, lon: float) -> SunFn:
    return lambda t: solar_elevation(t, lat, lon)


def replay(rows: Iterable[Union[Dict, LightFeatures]],
           thresholds: Optional[LightThresholds] = None,
           sun: Optional[SunFn] = None) -> List[LightDecision]:
    """Run a fresh model over ``rows`` (sampler CSV dicts or LightFeatures).

    ``sun`` maps a sample time to a sun elevation (e.g. :func:`sun_at`);
    ``None`` = camera only. Rows without usable sky values are skipped.
    Returns one :class:`LightDecision` per usable row.
    """
    model = AutoLightModel(thresholds)
    out: List[LightDecision] = []
    for row in rows:
        f = row if isinstance(row, LightFeatures) else from_sample_row(row)
        if f is None:
            continue
        out.append(model.update(f, sun(f.t) if sun else None, t=f.t))
    return out


def transitions(timeline: Sequence[LightDecision]) -> List[LightDecision]:
    """The first decision plus every decision where the state changed."""
    return [d for i, d in enumerate(timeline) if i == 0 or d.changed]


# ── CLI ───────────────────────────────────────────────────────────────────

def _fmt_t(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def _load_frames(path: str) -> List[LightFeatures]:
    from .frames import DirectorySource
    from .light_features import extract
    feats = []
    for fr in DirectorySource(path):
        f = extract(fr.path, t=fr.t)
        if f is not None:
            feats.append(f)
    return feats


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m cyberpunk_computer.vision.auto_lights",
        description="Replay the auto-light model over a recorded drive.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="samples.csv from tools/dashcam_sampler.py")
    src.add_argument("--frames", help="folder of YYYYmmdd_HHMMSS.jpg frames (needs ffprobe; adds sky_clip)")
    ap.add_argument("--latlon", default=f"{DEFAULT_LATLON[0]},{DEFAULT_LATLON[1]}",
                    help="position for the sun elevation (default Gdańsk)")
    ap.add_argument("--no-sun", action="store_true", help="camera only")
    ap.add_argument("--all", action="store_true", help="print every decision, not only changes")
    args = ap.parse_args(argv)

    if args.csv:
        with open(args.csv, newline="") as f:
            rows: List = list(csv.DictReader(f))
    else:
        rows = _load_frames(args.frames)
        if not rows:
            print("no frames could be read (ffprobe missing?)", file=sys.stderr)
            return 1
    sun = None
    if not args.no_sun:
        lat, lon = (float(x) for x in args.latlon.split(","))
        sun = sun_at(lat, lon)
    timeline = replay(rows, sun=sun)
    show = timeline if args.all else transitions(timeline)
    print("time                 lights reason              conf  sky_y  chroma  clip   sun")
    for d in show:
        ft = d.features
        clip = ft.get("sky_clip")
        print(f"{_fmt_t(d.t)}  {'ON ' if d.lights else 'off'}    {d.reason:<19} {d.confidence:4.2f}"
              f"  {ft.get('sky_y') or 0:5.1f}  {ft.get('sky_chroma') or 0:6.1f}"
              f"  {'  -  ' if clip is None else f'{clip:5.3f}'}"
              f"  {'-' if d.sun_elevation is None else f'{d.sun_elevation:5.1f}'}")
    on = sum(1 for d in timeline if d.lights)
    print(f"{len(timeline)} decisions, {on} ON / {len(timeline) - on} off, "
          f"{len(transitions(timeline)) - 1} switches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
