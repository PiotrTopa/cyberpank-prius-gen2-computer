"""NavEmulator: faithful port of gateway-rp2350/tools/nav_emu.py.

The strongest check replays the display's side of emulator sessions that the
real display accepted (2026-10-14) and requires our replies to equal what
nav_emu.py transmitted (``{"tx":..}`` lines), byte for byte except the
wall-clock time in the 9E reply.
"""

import time

import pytest

from cyberpunk_computer.comm.avc import AvcFrame, ManualScheduler, NavEmulator
from cyberpunk_computer.comm.avc.nav_emulator import IDENT_EN, IDENT_NL
from avc_captures import capture, records, tx_records


class Bus:
    def __init__(self):
        self.sched = ManualScheduler(start=0.0)
        self.sent = []          # (t, device_id, payload)

    def send(self, device_id, payload):
        self.sent.append((self.sched.now(), device_id, payload))
        return True

    def frames(self):
        return [(p["s"], " ".join(p["d"]), bool(p.get("b"))) for _, dev, p in self.sent
                if dev == 2 and "m" in p]


def make(**kw):
    bus = Bus()
    emu = NavEmulator(bus.send, bus.sched, **kw)
    return bus, emu


def feed(emu, bus, m, s, data, dt=0.01, **flags):
    bus.sched.advance(dt)
    emu.on_frame(AvcFrame.make(m, s, data, **flags))


def test_configure_matches_nav_emu_fw_auto_fw_ack():
    bus, emu = make()
    emu.configure()
    assert [(dev, p) for _, dev, p in bus.sent] == [
        (2, {"a": "auto", "slot": 0, "on": True, "m": "110", "s": "FFF", "d": ["12", "01", "00"],
             "tx": {"m": "178", "s": "110", "c": 15, "d": "00 01 12 10 58 24 85".split()}}),
        (2, {"a": "auto", "slot": 1, "on": True, "m": "110", "s": "FFF", "d": ["12", "01", "01"],
             "tx": {"m": "178", "s": "110", "c": 15, "d": "00 01 12 12 58 28 29 32 60 74 A4 56".split()}}),
        (0, {"a": "avc_cfg", "ack": "178"}),
    ]
    assert not any("pol" in p for _, _, p in bus.sent)      # polarity never touched
    bus.sent.clear()
    emu.deconfigure()
    assert [(dev, p) for _, dev, p in bus.sent] == [
        (0, {"a": "avc_cfg", "ack": "000"}),
        (2, {"a": "auto", "slot": 0, "on": False}),
        (2, {"a": "auto", "slot": 1, "on": False}),
    ]


def test_request_replies():
    bus, emu = make()
    emu.configure()
    bus.sent.clear()
    feed(emu, bus, 0x110, 0x178, "00 12 58 8F")
    feed(emu, bus, 0x110, 0x178, "00 56 58 F0 49 3C 44")
    feed(emu, bus, 0x110, 0xFFF, "12 01 20 3A", broadcast=True)
    feed(emu, bus, 0x110, 0x178, "00 25 58 80 40 00 00")
    feed(emu, bus, 0x110, 0x178, "00 25 58 84 04 00")
    feed(emu, bus, 0x110, 0x178, "00 25 58 84 00 00")
    assert bus.frames() == [
        ("110", "00 58 12 9F " + IDENT_EN, False),
        ("110", "00 58 56 B7 02", False),
        ("110", "00 01 12 30 3A FF", False),
        ("110", "00 58 12 50 02 01", False),
        ("110", "00 58 12 50 02 01", False),   # menu/dest/map press ack (+4 ms), not on release
    ]


def test_screen_grant_heartbeats_only_on_nav_screen_and_release():
    bus, emu = make()
    emu.configure()
    bus.sent.clear()
    feed(emu, bus, 0x110, 0x178, "00 12 58 42 02 01")
    bus.sched.advance(0.01)
    assert bus.frames()[:2] == [("110", "00 58 12 52 02 01", False), ("1FF", "58 31 F1 02 00", True)]
    bus.sent.clear()
    bus.sched.advance(1.7)                      # first beat at +1 s, DB 0.6 s later
    assert bus.frames() == [("1FF", "58 31 F1 02 00", True), ("FFF", "58 01 DB 00 00 00 00 00 00", True)]
    bus.sent.clear()
    # display leaves the nav screen: release request -> 53 + "my screen is off"
    feed(emu, bus, 0x110, 0x178, "00 12 58 43 02 00")
    bus.sched.advance(0.01)
    assert bus.frames() == [("110", "00 58 12 53 02 00", False), ("1FF", "58 31 F1 00 00", True)]
    bus.sent.clear()
    feed(emu, bus, 0x110, 0xFFF, "12 01 60 5F 05 00 05", broadcast=True)
    bus.sched.advance(11.0)                     # no heartbeats off screen 58
    assert bus.frames() == []
    feed(emu, bus, 0x110, 0xFFF, "12 01 60 58 01 00 05", broadcast=True)
    bus.sched.advance(5.0)
    assert ("1FF", "58 31 F1 02 00", True) in bus.frames()


