"""
Minimal SNTP server for the MFD Pi on the USB gadget link.

The MFD Pi Zero has no RTC and no internet: it is linked only to the phone
(``usb0``, phone 192.168.100.1, Pi 192.168.100.2). Without a time source it
booted at its last saved clock (2026-11-15 read 2026-11-05 02:xx). The Pi runs
the stock ``systemd-timesyncd`` with ``NTP=192.168.100.1`` (see
``deployment/timesyncd-prius.conf``); this module answers its requests from
the phone's clock.

Rules:

* Only clients on ``allow_net`` (the gadget subnet) get an answer.
* While the phone's own clock is not synchronized (RTC-less phone right
  after boot, before NTP or the HTTPS fallback) the reply carries leap
  indicator 3 ("not synchronized"): timesyncd discards it and keeps its
  saved clock instead of taking a stale phone time.
* Synchronized means ``systemd-timesyncd`` reached an NTP server in this
  boot (``/run/systemd/timesync/synchronized``, root dispersion 50 ms) or
  ``prius-clock-fallback`` stepped the clock from HTTPS ``Date`` headers
  (``/run/prius-clock-synced``, ~1 s accuracy, root dispersion 1 s).

The socket binds ``0.0.0.0:123`` (needs root; the backend runs as root).
pmOS's nftables input policy drops UDP 123 on every interface except
``usb*``.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import struct
import threading
import time
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

NTP_EPOCH_OFFSET = 2208988800          # 1900-01-01 -> 1970-01-01
TIMESYNCD_MARK = "/run/systemd/timesync/synchronized"
FALLBACK_MARK = "/run/prius-clock-synced"


def clock_quality(paths: Tuple[str, str] = (TIMESYNCD_MARK, FALLBACK_MARK)) -> Optional[float]:
    """Root dispersion (s) of the phone's clock, or None when it is not
    synchronized in this boot (both marks live in /run, cleared at boot)."""
    ntp, https = paths
    if os.path.exists(ntp):
        return 0.05
    if os.path.exists(https):
        return 1.0
    return None


def _ntp_ts(t: float) -> bytes:
    sec = int(t) + NTP_EPOCH_OFFSET
    frac = int((t - int(t)) * (1 << 32)) & 0xFFFFFFFF
    return struct.pack("!II", sec & 0xFFFFFFFF, frac)


def _short(seconds: float) -> bytes:
    """NTP short format (16.16 fixed point)."""
    v = max(0, min(0xFFFFFFFF, int(seconds * 65536)))
    return struct.pack("!I", v)


def build_reply(request: bytes, t_rx: float, t_tx: float,
                dispersion: Optional[float]) -> Optional[bytes]:
    """Server reply (mode 4) to a client request (mode 3), or None for
    anything else. ``dispersion`` None = not synchronized (leap 3)."""
    if len(request) < 48:
        return None
    b0 = request[0]
    vn = (b0 >> 3) & 0x7
    mode = b0 & 0x7
    if mode != 3 or vn < 1 or vn > 4:
        return None
    leap = 0 if dispersion is not None else 3
    stratum = 2 if dispersion is not None else 0
    poll = request[2]
    head = struct.pack("!BBbb", (leap << 6) | (vn << 3) | 4, stratum, poll, -20)
    return (head
            + _short(0.0)                                  # root delay
            + _short(dispersion if dispersion is not None else 16.0)
            + b"POCO"                                      # reference id
            + _ntp_ts(t_rx)                                # reference time
            + request[40:48]                               # origin = client transmit
            + _ntp_ts(t_rx)
            + _ntp_ts(t_tx))


class SntpServer:
    """UDP responder thread. ``quality()`` returns the root dispersion or
    None (not synchronized); ``clock`` is the wall clock."""

    def __init__(self, host: str = "0.0.0.0", port: int = 123,
                 allow_net: str = "192.168.100.0/24",
                 quality: Callable[[], Optional[float]] = clock_quality,
                 clock: Callable[[], float] = time.time) -> None:
        self.host = host
        self.port = port
        self.allow = ipaddress.ip_network(allow_net)
        self.quality = quality
        self.clock = clock
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.served = 0
        self.unsynced = 0
        self.refused = 0
        self._last_state: Optional[bool] = None

    def start(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, self.port))
            s.settimeout(1.0)
        except OSError as e:
            logger.warning("SNTP server for the MFD Pi not started (%s:%d): %s",
                           self.host, self.port, e)
            return False
        self._sock = s
        self._thread = threading.Thread(target=self._loop, name="sntp", daemon=True)
        self._thread.start()
        logger.info("SNTP server for the MFD Pi on %s:%d (clients %s)",
                    self.host, self.port, self.allow)
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    @property
    def port_bound(self) -> int:
        return self._sock.getsockname()[1] if self._sock is not None else 0

    def handle(self, data: bytes, addr: Tuple[str, int], t_rx: float) -> Optional[bytes]:
        try:
            if ipaddress.ip_address(addr[0]) not in self.allow:
                self.refused += 1
                return None
        except ValueError:
            return None
        disp = self.quality()
        synced = disp is not None
        if synced != self._last_state:
            self._last_state = synced
            logger.info("SNTP server: phone clock %s", "synchronized, serving time"
                        if synced else "NOT synchronized, replies say leap=3")
        reply = build_reply(data, t_rx, self.clock(), disp)
        if reply is not None:
            if synced:
                self.served += 1
            else:
                self.unsynced += 1
        return reply

    def _loop(self) -> None:
        s = self._sock
        while not self._stop.is_set() and s is not None:
            try:
                data, addr = s.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                time.sleep(1.0)
                continue
            t_rx = self.clock()
            try:
                reply = self.handle(data, addr, t_rx)
                if reply is not None:
                    s.sendto(reply, addr)
            except Exception:
                logger.exception("SNTP reply failed")
