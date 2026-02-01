"""
Engine settings screen.

Full-screen menu for engine display configuration.
Modeled after AudioScreen for consistent UI.
"""

import pygame
import time
from typing import Tuple, List, Optional, Any

from .base import Screen
from ..widgets.base import Rect
from ..colors import COLORS
from ..fonts import get_title_font, get_mono_font
from ...input.manager import InputEvent as IE


class MenuItem:
    """A single menu item with label and value."""
    
    def __init__(
        self,
        label: str,
        value: Any,
        min_val: Any = None,
        max_val: Any = None,
        step: Any = 1,
        options: Optional[List[str]] = None
    ):
        """
        Initialize menu item.
        
        Args:
            label: Display label
            value: Current value
            min_val: Minimum value (for numeric)
            max_val: Maximum value (for numeric)
            step: Step size for adjustments
            options: List of options (for select type)
        """
        self.label = label
        self.value = value
        self.min_val = min_val
        self.max_val = max_val
        self.step = step
        self.options = options
        
        if options and isinstance(value, int):
            self._option_index = value
        else:
            self._option_index = 0
    
    @property
    def display_value(self) -> str:
        """Get value as display string."""
        if self.options:
            return self.options[self._option_index]
        return str(self.value)
    
    def adjust(self, delta: int) -> None:
        """Adjust value by delta."""
        if self.options:
            self._option_index = (self._option_index + delta) % len(self.options)
            self.value = self._option_index
        elif self.min_val is not None and self.max_val is not None:
            self.value = max(self.min_val, min(self.max_val, self.value + delta * self.step))


