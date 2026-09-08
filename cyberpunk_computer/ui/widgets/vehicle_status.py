"""
Connection status indicator widget.

Small pulsing dot showing gateway connection / traffic state.
"""

import pygame

from .base import Widget, Rect
from ..colors import COLORS

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
