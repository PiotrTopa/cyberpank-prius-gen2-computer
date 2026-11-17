"""
Gateway AVC-LAN "personality" (gateway firmware 3.3.0+): rule compiler and the
nav (178) rule set.

The gateway can answer another unit's dialogue on its own from a rule table
stored in its flash (``gateway-rp2350/src/avc_pers.h`` is the reference for
the binary format; ``cyberpunk-prius-gateway/PROTOCOL.md`` for the commands).
This module turns :class:`Rule` / :class:`Periodic` objects into the binary
blobs the firmware stores, and builds the nav ECU personality: the same
replies, delays and conditions as :class:`~.nav_emulator.NavEmulator`, so the
display's whole start-up dialogue is answered by the gateway from power-on,
before the backend has connected.

Run as a script to print the blobs (used to regenerate the firmware repo's
test fixtures)::

    python -m cyberpunk_computer.comm.avc.personality --lang 02 --time-fmt new
"""

from __future__ import annotations

import argparse
import zlib
from dataclasses import dataclass, field
from typing import Iterable, List, Sequence, Tuple

from .frames import BCAST_1FF, BCAST_FFF, CLIMATE, CTRL_DEFAULT, DISPLAY, NAV, parse_hex

FMT = 1

# Limits of the 3.3.0 firmware (reported by {"a":"pers"} as "cap").
MAX_RULES = 32
MAX_BYTES = 3072
MAX_FRAMES = 16
MAX_DATA = 24
MAX_PREFIX = 12
MAX_PATCH = 4
MAX_DELAY_MS = 10000

T_MATCH = 0
T_PERIODIC = 1
F_STOP = 0x01
F_RETRY_NAK = 0x02

ANY = 0xFFFF

OP_ECHO = 0x40          # | request byte index
OP_HOUR = 0x81
OP_MIN = 0x82
OP_SEC = 0x83

# State flags of the nav personality.
S_SCREEN = 0x01          # display shows screen 58 (granted by us)
S_GRANTED = 0x02         # a screen grant happened in this bus session (heartbeat phase runs)

# Nav replies (ground truth: captures/2026-10-07/NAV_EMULATION_SPEC.md, the
# 2026-10-14 sessions the real display accepted).
IDENT_EN = "02 F9 D1 13 0A 58 A6 9A 80 80"
IDENT_NL = "02 F9 D1 3D 0A 58 A6 E4 80 8C"
ROLL_CALL_REPLY = "00 01 12 10 58 24 85"                     # "I host logicals 58, 24, 85"
ROLL_CALL_1_REPLY = "00 01 12 12 58 28 29 32 60 74 A4 56"


@dataclass(frozen=True)
class Frame:
    """One reply frame. ``patches``: (data position, op) pairs."""

    slave: int
    data: Tuple[int, ...]
    delay_ms: int = 0
    broadcast: bool = False
    master: int = NAV
    ctrl: int = CTRL_DEFAULT
    patches: Tuple[Tuple[int, int], ...] = ()

    def encode(self) -> bytes:
        if not (0 <= self.master <= 0xFFF and 0 <= self.slave <= 0xFFF):
            raise ValueError("address out of range")
        if len(self.data) > MAX_DATA or len(self.patches) > MAX_PATCH:
            raise ValueError("frame too long")
        if not 0 <= self.delay_ms <= MAX_DELAY_MS:
            raise ValueError("delay out of range")
        out = bytearray()
        out += self.master.to_bytes(2, "big") + self.slave.to_bytes(2, "big")
        out.append((self.ctrl & 0x0F) | (0x80 if self.broadcast else 0))
        out += self.delay_ms.to_bytes(2, "big")
        out.append(len(self.data))
        out.append(len(self.patches))
        for pos, op in self.patches:
            if not 0 <= pos < len(self.data):
                raise ValueError("patch position out of range")
            out += bytes((pos, op))
        out += bytes(self.data)
        return bytes(out)


def frame(slave: int, data: str, delay_ms: int = 0, broadcast: bool = False,
          patches: Sequence[Tuple[int, int]] = ()) -> Frame:
    return Frame(slave=slave, data=parse_hex(data), delay_ms=delay_ms,
                 broadcast=broadcast, patches=tuple(patches))


