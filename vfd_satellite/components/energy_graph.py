"""
Energy Graph Component - Historical MG power visualization.

Shows a scrolling graph of MG power over time with configurable time base.
"""

import time
import math
from typing import List, Tuple, Optional
from ..framebuffer import VFDFramebuffer


class EnergyGraphComponent:
    """
    Energy monitor for VFD display.
    
    Uses the third quarter of the display (x: 128-191, 64x48 pixels).
    Shows MG Assist/Regen graph with time-based integration.
    """
    
    REGION_X = 128
    REGION_WIDTH = 64
    REGION_HEIGHT = 48
    
    def __init__(self, framebuffer: VFDFramebuffer, time_base_sec: float = 60.0):
        """Initialize energy graph."""
        self.fb = framebuffer
        
        self._time_base_sec = time_base_sec
        self._graph_w = self.REGION_WIDTH
        self._graph_h = self.REGION_HEIGHT
        
        # Time per pixel column
        self._time_per_pixel = self._time_base_sec / self._graph_w
        
        # History buffer: (assist_sum, regen_sum, sample_count, ice_was_running)
        self._history: List[Tuple[float, float, int, bool]] = [(0.0, 0.0, 0, False)] * self._graph_w
        
        self._current_column = self._graph_w - 1
        self._last_tick_time: Optional[float] = None
        self._column_start_time: Optional[float] = None
        
        # Current values
        self._mg_power: float = 0.0
        self._ice_running: bool = False
        self._current_column_ice_active: bool = False
    
    def update(self, mg_power: float, ice_running: bool) -> None:
        """
        Update energy data.
        
        Args:
            mg_power: Normalized MG power (-1.0 to +1.0)
            ice_running: Whether ICE is running
        """
        current_time = time.time()
        
        if self._last_tick_time is None:
            self._last_tick_time = current_time
            self._column_start_time = current_time
        
        self._mg_power = max(-1.0, min(1.0, mg_power))
        self._ice_running = ice_running
        
        if ice_running:
            self._current_column_ice_active = True
        
        # Accumulate assist and regen separately
        assist_sum, regen_sum, count, ice_was_running = self._history[self._current_column]
        
        if self._mg_power > 0:
            assist_sum += self._mg_power
        else:
            regen_sum += abs(self._mg_power)
        
        self._history[self._current_column] = (
            assist_sum, regen_sum, count + 1,
            ice_was_running or self._current_column_ice_active
        )
        
        # Check for column advancement
        elapsed = current_time - self._column_start_time
        if elapsed >= self._time_per_pixel:
            self._advance_column(current_time)
        
        self._last_tick_time = current_time
    
    def set_time_base(self, seconds: float) -> None:
        """
        Change the time base for the power chart.
        
        Clears all historical data when time base changes.
        """
        self._time_base_sec = seconds
        self._time_per_pixel = self._time_base_sec / self._graph_w
        
        # Clear history when time base changes
        self._history = [(0.0, 0.0, 0, False)] * self._graph_w
        self._current_column = self._graph_w - 1
        self._column_start_time = time.time()
        self._current_column_ice_active = False
    
    def _get_display_exponent(self) -> float:
        """Calculate adaptive display exponent based on time base."""
        min_time, max_time = 15.0, 3600.0
        time_base = max(min_time, min(max_time, self._time_base_sec))
        log_ratio = (math.log(time_base) - math.log(min_time)) / (math.log(max_time) - math.log(min_time))
        return 1.0 - 0.7 * log_ratio
    
    def _advance_column(self, current_time: float) -> None:
        """Advance to the next column."""
        self._current_column_ice_active = self._ice_running
        self._current_column += 1
        
        if self._current_column >= self._graph_w:
            self._history.pop(0)
            self._history.append((0.0, 0.0, 0, self._ice_running))
            self._current_column = self._graph_w - 1
        
        self._column_start_time = current_time
    
    def tick(self) -> None:
        """Advance time-based animation."""
        current_time = time.time()
        
        if self._column_start_time is None:
            self._column_start_time = current_time
            self._last_tick_time = current_time
            return
        
        elapsed = current_time - self._column_start_time
        columns_to_advance = int(elapsed / self._time_per_pixel)
        
        for _ in range(columns_to_advance):
            self._advance_column(current_time)
    
    def render(self) -> None:
        """Render energy graph to framebuffer."""
        self.tick()
        
        # Clear region
        for y in range(self.REGION_HEIGHT):
            for x in range(self.REGION_X, self.REGION_X + self.REGION_WIDTH):
                self.fb.set_pixel(x, y, False)
        
        exponent = self._get_display_exponent()
        center_y = self.REGION_HEIGHT // 2
        
        # Draw center line (dotted)
        for x in range(self.REGION_X, self.REGION_X + self.REGION_WIDTH, 3):
            self.fb.set_pixel(x, center_y, True)
        
        # Draw history
        for i in range(self._current_column + 1):
            x = self.REGION_X + i
            assist_sum, regen_sum, count, ice_was_running = self._history[i]
            
            # ICE indicator at bottom
            if ice_was_running:
                self.fb.set_pixel(x, self.REGION_HEIGHT - 1, True)
            
            if count > 0:
                assist_val = assist_sum / count
                regen_val = regen_sum / count
            else:
                assist_val, regen_val = 0.0, 0.0
            
            # Apply power curve
            if assist_val > 0.001:
                assist_val = assist_val ** exponent
            if regen_val > 0.001:
                regen_val = regen_val ** exponent
            
            max_offset = self.REGION_HEIGHT // 2 - 1
            
            # Draw assist (upward)
            if assist_val > 0.01:
                pixels = min(int(assist_val * max_offset), max_offset)
                for dy in range(pixels):
                    self.fb.set_pixel(x, center_y - dy - 1, True)
            
            # Draw regen (downward)
            if regen_val > 0.01:
                pixels = min(int(regen_val * max_offset), max_offset)
                for dy in range(pixels):
                    self.fb.set_pixel(x, center_y + dy + 1, True)
        
        # Current value indicator
        val_str = f"{self._mg_power * 30:+.0f}"
        text_x = self.REGION_X + self.REGION_WIDTH - len(val_str) * 4 - 2
        self.fb.draw_text_3x5_xor(text_x, 1, val_str)
