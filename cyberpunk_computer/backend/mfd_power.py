"""
MFD video board (Raspberry Pi Zero v1.3) power management.

The MFD board generates the 15 kHz VGA666 video signal for the Prius Gen 2
MFD. Its 12 V comes through powerbox relay ch2 (``backend.port_power``
RelayPortPower, injected as ``controller``; the legacy ``hub``/``port`` fields
are only used by the direct ``prius-usb-power`` fallback) and it enumerates as
a USB Ethernet gadget (``cdc_ether`` → ``usb0``, Pi static IP 192.168.100.2;
the host side must be (re)configured to 192.168.100.1/24 after EVERY
enumeration — kernel forgets the address when the gadget re-registers).

Every hard cut risks the Pi's SD card, so the OFF path is always ``ssh
poweroff`` → wait ``shutdown_wait_s`` (or until the gadget disappears) → cut.
A powerbox hard restart drops the relay without that courtesy (relay VCC is
the RP2040's 5 V); the manager then sees the board unreachable and recovers
through its normal ON-state watchdog once the backend re-asserts the relay.

Power policy (state machine, driven from the backend loop at a slow cadence):

    OFF ──ACC on──▶ BOOTING ──iface+ping──▶ ON ──ACC off──▶ GRACE
     ▲                                       ▲                │
     │                                       └────ACC on──────┘
     │                                                        │ grace expires
     └── port power cut ◀── POWERING_OFF ◀── SHUTTING_DOWN ◀──┘ (ssh poweroff)

* ACC on: hub-port power on, wait for the gadget iface, configure the host
  IP, ping until reachable.
* Early start (``tick(acc_on, early=True)``): the same demand as ACC, raised
  by :class:`BeanWakeTrigger` when the BEAN body bus wakes and shows somebody
  getting in (doors unlocked or a door open), typically 10-30 s before the
  key reaches ACC. When the bus goes quiet again without ACC the board goes
  through the normal grace, then the clean shutdown.
* ACC off: keep running for a grace period (default 20 min) in case the key
  comes back; then ask the Pi to shut down cleanly over SSH, wait for the
  halt to complete, and finally cut the port power.
* Dark grace: on entering the grace the Pi is put into a dark standby over
  SSH (``dark_cmd``: the UI process frozen with SIGSTOP, framebuffer blanked,
  activity LED off) so it idles instead of rendering to a screen nobody
  sees; when the demand returns ``restore_cmd`` undoes it at once (SIGCONT:
  no boot, no UI start). Both commands are idempotent and are sent a few
  times, because the SSH call is fire-and-forget.
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


# Dark standby on the Pi (run as the ssh user, which has passwordless sudo).
# Every step tolerates failure; the restore order ends with SIGCONT so the UI
# redraws onto an unblanked framebuffer.
DARK_CMD = ("sudo systemctl kill -s SIGSTOP mfd.service; "
            "echo 1 | sudo tee /sys/class/graphics/fb0/blank >/dev/null 2>&1; "
            "echo 0 | sudo tee /sys/class/leds/ACT/brightness >/dev/null 2>&1; true")
RESTORE_CMD = ("echo 0 | sudo tee /sys/class/graphics/fb0/blank >/dev/null 2>&1; "
               "echo 1 | sudo tee /sys/class/leds/ACT/brightness >/dev/null 2>&1; "
               "sudo systemctl kill -s SIGCONT mfd.service; true")


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
    grace_s: float = 1200.0          # ACC-off grace before shutdown (20 min)
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
    # Early start from the BEAN body bus: "entry" = bus active and the doors
    # are unlocked or a door is open; "any" = any bus activity; "off".
    bean_wake: str = "entry"
    bean_active_s: float = 15.0      # the bus counts as active this long after a frame
    bean_own_tx_mute_s: float = 30.0 # ignore the bus this long after our own transmit
    # Dark standby during the grace (shell run on the Pi over SSH; "" = off).
    dark_grace: bool = True
    dark_cmd: str = DARK_CMD
    restore_cmd: str = RESTORE_CMD
    standby_sends: int = 3           # each command is repeated on this many ticks
    # While the board is ON the restore is re-sent this often (0 = never): the
    # SSH call is fire-and-forget, so a lost restore must not leave the UI
    # frozen with the key on.
    restore_resend_s: float = 30.0


class BeanWakeTrigger:
    """Early-start demand from the BEAN body bus (see MfdPowerConfig.bean_wake).

    ``update(bean, own_tx_ts, now, wall)`` -> bool. ``bean`` is BeanState (or
    None); activity is a change of ``frames_total`` judged on the backend's
    own clock (frame timestamps from the firmware are not wall-clock exact).
    The bus waking because WE transmitted (``bean_wake``, last reply time
    ``own_tx_ts``, wall clock) is not somebody getting in: muted for
    ``bean_own_tx_mute_s``. In "entry" mode locking the car and walking away
    (bus active, doors locked, nothing open) does not start the board.
    """

    def __init__(self, mode: str = "entry", active_s: float = 15.0,
                 own_tx_mute_s: float = 30.0) -> None:
        self.mode = mode if mode in ("entry", "any") else "off"
        self.active_s = active_s
        self.own_tx_mute_s = own_tx_mute_s
        self._frames: Optional[int] = None
        self._last_activity: Optional[float] = None
        self.reason = ""

    @staticmethod
    def _val(decoded, name):
        entry = (decoded or {}).get(name)
        return entry.get("value") if hasattr(entry, "get") else None

    def update(self, bean, own_tx_ts: Optional[float], now: float, wall: float) -> bool:
        if self.mode == "off" or bean is None:
            return False
        frames = int(getattr(bean, "frames_total", 0) or 0)
        if self._frames is None:
            self._frames = frames          # first look: no activity inferred
        elif frames != self._frames:
            self._frames = frames
            muted = own_tx_ts is not None and 0 <= wall - own_tx_ts < self.own_tx_mute_s
            if not muted:
                self._last_activity = now
        if self._last_activity is None or now - self._last_activity > self.active_s:
            return False
        if self.mode == "any":
            self.reason = "BEAN bus active"
            return True
        decoded = getattr(bean, "decoded", None)
        if self._val(decoded, "doors_locked") is False:
            self.reason = "BEAN: doors unlocked"
            return True
        if self._val(decoded, "any_door_open") or self._val(decoded, "door_open"):
            self.reason = "BEAN: door open"
            return True
        return False


class MfdPowerManager:
    """ACC-follower power manager for the MFD video board.

    Call :meth:`tick` from the backend loop; it self-limits to ``tick_s``.
    ``publish`` is invoked with a status dict whenever the observable state
    (phase / port power / reachability) changes.
    """

    PUBLISH_REFRESH_S = 30.0  # periodic status republish (keeps ping ages fresh)

    def __init__(
        self,
        config: MfdPowerConfig,
        publish: Optional[Callable[[dict], None]] = None,
        runner: Optional[Callable[..., "subprocess.CompletedProcess"]] = None,
        spawner: Optional[Callable[[list], None]] = None,
        iface_exists: Optional[Callable[[str], bool]] = None,
        clock: Callable[[], float] = time.monotonic,
        controller: Optional[object] = None,
        iface_configured: Optional[Callable[[str, str], bool]] = None,
    ) -> None:
        self.config = config
        self._publish = publish
        self._run = runner or self._default_runner
        self._spawn = spawner or self._default_spawner
        self._iface_exists = iface_exists or self._default_iface_exists
        self._iface_configured = iface_configured or self._default_iface_configured
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
        self._last_publish_t: float = 0.0
        self._reconciled = False  # startup: adopt whatever state the board is in
        self._last_enforce = 0.0  # last time we verified hw port power
        # Health/debug telemetry (published to the dashboard).
        self._state_since: float = time.time()      # wall clock of last state change
        self._last_ok_ping: Optional[float] = None  # wall clock of last good ping
        self._unreachable_since: Optional[float] = None  # monotonic, ON-state watchdog
        self._power_cycles: int = 0                 # boot-timeout + watchdog cycles
        self._boot_started: Optional[float] = None  # monotonic, boot duration measure
        self._last_boot_s: Optional[float] = None   # last power-on -> reachable time
        # Dark standby: what the Pi was last told, and how many repeats are left.
        self._dark: bool = False
        self._standby_left: int = 0
        self._last_restore: float = 0.0   # monotonic, last restore sent in ON

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

    def _default_iface_configured(self, iface: str, host_cidr: str) -> bool:
        """Sudo-free probe: is ``iface`` UP and carrying ``host_cidr``'s address?

        Reads the IFF_UP flag from sysfs and lists the v4 addresses with
        ``ip -o -4 addr show`` (no privilege needed). Any failure reads as
        "not configured", which falls through to the (idempotent) re-apply.
        """
        try:
            with open(f"/sys/class/net/{iface}/flags") as fh:
                if not int(fh.read().strip(), 16) & 0x1:   # IFF_UP
                    return False
        except (OSError, ValueError):
            return False
        want = host_cidr.split("/", 1)[0]
        try:
            r = self._run(["ip", "-o", "-4", "addr", "show", "dev", iface], timeout=5)
        except Exception:
            return False
        if getattr(r, "returncode", 1) != 0:
            return False
        out = getattr(r, "stdout", "") or ""
        return any(f" inet {want}/" in f" {line}" for line in str(out).splitlines())

    def _configure_host_iface(self) -> None:
        """(Re)apply the host-side IP config when it is missing.

        Gadget re-enumerations wipe the host address, so this runs every tick
        while the board is up — but only the sudo-free probe does in the steady
        state. Blindly re-adding every tick cost two sudo calls per 5 s (217 in
        the 18 min after a boot, 2026-11-04) and buried the journal.
        """
        cfg = self.config
        try:
            if self._iface_configured(cfg.iface, cfg.host_cidr):
                return
        except Exception:
            logger.debug("MFD host iface probe failed", exc_info=True)
        logger.info("MFD host link: configuring %s %s", cfg.iface, cfg.host_cidr)
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

    def _standby(self, dark: bool) -> None:
        """Ask for the dark standby (grace) or its restore; repeats follow
        from :meth:`_standby_tick`."""
        cfg = self.config
        if not cfg.dark_grace or not (cfg.dark_cmd and cfg.restore_cmd):
            return
        if dark != self._dark:
            logger.info("MFD board: %s", "dark standby for the grace (UI frozen, screen blanked)"
                        if dark else "restore from the dark standby")
        self._dark = dark
        self._standby_left = max(1, cfg.standby_sends)
        self._standby_tick()

    def _standby_tick(self) -> None:
        if self._standby_left <= 0:
            return
        self._standby_left -= 1
        cfg = self.config
        if not self._dark:
            self._last_restore = self._clock()
        cmd = ["sudo", "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
               "-o", "ConnectTimeout=5", f"{cfg.ssh_user}@{cfg.board_ip}",
               cfg.dark_cmd if self._dark else cfg.restore_cmd]
        try:
            self._spawn(cmd)
        except Exception:
            logger.exception("MFD board standby command failed")

    # ── observability ─────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

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
            "dark": self._dark,
        }

    def _emit(self) -> None:
        # Publish on any observable change, plus a periodic refresh so slowly
        # moving telemetry (last_ok_ping) doesn't go stale in the store/UI.
        key = (self._state, self._powered, self._reachable,
               self._power_cycles, self._last_boot_s)
        now = self._clock()
        if key == self._last_published and \
                (now - self._last_publish_t) < self.PUBLISH_REFRESH_S:
            return
        self._last_published = key
        self._last_publish_t = now
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
        self._dark = False            # a fresh boot is never dark
        self._standby_left = 0
        self._enter(into_state, self.config.boot_timeout_s)

    # ── state machine ─────────────────────────────────────────────────────

    def tick(self, acc_on: bool, early: bool = False, why: str = "") -> None:
        """``early``: start demand that is not ACC (BEAN entry); it is treated
        like ACC, and a start from OFF does not wait for the next slow tick."""
        now = self._clock()
        if early and not acc_on and self._state == STATE_OFF and self._reconciled:
            logger.info("MFD power: early start (%s)", why or "early demand")
            self._last_tick = 0.0
        acc_on = bool(acc_on or early)
        if self._last_tick and (now - self._last_tick) < self.config.tick_s:
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
                # The backend may have restarted mid-grace: make the Pi match.
                self._standby(not acc_on)
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
            # Gadget re-enumerations wipe the host address: keep checking
            # (and re-applying only when it is gone).
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
                self._standby(True)
            else:
                self._standby_tick()
                if (cfg.dark_grace and cfg.restore_cmd and cfg.restore_resend_s > 0
                        and self._standby_left <= 0 and reachable
                        and now - self._last_restore >= cfg.restore_resend_s):
                    self._dark = False
                    self._standby_left = 1
                    self._standby_tick()

        elif self._state == STATE_GRACE:
            if acc_on:
                logger.info("MFD power: %s during grace, staying up", why if early and why else "ACC back")
                self._enter(STATE_ON)
                self._standby(False)
            elif now >= (self._deadline or 0):
                self._request_shutdown()
                self._enter(STATE_SHUTTING_DOWN, cfg.shutdown_wait_s)
            else:
                self._standby_tick()
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
            self._dark = False
            self._standby_left = 0
            self._set_port_power(False)
            self._reachable = None
            self._enter(STATE_OFF)

        self._emit()
