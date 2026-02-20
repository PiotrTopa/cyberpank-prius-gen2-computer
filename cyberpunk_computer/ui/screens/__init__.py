"""
Screen definitions.

Contains all application screens (main dashboard, submenus, etc.)
"""

from .base import Screen
from .main_screen import MainScreen
from .audio_screen import AudioScreen
from .climate_screen import ClimateScreen
from .lights_screen import LightsScreen
from .ambient_screen import AmbientScreen
from .engine_screen import EngineScreen
from .engine_menu_screen import EngineMenuScreen
from .engine_detail_screen import EngineDetailScreen
from .ev_screen import EVScreen
from .battery_screen import BatteryScreen
from .dtc_screen import DTCScreen
from .avc_monitor_screen import AVCMonitorScreen
from .solicited_monitor_screen import SolicitedMonitorScreen

__all__ = [
    "Screen",
    "MainScreen",
    "AudioScreen",
    "ClimateScreen",
    "LightsScreen",
    "AmbientScreen",
    "EngineScreen",
    "EngineMenuScreen",
    "EngineDetailScreen",
    "EVScreen",
    "BatteryScreen",
    "DTCScreen",
    "AVCMonitorScreen",
    "SolicitedMonitorScreen",
]
