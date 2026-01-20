"""
Climate settings screen.

Full-screen menu for climate/HVAC configuration.
"""

import pygame
from typing import Tuple, List, Optional, Any
import time

from .base import Screen
from ..widgets.base import Rect
from ..colors import COLORS
from ..fonts import get_font, get_title_font, get_mono_font


class ClimateMenuItem:
    """A single climate menu item with label and value."""
    
    def __init__(
        self,
        label: str,
        value: Any,
        min_val: Any = None,
        max_val: Any = None,
        step: Any = 1,
        options: Optional[List[str]] = None,
        unit: str = "",
        readonly: bool = False
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
            unit: Unit suffix (°C, %, etc.)
            readonly: If true, item is display-only
        """
        self.label = label
        self.value = value
        self.min_val = min_val
        self.max_val = max_val
        self.step = step
        self.options = options
        self.unit = unit
        self.readonly = readonly
        
        if options and isinstance(value, int):
            self._option_index = value
        else:
            self._option_index = 0
    
    @property
    def display_value(self) -> str:
        """Get value as display string."""
        if self.options:
            return self.options[self._option_index]
        return f"{self.value}{self.unit}"
    
    def adjust(self, delta: int) -> None:
        """Adjust value by delta (positive = right/increase)."""
        if self.readonly:
            return
            
        if self.options:
            self._option_index = (self._option_index + delta) % len(self.options)
            self.value = self._option_index
        elif self.min_val is not None and self.max_val is not None:
            self.value = max(self.min_val, min(self.max_val, self.value + delta * self.step))


class ClimateScreen(Screen):
    """
    Climate settings screen.
    
    Shows climate control settings:
    - Target temperature
    - Fan speed
    - Mode (Auto/Manual/Eco)
    - A/C on/off
    - Recirculation
    - Air direction
    - Current temperatures (read-only)
    """
    
    # Layout constants
    HEADER_HEIGHT = 30
    ITEM_HEIGHT = 24
    ITEM_PADDING = 2
    SIDE_MARGIN = 16
    
    def __init__(
        self,
        size: Tuple[int, int],
        app=None,
        temp_target: int = 21,
        temp_in: int = 22,
        temp_out: int = -5,
        ac_on: bool = True,
        auto_mode: bool = True,
        recirc: bool = False
    ):
        """Initialize climate screen."""
        super().__init__(size, app)
        
        # Build menu items
        self.items: List[ClimateMenuItem] = [
            ClimateMenuItem("TARGET TEMP", temp_target, 16, 28, 1, unit="°C"),
            ClimateMenuItem("FAN SPEED", 3, 0, 7, 1),
            ClimateMenuItem("MODE", 0 if auto_mode else 1, options=["AUTO", "MANUAL", "ECO"]),
            ClimateMenuItem("A/C", 0 if ac_on else 1, options=["ON", "OFF"]),
            ClimateMenuItem("RECIRCULATION", 1 if recirc else 0, options=["OFF", "ON"]),
            ClimateMenuItem("AIR DIRECTION", 0, options=["FACE", "FACE+FEET", "FEET", "DEFROST"]),
            # Read-only current values
            ClimateMenuItem("INSIDE TEMP", temp_in, unit="°C", readonly=True),
            ClimateMenuItem("OUTSIDE TEMP", temp_out, unit="°C", readonly=True),
        ]
        
        # Navigation
        self._selected_index = 0
        self._editing = False
        
        # Inactivity tracking
        self._last_activity = time.time()
    
    @property
    def target_temp(self) -> int:
        """Get current target temperature."""
        return self.items[0].value
    
    @property
    def ac_on(self) -> bool:
        """Get A/C state."""
        return self.items[3].value == 0
    
    @property
    def auto_mode(self) -> bool:
        """Get auto mode state."""
        return self.items[2].value == 0
    
    @property
    def recirc(self) -> bool:
        """Get recirculation state."""
        return self.items[4].value == 1
    
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
        
        current_item = self.items[self._selected_index]
        
        if self._editing:
            if current_item.readonly:
                # Readonly items can't be edited
                self._editing = False
                return True
            
            if event == IE.ROTATE_LEFT:
                current_item.adjust(-1)
                return True
            elif event == IE.ROTATE_RIGHT:
                current_item.adjust(1)
                return True
            elif event == IE.PRESS_LIGHT:
                self._editing = False
                return True
            elif event == IE.PRESS_STRONG:
                self._editing = False
                self._exit_screen()
                return True
        else:
            if event == IE.ROTATE_LEFT:
                self._selected_index = (self._selected_index - 1) % len(self.items)
                return True
            elif event == IE.ROTATE_RIGHT:
                self._selected_index = (self._selected_index + 1) % len(self.items)
                return True
            elif event == IE.PRESS_LIGHT:
                if not current_item.readonly:
                    self._editing = True
                return True
            elif event == IE.PRESS_STRONG:
                self._exit_screen()
                return True
        
        return False
    
    def render(self, surface: pygame.Surface) -> None:
        """Render the climate settings screen."""
        surface.fill(COLORS["bg_dark"])
        
        self._render_header(surface)
        self._render_menu(surface)
        self._render_footer(surface)
    
    def _render_header(self, surface: pygame.Surface) -> None:
        """Render screen header."""
        pygame.draw.rect(
            surface,
            COLORS["bg_panel"],
            (0, 0, self.width, self.HEADER_HEIGHT)
        )
        
        font = get_title_font(16)
        title = "CLIMATE CONTROL"
        title_surf = font.render(title, True, COLORS["cyan"])
        title_x = (self.width - title_surf.get_width()) // 2
        title_y = (self.HEADER_HEIGHT - title_surf.get_height()) // 2
        surface.blit(title_surf, (title_x, title_y))
        
        pygame.draw.line(
            surface,
            COLORS["border_focus"],
            (0, self.HEADER_HEIGHT - 1),
            (self.width, self.HEADER_HEIGHT - 1)
        )
    
    def _render_menu(self, surface: pygame.Surface) -> None:
        """Render menu items."""
        y = self.HEADER_HEIGHT + self.ITEM_PADDING + 4
        
        font_label = get_mono_font(11)
        font_value = get_mono_font(11)
        
        for i, item in enumerate(self.items):
            is_selected = i == self._selected_index
            is_editing = is_selected and self._editing
            is_readonly = item.readonly
            
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
            
            # Label
            if is_readonly:
                label_color = COLORS["text_secondary"]
            elif is_selected:
                label_color = COLORS["cyan"]
            else:
                label_color = COLORS["text_secondary"]
            
            label_surf = font_label.render(item.label, True, label_color)
            label_y = y + (self.ITEM_HEIGHT - label_surf.get_height()) // 2
            surface.blit(label_surf, (item_rect.x + 6, label_y))
            
            # Value
            if is_readonly:
                value_color = COLORS["inactive"]
            elif is_editing:
                value_color = COLORS["active"]
            elif is_selected:
                value_color = COLORS["text_value"]
            else:
                value_color = COLORS["text_secondary"]
            
            value_text = item.display_value
            if is_editing:
                value_text = f"< {value_text} >"
            
            value_surf = font_value.render(value_text, True, value_color)
            value_x = item_rect.right - value_surf.get_width() - 6
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
