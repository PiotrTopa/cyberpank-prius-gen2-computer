"""
Input handling module.

Provides abstraction layer for different input sources including:
- Encoder (rotary + button)
- Touch screen
- Keyboard (for testing)
"""

from .manager import InputManager, InputEvent
from .touch import (
    TouchHandler,
    TouchEvent,
    TouchEventType,
    TouchZone,
    SwipeDirection,
    touch_to_input_event,
)

__all__ = [
    "InputManager",
    "InputEvent",
    "TouchHandler",
    "TouchEvent",
    "TouchEventType",
    "TouchZone",
    "SwipeDirection",
    "touch_to_input_event",
]
