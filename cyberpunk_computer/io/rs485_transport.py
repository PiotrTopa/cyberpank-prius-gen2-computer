"""
RS485 satellite transport — the gateway bus behind the relay ch5 rail.

See ``docs/SATELLITES.md`` §4 (RS485). This is a thin adapter: the RS485
satellites already work through the gateway ``OutputPort`` (egress) and the
ingress satellite dispatch (presence via ``UpdateSatelliteNodeAction(seen=
True)``), and the RS485 job queue keeps owning the rail wake-lock. The
transport only gives the registry the common view:

* **address** = the device id as a string (``"107"``);
* **presence** is passive: ingress keeps refreshing the twin nodes, and
  :meth:`discovered` reads them back from the store (devices 100–199 seen
  within the discovery TTL);
* **rediscovery** = ``{"a":"status"}`` to every candidate id, submitted by the
  registry *through the RS485 job queue* (``request_poll`` callback) so the
  rail wake-lock is taken exactly like any other satellite job;
* **availability** = the rail is up (``powerbox.rs485_power``; without a
  powerbox link: the rail was requested, as the queue assumes).

No thread, no own sockets; everything runs on the engine loop.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from typing import Callable, Deque, Iterable, List, Optional

from .ports import OutgoingCommand, OutputPort
from .satellite_transport import (
    DiscoveredDevice,
    LinkEvent,
    SatelliteTransport,
    TransportStatus,
    TRANSPORT_RS485,
    ID_RANGES,
)

logger = logging.getLogger(__name__)


class Rs485Transport(SatelliteTransport):
    """SatelliteTransport over the existing gateway serial path."""

    name = TRANSPORT_RS485

    def __init__(
        self,
        output_port: Optional[OutputPort],
        state_fn: Callable[[], object],
        candidate_ids: Callable[[], Iterable[int]] = lambda: (),
        request_poll: Optional[Callable[[List[int], dict], None]] = None,
        discovery_ttl_s: float = 600.0,
        poll_payload: Optional[dict] = None,
    ):
        self._port = output_port
        self._state_fn = state_fn
        self._candidate_ids = candidate_ids
        self.request_poll = request_poll
        self._ttl = discovery_ttl_s
        self._poll_payload = dict(poll_payload or {"a": "status"})
        self._events: Deque[LinkEvent] = deque()
        self._last_discovery = 0.0
        self._started = False

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> bool:
        self._started = True
        return True

    def stop(self) -> None:
        self._started = False

    def tick(self, now: float) -> None:
        # Cadence is owned by the registry; nothing to maintain here.
        return None

    def poll(self) -> Optional[LinkEvent]:
        return self._events.popleft() if self._events else None

    # ── rail ────────────────────────────────────────────────────────────────

    def rail_up(self) -> bool:
        state = self._state_fn()
        pb = state.powerbox
        if pb.connected and pb.rs485_power is not None:
            return bool(pb.rs485_power)
        return bool(state.satellites.power_requested or not pb.connected)

    # ── egress ──────────────────────────────────────────────────────────────

    def send(self, address: str, line: str) -> bool:
        """Send one line to device ``address``. The line is an NDJSON envelope
        (``{"d": {...}}``) or a bare payload object; the gateway gets the usual
        ``OutgoingCommand(device_id, "satellite", payload)``."""
        if self._port is None:
            return False
        try:
            device_id = int(address)
            obj = json.loads(line) if isinstance(line, str) else dict(line)
        except (TypeError, ValueError):
            logger.warning("RS485 send: bad address/line %r %r", address, line)
            return False
        payload = obj.get("d", obj) if isinstance(obj, dict) else None
        if not isinstance(payload, dict):
            return False
        try:
            return bool(self._port.send(OutgoingCommand(
                device_id=device_id, command_type="satellite", payload=payload)))
        except Exception:
            logger.exception("RS485 send to %s failed", address)
            return False

    # ── discovery ───────────────────────────────────────────────────────────

    def candidates(self) -> List[int]:
        lo, hi = ID_RANGES[TRANSPORT_RS485]
        ids = set()
        for dev in self._candidate_ids():
            try:
                dev = int(dev)
            except (TypeError, ValueError):
                continue
            if lo <= dev <= hi:
                ids.add(dev)
        return sorted(ids)

    def discover(self, force: bool = False) -> None:
        """Poll every candidate id with ``{"a":"status"}`` through the RS485
        queue. Without ``force`` this only runs while the rail is already up:
        a background rediscovery must never power the rail (and the gateway
        hub bonded to it) with the key off. ``force`` (operator) does."""
        if not force and not self.rail_up():
            return
        ids = self.candidates()
        if not ids or self.request_poll is None:
            self._finish_discovery()
            return
        try:
            self.request_poll(ids, dict(self._poll_payload))
        except Exception:
            logger.exception("RS485 rediscovery submit failed")

    def _finish_discovery(self) -> None:
        """Called by the registry when the poll job is done (or immediately)."""
        self._last_discovery = time.time()
        self._events.append(LinkEvent(kind="discovery", transport=self.name,
                                      devices=tuple(self.discovered()),
                                      ts=self._last_discovery))

    finish_discovery = _finish_discovery

    def discovered(self) -> List[DiscoveredDevice]:
        state = self._state_fn()
        now = time.time()
        lo, hi = ID_RANGES[TRANSPORT_RS485]
        out = []
        for dev, node in state.satellites.nodes.items():
            if not (lo <= dev <= hi) or not node.last_seen:
                continue
            if (now - node.last_seen) > self._ttl:
                continue
            out.append(DiscoveredDevice(
                transport=self.name, address=str(dev), name=node.name,
                last_seen=node.last_seen, linked=bool(node.online)))
        out.sort(key=lambda d: (not d.linked, int(d.address)))
        return out

    def status(self) -> TransportStatus:
        up = self.rail_up()
        return TransportStatus(
            transport=self.name,
            available=up,
            state="rail-on" if up else "rail-off",
            detail="" if self._port is not None else "no gateway port",
            last_discovery=self._last_discovery,
            devices_seen=len(self.discovered()),
        )
