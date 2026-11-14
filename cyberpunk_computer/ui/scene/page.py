"""
ScenePage: the home page of the main screen's centre column.

    ┌──────────────────────────┐  y = 1
    │  scene 238 x 168         │  parked: the car / driving: the road
    ├──────────────────────────┤  y = 169
    │  power box 238 x 54      │  HV power, history, SOC
    └──────────────────────────┘  y = 224 (bottom bar below: cruise, pages)

``update()`` steps the director every UI frame (cheap arithmetic);
``render()`` blits two cached surfaces and re-draws them only when needed.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import pygame

from ..colors import COLORS
from .director import SceneDirector
from .inputs import FEED, SceneFeed, SceneInputs, read_inputs
from .powerbox import PowerBox, PowerHistory
from .render import SceneRenderer

SCENE_H = 168
BOX_H = 54
PREWARM_S = 0.4


class ScenePage:
    def __init__(self, x: int, y: int, width: int, feed: Optional[SceneFeed] = FEED,
                 clock=time.monotonic):
        self.x, self.y, self.width = x, y, width
        self._clock = clock
        self.feed = feed
        self.director = SceneDirector()
        self.history = PowerHistory(clock)
        self.inputs = SceneInputs()
        self._state: Any = None
        self._inputs_for: Any = None
        self._feed_seen = -1
        # pygame objects are created on first render (keeps start-up light)
        self.scene: Optional[SceneRenderer] = None
        self.box: Optional[PowerBox] = None
        self._warm_t = 0.0

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

    def render(self, surface: pygame.Surface) -> None:
        if self.scene is None:
            self.scene = SceneRenderer((self.width, SCENE_H))
            self.box = PowerBox((self.width, BOX_H), self.history)
        st = self._state
        surface.blit(self.scene.render(self.director, self.inputs, self._clock()),
                     (self.x, self.y))
        sep_y = self.y + SCENE_H
        pygame.draw.line(surface, COLORS["border_normal"],
                         (self.x, sep_y), (self.x + self.width - 1, sep_y))
        energy = getattr(st, "energy", None)
        soc = getattr(energy, "battery_soc", None)
        bars = getattr(getattr(st, "avc", None), "battery_bars", None)
        if bars is None:
            bars = getattr(getattr(st, "dynamics", None), "soc_bars", 0) or None
        tb = getattr(getattr(st, "display", None), "power_chart_time_base", 60) or 60
        box = self.box.render(
            getattr(energy, "battery_power_kw", None), soc if soc else None, bars,
            self.inputs.ice_running, self.inputs.ready, int(tb))
        surface.blit(box, (self.x, sep_y + 1))
