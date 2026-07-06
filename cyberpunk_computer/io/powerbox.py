"""
Powerbox integration — computer side of the second RP2040 ("powerbox").

The powerbox is the power-management board between the Prius 12 V system and the
POCO board computer. Over its own USB-CDC NDJSON link (merged into the ingress by
:class:`MultiInputPort` with an id offset of :data:`DEVICE_POWERBOX_BASE`) it
reports:

    device 200 (DEVICE_POWERBOX_BASE)   system / status / command acks
    device 201 (DEVICE_POWERBOX_POWER)  INA219 telemetry: voltage / current / power
    device 202 (DEVICE_POWERBOX_EVENTS) ignition (ACC) + constant battery line

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
    SetPowerboxPowerStatusAction,
    SetPowerboxTelemetryAction,
)

if TYPE_CHECKING:  # avoid import cycles at runtime
    from .ingress import IngressController
    from .ports import OutputPort

logger = logging.getLogger(__name__)

# Last (role, version) we logged at INFO for the powerbox identity. The firmware
# re-announces IDENT every IDENT_HEARTBEAT_MS (~10 s) so the backend can identify
# a device that connected mid-stream; without de-duping we would log "Powerbox
# identified" at INFO every 10 s and bury real events (watchdog/recovery). We log
# at INFO only on first-seen or when role/version changes, DEBUG otherwise.
_last_identity_logged: Optional[tuple] = None


def reset_identity_log() -> None:
    """Forget the last-logged powerbox identity.

    Called when the link is marked disconnected (staleness watchdog) so the next
    IDENT/READY after recovery logs at INFO again — a genuine "link came back"
    event — instead of being de-duped to DEBUG.
    """
    global _last_identity_logged
    _last_identity_logged = None


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
    """Decode device 201 INA219 and env telemetry.

    Accepted keys (SI units preferred, milli-units tolerated):
        v / volt / voltage     bus voltage in volts   (or mv / mvolt in millivolts)
        i / cur / current      current in amps         (or ma / mcur in milliamps)
        p / pwr / power         power in watts          (or mw / mpwr in milliwatts)
        bmp_t, bmp_p           BMP280 temperature (C), pressure (Pa)
        aht_t, aht_h           AHT20 temperature (C), relative humidity (%)
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

    bmp_t = _coerce_float(data.get("bmp_t"))
    bmp_p = _coerce_float(data.get("bmp_p"))
    aht_t = _coerce_float(data.get("aht_t"))
    aht_h = _coerce_float(data.get("aht_h"))
    energy_mah = _coerce_float(data.get("mah"))

    if voltage is None and current is None and power is None and bmp_t is None and aht_t is None and energy_mah is None:
        return []
    return [SetPowerboxTelemetryAction(
        voltage=voltage, current=current, power=power,
        bmp_t=bmp_t, bmp_p=bmp_p, aht_t=aht_t, aht_h=aht_h, energy_mah=energy_mah
    )]


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
    """Decode device 200 system/status messages (ready banner, IDENT, STATUS, acks)."""
    msg = str(data.get("msg", "")).upper()
    if "POWERBOX_READY" in msg or "IDENT" in msg or "READY" in msg:
        ver = data.get("ver", "unknown")
        role = data.get("role", "powerbox")
        is_boot = "POWERBOX_READY" in msg  # genuine boot banner (firmware (re)booted)
        kind = "ready" if is_boot else ("identified" if "IDENT" in msg else "ready")
        # Edge-triggered logging. A POWERBOX_READY boot banner always logs at INFO
        # (it marks a fresh firmware boot — a meaningful event). The routine ~10 s
        # IDENT re-announce only logs at INFO on first-seen or role/version change,
        # DEBUG otherwise, so it does not bury watchdog/recovery events.
        global _last_identity_logged
        identity = (role, ver)
        if is_boot or identity != _last_identity_logged:
            _last_identity_logged = identity
            logger.info("Powerbox %s: role=%s v%s", kind, role, ver)
        else:
            logger.debug("Powerbox %s (re-announce): role=%s v%s", kind, role, ver)
        return [SetPowerboxConnectionAction(connected=True)]

    if msg == "STATUS":
        # Periodic power-management heartbeat: OUT rail states + POCO liveness +
        # rolling counter + state machine. Any missing key stays None (no update).
        def _b(key):
            return _coerce_bool(data[key]) if key in data else None
        hb = data.get("hb")
        try:
            hb = int(hb) if hb is not None else None
        except (TypeError, ValueError):
            hb = None
        pm = data.get("pm")
        return [SetPowerboxPowerStatusAction(
            out1=_b("out1"), out2=_b("out2"), out3=_b("out3"),
            poco_alive=_b("poco"),
            pm_state=str(pm) if pm is not None else None,
            hb=hb,
        )]

    if msg in ("SHUTDOWN", "SUICIDE"):
        # Powerbox is tearing down power. Reflect the state machine so the
        # dashboard/operator can see it; the rail states follow in STATUS.
        logger.warning("Powerbox %s: %s", msg, data.get("reason", ""))
        return [SetPowerboxPowerStatusAction(pm_state=msg.lower())]

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

