"""
Audio settings screen.

Full-screen menu for audio configuration.
"""

import pygame
from typing import Tuple, List, Optional, Any
import time

from .base import Screen
from ..widgets.base import Rect
from ..colors import COLORS
from ..fonts import get_font, get_title_font, get_mono_font


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
        self.options = options  # For select-type items
        
        # If options provided, value is an index
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
        """Adjust value by delta (positive = right/increase)."""
        if self.options:
            # Cycle through options
            self._option_index = (self._option_index + delta) % len(self.options)
            self.value = self._option_index
        elif self.min_val is not None and self.max_val is not None:
            # Numeric value with bounds
            self.value = max(self.min_val, min(self.max_val, self.value + delta * self.step))


class AudioScreen(Screen):
    """
    Audio settings screen.
    
    Shows a scrollable list of audio settings:
    - Volume
    - Bass
    - Treble
    - Balance
    - Sound position
    """
    
    # Layout constants
    HEADER_HEIGHT = 30
    ITEM_HEIGHT = 28
    ITEM_PADDING = 4
    SIDE_MARGIN = 20
    
    def __init__(self, size: Tuple[int, int], app=None, initial_volume: int = 50):
        """Initialize audio screen."""
        super().__init__(size, app)
        
        # Build menu items
        self.items: List[MenuItem] = [
            MenuItem("VOLUME", initial_volume, 0, 100, 5),
            MenuItem("BASS", 0, -10, 10, 1),
            MenuItem("TREBLE", 0, -10, 10, 1),
            MenuItem("BALANCE", 0, -10, 10, 1),
            MenuItem("FADER", 0, -10, 10, 1),
            MenuItem("POSITION", 0, options=["DRIVER", "FRONT", "CENTER", "ALL"]),
        ]
        
        # Navigation
        self._selected_index = 0
        self._editing = False  # True when adjusting a value
        
        # Inactivity tracking
        self._last_activity = time.time()
    
    @property
    def volume(self) -> int:
        """Get current volume value."""
        return self.items[0].value
    
    def on_enter(self) -> None:
        """Reset activity timer on enter."""
        self._last_activity = time.time()
    
    def _get_timeout(self) -> float:
        """Get screen exit timeout from config."""
        if self.app and hasattr(self.app, 'config'):
            return self.app.config.timeout_screen_exit
        return 30.0  # Default fallback
    
    def update(self, dt: float) -> None:
        """Check for inactivity timeout."""
        super().update(dt)
        
        # Check inactivity
        if time.time() - self._last_activity > self._get_timeout():
            self._exit_screen()
    
    def _reset_activity(self) -> None:
        """Reset inactivity timer."""
        self._last_activity = time.time()
    
    def _exit_screen(self) -> None:
        """Exit back to main screen."""
        if self.app:
            self.app.pop_screen()
    
    def handle_input(self, event) -> bool:
        """Handle input events."""
        from ...input.manager import InputEvent as IE
        
        self._reset_activity()
        
        if self._editing:
            # Editing mode: arrows adjust value
            if event == IE.ROTATE_LEFT:
                self.items[self._selected_index].adjust(-1)
                return True
            elif event == IE.ROTATE_RIGHT:
                self.items[self._selected_index].adjust(1)
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
        """Render the audio settings screen."""
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
        title = "AUDIO SETTINGS"
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
            
            # For numeric values, show with arrows if editing
            if is_editing and not item.options:
                value_text = f"< {value_text} >"
            elif is_editing and item.options:
                value_text = f"< {value_text} >"
            
            value_surf = font_value.render(value_text, True, value_color)
            value_x = item_rect.right - value_surf.get_width() - 8
            value_y = y + (self.ITEM_HEIGHT - value_surf.get_height()) // 2
            surface.blit(value_surf, (value_x, value_y))
            
            # Progress bar for volume-like items (not options)
            if is_selected and not item.options and item.min_val is not None:
                self._render_progress_bar(surface, item, item_rect)
            
            y += self.ITEM_HEIGHT + self.ITEM_PADDING
    
    def _render_progress_bar(self, surface: pygame.Surface, item: MenuItem, rect: pygame.Rect) -> None:
        """Render a small progress bar under the item."""
        bar_height = 3
        bar_y = rect.bottom - bar_height - 2
        bar_width = rect.width - 16
        bar_x = rect.x + 8
        
        # Background
        pygame.draw.rect(
            surface,
            COLORS["bg_dark"],
            (bar_x, bar_y, bar_width, bar_height)
        )
        
        # Fill
        if item.max_val != item.min_val:
            fill_ratio = (item.value - item.min_val) / (item.max_val - item.min_val)
            fill_width = int(bar_width * fill_ratio)
            if fill_width > 0:
                pygame.draw.rect(
                    surface,
                    COLORS["cyan"],
                    (bar_x, bar_y, fill_width, bar_height)
                )
    
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
