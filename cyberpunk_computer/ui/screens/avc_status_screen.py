"""
AVC-LAN status screen (read-only diagnostics).

Shows what the backend's AVC-LAN ingest (gateway fw 3.x, ``comm.avc``) has
made of the bus: link liveness, the display's active screen, nav emulation,
touch, and which signals currently come from AVC. Everything is read from
``AppState.avc`` / ``climate`` / ``energy``; nothing is decoded here.

Access: ENGINE menu -> AVC-LAN STATUS

Layout (480x240):
┌──────────────────────────────────────────────────────────┐
│  AVC-LAN STATUS                                  LIVE 0s │
├────────────────────────────┬─────────────────────────────┤
│  DISPLAY / NAV             │  SIGNALS                    │
│  SCREEN       5F ENERGY    │  OUTSIDE            22°C    │
│  NAV EMU      CONFIGURED   │  CLIMATE STATUS      AVC    │
│  NAV REG      YES          │  ENERGY FLOW         AVC    │
│  NAV SCREEN   --           │  BATT BARS             4    │
│  LIGHTS       ON           │  CLIMATE RAW                │
│  TOUCH        UP 120,88    │   00 10 40 ..               │
│  LAST BTN     ...          │  ENERGY RAW                 │
│  CLIMATE Q    0            │   00 AC 00                  │
├────────────────────────────┴─────────────────────────────┤
│ < BACK                                                   │
└──────────────────────────────────────────────────────────┘

The page is drawn into a cached surface and only re-drawn when one of the
state slices it shows is replaced (frozen dataclasses: identity check) or the
link age crosses a whole second, so a steady frame allocates nothing.
"""

import time
from typing import List, Optional, Tuple

import pygame

from .base import Screen
from ..colors import COLORS
from ..fonts import get_font, get_mono_font, get_title_font
from ...input.manager import InputEvent as IE

#: Link age (s) after which the AVC link is shown as STALE.
LINK_STALE_S = 5.0


def _yes_no(v: Optional[bool], yes: str = "YES", no: str = "NO") -> str:
    if v is None:
        return "--"
    return yes if v else no


