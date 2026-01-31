"""
VFD Display Simulator Widget.

Simulates a 256x48 dot matrix VFD display for testing.
Uses only basic pixel drawing operations compatible with RP2040 MicroPython.

This module is designed for easy extraction/porting to RP2040 MicroPython later.
All drawing uses rudimental operations: set_pixel, draw_line, draw_rect, fill_rect.
"""

import pygame
import time
from typing import Tuple, List, Optional
from dataclasses import dataclass

from .base import Widget, Rect
from .vfd_icons import ICON_LIGHTNING, ICON_ENGINE


# VFD Display dimensions (actual hardware)
VFD_WIDTH = 256
VFD_HEIGHT = 48


# ==============================================================================
# VFD Framebuffer - Portable drawing primitives
# ==============================================================================
# This class uses only basic operations that can be ported to MicroPython.
# No fancy pygame features - just pixel manipulation.

class VFDFramebuffer:
    """
    Binary framebuffer for VFD display.
    
    Uses only basic drawing primitives compatible with RP2040 MicroPython.
    All pixels are binary (on/off) - no shading.
    """
    
    def __init__(self, width: int = VFD_WIDTH, height: int = VFD_HEIGHT):
        """Initialize framebuffer with given dimensions."""
        self.width = width
        self.height = height
        # Binary pixel buffer: 0 = off, 1 = on
        self._buffer: List[List[int]] = [[0] * width for _ in range(height)]
    
    def clear(self) -> None:
        """Clear the framebuffer (all pixels off)."""
        for y in range(self.height):
            for x in range(self.width):
                self._buffer[y][x] = 0
    
    def set_pixel(self, x: int, y: int, on: bool = True) -> None:
        """Set a single pixel on or off."""
        if 0 <= x < self.width and 0 <= y < self.height:
            self._buffer[y][x] = 1 if on else 0
    
    def get_pixel(self, x: int, y: int) -> bool:
        """Get pixel state at position."""
        if 0 <= x < self.width and 0 <= y < self.height:
            return self._buffer[y][x] == 1
        return False
    
    def draw_hline(self, x: int, y: int, length: int, on: bool = True) -> None:
        """Draw a horizontal line."""
        for i in range(length):
            self.set_pixel(x + i, y, on)
    
    def draw_vline(self, x: int, y: int, length: int, on: bool = True) -> None:
        """Draw a vertical line."""
        for i in range(length):
            self.set_pixel(x, y + i, on)
    
    def draw_line(self, x0: int, y0: int, x1: int, y1: int, on: bool = True) -> None:
        """Draw a line using Bresenham's algorithm."""
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        
        while True:
            self.set_pixel(x0, y0, on)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy
    
    def draw_rect(self, x: int, y: int, w: int, h: int, on: bool = True) -> None:
        """Draw a rectangle outline."""
        self.draw_hline(x, y, w, on)              # Top
        self.draw_hline(x, y + h - 1, w, on)      # Bottom
        self.draw_vline(x, y, h, on)              # Left
        self.draw_vline(x + w - 1, y, h, on)      # Right
    
    def draw_rect_dotted(
        self, x: int, y: int, w: int, h: int, 
        spacing: int = 2, on: bool = True
    ) -> None:
        """
        Draw a rectangle outline with dotted lines.
        
        Args:
            x, y, w, h: Rectangle bounds
            spacing: Pixels between dots (2 = every other pixel)
            on: Pixel state
        """
        # Top edge
        for i in range(0, w, spacing):
            self.set_pixel(x + i, y, on)
        # Bottom edge
        for i in range(0, w, spacing):
            self.set_pixel(x + i, y + h - 1, on)
        # Left edge
        for i in range(0, h, spacing):
            self.set_pixel(x, y + i, on)
        # Right edge
        for i in range(0, h, spacing):
            self.set_pixel(x + w - 1, y + i, on)
    
    def fill_rect(self, x: int, y: int, w: int, h: int, on: bool = True) -> None:
        """Fill a rectangle."""
        for dy in range(h):
            self.draw_hline(x, y + dy, w, on)
    
    def fill_rect_dithered(
        self, x: int, y: int, w: int, h: int, 
        intensity: float = 1.0, pattern: int = 0
    ) -> None:
        """
        Fill a rectangle with dithering pattern.
        
        Args:
            x, y, w, h: Rectangle bounds
            intensity: Fill density 0.0 to 1.0 (1.0 = solid)
            pattern: Dither pattern type (0=checkerboard, 1=vertical, 2=horizontal, 3=diagonal)
        """
        for dy in range(h):
            for dx in range(w):
                px, py = x + dx, y + dy
                
                # Determine if pixel should be on based on pattern and intensity
                pixel_on = False
                
                if intensity >= 1.0:
                    pixel_on = True
                elif intensity <= 0.0:
                    pixel_on = False
                elif intensity >= 0.75:
                    # 75% - only skip every 4th pixel (checkerboard of checkerboard)
                    if pattern == 0:
                        pixel_on = not ((dx % 2 == 0) and (dy % 2 == 0) and ((dx + dy) % 4 == 0))
                    else:
                        pixel_on = not ((dx + dy) % 4 == 0)
                elif intensity >= 0.5:
                    # 50% - checkerboard pattern
                    if pattern == 0:
                        pixel_on = (dx + dy) % 2 == 0
                    elif pattern == 1:
                        pixel_on = dx % 2 == 0
                    elif pattern == 2:
                        pixel_on = dy % 2 == 0
                    else:  # pattern == 3, diagonal
                        pixel_on = (dx + dy) % 2 == 0
                elif intensity >= 0.25:
                    # 25% - sparse checkerboard
                    if pattern == 0:
                        pixel_on = (dx % 2 == 0) and (dy % 2 == 0)
                    else:
                        pixel_on = (dx + dy) % 4 == 0
                else:
                    # < 25% - very sparse
                    pixel_on = (dx % 4 == 0) and (dy % 4 == 0)
                
                if pixel_on:
                    self.set_pixel(px, py, True)
    
    def draw_char_3x5(self, x: int, y: int, char: str, on: bool = True) -> None:
        """
        Draw a single character using 3x5 pixel font.
        
        Returns the width of the character drawn (for spacing).
        """
        # Minimal 3x5 font - only digits and common symbols
        font_3x5 = {
            '0': [0b111, 0b101, 0b101, 0b101, 0b111],
            '1': [0b010, 0b110, 0b010, 0b010, 0b111],
            '2': [0b111, 0b001, 0b111, 0b100, 0b111],
            '3': [0b111, 0b001, 0b111, 0b001, 0b111],
            '4': [0b101, 0b101, 0b111, 0b001, 0b001],
            '5': [0b111, 0b100, 0b111, 0b001, 0b111],
            '6': [0b111, 0b100, 0b111, 0b101, 0b111],
            '7': [0b111, 0b001, 0b010, 0b010, 0b010],
            '8': [0b111, 0b101, 0b111, 0b101, 0b111],
            '9': [0b111, 0b101, 0b111, 0b001, 0b111],
            '-': [0b000, 0b000, 0b111, 0b000, 0b000],
            '+': [0b000, 0b010, 0b111, 0b010, 0b000],
            '.': [0b000, 0b000, 0b000, 0b000, 0b010],
            ' ': [0b000, 0b000, 0b000, 0b000, 0b000],
            'O': [0b111, 0b101, 0b101, 0b101, 0b111],
            'F': [0b111, 0b100, 0b110, 0b100, 0b100],
            'P': [0b110, 0b101, 0b110, 0b100, 0b100],
            'T': [0b111, 0b010, 0b010, 0b010, 0b010],
            'R': [0b110, 0b101, 0b110, 0b101, 0b101],
            'L': [0b100, 0b100, 0b100, 0b100, 0b111],
            'G': [0b111, 0b100, 0b101, 0b101, 0b111],
            'B': [0b110, 0b101, 0b110, 0b101, 0b110],
        }
        
        if char in font_3x5:
            rows = font_3x5[char]
            for row_idx, row in enumerate(rows):
                for col in range(3):
                    if row & (1 << (2 - col)):
                        self.set_pixel(x + col, y + row_idx, on)
    
    def draw_text_3x5(self, x: int, y: int, text: str, on: bool = True) -> int:
        """
        Draw text using 3x5 font.
        
        Returns: x position after the text (for continuation).
        """
        cursor_x = x
        for char in text.upper():
            self.draw_char_3x5(cursor_x, y, char, on)
            cursor_x += 4  # 3 pixels + 1 spacing
        return cursor_x
    
    def draw_text_3x5_xor(self, x: int, y: int, text: str) -> int:
        """
        Draw text using 3x5 font with XOR mode (inverts pixels).
        
        This makes text visible over any background by inverting pixels.
        
        Returns: x position after the text (for continuation).
        """
        cursor_x = x
        for char in text.upper():
            # XOR mode: invert pixels where character is drawn
            self._draw_char_3x5_xor(cursor_x, y, char)
            cursor_x += 4  # 3 pixels + 1 spacing
        return cursor_x
    
    def _draw_char_3x5_xor(self, x: int, y: int, char: str) -> None:
        """Draw a single character in XOR mode (inverts pixels)."""
        # Minimal 3x5 font - only digits and common symbols
        font_3x5 = {
            '0': [0b111, 0b101, 0b101, 0b101, 0b111],
            '1': [0b010, 0b110, 0b010, 0b010, 0b111],
            '2': [0b111, 0b001, 0b111, 0b100, 0b111],
            '3': [0b111, 0b001, 0b111, 0b001, 0b111],
            '4': [0b101, 0b101, 0b111, 0b001, 0b001],
            '5': [0b111, 0b100, 0b111, 0b001, 0b111],
            '6': [0b111, 0b100, 0b111, 0b101, 0b111],
            '7': [0b111, 0b001, 0b010, 0b010, 0b010],
            '8': [0b111, 0b101, 0b111, 0b101, 0b111],
            '9': [0b111, 0b101, 0b111, 0b001, 0b111],
            '-': [0b000, 0b000, 0b111, 0b000, 0b000],
            '+': [0b000, 0b010, 0b111, 0b010, 0b000],
            '.': [0b000, 0b000, 0b000, 0b000, 0b010],
            ' ': [0b000, 0b000, 0b000, 0b000, 0b000],
        }
        
        if char in font_3x5:
            rows = font_3x5[char]
            for row_idx, row in enumerate(rows):
                for col in range(3):
                    if row & (1 << (2 - col)):
                        px, py = x + col, y + row_idx
                        # XOR: flip the pixel state
                        if 0 <= px < self.width and 0 <= py < self.height:
                            self._buffer[py][px] = 1 - self._buffer[py][px]


