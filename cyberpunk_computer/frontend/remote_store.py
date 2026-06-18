"""
RemoteStore / RemoteTwin — a frontend-side stand-in for the engine's Store and
VirtualTwin, backed by the backend network API instead of local hardware.

The pygame UI consumes the engine through ``virtual_twin.store`` (subscribe /
state / dispatch / chart_data / replay_speed) and drives it with
``virtual_twin.update()`` each frame. RemoteTwin presents the same surface:

  - state snapshots arrive over the WebSocket on a client thread and are queued;
  - ``RemoteTwin.update()`` (called on the pygame thread) drains the queue,
    deserializes the newest snapshot into an AppState, feeds the local
    ChartDataStore, and notifies subscribers — preserving the engine's
    single-threaded "all dispatch on the main loop" model;
  - ``store.dispatch(action)`` translates UI actions into REST commands sent to
    the backend (the resulting state change comes back over the WebSocket).

This lets the existing screen code run unchanged on a remote display (e.g. a
Raspberry Pi) talking to the headless backend on the phone.
"""

from __future__ import annotations

import dataclasses
import logging
import queue
from typing import Callable, Dict, List, Optional, Tuple

from ..state.actions import (
    Action,
    BatchAction,
    SetACAction,
    SetAirDirectionAction,
    SetAutoModeAction,
    SetBalanceAction,
    SetBassAction,
    SetFaderAction,
    SetFanSpeedAction,
    SetMidAction,
    SetMuteAction,
    SetPowerChartTimeBaseAction,
    SetReadyModeAction,
    SetRecirculationAction,
    SetTargetTempAction,
    SetTrebleAction,
    SetVolumeAction,
)
from ..state.app_state import AppState
from ..state.chart_data import ChartDataStore
from ..state.rules.chart_data import ChartDataRule
from ..state.store import StateSlice
from .client import BackendClient
from .deserialize import deserialize_state

logger = logging.getLogger(__name__)

Subscriber = Callable[[AppState], None]


class RemoteStore:
    """Store-compatible view over backend state; dispatch -> backend commands."""

    def __init__(self, client: BackendClient) -> None:
        self._client = client
        self._state = AppState()
        self._subscribers: Dict[StateSlice, List[Subscriber]] = {s: [] for s in StateSlice}
        self._inbox: "queue.Queue[dict]" = queue.Queue(maxsize=64)
        self._chart_rule = ChartDataRule()
        self.chart_data = ChartDataStore()
        self._replay_speed: float = 1.0

    # ── Store-compatible surface used by the UI ──────────────────────────────

    @property
    def state(self) -> AppState:
        return self._state

    @property
    def replay_speed(self) -> float:
        return self._replay_speed

    @replay_speed.setter
    def replay_speed(self, value: float) -> None:
        self._replay_speed = max(0.1, value)

    def subscribe(self, slice_: StateSlice, callback: Subscriber) -> Callable[[], None]:
        self._subscribers[slice_].append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers[slice_]:
                self._subscribers[slice_].remove(callback)

        return unsubscribe

    def dispatch(self, action: Action) -> None:
        """Translate a UI action into a backend command (or apply locally)."""
        if isinstance(action, BatchAction):
            for sub in action.actions or []:
                self.dispatch(sub)
            return

        # Display-only action: no vehicle effect, apply to local state.
        if isinstance(action, SetPowerChartTimeBaseAction):
            new_display = self._state.display.with_time_base(action.time_base)
            self._state = dataclasses.replace(self._state, display=new_display)
            self._notify({StateSlice.DISPLAY})
            return

        translated = _translate(action)
        if translated is None:
            logger.debug("RemoteStore: no backend command for %s", type(action).__name__)
            return
        name, params = translated
        self._client.send_command(name, params)

    # ── snapshot intake (client thread -> main thread) ───────────────────────

    def enqueue_snapshot(self, state_dict: dict) -> None:
        """Called from the client thread; never blocks the engine/UI."""
        try:
            self._inbox.put_nowait(state_dict)
        except queue.Full:
            # Drop the oldest to keep only fresh state.
            try:
                self._inbox.get_nowait()
                self._inbox.put_nowait(state_dict)
            except queue.Empty:
                pass

    def apply_pending(self) -> int:
        """Drain queued snapshots on the main thread. Returns count applied."""
        count = 0
        while True:
            try:
                state_dict = self._inbox.get_nowait()
            except queue.Empty:
                break
            self._apply_snapshot(state_dict)
            count += 1
        return count

    def _apply_snapshot(self, state_dict: dict) -> None:
        old_state = self._state
        try:
            new_state = deserialize_state(state_dict)
        except Exception:
            logger.exception("Failed to deserialize state snapshot")
            return
        self._state = new_state
        # Feed local chart history using the exact same rule as the engine.
        try:
            self._chart_rule.evaluate(old_state, new_state, self)
        except Exception:
            logger.debug("Chart rule failed on remote snapshot", exc_info=True)
        self._notify(set(StateSlice))

    def _notify(self, slices) -> None:
        notified = set()
        for slice_ in slices:
            for callback in list(self._subscribers.get(slice_, [])):
                if callback not in notified:
                    try:
                        callback(self._state)
                    except Exception:
                        logger.exception("Subscriber error")
                    notified.add(callback)


