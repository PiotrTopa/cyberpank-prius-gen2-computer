"""
Keep the car computer's Wi-Fi AP (``prius``, wlan0 10.42.0.1) up while
something on it is needed with the key off.

The ``low`` power profile (POCO/rootfs/usr/local/sbin/prius-power) suspends
Wi-Fi ``/etc/prius/wifi-grace`` (20 min) after key-off through the transient
``prius-wifi-suspend.timer``. ``poco2`` is reachable only over that AP, so a
poco2 started with the key off after the grace (MFD operator hold, mode
``on``) could never be seen: it booted, found no AP, and kept running unseen
(2026-11-23 20:48: ~0.65 W on the 12 V all night).

Owners call :meth:`hold` / :meth:`release`; :meth:`tick` then

* while held: stops a pending ``prius-wifi-suspend`` timer and, when the AP has
  no IPv4, runs ``prius-wifi resume`` (re-applies /etc/prius/wifi-mode, ``ap``
  on the car), at most every ``resume_retry_s``;
* when the last hold goes and the power profile is ``low``: runs
  ``prius-power apply``, which re-arms the normal Wi-Fi grace (a fresh 20 min)
  and leaves everything else as the ``low`` profile has it.

Commands are fire-and-forget (``spawn``), never blocking the backend loop.
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Callable, List, Optional, Set

logger = logging.getLogger(__name__)

AP_ADDR = "10.42.0.1"
SUSPEND_UNIT = "prius-wifi-suspend"


def _default_ap_up(iface: str = "wlan0") -> bool:
    from ..io.wifi_transport import iface_ipv4
    return iface_ipv4(iface) == AP_ADDR


def _default_spawn(cmd: List[str]) -> None:
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


class WifiHold:
    def __init__(self, ap_up: Callable[[], bool] = _default_ap_up,
                 spawn: Callable[[List[str]], None] = _default_spawn,
                 clock: Callable[[], float] = time.monotonic,
                 resume_retry_s: float = 60.0,
                 wifi_cmd: str = "/usr/local/sbin/prius-wifi",
                 power_cmd: str = "/usr/local/sbin/prius-power") -> None:
        self._ap_up = ap_up
        self._spawn = spawn
        self._clock = clock
        self.resume_retry_s = resume_retry_s
        self.wifi_cmd = wifi_cmd
        self.power_cmd = power_cmd
        self._owners: Set[str] = set()
        self._held = False             # a hold was in force at the last tick
        self._last_resume: Optional[float] = None

    def hold(self, owner: str) -> None:
        self._owners.add(owner)

    def release(self, owner: str) -> None:
        self._owners.discard(owner)

    @property
    def held(self) -> bool:
        return bool(self._owners)

    def ap_up(self) -> bool:
        try:
            return bool(self._ap_up())
        except Exception:
            return False

    def tick(self, power_mode: Optional[str]) -> None:
        now = self._clock()
        if self._owners:
            if not self._held:
                logger.info("Wi-Fi hold on (%s): keeping the AP up", ", ".join(sorted(self._owners)))
                # Cancel the key-off suspend before it fires.
                self._spawn(["systemctl", "stop", SUSPEND_UNIT + ".timer", SUSPEND_UNIT + ".service"])
                self._held = True
            if not self.ap_up() and (self._last_resume is None
                                     or now - self._last_resume >= self.resume_retry_s):
                self._last_resume = now
                logger.info("Wi-Fi hold: AP down, prius-wifi resume")
                self._spawn(["timeout", "-k", "5", "120", self.wifi_cmd, "resume"])
        elif self._held:
            self._held = False
            self._last_resume = None
            if power_mode == "low":
                logger.info("Wi-Fi hold off: re-arming the key-off Wi-Fi grace (prius-power apply)")
                self._spawn(["timeout", "-k", "5", "60", self.power_cmd, "apply"])
            else:
                logger.info("Wi-Fi hold off")
