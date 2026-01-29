"""
Vehicle Status Widget.

Displays compact status indicators for:
- AVC-LAN connection status
- Volume level
- Climate temperature
- Vehicle mode
"""

import pygame
from dataclasses import dataclass
from typing import Optional, Tuple, List

from .base import Widget, Rect
from ..colors import COLORS, dim_color


@dataclass
class StatusItem:
    """Individual status indicator."""
    label: str
    value: str
    icon: Optional[str] = None
    color: Tuple[int, int, int] = COLORS["cyan_bright"]
    alert: bool = False


class VehicleStatusWidget(Widget):
    """
    Displays multiple status items in a compact format.
    
    Used in center area to show quick status overview.
    """
    
    def __init__(self, rect: Rect):
        """Initialize vehicle status widget."""
        super().__init__(rect, focusable=False)
        
        # Status items
        self._items: List[StatusItem] = []
        
        # Default items
        self._connection_status = "OFFLINE"
        self._volume = 0
        self._temperature = 22.0
        self._mode = "---"
        
        self._update_items()
        
    def _update_items(self) -> None:
        """Update status items from current state."""
        self._items = [
            StatusItem(
                label="AVC",
                value=self._connection_status,
                color=COLORS["active"] if self._connection_status == "ONLINE" else COLORS["error"]
            ),
            StatusItem(
                label="VOL",
                value=str(self._volume),
                color=COLORS["cyan_bright"]
            ),
            StatusItem(
                label="TEMP",
                value=f"{self._temperature:.0f}°",
                color=COLORS["orange"]
            ),
            StatusItem(
                label="MODE",
                value=self._mode,
                color=COLORS["magenta"]
            ),
        ]
        self._dirty = True
        
    def set_connection_status(self, online: bool) -> None:
        """Set AVC-LAN connection status."""
        self._connection_status = "ONLINE" if online else "OFFLINE"
        self._update_items()
        
    def set_volume(self, volume: int) -> None:
        """Set current volume level."""
        self._volume = volume
        self._update_items()
        
    def set_temperature(self, temp: float) -> None:
        """Set current temperature."""
        self._temperature = temp
        self._update_items()
        
    def set_mode(self, mode: str) -> None:
        """Set vehicle mode (PARK, DRIVE, etc.)."""
        self._mode = mode
        self._update_items()
        
    def render(self, surface: pygame.Surface) -> None:
        """Render status indicators."""
        if not self.visible:
            return
            
        from ..fonts import get_tiny_font
        font = get_tiny_font(8)
        
        # Calculate layout
        num_items = len(self._items)
        if num_items == 0:
            return
            
        item_width = self.rect.width // num_items
        
        for i, item in enumerate(self._items):
            x = self.rect.x + i * item_width
            y = self.rect.y
            
            # Label
            label_surf = font.render(item.label, True, dim_color(item.color, 0.6))
            surface.blit(label_surf, (x + 2, y))
            
            # Value
            value_surf = font.render(item.value, True, item.color)
            surface.blit(value_surf, (x + 2, y + 10))
            
            # Separator
            if i < num_items - 1:
                pygame.draw.line(
                    surface,
                    dim_color(COLORS["cyan_dim"], 0.3),
                    (x + item_width - 1, y),
                    (x + item_width - 1, y + self.rect.height),
                    1
                )


class ConnectionIndicator(Widget):
    """
    Simple connection status dot.
    
    Shows connection state with pulsing animation.
    """
    
    def __init__(self, rect: Rect):
        """Initialize connection indicator."""
        super().__init__(rect, focusable=False)
        
        self.connected = False
        self.receiving = False
        self._pulse_time = 0.0
        self._last_rx_time = 0.0
        
    def update(self, dt: float) -> None:
        """Update animation state."""
        super().update(dt)
        self._pulse_time += dt
        self._last_rx_time += dt
        
        if self._last_rx_time > 0.5:
            self.receiving = False
            
        self._dirty = True
        
    def set_connected(self, connected: bool) -> None:
        """Set connection state."""
        self.connected = connected
        self._dirty = True
        
    def on_message_received(self) -> None:
        """Called when a message is received."""
        self.receiving = True
        self._last_rx_time = 0.0
        self._dirty = True
        
    def render(self, surface: pygame.Surface) -> None:
        """Render connection indicator."""
        if not self.visible:
            return
            
        import math
        
        cx, cy = self.rect.center
        radius = min(self.rect.width, self.rect.height) // 2 - 1
        
        if self.connected:
            # Pulsing effect when receiving
            if self.receiving:
                pulse = 0.6 + 0.4 * math.sin(self._pulse_time * 10)
                color = (
                    int(COLORS["active"][0] * pulse),
                    int(COLORS["active"][1] * pulse),
                    int(COLORS["active"][2] * pulse)
                )
            else:
                color = COLORS["active"]
        else:
            # Slow pulse when disconnected
            pulse = 0.3 + 0.2 * math.sin(self._pulse_time * 2)
            color = (
                int(COLORS["error"][0] * pulse),
                int(COLORS["error"][1] * pulse),
                int(COLORS["error"][2] * pulse)
            )
            
        pygame.draw.circle(surface, color, (cx, cy), radius)
        pygame.draw.circle(surface, COLORS["cyan_dim"], (cx, cy), radius, 1)
