"""
Power-management rules — POCO power profile + 12 V under-voltage protection.

These rules react to :data:`StateSlice.POWERBOX` updates fed by the powerbox
RP2040 (ignition position + INA219 telemetry, see :mod:`cyberpunk_computer.io.powerbox`)
and drive two side effects through injected callables so they stay unit-testable:

    * :class:`PowerModeRule` — switches the POCO CPU power profile to ``full`` when
      the ignition (ACC/stacyjka) is on and ``low`` when the key is off.
    * :class:`UndervoltageProtectionRule` — when the Prius 12 V aux battery sags
      below a threshold for a sustained period, it flags the state and asks the
      powerbox to cut POCO power (after a graceful shutdown window).

Both rules are registered by the backend only; the dev/pygame path is untouched.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional, Set

from ..store import Store, StateSlice
from ..app_state import AppState
from .engine import StateRule, RulePriority

from ..actions import (
    RequestPowerboxShutdownAction,
    SetPowerboxUndervoltageAction,
)

logger = logging.getLogger(__name__)


class PowerModeRule(StateRule):
    """Switch the POCO CPU power profile based on the ignition position.

    ``apply_mode`` is called with ``True`` for ACC/ON (full performance) and
    ``False`` for key-off (low idle). It is only called on an actual transition
    of ``acc_on`` (or on the first observed state).
    """

    def __init__(self, apply_mode: Callable[[bool], None]):
        self._apply_mode = apply_mode

    @property
    def name(self) -> str:
        return "power_mode"

    @property
    def watches(self) -> Set[StateSlice]:
        return {StateSlice.POWERBOX}

    @property
    def priority(self) -> RulePriority:
        return RulePriority.HIGH

    def evaluate(
        self,
        old_state: Optional[AppState],
        new_state: AppState,
        store: Store,
    ) -> None:
        new_acc = new_state.powerbox.acc_on
        old_acc = old_state.powerbox.acc_on if old_state is not None else None
        if old_acc == new_acc:
            return
        try:
            self._apply_mode(new_acc)
        except Exception:
            logger.exception("PowerModeRule.apply_mode failed (acc_on=%s)", new_acc)


class UndervoltageProtectionRule(StateRule):
    """Cut POCO power when the 12 V aux battery is sustained under threshold.

    The Prius 12 V battery is monitored via the powerbox INA219. When the bus
    voltage stays below ``threshold`` for at least ``confirm_seconds`` (and the
    battery line is present), the rule:

        1. dispatches :class:`SetPowerboxUndervoltageAction` (state observability);
        2. dispatches :class:`RequestPowerboxShutdownAction` (state flag, drives egress);
        3. calls ``request_shutdown(reason)`` once (sends the powerbox command and/or
           triggers a local clean shutdown).

    The shutdown is latched. The under-voltage flag clears (hysteresis) once the
    voltage recovers above ``recover_threshold`` for ``recover_seconds``.
    """

    def __init__(
        self,
        request_shutdown: Callable[[str], None],
        threshold: float = 11.0,
        recover_threshold: float = 11.5,
        confirm_seconds: float = 5.0,
        recover_seconds: float = 5.0,
        grace_seconds: int = 30,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._request_shutdown = request_shutdown
        self._threshold = threshold
        self._recover_threshold = recover_threshold
        self._confirm_seconds = confirm_seconds
        self._recover_seconds = recover_seconds
        self._grace_seconds = grace_seconds
        self._clock = clock

        self._below_since: Optional[float] = None
        self._above_since: Optional[float] = None
        self._undervoltage_active = False
        self._shutdown_latched = False

    @property
    def name(self) -> str:
        return "undervoltage_protection"

    @property
    def watches(self) -> Set[StateSlice]:
        return {StateSlice.POWERBOX}

    @property
    def priority(self) -> RulePriority:
        return RulePriority.HIGH

    def evaluate(
        self,
        old_state: Optional[AppState],
        new_state: AppState,
        store: Store,
    ) -> None:
        pb = new_state.powerbox
        voltage = pb.system_voltage
        if voltage is None or not pb.batt_present:
            # No reading or battery line absent — do not act, reset timers.
            self._below_since = None
            self._above_since = None
            return

        now = self._clock()

        # Track sustained low voltage -> trip.
        if voltage < self._threshold:
            self._above_since = None
            if self._below_since is None:
                self._below_since = now
            elapsed = now - self._below_since
            if elapsed >= self._confirm_seconds and not self._shutdown_latched:
                self._trip(store, voltage)
            elif elapsed >= self._confirm_seconds and not self._undervoltage_active:
                self._set_flag(store, True)
        else:
            self._below_since = None
            # Track sustained recovery -> clear flag (latch stays until reboot).
            if voltage >= self._recover_threshold:
                if self._above_since is None:
                    self._above_since = now
                if (now - self._above_since) >= self._recover_seconds and self._undervoltage_active:
                    self._set_flag(store, False)
            else:
                self._above_since = None

    def _set_flag(self, store: Store, active: bool) -> None:
        if self._undervoltage_active == active:
            return
        self._undervoltage_active = active
        store.dispatch(SetPowerboxUndervoltageAction(active))

    def _trip(self, store: Store, voltage: float) -> None:
        self._shutdown_latched = True
        reason = f"undervoltage {voltage:.2f}V < {self._threshold:.2f}V"
        logger.warning("Under-voltage protection tripped: %s", reason)
        self._set_flag(store, True)
        store.dispatch(RequestPowerboxShutdownAction(reason))
        try:
            self._request_shutdown(reason)
        except Exception:
            logger.exception("UndervoltageProtectionRule.request_shutdown failed")
