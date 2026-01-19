"""
Application configuration.

All configuration values are centralized here for easy management
and environment-specific overrides.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    """Main application configuration."""
    
    # ─────────────────────────────────────────────────────────────────────────
    # Display Settings
    # ─────────────────────────────────────────────────────────────────────────
    
    # Native resolution (always render at this size)
    native_width: int = 480
    native_height: int = 240
    
    # Display scaling (1 = native, 2 = 960x480, 4 = 1920x960)
    scale_factor: int = 1
    
    # Fullscreen mode
    fullscreen: bool = False
    
    # Target frame rate
    target_fps: int = 30
    
    # Enable post-processing effects (scanlines, glow)
    effects_enabled: bool = True
    
    # ─────────────────────────────────────────────────────────────────────────
    # Development Settings
    # ─────────────────────────────────────────────────────────────────────────
    
    # Development mode (enables keyboard input, debug overlay)
    dev_mode: bool = False
    
    # Show FPS counter
    show_fps: bool = False
    
    # Show debug grid
    show_grid: bool = False
    
    # ─────────────────────────────────────────────────────────────────────────
    # Gateway Communication
    # ─────────────────────────────────────────────────────────────────────────
    
    # Enable Gateway connection
    gateway_enabled: bool = True
    
    # Serial port for Gateway (None = auto-detect)
    gateway_port: Optional[str] = None
    
    # Gateway baudrate
    gateway_baudrate: int = 1_000_000
    
    # Connection timeout in seconds
    gateway_timeout: float = 0.1
    
    # ─────────────────────────────────────────────────────────────────────────
    # Input Settings
    # ─────────────────────────────────────────────────────────────────────────
    
    # Key repeat delay (ms) for held keys in dev mode
    key_repeat_delay: int = 400
    key_repeat_interval: int = 100
    
    # ─────────────────────────────────────────────────────────────────────────
    # Computed Properties
    # ─────────────────────────────────────────────────────────────────────────
    
    @property
    def window_width(self) -> int:
        """Get actual window width (native * scale)."""
        return self.native_width * self.scale_factor
    
    @property
    def window_height(self) -> int:
        """Get actual window height (native * scale)."""
        return self.native_height * self.scale_factor
    
    @property
    def native_size(self) -> tuple[int, int]:
        """Get native resolution as tuple."""
        return (self.native_width, self.native_height)
    
    @property
    def window_size(self) -> tuple[int, int]:
        """Get window size as tuple."""
        return (self.window_width, self.window_height)
    
    def __post_init__(self):
        """Apply dev mode defaults."""
        if self.dev_mode:
            self.show_fps = True


# Default configuration instances
DEFAULT_CONFIG = Config()
DEV_CONFIG = Config(dev_mode=True, scale_factor=2)