# The powerbox listens on its LOCAL system channel (id 0). Commands are sent on
# the powerbox's own OutputPort, so the wire id is the powerbox-local id — NOT the
# computer-side DEVICE_POWERBOX_BASE offset (that offset only applies to ingress,
# where MultiInputPort maps the powerbox's id 0 -> 200).
POWERBOX_LOCAL_SYSTEM = 0


def build_power_off_command(reason: str = "undervoltage", grace_s: int = 30) -> OutgoingCommand:
    """Command the powerbox to shut down (and ultimately suicide) POCO power.

    ``grace_s`` gives the OS time to shut down cleanly before the powerbox drops
    the master rail (OUT1).
    """
    return OutgoingCommand(
        device_id=POWERBOX_LOCAL_SYSTEM,
        command_type="power",
        payload={"a": "off", "reason": reason, "grace_s": int(grace_s)},
        priority=100,
    )


def build_heartbeat_command(counter: int) -> OutgoingCommand:
    """Send the POCO->powerbox heartbeat with a rolling counter (0-255)."""
    return OutgoingCommand(
        device_id=POWERBOX_LOCAL_SYSTEM,
        command_type="power",
        payload={"a": "hb", "n": int(counter) & 0xFF},
        priority=10,
    )


def build_out_command(channel: int, on: bool) -> OutgoingCommand:
    """Set a controllable rail: OUT2 (RS485 satellites) or OUT3 (spare).

    OUT1 (master rail) is intentionally not controllable this way — it is only
    dropped by the powerbox's own shutdown/suicide path.
    """
    return OutgoingCommand(
        device_id=POWERBOX_LOCAL_SYSTEM,
        command_type="power",
        payload={"a": "out", "ch": int(channel), "on": bool(on)},
        priority=50,
    )


def build_button_command(ms: int = 3000) -> OutgoingCommand:
    """Pulse the POCO power button: ~3000 ms = power on, ~10000 ms = force reboot."""
    return OutgoingCommand(
        device_id=POWERBOX_LOCAL_SYSTEM,
        command_type="power",
        payload={"a": "button", "ms": int(ms)},
        priority=60,
    )


def build_fan_command(pin: int, duty: int, freq: int = 25000) -> OutgoingCommand:
    """Set the chassis fan PWM duty cycle.

    ``pin`` is the RP2040 GPIO driving the fan MOSFET (14 for the chassis fan).
    ``duty`` is the raw duty_u16 value (0-65535).
    """
    return OutgoingCommand(
        device_id=POWERBOX_LOCAL_SYSTEM,
        command_type="power",
        payload={"a": "fan", "pin": int(pin), "duty": int(duty), "freq": int(freq)},
        priority=20,
    )


class PowerboxCommander:
    """Send commands to the powerbox over an OutputPort (None = log only)."""

    def __init__(self, output_port: "Optional[OutputPort]" = None) -> None:
        self._output_port = output_port

    def _send(self, cmd: OutgoingCommand, what: str, warn: bool = False) -> bool:
        if self._output_port is None:
            log = logger.warning if warn else logger.debug
            log("Powerbox %s requested but no output port is wired — not sent", what)
            return False
        try:
            ok = self._output_port.send(cmd)
            (logger.warning if warn else logger.debug)("Sent powerbox %s: ok=%s", what, ok)
            return bool(ok)
        except Exception:
            logger.exception("Failed to send powerbox %s", what)
            return False

    def request_power_off(self, reason: str = "undervoltage", grace_s: int = 30) -> bool:
        return self._send(
            build_power_off_command(reason=reason, grace_s=grace_s),
            "power-off (reason=%s, grace=%ds)" % (reason, grace_s),
            warn=True,
        )

    def send_heartbeat(self, counter: int) -> bool:
        return self._send(build_heartbeat_command(counter), "heartbeat")

    def set_out(self, channel: int, on: bool) -> bool:
        return self._send(build_out_command(channel, on),
                          "out%d=%s" % (channel, "on" if on else "off"), warn=True)

    def press_button(self, ms: int = 3000) -> bool:
        return self._send(build_button_command(ms), "power-button %dms" % ms, warn=True)

    def set_fan(self, pin: int, duty: int, freq: int = 25000) -> bool:
        return self._send(build_fan_command(pin, duty, freq),
                          "fan pin%d duty=%d freq=%d" % (pin, duty, freq))


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
