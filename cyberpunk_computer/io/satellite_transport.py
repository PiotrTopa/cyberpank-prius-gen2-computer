"""
Satellite transport interface — the contract every satellite link implements.

See ``docs/SATELLITES.md``. A transport moves NDJSON lines between the car
computer and satellites over one physical medium (RS485 behind the gateway,
BLE via ``prius-bled``, Wi-Fi on the ``prius`` AP) and tells the
:class:`~cyberpunk_computer.backend.satellite_registry.SatelliteRegistry`
which devices it can see. Transports know nothing about trust, satellite ids
or the store: they address devices by a transport-specific ``address`` string
(RS485 device id, BLE MAC, Wi-Fi ``ip:port``), and the registry maps addresses
to satellite ids through the keychain.

Threading: ``tick``/``poll``/``send``/``discover`` are called from the backend
engine loop. A transport may run its own IO thread, but must hand results to
the engine loop through ``poll()`` (a thread-safe queue), never touch the
store, and never block for longer than a few milliseconds in any of the
methods below.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

TRANSPORT_RS485 = "rs485"
TRANSPORT_BLE = "ble"
TRANSPORT_WIFI = "wifi"
TRANSPORTS: Tuple[str, ...] = (TRANSPORT_RS485, TRANSPORT_BLE, TRANSPORT_WIFI)

# Satellite id ranges per transport (ids never move between transports).
ID_RANGES = {
    TRANSPORT_RS485: (100, 199),
    TRANSPORT_BLE: (300, 399),
    TRANSPORT_WIFI: (400, 499),
}

# Transports where the link itself is trusted (wired inside the car) and the
# application-level Ed25519 handshake is optional.
REQUIRE_AUTH = {
    TRANSPORT_RS485: False,
    TRANSPORT_BLE: True,
    TRANSPORT_WIFI: True,
}


@dataclass
class DiscoveredDevice:
    """A device the transport can see (connected or merely advertising)."""
    transport: str
    address: str
    name: str = ""
    rssi: Optional[int] = None
    last_seen: float = 0.0          # time.time()
    linked: bool = False            # a link (connection/session) is up right now
    fp: Optional[str] = None        # fingerprint offered during auth, if any
    pubkey: Optional[str] = None    # base64 raw Ed25519 public key offered during auth
    extra: dict = field(default_factory=dict)


@dataclass
class TransportStatus:
    """Health of one transport, mirrored into ``SatellitesState.transports``."""
    transport: str
    available: bool = False         # medium usable right now (bled reachable, AP up, rail on …)
    state: str = "init"             # short machine word: init|down|advertising|rail-off|rail-on|ap-down|listening|error
    detail: str = ""                # human hint ("10 s", "bled unreachable", …)
    last_discovery: float = 0.0     # time.time() of the last completed discovery pass
    devices_seen: int = 0           # size of discovered() right now


@dataclass
class LinkEvent:
    """Ingress from a transport, drained by the registry through ``poll()``.

    kind:
        ``"up"``   — a link to ``address`` came up (``name``/``extra`` optional);
        ``"down"`` — the link went away;
        ``"data"`` — one NDJSON line (without the trailing newline) from ``address``;
        ``"discovery"`` — a discovery pass finished (``devices`` = fresh list).
    """
    kind: str
    transport: str
    address: str = ""
    line: str = ""
    name: str = ""
    devices: Sequence[DiscoveredDevice] = ()
    extra: dict = field(default_factory=dict)
    ts: float = 0.0


class SatelliteTransport(ABC):
    """One medium carrying satellite NDJSON traffic. See module docstring."""

    #: one of TRANSPORTS
    name: str = ""

    @abstractmethod
    def start(self) -> bool:
        """Bring the transport up (open sockets, connect to bled …). Non-blocking."""

    @abstractmethod
    def stop(self) -> None:
        """Release everything. Idempotent."""

    @abstractmethod
    def tick(self, now: float) -> None:
        """Periodic housekeeping from the engine loop (reconnects, rediscovery
        cadence, keep-alives). Must return quickly."""

    @abstractmethod
    def poll(self) -> Optional[LinkEvent]:
        """Next pending event, or ``None``. Called repeatedly until ``None``."""

    @abstractmethod
    def send(self, address: str, line: str) -> bool:
        """Queue one NDJSON line (no trailing newline needed) to ``address``.
        Returns False when the device is not linked / the medium is down."""

    @abstractmethod
    def discover(self, force: bool = False) -> None:
        """Start a discovery pass (or schedule one); results arrive as a
        ``"discovery"`` LinkEvent. ``force`` ignores the rediscovery cadence."""

    @abstractmethod
    def discovered(self) -> List[DiscoveredDevice]:
        """Devices seen within the discovery TTL, linked ones first."""

    @abstractmethod
    def status(self) -> TransportStatus:
        """Current health snapshot."""

    def disconnect(self, address: str) -> None:  # optional for media without sessions
        """Drop the link to ``address`` (unknown/untrusted device, forget, revoke)."""

    def set_pairable(self, seconds: float) -> None:  # optional
        """Pairing window hint (BLE: make the adapter pairable; others: no-op)."""

    def set_advertisement(self, interval_ms: int, flags: int) -> None:  # optional
        """Advertisement hint (BLE only: interval + 2-byte flags)."""
