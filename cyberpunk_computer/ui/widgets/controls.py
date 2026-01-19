"""
Control widgets for user input.

Contains specialized widgets for volume sliders, toggles, etc.
"""

import pygame
from typing import Optional, Callable

from .base import Widget, Rect
from ..colors import COLORS, lerp_color
from ..fonts import get_font, get_mono_font, get_tiny_font


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
        
        # Show value text (tiny font for volume numbers)
        if self.show_value:
            font = get_tiny_font(8)
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
        # Inner area (inside border)
        inner_x = self.rect.x + 1
        inner_width = self.rect.width - 2
        inner_y = self.rect.y + 1
        inner_height = self.rect.height - 2
        
        gap = 1
        # Calculate segment width to fill exactly the available space
        total_gaps = (self.segments - 1) * gap
        segment_width = (inner_width - total_gaps) / self.segments
        
        for i in range(self.segments):
            # Use float calculation for position to avoid gaps
            seg_x = inner_x + i * (segment_width + gap)
            seg_w = segment_width
            
            # For last segment, extend to fill remaining space
            if i == self.segments - 1:
                seg_w = inner_x + inner_width - seg_x
            
            # Determine if this segment is filled
            filled_segments = round(self.normalized_value * self.segments)
            
            if i < filled_segments:
                color = COLORS["cyan"]
            else:
                color = COLORS["bg_panel"]
            
            seg_rect = pygame.Rect(
                int(seg_x),
                inner_y,
                int(seg_w),
                inner_height
            )
            pygame.draw.rect(surface, color, seg_rect)


class ToggleSwitch(Widget):
    """
    A toggle switch with encoder-friendly editing mode.
    
    Shows current state with VFD-style text.
    Enter activates editing, left/right arrows change state.
    """
    
    def __init__(
        self,
        rect: Rect,
        state: bool = False,
        on_text: str = "ON",
        off_text: str = "OFF",
        label: str = ""
    ):
        """
        Initialize toggle switch.
        
        Args:
            rect: Position and size
            state: Initial state (True = ON)
            on_text: Text to show when ON
            off_text: Text to show when OFF
            label: Optional label to show
        """
        super().__init__(rect, focusable=False)
        
        self.state = state
        self.on_text = on_text
        self.off_text = off_text
        self.label = label
        self._editing = False
    
    @property
    def editing(self) -> bool:
        """Check if currently in editing mode."""
        return self._editing
    
    def start_editing(self) -> None:
        """Enter editing mode."""
        self._editing = True
        self._dirty = True
    
    def stop_editing(self) -> None:
        """Exit editing mode."""
        self._editing = False
        self._dirty = True
    
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
        
        # Choose colors based on state and editing mode
        if self._editing:
            bg_color = COLORS["bg_frame_focus"]
            border_color = COLORS["border_active"]
        else:
            bg_color = COLORS["bg_dark"]
            border_color = None
        
        if self.state:
            text = self.on_text
            text_color = COLORS["active"]
        else:
            text = self.off_text
            text_color = COLORS["inactive"]
        
        # Background
        pygame.draw.rect(surface, bg_color, self.rect.to_pygame())
        
        # Border when editing
        if border_color:
            pygame.draw.rect(surface, border_color, self.rect.to_pygame(), 1)
        
        # Build display text with arrows if editing
        if self._editing:
            display_text = f"< {text} >"
        else:
            display_text = text
        
        # Text (use mono font for toggle labels)
        font = get_mono_font(11)
        text_surf = font.render(display_text, True, text_color)
        text_x = self.rect.x + (self.rect.width - text_surf.get_width()) // 2
        text_y = self.rect.y + (self.rect.height - text_surf.get_height()) // 2
        surface.blit(text_surf, (text_x, text_y))


class StatusIcon(Widget):
    """
    A read-only status icon display.
    
    Shows status with an icon/text but cannot be toggled directly.
    Used for informational indicators that are controlled elsewhere.
    """
    
    def __init__(
        self,
        rect: Rect,
        label: str = "",
        active: bool = False
    ):
        """
        Initialize status icon.
        
        Args:
            rect: Position and size
            label: Label text
            active: Whether the status is active
        """
        super().__init__(rect, focusable=False)
        
        self.label = label
        self._active = active
    
    def set_active(self, active: bool) -> None:
        """Set the active state."""
        if self._active != active:
            self._active = active
            self._dirty = True
    
    def render(self, surface: pygame.Surface) -> None:
        """Render the status icon."""
        if not self.visible:
            return
        
        # Choose color based on state
        if self._active:
            color = COLORS["cyan"]
        else:
            color = COLORS["inactive"]
        
        # Text
        font = get_mono_font(11)
        text_surf = font.render(self.label, True, color)
        text_x = self.rect.x + (self.rect.width - text_surf.get_width()) // 2
        text_y = self.rect.y + (self.rect.height - text_surf.get_height()) // 2
        surface.blit(text_surf, (text_x, text_y))


