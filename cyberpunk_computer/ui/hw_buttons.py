"""
MFD hardware / soft button -> screen mapping (one table, one place).

The backend publishes ``"button"`` events ``{"key", "pressed", "t"}`` for the
MFD bezel buttons (menu/dest/map) and the info-screen soft buttons
(maintenance/calendar/language). Only the press edge acts; releases are
ignored. Every target first unwinds the screen stack to the home dashboard,
so a button always lands on a known screen regardless of where the user was.

| key          | target           | meaning                                         |
|--------------|------------------|-------------------------------------------------|
| menu         | home             | home dashboard, page 1 (HOME), edits cancelled  |
| map          | home_dynamics    | dashboard page 2 (DYNAMICS: power/energy flow); |
|              |                  | pressed again on that page -> back to page 1    |
| dest         | engine_menu      | ENGINE / DIAGNOSTICS hub (lists every sub-view) |
| maintenance  | dtc              | DIAGNOSTICS / DTC (error codes)                 |
| calendar     | None             | logged, no-op (no natural target)               |
| language     | None             | logged, no-op (no natural target)               |
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

HW_BUTTON_TARGETS: Dict[str, Optional[str]] = {
    "menu": "home",
    "map": "home_dynamics",
    "dest": "engine_menu",
    "maintenance": "dtc",
    "calendar": None,
    "language": None,
}


def handle_hw_button(app, key: str, pressed: bool = True) -> Optional[str]:
    """Act on a hardware/soft button press. Returns the target name used
    (None for releases, unmapped keys and no-op keys)."""
    if not pressed:
        return None
    if key not in HW_BUTTON_TARGETS:
        logger.info("MFD button %r: unknown key, ignored", key)
        return None
    target = HW_BUTTON_TARGETS[key]
    if target is None:
        logger.info("MFD button %r: no screen mapped (no-op)", key)
        return None
    was_home = not getattr(app, "screen_stack", None)
    prev_page = getattr(app.current_screen, "current_page", None) if was_home else None
    home = app.go_home()
    if home is None:
        return None
    if target == "home":
        home.show_page(0)
    elif target == "home_dynamics":
        # Toggle: MAP pressed while already looking at DYNAMICS -> HOME.
        home.show_page(0 if prev_page == 1 else 1)
    elif target == "engine_menu":
        home.open_engine_menu()
    elif target == "dtc":
        home.open_dtc()
    logger.info("MFD button %r -> %s", key, target)
    return target
