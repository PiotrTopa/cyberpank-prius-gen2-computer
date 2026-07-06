"""
USB-CDC serial role discovery.

The backend talks to two RP2040 devices over USB-CDC: the **gateway** (CAN +
AVC-LAN + RS485) and the **powerbox** (12 V telemetry + ignition). Both are
MicroPython boards, so the kernel enumerates them as ``/dev/ttyACM*`` in an
arbitrary order that changes across reboots and re-plugging (the powerbox might
come up as ``ttyACM0`` one boot and ``ttyACM3`` the next).

Both devices speak the same NDJSON envelope and *both* use device ids ``1`` and
``2`` (for different buses), so the numeric id alone can NOT tell them apart.
Identity therefore comes from the **SYSTEM channel (id 0)**:

    * the unified identify reply ``{"msg":"IDENT","role":"gateway"|"powerbox"}``
      sent in response to ``{"id":0,"d":{"a":"whoami"}}``; or
    * the boot banner ``GATEWAY_READY`` / ``POWERBOX_READY`` (also carries
      ``role`` on recent firmware).

This module enumerates candidate ports (preferring the stable
``/dev/serial/by-id`` symlinks, which survive ``ttyACM`` renumbering), probes
each with a ``whoami`` and classifies it. :func:`classify_lines` is pure and
unit-tested; the IO wrapper :func:`identify_port` opens the port only briefly so
it never fights the long-lived :class:`SerialPort` reader.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import time
from typing import Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

ROLE_GATEWAY = "gateway"
ROLE_POWERBOX = "powerbox"
KNOWN_ROLES = (ROLE_GATEWAY, ROLE_POWERBOX)

# Map boot-banner ``msg`` values to a role (fallback when ``role`` is absent).
_BANNER_ROLES = {
    "GATEWAY_READY": ROLE_GATEWAY,
    "POWERBOX_READY": ROLE_POWERBOX,
}

# The identify request both firmwares answer on the SYSTEM channel.
WHOAMI_REQUEST = '{"id":0,"d":{"a":"whoami"}}\n'

DEFAULT_BAUDRATE = 1_000_000
DEFAULT_PROBE_TIMEOUT = 3.0

# Physical USB-hub-port → role map. The devices are maintained on dedicated hub
# ports (powerbox on port 1, gateway on port 2), so role can be resolved purely
# from the topology — no probing, and it works even when a device is wedged and
# silent. This is the primary discovery strategy; whoami is the fallback.
DEFAULT_PORT_ROLES: Dict[int, str] = {1: ROLE_POWERBOX, 2: ROLE_GATEWAY}


def parse_hub_port(usbdev: str):
    """Split a USB device sysfs name into ``(hub_location, port)``.

    Examples::

        "1-1.1"   -> ("1-1", 1)       # powerbox: hub 1-1, port 1
        "1-1.2"   -> ("1-1", 2)       # gateway:  hub 1-1, port 2
        "1-1.3.2" -> ("1-1.3", 2)     # nested hub
        "1-1"     -> ("usb1", 1)      # device directly on a root hub
    """
    if "." in usbdev:
        hub, _, port = usbdev.rpartition(".")
        return hub, int(port) if port.isdigit() else None
    bus, _, port = usbdev.partition("-")
    return f"usb{bus}", int(port) if port.isdigit() else None


def resolve_hub_port(dev_path: str):
    """Resolve a serial device path to its ``(hub_location, port)`` via sysfs.

    Follows by-id symlinks and the ``/sys/class/tty`` device link. Returns
    ``(None, None)`` if the topology can't be determined.
    """
    try:
        real = os.path.realpath(dev_path)
        name = os.path.basename(real)
        iface = os.path.realpath(os.path.join("/sys/class/tty", name, "device"))
        usbdev = os.path.basename(os.path.dirname(iface))  # e.g. "1-1.1"
        return parse_hub_port(usbdev)
    except OSError as exc:
        logger.debug("Could not resolve hub port for %s: %s", dev_path, exc)
        return None, None


def discover_roles_by_port(
    port_roles: Optional[Dict[int, str]] = None,
    candidates: Optional[Iterable[str]] = None,
    hub: Optional[str] = None,
    resolver: Optional[Callable[[str], tuple]] = None,
) -> Dict[str, str]:
    """Map roles to device paths purely from the USB hub port topology.

    ``port_roles`` maps a hub port number to a role (defaults to
    :data:`DEFAULT_PORT_ROLES`). ``hub`` optionally restricts matching to a
    single hub location (e.g. ``"1-1"``). ``resolver`` is injectable for tests.
    """
    if port_roles is None:
        port_roles = DEFAULT_PORT_ROLES
    if candidates is None:
        candidates = enumerate_candidates()
    resolve = resolver or resolve_hub_port

    found: Dict[str, str] = {}
    for path in candidates:
        hub_loc, port = resolve(path)
        if port is None:
            continue
        if hub is not None and hub_loc != hub:
            continue
        role = port_roles.get(port)
        if role and role not in found:
            found[role] = path
            logger.info("Port mapping: %s on hub %s port %s -> %s",
                        path, hub_loc, port, role)
    return found


def classify_lines(lines: Iterable[str]) -> Optional[str]:
    """Return the device role implied by a batch of NDJSON lines, or ``None``.

    Only the SYSTEM channel (``id == 0``) is authoritative. We accept an explicit
    ``role`` field first, then fall back to the ``*_READY`` boot banner. Non-JSON
    or non-system lines (telemetry, CAN frames, logs) are ignored.
    """
    for line in lines:
        if not line:
            continue
        text = line.strip()
        if not text or text[0] != "{":
            continue
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict) or obj.get("id") != 0:
            continue
        data = obj.get("d")
        if not isinstance(data, dict):
            continue
        role = data.get("role")
        if isinstance(role, str) and role.lower() in KNOWN_ROLES:
            return role.lower()
        msg = data.get("msg")
        if isinstance(msg, str):
            mapped = _BANNER_ROLES.get(msg.strip().upper())
            if mapped:
                return mapped
    return None


def enumerate_candidates() -> List[str]:
    """List candidate serial device paths, preferring stable by-id symlinks.

    ``/dev/serial/by-id/*-if00`` symlinks embed the board serial number and are
    stable across ``ttyACM`` renumbering, so binding a port to one of them lets
    the reconnect loop survive a re-plug that lands on a different ``ttyACM``.
    Falls back to raw ``/dev/ttyACM*`` when by-id is unavailable.
    """
    byid = sorted(glob.glob("/dev/serial/by-id/*-if00"))
    if byid:
        return byid
    return sorted(glob.glob("/dev/ttyACM*"))


def _read_lines_pyserial(
    path: str,
    baudrate: int,
    probe_timeout: float,
    send_whoami: bool,
) -> List[str]:
    """Open ``path`` briefly, optionally send whoami, and collect NDJSON lines."""
    try:
        import serial  # local import: keeps pyserial optional for unit tests
    except ImportError:  # pragma: no cover - exercised only without pyserial
        logger.warning("pyserial not available; cannot probe %s", path)
        return []

    lines: List[str] = []
    ser = None
    try:
        ser = serial.Serial(port=path, baudrate=baudrate, timeout=0.2)
        deadline = time.monotonic() + probe_timeout
        next_whoami = 0.0
        while time.monotonic() < deadline:
            # Re-send whoami periodically: a device that is already running (not
            # freshly booted) only identifies on request, and a single request
            # can be missed if it lands between the firmware's stdin polls.
            if send_whoami and time.monotonic() >= next_whoami:
                try:
                    ser.write(WHOAMI_REQUEST.encode("ascii"))
                    ser.flush()
                except Exception:  # noqa: BLE001 - probe is best-effort
                    pass
                next_whoami = time.monotonic() + 0.5
            raw = ser.readline()
            if not raw:
                continue
            lines.append(raw.decode("utf-8", errors="ignore"))
            # Early-out as soon as the lines are conclusive.
            if classify_lines(lines) is not None:
                break
    except Exception as exc:  # noqa: BLE001 - absent/busy device is expected
        logger.debug("Probe of %s failed: %s", path, exc)
    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                pass
    return lines


def identify_port(
    path: str,
    baudrate: int = DEFAULT_BAUDRATE,
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
    send_whoami: bool = True,
    reader: Optional[Callable[[str, int, float, bool], List[str]]] = None,
) -> Optional[str]:
    """Probe a single serial ``path`` and return its role (or ``None``).

    ``reader`` is injectable for testing; it must return the NDJSON lines read
    from the device given ``(path, baudrate, probe_timeout, send_whoami)``.
    """
    read = reader or _read_lines_pyserial
    lines = read(path, baudrate, probe_timeout, send_whoami)
    role = classify_lines(lines)
    if role:
        logger.info("Identified %s as %s", path, role)
    else:
        logger.debug("Could not identify %s", path)
    return role


def discover_roles(
    candidates: Optional[Iterable[str]] = None,
    baudrate: int = DEFAULT_BAUDRATE,
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
    skip: Iterable[str] = (),
    roles_wanted: Iterable[str] = KNOWN_ROLES,
    reader: Optional[Callable[[str, int, float, bool], List[str]]] = None,
) -> Dict[str, str]:
    """Probe candidate ports and return a ``{role: path}`` mapping.

    Ports in ``skip`` (e.g. ones already held open by a running port) are not
    probed. Stops early once every role in ``roles_wanted`` is found.
    """
    if candidates is None:
        candidates = enumerate_candidates()
    skip_set = set(skip)
    wanted = set(roles_wanted)
    found: Dict[str, str] = {}
    for path in candidates:
        if path in skip_set:
            continue
        if wanted and wanted.issubset(found.keys()):
            break
        role = identify_port(
            path, baudrate=baudrate, probe_timeout=probe_timeout, reader=reader
        )
        if role and role not in found:
            found[role] = path
    if found:
        logger.info("Serial role discovery: %s", found)
    else:
        logger.warning("Serial role discovery found no identifiable devices")
    return found


def discover_roles_combined(
    port_roles: Optional[Dict[int, str]] = None,
    hub: Optional[str] = None,
    candidates: Optional[Iterable[str]] = None,
    baudrate: int = DEFAULT_BAUDRATE,
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
    skip: Iterable[str] = (),
    roles_wanted: Iterable[str] = KNOWN_ROLES,
    use_whoami_fallback: bool = True,
) -> Dict[str, str]:
    """Resolve roles by hub port first, then fall back to whoami probing.

    Port-topology mapping is deterministic and works even for a silent/wedged
    device, so it is tried first. Any roles still missing afterwards are probed
    with ``whoami`` (e.g. if a device is on an unmapped port). Ports in ``skip``
    are never probed.
    """
    if candidates is None:
        candidates = enumerate_candidates()
    candidates = list(candidates)

    found = discover_roles_by_port(
        port_roles=port_roles, candidates=candidates, hub=hub
    )

    wanted = set(roles_wanted)
    if use_whoami_fallback and not wanted.issubset(found.keys()):
        # Probe only the not-yet-mapped, not-skipped candidates.
        mapped_paths = set(found.values())
        probe_candidates = [
            p for p in candidates if p not in mapped_paths and p not in set(skip)
        ]
        extra = discover_roles(
            candidates=probe_candidates,
            baudrate=baudrate,
            probe_timeout=probe_timeout,
            skip=skip,
            roles_wanted=wanted - set(found.keys()),
        )
        for role, path in extra.items():
            found.setdefault(role, path)

    if found:
        logger.info("Role discovery (port+whoami): %s", found)
    else:
        logger.warning("Role discovery found no devices")
    return found


def realpath(path: str) -> str:
    """Resolve a by-id symlink to its current ``/dev/ttyACM*`` target."""
    try:
        return os.path.realpath(path)
    except OSError:
        return path
