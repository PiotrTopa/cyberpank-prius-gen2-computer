"""
Widget submodule.

Contains all base and specialized widget classes.
"""

from .base import Widget, Rect
from .frame import Frame
from .controls import VolumeBar, ToggleSwitch, ValueDisplay, ModeIcon, StatusIcon
from .energy_monitor import EnergyMonitorWidget, MiniEnergyMonitor
from .vehicle_status import VehicleStatusWidget, ConnectionIndicator
from .vfd_display import VFDDisplayWidget, VFDFramebuffer, VFDEnergyMonitor

__all__ = [
    "Widget", "Rect", "Frame",
    "VolumeBar", "ToggleSwitch", "ValueDisplay", "ModeIcon", "StatusIcon",
    "EnergyMonitorWidget", "MiniEnergyMonitor",
    "VehicleStatusWidget", "ConnectionIndicator",
    "VFDDisplayWidget", "VFDFramebuffer", "VFDEnergyMonitor"
]