class ValueDisplay(Widget):
    """
    A labeled value display.
    
    Shows a label and value, useful for temperature readings, etc.
    Compact vertical layout: label on top, value directly below.
    """
    
    def __init__(
        self,
        rect: Rect,
        label: str = "",
        value: str = "",
        unit: str = "",
        compact: bool = False
    ):
        """
        Initialize value display.
        
        Args:
            rect: Position and size
            label: Label text (e.g., "IN", "OUT")
            value: Value text
            unit: Unit suffix (e.g., "°C")
            compact: Use compact vertical layout
        """
        super().__init__(rect, focusable=False)
        
        self.label = label
        self.value = value
        self.unit = unit
        self.compact = compact
    
    def set_value(self, value: str) -> None:
        """Update the displayed value."""
        if self.value != value:
            self.value = value
            self._dirty = True
    
    def render(self, surface: pygame.Surface) -> None:
        """Render the value display."""
        if not self.visible:
            return
        
        # Use tiny font for small labels, mono for values
        font_label = get_tiny_font(8)
        font_value = get_mono_font(13)
        
        # Calculate positions
        center_x = self.rect.x + self.rect.width // 2
        
        if self.compact:
            # Compact layout: label and value close together at top
            y_offset = self.rect.y + 2
            
            # Draw label (top)
            if self.label:
                label_surf = font_label.render(self.label, True, COLORS["text_secondary"])
                label_x = center_x - label_surf.get_width() // 2
                surface.blit(label_surf, (label_x, y_offset))
                y_offset += label_surf.get_height() + 1
            
            # Draw value with unit (directly below label)
            value_text = f"{self.value}{self.unit}"
            value_surf = font_value.render(value_text, True, COLORS["text_value"])
            value_x = center_x - value_surf.get_width() // 2
            surface.blit(value_surf, (value_x, y_offset))
        else:
            # Original layout: label top, value bottom
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


class ModeIcon(Widget):
    """
    A simple icon display using text characters/symbols.
    
    Displays mode indicators like fan, AC, recirculation, etc.
    Uses Unicode symbols or custom characters.
    """
    
    # Common climate mode icons (ASCII-safe alternatives)
    ICONS = {
        "fan": "*",       # Fan/blower
        "ac": "#",        # AC cooling
        "heat": "~",      # Heating
        "recirc": "@",    # Recirculation
        "auto": "A",      # Auto mode
        "eco": "E",       # Eco mode
        "defrost": "D",   # Defrost
    }
    
    def __init__(
        self,
        rect: Rect,
        icon: str = "auto",
        active: bool = False,
        label: str = ""
    ):
        """
        Initialize mode icon.
        
        Args:
            rect: Position and size
            icon: Icon key from ICONS dict or custom character
            active: Whether the mode is active
            label: Optional label below icon
        """
        super().__init__(rect, focusable=False)
        
        self.icon = icon
        self._active = active
        self.label = label
    
    @property
    def icon_char(self) -> str:
        """Get the icon character to display."""
        return self.ICONS.get(self.icon, self.icon)
    
    def set_active(self, active: bool) -> None:
        """Set the active state."""
        if self._active != active:
            self._active = active
            self._dirty = True
    
    def render(self, surface: pygame.Surface) -> None:
        """Render the mode icon."""
        if not self.visible:
            return
        
        # Choose color based on state
        if self._active:
            icon_color = COLORS["cyan"]
            label_color = COLORS["text_primary"]
        else:
            icon_color = COLORS["inactive"]
            label_color = COLORS["text_secondary"]
        
        center_x = self.rect.x + self.rect.width // 2
        
        # Draw icon (using mono font for symbols)
        font_icon = get_mono_font(14)
        icon_surf = font_icon.render(self.icon_char, True, icon_color)
        icon_x = center_x - icon_surf.get_width() // 2
        icon_y = self.rect.y + 2
        surface.blit(icon_surf, (icon_x, icon_y))
        
        # Draw label if present (tiny font for small labels)
        if self.label:
            font_label = get_tiny_font(8)
            label_surf = font_label.render(self.label, True, label_color)
            label_x = center_x - label_surf.get_width() // 2
            label_y = self.rect.bottom - label_surf.get_height() - 1
            surface.blit(label_surf, (label_x, label_y))

