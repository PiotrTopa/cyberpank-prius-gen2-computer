"""
Secondary POCO (``poco2``) power management.

``poco2`` is a second POCO F1 without display or battery: a headless slave on
the car computer's Wi-Fi AP (``prius``, 10.42.0.94). It has permanent power
from its own buck regulator, so "off" means the phone is shut down, not that
its supply is cut. Two things can change its power state:

* the **power button**, wired to powerbox GP26 (firmware >= 2.4.3): a
  ``press_ms`` hold (6 s) boots a phone that is off; a ``force_ms`` hold
  (12 s) hard-restarts a hung one. The button cannot switch the phone off.
* **SSH** with a dedicated key: ``sudo systemctl poweroff`` / ``reboot``
  (a sudoers rule on ``poco2`` allows exactly these two without a password).

Liveness is a ping; "up" additionally needs an SSH command to succeed, so the
manager never reports ON for a phone it could not shut down again.

Power policy (state machine, ticked from the backend loop)::

    UNKNOWN ──ping ok──▶ ON          UNKNOWN ──no answer──▶ OFF
    OFF ──wanted──▶ BOOTING ──ping+ssh──▶ ON ──not wanted──▶ GRACE
     ▲                 │ timeout: press again,               │ expires
     │                 ▼ then BOOT_FAILED (retry later)      ▼
     └──── no ping for off_settle_s ◀──────────────── SHUTTING_DOWN (ssh poweroff)

* Mode ``auto`` follows ACC: on at key-on, off ``grace_s`` after key-off.
  ``on`` keeps it running, ``off`` keeps it shut down (no grace).
* A phone found running while OFF (started by hand) is adopted; in ``auto``
  with the key off, or in ``off``, it then goes through the normal shutdown.
* ON and unreachable for ``lost_recover_s``: one forced restart through the
  button, at most once per ``recover_min_interval_s`` (0 disables). The link
  is Wi-Fi only, so the default is long.
* SHUTTING_DOWN re-sends the SSH power-off every ``shutdown_retry_s`` while
  the phone still answers; it never uses the button (a long hold restarts).
* The 12 V current from the powerbox INA219 is recorded as a median over
  ``current_window_s`` before the power-off and over ``off_measure_s`` after
  the phone went silent; the difference is published as ``off_drop_a``
  (other loads on the box move the figure too).

All external effects go through injectable callables, so the state machine is
unit-testable without hardware.
"""

from __future__ import annotations

import logging
import statistics
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

STATE_UNKNOWN = "unknown"
STATE_OFF = "off"
STATE_BOOTING = "booting"
STATE_ON = "on"
STATE_GRACE = "grace"
STATE_SHUTTING_DOWN = "shutting_down"
STATE_BOOT_FAILED = "boot_failed"

MODE_AUTO = "auto"
MODE_ON = "on"
MODE_OFF = "off"
MODES = (MODE_AUTO, MODE_ON, MODE_OFF)


@dataclass
class Poco2PowerConfig:
    """Knobs of the secondary POCO power manager (``BACKEND_POCO2_*``)."""
    host: str = "10.42.0.94"
    ssh_user: str = "user"
    ssh_key: str = "/etc/prius/poco2_ssh_key"
    known_hosts: str = "/etc/prius/poco2_known_hosts"
    mode: str = MODE_AUTO            # initial operator mode
    grace_s: float = 120.0           # key-off grace before the shutdown (auto)
    press_ms: int = 6000             # power-on hold
    force_ms: int = 12000            # forced hard restart hold
    boot_timeout_s: float = 150.0    # press -> ping+ssh, before the next press
    boot_tries: int = 3              # presses before BOOT_FAILED
    boot_retry_s: float = 600.0      # BOOT_FAILED -> next round of presses
    shutdown_retry_s: float = 30.0   # re-send the ssh power-off this often
    off_settle_s: float = 15.0       # silent this long = off (and safe to press)
    off_measure_s: float = 30.0      # after OFF: 12 V current, median over this window
    current_window_s: float = 30.0   # ... and over this window before the power-off
    lost_recover_s: float = 300.0    # ON but silent this long -> forced restart (0 = never)
    recover_min_interval_s: float = 900.0
    tick_s: float = 5.0              # manager cadence within the backend loop
    idle_probe_s: float = 60.0       # ping cadence while OFF and not wanted
    ping_timeout_s: int = 2
    ssh_timeout_s: int = 8
    alive_report_s: float = 30.0     # liveness echo to the powerbox ("p2")