# ==============================================================================
# VFD Fuel Gauge - Dual fuel level display with active indicator
# ==============================================================================

class VFDFuelGauge:
    """
    Fuel gauge for VFD display - Horizontal layout.
    
    Uses the first quarter of the display (0-63, 64x48 pixels).
    Layout:
    - Top: 3 vertical bars side-by-side (Petrol, Battery, LPG)
    - Bottom: 3 indicators horizontally (PTR, BTT, LPG)
    """
    
    # Display region - first 1/4 of VFD (leftmost)
    REGION_X = 0
    REGION_WIDTH = 64
    REGION_HEIGHT = 48
    
    def __init__(self, framebuffer: VFDFramebuffer):
        """Initialize fuel gauge."""
        self.fb = framebuffer
        
        # Current levels
        self._petrol_level: int = 0      # Liters (0-45)
        self._lpg_level: int = 0         # Liters (0-60)
        self._battery_soc: float = 0.6   # State of charge (0.0-1.0)
        self._active_fuel: str = "OFF"   # "OFF", "PETROL", "LPG"
        
        # Capacities
        self._petrol_max = 45  # liters
        self._lpg_max = 60     # liters
    
    def update(self, petrol_level: int, lpg_level: int, battery_soc: float, active_fuel: str) -> None:
        """
        Update fuel gauge data.
        
        Args:
            petrol_level: Petrol level in liters (0-45)
            lpg_level: LPG level in liters (0-60)
            battery_soc: Battery state of charge (0.0-1.0)
            active_fuel: Active fuel type ("OFF", "PETROL", "LPG") or FuelType enum
        """
        self._petrol_level = petrol_level
        self._lpg_level = lpg_level
        self._battery_soc = battery_soc
        # Handle both string and enum
        if isinstance(active_fuel, str):
            self._active_fuel = active_fuel.upper()
        else:
            # It's an enum, get the name
            self._active_fuel = active_fuel.name if hasattr(active_fuel, 'name') else str(active_fuel).upper()
    
    def render(self) -> None:
        """Render fuel gauge to the framebuffer."""
        # Clear our region
        for y in range(self.REGION_HEIGHT):
            for x in range(self.REGION_X, self.REGION_X + self.REGION_WIDTH):
                self.fb.set_pixel(x, y, False)
        
        # Layout: 64px wide, 48px tall
        # Bottom 9px for indicators: PTR BTT LPG
        # Top 39px for bars (reaching y=0 at top)
        
        indicator_height = 9  # 5px text + 4px padding
        indicator_y = self.REGION_HEIGHT - indicator_height  # y=39
        bar_height = indicator_y  # 39px for bars, bars reach y=0
        
        # 3 bars, each 19px wide with 2px gaps: 19 + 2 + 19 + 2 + 19 = 61px (3px margin)
        bar_width = 19
        bar_gap = 2
        bar_y = 0  # Start at y=0 (top)
        
        # Bar positions
        petrol_bar_x = self.REGION_X + 2
        battery_bar_x = petrol_bar_x + bar_width + bar_gap
        lpg_bar_x = battery_bar_x + bar_width + bar_gap
        
        # Render 3 vertical bars
        self._render_fuel_bar(petrol_bar_x, bar_y, bar_width, bar_height, 
                             self._petrol_level, self._petrol_max)
        
        # Battery bar - use Prius-style 6-bar visualization (0.4-0.8 range mapped to 6 bars)
        self._render_battery_bar(battery_bar_x, bar_y, bar_width, bar_height, self._battery_soc)
        
        self._render_fuel_bar(lpg_bar_x, bar_y, bar_width, bar_height,
                             self._lpg_level, self._lpg_max)
        
        # Render 3 horizontal indicators at bottom
        self._render_indicators(indicator_y)
    
    def _render_fuel_bar(self, x: int, y: int, w: int, h: int, level: int, max_level: int) -> None:
        """
        Render a vertical fuel level bar with quantized segments.
        
        For h=39 (bar height):
        - Frame: 1px
        - Inner: 37px
        - 6 segments: margin(2) + [seg(5) + gap(1)]*5 + seg(5) + margin(2) = 2 + 30 + 5 + 2 = 39 (perfect)
        """
        # Draw outer frame
        self.fb.draw_rect(x, y, w, h, True)
        
        # Calculate segments (6 segments for fuel)
        num_segments = 6
        if max_level > 0:
            fill_ratio = min(1.0, max(0.0, level / max_level))
        else:
            fill_ratio = 0.0
        
        filled_segments = int(fill_ratio * num_segments + 0.5)  # Round to nearest
        
        # Inner dimensions
        inner_x = x + 2  # 1px frame + 1px margin
        inner_w = w - 4
        
        # Segment layout: h=39, inner=37
        # Pattern: margin(2) + [seg(5) + gap(1)]*5 + seg(5) + margin(2) = 2 + 30 + 5 + 2 = 39
        segment_h = 5
        segment_gap = 1
        top_margin = 2
        
        # Draw segments from bottom to top
        for i in range(num_segments):
            seg_y = y + h - top_margin - (i + 1) * segment_h - i * segment_gap
            
            if i < filled_segments:
                for py in range(seg_y, seg_y + segment_h):
                    for px in range(inner_x, inner_x + inner_w):
                        self.fb.set_pixel(px, py, True)
    
    def _render_battery_bar(self, x: int, y: int, w: int, h: int, soc: float) -> None:
        """
        Render battery bar with Prius-style SOC display.
        
        Prius shows 6 bars representing the usable range (typically 0.4-0.8 SOC).
        Map 0.4-0.8 range to 6 bars (each bar = ~6.7% of total capacity).
        """
        # Draw outer frame
        self.fb.draw_rect(x, y, w, h, True)
        
        # Prius-style: map 0.4-0.8 SOC to 6 bars
        # Below 0.4 = 0 bars, above 0.8 = 6 bars
        usable_min = 0.4
        usable_max = 0.8
        
        if soc <= usable_min:
            usable_ratio = 0.0
        elif soc >= usable_max:
            usable_ratio = 1.0
        else:
            usable_ratio = (soc - usable_min) / (usable_max - usable_min)
        
        num_segments = 6
        filled_segments = int(usable_ratio * num_segments + 0.5)
        
        # Same layout as fuel bars
        inner_x = x + 2
        inner_w = w - 4
        segment_h = 5
        segment_gap = 1
        top_margin = 2
        
        # Draw segments from bottom to top
        for i in range(num_segments):
            seg_y = y + h - top_margin - (i + 1) * segment_h - i * segment_gap
            
            if i < filled_segments:
                for py in range(seg_y, seg_y + segment_h):
                    for px in range(inner_x, inner_x + inner_w):
                        self.fb.set_pixel(px, py, True)
    
    def _render_indicators(self, y: int) -> None:
        """Render three indicators horizontally at bottom: PTR BTT LPG."""
        # 3 indicators, each ~19px wide (matching bar width)
        # Text is 3 chars = 11px, centered in 19px
        
        bar_width = 19
        bar_gap = 2
        
        indicators = [
            ("PTR", self.REGION_X + 2, self._active_fuel == "PETROL"),
            ("BTT", self.REGION_X + 2 + bar_width + bar_gap, self._active_fuel == "BATTERY"),  # Battery never active
            ("LPG", self.REGION_X + 2 + 2 * (bar_width + bar_gap), self._active_fuel == "LPG"),
        ]
        
        for text, base_x, is_active in indicators:
            # Center text in bar width
            text_width = 11  # 3 chars * 4 - 1
            text_x = base_x + (bar_width - text_width) // 2
            text_y = y + 2  # 2px from top of indicator area
            
            # Always draw text
            self.fb.draw_text_3x5(text_x, text_y, text, True)
            
            if is_active:
                # Draw arrow indicators >TEXT< around active indicator
                text_center_y = text_y + 2  # Center of 5px tall text
                
                # Left arrow > (pointing right towards text) - closer to text
                arrow_left_x = text_x - 2  # Just 2px from text
                self.fb.set_pixel(arrow_left_x, text_center_y, True)  # Point
                self.fb.set_pixel(arrow_left_x - 1, text_center_y - 1, True)  # Top diagonal
                self.fb.set_pixel(arrow_left_x - 1, text_center_y + 1, True)  # Bottom diagonal
                
                # Right arrow < (pointing left towards text) - closer to text
                arrow_right_x = text_x + text_width + 1  # Just 1px from text
                self.fb.set_pixel(arrow_right_x, text_center_y, True)  # Point
                self.fb.set_pixel(arrow_right_x + 1, text_center_y - 1, True)  # Top diagonal
                self.fb.set_pixel(arrow_right_x + 1, text_center_y + 1, True)  # Bottom diagonal


