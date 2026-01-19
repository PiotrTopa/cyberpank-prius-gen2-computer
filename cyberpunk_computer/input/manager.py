"""
Input manager.

Handles input abstraction between development (keyboard)
and production (encoder) input sources.
"""

import pygame
from enum import Enum, auto
from typing import Optional
from dataclasses import dataclass

from ..config import Config


class InputEvent(Enum):
    """
    Abstract input events.
    
    These are the logical input events that the UI responds to,
    independent of the physical input source.
    """
    
    # Rotation events (encoder turn or arrow keys)
    ROTATE_LEFT = auto()
    ROTATE_RIGHT = auto()
    
    # Press events (encoder clicks or keyboard)
    PRESS_LIGHT = auto()   # Short/light press - Enter key
    PRESS_STRONG = auto()  # Strong/long press - Space key
    
    # Navigation
    BACK = auto()          # Back/cancel - Escape key


@dataclass
class EncoderConfig:
    """
    Configuration to send to the encoder.
    
    When focus changes, the UI can request a specific
    encoder behavior mode.
    """
    
    mode: str = "stepped"  # "smooth" or "stepped"
    min_val: int = 0
    max_val: int = 100
    step: int = 1
    detents: bool = True   # Haptic detent feedback
    current: int = 0       # Current position
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "mode": self.mode,
            "min": self.min_val,
            "max": self.max_val,
            "step": self.step,
            "detents": self.detents,
            "current": self.current
        }


class InputManager:
    """
    Manages input from different sources.
    
    In development mode, maps keyboard to input events.
    In production mode, would read from encoder hardware.
    """
    
    # Keyboard mapping for development
    KEY_MAP = {
        pygame.K_LEFT: InputEvent.ROTATE_LEFT,
        pygame.K_RIGHT: InputEvent.ROTATE_RIGHT,
        pygame.K_RETURN: InputEvent.PRESS_LIGHT,
        pygame.K_SPACE: InputEvent.PRESS_STRONG,
        pygame.K_ESCAPE: InputEvent.BACK,
        
        # Alternative mappings
        pygame.K_UP: InputEvent.ROTATE_LEFT,
        pygame.K_DOWN: InputEvent.ROTATE_RIGHT,
        pygame.K_KP_ENTER: InputEvent.PRESS_LIGHT,
    }
    
    def __init__(self, config: Config):
        """
        Initialize the input manager.
        
        Args:
            config: Application configuration
        """
        self.config = config
        self.dev_mode = config.dev_mode
        
        # Current encoder configuration
        self._encoder_config = EncoderConfig()
        
        # For key repeat in dev mode
        pygame.key.set_repeat(
            config.key_repeat_delay,
            config.key_repeat_interval
        )
    
    def process_event(self, event: pygame.event.Event) -> Optional[InputEvent]:
        """
        Process a pygame event and return an input event.
        
        Args:
            event: Pygame event to process
        
        Returns:
            InputEvent if event was recognized, None otherwise
        """
        if event.type == pygame.KEYDOWN:
            return self._handle_keydown(event)
        
        # TODO: Handle encoder events when hardware is connected
        # Would listen for serial input or GPIO events
        
        return None
    
    def _handle_keydown(self, event: pygame.event.Event) -> Optional[InputEvent]:
        """
        Handle keyboard input.
        
        Args:
            event: Pygame KEYDOWN event
        
        Returns:
            Mapped InputEvent or None
        """
        return self.KEY_MAP.get(event.key)
    
    def set_encoder_config(self, config: EncoderConfig) -> None:
        """
        Set the encoder configuration.
        
        In production, this would send a command to the encoder
        hardware to change its behavior.
        
        Args:
            config: New encoder configuration
        """
        self._encoder_config = config
        
        if not self.dev_mode:
            # TODO: Send configuration to encoder via Gateway
            # message = {"mode": config.to_dict()}
            # gateway.send(ENCODER_DEVICE_ID, message)
            pass
    
    def get_encoder_config(self) -> EncoderConfig:
        """Get current encoder configuration."""
        return self._encoder_config


# Predefined encoder configurations for common use cases
ENCODER_CONFIGS = {
    "volume": EncoderConfig(
        mode="smooth",
        min_val=0,
        max_val=100,
        step=1,
        detents=False,
        current=50
    ),
    
    "menu": EncoderConfig(
        mode="stepped",
        min_val=0,
        max_val=10,  # Will be set based on menu items
        step=1,
        detents=True,
        current=0
    ),
    
    "mode_selector": EncoderConfig(
        mode="stepped",
        min_val=0,
        max_val=4,
        step=1,
        detents=True,
        current=0
    ),
    
    "temperature": EncoderConfig(
        mode="smooth",
        min_val=16,
        max_val=28,
        step=1,
        detents=True,
        current=21
    ),
}
