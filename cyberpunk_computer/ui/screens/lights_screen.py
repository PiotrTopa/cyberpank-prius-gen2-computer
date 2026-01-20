"""
Lights settings screen.

Full-screen settings menu for lights configuration.
"""

import pygame
import time
from typing import Tuple, List, Any, Callable

from .base import Screen
from ..widgets.base import Rect
from ..widgets.controls import ValueDisplay
from ..colors import COLORS
from ..fonts import get_font, get_mono_font
from ...persistence import get_settings, save_settings


class LightsScreen(Screen):
    """
    Lights settings screen.
    
    Settings:
    - Mode: AUTO / MANUAL / OFF
    - DRL: ON / OFF
    - BiLED Mode: OFF / ON / PWM
    - BiLED Brightness: 0-100 (only when PWM mode)
    """
    
    # Modes
    MODES = ["AUTO", "MANUAL", "OFF"]
    BILED_MODES = ["OFF", "ON", "PWM"]
    
    def __init__(
        self,
        size: Tuple[int, int],
        app=None,
        mode: str = "AUTO",
        drl_enabled: bool = True,
        biled_mode: str = "OFF",
        biled_brightness: int = 100
    ):
        """Initialize the lights screen."""
        super().__init__(size, app)
        
        # Settings
        self._mode = mode
        self._drl_enabled = drl_enabled
        self._biled_mode = biled_mode
        self._biled_brightness = biled_brightness
        
        # Menu state
        self._menu_items: List[str] = []
        self._selected_index = 0
        self._editing = False
        self._last_activity_time = time.time()
        
        # Build menu
        self._build_menu()
    
    def _build_menu(self) -> None:
        """Build the menu items list."""
        self._menu_items = [
            "mode",
            "drl",
            "biled_mode",
        ]
        # Only show brightness when in PWM mode
        if self._biled_mode == "PWM":
            self._menu_items.append("biled_brightness")
    
    def _get_item_label(self, item: str) -> str:
        """Get display label for a menu item."""
        labels = {
            "mode": "Mode",
            "drl": "DRL",
            "biled_mode": "BiLED Mode",
            "biled_brightness": "BiLED Brightness",
        }
        return labels.get(item, item)
    
    def _get_item_value(self, item: str) -> str:
        """Get current value string for a menu item."""
        if item == "mode":
            return self._mode
        elif item == "drl":
            return "ON" if self._drl_enabled else "OFF"
        elif item == "biled_mode":
            return self._biled_mode
        elif item == "biled_brightness":
            return f"{self._biled_brightness}%"
        return ""
    
    def _adjust_value(self, item: str, delta: int) -> None:
        """Adjust a menu item value by delta."""
        if item == "mode":
            idx = self.MODES.index(self._mode)
            idx = (idx + delta) % len(self.MODES)
            self._mode = self.MODES[idx]
        elif item == "drl":
            self._drl_enabled = not self._drl_enabled
        elif item == "biled_mode":
            idx = self.BILED_MODES.index(self._biled_mode)
            idx = (idx + delta) % len(self.BILED_MODES)
            self._biled_mode = self.BILED_MODES[idx]
            # Rebuild menu when biled mode changes
            self._build_menu()
            # Clamp selected index if needed
            if self._selected_index >= len(self._menu_items):
                self._selected_index = len(self._menu_items) - 1
        elif item == "biled_brightness":
            self._biled_brightness = max(0, min(100, self._biled_brightness + delta * 5))
        
        # Save settings
        self._save_settings()
    
    def _save_settings(self) -> None:
        """Save current settings to persistence."""
        settings = get_settings()
        settings.lights.mode = self._mode
        settings.lights.drl_enabled = self._drl_enabled
        settings.lights.biled_mode = self._biled_mode
        settings.lights.biled_brightness = self._biled_brightness
        save_settings()
    
    def update(self, dt: float) -> None:
        """Update screen with inactivity timeout."""
        super().update(dt)
        
        # Get timeout from config
        screen_timeout = 30.0
        editing_timeout = 60.0
        if self.app and hasattr(self.app, 'config'):
            screen_timeout = self.app.config.timeout_screen_exit
            editing_timeout = self.app.config.timeout_editing_exit
        
        timeout = editing_timeout if self._editing else screen_timeout
        
        if time.time() - self._last_activity_time > timeout:
            self._exit_screen()
    
    def _exit_screen(self) -> None:
        """Exit back to main screen."""
        if self.app:
            self.app.pop_screen()
    
    def render(self, surface: pygame.Surface) -> None:
        """Render the lights settings screen."""
        # Background
        surface.fill(COLORS["bg_dark"])
        
        # Header
        font_title = get_font(14, "title")
        title = "LIGHTS SETTINGS"
        title_surf = font_title.render(title, True, COLORS["cyan"])
        title_x = (self.width - title_surf.get_width()) // 2
        surface.blit(title_surf, (title_x, 8))
        
        # Separator line
        pygame.draw.line(
            surface,
            COLORS["cyan_dim"],
            (20, 28),
            (self.width - 20, 28),
            1
        )
        
        # Menu items
        font = get_mono_font(12)
        font_small = get_mono_font(10)
        
        item_height = 28
        start_y = 40
        
        for i, item in enumerate(self._menu_items):
            y = start_y + i * item_height
            
            # Selection highlight
            is_selected = i == self._selected_index
            
            if is_selected:
                if self._editing:
                    # Editing mode - amber background
                    pygame.draw.rect(
                        surface,
                        COLORS["bg_frame_focus"],
                        (10, y, self.width - 20, item_height - 2)
                    )
                    pygame.draw.rect(
                        surface,
                        COLORS["amber"],
                        (10, y, self.width - 20, item_height - 2),
                        1
                    )
                else:
                    # Selected - cyan highlight
                    pygame.draw.rect(
                        surface,
                        COLORS["bg_frame_focus"],
                        (10, y, self.width - 20, item_height - 2)
                    )
                    pygame.draw.rect(
                        surface,
                        COLORS["cyan"],
                        (10, y, self.width - 20, item_height - 2),
                        1
                    )
            
            # Label
            label = self._get_item_label(item)
            label_color = COLORS["text_primary"] if is_selected else COLORS["text_secondary"]
            label_surf = font.render(label, True, label_color)
            surface.blit(label_surf, (20, y + 6))
            
            # Value
            value = self._get_item_value(item)
            
            if is_selected and self._editing:
                # Show arrows when editing
                value_text = f"< {value} >"
                value_color = COLORS["amber"]
            else:
                value_text = value
                value_color = COLORS["text_value"]
            
            value_surf = font.render(value_text, True, value_color)
            value_x = self.width - 20 - value_surf.get_width()
            surface.blit(value_surf, (value_x, y + 6))
        
        # Footer hint
        hint_font = get_mono_font(9)
        if self._editing:
            hint = "[</>] Adjust  [OK] Confirm"
        else:
            hint = "[OK] Edit  [HOLD] Back"
        
        hint_surf = hint_font.render(hint, True, COLORS["text_secondary"])
        hint_x = (self.width - hint_surf.get_width()) // 2
        surface.blit(hint_surf, (hint_x, self.height - 16))
    
    def handle_input(self, event) -> bool:
        """Handle input events."""
        from ...input.manager import InputEvent as IE
        
        self._last_activity_time = time.time()
        
        if self._editing:
            # Editing mode
            if event == IE.ROTATE_LEFT:
                self._adjust_value(self._menu_items[self._selected_index], -1)
                return True
            elif event == IE.ROTATE_RIGHT:
                self._adjust_value(self._menu_items[self._selected_index], 1)
                return True
            elif event == IE.PRESS_LIGHT or event == IE.PRESS_STRONG:
                self._editing = False
                return True
        else:
            # Navigation mode
            if event == IE.ROTATE_LEFT:
                self._selected_index = max(0, self._selected_index - 1)
                return True
            elif event == IE.ROTATE_RIGHT:
                self._selected_index = min(len(self._menu_items) - 1, self._selected_index + 1)
                return True
            elif event == IE.PRESS_LIGHT:
                self._editing = True
                return True
            elif event == IE.PRESS_STRONG:
                self._exit_screen()
                return True
        
        return False
