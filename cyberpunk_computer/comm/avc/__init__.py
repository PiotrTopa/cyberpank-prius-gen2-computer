"""
AVC-LAN support for gateway firmware 3.x (verified protocol map).

Source of truth: ``cyberpunk-prius-gateway/captures/2026-10-07/AVC_MAP.md`` and
``NAV_EMULATION_SPEC.md``; see ``docs/AVC_BACKEND.md`` in this repo.

* :mod:`.frames`         frame model + gateway TX payloads
* :mod:`.decoder`        table-driven decoder → typed updates
* :mod:`.touch`          touch panel calibration (raw → 480×240)
* :mod:`.nav_emulator`   nav ECU (178) emulation (port of nav_emu.py)
* :mod:`.climate_remote` climate touch-button remote control
* :mod:`.scheduler`      timer thread / manual test scheduler

Nothing in this package imports pygame.
"""

from .frames import AvcFrame, BCAST_1FF, BCAST_FFF, CLIMATE, DISPLAY, NAV, tx_payload
from .touch import TouchCalibration, DEFAULT_CALIBRATION
from .decoder import (
    AvcDecoder, AvcUpdate, BatteryInfo, ClimateButtonFrame, ClimateSetpoint,
    ClimateStatus, DisplayScreen, EnergyFlow, Lights, NavButton, NavLink,
    OutsideTemp, Touch, CLIMATE_BUTTONS, SCREENS, NAV_BUTTONS, INFO_BUTTONS,
)
from .climate_remote import ClimateRemote, BUTTON_NAMES as CLIMATE_BUTTON_NAMES
from .nav_emulator import NavEmulator
from .scheduler import ManualScheduler, ThreadScheduler

__all__ = [
    "AvcFrame", "BCAST_1FF", "BCAST_FFF", "CLIMATE", "DISPLAY", "NAV", "tx_payload",
    "TouchCalibration", "DEFAULT_CALIBRATION",
    "AvcDecoder", "AvcUpdate", "BatteryInfo", "ClimateButtonFrame", "ClimateSetpoint",
    "ClimateStatus", "DisplayScreen", "EnergyFlow", "Lights", "NavButton", "NavLink",
    "OutsideTemp", "Touch", "CLIMATE_BUTTONS", "SCREENS", "NAV_BUTTONS", "INFO_BUTTONS",
    "ClimateRemote", "CLIMATE_BUTTON_NAMES", "NavEmulator",
    "ManualScheduler", "ThreadScheduler",
]