class EngineScreen(Screen):
    """
    Engine settings screen.
    
    Shows configuration for engine-related displays:
    - Power chart time base (15s, 30s, 1min, 2min, 5min)
    """
    
    # Layout constants (same as AudioScreen)
    HEADER_HEIGHT = 30
    ITEM_HEIGHT = 28
    ITEM_PADDING = 4
    SIDE_MARGIN = 20
    
    # Time base options (in seconds)
    TIME_BASE_OPTIONS = ["15s", "1m", "5m", "15m", "1h"]
    TIME_BASE_VALUES = [15, 60, 300, 900, 3600]
    
    def __init__(self, size: Tuple[int, int], app=None, initial_timebase: int = 60):
        """Initialize engine screen."""
        super().__init__(size, app)
        
        # Find initial time base index
        try:
            initial_index = self.TIME_BASE_VALUES.index(initial_timebase)
        except ValueError:
            initial_index = 2  # Default to 1min
        
        # Build menu items
        self.items: List[MenuItem] = [
            MenuItem(
                "CHART TIME",
                initial_index,
                options=self.TIME_BASE_OPTIONS
            ),
        ]
        
        # Navigation
        self._selected_index = 0
        self._editing = False
        
        # Inactivity tracking
        self._last_activity = time.time()
    
    def get_timebase_seconds(self) -> int:
        """Get current time base value in seconds."""
        index = self.items[0].value
        return self.TIME_BASE_VALUES[index]
    
    def on_enter(self) -> None:
        """Reset activity timer on enter."""
        self._last_activity = time.time()
    
    def _get_timeout(self) -> float:
        """Get screen exit timeout from config."""
        if self.app and hasattr(self.app, 'config'):
            return self.app.config.timeout_screen_exit
        return 30.0
    
    def update(self, dt: float) -> None:
        """Check for inactivity timeout."""
        super().update(dt)
        
        if time.time() - self._last_activity > self._get_timeout():
            self._exit_screen()
    
    def _reset_activity(self) -> None:
        """Reset inactivity timer."""
        self._last_activity = time.time()
    
    def _exit_screen(self) -> None:
        """Exit back to main screen."""
        if self.app:
            self.app.pop_screen()
    
    def _on_value_changed(self) -> None:
        """Handle value change - update store time base setting."""
        if not self.app:
            return
        
        timebase = self.get_timebase_seconds()
        
        # Dispatch action to store - VFD satellite receives via egress
        from ...state.actions import SetPowerChartTimeBaseAction
        if hasattr(self.app, '_store') and self.app._store:
            self.app._store.dispatch(SetPowerChartTimeBaseAction(timebase))

    def handle_input(self, event) -> bool:
        """Handle input events."""
        self._reset_activity()
        
        if self._editing:
            # Editing mode: arrows adjust value
            if event == IE.ROTATE_LEFT:
                item = self.items[self._selected_index]
                item.adjust(-1)
                self._on_value_changed()
                return True
            elif event == IE.ROTATE_RIGHT:
                item = self.items[self._selected_index]
                item.adjust(1)
                self._on_value_changed()
                return True
            elif event == IE.PRESS_LIGHT:
                # Exit editing mode
                self._editing = False
                return True
            elif event == IE.PRESS_STRONG:
                # Exit editing and screen
                self._editing = False
                self._exit_screen()
                return True
        else:
            # Navigation mode
            if event == IE.ROTATE_LEFT:
                self._selected_index = (self._selected_index - 1) % len(self.items)
                return True
            elif event == IE.ROTATE_RIGHT:
                self._selected_index = (self._selected_index + 1) % len(self.items)
                return True
            elif event == IE.PRESS_LIGHT:
                # Enter editing mode
                self._editing = True
                return True
            elif event == IE.PRESS_STRONG:
                # Exit screen
                self._exit_screen()
                return True
        
        return False
    
    def render(self, surface: pygame.Surface) -> None:
        """Render the engine settings screen."""
        # Background
        surface.fill(COLORS["bg_dark"])
        
        # Header
        self._render_header(surface)
        
        # Menu items
        self._render_menu(surface)
        
        # Footer hint
        self._render_footer(surface)
    
    def _render_header(self, surface: pygame.Surface) -> None:
        """Render screen header."""
        # Title bar background
        pygame.draw.rect(
            surface,
            COLORS["bg_panel"],
            (0, 0, self.width, self.HEADER_HEIGHT)
        )
        
        # Title
        font = get_title_font(16)
        title = "ENGINE SETTINGS"
        title_surf = font.render(title, True, COLORS["cyan"])
        title_x = (self.width - title_surf.get_width()) // 2
        title_y = (self.HEADER_HEIGHT - title_surf.get_height()) // 2
        surface.blit(title_surf, (title_x, title_y))
        
        # Separator line
        pygame.draw.line(
            surface,
            COLORS["border_focus"],
            (0, self.HEADER_HEIGHT - 1),
            (self.width, self.HEADER_HEIGHT - 1)
        )
    
    def _render_menu(self, surface: pygame.Surface) -> None:
        """Render menu items."""
        y = self.HEADER_HEIGHT + self.ITEM_PADDING
        
        font_label = get_mono_font(12)
        font_value = get_mono_font(12)
        
        for i, item in enumerate(self.items):
            is_selected = i == self._selected_index
            is_editing = is_selected and self._editing
            
            # Item background
            item_rect = pygame.Rect(
                self.SIDE_MARGIN,
                y,
                self.width - self.SIDE_MARGIN * 2,
                self.ITEM_HEIGHT
            )
            
            if is_selected:
                bg_color = COLORS["bg_frame_focus"]
                border_color = COLORS["border_active"] if is_editing else COLORS["border_focus"]
                pygame.draw.rect(surface, bg_color, item_rect)
                pygame.draw.rect(surface, border_color, item_rect, 1)
            
            # Label (left side)
            label_color = COLORS["cyan"] if is_selected else COLORS["text_secondary"]
            label_surf = font_label.render(item.label, True, label_color)
            label_y = y + (self.ITEM_HEIGHT - label_surf.get_height()) // 2
            surface.blit(label_surf, (item_rect.x + 8, label_y))
            
            # Value (right side)
            value_color = COLORS["active"] if is_editing else (COLORS["text_value"] if is_selected else COLORS["text_secondary"])
            value_text = item.display_value
            
            # Show with arrows if editing
            if is_editing:
                value_text = f"< {value_text} >"
            
            value_surf = font_value.render(value_text, True, value_color)
            value_x = item_rect.right - value_surf.get_width() - 8
            value_y = y + (self.ITEM_HEIGHT - value_surf.get_height()) // 2
            surface.blit(value_surf, (value_x, value_y))
            
            y += self.ITEM_HEIGHT + self.ITEM_PADDING
    
    def _render_footer(self, surface: pygame.Surface) -> None:
        """Render footer with hints."""
        font = get_mono_font(10)
        
        if self._editing:
            hint = "[<>] ADJUST   [ENTER] DONE   [SPACE] EXIT"
        else:
            hint = "[<>] SELECT   [ENTER] EDIT   [SPACE] EXIT"
        
        hint_surf = font.render(hint, True, COLORS["text_secondary"])
        hint_x = (self.width - hint_surf.get_width()) // 2
        hint_y = self.height - hint_surf.get_height() - 4
        surface.blit(hint_surf, (hint_x, hint_y))
