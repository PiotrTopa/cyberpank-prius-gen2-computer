"""
Engine menu screen.

Settings/diagnostics hub accessed via light press on ENGINE frame.
Provides navigation to:
- Chart Settings (time base for graphs)
- Error Codes / DTC diagnostics
- AVC-LAN bus monitor

This is a settings/menu screen — auto-dismisses after inactivity timeout.
"""

import pygame
import time
from typing import Tuple, Optional, List

from .base import Screen
from ..colors import COLORS
from ..fonts import get_title_font, get_mono_font, get_font
from ...input.manager import InputEvent as IE
from ...state.store import Store


class _MenuEntry:
    """A single menu entry with label, description, and action."""

    def __init__(self, label: str, description: str, action_key: str):
        self.label = label
        self.description = description
        self.action_key = action_key


class EngineMenuScreen(Screen):
    """
    Engine settings & diagnostics menu.

    A simple scrollable list of items. Light press selects/opens,
    strong press exits back to main screen.

    Auto-dismisses after inactivity (settings screen behavior).
    """

    HEADER_HEIGHT = 26
    ITEM_HEIGHT = 32
    SIDE_MARGIN = 16

    def __init__(
        self,
        size: Tuple[int, int],
        app=None,
        store: Optional[Store] = None,
        initial_timebase: int = 60,
    ):
        super().__init__(size, app)
        self._store = store
        self._initial_timebase = initial_timebase
        self._last_activity = time.time()

        self._items: List[_MenuEntry] = [
            _MenuEntry("DATA SOURCES", "Select which CAN PIDs to fetch", "data_sources"),
            _MenuEntry("CHART SETTINGS", "Graph time base configuration", "chart_settings"),
            _MenuEntry("ERROR CODES", "OBD-II diagnostic trouble codes", "dtc"),
            _MenuEntry("SOLICITED MONITOR", "Live solicited CAN PID values", "solicited"),
            _MenuEntry("AVC-LAN MONITOR", "Live AVC-LAN bus sniffer", "avc"),
        ]

        self._selected_index = 0

    def on_enter(self) -> None:
        self._last_activity = time.time()

    def _get_timeout(self) -> float:
        if self.app and hasattr(self.app, 'config'):
            return self.app.config.timeout_screen_exit
        return 30.0

    def update(self, dt: float) -> None:
        super().update(dt)
        if time.time() - self._last_activity > self._get_timeout():
            self._exit_screen()

    def _exit_screen(self) -> None:
        if self.app:
            self.app.pop_screen()

    def handle_input(self, event) -> bool:
        self._last_activity = time.time()

        if event == IE.ROTATE_LEFT:
            self._selected_index = max(0, self._selected_index - 1)
            return True
        elif event == IE.ROTATE_RIGHT:
            self._selected_index = min(len(self._items) - 1, self._selected_index + 1)
            return True
        elif event == IE.PRESS_LIGHT:
            self._activate_item(self._items[self._selected_index])
            return True
        elif event in (IE.PRESS_STRONG, IE.BACK):
            self._exit_screen()
            return True

        return False

    def _activate_item(self, item: _MenuEntry) -> None:
        """Open the screen corresponding to the selected menu item."""
        if not self.app:
            return

        if item.action_key == "data_sources":
            from .data_sources_screen import DataSourcesScreen
            screen = DataSourcesScreen(
                (self.width, self.height),
                self.app,
            )
            self.app.push_screen(screen)

        elif item.action_key == "chart_settings":
            from .engine_screen import EngineScreen
            screen = EngineScreen(
                (self.width, self.height),
                self.app,
                initial_timebase=self._initial_timebase,
            )
            self.app.push_screen(screen)

        elif item.action_key == "dtc":
            from .dtc_screen import DTCScreen
            screen = DTCScreen(
                (self.width, self.height),
                self.app,
                store=self._store,
            )
            self.app.push_screen(screen)

        elif item.action_key == "avc":
            from .avc_monitor_screen import AVCMonitorScreen
            screen = AVCMonitorScreen(
                (self.width, self.height),
                self.app,
                store=self._store,
            )
            self.app.push_screen(screen)

        elif item.action_key == "solicited":
            from .solicited_monitor_screen import SolicitedMonitorScreen
            screen = SolicitedMonitorScreen(
                (self.width, self.height),
                self.app,
                store=self._store,
            )
            self.app.push_screen(screen)

    # ─── Rendering ────────────────────────────────────────────────────

    def render(self, surface: pygame.Surface) -> None:
        surface.fill(COLORS["bg_dark"])
        self._render_header(surface)
        self._render_menu(surface)
        self._render_footer(surface)

    def _render_header(self, surface: pygame.Surface) -> None:
        pygame.draw.rect(
            surface, COLORS["bg_panel"],
            (0, 0, self.width, self.HEADER_HEIGHT),
        )
        font = get_title_font(14)
        title = "ENGINE / DIAGNOSTICS"
        s = font.render(title, True, COLORS["cyan"])
        surface.blit(s, ((self.width - s.get_width()) // 2,
                         (self.HEADER_HEIGHT - s.get_height()) // 2))
        pygame.draw.line(
            surface, COLORS["border_focus"],
            (0, self.HEADER_HEIGHT - 1), (self.width, self.HEADER_HEIGHT - 1),
        )

    def _render_menu(self, surface: pygame.Surface) -> None:
        font_label = get_mono_font(12)
        font_desc = get_mono_font(9)

        y = self.HEADER_HEIGHT + 8

        for i, item in enumerate(self._items):
            is_selected = i == self._selected_index

            item_rect = pygame.Rect(
                self.SIDE_MARGIN, y,
                self.width - self.SIDE_MARGIN * 2, self.ITEM_HEIGHT,
            )

            if is_selected:
                pygame.draw.rect(surface, COLORS["bg_frame_focus"], item_rect)
                pygame.draw.rect(surface, COLORS["border_focus"], item_rect, 1)

            # Chevron indicator
            if is_selected:
                chevron = get_mono_font(12).render(">", True, COLORS["cyan"])
                surface.blit(chevron, (item_rect.x + 4, y + 4))

            # Label
            label_color = COLORS["cyan"] if is_selected else COLORS["text_secondary"]
            label_surf = font_label.render(item.label, True, label_color)
            surface.blit(label_surf, (item_rect.x + 18, y + 2))

            # Description
            desc_color = COLORS["text_tertiary"]
            desc_surf = font_desc.render(item.description, True, desc_color)
            surface.blit(desc_surf, (item_rect.x + 18, y + 18))

            y += self.ITEM_HEIGHT + 4

    def _render_footer(self, surface: pygame.Surface) -> None:
        font = get_mono_font(10)
        bar_h = 22
        bar_y = self.height - bar_h

        # Dark bar background
        pygame.draw.rect(surface, COLORS["bg_panel"], (0, bar_y, self.width, bar_h))
        pygame.draw.line(surface, COLORS["border_dim"], (0, bar_y), (self.width, bar_y))

        # Hint text (menu screen — no button bar, uses inline select)
        hint = "[OK] OPEN   [BACK] EXIT"
        s = font.render(hint, True, COLORS["text_tertiary"])
        surface.blit(s, ((self.width - s.get_width()) // 2,
                         bar_y + (bar_h - s.get_height()) // 2))