# ==============================================================================
# VFD Energy Monitor - Tesla-style visualization
# ==============================================================================
# ==============================================================================

class VFDEnergyMonitor:
    """
    Energy monitor for VFD display - Tesla-inspired style.
    
    Uses the third quarter of the display (128-191, 64x48 pixels).
    Extended to touch the left power bar (MG bar) on the right side.
    Shows MG Assist/Regen graph with time-based integration.
    
    The graph covers 1 minute of data with time-independent sampling.
    Values are integrated (accumulated) within each pixel's timeframe.
    """
    
    # Display region - power chart uses 1/4 of VFD (64 pixels)
    # Chart: x=128-191, Power bars: x=192-255
    REGION_X = 128
    REGION_WIDTH = 64  # 1/4 of VFD width (128 to 191)
    REGION_HEIGHT = 48
    
    # Graph settings
    GRAPH_DURATION_SEC = 60.0  # Total time span covered by graph (default: 1 minute)
    
    def __init__(self, framebuffer: VFDFramebuffer, time_base_sec: float = 60.0):
        """
        Initialize the energy monitor.
        
        Args:
            framebuffer: VFD framebuffer to draw on
            time_base_sec: Time base in seconds (default: 60s)
        """
        self.fb = framebuffer
        
        # Time base configuration (easy to change)
        self._time_base_sec = time_base_sec
        
        # Calculate graph dimensions - use full height with aligned zero baseline
        self._graph_x = self.REGION_X
        self._graph_y = 0  # Start from top
        self._graph_w = self.REGION_WIDTH  # Full width (64 pixels)
        self._graph_h = self.REGION_HEIGHT  # Full height (48 pixels)
        
        # Time per pixel column (calculated from time base)
        self._time_per_pixel = self._time_base_sec / self._graph_w
        
        # History buffer: one entry per pixel column
        # Each entry stores (assist_sum, regen_sum, sample_count, ice_was_running)
        # assist_sum: accumulated positive power (assist/discharge)
        # regen_sum: accumulated negative power (regen/charge) - stored as positive
        # This way assist and regen don't cancel each other out
        self._history: List[Tuple[float, float, int, bool]] = [(0.0, 0.0, 0, False)] * self._graph_w
        
        # Current pixel column being filled (start from rightmost position)
        self._current_column = self._graph_w - 1
        
        # Time tracking
        self._last_tick_time: Optional[float] = None
        self._column_start_time: Optional[float] = None
        
        # Current instantaneous value
        self._mg_power: float = 0.0
        
        # Total energy tracking (kWh)
        self._total_regen_kwh: float = 0.0  # Accumulated regenerated energy
        self._total_assist_kwh: float = 0.0  # Accumulated assist energy
        self._last_energy_update: Optional[float] = None  # For time-based integration
        
        # ICE state machine - persistent state with memory
        # Once we know ICE state, we assume it stays that way until explicitly changed
        self._ice_running_state: bool = False
        
        # Track if ICE was running at any point in current column
        self._current_column_ice_active: bool = False
        
    def update(
        self,
        mg_power_kw: float,
        ice_rpm: int = 0,
        ice_load_percent: float = 0.0,
        ice_running: bool = False
    ) -> None:
        """
        Update energy data.
        
        Args:
            mg_power_kw: Motor/Generator power in kW (negative = regen)
            ice_rpm: Engine RPM (unused, kept for API compatibility)
            ice_load_percent: ICE load as percentage (unused, kept for API compatibility)
            ice_running: True if ICE is currently running
        """
        current_time = time.time()
        
        # Initialize timing on first update
        if self._last_tick_time is None:
            self._last_tick_time = current_time
            self._column_start_time = current_time
            self._last_energy_update = current_time
        
        # Normalize MG power to -1.0 to +1.0 range
        # Prius Gen2 typical: ±20kW, peak ~25kW
        # Using 25kW max for more sensitive visualization
        max_power = 25.0
        self._mg_power = max(-1.0, min(1.0, mg_power_kw / max_power))
        
        # Accumulate total energy (kWh) using time-based integration
        if self._last_energy_update is not None:
            dt_hours = (current_time - self._last_energy_update) / 3600.0
            if mg_power_kw > 0:
                # Assist - battery discharging
                self._total_assist_kwh += mg_power_kw * dt_hours
            else:
                # Regen - battery charging (store as positive)
                self._total_regen_kwh += abs(mg_power_kw) * dt_hours
        self._last_energy_update = current_time
        
        # Update ICE state machine - remember last known state
        # State changes explicitly via ice_running parameter
        self._ice_running_state = ice_running
        
        # Track if ICE is/was running in current column
        if self._ice_running_state:
            self._current_column_ice_active = True
        
        # Accumulate assist and regen separately (integration)
        # This way they don't cancel each other out
        assist_sum, regen_sum, count, ice_was_running = self._history[self._current_column]
        
        if self._mg_power > 0:
            # Assist (positive power - battery discharging)
            assist_sum += self._mg_power
        else:
            # Regen (negative power - battery charging) - store as positive
            regen_sum += abs(self._mg_power)
        
        self._history[self._current_column] = (
            assist_sum,
            regen_sum,
            count + 1,
            ice_was_running or self._current_column_ice_active
        )
        
        # Check if it's time to advance to next column
        elapsed_in_column = current_time - self._column_start_time
        if elapsed_in_column >= self._time_per_pixel:
            self._advance_column(current_time)
        
        self._last_tick_time = current_time
    
    def set_time_base(self, seconds: float) -> None:
        """
        Change the time base for the power chart.
        
        Args:
            seconds: New time base in seconds (e.g., 15, 30, 60, 120, 300)
        """
        self._time_base_sec = seconds
        self._time_per_pixel = self._time_base_sec / self._graph_w
        # Note: History is preserved, but time scale changes
    
    def _get_display_exponent(self) -> float:
        """
        Calculate the display exponent based on current time base.
        
        Uses adaptive power curve scaling:
        - Short time bases (15s): Linear (exponent = 1.0)
        - Long time bases (1h): Aggressive compression (exponent = 0.3)
        
        This ensures brief events remain visible on longer time scales
        while keeping short time scales accurate and linear.
        """
        import math
        
        # Range: 15s to 3600s (1h)
        min_time = 15.0
        max_time = 3600.0
        
        # Clamp time base to valid range
        time_base = max(min_time, min(max_time, self._time_base_sec))
        
        # Linear interpolation in log space
        # log(15) ≈ 2.71, log(3600) ≈ 8.19
        log_ratio = (math.log(time_base) - math.log(min_time)) / (math.log(max_time) - math.log(min_time))
        
        # Exponent range: 1.0 (linear) to 0.3 (aggressive)
        exponent = 1.0 - 0.7 * log_ratio
        
        return exponent
    
    def _advance_column(self, current_time: float) -> None:
        """Advance to the next column, shifting history if at end."""
        # Prepare for new column: inherit current ICE state (state machine with memory)
        # The new column starts with whatever the current state is
        self._current_column_ice_active = self._ice_running_state
        
        self._current_column += 1
        
        if self._current_column >= self._graph_w:
            # Shift all history left by one
            self._history.pop(0)
            # New column inherits current ICE state, starts with zero assist/regen
            self._history.append((0.0, 0.0, 0, self._ice_running_state))
            self._current_column = self._graph_w - 1
        
        self._column_start_time = current_time
    
    def tick(self) -> None:
        """
        Time tick - call this regularly to advance graph independently of data.
        
        This ensures the graph advances even when no CAN messages arrive.
        """
        current_time = time.time()
        
        if self._column_start_time is None:
            self._column_start_time = current_time
            self._last_tick_time = current_time
            return
        
        # Check if we need to advance columns based on elapsed time
        elapsed = current_time - self._column_start_time
        columns_to_advance = int(elapsed / self._time_per_pixel)
        
        for _ in range(columns_to_advance):
            self._advance_column(current_time)
    
    def render(self) -> None:
        """Render the energy monitor to the framebuffer."""
        # Call tick to ensure time-based advancement
        self.tick()
        
        # Clear our region only
        for y in range(self.REGION_HEIGHT):
            for x in range(self.REGION_X, self.REGION_X + self.REGION_WIDTH):
                self.fb.set_pixel(x, y, False)
        
        # Render MG graph (full height)
        self._render_mg_graph()
    
    def _render_mg_graph(self) -> None:
        """Render MG (Motor/Generator) power graph using full height.
        
        Shows both assist (upward) and regen (downward) separately.
        They don't cancel each other out - both are shown independently.
        
        Uses adaptive power curve scaling based on time base:
        - Short time bases (15s): Linear display
        - Long time bases (1h): Compressed to keep brief events visible
        """
        graph_x = self._graph_x
        graph_y = self._graph_y
        graph_w = self._graph_w
        graph_h = self._graph_h
        
        # Get adaptive exponent for current time base
        exponent = self._get_display_exponent()
        
        # Center line (zero line) at middle
        center_y = graph_y + graph_h // 2
        
        # Draw center line (dotted, matching power bars style)
        for x in range(graph_x, graph_x + graph_w, 3):
            self.fb.set_pixel(x, center_y, True)
        
        # Draw history graph
        # Only render up to current column (rest is future/empty)
        for i in range(self._current_column + 1):
            x = graph_x + i
            assist_sum, regen_sum, count, ice_was_running = self._history[i]
            
            # Draw ICE running indicator at bottom row
            if ice_was_running:
                self.fb.set_pixel(x, self.REGION_HEIGHT - 1, True)
            
            # Calculate average assist and regen values for this column
            if count > 0:
                assist_val = assist_sum / count  # Average assist per sample
                regen_val = regen_sum / count    # Average regen per sample
            else:
                assist_val = 0.0
                regen_val = 0.0
            
            # Apply adaptive power curve scaling
            # val ** exponent: smaller exponent = more compression for small values
            if assist_val > 0.001:
                assist_val = assist_val ** exponent
            if regen_val > 0.001:
                regen_val = regen_val ** exponent
            
            # Map values to pixel offset from center
            # Each value: 0.0 to 1.0 -> pixel offset
            max_offset = graph_h // 2 - 1
            
            # Draw assist bar UPWARD from center (positive values)
            if assist_val > 0.01:
                assist_pixels = int(assist_val * max_offset)
                assist_pixels = min(assist_pixels, max_offset)
                for dy in range(assist_pixels):
                    self.fb.set_pixel(x, center_y - dy - 1, True)
            
            # Draw regen bar DOWNWARD from center (stored as positive)
            if regen_val > 0.01:
                regen_pixels = int(regen_val * max_offset)
                regen_pixels = min(regen_pixels, max_offset)
                for dy in range(regen_pixels):
                    self.fb.set_pixel(x, center_y + dy + 1, True)
        
        # Current value indicator (at top right, XORed with chart)
        # Always show current value
        current_val = self._mg_power
        val_str = f"{current_val * 25:+.0f}"
        text_x = self.REGION_X + self.REGION_WIDTH - len(val_str) * 4 - 2
        self.fb.draw_text_3x5_xor(text_x, 1, val_str)


