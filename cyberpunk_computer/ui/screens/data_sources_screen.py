"""
Data Sources settings screen.

Toggle which CAN subscription groups are active.
Accessed via ENGINE menu -> DATA SOURCES.

Changes take effect immediately (subscribe/unsubscribe gateway slots)
and are persisted to user_settings.json between executions.
"""

import pygame
import time
import logging
from typing import Tuple, Optional, List

from .base import Screen
from ..colors import COLORS
from ..fonts import get_title_font, get_mono_font
from ...input.manager import InputEvent as IE
from ...comm.subscription_groups import TOGGLEABLE_GROUPS, SubscriptionGroup
from ...persistence import get_settings

logger = logging.getLogger(__name__)


class _ToggleItem:
    """A toggleable group with current state."""

    __slots__ = ("group", "enabled")

    def __init__(self, group: SubscriptionGroup, enabled: bool):
        self.group = group
        self.enabled = enabled


class DataSourcesScreen(Screen):
    """
    Data Sources toggle screen.

    Shows all toggleable CAN subscription groups with ON/OFF state.
    Rotate to navigate, light press to toggle, back to exit.

    Auto-dismisses after inactivity timeout.
    """

    HEADER_HEIGHT = 26
    ITEM_HEIGHT = 32
    ITEM_GAP = 4
    SIDE_MARGIN = 16
    FOOTER_HEIGHT = 22
    CONTENT_PAD_TOP = 8
    SCROLLBAR_WIDTH = 4

    def __init__(
        self,
        size: Tuple[int, int],
        app=None,
    ):
        super().__init__(size, app)
        self._last_activity = time.time()
        self._selected_index = 0
        self._scroll_offset = 0  # pixels scrolled

        # Build toggle items from group definitions + persisted settings
        settings = get_settings()
        self._items: List[_ToggleItem] = [
            _ToggleItem(
                group=g,
                enabled=settings.data_sources.is_group_enabled(g.key),
            )
            for g in TOGGLEABLE_GROUPS
        ]

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

    # ─── Input ────────────────────────────────────────────────────────

    def _content_top(self) -> int:
        return self.HEADER_HEIGHT + self.CONTENT_PAD_TOP

    def _content_bottom(self) -> int:
        return self.height - self.FOOTER_HEIGHT

    def _visible_height(self) -> int:
        return self._content_bottom() - self._content_top()

    def _item_stride(self) -> int:
        return self.ITEM_HEIGHT + self.ITEM_GAP

    def _total_content_height(self) -> int:
        n = len(self._items)
        return n * self._item_stride() - self.ITEM_GAP if n else 0

    def _max_scroll(self) -> int:
        return max(0, self._total_content_height() - self._visible_height())

    def _ensure_selected_visible(self) -> None:
        """Adjust scroll offset so the selected item is fully visible."""
        stride = self._item_stride()
        item_top = self._selected_index * stride
        item_bottom = item_top + self.ITEM_HEIGHT

        if item_top < self._scroll_offset:
            self._scroll_offset = item_top
        elif item_bottom > self._scroll_offset + self._visible_height():
            self._scroll_offset = item_bottom - self._visible_height()

        self._scroll_offset = max(0, min(self._scroll_offset, self._max_scroll()))

    def handle_input(self, event) -> bool:
        self._last_activity = time.time()

        if event == IE.ROTATE_LEFT:
            self._selected_index = max(0, self._selected_index - 1)
            self._ensure_selected_visible()
            return True
        elif event == IE.ROTATE_RIGHT:
            self._selected_index = min(len(self._items) - 1, self._selected_index + 1)
            self._ensure_selected_visible()
            return True
        elif event == IE.PRESS_LIGHT:
            self._toggle_current()
            return True
        elif event in (IE.PRESS_STRONG, IE.BACK):
            self._exit_screen()
            return True

        return False

    def _toggle_current(self) -> None:
        """Toggle the currently selected group and apply immediately."""
        item = self._items[self._selected_index]
        item.enabled = not item.enabled

        # Persist
        settings = get_settings()
        settings.data_sources.set_group_enabled(item.group.key, item.enabled)
        settings.save()

        # Apply to gateway
        vt = getattr(self.app, '_virtual_twin', None)
        if vt is None:
            return

        if item.enabled:
            vt.subscribe_group(item.group)
        else:
            vt.unsubscribe_group(item.group)

        logger.info(
            "Data source %s: %s",
            item.group.key, "ON" if item.enabled else "OFF",
        )

    # ─── Rendering ────────────────────────────────────────────────────

    def render(self, surface: pygame.Surface) -> None:
        surface.fill(COLORS["bg_dark"])
        self._render_header(surface)
        self._render_items(surface)
        self._render_scrollbar(surface)
        self._render_footer(surface)

    def _render_header(self, surface: pygame.Surface) -> None:
        pygame.draw.rect(
            surface, COLORS["bg_panel"],
            (0, 0, self.width, self.HEADER_HEIGHT),
        )
        font = get_title_font(14)
        title = "DATA SOURCES"
        s = font.render(title, True, COLORS["cyan"])
        surface.blit(s, (
            (self.width - s.get_width()) // 2,
            (self.HEADER_HEIGHT - s.get_height()) // 2,
        ))
        pygame.draw.line(
            surface, COLORS["border_focus"],
            (0, self.HEADER_HEIGHT - 1),
            (self.width, self.HEADER_HEIGHT - 1),
        )

    def _render_items(self, surface: pygame.Surface) -> None:
        font_label = get_mono_font(12)
        font_desc = get_mono_font(9)
        font_badge = get_mono_font(11)

        content_top = self._content_top()
        content_bottom = self._content_bottom()
        stride = self._item_stride()
        scrollbar_reserved = self.SCROLLBAR_WIDTH + 4 if self._max_scroll() > 0 else 0

        # Clip to content area so items don't bleed into header/footer
        clip_rect = pygame.Rect(0, content_top, self.width, content_bottom - content_top)
        old_clip = surface.get_clip()
        surface.set_clip(clip_rect)

        for i, item in enumerate(self._items):
            y = content_top + i * stride - self._scroll_offset

            # Skip items fully outside visible area
            if y + self.ITEM_HEIGHT < content_top or y > content_bottom:
                continue

            is_selected = i == self._selected_index

            item_rect = pygame.Rect(
                self.SIDE_MARGIN, y,
                self.width - self.SIDE_MARGIN * 2 - scrollbar_reserved,
                self.ITEM_HEIGHT,
            )

            # Background highlight
            if is_selected:
                pygame.draw.rect(surface, COLORS["bg_frame_focus"], item_rect)
                pygame.draw.rect(surface, COLORS["border_focus"], item_rect, 1)

            # Chevron
            if is_selected:
                chevron = font_label.render(">", True, COLORS["cyan"])
                surface.blit(chevron, (item_rect.x + 4, y + 4))

            # Label
            label_color = COLORS["cyan"] if is_selected else COLORS["text_secondary"]
            label_surf = font_label.render(item.group.label, True, label_color)
            surface.blit(label_surf, (item_rect.x + 18, y + 2))

            # Description
            desc_surf = font_desc.render(
                item.group.description, True, COLORS["text_tertiary"],
            )
            surface.blit(desc_surf, (item_rect.x + 18, y + 18))

            # ON/OFF badge (right-aligned)
            if item.enabled:
                badge_text = "ON"
                badge_fg = COLORS.get("green_bright", (0, 230, 118))
            else:
                badge_text = "OFF"
                badge_fg = COLORS.get("text_tertiary", (100, 100, 100))

            badge_surf = font_badge.render(badge_text, True, badge_fg)
            badge_x = item_rect.right - badge_surf.get_width() - 8
            badge_y = y + (self.ITEM_HEIGHT - badge_surf.get_height()) // 2
            surface.blit(badge_surf, (badge_x, badge_y))

        surface.set_clip(old_clip)

    def _render_scrollbar(self, surface: pygame.Surface) -> None:
        """Render a thin scrollbar track + thumb on the right edge."""
        max_scroll = self._max_scroll()
        if max_scroll <= 0:
            return  # everything fits, no scrollbar needed

        content_top = self._content_top()
        track_height = self._content_bottom() - content_top
        total_content = self._total_content_height()

        track_x = self.width - self.SCROLLBAR_WIDTH - 2

        # Track background
        track_rect = pygame.Rect(track_x, content_top, self.SCROLLBAR_WIDTH, track_height)
        pygame.draw.rect(surface, COLORS.get("bg_panel", (30, 30, 30)), track_rect)

        # Thumb size proportional to visible / total
        thumb_height = max(12, int(track_height * track_height / total_content))
        scrollable_track = track_height - thumb_height
        thumb_y = content_top + int(scrollable_track * self._scroll_offset / max_scroll)

        thumb_rect = pygame.Rect(track_x, thumb_y, self.SCROLLBAR_WIDTH, thumb_height)
        pygame.draw.rect(surface, COLORS.get("cyan", (0, 200, 255)), thumb_rect)

    def _render_footer(self, surface: pygame.Surface) -> None:
        font = get_mono_font(10)
        bar_h = self.FOOTER_HEIGHT
        bar_y = self.height - bar_h

        pygame.draw.rect(surface, COLORS["bg_panel"], (0, bar_y, self.width, bar_h))
        pygame.draw.line(surface, COLORS["border_dim"], (0, bar_y), (self.width, bar_y))

        hint = "[OK] TOGGLE   [BACK] EXIT"
        s = font.render(hint, True, COLORS["text_tertiary"])
        surface.blit(s, (
            (self.width - s.get_width()) // 2,
            bar_y + (bar_h - s.get_height()) // 2,
        ))
