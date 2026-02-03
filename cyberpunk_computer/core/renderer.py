"""
Rendering pipeline.

Handles the native surface, scaling, and post-processing effects.
Supports direct framebuffer output when SDL video drivers aren't available.
"""

import logging
import os
import pygame

from ..config import Config
from .framebuffer import FramebufferOutput, is_framebuffer_available

logger = logging.getLogger(__name__)


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
        self.framebuffer: FramebufferOutput | None = None
        self.use_direct_fb = False
        
        # Create the native surface (always 480x240)
        self.native_surface = pygame.Surface(config.native_size)
        
        # Determine display flags based on video driver
        video_driver = os.environ.get('SDL_VIDEODRIVER', '')
        
        # Try to initialize SDL display
        try:
            if video_driver == 'dummy':
                # Dummy driver - we'll use direct framebuffer output
                logger.info("Using SDL dummy driver with direct framebuffer output")
                self.window = pygame.display.set_mode(config.native_size)
                self._setup_direct_framebuffer()
            else:
                # Try normal SDL initialization
                flags = pygame.FULLSCREEN if config.fullscreen else 0
                self.window = pygame.display.set_mode(config.window_size, flags)
                logger.info(f"Using SDL video driver: {pygame.display.get_driver()}")
        except pygame.error as e:
            # SDL video failed, fall back to dummy + direct FB
            logger.warning(f"SDL video initialization failed: {e}")
            logger.info("Falling back to dummy driver with direct framebuffer")
            os.environ['SDL_VIDEODRIVER'] = 'dummy'
            pygame.display.quit()
            pygame.display.init()
            self.window = pygame.display.set_mode(config.native_size)
            self._setup_direct_framebuffer()
        
        # Pre-create scaled surface if needed (only for non-FB mode)
        if config.scale_factor > 1 and not self.use_direct_fb:
            self.scaled_surface = pygame.Surface(config.window_size)
        else:
            self.scaled_surface = None
        
        # Create scanline overlay if effects enabled
        self.scanline_overlay = None
        if config.effects_enabled and config.scale_factor >= 2 and not self.use_direct_fb:
            self._create_scanline_overlay()
    
    def _setup_direct_framebuffer(self) -> None:
        """Set up direct framebuffer output."""
        fb_device = os.environ.get('SDL_FBDEV', '/dev/fb0')
        
        if not is_framebuffer_available(fb_device):
            logger.warning(f"Framebuffer {fb_device} not available")
            return
        
        self.framebuffer = FramebufferOutput(fb_device)
        if self.framebuffer.initialize():
            self.use_direct_fb = True
            fb_size = self.framebuffer.get_size()
            logger.info(f"Direct framebuffer output enabled: {fb_size[0]}x{fb_size[1]}")
        else:
            self.framebuffer = None
            logger.warning("Failed to initialize direct framebuffer output")
    
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
        Uses direct framebuffer output when SDL video isn't available.
        """
        if self.use_direct_fb and self.framebuffer:
            # Direct framebuffer output - write native surface to /dev/fb0
            self.framebuffer.blit_surface(self.native_surface)
        elif self.config.scale_factor == 1:
            # No scaling needed, blit directly
            self.window.blit(self.native_surface, (0, 0))
            pygame.display.flip()
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
            
            pygame.display.flip()
    
    def cleanup(self) -> None:
        """Clean up renderer resources."""
        if self.framebuffer:
            self.framebuffer.cleanup()
            self.framebuffer = None
        # Surfaces are automatically cleaned up by Python GC
    
    def toggle_effects(self) -> None:
        """Toggle post-processing effects on/off."""
        self.config.effects_enabled = not self.config.effects_enabled
        
        if self.config.effects_enabled and self.config.scale_factor >= 2:
            self._create_scanline_overlay()
        else:
            self.scanline_overlay = None
