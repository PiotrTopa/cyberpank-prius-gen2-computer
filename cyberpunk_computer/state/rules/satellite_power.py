"""
Satellite power rules — OUT2 (RS485 satellite rail) wake-lock state machine.

The OUT2 rail policy is: **powered iff at least one named holder is held**
(``SatellitesState.power_holders``). Holders are wake-locks:

    * ``"acc"``      — held while the ignition (ACC) is on, so satellites are
                        always ready when the car is in use
                        (:class:`SatelliteAccHoldRule`);
    * ``"queue"``    — held by the satellite job queue while jobs are pending
                        or lingering (see backend.satellites), so scheduled
                        sensor reads / event commands with the key off share a
                        single power-up without rail flapping;
    * ``"manual:*"`` — operator/API holds.

:class:`SatellitePowerRule` translates the holder set into the physical rail:
it calls the injected ``set_out2`` (PowerboxCommander.set_out(2, …)) on
transitions, applies a linger before powering OFF (debounces key-off/burst
gaps), and re-asserts the command while the powerbox STATUS mirror
(``powerbox.out2``) disagrees — the STATUS heartbeat arrives at ~1 Hz, which
also provides the rule's re-evaluation clock for linger/retry timing.

The gateway board (CAN/AVC-LAN + RS485 master) shares exactly the same
logical requirement — needed when ACC is on (CAN/AVC) and when satellite jobs
run (RS485) — so the rule also drives the optional ``set_gateway`` callback
(USB hub-port power) in lockstep with OUT2. There is no separate manual
gateway control; its convergence/reassert is handled by the backend's
gateway-USB desired-state poll.

Both rules dispatch observability actions only; the actual rail truth remains
``powerbox.out2``.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional, Set

from ..store import Store, StateSlice
from ..app_state import AppState
from .engine import StateRule, RulePriority

from ..actions import (
    SatellitePowerHoldAction,
    SetSatellitePowerAction,
)

logger = logging.getLogger(__name__)

HOLDER_ACC = "acc"
HOLDER_QUEUE = "queue"


class SatelliteAccHoldRule(StateRule):
    """Hold the OUT2 rail while the ignition (ACC) is on.

    Acquires the ``"acc"`` holder on the OFF->ON edge and releases it on
    ON->OFF (the power-off itself is further debounced by the linger in
    :class:`SatellitePowerRule`).
    """

    @property
    def name(self) -> str:
        return "satellite_acc_hold"

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
        new_acc = bool(new_state.powerbox.acc_on)
        held = HOLDER_ACC in new_state.satellites.power_holders
        if new_acc == held:
            return
        logger.info("Satellite ACC hold %s (acc_on=%s)",
                    "acquire" if new_acc else "release", new_acc)
        store.dispatch(SatellitePowerHoldAction(HOLDER_ACC, acquire=new_acc))


class SatellitePowerRule(StateRule):
    """Drive the physical OUT2 rail from the wake-lock holder set.

    * desired = ``bool(power_holders)``;
    * ON is applied immediately;
    * OFF is applied only after ``linger_s`` of the holder set staying empty
      (re-checked on every POWERBOX/SATELLITES update — the powerbox STATUS
      heartbeat at ~1 Hz guarantees progress);
    * while the powerbox ``out2`` mirror disagrees with the applied value the
      command is re-sent every ``retry_s`` (handles a lost serial command or a
      powerbox reboot back to its boot default).
    """

    def __init__(
        self,
        set_out2: Callable[[bool], bool],
        linger_s: float = 10.0,
        retry_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        set_gateway: Optional[Callable[[bool], None]] = None,
    ):
        self._set_out2 = set_out2
        self._set_gateway = set_gateway
        self._linger_s = linger_s
        self._retry_s = retry_s
        self._clock = clock

        self._empty_since: Optional[float] = None
        self._applied: Optional[bool] = None   # last value we commanded
        self._last_send: float = 0.0

    @property
    def name(self) -> str:
        return "satellite_power"

    @property
    def watches(self) -> Set[StateSlice]:
        return {StateSlice.SATELLITES, StateSlice.POWERBOX}

    @property
    def priority(self) -> RulePriority:
        return RulePriority.NORMAL

    def _apply(self, on: bool, store: Store, reason: str) -> None:
        now = self._clock()
        try:
            self._set_out2(on)
        except Exception:
            logger.exception("SatellitePowerRule set_out2(%s) failed", on)
            return
        first = self._applied is not on
        self._applied = on
        self._last_send = now
        if first:
            # Gateway USB power is bonded to the rail: same holders, same
            # transitions. Only on edges — its convergence loop retries.
            if self._set_gateway is not None:
                try:
                    self._set_gateway(on)
                except Exception:
                    logger.exception("SatellitePowerRule set_gateway(%s) failed", on)
            logger.info("Satellite rail OUT2 -> %s (%s)", "ON" if on else "OFF", reason)
            store.dispatch(SetSatellitePowerAction(on))

    def evaluate(
        self,
        old_state: Optional[AppState],
        new_state: AppState,
        store: Store,
    ) -> None:
        now = self._clock()
        holders = new_state.satellites.power_holders
        desired = bool(holders)

        if desired:
            self._empty_since = None
            if self._applied is not True:
                self._apply(True, store, "holders=%s" % sorted(holders))
        else:
            if self._empty_since is None:
                self._empty_since = now
            if self._applied is not False and (now - self._empty_since) >= self._linger_s:
                self._apply(False, store, "no holders for %.0fs" % self._linger_s)

        # Re-assert while the powerbox mirror disagrees with what we commanded.
        actual = new_state.powerbox.out2
        if (
            self._applied is not None
            and actual is not None
            and actual != self._applied
            and (now - self._last_send) >= self._retry_s
        ):
            self._apply(self._applied, store,
                        "reassert (mirror out2=%s)" % actual)
