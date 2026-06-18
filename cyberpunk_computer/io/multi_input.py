"""
MultiInputPort — merge several InputPorts into one stream for the ingress.

Symmetric to :class:`MultiOutputPort`. The backend talks to two physical
RP2040 devices over separate USB-CDC serial ports:

    /dev/ttyACM0  gateway   — CAN + AVC-LAN + RS485 satellites (device ids 0/1/2/100+)
    /dev/ttyACM1  powerbox  — voltage/temperature telemetry + power events (future)

Each child port can be given a ``device_id_offset`` so a second device's ids do
not collide with the gateway's in the unified Store. The powerbox is expected to
live in a reserved range (see ``DEVICE_POWERBOX_BASE``). The powerbox protocol
is not implemented yet, so its port is created only when a port path is provided.

``poll()`` round-robins across children so no single noisy device starves the
others.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import List, Optional, Sequence, Tuple

from .ports import InputPort, MessageCategory, RawMessage

logger = logging.getLogger(__name__)


class MultiInputPort(InputPort):
    """Aggregate multiple :class:`InputPort` children into a single input."""

    def __init__(self, ports: Sequence[Tuple[InputPort, int]]) -> None:
        """
        Args:
            ports: sequence of ``(port, device_id_offset)``. The offset is added
                to each polled message's ``device_id`` (use 0 for the gateway).
        """
        self._ports: List[Tuple[InputPort, int]] = list(ports)
        self._next = 0

    def start(self) -> bool:
        """Start all child ports; succeed if at least one starts."""
        any_ok = False
        for port, _ in self._ports:
            try:
                if port.start():
                    any_ok = True
            except Exception:
                logger.exception("Failed to start input port %s", port.name)
        return any_ok

    def stop(self) -> None:
        for port, _ in self._ports:
            try:
                port.stop()
            except Exception:
                logger.exception("Failed to stop input port %s", port.name)

    def poll(self) -> Optional[RawMessage]:
        """Round-robin poll; return the next message from any child, or None."""
        n = len(self._ports)
        for i in range(n):
            idx = (self._next + i) % n
            port, offset = self._ports[idx]
            try:
                msg = port.poll()
            except Exception:
                logger.exception("poll() failed on %s", port.name)
                msg = None
            if msg is not None:
                self._next = (idx + 1) % n
                if offset:
                    # Re-tag into the child's reserved id range. Resetting the
                    # category to UNKNOWN makes RawMessage.__post_init__ recompute
                    # it from the new device id.
                    msg = replace(
                        msg,
                        device_id=msg.device_id + offset,
                        category=MessageCategory.UNKNOWN,
                    )
                return msg
        return None

    def is_connected(self) -> bool:
        return any(port.is_connected() for port, _ in self._ports)

    @property
    def name(self) -> str:
        return "MultiInputPort(" + ", ".join(p.name for p, _ in self._ports) + ")"