def test_rollcall_nak_retry():
    bus, emu = make()
    emu.configure()
    bus.sent.clear()
    feed(emu, bus, 0x178, 0x110, "00 01 12 10 58 24 85", echo=True, nak=True)
    assert bus.frames() == [("110", "00 01 12 10 58 24 85", False)]


def test_live_announce_on_running_bus_only():
    # Backend (re)started while the car is on: display traffic, no roll call,
    # nothing addressed to 178 -> announce like the real nav (178->1FF).
    bus, emu = make()
    emu.configure()
    bus.sent.clear()
    for _ in range(40):                          # 4 s of energy-screen traffic
        feed(emu, bus, 0x1C6, 0x110, "00 E4 5F B9 00 00 00", dt=0.1)
    assert ("1FF", "01 12 13 58 24 85", True) in bus.frames()
    n = bus.frames().count(("1FF", "01 12 13 58 24 85", True))
    # display answers (00 12 01 03 to 178) -> no further announcements
    feed(emu, bus, 0x110, 0x178, "00 12 01 03")
    bus.sched.advance(20.0)
    assert bus.frames().count(("1FF", "01 12 13 58 24 85", True)) == n

    # Cold start: the roll call comes first -> never announce.
    bus2, emu2 = make()
    emu2.configure()
    bus2.sent.clear()
    feed(emu2, bus2, 0x110, 0xFFF, "12 01 00", broadcast=True)
    for _ in range(60):
        feed(emu2, bus2, 0x1C6, 0x110, "00 E0 5D F7 44", dt=0.1)
    assert ("1FF", "01 12 13 58 24 85", True) not in bus2.frames()


def _mask_time(data):
    # 00 58 56 9E 00 01 01 HH MM SS FF: wall clock differs between runs
    if data.startswith("00 58 56 9E"):
        t = data.split()
        t[7:10] = ["hh", "mm", "ss"]
        return " ".join(t)
    return data


@pytest.mark.parametrize("rel,kw,later", [
    # cold start, English (user: "english!") - nav_emu.py as finally accepted
    ("2026-10-14/emu11_cold_lang02.ndjson", {}, False),
    # screen switching, release ack, buttons, touch; nav_emu.py as it was at
    # 14:19 (run1 Dutch language/ident, old time header). Two replies were
    # added to nav_emu.py after this run (real nav observed 14:45-14:58):
    # "178->1FF 58 31 F1 00 00" after the release ack and the ack of the
    # info soft buttons (25 58 80). They are removed from our output here.
    ("2026-10-14/emu8_release_ack.ndjson",
     {"lang": "06", "ident": IDENT_NL, "time_fmt": "old"}, True),
])
def test_replies_equal_nav_emu_recording(rel, kw, later):
    path = capture(rel)
    bus, emu = make(**kw)
    emu.configure()
    bus.sent.clear()
    lines = list(records(path))
    t0 = lines[0][0]
    soft_ack_idx = set()
    for ht, obj in lines:
        bus.sched.advance(max(0.0, (ht - t0) - bus.sched.now()))
        d = obj.get("d")
        if obj.get("id") == 2 and isinstance(d, dict) and "m" in d:
            f = AvcFrame.from_gateway(d)
            n = len(bus.sent)
            emu.on_frame(f)
            if f.master == 0x110 and f.startswith((0x00, 0x25, 0x58, 0x80)):
                soft_ack_idx.update(range(n, len(bus.sent)))
    bus.sched.advance(1.0)
    ours = []
    for i, (_, dev, p) in enumerate(bus.sent):
        if dev != 2 or "m" not in p:
            continue
        item = (p["s"], _mask_time(" ".join(p["d"])), bool(p.get("b")))
        if later and (i in soft_ack_idx or item == ("1FF", "58 31 F1 00 00", True)):
            continue
        ours.append(item)
    recorded = [(p["s"], _mask_time(" ".join(p["d"])), bool(p.get("b")))
                for _, p in tx_records(path)]
    assert len(recorded) > 50
    assert ours == recorded
