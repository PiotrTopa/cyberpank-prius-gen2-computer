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

Gateway personality (firmware 3.3.0+)
-------------------------------------
The gateway can answer the whole dialogue itself from rules in its flash
(:mod:`.personality`), from power-on, before this backend is connected. On
every connect the emulator asks ``whoami``; with firmware >= 3.3.0 it

1. fetches the early-frame buffer (frames the gateway received before the
   host connected) and folds it into its session state (roll call seen and
   answered, display talking to 178, current screen) without answering any of
   them (they are stale);
2. reads the stored personality (``{"a":"pers"}``) and compares its CRC, rule
   count, enable flag and ACK address with :func:`.personality.nav_blobs` for
   the configured language; only when they differ it writes the rules and
   saves them (one flash write per change, never at every connect);
3. once verified: clears the two RAM auto-reply slots (the personality
   answers the roll call), sets the gateway clock (time reply) and resyncs it
   every 10 min.

No double answers: a frame the personality answered arrives tagged ``"au":1``
and is never answered here (state is still tracked); an ``au`` echo is a
frame the gateway sent (no NAK retry here, the firmware retries itself); a
screen grant answered by the gateway means the gateway owns the 5 s
heartbeats. Untagged requests are answered as before. Older firmware (3.2.x)
gets exactly the previous behaviour.

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
from . import personality as _pers

logger = logging.getLogger(__name__)

SendFn = Callable[[int, Dict[str, Any]], bool]

# Identification reply to 00 12 58 8F (bytes after 00 58 12 9F). nav_emu.py
# default = the 2026-10-14 English set; IDENT_NL = run1 2026-10-07.
IDENT_EN = _pers.IDENT_EN
IDENT_NL = _pers.IDENT_NL

ROLL_CALL_REPLY = _pers.ROLL_CALL_REPLY                      # "I host logicals 58, 24, 85"
ROLL_CALL_1_REPLY = _pers.ROLL_CALL_1_REPLY
LIVE_ANNOUNCE = "01 12 13 58 24 85"                          # 178->1FF, real nav 2026-10-14 15:07:09

#: Gateway auto-reply slots (fw 3.2.0), exactly as nav_emu.py --fw-auto installs them.
AUTO_SLOTS = (
    (0, "12 01 00", ROLL_CALL_REPLY),
    (1, "12 01 01", ROLL_CALL_1_REPLY),
)

SOFT_BUTTONS = {0x01: "maintenance", 0x08: "calendar", 0x40: "language"}

#: First gateway firmware with the stored personality and the early-frame buffer.
PERSONALITY_MIN_VER = (3, 3, 0)