class AVCStatusScreen(Screen):
    """Read-only AVC-LAN diagnostics page built on ``AppState.avc``."""

    HEADER_H = 22
    FOOTER_H = 22
    ROW_H = 14
    PAD = 8

    def __init__(self, size: Tuple[int, int], app=None, store=None):
        super().__init__(size, app)
        self._store = store
        self._last_activity = time.time()
        self._canvas = pygame.Surface(size)
        # dirty tracking (identity of the frozen state slices + age bucket)
        self._ref_avc = None
        self._ref_climate = None
        self._ref_energy = None
        self._age_key = -2
        self._accent = None

    # ─── Lifecycle / input ────────────────────────────────────────────

    def on_enter(self) -> None:
        self._last_activity = time.time()
        self._ref_avc = None   # force a redraw

    def _get_timeout(self) -> float:
        if self.app and hasattr(self.app, "config"):
            return self.app.config.timeout_screen_exit
        return 30.0

    def update(self, dt: float) -> None:
        super().update(dt)
        if time.time() - self._last_activity > self._get_timeout():
            self._exit()

    def _exit(self) -> None:
        if self.app:
            self.app.pop_screen()

    def handle_input(self, event) -> bool:
        self._last_activity = time.time()
        if event in (IE.PRESS_LIGHT, IE.PRESS_STRONG, IE.BACK):
            self._exit()
            return True
        return False

    # ─── Rendering ────────────────────────────────────────────────────

    def _link_age(self, avc) -> int:
        """Whole seconds since the last AVC frame, -1 = never seen."""
        if not avc.last_frame_time:
            return -1
        return max(0, int(time.time() - avc.last_frame_time))

    def render(self, surface: pygame.Surface) -> None:
        if self._store is None:
            surface.fill(COLORS["bg_dark"])
            return
        st = self._store.state
        age = self._link_age(st.avc)
        # Ages past the stale threshold only change the header digits; cap
        # the bucket so a dead link does not redraw every second forever.
        age_key = min(age, 99)
        if (st.avc is not self._ref_avc or st.climate is not self._ref_climate
                or st.energy is not self._ref_energy or age_key != self._age_key
                or COLORS["cyan"] != self._accent):
            self._ref_avc, self._ref_climate, self._ref_energy = st.avc, st.climate, st.energy
            self._age_key = age_key
            self._accent = COLORS["cyan"]
            self._draw(st, age)
        surface.blit(self._canvas, (0, 0))

    def _draw(self, st, age: int) -> None:
        c = self._canvas
        c.fill(COLORS["bg_dark"])
        avc, climate, energy = st.avc, st.climate, st.energy

        # Header: title + link state
        pygame.draw.rect(c, COLORS["bg_panel"], (0, 0, self.width, self.HEADER_H))
        title = get_title_font(12).render("AVC-LAN STATUS", True, COLORS["cyan"])
        c.blit(title, (self.PAD, (self.HEADER_H - title.get_height()) // 2))
        if age < 0:
            link, link_c = "NO FRAMES", COLORS["text_dim"]
        elif age <= LINK_STALE_S:
            link, link_c = f"LIVE {age}s", COLORS["green_bright"]
        else:
            link, link_c = (f"STALE {age}s" if age < 99 else "STALE"), COLORS["red_bright"]
        s = get_mono_font(12).render(link, True, link_c)
        c.blit(s, (self.width - self.PAD - s.get_width(), (self.HEADER_H - s.get_height()) // 2))
        pygame.draw.line(c, COLORS["border_focus"],
                         (0, self.HEADER_H - 1), (self.width, self.HEADER_H - 1))

        # Columns
        col_w = (self.width - self.PAD * 3) // 2
        left_x = self.PAD
        right_x = self.PAD * 2 + col_w
        top = self.HEADER_H + 4
        pygame.draw.line(c, COLORS["border_dim"],
                         (self.width // 2, self.HEADER_H + 2),
                         (self.width // 2, self.height - self.FOOTER_H - 2))

        if avc.screen:
            screen = f"{avc.screen} {avc.screen_name.upper()}".strip()
        else:
            screen = "--"
        if avc.touch_pressed:
            touch = f"DOWN {avc.touch_x},{avc.touch_y}"
        elif avc.touch_x or avc.touch_y:
            touch = f"UP {avc.touch_x},{avc.touch_y}"
        else:
            touch = "--"
        left: List[Tuple[str, str, bool]] = [
            ("SCREEN", screen, bool(avc.screen)),
            ("NAV EMU", avc.nav_emulation.upper(), avc.nav_emulation != "off"),
            ("NAV REG", _yes_no(avc.nav_registered), avc.nav_registered),
            ("NAV SCREEN", "ACTIVE" if avc.nav_screen_active else "--", avc.nav_screen_active),
            ("LIGHTS", _yes_no(avc.lights_on, "ON", "OFF"), avc.lights_on is not None),
            ("TOUCH", touch, avc.touch_pressed),
            ("LAST BTN", (avc.last_button or "--").upper()[:14], bool(avc.last_button)),
            ("CLIMATE Q", str(avc.climate_queue), avc.climate_queue > 0),
            ("EV CANCEL", "YES" if avc.ev_cancelled else "--", avc.ev_cancelled),
        ]
        self._section(c, left_x, top, col_w, "DISPLAY / NAV", left)

        outside = (f"{climate.outside_temp:.0f}°C"
                   if climate.outside_temp is not None else "--")
        right: List[Tuple[str, str, bool]] = [
            ("OUTSIDE", outside, climate.outside_temp is not None),
            ("CLIMATE STATUS", (climate.status_source or "--").upper(),
             climate.status_source == "avc"),
            ("ENERGY FLOW", (energy.flow_source or "--").upper(),
             energy.flow_source == "avc"),
            ("BATT BARS", "--" if avc.battery_bars is None else str(avc.battery_bars),
             avc.battery_bars is not None),
        ]
        y = self._section(c, right_x, top, col_w, "SIGNALS", right)
        y = self._raw(c, right_x, y, col_w, "CLIMATE RAW (F3)", avc.climate_raw)
        self._raw(c, right_x, y, col_w, "ENERGY RAW (B9)", avc.energy_raw)

        # Footer bar (the BACK chip is drawn by the pointer overlay)
        bar_y = self.height - self.FOOTER_H
        pygame.draw.rect(c, COLORS["bg_panel"], (0, bar_y, self.width, self.FOOTER_H))
        pygame.draw.line(c, COLORS["border_dim"], (0, bar_y), (self.width, bar_y))
        hint = get_mono_font(10).render("[PRESS] BACK", True, COLORS["text_tertiary"])
        c.blit(hint, ((self.width - hint.get_width()) // 2,
                      bar_y + (self.FOOTER_H - hint.get_height()) // 2))

    def _section(self, c, x: int, y: int, w: int, title: str,
                 rows: List[Tuple[str, str, bool]]) -> int:
        t = get_font(10, "title").render(title, True, COLORS["cyan_bright"])
        c.blit(t, (x, y))
        y += self.ROW_H + 2
        f_label = get_font(8)
        f_value = get_mono_font(12)
        for label, value, active in rows:
            c.blit(f_label.render(label, True, COLORS["text_secondary"]), (x, y + 3))
            v = f_value.render(value, True,
                               COLORS["green_bright"] if active else COLORS["text_dim"])
            c.blit(v, (x + w - v.get_width(), y))
            y += self.ROW_H + 2
        return y + 2

    def _raw(self, c, x: int, y: int, w: int, title: str, raw: str) -> int:
        c.blit(get_font(8).render(title, True, COLORS["text_secondary"]), (x, y + 3))
        y += self.ROW_H
        v = get_mono_font(12).render(raw or "--", True,
                                     COLORS["yellow"] if raw else COLORS["text_dim"])
        c.blit(v, (x + w - v.get_width(), y))
        return y + self.ROW_H + 2

    # ─── Pointer / touch ──────────────────────────────────────────────

    def setup_pointer(self) -> None:
        """Footer "< BACK" chip (the page itself is read-only)."""
        self.add_back_chip()
