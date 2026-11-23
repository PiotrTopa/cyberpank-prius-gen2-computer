"""
ScenePage: the centre column of the main screen.

    ┌──────────────────────────┐  y = 1
    │  scene 238 x 216         │  parked: the car / driving: the road
    ├──────────────────────────┤  y = 217
    │  strip 194 x 21  │ dots  │  cruise slot, HV power, history, SOC
    └──────────────────────────┘  y = 239

While the road view is open a small button in the scene's bottom-right corner
switches the scene rectangle to the radar view (radar.py, an ATC-style plan of
the tracked road users) and back: ``RDR`` on the road view, ``ROAD`` on the
radar view, one tap toggles (``MainScreen`` registers the touch zone,
``radar_button_hit_rect``). The choice is kept by the page for the session
(not across restarts); with the radar view chosen, the road view comes back
after ``radar_fallback_s`` without radar data, and the radar view returns with
the data. Parked (the car view), the button is not shown.

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
from .alerts import ALERT_FEED, AlertBanner, AlertFeed
from .director import VIEW_STEPS, SceneDirector
from .inputs import FEED, SceneFeed, SceneInputs, read_inputs, read_perception
from .powerbox import PowerHistory, StatusStrip
# the radar-view button's geometry (light: the main screen imports it at start-up for the touch zone)
from .radar_button import BTN_H, BTN_W, radar_button_hit_rect, radar_button_rect  # noqa: F401
from .render import SceneRenderer
from .tuning import Tuning, load as load_tuning

SCENE_H = 216
STRIP_H = 21
DOTS_W = 44            # room of the page dots at the right end of the strip
PREWARM_S = 0.4


class ScenePage:
    def __init__(self, x: int, y: int, width: int, feed: Optional[SceneFeed] = FEED,
                 clock=time.monotonic, tuning: Optional[Tuning] = None,
                 alerts: Optional[AlertFeed] = ALERT_FEED):
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
        # alert banner in the strip row + real-speed chip (scene/alerts.py)
        self.alerts = alerts
        self.banner: Optional[AlertBanner] = None
        self._alert_drawn: Optional[tuple] = None
        self._chip_drawn: Optional[tuple] = None
        # radar view (radar.py): the driver's choice, kept for the session
        self.radar_on = False
        self.radar = None                    # RadarView, built with the scene renderer
        self._radar_quiet = 0.0              # seconds the chosen radar view has had no radar data
        self._radar_fallen = False           # ... so the road view is shown instead
        self._btn_drawn: Optional[tuple] = None
        self._btn_cache: dict = {}

    # -- data -------------------------------------------------------------------

    def set_state(self, state: Any) -> None:
        """New AppState snapshot (store subscription)."""
        self._state = state
        energy = getattr(state, "energy", None)
        self.history.add(getattr(energy, "battery_power_kw", None))

    def _refresh_inputs(self) -> None:
        feed_n = self.feed.received if self.feed is not None else 0
        if self._state is not self._inputs_for:
            self._inputs_for = self._state
            self._feed_seen = feed_n
            if self._state is not None:
                self.inputs = read_inputs(self._state, self.feed, self.tuning.map_limit)
        elif feed_n != self._feed_seen:
            # a road event alone (5-10/s): only the perception fields change
            self._feed_seen = feed_n
            if self._state is not None:
                self.inputs = self.inputs._replace(
                    **read_perception(self._state, self.feed, self.tuning.map_limit))

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
        self._update_radar(dt)

    # -- radar view ---------------------------------------------------------------

    def radar_button_visible(self) -> bool:
        """The button is there while the road view is fully open (not parked, not mid camera move)."""
        return bool(self.tuning.radar_view) and self.director.view_step == VIEW_STEPS

    def radar_shown(self) -> bool:
        """The scene rectangle shows the radar view now."""
        return self.radar_on and not self._radar_fallen and self.radar_button_visible()

    def toggle_radar(self) -> None:
        """One tap on the button: road view <-> radar view (the choice stays for the session)."""
        if not self.radar_button_visible() and not self.radar_on:
            return
        self.radar_on = not self.radar_on
        self._radar_quiet = 0.0
        self._radar_fallen = False

    def _update_radar(self, dt: float) -> None:
        if not self.radar_on:
            return
        from .radar import RADAR_STALE_S
        d = self.director
        if d.vision == "live" and d.radar_age <= RADAR_STALE_S:
            self._radar_quiet, self._radar_fallen = 0.0, False
        else:
            self._radar_quiet += dt
            if self._radar_quiet > float(self.tuning.radar_fallback_s):
                self._radar_fallen = True
        if self.radar is not None and self.radar_shown():
            self.radar.update(d, self.inputs, dt)

    def _button_sig(self) -> Optional[tuple]:
        if not self.radar_button_visible():
            return None
        return (self.radar_shown(), self.radar_on)

    def _button_surface(self, sig: tuple) -> pygame.Surface:
        surf = self._btn_cache.get(sig)
        if surf is None:
            shown, on = sig
            pal = self.scene.pal
            surf = pygame.Surface((BTN_W, BTN_H))
            surf.fill((0, 0, 0))
            if shown:
                label, frame, text = "ROAD", pal["lane_mid"], pal["value"]
            elif on:
                # chosen, but no radar data: the road view is back; the button says the choice still stands
                surf.fill(COLORS["cyan_dark"])
                label, frame, text = "RDR", pal["dim"], pal["dim"]
            else:
                label, frame, text = "RDR", pal["lane_mid"], pal["value"]
            pygame.draw.rect(surf, frame, (0, 0, BTN_W, BTN_H), 1)
            t = self.scene.text(label, 14, text)
            surf.blit(t, ((BTN_W - t.get_width()) // 2, (BTN_H - 10) // 2 - 1))
            self._btn_cache[sig] = surf
        return surf

    def _alert_now(self):
        return self.alerts.current() if self.alerts is not None else None

    @staticmethod
    def _chip_sig(a) -> Optional[tuple]:
        chip = a.get("chip") if a else None
        return (chip.get("real_kmh"),) if isinstance(chip, dict) and chip.get("real_kmh") is not None else None

    def needs_frame(self, home: bool = True) -> bool:
        """Whether the next render would show something new: the scene would
        re-draw (home page), the power history moved on, or the alert banner
        appeared / changed / went."""
        if self.strip is None or self.history.version != self._hist_seen:
            return True
        a = self._alert_now()
        if AlertBanner.signature(a) != self._alert_drawn:
            return True
        if home and self._chip_sig(a) != self._chip_drawn:
            return True
        if not home:
            return False
        if self._button_sig() != self._btn_drawn:
            return True
        if self.radar_shown():
            return self.radar is None or self.radar.due(self.director, self.inputs, self._clock())
        return self.scene is None or self.scene.due(self.director, self.inputs, self._clock())

    def render_scene(self, surface: pygame.Surface) -> None:
        if self.scene is None:
            self.scene = SceneRenderer((self.width, SCENE_H), tuning=self.tuning)
        btn = self._button_sig()
        if btn != self._btn_drawn:
            # switched: the picture coming back is redrawn from what it shows now
            self.scene.invalidate()
            if self.radar is not None:
                self.radar.invalidate()
        if btn is not None and btn[0]:
            if self.radar is None:
                from .radar import RadarView
                self.radar = RadarView(self.scene, (self.width, SCENE_H))
                self.radar.keepout = [pygame.Rect(radar_button_rect(self.width)).inflate(4, 4)]
            pic = self.radar.render(self.director, self.inputs, self._clock())
        else:
            pic = self.scene.render(self.director, self.inputs, self._clock())
        surface.blit(pic, (self.x, self.y))
        self._btn_drawn = btn
        if btn is not None:
            bx, by, _, _ = radar_button_rect(self.width)
            surface.blit(self._button_surface(btn), (self.x + bx, self.y + by))
        a = self._alert_now()
        chip_sig = self._chip_sig(a)
        self._chip_drawn = chip_sig
        if chip_sig is not None:
            if self.banner is None:
                self.banner = AlertBanner((self.width - DOTS_W, STRIP_H))
            chip = self.banner.chip(chip_sig[0])
            if chip is not None:
                # under the limit sign, right edge of the scene
                surface.blit(chip, (self.x + self.width - chip.get_width() - 3, self.y + 33))

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
        a = self._alert_now()
        self._alert_drawn = AlertBanner.signature(a)
        if a is not None:
            if self.banner is None:
                self.banner = AlertBanner((self.width - DOTS_W, STRIP_H))
            surface.blit(self.banner.render(a), (self.x, sep_y + 1))

    def alert_rect(self):
        """(x, y, w, h) of the banner in screen coordinates (the strip row)."""
        return (self.x, self.y + SCENE_H + 1, self.width - DOTS_W, STRIP_H)

    def alert_shown(self) -> bool:
        return self._alert_now() is not None

    def render(self, surface: pygame.Surface) -> None:
        self.render_scene(surface)
        self.render_strip(surface)
