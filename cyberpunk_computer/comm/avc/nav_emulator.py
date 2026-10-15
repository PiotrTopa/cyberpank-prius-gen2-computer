"""
Navigation ECU (AVC-LAN 178, logical 58) emulation for the backend.

Faithful port of ``cyberpunk-prius-gateway/gateway-rp2350/tools/nav_emu.py``
as run on 2026-10-14 with ``--fw-auto --fw-ack`` (accepted by the real
display; English UI with ``B7 02``). Ground truth:
``captures/2026-10-07/NAV_EMULATION_SPEC.md`` (sections "Resolved on-car
2026-10-14" and "Real nav observed again" supersede the draft).

What it does
------------
* On gateway connect / gateway reboot: install the two firmware auto-reply
  slots (roll call ``12 01 00`` / ``12 01 01`` answered by the gateway itself
  within ~0.3 ms, before the climate ECU) and ``avc_cfg ack:"178"`` (the
  gateway stretches the ACK slots of frames addressed to 178). Polarity is
  left at the gateway's setting — ``pol`` is never sent from here.
* Answers the display's requests (identification, setup, settings, time,
  language, screen request/release, 60 s ping, button acks) with the bytes
  nav_emu.py sends.
* Broadcasts ``58 31 F1 02 00`` + ``58 01 DB …`` (5 s) only while the
  display shows screen 58; outside screen 58 they make the display complain.
* Backend restart while the car is on: if the bus is alive but the display
  does not talk to 178 (it dropped the nav while we were away), announce like
  the real nav did on 2026-10-14 15:07:09 (``realnav_full.ndjson``):
  ``178->1FF 01 12 13 58 24 85`` (broadcast). The display then re-runs the
  setup dialogue (``00 12 01 03``, ``12 01 01`` …), which the normal handlers
  answer.

Transmission is through a ``send(device_id, payload)`` callable that writes one
gateway JSON line (``{"id": device_id, "d": payload}``).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .frames import (AvcFrame, BCAST_1FF, BCAST_FFF, CLIMATE, DISPLAY, NAV,
                     parse_hex, tx_payload)

logger = logging.getLogger(__name__)

SendFn = Callable[[int, Dict[str, Any]], bool]

# Identification reply to 00 12 58 8F (bytes after 00 58 12 9F). nav_emu.py
# default = the 2026-10-14 English set; IDENT_NL = run1 2026-10-07.
IDENT_EN = "02 F9 D1 13 0A 58 A6 9A 80 80"
IDENT_NL = "02 F9 D1 3D 0A 58 A6 E4 80 8C"

ROLL_CALL_REPLY = "00 01 12 10 58 24 85"                     # "I host logicals 58, 24, 85"
ROLL_CALL_1_REPLY = "00 01 12 12 58 28 29 32 60 74 A4 56"
LIVE_ANNOUNCE = "01 12 13 58 24 85"                          # 178->1FF, real nav 2026-10-14 15:07:09

#: Gateway auto-reply slots (fw 3.2.0), exactly as nav_emu.py --fw-auto installs them.
AUTO_SLOTS = (
    (0, "12 01 00", ROLL_CALL_REPLY),
    (1, "12 01 01", ROLL_CALL_1_REPLY),
)

SOFT_BUTTONS = {0x01: "maintenance", 0x08: "calendar", 0x40: "language"}


def bcd(n: int) -> str:
    return "%02X" % ((n // 10) * 16 + n % 10)


def _hex(data: tuple) -> str:
    return " ".join(f"{b:02X}" for b in data)


class NavEmulator:
    """Stand-in for the nav ECU. Thread-safe: frames may arrive on the serial
    reader thread while timers fire on the scheduler thread."""

    #: Seconds without any AVC frame after which the bus counts as asleep.
    BUS_IDLE_S = 10.0
    #: How long after the bus wakes up (first display frame) a missing roll
    #: call / missing display→178 traffic triggers the live announcement.
    ANNOUNCE_AFTER_S = 3.0
    ANNOUNCE_RETRY_S = 5.0
    ANNOUNCE_MAX = 3
    RC_RETRIES = 3

    def __init__(
        self,
        send: SendFn,
        scheduler,
        *,
        lang: str = "02",
        ident: str = IDENT_EN,
        time_fmt: str = "new",
        burst: bool = True,
        btn_ack: bool = True,
        ack_release: bool = True,
        clock: Callable[[], float] = time.localtime,
        on_status: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._send = send
        self._sched = scheduler
        self.lang = lang
        self.ident = ident
        self.time_fmt = time_fmt
        self.burst = burst
        self.btn_ack = btn_ack
        self.ack_release = ack_release
        self._localtime = clock
        self._on_status = on_status
        self._lock = threading.RLock()

        self.screen: Optional[int] = None     # display's active screen (12 01 60 XX)
        self.periodic_on = False
        self.configured = False
        self.enabled = True
        self.n_tx = 0
        self.n_echo = 0
        self.rc_retries = 0
        self.log: List[str] = []             # last notes (bounded)

        # Bus session tracking (live-announce logic).
        self._last_frame_t: Optional[float] = None
        self._session_start: Optional[float] = None
        self._rollcall_seen = False
        self._display_talks = False
        self._announces = 0
        self._last_announce: float = 0.0
        self._tick_running = False

    # ── io ────────────────────────────────────────────────────────────────

    def _tx(self, slave: int, data: str, bcast: bool = False, delay: float = 0.0) -> None:
        def go() -> None:
            if not self.enabled:
                return
            payload = tx_payload(NAV, slave, data, broadcast=bcast)
            if self._send(2, payload):
                self.n_tx += 1

        if delay:
            self._sched.call_later(delay, go)
        else:
            go()

    def _note(self, msg: str) -> None:
        logger.info("nav-emu: %s", msg)
        self.log.append(msg)
        del self.log[:-50]

    def _status(self) -> None:
        if self._on_status is not None:
            try:
                self._on_status(self.status())
            except Exception:
                logger.exception("nav-emu status callback failed")

    def status(self) -> Dict[str, Any]:
        return {
            "configured": self.configured,
            "screen": f"{self.screen:02X}" if self.screen is not None else None,
            "nav_screen": self.screen == 0x58,
            "registered": self._display_talks,
            "tx": self.n_tx,
            "announces": self._announces,
        }

    # ── gateway configuration ─────────────────────────────────────────────

    def configure(self) -> None:
        """Install auto-reply slots + 178 ACK on the gateway (nav_emu.py
        ``--fw-auto --fw-ack`` order: slots first, then avc_cfg ack). Called
        on every (re)connect and on GATEWAY_READY."""
        with self._lock:
            for slot, match, reply in AUTO_SLOTS:
                self._send(2, {
                    "a": "auto", "slot": slot, "on": True,
                    "m": f"{DISPLAY:03X}", "s": f"{BCAST_FFF:03X}",
                    "d": [f"{b:02X}" for b in parse_hex(match)],
                    "tx": tx_payload(NAV, DISPLAY, reply),
                })
            self._send(0, {"a": "avc_cfg", "ack": f"{NAV:03X}"})
            self.configured = True
            self.rc_retries = 0
            # A fresh connection: re-evaluate whether the display knows us.
            self._session_start = None
            self._last_frame_t = None
            self._rollcall_seen = False
            self._display_talks = False
            self._announces = 0
            self._note("gateway configured (auto slots 0/1, ack 178)")
            self._start_tick()
        self._status()

    def deconfigure(self) -> None:
        """Undo :meth:`configure` (nav_emu.py does this at exit)."""
        with self._lock:
            if not self.configured:
                return
            self._send(0, {"a": "avc_cfg", "ack": "000"})
            for slot, _m, _r in AUTO_SLOTS:
                self._send(2, {"a": "auto", "slot": slot, "on": False})
            self.configured = False
            self._note("gateway deconfigured")

    # ── behaviour (port of nav_emu.Emu) ───────────────────────────────────

    def time_reply(self) -> str:
        lt = self._localtime()
        hh, mm, ss = bcd(lt.tm_hour), bcd(lt.tm_min), bcd(lt.tm_sec)
        return ("00 03 01 " if self.time_fmt == "old" else "00 01 01 ") + f"{hh} {mm} {ss} FF"

    def _start_periodic(self) -> None:
        if self.periodic_on:
            return
        self.periodic_on = True
        self._note("periodic heartbeats on")

        def beat() -> None:
            if not self.enabled:
                self.periodic_on = False
                return
            # Only while the display is on the nav screen (run1_nav; emu6 2026-10-14:
            # otherwise the display asks 00 12 58 43 02 00 every second).
            if self.screen == 0x58:
                self._tx(BCAST_1FF, "58 31 F1 02 00", bcast=True)
                self._tx(BCAST_FFF, "58 01 DB 00 00 00 00 00 00", bcast=True, delay=0.6)
            self._sched.call_later(5.0, beat)

        self._sched.call_later(1.0, beat)

    def _initial_burst(self) -> None:
        if not self.burst:
            return
        self._tx(DISPLAY, "00 58 32 E0")
        self._tx(CLIMATE, "00 85 E0 E0", delay=0.003)
        self._tx(DISPLAY, "00 58 56 E0", delay=0.007)
        self._tx(DISPLAY, "00 58 34 E0", delay=0.010)
        self._tx(CLIMATE, "00 85 E0 E4", delay=0.016)
        self._tx(CLIMATE, "00 85 E0 E6", delay=0.019)
        for k in range(1, 4):                       # repeated 3x at 300 ms in run 1
            self._tx(DISPLAY, "00 58 32 E0", delay=0.3 * k + 0.05)
            self._tx(DISPLAY, "00 58 56 E0", delay=0.3 * k + 0.053)
            self._tx(DISPLAY, "00 58 34 E0", delay=0.3 * k + 0.057)

    def announce(self) -> None:
        """Live-bus (re)announcement, as the real nav did once booted."""
        self._tx(BCAST_1FF, LIVE_ANNOUNCE, bcast=True)
        self._announces += 1
        self._last_announce = self._sched.now()
        self._note(f"announce 178->1FF {LIVE_ANNOUNCE} (#{self._announces})")

    # ── input ─────────────────────────────────────────────────────────────

    def on_gateway_line(self, obj: Dict[str, Any]) -> None:
        """Feed every parsed gateway JSON line (called on the reader thread)."""
        if not isinstance(obj, dict):
            return
        dev = obj.get("id")
        d = obj.get("d")
        if dev == 0 and isinstance(d, dict):
            msg = str(d.get("msg", ""))
            if "GATEWAY_READY" in msg and self.configured:
                # Gateway rebooted: its RAM tables are gone.
                self._note("GATEWAY_READY -> reconfigure")
                self.configure()
            return
        if dev != 2 or not isinstance(d, dict):
            return
        frame = AvcFrame.from_gateway(d)
        if frame is not None:
            self.on_frame(frame)

    def on_frame(self, f: AvcFrame) -> None:
        with self._lock:
            if not self.enabled:
                return
            now = self._sched.now()
            if self._last_frame_t is None or now - self._last_frame_t > self.BUS_IDLE_S:
                # Bus (re)awake: new session.
                self._session_start = now
                self._rollcall_seen = False
                self._display_talks = False
                self._announces = 0
            self._last_frame_t = now

            if f.echo:
                self.n_echo += 1
                # Roll-call answer not acknowledged by the display: resend
                # (2026-10-14 cold start: first answer NAKed, never retried).
                if f.nak and f.master == NAV and _hex(f.data) == ROLL_CALL_REPLY \
                        and self.rc_retries < self.RC_RETRIES:
                    self.rc_retries += 1
                    self._note(f"roll-call answer NAKed -> retry {self.rc_retries}")
                    self._tx(DISPLAY, ROLL_CALL_REPLY)
                return
            if f.master != DISPLAY:
                return
            if f.slave == NAV:
                self._display_talks = True
            self._handle(f)

    def _handle(self, f: AvcFrame) -> None:
        b = f.data
        data = _hex(b)
        if data.startswith("12 01 60 ") and len(b) >= 4:
            if b[3] != self.screen:
                self.screen = b[3]
                self._note(f"display screen -> {self.screen:02X}")
                self._status()
        # 60 s re-registration ping 12 01 20 XX: answer 00 01 12 30 XX FF (nav)
        if data.startswith("12 01 20 ") and len(b) == 4:
            self._tx(DISPLAY, "00 01 12 30 %02X FF" % b[3])
            return
        if data == "12 01 00":
            self._rollcall_seen = True
            self._note("roll call 12 01 00 (gateway auto-reply slot 0 answers)")
            return
        if data == "12 01 01":
            return                                  # gateway auto-reply slot 1 answers
        if data == "00 01 07":
            self._tx(DISPLAY, "00 01 00 17")
            return
        if f.slave != NAV:
            return
        # ── unicast to 178 ──
        if data == "00 12 58 8F":                   # identification, polled every 30 s
            self._tx(DISPLAY, "00 58 12 9F " + self.ident)
        elif data.startswith("00 12 01 02 17 80"):
            self._tx(DISPLAY, "00 01 12 12 27 58 40 64 62 61 55 6E 6D")
        elif data.startswith("00 12 01 02 17 81"):
            self._tx(DISPLAY, "00 01 12 12 34 A2 58 43 63 65 44 45 E0")
        elif data.startswith("00 12 01 02 11 00 17 82"):
            self._note("setup done -> initial burst")
            self._initial_burst()
        elif data == "00 32 58 E0":
            self._tx(DISPLAY, "00 58 32 F0 C9 00 00 01")
        elif data == "00 6E 58 E0":
            self._tx(DISPLAY, "00 58 6E F0 C9 00 00 01")
        elif data.startswith("00 56 58 F0"):        # settings query -> UI language B7 LL
            self._tx(DISPLAY, "00 58 56 B7 %s" % self.lang)
        elif data == "00 32 58 F0 00 00 00 02":     # run1 nav answered this with B7 as well
            self._tx(DISPLAY, "00 58 56 B7 %s" % self.lang)
        elif data == "00 56 58 8E":                 # time request (5 s on the nav screen)
            self._tx(DISPLAY, "00 58 56 9E " + self.time_reply())
        elif data == "00 12 58 42 02 01":           # screen request -> grant
            self._tx(DISPLAY, "00 58 12 52 02 01")
            self._tx(BCAST_1FF, "58 31 F1 02 00", bcast=True, delay=0.004)
            self.screen = 0x58
            self._start_periodic()
            self._status()
        elif data == "00 12 58 43 02 00":           # screen release request
            if self.ack_release:
                self._tx(DISPLAY, "00 58 12 53 02 00")
                self._tx(BCAST_1FF, "58 31 F1 00 00", bcast=True, delay=0.002)
            self.screen = None
            self._status()
        elif data.startswith("00 25 58 80") and len(b) >= 5:   # info soft buttons
            self._note("soft button %02X (%s)" % (b[4], SOFT_BUTTONS.get(b[4], "?")))
            if self.btn_ack:
                self._tx(DISPLAY, "00 58 12 50 02 01")
        elif data.startswith("00 25 58 84") and len(b) >= 5:   # hardware buttons
            if b[4] != 0 and self.btn_ack:
                self._tx(DISPLAY, "00 58 12 50 02 01", delay=0.004)

    # ── live-bus announcement supervisor ──────────────────────────────────

    def _start_tick(self) -> None:
        if self._tick_running:
            return
        self._tick_running = True

        def tick() -> None:
            try:
                self.tick()
            finally:
                if self.enabled:
                    self._sched.call_later(0.5, tick)
                else:
                    self._tick_running = False

        self._sched.call_later(0.5, tick)

    def tick(self) -> None:
        with self._lock:
            if not (self.enabled and self.configured) or self._session_start is None:
                return
            now = self._sched.now()
            if self._last_frame_t is not None and now - self._last_frame_t > self.BUS_IDLE_S:
                return                                  # bus asleep
            if self._rollcall_seen or self._display_talks:
                return                                  # cold start handled / display knows us
            if now - self._session_start < self.ANNOUNCE_AFTER_S:
                return
            if self._announces >= self.ANNOUNCE_MAX:
                return
            if self._announces and now - self._last_announce < self.ANNOUNCE_RETRY_S:
                return
            self.announce()
