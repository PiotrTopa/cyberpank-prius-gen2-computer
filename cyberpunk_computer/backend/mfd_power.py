"""
MFD video board (Raspberry Pi Zero 2W) power management.

The MFD board generates the 15 kHz VGA666 video signal for the Prius Gen 2
MFD. It hangs off the USB hub's PPPS-capable port (currently the only socket
with genuinely switchable VBUS) and enumerates as a USB Ethernet gadget
(``cdc_ether`` → ``usb0``, Pi static IP 192.168.100.2; the host side must be
(re)configured to 192.168.100.1/24 after EVERY enumeration — kernel forgets
the address when the gadget re-registers).

Power policy (state machine, driven from the backend loop at a slow cadence):

    OFF ──ACC on──▶ BOOTING ──iface+ping──▶ ON ──ACC off──▶ GRACE
     ▲                                       ▲                │
     │                                       └────ACC on──────┘
     │                                                        │ grace expires
     └── port power cut ◀── POWERING_OFF ◀── SHUTTING_DOWN ◀──┘ (ssh poweroff)

* ACC on: hub-port power on, wait for the gadget iface, configure the host
  IP, ping until reachable.
* ACC off: keep running for a grace period (default 10 min) in case the key
  comes back; then ask the Pi to shut down cleanly over SSH, wait for the
  halt to complete, and finally cut the port power.
* While ON the manager keeps enforcing host-side IP config (gadget
  re-enumerations wipe it) and tracks reachability via ping.

All external effects go through an injectable ``runner`` so the whole state
machine is unit-testable without hardware.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# State machine phases (exposed verbatim in AppState.connection.mfd_state).
STATE_DISABLED = "disabled"
STATE_OFF = "off"
STATE_BOOTING = "booting"
STATE_ON = "on"
STATE_GRACE = "grace"
STATE_SHUTTING_DOWN = "shutting_down"
STATE_POWERING_OFF = "powering_off"


@dataclass
class MfdPowerConfig:
    """Configuration for the MFD video-board power manager.

    Everything an installation might need to retune lives here and is wired
    to ``BACKEND_MFD_*`` environment variables (see backend.__main__), so
    moving the board to another hub port or changing addressing is a config
    edit + service restart — no code change.
    """
    hub: str = "1-1"                 # uhubctl hub location
    port: int = 5                    # hub port with switchable VBUS (PPPS)
    iface: str = "usb0"              # gadget netdev name on the host
    host_cidr: str = "192.168.100.1/24"   # host-side address on the gadget link
    board_ip: str = "192.168.100.2"  # Pi's static IP
    ssh_user: str = "piotr"          # account on the Pi for the shutdown call
    grace_s: float = 600.0           # ACC-off grace before shutdown (10 min)
    boot_timeout_s: float = 180.0    # max wait for iface+ping before re-cycle
    shutdown_wait_s: float = 45.0    # wait after `poweroff` before cutting VBUS
    tick_s: float = 5.0              # manager cadence within the backend loop
    ping_timeout_s: int = 2          # per-ping wait
    enforce_interval_s: float = 60.0 # how often to verify hw port power matches desired state
    # ON-state watchdog: the Pi's cdc_ether gadget can enumerate and then come
    # up with a dead TX queue (NETDEV WATCHDOG: transmit queue timed out) —
    # host-side USBDEVFS_RESET does NOT fix it; only a VBUS cycle does. If the
    # board stays unreachable in ON for this long, power-cycle it. 0 disables.
    unreachable_recover_s: float = 45.0


class MfdPowerManager:
    """ACC-follower power manager for the MFD video board.

    Call :meth:`tick` from the backend loop; it self-limits to ``tick_s``.
    ``publish`` is invoked with a status dict whenever the observable state
    (phase / port power / reachability) changes.
    """

    def __init__(
        self,
        config: MfdPowerConfig,
        publish: Optional[Callable[[dict], None]] = None,
        runner: Optional[Callable[..., "subprocess.CompletedProcess"]] = None,
        spawner: Optional[Callable[[list], None]] = None,
        iface_exists: Optional[Callable[[str], bool]] = None,
        clock: Callable[[], float] = time.monotonic,
        controller: Optional[object] = None,
    ) -> None:
        self.config = config
        self._publish = publish
        self._run = runner or self._default_runner
        self._spawn = spawner or self._default_spawner
        self._iface_exists = iface_exists or self._default_iface_exists
        self._clock = clock
        # Port power controller (backend.port_power.RelayPortPower on the car:
        # the Pi's VBUS goes through powerbox relay ch3, with data-off
        # sequencing and lossy-command verification). Falls back to the legacy
        # direct prius-usb-power calls when None (tests / old deployments).
        self._controller = controller

        self._state = STATE_OFF
        self._powered: Optional[bool] = None   # last commanded port power
        self._reachable: Optional[bool] = None
        self._deadline: Optional[float] = None  # phase timeout (grace/boot/halt)
        self._last_tick = 0.0
        self._last_published: Optional[tuple] = None
        self._reconciled = False  # startup: adopt whatever state the board is in
        self._last_enforce = 0.0  # last time we verified hw port power
        # Health/debug telemetry (published to the dashboard).
        self._state_since: float = time.time()      # wall clock of last state change
        self._last_ok_ping: Optional[float] = None  # wall clock of last good ping
        self._unreachable_since: Optional[float] = None  # monotonic, ON-state watchdog
        self._power_cycles: int = 0                 # boot-timeout + watchdog cycles
        self._boot_started: Optional[float] = None  # monotonic, boot duration measure
        self._last_boot_s: Optional[float] = None   # last power-on -> reachable time

    # ── external effects (injectable) ────────────────────────────────────

    @staticmethod
    def _default_runner(cmd, timeout=10):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    @staticmethod
    def _default_spawner(cmd) -> None:
        # Fire-and-forget (the Pi's poweroff drops the connection anyway).
        subprocess.Popen(cmd, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @staticmethod
    def _default_iface_exists(iface: str) -> bool:
        return os.path.isdir(f"/sys/class/net/{iface}")

    def _set_port_power(self, on: bool) -> None:
        if self._controller is not None:
            try:
                self._controller.set(on)
                self._powered = on
                logger.info("MFD board port power -> %s (controller)", "ON" if on else "OFF")
            except Exception:
                logger.exception("MFD port power %s failed (controller)", "on" if on else "off")
            return
        loc = f"{self.config.hub} {self.config.port}"
        try:
            self._run(["sudo", "prius-usb-power", "on" if on else "off", loc])
            self._powered = on
            logger.info("MFD board port power -> %s (%s)", "ON" if on else "OFF", loc)
        except Exception:
            logger.exception("MFD port power %s failed", "on" if on else "off")

    def _query_hw_port_power(self) -> Optional[bool]:
        """Ask uhubctl for the actual hardware power state of the MFD port.

        Returns True if port power is on, False if off, None on error.
        Parses lines like ``  Port 5: 0100 power`` or ``  Port 5: 0000 off``.
        """
        if self._controller is not None:
            try:
                return self._controller.read()
            except Exception:
                logger.debug("MFD controller read failed", exc_info=True)
                return None
        try:
            r = self._run(
                ["sudo", "prius-usb-power", "status"],
                timeout=10,
            )
            if r.returncode != 0:
                return None
            hub_section = False
            for line in r.stdout.splitlines():
                # uhubctl groups output by hub; look for our hub.
                if f"hub {self.config.hub}" in line.lower() or \
                   f"Hub {self.config.hub}" in line or \
                   f"location {self.config.hub}" in line.replace("-", "-"):
                    hub_section = True
                    continue
                if hub_section and line.strip().startswith("Current"):
                    # Next hub starts; stop scanning.
                    hub_section = False
                    continue
                if hub_section and f"Port {self.config.port}:" in line:
                    low = line.lower()
                    if "power" in low:
                        return True
                    if "off" in low:
                        return False
        except Exception:
            logger.debug("MFD hw port power query failed", exc_info=True)
        return None

    def _enforce_port_power(self) -> None:
        """Re-apply desired port power if hardware state has drifted.

        Hub resets (e.g. powerbox CDC recovery unbind/rebind of hub 1-1)
        silently restore all ports to ON. This method detects that drift
        and re-issues the uhubctl command.
        """
        if self._powered is None:
            return  # no desired state yet
        hw = self._query_hw_port_power()
        if hw is None:
            return  # query failed, skip this cycle
        if hw == self._powered:
            return  # hardware matches desired, all good
        logger.warning(
            "MFD port power DRIFT detected: desired=%s actual=%s — re-enforcing",
            "ON" if self._powered else "OFF",
            "ON" if hw else "OFF",
        )
        self._set_port_power(self._powered)

    def _configure_host_iface(self) -> None:
        """(Re)apply host-side IP config; idempotent, safe every tick."""
        cfg = self.config
        try:
            # "File exists" from a duplicate add is expected and harmless.
            self._run(["sudo", "ip", "addr", "add", cfg.host_cidr, "dev", cfg.iface])
            self._run(["sudo", "ip", "link", "set", cfg.iface, "up"])
        except Exception:
            logger.debug("MFD host iface config failed", exc_info=True)

    def _ping(self) -> bool:
        cfg = self.config
        try:
            r = self._run(
                ["ping", "-c", "1", "-W", str(cfg.ping_timeout_s), cfg.board_ip],
                timeout=cfg.ping_timeout_s + 3,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _request_shutdown(self) -> None:
        cfg = self.config
        cmd = [
            "sudo", "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=5",
            f"{cfg.ssh_user}@{cfg.board_ip}",
            "sudo", "poweroff",
        ]
        try:
            self._spawn(cmd)
            logger.info("MFD board: clean shutdown requested over SSH (%s)", cfg.board_ip)
        except Exception:
            logger.exception("MFD board shutdown request failed")

    # ── observability ─────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "state": self._state,
            "usb_power": self._powered,
            "reachable": self._reachable,
            "deadline_in_s": (
                max(0.0, self._deadline - self._clock())
                if self._deadline is not None else None
            ),
            # Health/debug telemetry (wall-clock timestamps; UI derives ages).
            "state_since": self._state_since,
            "last_ok_ping": self._last_ok_ping,
            "power_cycles": self._power_cycles,
            "last_boot_s": self._last_boot_s,
        }

    def _emit(self) -> None:
        key = (self._state, self._powered, self._reachable,
               self._power_cycles, self._last_boot_s)
        if key == self._last_published:
            return
        self._last_published = key
        if self._publish is not None:
            try:
                self._publish(self.status())
            except Exception:
                logger.exception("MFD status publish failed")

    def _enter(self, state: str, deadline_in: Optional[float] = None) -> None:
        if state != self._state:
            logger.info("MFD power: %s -> %s", self._state, state)
            self._state_since = time.time()
        self._state = state
        self._deadline = (self._clock() + deadline_in) if deadline_in is not None else None

    def _power_cycle(self, reason: str, into_state: str = STATE_BOOTING) -> None:
        """Cut and restore the board's VBUS, counting the cycle for telemetry."""
        self._power_cycles += 1
        logger.warning("MFD power-cycle #%d: %s", self._power_cycles, reason)
        self._set_port_power(False)
        self._set_port_power(True)
        self._boot_started = self._clock()
        self._unreachable_since = None
        self._enter(into_state, self.config.boot_timeout_s)

    # ── state machine ─────────────────────────────────────────────────────

    def tick(self, acc_on: bool) -> None:
        now = self._clock()
        if (now - self._last_tick) < self.config.tick_s:
            return
        self._last_tick = now
        cfg = self.config

        if not self._reconciled:
            # Startup reconciliation: the backend may restart while the board
            # is up (e.g. mid-grace). If the gadget iface exists the board is
            # running — adopt it (ON if ACC, else GRACE so it still gets a
            # clean shutdown). Otherwise ensure the port is really off so a
            # half-powered board can't linger after an unclean restart.
            self._reconciled = True
            if self._iface_exists(cfg.iface):
                logger.info("MFD power: adopting already-running board at startup")
                # Assert port power (not just assume it): the gadget iface can
                # be STALE (kernel keeps usb0 registered briefly after VBUS
                # loss, or the backend restarted mid-teardown). Going through
                # _set_port_power records desired=ON in the controller, whose
                # enforcement then actually powers the port if reality differs.
                self._set_port_power(True)
                self._enter(STATE_ON if acc_on else STATE_GRACE,
                            None if acc_on else cfg.grace_s)
            elif not acc_on:
                self._set_port_power(False)

        if self._state == STATE_OFF:
            self._reachable = None
            self._unreachable_since = None
            if acc_on:
                self._set_port_power(True)
                self._boot_started = now
                self._enter(STATE_BOOTING, cfg.boot_timeout_s)
            elif self._powered is False and (now - self._last_enforce) >= cfg.enforce_interval_s:
                # Periodically verify the hub hasn't been reset behind our back
                # (e.g. by serial_io powerbox CDC recovery unbinding hub 1-1).
                self._last_enforce = now
                self._enforce_port_power()

        elif self._state == STATE_BOOTING:
            if self._iface_exists(cfg.iface):
                self._configure_host_iface()
                if self._ping():
                    self._reachable = True
                    self._last_ok_ping = time.time()
                    if self._boot_started is not None:
                        self._last_boot_s = round(now - self._boot_started, 1)
                    self._enter(STATE_ON)
            if self._state == STATE_BOOTING and now >= (self._deadline or 0):
                # Boot never completed: power-cycle and try again. Also covers
                # the board being physically absent — we just keep retrying at
                # boot_timeout cadence, which is harmless.
                self._power_cycle(
                    "board did not come up within %.0fs" % cfg.boot_timeout_s)
            if not acc_on:
                # Key went away mid-boot: let it finish booting, then the
                # ON handler moves it to GRACE next tick.
                pass

        elif self._state == STATE_ON:
            # Gadget re-enumerations wipe the host address: keep re-applying.
            if self._iface_exists(cfg.iface):
                self._configure_host_iface()
                reachable = self._ping()
            else:
                reachable = False
            if reachable != self._reachable:
                logger.info("MFD board %s (%s)",
                            "reachable" if reachable else "UNREACHABLE", cfg.board_ip)
            self._reachable = reachable
            if reachable:
                self._last_ok_ping = time.time()
                self._unreachable_since = None
            else:
                # Unreachable-in-ON watchdog: a dead cdc_ether TX queue only
                # recovers with a VBUS cycle (USBDEVFS_RESET verified useless
                # 2026-08-10). Give the link unreachable_recover_s to come
                # back on its own, then cycle.
                if self._unreachable_since is None:
                    self._unreachable_since = now
                elif (cfg.unreachable_recover_s > 0 and
                        now - self._unreachable_since >= cfg.unreachable_recover_s):
                    self._power_cycle(
                        "board unreachable for %.0fs in ON (dead gadget?)"
                        % (now - self._unreachable_since))
            if not acc_on and self._state == STATE_ON:
                self._enter(STATE_GRACE, cfg.grace_s)

        elif self._state == STATE_GRACE:
            if acc_on:
                logger.info("MFD power: ACC back during grace, staying up")
                self._enter(STATE_ON)
            elif now >= (self._deadline or 0):
                self._request_shutdown()
                self._enter(STATE_SHUTTING_DOWN, cfg.shutdown_wait_s)
            else:
                # Keep the link maintained during grace.
                if self._iface_exists(cfg.iface):
                    self._configure_host_iface()
                    self._reachable = self._ping()

        elif self._state == STATE_SHUTTING_DOWN:
            # ACC returning now is deliberately ignored: the halt is already in
            # flight; complete the power-off and OFF will restart the board.
            gone = not self._iface_exists(cfg.iface)
            if gone or now >= (self._deadline or 0):
                self._enter(STATE_POWERING_OFF)

        if self._state == STATE_POWERING_OFF:
            self._set_port_power(False)
            self._reachable = None
            self._enter(STATE_OFF)

        self._emit()
