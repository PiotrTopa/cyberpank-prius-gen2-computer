"""
Powerbox integration — computer side of the second RP2040 ("powerbox").

The powerbox is the power-management board between the Prius 12 V system and the
POCO board computer. Over its own USB-CDC NDJSON link (merged into the ingress by
:class:`MultiInputPort` with an id offset of :data:`DEVICE_POWERBOX_BASE`) it
reports:

    device 200 (DEVICE_POWERBOX_BASE)   system / status / command acks
    device 201 (DEVICE_POWERBOX_POWER)  INA219 telemetry: voltage / current / power
    device 202 (DEVICE_POWERBOX_EVENTS) ignition (ACC/stacyjka) + constant battery line

This module provides:

    * :func:`parse_powerbox_message` — decode a raw powerbox payload to Actions;
    * :func:`register_powerbox_ingress` — wire those parsers into an IngressController;
    * :func:`build_power_off_command` — the command the computer sends back to the
      powerbox to cut POCO power;
    * :class:`PowerboxCommander` — sends powerbox commands over an OutputPort;
    * :class:`PriusPowerController` — applies the POCO power profile (full/low) by
      writing the ``/etc/prius/power-mode`` flag the ``prius-power`` unit watches.

The powerbox firmware/protocol is still being built; the wire formats here are the
computer's expectation and are tolerant of key/unit variants so the firmware can
settle without breaking ingest. No transport is required for the parsers/rules to
be unit-tested.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, List, Optional

from .ports import (
    DEVICE_POWERBOX_BASE,
    DEVICE_POWERBOX_EVENTS,
    DEVICE_POWERBOX_POWER,
    OutgoingCommand,
)
from ..state.actions import (
    Action,
    SetPowerboxConnectionAction,
    SetPowerboxIgnitionAction,
    SetPowerboxTelemetryAction,
)

if TYPE_CHECKING:  # avoid import cycles at runtime
    from .ingress import IngressController
    from .ports import OutputPort

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Inbound: raw powerbox payload -> Actions
# ─────────────────────────────────────────────────────────────────────────────

def _coerce_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "on", "yes"):
            return True
        if low in ("0", "false", "off", "no"):
            return False
    return None


def parse_power_telemetry(data: dict) -> List[Action]:
    """Decode device 201 INA219 telemetry.

    Accepted keys (SI units preferred, milli-units tolerated):
        v / volt / voltage     bus voltage in volts   (or mv / mvolt in millivolts)
        i / cur / current      current in amps         (or ma / mcur in milliamps)
        p / pwr / power         power in watts          (or mw / mpwr in milliwatts)
    """
    voltage = _coerce_float(data.get("v", data.get("voltage", data.get("volt"))))
    if voltage is None:
        mv = _coerce_float(data.get("mv", data.get("mvolt")))
        if mv is not None:
            voltage = mv / 1000.0

    current = _coerce_float(data.get("i", data.get("current", data.get("cur"))))
    if current is None:
        ma = _coerce_float(data.get("ma", data.get("mcur")))
        if ma is not None:
            current = ma / 1000.0

    power = _coerce_float(data.get("p", data.get("power", data.get("pwr"))))
    if power is None:
        mw = _coerce_float(data.get("mw", data.get("mpwr")))
        if mw is not None:
            power = mw / 1000.0

    if voltage is None and current is None and power is None:
        return []
    return [SetPowerboxTelemetryAction(voltage=voltage, current=current, power=power)]


def parse_power_event(data: dict) -> List[Action]:
    """Decode device 202 ignition / power events.

    Accepted forms:
        {"acc": true, "batt": true}
        {"event": "ignition_on"}  / "ignition_off" / "acc_on" / "acc_off"
    """
    acc = _coerce_bool(data.get("acc", data.get(" acc")))
    batt = _coerce_bool(data.get("batt", data.get("battery")))

    event = data.get("event")
    if isinstance(event, str):
        ev = event.strip().lower()
        if ev in ("ignition_on", "acc_on", "on"):
            acc = True
        elif ev in ("ignition_off", "acc_off", "off"):
            acc = False
        elif ev in ("battery_lost", "batt_off"):
            batt = False
        elif ev in ("battery_present", "batt_on"):
            batt = True

    if acc is None and batt is None:
        return []
    return [
        SetPowerboxIgnitionAction(
            acc_on=bool(acc) if acc is not None else False,
            batt_present=bool(batt) if batt is not None else True,
        )
    ]


def parse_powerbox_system(data: dict) -> List[Action]:
    """Decode device 200 system/status messages (ready banner, acks)."""
    msg = str(data.get("msg", "")).upper()
    if "POWERBOX_READY" in msg or "READY" in msg:
        ver = data.get("ver", "unknown")
        logger.info("Powerbox ready: v%s", ver)
        return [SetPowerboxConnectionAction(connected=True)]

    ack = data.get("ack")
    if ack:
        logger.info("Powerbox ack: %s", ack)
    return []


def parse_powerbox_message(device_id: int, data: dict) -> List[Action]:
    """Route a powerbox payload to the right parser by (offset) device id."""
    if device_id == DEVICE_POWERBOX_POWER:
        return parse_power_telemetry(data)
    if device_id == DEVICE_POWERBOX_EVENTS:
        return parse_power_event(data)
    if device_id == DEVICE_POWERBOX_BASE:
        return parse_powerbox_system(data)
    return []


def register_powerbox_ingress(ingress: "IngressController") -> None:
    """Register satellite handlers for the powerbox device id range."""
    ingress.register_satellite_handler(
        DEVICE_POWERBOX_BASE, lambda d: parse_powerbox_message(DEVICE_POWERBOX_BASE, d)
    )
    ingress.register_satellite_handler(
        DEVICE_POWERBOX_POWER, lambda d: parse_powerbox_message(DEVICE_POWERBOX_POWER, d)
    )
    ingress.register_satellite_handler(
        DEVICE_POWERBOX_EVENTS, lambda d: parse_powerbox_message(DEVICE_POWERBOX_EVENTS, d)
    )
    logger.info("Powerbox ingress handlers registered (devices %d-%d)",
                DEVICE_POWERBOX_BASE, DEVICE_POWERBOX_EVENTS)


# ─────────────────────────────────────────────────────────────────────────────
# Outbound: computer -> powerbox commands
# ─────────────────────────────────────────────────────────────────────────────

def build_power_off_command(reason: str = "undervoltage", grace_s: int = 30) -> OutgoingCommand:
    """Command the powerbox to cut POCO power after a grace period.

    ``grace_s`` gives the OS time to shut down cleanly before the rail is cut.
    The device id is in the reserved powerbox range; a powerbox OutputPort is
    expected to translate it back to the powerbox's local id 0.
    """
    return OutgoingCommand(
        device_id=DEVICE_POWERBOX_BASE,
        command_type="power",
        payload={"a": "off", "reason": reason, "grace_s": int(grace_s)},
        priority=100,
    )


class PowerboxCommander:
    """Send commands to the powerbox over an OutputPort (None = log only)."""

    def __init__(self, output_port: "Optional[OutputPort]" = None) -> None:
        self._output_port = output_port

    def request_power_off(self, reason: str = "undervoltage", grace_s: int = 30) -> bool:
        cmd = build_power_off_command(reason=reason, grace_s=grace_s)
        if self._output_port is None:
            logger.warning(
                "Powerbox power-off requested (reason=%s, grace=%ds) but no powerbox "
                "output port is wired — command not sent", reason, grace_s,
            )
            return False
        try:
            ok = self._output_port.send(cmd)
            logger.warning("Sent powerbox power-off (reason=%s, grace=%ds): ok=%s",
                           reason, grace_s, ok)
            return bool(ok)
        except Exception:
            logger.exception("Failed to send powerbox power-off command")
            return False


# ─────────────────────────────────────────────────────────────────────────────
# POCO power profile control (writes the prius-power flag file)
# ─────────────────────────────────────────────────────────────────────────────

class PriusPowerController:
    """Apply the POCO CPU power profile by writing ``/etc/prius/power-mode``.

    The ``prius-power.path`` systemd unit watches this flag and re-applies the
    profile (``full`` = all cores, ``low`` = Gold cluster offline + capped). On a
    dev machine the path is not writable; that is logged and treated as a no-op so
    the same code runs everywhere.
    """

    FULL = "full"
    LOW = "low"

    def __init__(self, flag_path: str = "/etc/prius/power-mode") -> None:
        self._flag_path = flag_path
        self._last_written: Optional[str] = None

    @property
    def current(self) -> Optional[str]:
        return self._last_written

    def set_mode(self, mode: str) -> bool:
        """Write the power-mode flag if it changed. Returns True on write."""
        mode = self.FULL if mode == self.FULL else self.LOW
        if mode == self._last_written:
            return False
        try:
            with open(self._flag_path, "w", encoding="ascii") as fh:
                fh.write(mode + "\n")
            self._last_written = mode
            logger.info("POCO power profile -> %s (%s)", mode, self._flag_path)
            return True
        except OSError as exc:
            # Remember intent so we do not spam the log every tick on dev hosts.
            self._last_written = mode
            logger.info("POCO power profile -> %s (no-op: %s)", mode, exc)
            return False

    def set_for_ignition(self, acc_on: bool) -> bool:
        """ACC/ON -> full performance; key off -> low idle."""
        return self.set_mode(self.FULL if acc_on else self.LOW)
