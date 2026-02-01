"""
Power Flow Component - Tesla-inspired energy flow visualization.

Shows energy flow between ICE, Battery, and Wheels with animated arrows.
"""

import time
from typing import List
from ..framebuffer import VFDFramebuffer
from ..icons import ICON_ENGINE, ICON_BATTERY, ICON_WHEEL


class PowerFlowComponent:
    """
    Power flow diagram for VFD display.
    
    Uses the second quarter of the display (x: 64-127, 64x48 pixels).
    Shows simplified energy flow:
    - ICE → Battery (charging)
    - Battery → MG (assist)
    - Brakes → Battery (regen)
    """
    
    # Display region
    REGION_X = 64
    REGION_WIDTH = 64
    REGION_HEIGHT = 48
    
    def __init__(self, framebuffer: VFDFramebuffer):
        """Initialize power flow diagram."""
        self.fb = framebuffer
        
        # Current power flows (0.0-1.0 normalized)
        self._ice_to_battery: float = 0.0
        self._battery_to_wheels: float = 0.0
        self._wheels_to_battery: float = 0.0
        self._ice_to_wheels: float = 0.0
        
        # Animation state (0-239 for ~4 sec at 60fps)
        self._anim_phase: int = 0
    
    def update(
        self,
        mg_power: float,
        speed: float,
        ice_running: bool
    ) -> None:
        """
        Update power flow data from normalized values.
        
        Args:
            mg_power: Normalized MG power (-1.0 to +1.0)
            speed: Normalized speed (0.0 to 1.0)
            ice_running: Whether ICE is running
        """
        # Reset all flows
        self._ice_to_battery = 0.0
        self._battery_to_wheels = 0.0
        self._wheels_to_battery = 0.0
        self._ice_to_wheels = 0.0
        
        # Power threshold: 1.0kW = 0.033 normalized (1/30)
        threshold = 0.033
        
        # BATT → WHEELS: Battery discharging (positive MG power)
        if mg_power > threshold:
            self._battery_to_wheels = min(1.0, mg_power)
        
        # WHEELS → BATT: Regenerative braking (negative power while moving)
        if mg_power < -threshold and speed > 0.01:
            self._wheels_to_battery = min(1.0, abs(mg_power))
        
        # ICE → BATT: ICE charging battery (negative power while stationary or slow)
        if ice_running and mg_power < -threshold and self._wheels_to_battery < threshold:
            self._ice_to_battery = min(1.0, abs(mg_power))
        
        # ICE → WHEELS: ICE running and moving
        if ice_running and speed > 0.01:
            self._ice_to_wheels = min(1.0, speed)
    
    def tick(self) -> None:
        """Advance animation frame."""
        self._anim_phase = (self._anim_phase + 1) % 240
    
    def render(self) -> None:
        """Render power flow diagram to framebuffer."""
        # Clear region
        for y in range(self.REGION_HEIGHT):
            for x in range(self.REGION_X, self.REGION_X + self.REGION_WIDTH):
                self.fb.set_pixel(x, y, False)
        
        # Advance animation
        self.tick()
        
        # Triangle layout:
        #       ICE (top center)
        #      /   \
        #   BAT     MG (bottom corners)
        
        cx = self.REGION_X + self.REGION_WIDTH // 2
        
        # Node positions
        ice_x, ice_y = cx, 10
        battery_x, battery_y = self.REGION_X + 16, 38
        mg_x, mg_y = self.REGION_X + self.REGION_WIDTH - 16, 38
        
        # Draw nodes with icons
        self.fb.draw_icon_centered(ice_x, ice_y, ICON_ENGINE)
        self.fb.draw_icon_centered(battery_x, battery_y, ICON_BATTERY)
        self.fb.draw_icon_centered(mg_x, mg_y, ICON_WHEEL)
        
        # Draw flow arrows
        if self._ice_to_battery > 0.05:
            self._draw_flow_arrow(ice_x - 3, ice_y + 5, battery_x + 3, battery_y - 3)
        
        if self._battery_to_wheels > 0.05:
            self._draw_flow_arrow(battery_x + 5, battery_y, mg_x - 5, mg_y)
        
        if self._wheels_to_battery > 0.05:
            self._draw_flow_arrow(mg_x - 5, mg_y, battery_x + 5, battery_y)
        
        if self._ice_to_wheels > 0.05:
            self._draw_flow_arrow(ice_x + 3, ice_y + 5, mg_x - 3, mg_y - 3)
    
    def _draw_flow_arrow(self, x0: int, y0: int, x1: int, y1: int) -> None:
        """Draw animated triangles showing power flow."""
        dx = x1 - x0
        dy = y1 - y0
        length = int((dx**2 + dy**2)**0.5)
        
        if length == 0:
            return
        
        is_horizontal = abs(dx) > abs(dy)
        is_right = dx > 0
        is_down = dy > 0
        
        triangle_spacing = 14
        num_triangles = max(3, int(length / triangle_spacing))
        
        for i in range(num_triangles):
            offset = (i * triangle_spacing + self._anim_phase / 1.5) % length
            t = offset / length
            
            tri_x = int(x0 + dx * t)
            tri_y = int(y0 + dy * t)
            
            self._draw_chevron(tri_x, tri_y, is_horizontal, is_right, is_down)
    
    def _draw_chevron(self, x: int, y: int, is_horizontal: bool, is_right: bool, is_down: bool) -> None:
        """Draw a chevron pointing in the flow direction."""
        if is_horizontal:
            if is_right:
                self.fb.set_pixel(x, y, True)
                self.fb.set_pixel(x - 1, y - 1, True)
                self.fb.set_pixel(x - 1, y + 1, True)
            else:
                self.fb.set_pixel(x, y, True)
                self.fb.set_pixel(x + 1, y - 1, True)
                self.fb.set_pixel(x + 1, y + 1, True)
        else:
            if is_down:
                self.fb.set_pixel(x, y, True)
                self.fb.set_pixel(x - 1, y - 1, True)
                self.fb.set_pixel(x + 1, y - 1, True)
            else:
                self.fb.set_pixel(x, y, True)
                self.fb.set_pixel(x - 1, y + 1, True)
                self.fb.set_pixel(x + 1, y + 1, True)
