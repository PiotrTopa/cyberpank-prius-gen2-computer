"""
USB hub port power control — one abstraction over the two very different ways
this car powers a hub socket on and off.

Topology (ActionStar 2101:8500 hub ``1-1``; relay numbers per the 2026-11-03
board, powerbox/RELAY-CHANGE-PLAN.md: board relay N = firmware channel N):

    socket 1 -> internal port 5 : POWERBOX   — the only socket with real
                native per-port VBUS switching (uhubctl PPPS)
    socket 2 -> internal port 2 : GATEWAY    — 12 V via relay ch3 = the CAN
                hub that carries the gateway AND the comma.ai panda (one port)
    socket 3 -> internal port 3 : MFD PI     — 12 V via relay ch2
    socket 4 -> internal port 4 : CAMERA HUB — 12 V via relay ch4 (VIA hub,
                fvision dashcam / aux devices)
    relay ch1 (NC) is the powerbox's OWN supply = hard restart; never a port.
    relay ch5 is the RS485 satellite rail (state/rules/satellite_power.py).

The internal data-port numbers above are the pre-rework socket positions;
verify them with ``uhubctl`` after the rewiring and override via
PortPowerConfig if a device moved sockets.

Two controller flavours:

* :class:`UhubctlPortPower` — native per-port power switching (PPPS) through
  ``uhubctl``. Only the powerbox's socket supports this on the current hub.

* :class:`RelayPortPower` — VBUS switched by a relay driven by the powerbox
  firmware (PCF8574 expander, NDJSON ``{"a":"relay","ch":N,"on":b}``). Two
  hard-won rules are encoded here:

  1. **VBUS-off first, data-off right after the disconnect; VBUS-on before
     data-on.** A device left data-connected on an unpowered socket back-feeds
     through its ESD diodes and destabilises the ENTIRE hub (EMI port-disables,
     -32/-71 enumeration storms — diagnosed 2026-08-01), so the hub port is
     administratively disabled (``uhubctl -a off`` = data-only disconnect on
     this hub) whenever its VBUS is off, and re-enabled only after VBUS is
     back. The ORDER of the off sequence was reversed on 2026-11-04: disabling
     the port *before* cutting VBUS hides the disconnect from the host — the
     parent hub reports nothing for a disabled port, the kernel only gives up
     after ~10 s of ``clear tt`` errors (321 of them in one key-off) and on
     that timeout re-enables the port, so the data-off had to be applied a
     second time by ``enforce()``. Cutting VBUS first gives a clean disconnect
     within a second; ``tick()`` then disables the port as soon as the device
     is gone from sysfs (``data_off_max_wait_s`` cap). ON enables the port
     first and then closes the relay, so the hub is ready when VBUS arrives.

  2. **Commands are lossy; state must be verified.** The powerbox CDC link can
     silently swallow a command (consumed, never acked). Controllers therefore
     only record the *desired* state here; ``enforce()`` compares it against
     the powerbox's mirrored ``rly`` telemetry (STATUS heartbeat) and re-issues
     the sequence until reality matches. The backend calls ``enforce()`` on a
     slow tick, which also self-heals after a hub reset (which cold-boots the
     powerbox and releases every relay).

All external effects are injectable for tests.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from ..state.app_state import (
    RELAY_CH_CAM_HUB,
    RELAY_CH_CAN_HUB,
    RELAY_CH_MFD,
    RELAY_CH_RESTART,
)

logger = logging.getLogger(__name__)


@dataclass
class PortPowerConfig:
    """Static description of the hub power topology (defaults = current car)."""
    hub: str = "1-1"                # hub location for uhubctl / sysfs
    powerbox_port: int = 5          # native-PPS internal port (powerbox socket)
    gateway_relay_ch: int = RELAY_CH_CAN_HUB   # CAN hub: gateway + panda
    gateway_data_port: int = 2      # internal hub port carrying the gateway
    mfd_relay_ch: int = RELAY_CH_MFD
    mfd_data_port: int = 3
    camera_relay_ch: int = RELAY_CH_CAM_HUB    # VIA hub: dashcam / aux
    camera_data_port: int = 4
    restart_relay_ch: int = RELAY_CH_RESTART   # never a port (guarded)


def _default_runner(cmd, timeout=10):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _sysfs_device_present(hub: str, port: int) -> Optional[bool]:
    """Is a device enumerated under hub port ``<hub>.<port>`` (sysfs)?"""
    try:
        return os.path.exists("/sys/bus/usb/devices/%s.%d" % (hub, int(port)))
    except Exception:
        return None


def parse_uhubctl_port_power(output: str, hub: str, port: int) -> Optional[bool]:
    """Extract one port's power bit from ``uhubctl`` output.

    A powered port's status word has the PORT_POWER bit (``01xx``); a cut port
    reads ``0000``. Returns None when the hub/port is not in the output.
    """
    hub_section = False
    for line in output.splitlines():
        if line.startswith("Current status for hub"):
            hub_section = f" {hub} " in line or f"hub {hub}" in line
            continue
        if hub_section:
            stripped = line.strip()
            if stripped.startswith(f"Port {port}:"):
                rest = stripped.split(":", 1)[1].strip()
                word = rest.split()[0] if rest else ""
                try:
                    return bool(int(word, 16) & 0x0100)
                except ValueError:
                    low = rest.lower()
                    if "power" in low:
                        return True
                    if "off" in low:
                        return False
                    return None
    return None


class UhubctlPortPower:
    """Native per-port power switching (PPPS) via uhubctl.

    Used for the powerbox's socket (internal port 5) — the one socket whose
    VBUS the hub can really cut. ``set``/``read``/``enforce`` share the same
    surface as :class:`RelayPortPower` so callers don't care which they hold.
    """

    def __init__(self, hub: str, port: int,
                 runner: Optional[Callable] = None) -> None:
        self.hub = hub
        self.port = port
        self._run = runner or _default_runner
        self._desired: Optional[bool] = None

    @property
    def desired(self) -> Optional[bool]:
        return self._desired

    def set(self, on: bool) -> bool:
        self._desired = bool(on)
        return self._apply(on)

    def _apply(self, on: bool) -> bool:
        try:
            r = self._run(["sudo", "-n", "uhubctl", "-f", "-l", self.hub,
                           "-p", str(self.port), "-a", "on" if on else "off"])
            ok = r.returncode == 0
            logger.info("uhubctl port %s %s -> %s (%s)", self.hub, self.port,
                        "ON" if on else "OFF", "ok" if ok else "FAILED")
            return ok
        except Exception:
            logger.exception("uhubctl port power %s failed", "on" if on else "off")
            return False

    def read(self) -> Optional[bool]:
        try:
            r = self._run(["sudo", "-n", "uhubctl", "-f"])
            if r.returncode != 0:
                return None
            return parse_uhubctl_port_power(r.stdout, self.hub, self.port)
        except Exception:
            logger.debug("uhubctl read failed", exc_info=True)
            return None

    def enforce(self) -> None:
        if self._desired is None:
            return
        actual = self.read()
        if actual is None or actual == self._desired:
            return
        logger.warning("Port %s/%s power drift: desired=%s actual=%s — re-applying",
                       self.hub, self.port, self._desired, actual)
        self._apply(self._desired)


class RelayPortPower:
    """VBUS via a powerbox relay + hub data-port sequencing + verification.

    ``send_relay(ch, on)`` transmits the (lossy) powerbox relay command.
    ``get_relays()`` returns the mirrored ``rly`` array from the powerbox
    STATUS heartbeat (``None`` when unknown / link down) — the ground truth
    ``enforce()`` verifies against.
    """

    def __init__(
        self,
        name: str,
        relay_ch: int,
        hub: str,
        data_port: int,
        send_relay: Callable[[int, bool], bool],
        get_relays: Callable[[], Optional[Sequence]],
        runner: Optional[Callable] = None,
        enforce_min_interval_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        telemetry_fresh: Optional[Callable[[], bool]] = None,
        device_present: Optional[Callable[[], Optional[bool]]] = None,
        data_off_max_wait_s: float = 3.0,
    ) -> None:
        if int(relay_ch) == RELAY_CH_RESTART:
            raise ValueError(
                "relay ch%d is the powerbox's own restart relay and cannot back a "
                "port (%s)" % (relay_ch, name))
        self.name = name
        self.relay_ch = int(relay_ch)
        self.hub = hub
        self.data_port = data_port
        self._send_relay = send_relay
        self._get_relays = get_relays
        self._run = runner or _default_runner
        self._desired: Optional[bool] = None
        self._enforce_min_s = enforce_min_interval_s
        self._clock = clock
        self._last_apply = 0.0
        # Is the mirrored ``rly`` telemetry currently trustworthy? When the
        # powerbox USB-CDC link wedges, the mirror freezes at its last value and
        # enforce() would otherwise read it as ground truth forever (see
        # enforce()). Defaults to "always fresh" so callers that don't wire a
        # link-health probe keep the previous behaviour.
        self._telemetry_fresh = telemetry_fresh or (lambda: True)
        self._stale_logged = False
        # OFF is two-step: VBUS off now, data-off once the device has dropped
        # off the bus (tick()). ``_data_off_due`` is the deadline of a pending
        # data-off (None = nothing pending).
        self._device_present = device_present or (
            lambda: _sysfs_device_present(hub, data_port))
        self._data_off_max_wait_s = float(data_off_max_wait_s)
        self._data_off_due: Optional[float] = None
        self._data_off_since: Optional[float] = None

    @property
    def desired(self) -> Optional[bool]:
        return self._desired

    def _data(self, enable: bool) -> None:
        """Administratively enable/disable the hub data port (uhubctl).

        On this hub, ``-a off`` for a non-PPS port is a DATA-ONLY disconnect —
        exactly what neutralises the unpowered-device bus poisoning.
        """
        try:
            self._run(["sudo", "-n", "uhubctl", "-f", "-l", self.hub,
                       "-p", str(self.data_port), "-a", "on" if enable else "off"])
            logger.info("Port %s/%s data %s (%s)", self.hub, self.data_port,
                        "enabled" if enable else "disabled", self.name)
        except Exception:
            logger.exception("uhubctl data toggle failed (%s)", self.name)

    def set(self, on: bool) -> bool:
        """Record desired state and apply the full sequence once.

        The lossy link means this single attempt may silently fail; the caller
        keeps ``enforce()`` running on a tick to converge.
        """
        self._desired = bool(on)
        return self._apply(on)

    def _apply(self, on: bool) -> bool:
        self._last_apply = self._clock()
        if on:
            self._data_off_due = None
            # Port enabled BEFORE the coil closes: the hub is ready when VBUS
            # arrives (the relay command lands ~1 s later over the CDC link).
            self._data(True)
            ok = self._send_relay(self.relay_ch, True)
        else:
            ok = self._send_relay(self.relay_ch, False)
            # Data-off follows the disconnect (tick()); re-arm if pending.
            self._data_off_since = self._last_apply
            self._data_off_due = self._last_apply + self._data_off_max_wait_s
        logger.info("Port power %s (relay ch%d) -> %s (sent=%s)",
                    self.name, self.relay_ch, "ON" if on else "OFF", ok)
        return ok

    @property
    def off_pending(self) -> bool:
        """True between the VBUS-off command and the data-off that follows."""
        return self._data_off_due is not None

    def tick(self) -> None:
        """Finish a pending OFF: disable the hub data port once the device has
        dropped off the bus, or after ``data_off_max_wait_s``. Cheap (one
        sysfs stat while pending, nothing otherwise) — call on every tick."""
        if self._data_off_due is None:
            return
        now = self._clock()
        present: Optional[bool] = None
        try:
            present = self._device_present()
        except Exception:
            logger.debug("device-present probe failed (%s)", self.name,
                         exc_info=True)
        if present is False or now >= self._data_off_due:
            self._data_off_due = None
            since = self._data_off_since if self._data_off_since is not None else now
            if present is False:
                why = "device gone after %.1fs" % (now - since)
            else:
                why = ("no disconnect seen within %.1fs"
                       % self._data_off_max_wait_s)
            logger.info("Port %s: finishing OFF (%s)", self.name, why)
            self._data(False)

    def read(self) -> Optional[bool]:
        """Actual VBUS state from the mirrored powerbox ``rly`` telemetry."""
        relays = self._get_relays()
        if not relays:
            return None
        idx = self.relay_ch - 1
        if idx < 0 or idx >= len(relays):
            return None
        val = relays[idx]
        return None if val is None else bool(val)

    def read_data_state(self) -> Optional[bool]:
        """Actual hub data-port state (enabled/disabled) via uhubctl."""
        try:
            r = self._run(["sudo", "-n", "uhubctl", "-f"])
            if r.returncode != 0:
                return None
            return parse_uhubctl_port_power(r.stdout, self.hub, self.data_port)
        except Exception:
            logger.debug("uhubctl data-state read failed (%s)", self.name,
                         exc_info=True)
            return None

    def enforce(self) -> None:
        """Re-apply the desired state when reality disagrees.

        Converges BOTH halves of the sequence:

        * VBUS (relay) drift — verified against the powerbox "rly" telemetry.
          Covers lost relay commands and hub resets that cold-boot the powerbox
          (PCF wakes with every coil released).
        * Data-port drift — verified against uhubctl. A hub reset silently
          re-enables every port; for a port whose VBUS is OFF that recreates
          the unpowered-zombie bus poisoning, so the data-off MUST be
          re-applied (diagnosed the hard way 2026-08-01: zombie gateway →
          poisoning → more hub resets → more zombies).

        VBUS drift is only actionable while the powerbox telemetry is FRESH.
        A wedged USB-CDC link freezes the ``rly`` mirror at its last value
        (typically all-off after a cold boot) while commands still go out fine,
        so an ungated enforce() re-issues relay commands forever against a
        mirror that can never update — audible relay chatter, coil inrush on the
        5 V rail the hub shares, and an enumeration storm that keeps the link
        wedged. That self-sustaining oscillation cost an afternoon on
        2026-08-31; when we cannot verify, we do not re-issue.
        """
        if self._desired is None:
            return
        self.tick()
        if (self._clock() - self._last_apply) < self._enforce_min_s:
            return  # give the last attempt time to land / telemetry to update
        fresh = True
        try:
            fresh = bool(self._telemetry_fresh())
        except Exception:  # a broken probe must not disable enforcement
            logger.debug("telemetry freshness probe failed (%s)", self.name,
                         exc_info=True)
        if not fresh and not self._stale_logged:
            self._stale_logged = True
            logger.warning(
                "Relay port %s: powerbox telemetry stale — suspending VBUS "
                "drift enforcement (mirror unverifiable; re-issuing would only "
                "churn the bus). Data-port enforcement continues.", self.name,
            )
        elif fresh and self._stale_logged:
            self._stale_logged = False
            logger.info("Relay port %s: telemetry fresh again — VBUS drift "
                        "enforcement resumed.", self.name)
        # Unverifiable mirror -> treat VBUS state as unknown rather than as truth.
        actual = self.read() if fresh else None
        vbus_drift = actual is not None and actual != self._desired
        data_drift = False
        if not vbus_drift and self._desired is False and self._data_off_due is None:
            data = self.read_data_state()
            data_drift = data is True  # data enabled on a port meant to be dark
        if not vbus_drift and not data_drift:
            return
        logger.warning(
            "Relay port %s drift (%s): desired=%s vbus=%s — re-applying sequence",
            self.name, "vbus" if vbus_drift else "data-port", self._desired, actual,
        )
        self._apply(self._desired)


@dataclass
class HubPortPower:
    """The car's full set of port power controllers, built from one config.

    ``gateway`` is the CAN hub (relay ch3): switching it switches the gateway
    AND the comma.ai panda together. ``camera`` is the VIA hub (relay ch4)
    with the dashcam. ``mfd`` is the Pi Zero (relay ch2).
    """
    powerbox: UhubctlPortPower
    gateway: RelayPortPower
    mfd: RelayPortPower
    camera: RelayPortPower

    @classmethod
    def build(
        cls,
        config: PortPowerConfig,
        send_relay: Callable[[int, bool], bool],
        get_relays: Callable[[], Optional[Sequence]],
        runner: Optional[Callable] = None,
        telemetry_fresh: Optional[Callable[[], bool]] = None,
    ) -> "HubPortPower":
        chans = (config.gateway_relay_ch, config.mfd_relay_ch, config.camera_relay_ch)
        if config.restart_relay_ch in chans:
            raise ValueError("PortPowerConfig assigns the powerbox restart relay "
                             "(ch%d) to a port" % config.restart_relay_ch)
        if len(set(chans)) != len(chans):
            raise ValueError("PortPowerConfig assigns one relay to two ports: %r" % (chans,))

        def relay(name: str, ch: int, data_port: int) -> RelayPortPower:
            return RelayPortPower(
                name, ch, config.hub, data_port,
                send_relay=send_relay, get_relays=get_relays, runner=runner,
                telemetry_fresh=telemetry_fresh,
            )
        return cls(
            powerbox=UhubctlPortPower(config.hub, config.powerbox_port, runner=runner),
            gateway=relay("gateway", config.gateway_relay_ch, config.gateway_data_port),
            mfd=relay("mfd", config.mfd_relay_ch, config.mfd_data_port),
            camera=relay("camera", config.camera_relay_ch, config.camera_data_port),
        )

    @property
    def relay_ports(self) -> tuple:
        return (self.gateway, self.mfd, self.camera)

    def tick_all(self) -> None:
        """Advance pending OFF sequences (data-off right after the disconnect).
        Cheap; the backend calls it every engine tick."""
        for c in self.relay_ports:
            try:
                c.tick()
            except Exception:
                logger.exception("port power tick failed (%s)", c.name)

    def enforce_all(self) -> None:
        for c in self.relay_ports:
            try:
                c.enforce()
            except Exception:
                logger.exception("Port power enforce failed (%s)", c.name)

    def reassert_all(self) -> int:
        """Re-send every recorded desired state unconditionally.

        Called after a powerbox boot banner: the PCF8574 woke at 0xFF, so
        every NO load is OFF regardless of what the (now stale) ``rly`` mirror
        said. Returns the number of ports re-applied.
        """
        n = 0
        for c in self.relay_ports:
            if c.desired is None:
                continue
            try:
                c._apply(c.desired)
                n += 1
            except Exception:
                logger.exception("Port power re-assert failed (%s)", c.name)
        return n
