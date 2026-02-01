"""
VFD Satellite Display Package.

A standalone VFD (Vacuum Fluorescent Display) simulation that receives
data from the main CyberPunk Computer via NDJSON protocol.

Device ID: 110
"""

__version__ = "1.0.0"
__device_id__ = 110

from .state import VFDState
from .framebuffer import VFDFramebuffer
from .renderer import VFDRenderer

__all__ = ["VFDState", "VFDFramebuffer", "VFDRenderer"]
