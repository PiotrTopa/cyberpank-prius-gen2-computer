"""
Secondary POCO ("roadsense" board) power management.

The secondary POCO F1 runs the GPU-heavy road perception stack and must only
be up while the car is on. It has permanent power (its own buck on the 12 V
feed) and no battery, so unlike the MFD Pi it is never power-switched:

* **power on**  = a 6 s hold of its power button, pressed by the powerbox on
  our command (``{"a":"button","poco":2,"ms":6000}``);
* **power off** = a clean ``poweroff`` over SSH;
* **reachability** = ping over the network.

Power policy (same shape as :mod:`backend.mfd_power`, driven from the backend
loop at a slow cadence):

    OFF ──ACC on──▶ BOOTING ──ping ok──▶ ON ──ACC off──▶ GRACE
     ▲                 │ timeout: press again,       ▲        │
     │                 │ then give up (OFF)          └─ACC on─┘
     │                                                        │ grace expires
     └────── halted (unreachable) ◀── SHUTTING_DOWN ◀─────────┘ (ssh poweroff)

* ACC on: press the button, wait for the board to answer pings. If it does
  not come up within ``boot_timeout_s`` press again, up to ``max_boot_presses``
  times per ACC cycle, then stop (OFF) until the next key-on so a dead board
  is not hammered forever.
* ACC off: keep running for a grace period (default 10 min) in case the key
  comes back; then ask the board to shut down cleanly over SSH and wait for
  it to go silent (or ``shutdown_wait_s``).
* While ON the manager tracks reachability by ping and reports it to the
  powerbox (``{"a":"p2","alive":b}``) so the box can show it in STATUS.

All external effects go through injectable callables so the whole state
machine is unit-testable without hardware.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# State machine phases (exposed verbatim in AppState.connection.poco2_state).
STATE_OFF = "off"
STATE_BOOTING = "booting"
STATE_ON = "on"
STATE_GRACE = "grace"
STATE_SHUTTING_DOWN = "shutting_down"


@dataclass
class Poco2PowerConfig:
    """Configuration for the secondary-POCO power manager (BACKEND_POCO2_*)."""
    board_ip: str = "192.168.0.86"   # how the main POCO reaches the secondary
    ssh_user: str = "user"           # account on the secondary for the poweroff
    press_ms: int = 6000             # power-on hold (bench 2026-10-26: 3 s fails, 6 s boots)
    grace_s: float = 600.0           # ACC-off grace before shutdown (10 min)
    boot_timeout_s: float = 150.0    # max wait for ping after a press before pressing again
    max_boot_presses: int = 2        # presses per ACC cycle before giving up
    shutdown_wait_s: float = 60.0    # wait after `poweroff` before declaring it off
    tick_s: float = 5.0              # manager cadence within the backend loop
    ping_timeout_s: int = 2          # per-ping wait
    # In ON, how long the board may stay unreachable before we treat it as
    # gone (crashed / powered itself off) and go back to OFF -> re-boot. 0
    # disables (stay ON regardless of pings).
    unreachable_off_s: float = 120.0


class Poco2PowerManager:
    """ACC-follower for the secondary POCO. Call :meth:`tick` from the backend
    loop; it self-limits to ``tick_s``. ``publish`` receives a status dict on
    every observable change; ``press`` issues the powerbox button command;
    ``report_alive`` mirrors reachability to the powerbox."""

    PUBLISH_REFRESH_S = 30.0

    def __init__(
        self,
        config: Poco2PowerConfig,
        press: Callable[[int], bool],
        publish: Optional[Callable[[dict], None]] = None,
        report_alive: Optional[Callable[[bool], bool]] = None,
        runner: Optional[Callable[..., "subprocess.CompletedProcess"]] = None,
        spawner: Optional[Callable[[list], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._press = press
        self._publish = publish
        self._report_alive = report_alive
        self._run = runner or self._default_runner
        self._spawn = spawner or self._default_spawner
        self._clock = clock

        self._state = STATE_OFF
        self._reachable: Optional[bool] = None
        self._deadline: Optional[float] = None
        self._last_tick = 0.0
        self._last_published: Optional[tuple] = None
        self._last_publish_t: float = 0.0
        self._reconciled = False
        self._boot_presses = 0
        self._gave_up = False          # board never came up this ACC cycle
        self._boot_started: Optional[float] = None
        self._unreachable_since: Optional[float] = None
        self._last_reported_alive: Optional[bool] = None
        # Telemetry.
        self._state_since: float = time.time()
        self._last_ok_ping: Optional[float] = None
        self._last_boot_s: Optional[float] = None

    # ── external effects (injectable) ────────────────────────────────────

    @staticmethod
    def _default_runner(cmd, timeout=10):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    @staticmethod
    def _default_spawner(cmd) -> None:
        subprocess.Popen(cmd, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=5",
            f"{cfg.ssh_user}@{cfg.board_ip}",
            "sudo", "poweroff",
        ]
        try:
            self._spawn(cmd)
            logger.info("Secondary POCO: clean shutdown requested over SSH (%s)", cfg.board_ip)
        except Exception:
            logger.exception("Secondary POCO shutdown request failed")

    def _press_power(self, why: str) -> None:
        self._boot_presses += 1
        logger.info("Secondary POCO: power-button press #%d (%s)", self._boot_presses, why)
        try:
            self._press(self.config.press_ms)
        except Exception:
            logger.exception("Secondary POCO button press failed")

    def _set_reachable(self, reachable: Optional[bool]) -> None:
        if reachable != self._reachable and reachable is not None:
            logger.info("Secondary POCO %s (%s)",
                        "reachable" if reachable else "UNREACHABLE", self.config.board_ip)
        self._reachable = reachable
        if reachable:
            self._last_ok_ping = time.time()
        alive = bool(reachable)
        if alive != self._last_reported_alive and self._report_alive is not None:
            self._last_reported_alive = alive
            try:
                self._report_alive(alive)
            except Exception:
                logger.debug("Secondary POCO alive report failed", exc_info=True)

    # ── observability ─────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "state": self._state,
            "reachable": self._reachable,
            "deadline_in_s": (
                max(0.0, self._deadline - self._clock())
                if self._deadline is not None else None
            ),
            "state_since": self._state_since,
            "last_ok_ping": self._last_ok_ping,
            "boot_presses": self._boot_presses,
            "gave_up": self._gave_up,
            "last_boot_s": self._last_boot_s,
        }

    def _emit(self) -> None:
        key = (self._state, self._reachable, self._boot_presses, self._last_boot_s)
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
                logger.exception("Secondary POCO status publish failed")

    def _enter(self, state: str, deadline_in: Optional[float] = None) -> None:
        if state != self._state:
            logger.info("Secondary POCO power: %s -> %s", self._state, state)
            self._state_since = time.time()
        self._state = state
        self._deadline = (self._clock() + deadline_in) if deadline_in is not None else None

    def _start_boot(self, now: float, why: str) -> None:
        self._boot_started = now
        self._press_power(why)
        self._enter(STATE_BOOTING, self.config.boot_timeout_s)

    # ── state machine ─────────────────────────────────────────────────────

    def tick(self, acc_on: bool) -> None:
        now = self._clock()
        if (now - self._last_tick) < self.config.tick_s:
            return
        self._last_tick = now
        cfg = self.config

        if not self._reconciled:
            # Startup: the backend may restart while the board is up (mid-grace
            # or while driving). Adopt a reachable board instead of pressing
            # its button, which on a running phone is at best a no-op.
            self._reconciled = True
            if self._ping():
                logger.info("Secondary POCO: adopting already-running board at startup")
                self._set_reachable(True)
                self._enter(STATE_ON if acc_on else STATE_GRACE,
                            None if acc_on else cfg.grace_s)
            else:
                self._set_reachable(False)

        if not acc_on:
            self._gave_up = False      # a fresh key-on may try again

        if self._state == STATE_OFF:
            self._unreachable_since = None
            if acc_on and not self._gave_up:
                self._boot_presses = 0
                self._start_boot(now, "ACC on")

        elif self._state == STATE_BOOTING:
            if self._ping():
                self._set_reachable(True)
                if self._boot_started is not None:
                    self._last_boot_s = round(now - self._boot_started, 1)
                self._enter(STATE_ON)
            elif now >= (self._deadline or 0):
                if self._boot_presses < cfg.max_boot_presses:
                    self._start_boot(now, "no ping within %.0fs" % cfg.boot_timeout_s)
                else:
                    logger.error(
                        "Secondary POCO did not come up after %d presses — giving up "
                        "until the next key-on", self._boot_presses)
                    self._set_reachable(False)
                    self._gave_up = True   # stay OFF until ACC cycles
                    self._enter(STATE_OFF)
            # Key going away mid-boot: let it finish, ON moves it to GRACE.

        elif self._state == STATE_ON:
            reachable = self._ping()
            self._set_reachable(reachable)
            if reachable:
                self._unreachable_since = None
            else:
                if self._unreachable_since is None:
                    self._unreachable_since = now
                elif (cfg.unreachable_off_s > 0 and
                        now - self._unreachable_since >= cfg.unreachable_off_s):
                    # Crashed or powered itself off: go back to OFF so a
                    # key-on (or the next tick, if ACC is still on) re-boots it.
                    logger.warning("Secondary POCO unreachable for %.0fs in ON — treating as off",
                                   now - self._unreachable_since)
                    self._unreachable_since = None
                    self._enter(STATE_OFF)
            if not acc_on and self._state == STATE_ON:
                self._enter(STATE_GRACE, cfg.grace_s)

        elif self._state == STATE_GRACE:
            if acc_on:
                logger.info("Secondary POCO power: ACC back during grace, staying up")
                self._enter(STATE_ON)
            elif now >= (self._deadline or 0):
                self._request_shutdown()
                self._enter(STATE_SHUTTING_DOWN, cfg.shutdown_wait_s)
            else:
                self._set_reachable(self._ping())

        elif self._state == STATE_SHUTTING_DOWN:
            # ACC returning now is deliberately ignored: the halt is in flight;
            # OFF will re-boot the board on the next tick if ACC is still on.
            gone = not self._ping()
            if gone or now >= (self._deadline or 0):
                self._set_reachable(False)
                self._enter(STATE_OFF)

        self._emit()
