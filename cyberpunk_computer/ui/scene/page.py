"""
ScenePage: the centre column of the main screen.

    ┌──────────────────────────┐  y = 1
    │  scene 238 x 216         │  parked: the car / driving: the road
    ├──────────────────────────┤  y = 217
    │  strip 194 x 21  │ dots  │  cruise slot, HV power, history, SOC
    └──────────────────────────┘  y = 239

``update()`` steps the director every UI frame (cheap arithmetic);
``render_scene()`` and ``render_strip()`` blit cached surfaces that are
re-drawn only when needed; ``needs_frame()`` tells the frame pacer whether
either would change. The strip is shown on every page of the main screen,
the scene on the home page.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import pygame

from ..colors import COLORS
from .director import SceneDirector
from .inputs import FEED, SceneFeed, SceneInputs, read_inputs
from .powerbox import PowerHistory, StatusStrip
from .render import SceneRenderer
from .tuning import Tuning, load as load_tuning

SCENE_H = 216
STRIP_H = 21
DOTS_W = 44            # room of the page dots at the right end of the strip
PREWARM_S = 0.4


class ScenePage:
    def __init__(self, x: int, y: int, width: int, feed: Optional[SceneFeed] = FEED,
                 clock=time.monotonic, tuning: Optional[Tuning] = None):
        # the tunables (tuning.py): defaults + the optional JSON overrides
        self.tuning = tuning if tuning is not None else load_tuning()
        self.x, self.y, self.width = x, y, width
        self._clock = clock
        self.feed = feed
        self.director = SceneDirector(self.tuning)
        self.history = PowerHistory(clock)
        self.inputs = SceneInputs()
        self._state: Any = None
        self._inputs_for: Any = None
        self._feed_seen = -1
        # pygame objects are created on first render (keeps start-up light)
        self.scene: Optional[SceneRenderer] = None
        self.strip: Optional[StatusStrip] = None
        self._warm_t = 0.0
        self._hist_seen = -1

    # -- data -------------------------------------------------------------------

    def set_state(self, state: Any) -> None:
        """New AppState snapshot (store subscription)."""
        self._state = state
        energy = getattr(state, "energy", None)
        self.history.add(getattr(energy, "battery_power_kw", None))

    def _refresh_inputs(self) -> None:
        feed_n = self.feed.received if self.feed is not None else 0
        if self._state is not self._inputs_for or feed_n != self._feed_seen:
            self._inputs_for = self._state
            self._feed_seen = feed_n
            if self._state is not None:
                self.inputs = read_inputs(self._state, self.feed)

    # -- frame --------------------------------------------------------------------

    def update(self, dt: float) -> None:
        self._refresh_inputs()
        d = self.director
        d.update(self.inputs, dt)
        # While nothing moves, pre-render the camera-move sprites one at a
        # time (one car render per PREWARM_S; see SceneRenderer.prewarm).
        self._warm_t += dt
        if self._warm_t >= PREWARM_S and self.scene is not None:
            self._warm_t = 0.0
            if d.view_raw in (0.0, 1.0) and not any(d.door_step):
                self.scene.prewarm(self.inputs)
        if self._state is not None:
            # keep the history ticking through quiet seconds
            energy = getattr(self._state, "energy", None)
            self.history.add(getattr(energy, "battery_power_kw", None))

    def needs_frame(self, home: bool = True) -> bool:
        """Whether the next render would show something new: the scene would
        re-draw (home page) or the power history moved on."""
        if self.strip is None or self.history.version != self._hist_seen:
            return True
        if not home:
            return False
        return self.scene is None or self.scene.due(self.director, self.inputs, self._clock())

    def render_scene(self, surface: pygame.Surface) -> None:
        if self.scene is None:
            self.scene = SceneRenderer((self.width, SCENE_H), tuning=self.tuning)
        surface.blit(self.scene.render(self.director, self.inputs, self._clock()),
                     (self.x, self.y))

    def render_strip(self, surface: pygame.Surface) -> None:
        if self.strip is None:
            self.strip = StatusStrip((self.width - DOTS_W, STRIP_H), self.history)
        st = self._state
        inp = self.inputs
        self._hist_seen = self.history.version
        sep_y = self.y + SCENE_H
        pygame.draw.line(surface, COLORS["border_normal"],
                         (self.x, sep_y), (self.x + self.width - 1, sep_y))
        energy = getattr(st, "energy", None)
        soc = getattr(energy, "battery_soc", None)
        bars = getattr(getattr(st, "avc", None), "battery_bars", None)
        if bars is None:
            bars = getattr(getattr(st, "dynamics", None), "soc_bars", 0) or None
        tb = getattr(getattr(st, "display", None), "power_chart_time_base", 60) or 60
        strip = self.strip.render(
            getattr(energy, "battery_power_kw", None), soc if soc else None, bars,
            inp.ice_running, inp.ready, int(tb),
            inp.cruise_active, inp.cruise_main, inp.cruise_set_kmh, inp.cruise_mem_kmh,
            inp.cruise_gap)
        surface.blit(strip, (self.x, sep_y + 1))

    def render(self, surface: pygame.Surface) -> None:
        self.render_scene(surface)
        self.render_strip(surface)
