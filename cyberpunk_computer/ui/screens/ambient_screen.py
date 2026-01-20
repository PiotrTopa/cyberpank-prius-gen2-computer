"""
Ambient lighting settings screen.

Full-screen settings menu for ambient RGB lighting configuration.
Includes HUE/Saturation color picker with visual preview.
"""

import pygame
import time
import math
from typing import Tuple, List

from .base import Screen
from ..widgets.base import Rect
from ..colors import COLORS
from ..fonts import get_font, get_mono_font
from ...persistence import get_settings, save_settings


def hsl_to_rgb(h: int, s: int, l: int = 50) -> Tuple[int, int, int]:
    """
    Convert HSL to RGB color.
    
    Args:
        h: Hue 0-360
        s: Saturation 0-100
        l: Lightness 0-100 (default 50)
    
    Returns:
        RGB tuple (0-255 each)
    """
    h = h % 360
    s = s / 100.0
    l = l / 100.0
    
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = l - c / 2
    
    if h < 60:
        r, g, b = c, x, 0
    elif h < 120:
        r, g, b = x, c, 0
    elif h < 180:
        r, g, b = 0, c, x
    elif h < 240:
        r, g, b = 0, x, c
    elif h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x
    
    return (
        int((r + m) * 255),
        int((g + m) * 255),
        int((b + m) * 255)
    )


