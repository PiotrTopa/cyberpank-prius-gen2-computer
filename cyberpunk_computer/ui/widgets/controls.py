"""
Control widgets for user input.

Contains specialized widgets for volume sliders, toggles, etc.
"""

import pygame
from typing import Optional, Callable

from .base import Widget, Rect
from ..colors import COLORS, lerp_color
from ..fonts import get_font


class VolumeBar(Widget):
    """
    A horizontal volume/level bar display.
    
    Shows current level with segmented or continuous display.
    Used in Audio frame for volume indication.
    """
    
    def __init__(
        self,
        rect: Rect,
        value: int = 50,
        min_val: int = 0,
        max_val: int = 100,
        show_value: bool = True,
        segments: int = 0  # 0 = continuous
    ):
        """
        Initialize volume bar.
        
        Args:
            rect: Position and size
            value: Current value
            min_val: Minimum value
            max_val: Maximum value
            show_value: Show numeric value
            segments: Number of segments (0 = continuous)
        """
        super().__init__(rect, focusable=False)
        
        self.value = value
        self.min_val = min_val
        self.max_val = max_val
        self.show_value = show_value
        self.segments = segments
    
    @property
    def normalized_value(self) -> float:
        """Get value normalized to 0.0-1.0 range."""
        range_val = self.max_val - self.min_val
        if range_val == 0:
            return 0.0
        return (self.value - self.min_val) / range_val
    
    def set_value(self, value: int) -> None:
        """Set the current value."""
        self.value = max(self.min_val, min(self.max_val, value))
        self._dirty = True
    
    def render(self, surface: pygame.Surface) -> None:
        """Render the volume bar."""
        if not self.visible:
            return
        
        # Background
        pygame.draw.rect(
            surface,
            COLORS["bg_dark"],
            self.rect.to_pygame()
        )
        
        # Border
        pygame.draw.rect(
            surface,
            COLORS["cyan_dim"],
            self.rect.to_pygame(),
            1
        )
        
        # Calculate fill area
        fill_width = int((self.rect.width - 4) * self.normalized_value)
        
        if fill_width > 0:
            if self.segments > 0:
                self._render_segmented(surface, fill_width)
            else:
                self._render_continuous(surface, fill_width)
        
        # Show value text
        if self.show_value:
            font = get_font(10)
            text = f"{self.value}"
            text_surf = font.render(text, True, COLORS["text_value"])
            text_x = self.rect.x + (self.rect.width - text_surf.get_width()) // 2
            text_y = self.rect.y + (self.rect.height - text_surf.get_height()) // 2
            surface.blit(text_surf, (text_x, text_y))
    
    def _render_continuous(self, surface: pygame.Surface, fill_width: int) -> None:
        """Render as continuous bar."""
        fill_rect = pygame.Rect(
            self.rect.x + 2,
            self.rect.y + 2,
            fill_width,
            self.rect.height - 4
        )
        
        # Gradient-like effect using two colors
        pygame.draw.rect(surface, COLORS["cyan_dim"], fill_rect)
        
        # Brighter top half
        highlight_rect = pygame.Rect(
            fill_rect.x,
            fill_rect.y,
            fill_rect.width,
            fill_rect.height // 2
        )
        pygame.draw.rect(surface, COLORS["cyan_mid"], highlight_rect)
    
    def _render_segmented(self, surface: pygame.Surface, fill_width: int) -> None:
        """Render as segmented bar."""
        total_width = self.rect.width - 4
        segment_width = total_width // self.segments
        gap = 1
        
        for i in range(self.segments):
            seg_x = self.rect.x + 2 + i * segment_width
            seg_width = segment_width - gap
            
            # Determine if this segment is filled
            segment_fill = (i + 1) / self.segments
            
            if segment_fill <= self.normalized_value:
                color = COLORS["cyan"]
            elif segment_fill - (1 / self.segments) < self.normalized_value:
                # Partially filled segment
                color = COLORS["cyan_dim"]
            else:
                color = COLORS["bg_panel"]
            
            seg_rect = pygame.Rect(
                seg_x,
                self.rect.y + 2,
                seg_width,
                self.rect.height - 4
            )
            pygame.draw.rect(surface, color, seg_rect)


class ToggleSwitch(Widget):
    """
    A simple ON/OFF toggle display.
    
    Shows current state with VFD-style text.
    """
    
    def __init__(
        self,
        rect: Rect,
        state: bool = False,
        on_text: str = "ON",
        off_text: str = "OFF"
    ):
        """
        Initialize toggle switch.
        
        Args:
            rect: Position and size
            state: Initial state (True = ON)
            on_text: Text to show when ON
            off_text: Text to show when OFF
        """
        super().__init__(rect, focusable=False)
        
        self.state = state
        self.on_text = on_text
        self.off_text = off_text
    
    def set_state(self, state: bool) -> None:
        """Set the toggle state."""
        if self.state != state:
            self.state = state
            self._dirty = True
    
    def toggle(self) -> bool:
        """Toggle the state and return new state."""
        self.state = not self.state
        self._dirty = True
        return self.state
    
    def render(self, surface: pygame.Surface) -> None:
        """Render the toggle switch."""
        if not self.visible:
            return
        
        # Choose colors based on state
        if self.state:
            text = self.on_text
            color = COLORS["active"]
            bg_color = COLORS["bg_dark"]
        else:
            text = self.off_text
            color = COLORS["inactive"]
            bg_color = COLORS["bg_dark"]
        
        # Background
        pygame.draw.rect(surface, bg_color, self.rect.to_pygame())
        
        # Text
        font = get_font(12, bold=True)
        text_surf = font.render(text, True, color)
        text_x = self.rect.x + (self.rect.width - text_surf.get_width()) // 2
        text_y = self.rect.y + (self.rect.height - text_surf.get_height()) // 2
        surface.blit(text_surf, (text_x, text_y))


class ValueDisplay(Widget):
    """
    A labeled value display.
    
    Shows a label and value, useful for temperature readings, etc.
    """
    
    def __init__(
        self,
        rect: Rect,
        label: str = "",
        value: str = "",
        unit: str = ""
    ):
        """
        Initialize value display.
        
        Args:
            rect: Position and size
            label: Label text (e.g., "IN", "OUT")
            value: Value text
            unit: Unit suffix (e.g., "°C")
        """
        super().__init__(rect, focusable=False)
        
        self.label = label
        self.value = value
        self.unit = unit
    
    def set_value(self, value: str) -> None:
        """Update the displayed value."""
        if self.value != value:
            self.value = value
            self._dirty = True
    
    def render(self, surface: pygame.Surface) -> None:
        """Render the value display."""
        if not self.visible:
            return
        
        font_label = get_font(8)
        font_value = get_font(14, bold=True)
        
        # Calculate positions
        center_x = self.rect.x + self.rect.width // 2
        
        # Draw label (top)
        if self.label:
            label_surf = font_label.render(self.label, True, COLORS["text_secondary"])
            label_x = center_x - label_surf.get_width() // 2
            surface.blit(label_surf, (label_x, self.rect.y))
        
        # Draw value with unit (bottom)
        value_text = f"{self.value}{self.unit}"
        value_surf = font_value.render(value_text, True, COLORS["text_value"])
        value_x = center_x - value_surf.get_width() // 2
        value_y = self.rect.y + self.rect.height - value_surf.get_height()
        surface.blit(value_surf, (value_x, value_y))
