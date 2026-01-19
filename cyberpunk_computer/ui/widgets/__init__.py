"""
Widget submodule.

Contains all base and specialized widget classes.
"""

from .base import Widget, Rect
from .frame import Frame
from .controls import VolumeBar, ToggleSwitch, ValueDisplay, ModeIcon, StatusIcon

__all__ = [
    "Widget", "Rect", "Frame",
    "VolumeBar", "ToggleSwitch", "ValueDisplay", "ModeIcon", "StatusIcon"
]