# ==============================================================================
# VFD Power Bars - Instant power visualization
# ==============================================================================

class VFDPowerBars:
    """
    Instant power bars for VFD display.
    
    Uses the rightmost 1/4 of the display (192-255, 64x48 pixels).
    Shows two vertical bars side by side:
    - Left bar: MG power (up = assist, down = regen)
    - Right bar: Fuel consumption (up) / Braking (down)
    
    Values are smoothed using exponential moving average (EMA) for
    visually pleasing transitions.
    """
    
    # Display region - power bars use 1/4 of VFD (64 pixels)
    # Matches power chart width for balanced layout
    REGION_X = 192
    REGION_WIDTH = 64
    REGION_HEIGHT = 48
    
    # EMA smoothing factor (0.0-1.0)
    # Lower = smoother but slower response
    # Higher = faster response but more jittery
    # 0.15 gives nice smooth transitions (~6-7 samples to reach 63% of new value)
    EMA_ALPHA = 0.15
    
    def __init__(self, framebuffer: VFDFramebuffer):
        """Initialize power bars."""
        self.fb = framebuffer
        
        # Current display values (smoothed, normalized -1.0 to +1.0)
        self._mg_power: float = 0.0      # Positive = assist, negative = regen
        self._fuel_brake: float = 0.0    # Positive = fuel consumption, negative = braking
        
        # Target values (raw input, before smoothing)
        self._mg_power_target: float = 0.0
        self._fuel_brake_target: float = 0.0
        
        # Bar layout - maximize space utilization
        self._bar_width = 30  # Wider bars for better visibility
        self._bar_height = self.REGION_HEIGHT  # Full height, no margin
        self._bar_y = 0  # No top margin
        
        # Position bars with minimal spacing, fill the region
        bar_spacing = 2  # Minimal gap between bars
        total_bars_width = self._bar_width * 2 + bar_spacing
        # Center within region to use maximum space
        start_x = self.REGION_X + (self.REGION_WIDTH - total_bars_width) // 2
        
        self._mg_bar_x = start_x
        self._fuel_bar_x = start_x + self._bar_width + bar_spacing
    
    def _ema(self, current: float, target: float, alpha: float = None) -> float:
        """
        Apply exponential moving average smoothing.
        
        EMA formula: new_value = alpha * target + (1 - alpha) * current
        
        Args:
            current: Current smoothed value
            target: Target (raw input) value
            alpha: Smoothing factor (uses class default if None)
        
        Returns:
            New smoothed value
        """
        if alpha is None:
            alpha = self.EMA_ALPHA
        return alpha * target + (1.0 - alpha) * current
        
    def update(
        self,
        mg_power_kw: float,
        fuel_flow_rate: float,
        brake_pressure: int,
        ice_running: bool = False
    ) -> None:
        """
        Update power bar target data.
        
        This only sets the target values. The actual smoothing is applied
        in tick() which is called on each render for consistent animation.
        
        Args:
            mg_power_kw: Motor/Generator power in kW (positive = assist, negative = regen)
            fuel_flow_rate: Fuel flow rate in L/h (0 to ~8 for Prius), -1 means N/A
            brake_pressure: Brake pedal pressure (0-127)
            ice_running: Whether the ICE is currently running
        """
        # Normalize MG power to -1.0 to +1.0 range
        max_power = 30.0
        self._mg_power_target = max(-1.0, min(1.0, mg_power_kw / max_power))
        
        # Normalize fuel/brake to -1.0 to +1.0 range
        # Fuel: 0-8 L/h maps to 0-1.0 (positive, goes up)
        # Brake: 0-127 maps to 0-1.0 (but negative, goes down)
        max_fuel_flow = 8.0  # L/h at full load
        max_brake = 127.0
        
        if brake_pressure > 5:  # Some threshold to avoid noise
            # Braking takes priority - show negative
            self._fuel_brake_target = -min(1.0, brake_pressure / max_brake)
        elif ice_running and fuel_flow_rate > 0.1:  # ICE must be running for positive fuel
            # Fuel consumption - show positive only when ICE is running
            self._fuel_brake_target = min(1.0, fuel_flow_rate / max_fuel_flow)
        else:
            # ICE off or N/A fuel reading - cannot show positive value
            self._fuel_brake_target = 0.0
    
    def tick(self) -> None:
        """
        Time tick - apply EMA smoothing to display values.
        
        Call this on each render frame for consistent, smooth animation
        regardless of how often data updates arrive.
        """
        self._mg_power = self._ema(self._mg_power, self._mg_power_target)
        self._fuel_brake = self._ema(self._fuel_brake, self._fuel_brake_target)
    
    def render(self) -> None:
        """Render power bars to the framebuffer."""
        # Apply smoothing on each render for consistent animation
        self.tick()
        
        # Clear our region
        for y in range(self.REGION_HEIGHT):
            for x in range(self.REGION_X, self.REGION_X + self.REGION_WIDTH):
                self.fb.set_pixel(x, y, False)
        
        # Draw both bars with their icons
        self._render_bar(self._mg_bar_x, self._mg_power, ICON_LIGHTNING)
        self._render_bar(self._fuel_bar_x, self._fuel_brake, ICON_ENGINE)
    
    def _render_bar(self, bar_x: int, value: float, icon: List[List[int]]) -> None:
        """
        Render a single vertical bar with icon.
        
        Bar starts at center (0), goes up for positive values, down for negative.
        Icon is drawn at center and XORed with bar fill.
        """
        bar_y = self._bar_y
        bar_w = self._bar_width
        bar_h = self._bar_height
        
        # Center line position
        center_y = bar_y + bar_h // 2
        
        # Draw center line (zero reference) - dotted
        for x in range(bar_x, bar_x + bar_w, 3):
            self.fb.set_pixel(x, center_y, True)
        
        # Calculate fill area
        max_fill = bar_h // 2 - 1
        fill_height = int(abs(value) * max_fill)
        
        # Track which pixels are filled by the bar
        bar_pixels: set = set()
        
        if fill_height > 0:
            if value > 0:
                # Positive - fill upward from center
                fill_y_start = center_y - fill_height
                fill_y_end = center_y
                for y in range(fill_y_start, fill_y_end):
                    for x in range(bar_x + 1, bar_x + bar_w - 1):
                        self.fb.set_pixel(x, y, True)
                        bar_pixels.add((x, y))
            else:
                # Negative - fill downward from center
                fill_y_start = center_y + 1
                fill_y_end = center_y + 1 + fill_height
                for y in range(fill_y_start, fill_y_end):
                    for x in range(bar_x + 1, bar_x + bar_w - 1):
                        self.fb.set_pixel(x, y, True)
                        bar_pixels.add((x, y))
        
        # Draw icon at top 1/4 of bar (XOR with bar pixels)
        icon_h = len(icon)
        icon_w = len(icon[0]) if icon else 0
        icon_x = bar_x + (bar_w - icon_w) // 2
        # Position icon center at 1/4 from top (y = 48/4 = 12)
        icon_center_y = self.REGION_HEIGHT // 4
        icon_y = icon_center_y - icon_h // 2
        
        for row_idx, row in enumerate(icon):
            for col_idx, pixel in enumerate(row):
                if pixel:
                    px = icon_x + col_idx
                    py = icon_y + row_idx
                    # XOR: if bar is filled at this pixel, invert; otherwise draw
                    if (px, py) in bar_pixels:
                        self.fb.set_pixel(px, py, False)  # Invert (bar is ON, so turn OFF)
                    else:
                        self.fb.set_pixel(px, py, True)   # Draw icon pixel


