"""
Frame pacing of the UI loop: render a frame only when the picture can change.

The Pi Zero cannot afford to compose and push 480x240 at the loop rate when
nothing moves. ``FramePacer`` answers two questions per loop pass:

``loop_fps(now)``
    how fast the loop polls input and steps animations: ``fps_touch`` while
    the screen is used or a panel pulses, else ``fps_scene``.

``due(now, changed, animating, pulsing)``
    whether to render now. The screen reports

    * ``changed``   - its content changed (store snapshot, input, page)
    * ``animating`` - something moves at its own pace (the scene caps its own
      redraws at ``fps_scene``; screens without hints always say True)
    * ``pulsing``   - only a slow decoration moves (a focused / edited panel)

    A frame is due when changed / animating / interacting (at most
    ``fps_touch``), pulsing (at most ``fps_pulse``) or when the last frame is
    older than ``1 / fps_idle`` (anything time-based the hints miss).

Rates are tunables (tuning.py); the pacer itself is pure arithmetic.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from .tuning import Tuning

# A frame counts as on time this early (the loop ticks at about the frame
# rate; without slack a few ms of jitter would skip every other frame).
SLACK = 0.8


class FramePacer:
    def __init__(self, tuning: Optional[Tuning] = None,
                 clock: Callable[[], float] = time.monotonic):
        t = tuning or Tuning()
        self._clock = clock
        self.touch_s = 1.0 / max(0.1, t.fps_touch)
        self.pulse_s = 1.0 / max(0.1, min(t.fps_pulse, t.fps_touch))
        self.idle_s = 1.0 / max(0.01, t.fps_idle)
        self.fps_touch = max(0.1, t.fps_touch)
        self.fps_scene = max(0.1, min(t.fps_scene, t.fps_touch))
        self.touch_hold_s = max(0.0, t.touch_hold_s)
        self.last = float("-inf")
        self._input_t = float("-inf")
        self._poked = True
        self.frames = 0

    # -- events ---------------------------------------------------------------

    def note_input(self, now: Optional[float] = None) -> None:
        """Touch / button / key: render promptly and hold the touch rate."""
        self._input_t = self._clock() if now is None else now
        self._poked = True

    def poke(self) -> None:
        """Content changed outside the screen's own hints (screen switch)."""
        self._poked = True

    def interacting(self, now: float) -> bool:
        return now - self._input_t < self.touch_hold_s

    # -- decisions ------------------------------------------------------------

    def loop_fps(self, now: float, pulsing: bool = False) -> float:
        return self.fps_touch if (pulsing or self.interacting(now)) else self.fps_scene

    def due(self, now: float, changed: bool = False, animating: bool = False,
            pulsing: bool = False) -> bool:
        el = now - self.last
        if el >= self.idle_s:
            return True
        if el < SLACK * self.touch_s:
            return False
        if changed or animating or self._poked or self.interacting(now):
            return True
        return pulsing and el >= SLACK * self.pulse_s

    def rendered(self, now: float) -> None:
        self.last = now
        self._poked = False
        self.frames += 1