def _translate(action: Action) -> Optional[Tuple[str, Dict]]:
    """Map a UI Action to a (command_name, params) pair, or None if unsupported."""
    if isinstance(action, SetVolumeAction):
        return "set_volume", {"value": action.volume}
    if isinstance(action, SetMuteAction):
        return "set_mute", {"muted": action.muted}
    if isinstance(action, SetBassAction):
        return "set_tone", {"bass": action.bass}
    if isinstance(action, SetMidAction):
        return "set_tone", {"mid": action.mid}
    if isinstance(action, SetTrebleAction):
        return "set_tone", {"treble": action.treble}
    if isinstance(action, SetBalanceAction):
        return "set_tone", {"balance": action.balance}
    if isinstance(action, SetFaderAction):
        return "set_tone", {"fader": action.fader}
    if isinstance(action, SetTargetTempAction):
        return "set_climate", {"target_temp": action.temp}
    if isinstance(action, SetFanSpeedAction):
        return "set_climate", {"fan": action.speed}
    if isinstance(action, SetACAction):
        return "set_climate", {"ac": action.ac_on}
    if isinstance(action, SetAutoModeAction):
        return "set_climate", {"auto": action.auto_mode}
    if isinstance(action, SetRecirculationAction):
        return "set_climate", {"recirculation": action.recirculation}
    if isinstance(action, SetAirDirectionAction):
        return "set_climate", {"air_direction": action.direction}
    if isinstance(action, SetReadyModeAction):
        return "set_ready", {"on": action.ready}
    return None


class _IngressShim:
    """Minimal ingress stand-in so the pygame Application composes unchanged."""

    class _Stats:
        messages_received = 0
        avc_messages = 0
        can_messages = 0
        errors = 0

    def __init__(self) -> None:
        self.stats = self._Stats()
        self._log_callbacks: list = []

    def add_message_log_callback(self, callback) -> None:
        # The frontend has no raw message stream; keep for API compatibility.
        self._log_callbacks.append(callback)

    def set_analysis_mode(self, enabled: bool) -> None:
        pass

    def set_solicited_debug(self, enabled: bool) -> None:
        pass


class RemoteTwin:
    """VirtualTwin-compatible facade backed by the backend network API."""

    def __init__(
        self,
        host: str,
        port: int = 8080,
        token: Optional[str] = None,
        poll_interval: float = 1.0,
    ) -> None:
        self._client = BackendClient(
            host=host,
            port=port,
            token=token,
            on_state=self._on_state,
            poll_interval=poll_interval,
        )
        self.store = RemoteStore(self._client)
        self.ingress = _IngressShim()
        self.input_port = None  # no local input source on the frontend

    def _on_state(self, state_dict: dict) -> None:
        # Runs on the client thread: just enqueue; applied in update().
        self.store.enqueue_snapshot(state_dict)

    def start(self) -> bool:
        self._client.start()
        logger.info("RemoteTwin started (backend client connecting)")
        return True

    def stop(self) -> None:
        self._client.stop()

    def update(self) -> int:
        return self.store.apply_pending()

    @property
    def connected(self) -> bool:
        return self._client.connected