# ==============================================================================
# VFD Display Widget - Pygame wrapper for simulation
# ==============================================================================

class VFDDisplayWidget(Widget):
    """
    VFD Display simulator widget for pygame.
    
    Renders the VFDFramebuffer to the screen with proper scaling
    and a visible frame around the display.
    
    Display specs:
    - 256x48 pixels
    - Binary (on/off only)
    - Cyan VFD color for ON pixels
    - Dark background for OFF pixels
    """
    
    # VFD colors (simulating real VFD phosphor)
    COLOR_ON = (0, 255, 200)      # Bright cyan-green (VFD phosphor)
    COLOR_OFF = (0, 20, 15)       # Very dim (off pixel, slight glow)
    COLOR_FRAME = (80, 80, 90)    # Metal frame color
    COLOR_FRAME_INNER = (40, 40, 45)  # Inner frame shadow
    
    FRAME_WIDTH = 3  # Frame thickness in pixels (before scaling)
    
    def __init__(
        self,
        rect: Rect,
        scale: int = 1
    ):
        """
        Initialize VFD display widget.
        
        Args:
            rect: Widget position and size on screen
            scale: Pixel scaling factor (1 = native 256x48)
        """
        super().__init__(rect, focusable=False)
        
        self.scale = scale
        
        # Create framebuffer
        self.framebuffer = VFDFramebuffer(VFD_WIDTH, VFD_HEIGHT)
        
        # Create fuel gauge (second quarter, x=64-127)
        self.fuel_gauge = VFDFuelGauge(self.framebuffer)
        
        # Create energy monitor (third quarter, x=128-191)
        self.energy_monitor = VFDEnergyMonitor(self.framebuffer)
        
        # Create power bars (rightmost 1/4, x=192-255)
        self.power_bars = VFDPowerBars(self.framebuffer)
        
        # Create pygame surface for rendering
        # Account for frame
        total_width = VFD_WIDTH + self.FRAME_WIDTH * 2
        total_height = VFD_HEIGHT + self.FRAME_WIDTH * 2
        self._surface = pygame.Surface((total_width * scale, total_height * scale))
        
    def update_energy(
        self,
        mg_power_kw: float,
        ice_rpm: int,
        ice_load_percent: float,
        fuel_flow_rate: float = 0.0,
        brake_pressure: int = 0,
        ice_running: bool = False
    ) -> None:
        """Update energy monitor and power bars data."""
        # Update history graph (pass ice_running for ICE indicator line)
        self.energy_monitor.update(mg_power_kw, ice_rpm, ice_load_percent, ice_running)
        
        # Update power bars (pass ice_running for fuel display logic)
        self.power_bars.update(mg_power_kw, fuel_flow_rate, brake_pressure, ice_running)
    
    def update_fuel(
        self,
        petrol_level: int,
        lpg_level: int,
        battery_soc: float,
        active_fuel: str
    ) -> None:
        """Update fuel gauge data."""
        self.fuel_gauge.update(petrol_level, lpg_level, battery_soc, active_fuel)
    
    def render(self, surface: pygame.Surface) -> None:
        """Render the VFD display to the pygame surface."""
        # Clear surface with frame color
        self._surface.fill(self.COLOR_FRAME)
        
        # Draw inner frame shadow
        inner_rect = pygame.Rect(
            1 * self.scale,
            1 * self.scale,
            (VFD_WIDTH + self.FRAME_WIDTH * 2 - 2) * self.scale,
            (VFD_HEIGHT + self.FRAME_WIDTH * 2 - 2) * self.scale
        )
        pygame.draw.rect(self._surface, self.COLOR_FRAME_INNER, inner_rect)
        
        # Render fuel gauge to framebuffer
        self.fuel_gauge.render()
        
        # Render power bars to framebuffer
        self.power_bars.render()
        
        # Render energy monitor to framebuffer
        self.energy_monitor.render()
        
        # Render framebuffer pixels
        fb_offset_x = self.FRAME_WIDTH * self.scale
        fb_offset_y = self.FRAME_WIDTH * self.scale
        
        for y in range(VFD_HEIGHT):
            for x in range(VFD_WIDTH):
                color = self.COLOR_ON if self.framebuffer.get_pixel(x, y) else self.COLOR_OFF
                
                # Draw scaled pixel
                px = fb_offset_x + x * self.scale
                py = fb_offset_y + y * self.scale
                
                if self.scale == 1:
                    self._surface.set_at((px, py), color)
                else:
                    pygame.draw.rect(
                        self._surface,
                        color,
                        pygame.Rect(px, py, self.scale, self.scale)
                    )
        
        # Blit to target surface at widget position
        surface.blit(self._surface, (self.rect.x, self.rect.y))
