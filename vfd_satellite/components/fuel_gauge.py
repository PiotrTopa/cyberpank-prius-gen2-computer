"""
Fuel Gauge Component - Fuel levels and active fuel indicator.

Shows petrol, LPG, and battery levels with active fuel indicator.
"""

from ..framebuffer import VFDFramebuffer
from ..state import FuelType


class FuelGaugeComponent:
    """
    Fuel gauge for VFD display.
    
    Uses the first quarter of the display (x: 0-63, 64x48 pixels).
    Layout:
    - Top: 3 vertical bars (Petrol, LPG, Battery)
    - Bottom: 3 indicators (PTR, LPG, BTT)
    """
    
    REGION_X = 0
    REGION_WIDTH = 64
    REGION_HEIGHT = 48
    
    def __init__(self, framebuffer: VFDFramebuffer):
        """Initialize fuel gauge."""
        self.fb = framebuffer
        
        # Current levels
        self._petrol_level: int = 0
        self._lpg_level: int = 0
        self._battery_soc: float = 0.6
        self._active_fuel: FuelType = FuelType.OFF
        
        # Capacities
        self._petrol_max = 45
        self._lpg_max = 60
    
    def update(
        self,
        petrol_level: int,
        lpg_level: int,
        battery_soc: float,
        active_fuel: FuelType
    ) -> None:
        """Update fuel gauge data."""
        self._petrol_level = petrol_level
        self._lpg_level = lpg_level
        self._battery_soc = battery_soc
        self._active_fuel = active_fuel
    
    def render(self) -> None:
        """Render fuel gauge to the framebuffer."""
        # Clear region
        for y in range(self.REGION_HEIGHT):
            for x in range(self.REGION_X, self.REGION_X + self.REGION_WIDTH):
                self.fb.set_pixel(x, y, False)
        
        indicator_height = 9
        indicator_y = self.REGION_HEIGHT - indicator_height
        bar_height = indicator_y
        
        bar_width = 19
        bar_gap = 2
        bar_y = 0
        
        petrol_bar_x = self.REGION_X + 2
        lpg_bar_x = petrol_bar_x + bar_width + bar_gap
        battery_bar_x = lpg_bar_x + bar_width + bar_gap
        
        # Render bars
        self._render_fuel_bar(petrol_bar_x, bar_y, bar_width, bar_height,
                             self._petrol_level, self._petrol_max)
        self._render_fuel_bar(lpg_bar_x, bar_y, bar_width, bar_height,
                             self._lpg_level, self._lpg_max)
        self._render_battery_bar(battery_bar_x, bar_y, bar_width, bar_height,
                                self._battery_soc)
        
        # Render indicators
        self._render_indicators(indicator_y)
    
    def _render_fuel_bar(self, x: int, y: int, w: int, h: int, level: int, max_level: int) -> None:
        """Render a vertical fuel level bar."""
        num_segments = 8
        if max_level > 0:
            fill_ratio = min(1.0, max(0.0, level / max_level))
        else:
            fill_ratio = 0.0
        
        filled_segments = int(fill_ratio * num_segments + 0.5)
        
        inner_x = x + 1
        inner_w = w - 2
        segment_h = 4
        segment_gap = 1
        
        for i in range(num_segments):
            seg_y = y + h - (i + 1) * segment_h - i * segment_gap
            
            if i < filled_segments:
                for py in range(seg_y, seg_y + segment_h):
                    for px in range(inner_x, inner_x + inner_w):
                        self.fb.set_pixel(px, py, True)
    
    def _render_battery_bar(self, x: int, y: int, w: int, h: int, soc: float) -> None:
        """Render battery bar with Prius Gen 2 SOC mapping."""
        # Authentic mapping
        if soc >= 0.75:
            filled_segments = 8
        elif soc >= 0.70:
            filled_segments = 7
        elif soc >= 0.60:
            filled_segments = 6
        elif soc >= 0.55:
            filled_segments = 5
        elif soc >= 0.50:
            filled_segments = 4
        elif soc >= 0.45:
            filled_segments = 3
        elif soc >= 0.40:
            filled_segments = 2
        elif soc >= 0.35:
            filled_segments = 1
        else:
            filled_segments = 0
        
        num_segments = 8
        inner_x = x + 1
        inner_w = w - 2
        segment_h = 4
        segment_gap = 1
        
        for i in range(num_segments):
            seg_y = y + h - (i + 1) * segment_h - i * segment_gap
            
            if i < filled_segments:
                for py in range(seg_y, seg_y + segment_h):
                    for px in range(inner_x, inner_x + inner_w):
                        self.fb.set_pixel(px, py, True)
    
    def _render_indicators(self, y: int) -> None:
        """Render fuel type indicators at bottom."""
        bar_width = 19
        bar_gap = 2
        
        indicators = [
            ("PTR", self.REGION_X + 2, self._active_fuel == FuelType.PTR),
            ("LPG", self.REGION_X + 2 + bar_width + bar_gap, self._active_fuel == FuelType.LPG),
            ("BTT", self.REGION_X + 2 + 2 * (bar_width + bar_gap), False),
        ]
        
        for text, base_x, is_active in indicators:
            text_width = 11
            text_x = base_x + (bar_width - text_width) // 2
            text_y = y + 2
            
            self.fb.draw_text_3x5(text_x, text_y, text, True)
            
            if is_active:
                text_center_y = text_y + 2
                
                # Left arrow
                arrow_left_x = text_x - 2
                self.fb.set_pixel(arrow_left_x, text_center_y, True)
                self.fb.set_pixel(arrow_left_x - 1, text_center_y - 1, True)
                self.fb.set_pixel(arrow_left_x - 1, text_center_y + 1, True)
                
                # Right arrow
                arrow_right_x = text_x + text_width + 1
                self.fb.set_pixel(arrow_right_x, text_center_y, True)
                self.fb.set_pixel(arrow_right_x + 1, text_center_y - 1, True)
                self.fb.set_pixel(arrow_right_x + 1, text_center_y + 1, True)
