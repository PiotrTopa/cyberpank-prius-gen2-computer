"""
Persistence Middleware - Auto-saves user preferences on relevant state changes.

Listens for action types that modify user preferences (not live telemetry)
and persists the updated state to disk with debouncing.
"""

import logging
import time
from typing import TYPE_CHECKING

from ..state.actions import ActionType

if TYPE_CHECKING:
    from ..state.actions import Action
    from ..state.store import Store
    from ..persistence import SettingsManager

logger = logging.getLogger(__name__)

# Action types that modify user preferences and should trigger a save.
# Excludes all live telemetry (temps, RPMs, SOC, speed, etc.)
PERSISTABLE_ACTIONS = frozenset({
    # Audio EQ preferences
    ActionType.SET_VOLUME,
    ActionType.SET_BASS,
    ActionType.SET_MID,
    ActionType.SET_TREBLE,
    ActionType.SET_BALANCE,
    ActionType.SET_FADER,
    # Display preferences
    ActionType.SET_SCREEN_BRIGHTNESS,
    ActionType.SET_AMBIENT_COLOR,
    ActionType.SET_POWER_CHART_TIME_BASE,
})

# Minimum interval between saves (seconds) to avoid thrashing disk
SAVE_DEBOUNCE_SECONDS = 2.0


def create_persistence_middleware(settings_mgr: "SettingsManager"):
    """
    Create a Store middleware that auto-saves preferences on change.

    Uses debouncing: marks state as dirty on relevant actions,
    then saves only if enough time has passed since last save.

    Args:
        settings_mgr: The global SettingsManager instance

    Returns:
        Middleware function(action, store)
    """
    _last_save_time = 0.0
    _dirty = False

    def middleware(action: "Action", store: "Store") -> None:
        nonlocal _last_save_time, _dirty

        if action.type not in PERSISTABLE_ACTIONS:
            return

        _dirty = True
        now = time.time()

        if now - _last_save_time >= SAVE_DEBOUNCE_SECONDS:
            _flush(store)
            _last_save_time = now
            _dirty = False

    def _flush(store: "Store") -> None:
        """Extract preferences from state and save to disk."""
        try:
            settings_mgr.update_from_app_state(store.state)
            settings_mgr.save()
            logger.debug("Auto-saved user preferences")
        except Exception as e:
            logger.error(f"Failed to auto-save preferences: {e}")

    # Expose flush for shutdown save
    middleware.flush = lambda store: _flush(store) if _dirty else None  # type: ignore[attr-defined]
    middleware.is_dirty = lambda: _dirty  # type: ignore[attr-defined]

    return middleware