class Poco2PowerManager:
    """ACC follower for the secondary POCO; see the module docstring."""

    def __init__(
        self,
        config: Optional[Poco2PowerConfig] = None,
        press: Optional[Callable[[int], bool]] = None,
        publish: Optional[Callable[[dict], None]] = None,
        report_alive: Optional[Callable[[bool], object]] = None,
        runner: Optional[Callable] = None,
        spawner: Optional[Callable] = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        self.config = config or Poco2PowerConfig()
        self._press_cb = press
        self._publish = publish
        self._report_alive = report_alive
        self._run = runner or self._default_runner
        self._spawn = spawner or self._default_spawner
        self._clock = clock
        self._wall = wall

        self.mode = self.config.mode if self.config.mode in MODES else MODE_AUTO
        self._state = STATE_UNKNOWN
        self._state_since = wall()
        self._deadline: Optional[float] = None
        self._last_tick = float("-inf")
        self._last_probe = float("-inf")
        self._unknown_misses = 0
        self._reachable: Optional[bool] = None
        self._ssh_ok: Optional[bool] = None
        self._last_ok: Optional[float] = None       # clock of the last good ping
        self._last_ok_wall: Optional[float] = None
        self._silent_since: Optional[float] = None  # clock of the first missed ping
        self._boot_tries = 0
        self._boot_started: Optional[float] = None
        self._last_boot_s: Optional[float] = None
        self._presses = 0
        self._forced = 0
        self._last_force = float("-inf")
        self._last_shutdown_send = float("-inf")
        self._shutdown_sends = 0
        self._shutdown_started: Optional[float] = None
        self._last_shutdown_s: Optional[float] = None
        self._restart_pending: Optional[str] = None   # "clean" | "force"
        self._await_down = False     # clean restart sent: wait until it stops answering
        self._last_alive_report = float("-inf")
        self._last_alive_value: Optional[bool] = None
        self._current_a: Optional[float] = None
        self._samples: deque = deque(maxlen=120)     # (clock, amps), about 1 per second
        self._i_before_off: Optional[float] = None
        self._i_after_off: Optional[float] = None
        self._off_measure_at: Optional[float] = None
        self._last_error = ""
        self._last_status: Optional[dict] = None

    # ── external effects (injectable) ────────────────────────────────────

    @staticmethod
    def _default_runner(cmd, timeout=10):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    @staticmethod
    def _default_spawner(cmd) -> None:
        # Fire-and-forget: a power-off drops the connection anyway.
        subprocess.Popen(cmd, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _ssh_cmd(self, *remote: str) -> List[str]:
        cfg = self.config
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
               "-o", "StrictHostKeyChecking=accept-new"]
        if cfg.known_hosts:
            cmd += ["-o", "UserKnownHostsFile=%s" % cfg.known_hosts]
        if cfg.ssh_key:
            cmd += ["-i", cfg.ssh_key]
        return cmd + ["%s@%s" % (cfg.ssh_user, cfg.host), *remote]

    def _ping(self) -> bool:
        cfg = self.config
        try:
            r = self._run(["ping", "-c", "1", "-W", str(cfg.ping_timeout_s), cfg.host],
                          timeout=cfg.ping_timeout_s + 3)
            return r.returncode == 0
        except Exception:
            return False

    def _ssh_check(self) -> bool:
        try:
            r = self._run(self._ssh_cmd("true"), timeout=self.config.ssh_timeout_s)
            return r.returncode == 0
        except Exception:
            return False

    def _ssh_fire(self, what: str, *remote: str) -> None:
        try:
            self._spawn(self._ssh_cmd(*remote))
            logger.info("poco2: %s requested over SSH (%s)", what, self.config.host)
        except Exception:
            logger.exception("poco2: %s request failed", what)
            self._last_error = "%s request failed" % what

    def _press(self, ms: int, why: str) -> bool:
        ok = False
        if self._press_cb is not None:
            try:
                ok = bool(self._press_cb(int(ms)))
            except Exception:
                logger.exception("poco2: button press failed")
        if ok:
            self._presses += 1
            logger.warning("poco2: power button %d ms (%s)", ms, why)
        else:
            self._last_error = "button press not sent (%s)" % why
            logger.warning("poco2: power button press NOT sent (%s): powerbox link down?", why)
        return ok

    # ── operator interface ───────────────────────────────────────────────

    def set_mode(self, mode: str) -> None:
        mode = str(mode).strip().lower()
        if mode not in MODES:
            raise ValueError("mode must be one of %s" % ", ".join(MODES))
        if mode != self.mode:
            logger.warning("poco2: mode %s -> %s", self.mode, mode)
            self.mode = mode
            self._last_tick = float("-inf")     # act on the next loop pass
            self._emit()

    def request_restart(self, force: bool = False) -> None:
        """Restart the phone: cleanly over SSH, or (``force``) through a
        ``force_ms`` button hold. Acted on at the next tick."""
        self._restart_pending = "force" if force else "clean"
        self._last_tick = float("-inf")

    @property
    def state(self) -> str:
        return self._state

    def is_on(self) -> bool:
        return self._state in (STATE_ON, STATE_GRACE)

    def status(self) -> dict:
        drop = None
        if self._i_before_off is not None and self._i_after_off is not None:
            drop = round(self._i_before_off - self._i_after_off, 4)
        return {
            "state": self._state,
            "mode": self.mode,
            "reachable": self._reachable,
            "ssh_ok": self._ssh_ok,
            "state_since": self._state_since,
            "last_ok_ping": self._last_ok_wall,
            "presses": self._presses,
            "forced_restarts": self._forced,
            "boot_tries": self._boot_tries,
            "last_boot_s": self._last_boot_s,
            "last_shutdown_s": self._last_shutdown_s,
            "i_before_off_a": self._i_before_off,
            "i_after_off_a": self._i_after_off,
            "off_drop_a": drop,
            "last_error": self._last_error,
        }

    def _emit(self) -> None:
        st = self.status()
        if st != self._last_status:
            self._last_status = st
            if self._publish is not None:
                try:
                    self._publish(st)
                except Exception:
                    logger.exception("poco2: status publish failed")

    def _enter(self, state: str, deadline_in: Optional[float] = None) -> None:
        if state != self._state:
            logger.info("poco2 power: %s -> %s", self._state, state)
            self._state = state
            self._state_since = self._wall()
        self._deadline = (self._clock() + deadline_in) if deadline_in is not None else None

    # ── state machine ────────────────────────────────────────────────────

    def _probe(self, now: float) -> bool:
        ok = self._ping()
        self._last_probe = now
        self._reachable = ok
        if ok:
            self._last_ok = now
            self._last_ok_wall = self._wall()
            self._silent_since = None
        elif self._silent_since is None:
            self._silent_since = now
        if self._report_alive is not None and (
                ok != self._last_alive_value
                or (ok and now - self._last_alive_report >= self.config.alive_report_s)):
            try:
                self._report_alive(ok)
            except Exception:
                logger.exception("poco2: alive report failed")
            self._last_alive_value = ok
            self._last_alive_report = now
        return ok

    def _median_current(self, start: float, end: float) -> Optional[float]:
        """Median of the 12 V current samples in [start, end] (the box feeds
        other loads too, so single readings jump); the last reading when the
        window is empty."""
        vals = [a for t, a in self._samples if start <= t <= end]
        if not vals:
            return self._current_a
        return round(statistics.median(vals), 4)

    def _silent_for(self, now: float) -> float:
        return (now - self._silent_since) if self._silent_since is not None else 0.0

    def _start_boot(self, now: float, why: str, ms: Optional[int] = None) -> None:
        if not self._press(ms or self.config.press_ms, why):
            return                                   # stay; retried next tick
        self._boot_tries += 1
        if self._boot_started is None:
            self._boot_started = now
        self._ssh_ok = None
        self._enter(STATE_BOOTING, self.config.boot_timeout_s)

    def _start_shutdown(self, now: float, why: str) -> None:
        self._i_before_off = self._median_current(now - self.config.current_window_s, now)
        self._i_after_off = None
        self._off_measure_at = None
        self._shutdown_started = now
        self._shutdown_sends = 0
        self._last_shutdown_send = float("-inf")
        logger.info("poco2: shutting down (%s)", why)
        self._enter(STATE_SHUTTING_DOWN)

    def tick(self, acc_on: bool, acc_known: bool = True,
             current_a: Optional[float] = None, inhibit: bool = False,
             early: bool = False) -> None:
        """Advance the manager. ``acc_on``/``acc_known``: the debounced ACC
        state and whether the powerbox has reported it yet; ``current_a``: the
        12 V current from the powerbox INA219 (None = unknown); ``inhibit``:
        a protection is active (12 V under-voltage, powerbox shutdown), the
        phone is not wanted whatever the mode says; ``early``: the MFD's own
        start demand that is not ACC (BEAN entry, operator hold): in auto mode
        it wants the phone exactly like ACC, so both boards start together and
        share the key-off grace."""
        now = self._clock()
        cfg = self.config
        if current_a is not None:
            self._current_a = float(current_a)
            if not self._samples or now - self._samples[-1][0] >= 1.0:
                self._samples.append((now, self._current_a))
        if (self._off_measure_at is not None and now >= self._off_measure_at
                and self._state == STATE_OFF):
            self._i_after_off = self._median_current(now - cfg.off_measure_s, now)
            self._off_measure_at = None
            if self._i_before_off is not None and self._i_after_off is not None:
                logger.info("poco2: 12 V current %.3f A before the power-off, %.3f A after "
                            "(drop %.3f A)", self._i_before_off, self._i_after_off,
                            self._i_before_off - self._i_after_off)
            self._emit()
        if now - self._last_tick < cfg.tick_s:
            return
        self._last_tick = now

        if inhibit:
            wanted: Optional[bool] = False
        elif self.mode == MODE_ON:
            wanted = True
        elif self.mode == MODE_OFF:
            wanted = False
        else:
            wanted = (bool(acc_on) or bool(early)) if (acc_known or early) else None     # None = hold

        state = self._state

        if state == STATE_UNKNOWN:
            if self._probe(now):
                self._ssh_ok = self._ssh_check()
                self._enter(STATE_ON)
            else:
                self._unknown_misses += 1
                if self._unknown_misses >= 3:
                    self._enter(STATE_OFF)

        elif state in (STATE_OFF, STATE_BOOT_FAILED):
            due = wanted or now - self._last_probe >= cfg.idle_probe_s
            if due and self._probe(now):
                logger.info("poco2: found running while %s, adopting", state)
                self._boot_tries = 0
                self._boot_started = None
                self._ssh_ok = self._ssh_check()
                self._enter(STATE_ON)
            elif wanted and state == STATE_OFF:
                self._start_boot(now, "power on")
            elif wanted and self._deadline is not None and now >= self._deadline:
                self._boot_tries = 0
                self._boot_started = None
                self._start_boot(now, "power on (retry round)")
            elif not wanted and state == STATE_BOOT_FAILED and wanted is not None:
                self._boot_tries = 0
                self._boot_started = None
                self._enter(STATE_OFF)

        elif state == STATE_BOOTING:
            up = self._probe(now)
            if self._await_down:
                if not up or self._silent_for(now) > 0:
                    self._await_down = False
                up = False
            if up and self._ssh_check():
                self._ssh_ok = True
                if self._boot_started is not None:
                    self._last_boot_s = round(now - self._boot_started, 1)
                    logger.info("poco2: up after %.0f s", self._last_boot_s)
                self._boot_tries = 0
                self._boot_started = None
                self._last_error = ""
                self._enter(STATE_ON)
            elif self._deadline is not None and now >= self._deadline:
                self._await_down = False
                if wanted is False:
                    self._boot_tries = 0
                    self._boot_started = None
                    self._enter(STATE_OFF)
                elif self._boot_tries < cfg.boot_tries:
                    if not self._reachable:
                        self._start_boot(now, "no answer after %.0f s, press %d"
                                         % (cfg.boot_timeout_s, self._boot_tries + 1))
                    else:        # answers ping but not SSH: give it another window
                        self._deadline = now + cfg.boot_timeout_s
                        self._boot_tries += 1
                else:
                    self._last_error = "no answer after %d presses" % self._boot_tries
                    logger.error("poco2: %s; next round in %.0f s",
                                 self._last_error, cfg.boot_retry_s)
                    self._enter(STATE_BOOT_FAILED, cfg.boot_retry_s)

        elif state in (STATE_ON, STATE_GRACE):
            ok = self._probe(now)
            if ok and not self._ssh_ok:
                self._ssh_ok = self._ssh_check()
            pending, self._restart_pending = self._restart_pending, None
            if pending == "force":
                self._forced += 1
                self._last_force = now
                self._boot_started = now
                self._start_boot(now, "forced restart (operator)", cfg.force_ms)
            elif pending == "clean" and ok:
                self._ssh_fire("restart", "sudo", "-n", "systemctl", "reboot")
                self._boot_started = now
                self._boot_tries = 1
                self._ssh_ok = None
                self._await_down = True
                self._enter(STATE_BOOTING, cfg.boot_timeout_s)
            elif wanted is False and ok:
                if (state == STATE_ON and self.mode == MODE_AUTO and cfg.grace_s > 0
                        and not inhibit):
                    self._enter(STATE_GRACE, cfg.grace_s)
                elif (state == STATE_ON or inhibit
                      or (self._deadline is not None and now >= self._deadline)):
                    self._start_shutdown(now, "protection" if inhibit else
                                         "mode off" if self.mode == MODE_OFF
                                         else "key off, grace over")
            elif wanted and state == STATE_GRACE:
                logger.info("poco2: wanted again during the grace, staying up")
                self._enter(STATE_ON)
            elif not ok:
                silent = self._silent_for(now)
                if wanted is False and silent >= cfg.off_settle_s:
                    logger.info("poco2: went silent while not wanted, taking it as off")
                    self._enter(STATE_OFF)
                elif (wanted and cfg.lost_recover_s > 0 and silent >= cfg.lost_recover_s
                      and now - self._last_force >= cfg.recover_min_interval_s):
                    logger.error("poco2: no answer for %.0f s while ON, forcing a restart", silent)
                    self._forced += 1
                    self._last_force = now
                    self._boot_started = now
                    self._start_boot(now, "forced restart (unreachable)", cfg.force_ms)

        elif state == STATE_SHUTTING_DOWN:
            ok = self._probe(now)
            if ok:
                if now - self._last_shutdown_send >= cfg.shutdown_retry_s:
                    self._last_shutdown_send = now
                    self._shutdown_sends += 1
                    if self._shutdown_sends > 1:
                        self._last_error = "power-off sent %d times" % self._shutdown_sends
                        logger.warning("poco2: still answering, %s", self._last_error)
                    self._ssh_fire("clean shutdown", "sudo", "-n", "systemctl", "poweroff")
            elif self._silent_for(now) >= cfg.off_settle_s:
                if self._shutdown_started is not None:
                    self._last_shutdown_s = round(now - self._shutdown_started, 1)
                self._shutdown_started = None
                self._ssh_ok = None
                self._last_error = ""
                self._off_measure_at = now + cfg.off_measure_s
                self._last_probe = now
                logger.info("poco2: off (silent %.0f s after the power-off, %s s in all)",
                            self._silent_for(now), self._last_shutdown_s)
                self._enter(STATE_OFF)

        self._emit()