class AmbientScreen(Screen):
    """
    Ambient lighting settings screen.
    
    Settings:
    - Mode: OFF / MANUAL / CYBER / SMOOTH / ROMANCE / MUSIC
    - Hue: 0-360 (color wheel)
    - Saturation: 0-100%
    - Brightness: 0-100%
    
    Features color preview showing the current HUE/Saturation.
    """
    
    # Available modes
    MODES = ["OFF", "MANUAL", "CYBER", "SMOOTH", "ROMANCE", "MUSIC"]
    
    # Mode descriptions
    MODE_DESCRIPTIONS = {
        "OFF": "Lighting disabled",
        "MANUAL": "Fixed color",
        "CYBER": "Cyberpunk theme",
        "SMOOTH": "Smooth transitions",
        "ROMANCE": "Warm romantic",
        "MUSIC": "Music reactive",
    }
    
    def __init__(
        self,
        size: Tuple[int, int],
        app=None,
        mode: str = "OFF",
        hue: int = 180,
        saturation: int = 100,
        brightness: int = 80
    ):
        """Initialize the ambient screen."""
        super().__init__(size, app)
        
        # Settings
        self._mode = mode
        self._hue = hue
        self._saturation = saturation
        self._brightness = brightness
        
        # Menu state
        self._menu_items: List[str] = []
        self._selected_index = 0
        self._editing = False
        self._last_activity_time = time.time()
        
        # Build menu
        self._build_menu()
    
    def _build_menu(self) -> None:
        """Build the menu items list based on current mode."""
        self._menu_items = ["mode"]
        
        # Only show color settings for MANUAL mode
        if self._mode == "MANUAL":
            self._menu_items.extend(["hue", "saturation", "brightness"])
    
    def _get_item_label(self, item: str) -> str:
        """Get display label for a menu item."""
        labels = {
            "mode": "Mode",
            "hue": "Hue",
            "saturation": "Saturation",
            "brightness": "Brightness",
        }
        return labels.get(item, item)
    
    def _get_item_value(self, item: str) -> str:
        """Get current value string for a menu item."""
        if item == "mode":
            return self._mode
        elif item == "hue":
            return f"{self._hue}°"
        elif item == "saturation":
            return f"{self._saturation}%"
        elif item == "brightness":
            return f"{self._brightness}%"
        return ""
    
    def _adjust_value(self, item: str, delta: int) -> None:
        """Adjust a menu item value by delta."""
        if item == "mode":
            idx = self.MODES.index(self._mode)
            idx = (idx + delta) % len(self.MODES)
            self._mode = self.MODES[idx]
            # Rebuild menu when mode changes
            self._build_menu()
            # Clamp selected index
            if self._selected_index >= len(self._menu_items):
                self._selected_index = len(self._menu_items) - 1
        elif item == "hue":
            self._hue = (self._hue + delta * 10) % 360
        elif item == "saturation":
            self._saturation = max(0, min(100, self._saturation + delta * 5))
        elif item == "brightness":
            self._brightness = max(0, min(100, self._brightness + delta * 5))
        
        # Save settings
        self._save_settings()
    
    def _save_settings(self) -> None:
        """Save current settings to persistence."""
        settings = get_settings()
        settings.ambient.mode = self._mode
        settings.ambient.hue = self._hue
        settings.ambient.saturation = self._saturation
        settings.ambient.brightness = self._brightness
        save_settings()
    
    def _get_preview_color(self) -> Tuple[int, int, int]:
        """Get current preview color as RGB."""
        return hsl_to_rgb(self._hue, self._saturation, self._brightness // 2 + 25)
    
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
        """Render the ambient settings screen."""
        # Background
        surface.fill(COLORS["bg_dark"])
        
        # Header
        font_title = get_font(14, "title")
        title = "AMBIENT SETTINGS"
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
        
        # Left side: Menu items
        # Right side: Color preview (when in MANUAL mode)
        
        menu_width = self.width - 100 if self._mode == "MANUAL" else self.width
        preview_width = 80
        preview_x = self.width - preview_width - 10
        
        # Menu items
        font = get_mono_font(12)
        font_small = get_mono_font(9)
        
        item_height = 28
        start_y = 40
        
        for i, item in enumerate(self._menu_items):
            y = start_y + i * item_height
            
            # Selection highlight
            is_selected = i == self._selected_index
            
            menu_right = preview_x - 10 if self._mode == "MANUAL" else self.width - 20
            
            if is_selected:
                if self._editing:
                    # Editing mode - amber background
                    pygame.draw.rect(
                        surface,
                        COLORS["bg_frame_focus"],
                        (10, y, menu_right - 10, item_height - 2)
                    )
                    pygame.draw.rect(
                        surface,
                        COLORS["amber"],
                        (10, y, menu_right - 10, item_height - 2),
                        1
                    )
                else:
                    # Selected - cyan highlight
                    pygame.draw.rect(
                        surface,
                        COLORS["bg_frame_focus"],
                        (10, y, menu_right - 10, item_height - 2)
                    )
                    pygame.draw.rect(
                        surface,
                        COLORS["cyan"],
                        (10, y, menu_right - 10, item_height - 2),
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
            value_x = menu_right - 10 - value_surf.get_width()
            surface.blit(value_surf, (value_x, y + 6))
        
        # Mode description
        if self._mode in self.MODE_DESCRIPTIONS:
            desc_y = start_y + len(self._menu_items) * item_height + 10
            desc = self.MODE_DESCRIPTIONS[self._mode]
            desc_surf = font_small.render(desc, True, COLORS["text_secondary"])
            surface.blit(desc_surf, (20, desc_y))
        
        # Color preview (only in MANUAL mode)
        if self._mode == "MANUAL":
            self._render_color_preview(surface, preview_x, start_y, preview_width - 10, 100)
        
        # Footer hint
        hint_font = get_mono_font(9)
        if self._editing:
            hint = "[</>] Adjust  [OK] Confirm"
        else:
            hint = "[OK] Edit  [HOLD] Back"
        
        hint_surf = hint_font.render(hint, True, COLORS["text_secondary"])
        hint_x = (self.width - hint_surf.get_width()) // 2
        surface.blit(hint_surf, (hint_x, self.height - 16))
    
    def _render_color_preview(
        self, 
        surface: pygame.Surface, 
        x: int, 
        y: int, 
        width: int, 
        height: int
    ) -> None:
        """Render the color preview box."""
        # Get current color
        color = self._get_preview_color()
        
        # Border
        pygame.draw.rect(
            surface,
            COLORS["cyan_dim"],
            (x, y, width, height),
            1
        )
        
        # Color fill (with small margin)
        pygame.draw.rect(
            surface,
            color,
            (x + 2, y + 2, width - 4, height - 4)
        )
        
        # Label
        font = get_mono_font(8)
        label = "PREVIEW"
        label_surf = font.render(label, True, COLORS["text_secondary"])
        label_x = x + (width - label_surf.get_width()) // 2
        surface.blit(label_surf, (label_x, y + height + 4))
        
        # Draw a simple hue bar below preview
        bar_y = y + height + 20
        bar_height = 10
        
        # Draw hue spectrum
        for i in range(width):
            hue = int(i * 360 / width)
            c = hsl_to_rgb(hue, 100, 50)
            pygame.draw.line(surface, c, (x + i, bar_y), (x + i, bar_y + bar_height))
        
        # Draw position marker
        marker_x = x + int(self._hue * width / 360)
        pygame.draw.line(
            surface,
            COLORS["text_value"],
            (marker_x, bar_y - 2),
            (marker_x, bar_y + bar_height + 2),
            2
        )
    
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
