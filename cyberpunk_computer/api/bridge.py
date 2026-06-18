"""
StoreBridge — the thread boundary between the single-threaded engine loop and
the asyncio-based network API.

Concurrency model (see store.py: the Store is single-threaded, no locks):

    engine thread        owns the Store. ONLY this thread calls store.dispatch.
    api thread           runs uvicorn + an asyncio loop, handles HTTP/WS.

Two directions cross the boundary:

  commands  api -> engine : HTTP handlers enqueue Actions on a thread-safe
                            queue.Queue. The engine loop calls drain_commands()
                            once per tick and dispatches them on its own thread.

  snapshots engine -> api : the Store subscription callback (engine thread) calls
                            on_state(); we hand the serialized snapshot to the
                            asyncio loop via loop.call_soon_threadsafe and fan it
                            out to per-connection queues (latest-only coalescing),
                            so a burst of dispatches never blocks the engine and
                            slow WS clients only ever see the newest state.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import time
from typing import Any, Dict, Optional, Set

from .commands import build_command
from .serialization import serialize_state

logger = logging.getLogger(__name__)


class StoreBridge:
    """Marshal commands and state snapshots across the engine/API thread boundary."""

    def __init__(self, store: Any, command_queue_max: int = 256) -> None:
        self._store = store
        self._commands: "queue.Queue[Any]" = queue.Queue(maxsize=command_queue_max)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._clients: Set["asyncio.Queue[Dict[str, Any]]"] = set()
        self._latest: Optional[Dict[str, Any]] = None
        self._latest_ts: float = 0.0

    # ── engine (main) thread side ────────────────────────────────────────────

    def on_state(self, state: Any) -> None:
        """Store subscription callback. Runs on the engine thread."""
        envelope = {"type": "state", "ts": time.time(), "state": serialize_state(state)}
        self._latest = envelope
        self._latest_ts = envelope["ts"]
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._fanout, envelope)
            except RuntimeError:
                # Loop is shutting down; drop the snapshot.
                pass

    def drain_commands(self) -> int:
        """Dispatch all queued commands on the engine thread. Returns count."""
        count = 0
        while True:
            try:
                action = self._commands.get_nowait()
            except queue.Empty:
                break
            try:
                self._store.dispatch(action)
                count += 1
            except Exception:
                logger.exception("Failed to dispatch command action %r", action)
        return count

    # ── asyncio loop thread side ─────────────────────────────────────────────

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Register the API's event loop (called from the API thread at startup)."""
        self._loop = loop

    def _fanout(self, envelope: Dict[str, Any]) -> None:
        """Push the latest snapshot to each client queue. Runs on the loop thread."""
        for q in self._clients:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(envelope)
            except asyncio.QueueFull:
                pass

    def register_client(self) -> "asyncio.Queue[Dict[str, Any]]":
        """Create a latest-only queue for a WS connection. Runs on the loop thread."""
        q: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=1)
        self._clients.add(q)
        if self._latest is not None:
            q.put_nowait(self._latest)
        return q

    def unregister_client(self, q: "asyncio.Queue[Dict[str, Any]]") -> None:
        """Drop a WS connection's queue. Runs on the loop thread."""
        self._clients.discard(q)

    # ── API thread side (HTTP handlers) ──────────────────────────────────────

    def submit_command(self, name: str, params: Dict[str, Any]) -> Any:
        """Build and enqueue a command Action for the engine thread.

        Raises UnknownCommand / CommandError (from build_command) or RuntimeError
        if the command queue is saturated.
        """
        action = build_command(name, params)
        try:
            self._commands.put_nowait(action)
        except queue.Full:
            raise RuntimeError("command queue is full; engine not draining")
        return action

    def latest_snapshot(self) -> Optional[Dict[str, Any]]:
        """Most recent serialized state envelope, or None before the first tick."""
        return self._latest