@dataclass(frozen=True)
class Rule:
    """Match rule: a received frame (master, slave, data prefix, length
    bounds, state condition) -> state change + reply frames."""

    prefix: Tuple[int, ...]
    frames: Tuple[Frame, ...] = ()
    master: int = DISPLAY
    slave: int = ANY
    lmin: int = 0
    lmax: int = 0xFF
    need_set: int = 0
    need_clr: int = 0
    set: int = 0
    clr: int = 0
    stop: bool = True
    retry_nak: bool = False
    name: str = ""

    def encode(self) -> bytes:
        if len(self.prefix) > MAX_PREFIX or len(self.frames) > MAX_FRAMES:
            raise ValueError(f"rule {self.name}: too many prefix bytes / frames")
        lmin = max(self.lmin, 0)
        flags = (F_STOP if self.stop else 0) | (F_RETRY_NAK if self.retry_nak else 0)
        out = bytearray((T_MATCH, flags))
        out += self.master.to_bytes(2, "big") + self.slave.to_bytes(2, "big")
        out += bytes((lmin, self.lmax, self.need_set, self.need_clr, self.set, self.clr,
                      len(self.prefix)))
        out += bytes(self.prefix)
        out.append(len(self.frames))
        for f in self.frames:
            out += f.encode()
        return bytes(out)


@dataclass(frozen=True)
class Periodic:
    """Periodic rule: runs while ``(state & run_set) == run_set`` (phase starts
    when that becomes true); each beat sends ``frames`` if
    ``(state & send_set) == send_set``."""

    run_set: int
    send_set: int
    first_ms: int
    period_ms: int
    frames: Tuple[Frame, ...] = ()
    name: str = ""

    def encode(self) -> bytes:
        if not self.run_set:
            raise ValueError("a periodic rule must depend on a state flag")
        out = bytearray((T_PERIODIC, 0, self.run_set, self.send_set))
        out += self.first_ms.to_bytes(2, "big") + self.period_ms.to_bytes(2, "big")
        out.append(len(self.frames))
        for f in self.frames:
            if any((op & 0xC0) == OP_ECHO for _, op in f.patches):
                raise ValueError("echo patch in a periodic rule")
            out += f.encode()
        return bytes(out)


def body_crc(blobs: Iterable[bytes]) -> int:
    """CRC the gateway reports as ``crc`` / ``stored.crc``: zlib CRC-32 over
    every rule as (u16 length big endian, blob)."""
    crc = 0
    for b in blobs:
        crc = zlib.crc32(len(b).to_bytes(2, "big") + b, crc)
    return crc & 0xFFFFFFFF


def check_capacity(blobs: Sequence[bytes]) -> None:
    if len(blobs) > MAX_RULES or sum(len(b) for b in blobs) > MAX_BYTES:
        raise ValueError("personality exceeds the gateway's rule table")


# ── the nav ECU (178) personality ───────────────────────────────────────────

def burst_frames() -> Tuple[Frame, ...]:
    """Initial burst after setup (NavEmulator._initial_burst, run1 timings)."""
    fr = [
        frame(DISPLAY, "00 58 32 E0", 0),
        frame(CLIMATE, "00 85 E0 E0", 3),
        frame(DISPLAY, "00 58 56 E0", 7),
        frame(DISPLAY, "00 58 34 E0", 10),
        frame(CLIMATE, "00 85 E0 E4", 16),
        frame(CLIMATE, "00 85 E0 E6", 19),
    ]
    for k in range(1, 4):                       # repeated 3x at 300 ms in run 1
        fr += [frame(DISPLAY, "00 58 32 E0", 300 * k + 50),
               frame(DISPLAY, "00 58 56 E0", 300 * k + 53),
               frame(DISPLAY, "00 58 34 E0", 300 * k + 57)]
    return tuple(fr)