def parse_version(v: Any) -> Optional[tuple]:
    try:
        return tuple(int(x) for x in str(v).split(".")[:3])
    except (TypeError, ValueError):
        return None


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
    #: Gateway clock resync period while the personality is active.
    CLOCK_RESYNC_S = 600.0

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
        use_personality: bool = True,
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
        self.use_personality = use_personality
        self._desired = _pers.nav_blobs(lang=lang, ident=ident, time_fmt=time_fmt, burst=burst,
                                        btn_ack=btn_ack, ack_release=ack_release)
        self._desired_crc = _pers.body_crc(self._desired)

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

        # Gateway firmware / personality (3.3.0+).
        self.fw_ver: Optional[tuple] = None
        self.pers = "unknown"        # unknown|unsupported|checking|writing|active|ram_only|failed|disabled
        self.pers_status: Optional[Dict[str, Any]] = None
        self.early: Optional[Dict[str, Any]] = None     # last early-buffer summary
        self.early_rollcall_ms: Optional[int] = None    # gateway uptime of the roll call (early buffer)
        self._pers_writes = 0
        self._fw_periodic = False    # the gateway sends the 5 s heartbeats
        self._suppress = False       # handling a frame the gateway already answered
        self._screen_live = False    # screen known from a live frame (early frames must not override)
        self._last_clock: Optional[float] = None

    # ── io ────────────────────────────────────────────────────────────────

    def _tx(self, slave: int, data: str, bcast: bool = False, delay: float = 0.0) -> None:
        if self._suppress:          # the gateway's personality answered this request
            return

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
            "fw": ".".join(map(str, self.fw_ver)) if self.fw_ver else None,
            "personality": self.pers,
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
            # Feature detection: IDENT carries the firmware version (3.3.0+:
            # stored personality, early-frame buffer). Every version answers it.
            self._send(0, {"a": "whoami"})
            self.configured = True
            self.rc_retries = 0
            self.fw_ver = None
            self.pers = "unknown"
            self.pers_status = None
            self._pers_writes = 0
            self._fw_periodic = False
            self._screen_live = False
            self._last_clock = None
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
            if self.pers in ("active", "ram_only"):
                # The gateway is the nav now (rules in its flash, answered from
                # power-on): keep its 178 ACK, without it the display's unicasts die.
                for slot, _m, _r in AUTO_SLOTS:
                    self._send(2, {"a": "auto", "slot": slot, "on": False})
                self.configured = False
                self._note("gateway deconfigured (personality and ACK 178 stay in the gateway)")
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
            # otherwise the display asks 00 12 58 43 02 00 every second), and
            # not when the gateway's personality sends them.
            if self.screen == 0x58 and not self._fw_periodic:
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
            if msg in ("IDENT", "GATEWAY_READY", "GW_HB") and "ver" in d:
                self._on_version(d.get("ver"))
            return
        if dev != 2 or not isinstance(d, dict):
            return
        if "m" in d:
            frame = AvcFrame.from_gateway(d)
            if frame is not None:
                self.on_frame(frame)
        elif isinstance(d.get("q"), dict):
            frame = AvcFrame.from_gateway(d["q"])
            if frame is not None:
                self.on_early_frame(frame, obj.get("ts"))
        elif isinstance(d.get("pers"), dict):
            self._on_pers_status(d["pers"])
        elif "pers_save" in d:
            self._on_pers_save(d)
        elif "pers_set" in d and d.get("ok") is False:
            with self._lock:
                self.pers = "failed"
                self._note(f"personality rule {d.get('pers_set')} rejected (err {d.get('err')}): host answers")
            self._status()
        elif isinstance(d.get("early"), dict):
            with self._lock:
                self.early = d["early"]
                rc = self.early_rollcall_ms
                self._note(f"early buffer: {d['early'].get('n')} frames, {d['early'].get('lost')} lost, "
                           f"gateway now {d['early'].get('now')} ms"
                           + (f", roll call at {rc} ms" if rc is not None else ", no roll call"))

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
                # An "au" echo was sent by the gateway's personality, which
                # retries on its own.
                if f.nak and not f.auto and f.master == NAV and _hex(f.data) == ROLL_CALL_REPLY \
                        and self.rc_retries < self.RC_RETRIES:
                    self.rc_retries += 1
                    self._note(f"roll-call answer NAKed -> retry {self.rc_retries}")
                    self._tx(DISPLAY, ROLL_CALL_REPLY)
                return
            if f.master != DISPLAY:
                return
            if f.slave == NAV:
                self._display_talks = True
            self._suppress = f.auto
            try:
                self._handle(f)
            finally:
                self._suppress = False

    def _handle(self, f: AvcFrame) -> None:
        b = f.data
        data = _hex(b)
        if data.startswith("12 01 60 ") and len(b) >= 4:
            self._screen_live = True
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
            self._note("roll call 12 01 00 (gateway answers: %s)"
                       % ("personality" if f.auto else "auto-reply slot 0"))
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
            self._screen_live = True
            if f.auto:
                self._fw_periodic = True            # the gateway's periodic rule beats
            else:
                self._start_periodic()
            self._status()
        elif data == "00 12 58 43 02 00":           # screen release request
            if self.ack_release:
                self._tx(DISPLAY, "00 58 12 53 02 00")
                self._tx(BCAST_1FF, "58 31 F1 00 00", bcast=True, delay=0.002)
            self.screen = None
            self._screen_live = True
            self._status()
        elif data.startswith("00 25 58 80") and len(b) >= 5:   # info soft buttons
            self._note("soft button %02X (%s)" % (b[4], SOFT_BUTTONS.get(b[4], "?")))
            if self.btn_ack:
                self._tx(DISPLAY, "00 58 12 50 02 01")
        elif data.startswith("00 25 58 84") and len(b) >= 5:   # hardware buttons
            if b[4] != 0 and self.btn_ack:
                self._tx(DISPLAY, "00 58 12 50 02 01", delay=0.004)

    # ── gateway personality (fw 3.3.0+) ────────────────────────────────────

    def _on_version(self, ver: Any) -> None:
        v = parse_version(ver)
        with self._lock:
            if v is None or self.fw_ver is not None or not self.configured:
                return
            self.fw_ver = v
            if v < PERSONALITY_MIN_VER:
                self.pers = "unsupported"
                self._note(f"gateway {ver}: no personality (host answers, RAM auto-reply slots)")
                return
            # Frames the gateway kept while we were not connected, then the stored rules.
            self._send(2, {"a": "early"})
            self.pers = "checking"
            self._send(2, {"a": "pers"})
        self._status()

    def _desired_matches(self, st: Dict[str, Any], stored: bool) -> bool:
        src = (st.get("stored") or {}) if stored else st
        try:
            crc = int(str(src.get("crc")), 16)
        except (TypeError, ValueError):
            return False
        ok = crc == self._desired_crc and src.get("n") == len(self._desired) and bool(src.get("on"))
        if stored:
            ok = ok and str(src.get("ack", "")).upper() == f"{NAV:03X}"
        return ok

    def _on_pers_status(self, st: Dict[str, Any]) -> None:
        with self._lock:
            self.pers_status = st
            if self.pers in ("unknown", "unsupported") or not self.configured:
                return
            ram_ok = self._desired_matches(st, stored=False)
            stored_ok = self._desired_matches(st, stored=True)
            if not self.use_personality:
                if st.get("active"):
                    self._send(2, {"a": "pers", "op": "enable", "on": False})     # RAM only
                    self._note("gateway personality disabled for this session (use_personality=False)")
                self.pers = "disabled"
            elif ram_ok and stored_ok:
                if self.pers != "active":
                    self.pers = "active"
                    self._fw_periodic = True
                    # One source of truth: the personality answers the roll call.
                    for slot, _m, _r in AUTO_SLOTS:
                        self._send(2, {"a": "auto", "slot": slot, "on": False})
                    self._send_clock()
                    self._note(f"gateway personality verified ({len(self._desired)} rules, "
                               f"crc {self._desired_crc:08X}, flash {st.get('flash')})")
            elif self._pers_writes >= 1:
                # Written once this connection and still not right: do not loop on flash writes.
                self.pers = "ram_only" if ram_ok and st.get("active") else "failed"
                self._note(f"gateway personality not verified after writing ({self.pers}); host answers "
                           "whatever the gateway does not tag")
            else:
                self._pers_writes += 1
                self.pers = "writing"
                if not ram_ok:
                    self._send(2, {"a": "pers", "op": "clear"})
                    for i, blob in enumerate(self._desired):
                        self._send(2, {"a": "pers", "op": "set", "i": i, "r": blob.hex().upper()})
                self._send(0, {"a": "avc_cfg", "ack": f"{NAV:03X}"})
                self._send(2, {"a": "pers", "op": "enable", "on": True})
                self._send(2, {"a": "pers", "op": "save"})
                self._note(f"gateway personality differs (ram {'ok' if ram_ok else 'differs'}, stored "
                           f"{'ok' if stored_ok else 'differs'}): writing {len(self._desired)} rules + save")
        self._status()

    def _on_pers_save(self, d: Dict[str, Any]) -> None:
        with self._lock:
            self._note(f"gateway personality save: {d.get('pers_save')} ({d.get('ms')} ms)")
            if self.configured:
                self._send(2, {"a": "pers"})                 # verify

    def _send_clock(self) -> None:
        lt = self._localtime()
        self._send(2, {"a": "pers", "op": "clock", "s": lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec})
        self._last_clock = self._sched.now()

    def on_early_frame(self, f: AvcFrame, ts: Any = None) -> None:
        """A frame from the gateway's early buffer (received before we were
        connected): session state only, never answered (it is stale)."""
        with self._lock:
            if not self.enabled:
                return
            now = self._sched.now()
            if self._last_frame_t is None:
                self._last_frame_t = now
                self._session_start = now
            if f.echo or f.master != DISPLAY:
                return
            b = f.data
            data = _hex(b)
            if f.slave == NAV:
                self._display_talks = True
            if data == "12 01 00":
                if isinstance(ts, int):
                    self.early_rollcall_ms = ts
                # Answered by the personality: the display set up the nav. Not
                # answered: leave it to the live announcement (display re-runs setup).
                if f.auto:
                    self._rollcall_seen = True
            if self._screen_live:
                return
            if data.startswith("12 01 60 ") and len(b) >= 4:
                self.screen = b[3]
            elif data == "00 12 58 42 02 01" and f.auto:
                self.screen = 0x58
                self._fw_periodic = True
            elif data == "00 12 58 43 02 00" and f.auto:
                self.screen = None
        self._status()

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
            if self.enabled and self.configured and self.pers == "active" and self._last_clock is not None \
                    and self._sched.now() - self._last_clock >= self.CLOCK_RESYNC_S:
                self._send_clock()
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
