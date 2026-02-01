"""
Power Bars Component - Instant power visualization.

Shows two vertical bars for MG power and fuel/brake.
"""

from typing import List, Set
from ..framebuffer import VFDFramebuffer
from ..icons import ICON_LIGHTNING, ICON_ENGINE


class PowerBarsComponent:
    """
    Instant power bars for VFD display.
    
    Uses the rightmost quarter of the display (x: 192-255, 64x48 pixels).
    Shows two vertical bars:
    - Left: MG power (up = assist, down = regen)
    - Right: Fuel consumption (up) / Braking (down)
    """
    
    REGION_X = 192
    REGION_WIDTH = 64
    REGION_HEIGHT = 48
    
    # EMA smoothing factor
    EMA_ALPHA = 0.15
    
    def __init__(self, framebuffer: VFDFramebuffer):
        """Initialize power bars."""
        self.fb = framebuffer
        
        # Display values (smoothed)
        self._mg_power: float = 0.0
        self._fuel_brake: float = 0.0
        
        # Target values
        self._mg_power_target: float = 0.0
        self._fuel_brake_target: float = 0.0
        
        # Bar layout
        self._bar_width = 30
        self._bar_height = self.REGION_HEIGHT
        self._bar_y = 0
        
        bar_spacing = 2
        total_width = self._bar_width * 2 + bar_spacing
        start_x = self.REGION_X + (self.REGION_WIDTH - total_width) // 2
        
        self._mg_bar_x = start_x
        self._fuel_bar_x = start_x + self._bar_width + bar_spacing
    
    def _ema(self, current: float, target: float) -> float:
        """Apply EMA smoothing."""
        return self.EMA_ALPHA * target + (1.0 - self.EMA_ALPHA) * current
    
    def update(
        self,
        mg_power: float,
        fuel_flow: float,
        brake: float,
        ice_running: bool
    ) -> None:
        """
        Update power bar targets.
        
        Args:
            mg_power: Normalized MG power (-1.0 to +1.0)
            fuel_flow: Normalized fuel flow (0.0 to 1.0)
            brake: Normalized brake pressure (0.0 to 1.0)
            ice_running: Whether ICE is running
        """
        self._mg_power_target = max(-1.0, min(1.0, mg_power))
        
        if brake > 0.04:  # Brake threshold
            self._fuel_brake_target = -brake
        elif ice_running and fuel_flow > 0.01:
            self._fuel_brake_target = fuel_flow
        else:
            self._fuel_brake_target = 0.0
    
    def tick(self) -> None:
        """Apply EMA smoothing."""
        self._mg_power = self._ema(self._mg_power, self._mg_power_target)
        self._fuel_brake = self._ema(self._fuel_brake, self._fuel_brake_target)
    
    def render(self) -> None:
        """Render power bars to framebuffer."""
        self.tick()
        
        # Clear region
        for y in range(self.REGION_HEIGHT):
            for x in range(self.REGION_X, self.REGION_X + self.REGION_WIDTH):
                self.fb.set_pixel(x, y, False)
        
        self._render_bar(self._mg_bar_x, self._mg_power, ICON_LIGHTNING)
        self._render_bar(self._fuel_bar_x, self._fuel_brake, ICON_ENGINE)
    
    def _render_bar(self, bar_x: int, value: float, icon: List[List[int]]) -> None:
        """Render a single vertical bar with icon."""
        bar_y = self._bar_y
        bar_w = self._bar_width
        bar_h = self._bar_height
        
        center_y = bar_y + bar_h // 2
        
        # Draw center line (dotted)
        for x in range(bar_x, bar_x + bar_w, 3):
            self.fb.set_pixel(x, center_y, True)
        
        max_fill = bar_h // 2 - 1
        fill_height = int(abs(value) * max_fill)
        
        bar_pixels: Set[tuple] = set()
        
        if fill_height > 0:
            if value > 0:
                fill_y_start = center_y - fill_height
                fill_y_end = center_y
            else:
                fill_y_start = center_y + 1
                fill_y_end = center_y + 1 + fill_height
            
            for y in range(fill_y_start, fill_y_end):
                for x in range(bar_x + 1, bar_x + bar_w - 1):
                    self.fb.set_pixel(x, y, True)
                    bar_pixels.add((x, y))
        
        # Draw icon at 1/4 height (XOR with bar)
        icon_h = len(icon)
        icon_w = len(icon[0]) if icon else 0
        icon_x = bar_x + (bar_w - icon_w) // 2
        icon_center_y = self.REGION_HEIGHT // 4
        icon_y = icon_center_y - icon_h // 2
        
        for row_idx, row in enumerate(icon):
            for col_idx, pixel in enumerate(row):
                if pixel:
                    px = icon_x + col_idx
                    py = icon_y + row_idx
                    if (px, py) in bar_pixels:
                        self.fb.set_pixel(px, py, False)
                    else:
                        self.fb.set_pixel(px, py, True)