def nav_rules(lang: str = "02", ident: str = IDENT_EN, time_fmt: str = "new",
              burst: bool = True, btn_ack: bool = True, ack_release: bool = True) -> List:
    """The nav personality, rule for rule what NavEmulator._handle does.

    Order matters: rules are tried in order and each one stops the search
    (``F_STOP``), like the emulator's if/elif chain.
    """
    to178 = dict(slave=NAV)
    time_hdr = "00 03 01" if time_fmt == "old" else "00 01 01"
    rules: List = [
        # Roll call: new session (all flags cleared), answer at once; a NAKed
        # answer is resent up to 3 times (2026-10-14 cold start).
        Rule(parse_hex("12 01 00"), (frame(DISPLAY, ROLL_CALL_REPLY),), lmin=3, lmax=3,
             clr=0xFF, retry_nak=True, name="rollcall"),
        Rule(parse_hex("12 01 01"), (frame(DISPLAY, ROLL_CALL_1_REPLY),), lmin=3, lmax=3,
             name="rollcall1"),
        # Display screen broadcast 12 01 60 XX: track screen 58 (heartbeats).
        Rule(parse_hex("12 01 60 58"), (), need_set=S_GRANTED, set=S_SCREEN, name="screen58"),
        Rule(parse_hex("12 01 60"), (), lmin=4, clr=S_SCREEN, name="screen_other"),
        # 60 s re-registration ping 12 01 20 XX -> 00 01 12 30 XX FF
        Rule(parse_hex("12 01 20"), (frame(DISPLAY, "00 01 12 30 00 FF", patches=[(4, OP_ECHO | 3)]),),
             lmin=4, lmax=4, name="ping"),
        Rule(parse_hex("00 01 07"), (frame(DISPLAY, "00 01 00 17"),), lmin=3, lmax=3, name="sys07"),
        # ── unicast to 178 ──
        Rule(parse_hex("00 12 58 8F"), (frame(DISPLAY, "00 58 12 9F " + ident),), lmin=4, lmax=4,
             name="ident", **to178),
        Rule(parse_hex("00 12 01 02 17 80"), (frame(DISPLAY, "00 01 12 12 27 58 40 64 62 61 55 6E 6D"),),
             name="setup80", **to178),
        Rule(parse_hex("00 12 01 02 17 81"), (frame(DISPLAY, "00 01 12 12 34 A2 58 43 63 65 44 45 E0"),),
             name="setup81", **to178),
        Rule(parse_hex("00 12 01 02 11 00 17 82"), burst_frames() if burst else (), name="setup82", **to178),
        Rule(parse_hex("00 32 58 E0"), (frame(DISPLAY, "00 58 32 F0 C9 00 00 01"),), lmin=4, lmax=4,
             name="q32", **to178),
        Rule(parse_hex("00 6E 58 E0"), (frame(DISPLAY, "00 58 6E F0 C9 00 00 01"),), lmin=4, lmax=4,
             name="q6E", **to178),
        Rule(parse_hex("00 56 58 F0"), (frame(DISPLAY, "00 58 56 B7 " + lang),), name="lang", **to178),
        Rule(parse_hex("00 32 58 F0 00 00 00 02"), (frame(DISPLAY, "00 58 56 B7 " + lang),), lmin=8, lmax=8,
             name="lang32", **to178),
        # Time: gateway clock (the backend sets it on connect; until then it
        # counts from 00:00:00 at gateway power-on, as the real nav did before
        # its GPS fix: 2026-10-14 "00 01 01 00 00 17").
        Rule(parse_hex("00 56 58 8E"),
             (frame(DISPLAY, f"00 58 56 9E {time_hdr} 00 00 00 FF",
                    patches=[(7, OP_HOUR), (8, OP_MIN), (9, OP_SEC)]),),
             lmin=4, lmax=4, name="time", **to178),
        # Screen request -> grant + "my screen is 58"; heartbeats start.
        Rule(parse_hex("00 12 58 42 02 01"),
             (frame(DISPLAY, "00 58 12 52 02 01"), frame(BCAST_1FF, "58 31 F1 02 00", 4, broadcast=True)),
             lmin=6, lmax=6, set=S_SCREEN | S_GRANTED, name="grant", **to178),
        Rule(parse_hex("00 12 58 43 02 00"),
             (frame(DISPLAY, "00 58 12 53 02 00"), frame(BCAST_1FF, "58 31 F1 00 00", 2, broadcast=True))
             if ack_release else (),
             lmin=6, lmax=6, clr=S_SCREEN, name="release", **to178),
    ]
    if btn_ack:
        rules += [
            Rule(parse_hex("00 25 58 80"), (frame(DISPLAY, "00 58 12 50 02 01"),), lmin=5,
                 name="softbtn", **to178),
            Rule(parse_hex("00 25 58 84 00"), (), name="hwbtn_release", **to178),
            Rule(parse_hex("00 25 58 84"), (frame(DISPLAY, "00 58 12 50 02 01", 4),), lmin=5,
                 name="hwbtn", **to178),
        ]
    # Heartbeats every 5 s while screen 58 is shown (first one 1 s after the
    # first grant of the session, DB 0.6 s after F1), NavEmulator._start_periodic.
    rules.append(Periodic(run_set=S_GRANTED, send_set=S_SCREEN, first_ms=1000, period_ms=5000,
                          frames=(frame(BCAST_1FF, "58 31 F1 02 00", 0, broadcast=True),
                                  frame(BCAST_FFF, "58 01 DB 00 00 00 00 00 00", 600, broadcast=True)),
                          name="heartbeat"))
    return rules


def nav_blobs(**kw) -> List[bytes]:
    blobs = [r.encode() for r in nav_rules(**kw)]
    check_capacity(blobs)
    return blobs


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--lang", default="02")
    ap.add_argument("--ident", choices=("en", "nl"), default="en")
    ap.add_argument("--time-fmt", choices=("new", "old"), default="new")
    a = ap.parse_args(argv)
    blobs = nav_blobs(lang=a.lang, ident=IDENT_EN if a.ident == "en" else IDENT_NL, time_fmt=a.time_fmt)
    print(f"# nav personality lang={a.lang} ident={a.ident} time={a.time_fmt}: "
          f"{len(blobs)} rules, {sum(map(len, blobs))} bytes, crc {body_crc(blobs):08X}")
    for b in blobs:
        print(b.hex().upper())


if __name__ == "__main__":
    main()
