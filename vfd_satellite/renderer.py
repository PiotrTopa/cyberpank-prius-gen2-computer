"""
VFD Renderer - Combines all components and handles display output.

Manages the framebuffer, components, and rendering to display.
"""

import pygame
from typing import Optional

from .framebuffer import VFDFramebuffer, VFD_WIDTH, VFD_HEIGHT
from .state import VFDState, FuelType
from .components import (
    PowerFlowComponent,
    FuelGaugeComponent,
    EnergyGraphComponent,
    PowerBarsComponent,
)


class VFDRenderer:
    """
    VFD display renderer.
    
    Combines all display components and renders to pygame surface.
    """
    
    # VFD colors (simulating real VFD phosphor)
    COLOR_ON = (0, 255, 200)       # Bright cyan-green
    COLOR_OFF = (0, 20, 15)        # Very dim
    COLOR_FRAME = (80, 80, 90)     # Metal frame
    COLOR_FRAME_INNER = (40, 40, 45)
    
    FRAME_WIDTH = 3
    
    def __init__(self, scale: int = 1):
        """
        Initialize VFD renderer.
        
        Args:
            scale: Pixel scaling factor
        """
        self.scale = scale
        
        # Create framebuffer
        self.framebuffer = VFDFramebuffer(VFD_WIDTH, VFD_HEIGHT)
        
        # Create components
        self.fuel_gauge = FuelGaugeComponent(self.framebuffer)
        self.power_flow = PowerFlowComponent(self.framebuffer)
        self.energy_graph = EnergyGraphComponent(self.framebuffer)
        self.power_bars = PowerBarsComponent(self.framebuffer)
        
        # Create pygame surface
        total_width = VFD_WIDTH + self.FRAME_WIDTH * 2
        total_height = VFD_HEIGHT + self.FRAME_WIDTH * 2
        self._surface = pygame.Surface((total_width * scale, total_height * scale))
        
        # Track last config
        self._last_time_base: Optional[int] = None
    
    def update(self, state: VFDState) -> None:
        """
        Update all components from VFD state.
        
        Args:
            state: Current VFD state from receiver
        """
        energy = state.energy
        state_data = state.state
        config = state.config
        
        # Update time base if changed
        if config.time_base != self._last_time_base:
            self.energy_graph.set_time_base(config.time_base)
            self._last_time_base = config.time_base
        
        # Update fuel gauge
        self.fuel_gauge.update(
            petrol_level=energy.petrol_level,
            lpg_level=energy.lpg_level,
            battery_soc=energy.battery_soc,
            active_fuel=state_data.active_fuel,
        )
        
        # Update power flow
        self.power_flow.update(
            mg_power=energy.mg_power,
            speed=energy.speed,
            ice_running=energy.ice_running,
        )
        
        # Update energy graph
        self.energy_graph.update(
            mg_power=energy.mg_power,
            ice_running=energy.ice_running,
        )
        
        # Update power bars
        self.power_bars.update(
            mg_power=energy.mg_power,
            fuel_flow=energy.fuel_flow,
            brake=energy.brake,
            ice_running=energy.ice_running,
        )
    
    def render(self, target_surface: pygame.Surface, x: int = 0, y: int = 0) -> None:
        """
        Render VFD to target pygame surface.
        
        Args:
            target_surface: Surface to render to
            x, y: Position on target surface
        """
        # Clear with frame color
        self._surface.fill(self.COLOR_FRAME)
        
        # Inner frame shadow
        inner_rect = pygame.Rect(
            1 * self.scale,
            1 * self.scale,
            (VFD_WIDTH + self.FRAME_WIDTH * 2 - 2) * self.scale,
            (VFD_HEIGHT + self.FRAME_WIDTH * 2 - 2) * self.scale
        )
        pygame.draw.rect(self._surface, self.COLOR_FRAME_INNER, inner_rect)
        
        # Render components to framebuffer
        self.fuel_gauge.render()
        self.power_flow.render()
        self.energy_graph.render()
        self.power_bars.render()
        
        # Render framebuffer pixels
        fb_offset_x = self.FRAME_WIDTH * self.scale
        fb_offset_y = self.FRAME_WIDTH * self.scale
        
        for py in range(VFD_HEIGHT):
            for px in range(VFD_WIDTH):
                color = self.COLOR_ON if self.framebuffer.get_pixel(px, py) else self.COLOR_OFF
                
                sx = fb_offset_x + px * self.scale
                sy = fb_offset_y + py * self.scale
                
                if self.scale == 1:
                    self._surface.set_at((sx, sy), color)
                else:
                    pygame.draw.rect(
                        self._surface, color,
                        pygame.Rect(sx, sy, self.scale, self.scale)
                    )
        
        # Blit to target
        target_surface.blit(self._surface, (x, y))
    
    def get_size(self) -> tuple:
        """Get the size of the VFD surface."""
        return self._surface.get_size()
