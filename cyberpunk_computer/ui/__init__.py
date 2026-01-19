"""
UI framework module.

Contains the widget system, screens, and visual components.
"""

from .colors import COLORS
from .widgets.base import Widget, Rect
from .widgets.frame import Frame
from .focus import FocusManager

__all__ = [
    "COLORS",
    "Widget",
    "Rect",
    "Frame",
    "FocusManager"
]
