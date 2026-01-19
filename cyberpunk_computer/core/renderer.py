"""
Rendering pipeline.

Handles the native surface, scaling, and post-processing effects.
"""

import pygame

from ..config import Config


class Renderer:
    """
    Manages rendering pipeline with scaling and post-processing.
    
    All game logic renders to a native 480x240 surface, which is then
    scaled up to the window size as a post-processing step.
    """
    
    def __init__(self, config: Config):
        """
        Initialize the renderer.
        
        Args:
            config: Application configuration
        """
        self.config = config
        
        # Create the native surface (always 480x240)
        self.native_surface = pygame.Surface(config.native_size)
        
        # Create the display window
        flags = 0
        if config.fullscreen:
            flags |= pygame.FULLSCREEN
        
        self.window = pygame.display.set_mode(config.window_size, flags)
        
        # Pre-create scaled surface if needed
        if config.scale_factor > 1:
            self.scaled_surface = pygame.Surface(config.window_size)
        else:
            self.scaled_surface = None
        
        # Create scanline overlay if effects enabled
        self.scanline_overlay = None
        if config.effects_enabled and config.scale_factor >= 2:
            self._create_scanline_overlay()
    
    def _create_scanline_overlay(self) -> None:
        """Create a semi-transparent scanline overlay for CRT effect."""
        self.scanline_overlay = pygame.Surface(
            self.config.window_size, 
            pygame.SRCALPHA
        )
        
        # Draw horizontal scanlines
        scanline_alpha = 20  # Subtle effect
        for y in range(0, self.config.window_height, 2):
            pygame.draw.line(
                self.scanline_overlay,
                (0, 0, 0, scanline_alpha),
                (0, y),
                (self.config.window_width, y)
            )
    
    def get_surface(self) -> pygame.Surface:
        """
        Get the native surface to draw on.
        
        Returns:
            The native 480x240 surface
        """
        return self.native_surface
    
    def present(self) -> None:
        """
        Present the frame to the display.
        
        Handles scaling and post-processing effects.
        """
        if self.config.scale_factor == 1:
            # No scaling needed, blit directly
            self.window.blit(self.native_surface, (0, 0))
        else:
            # Scale up using nearest-neighbor for crisp pixels
            pygame.transform.scale(
                self.native_surface,
                self.config.window_size,
                self.scaled_surface
            )
            self.window.blit(self.scaled_surface, (0, 0))
            
            # Apply scanline overlay if enabled
            if self.scanline_overlay:
                self.window.blit(self.scanline_overlay, (0, 0))
        
        # Update display
        pygame.display.flip()
    
    def cleanup(self) -> None:
        """Clean up renderer resources."""
        # Surfaces are automatically cleaned up by Python GC
        pass
    
    def toggle_effects(self) -> None:
        """Toggle post-processing effects on/off."""
        self.config.effects_enabled = not self.config.effects_enabled
        
        if self.config.effects_enabled and self.config.scale_factor >= 2:
            self._create_scanline_overlay()
        else:
            self.scanline_overlay = None
