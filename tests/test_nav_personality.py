"""Gateway personality (firmware 3.3.0+) from the backend's side.

``FakeGateway`` speaks the 3.3.0 commands (whoami, pers status/set/clear/
enable/save/clock, early buffer, auto slots, avc_cfg) and interprets the rule
blobs the way ``gateway-rp2350/src/avc_pers.c`` does (match part only: the C
engine itself is checked byte for byte against the recordings by the firmware
repo's ``test/pers_replay.py``). It answers display frames from its
personality, tags them ``au`` and forwards them to the emulator like the real
gateway would.
"""

import time
import zlib

import pytest

from cyberpunk_computer.comm.avc import AvcFrame, ManualScheduler, NavEmulator
from cyberpunk_computer.comm.avc import personality as P
from cyberpunk_computer.comm.avc.nav_emulator import ROLL_CALL_REPLY
from avc_captures import capture, records


# ── fake gateway ─────────────────────────────────────────────────────────────

def _be16(b, i):
    return (b[i] << 8) | b[i + 1]


def _frames(b, pos):
    out = []
    n = b[pos]
    pos += 1
    for _ in range(n):
        m, s, ctl, dl, ln, npt = _be16(b, pos), _be16(b, pos + 2), b[pos + 4], _be16(b, pos + 5), b[pos + 7], b[pos + 8]
        data_at = pos + 9 + 2 * npt
        out.append({"m": m, "s": s, "b": bool(ctl & 0x80), "delay": dl,
                    "patches": [(b[pos + 9 + 2 * k], b[pos + 10 + 2 * k]) for k in range(npt)],
                    "data": list(b[data_at:data_at + ln])})
        pos = data_at + ln
    return out


class FakeGateway:
    def __init__(self, ver="3.3.0", flash=None, reject_rule=None, save_result="ok"):
        self.ver = ver
        self.rules = []            # blobs in RAM
        self.on = False
        self.ack = 0
        self.flash = flash         # (blobs, on, ack) or None (blank)
        self.flash_writes = 0
        self.reject_rule = reject_rule
        self.save_result = save_result
        self.state = 0
        self.slots = {}
        self.clock = None
        self.early = []            # (ts, frame dict)
        self.cmds = []             # every command received
        self.bus_tx = []           # frames the personality sent
        self.lines = []            # lines to deliver to the host
        if flash:
            self.rules, self.on, self.ack = list(flash[0]), flash[1], flash[2]

    # host -> gateway
    def send(self, dev, payload):
        self.cmds.append((dev, payload))
        if self.ver.startswith("3.2") and dev == 2 and payload.get("a") in ("pers", "early"):
            self.lines.append({"id": 0, "d": {"err": "BAD_AVC_HEADER"}})
            return True
        a = payload.get("a")
        if dev == 0 and a == "whoami":
            self.lines.append({"id": 0, "d": {"msg": "IDENT", "role": "gateway", "ver": self.ver}})
        elif dev == 0 and a == "avc_cfg" and "ack" in payload:
            self.ack = int(payload["ack"], 16)
        elif dev == 2 and a == "auto":
            self.slots[payload["slot"]] = payload.get("on", True)
        elif dev == 2 and a == "early":
            for ts, fr in self.early:
                self.lines.append({"id": 2, "ts": ts, "d": {"q": fr}})
            self.lines.append({"id": 2, "d": {"early": {"n": len(self.early), "lost": 0, "now": 5000, "done": True}}})
            self.early = []
        elif dev == 2 and a == "pers":
            self._pers(payload)
        return True

    def _status(self):
        st = {"fmt": 1, "on": self.on, "active": self.on and bool(self.rules), "n": len(self.rules),
              "crc": "%08X" % P.body_crc(self.rules), "ack": "%03X" % self.ack, "state": "%02X" % self.state,
              "flash": "loaded" if self.flash else "blank", "stored": None}
        if self.flash:
            st["stored"] = {"crc": "%08X" % P.body_crc(self.flash[0]), "n": len(self.flash[0]),
                            "on": self.flash[1], "ack": "%03X" % self.flash[2]}
        self.lines.append({"id": 2, "d": {"pers": st}})

    def _pers(self, p):
        op = p.get("op")
        if op is None:
            self._status()
        elif op == "clear":
            self.rules = []
            self._status()
        elif op == "set":
            i, blob = p["i"], bytes.fromhex(p["r"])
            if i == self.reject_rule or i > len(self.rules):
                self.lines.append({"id": 2, "d": {"pers_set": i, "ok": False, "err": -1}})
                return
            if i == len(self.rules):
                self.rules.append(blob)
            else:
                self.rules[i] = blob
            self.lines.append({"id": 2, "d": {"pers_set": i, "ok": True, "n": len(self.rules)}})
        elif op == "enable":
            self.on = bool(p.get("on", True))
            self._status()
        elif op == "save":
            new = (list(self.rules), self.on, self.ack)
            res = self.save_result
            if res == "ok" and self.flash == new:
                res = "same"
            elif res == "ok":
                self.flash = new
                self.flash_writes += 1
            self.lines.append({"id": 2, "d": {"pers_save": res, "ms": 48}})
        elif op == "clock":
            self.clock = p["s"]
            self.lines.append({"id": 2, "d": {"pers_clock": p["s"]}})

    # bus -> gateway: run the personality, return the line for the host
    def receive(self, fr):
        data = [int(x, 16) for x in fr["d"]]
        m, s = int(fr["m"], 16), int(fr["s"], 16)
        answered = 0
        if self.on:
            for blob in self.rules:
                b = blob
                if b[0] != 0:
                    continue
                bm, bs = _be16(b, 2), _be16(b, 4)
                if bm != 0xFFFF and bm != m or bs != 0xFFFF and bs != s:
                    continue
                if not b[6] <= len(data) <= b[7]:
                    continue
                if (self.state & b[8]) != b[8] or (self.state & b[9]):
                    continue
                plen = b[12]
                if list(b[13:13 + plen]) != data[:plen]:
                    continue
                self.state = (self.state & ~b[11]) | b[10]
                for f in _frames(b, 13 + plen):
                    d = list(f["data"])
                    for pos, op in f["patches"]:
                        if (op & 0xC0) == 0x40:
                            d[pos] = data[op & 0x3F] if (op & 0x3F) < len(data) else 0
                    self.bus_tx.append(("%03X" % f["s"], " ".join("%02X" % x for x in d), f["b"]))
                    answered += 1
                if b[1] & 1:
                    break
        out = dict(fr)
        if answered:
            out["au"] = 1
        return out

    def drain(self, emu):
        while self.lines:
            emu.on_gateway_line(self.lines.pop(0))


def make(gw, **kw):
    sched = ManualScheduler(start=0.0)
    emu = NavEmulator(gw.send, sched, clock=lambda: time.struct_time((2026, 11, 17, 19, 9, 26, 0, 321, 0)), **kw)
    return sched, emu


def connect(gw, emu):
    emu.configure()
    gw.drain(emu)
    gw.drain(emu)


def host_frames(gw, since=0):
    return [(p["s"], " ".join(p["d"]), bool(p.get("b"))) for dev, p in gw.cmds[since:] if dev == 2 and "m" in p and "a" not in p]


def frame(m, s, data, b=False):
    d = {"m": m, "s": s, "c": 15, "d": data.split()}
    if b:
        d["b"] = 1
    return d


# ── personality compiler ─────────────────────────────────────────────────────

def test_nav_blobs_fit_and_crc_is_zlib_over_length_prefixed_blobs():
    blobs = P.nav_blobs()
    assert len(blobs) == 21 and sum(map(len, blobs)) <= P.MAX_BYTES
    crc = 0
    for b in blobs:
        crc = zlib.crc32(len(b).to_bytes(2, "big") + b, crc)
    assert P.body_crc(blobs) == crc
    # language and time format end up in the blobs; nothing else changes
    other = P.nav_blobs(lang="06", time_fmt="old")
    assert [i for i, (x, y) in enumerate(zip(blobs, other)) if x != y] == [12, 13, 14]
    # first rule: roll call, retry on NAK, clears all flags, replies at once
    r = blobs[0]
    assert r[:13] == bytes([0, 0x03, 0x01, 0x10, 0xFF, 0xFF, 3, 3, 0, 0, 0, 0xFF, 3])
    assert r[13:16] == bytes.fromhex("120100")
    with pytest.raises(ValueError):
        P.Periodic(run_set=0, send_set=1, first_ms=0, period_ms=5000).encode()
    with pytest.raises(ValueError):
        P.frame(0x110, " ".join(["00"] * 25)).encode()


def test_personality_matches_firmware_fixture_if_present():
    from pathlib import Path
    fx = Path("/home/piotr/projects/cyberpunk-prius-gateway/gateway-rp2350/test/fixtures/nav_en_lang02_new.hex")
    if not fx.exists():
        pytest.skip("firmware repo not available")
    lines = [l for l in fx.read_text().splitlines() if l and not l.startswith("#")]
    assert lines == [b.hex().upper() for b in P.nav_blobs(lang="02", time_fmt="new")]


# ── install / verify ─────────────────────────────────────────────────────────

def test_blank_gateway_gets_rules_written_and_saved_once():
    gw = FakeGateway()
    sched, emu = make(gw)
    connect(gw, emu)
    for _ in range(3):
        gw.drain(emu)
    assert emu.pers == "active"
    assert gw.flash_writes == 1
    sets = [p for dev, p in gw.cmds if dev == 2 and p.get("op") == "set"]
    assert len(sets) == 21 and [p["i"] for p in sets] == list(range(21))
    assert gw.flash[0] == P.nav_blobs() and gw.flash[1] is True and gw.flash[2] == 0x178
    assert gw.slots == {0: False, 1: False}                 # RAM slots cleared: one source of truth
    assert gw.clock == 19 * 3600 + 9 * 60 + 26              # gateway clock set from the phone
    assert any(dev == 2 and p.get("a") == "early" for dev, p in gw.cmds)
    assert emu.status()["personality"] == "active" and emu.status()["fw"] == "3.3.0"


def test_reconnect_with_matching_flash_writes_nothing():
    gw = FakeGateway(flash=(P.nav_blobs(), True, 0x178))
    gw.ack = 0x178
    sched, emu = make(gw)
    connect(gw, emu)
    gw.drain(emu)
    assert emu.pers == "active"
    assert gw.flash_writes == 0
    assert not any(p.get("op") in ("set", "save", "clear") for dev, p in gw.cmds if dev == 2)
    # gateway reboot -> reconfigure -> verified again, still no write
    n = len(gw.cmds)
    emu.on_gateway_line({"id": 0, "d": {"msg": "GATEWAY_READY", "ver": "3.3.0"}})
    for _ in range(3):
        gw.drain(emu)
    assert emu.pers == "active" and gw.flash_writes == 0
    assert not any(p.get("op") in ("set", "save") for dev, p in gw.cmds[n:] if dev == 2)


def test_language_change_rewrites_once():
    gw = FakeGateway(flash=(P.nav_blobs(lang="06"), True, 0x178))
    sched, emu = make(gw, lang="02")
    connect(gw, emu)
    for _ in range(3):
        gw.drain(emu)
    assert emu.pers == "active" and gw.flash_writes == 1 and gw.flash[0] == P.nav_blobs(lang="02")
    # same connection, status asked again: no second write
    gw.send(2, {"a": "pers"})
    gw.drain(emu)
    assert gw.flash_writes == 1


def test_rejected_rule_falls_back_to_host_answers():
    gw = FakeGateway(reject_rule=5)
    sched, emu = make(gw)
    connect(gw, emu)
    for _ in range(3):
        gw.drain(emu)
    assert emu.pers == "failed"
    assert gw.slots == {0: True, 1: True}                    # RAM slots stay
    n = len(gw.cmds)
    emu.on_gateway_line({"id": 2, "d": frame("110", "178", "00 12 58 8F")})
    assert host_frames(gw, n) == [("110", "00 58 12 9F " + P.IDENT_EN, False)]


def test_save_busy_is_not_retried_in_a_loop():
    gw = FakeGateway(save_result="busy")
    sched, emu = make(gw)
    connect(gw, emu)
    for _ in range(5):
        gw.drain(emu)
    saves = [p for dev, p in gw.cmds if dev == 2 and p.get("op") == "save"]
    assert len(saves) == 1 and gw.flash_writes == 0
    assert emu.pers == "ram_only"


def test_old_firmware_gets_the_old_behaviour():
    gw = FakeGateway(ver="3.2.11")
    sched, emu = make(gw)
    connect(gw, emu)
    gw.drain(emu)
    assert emu.pers == "unsupported"
    assert [(dev, p) for dev, p in gw.cmds] == [
        (2, {"a": "auto", "slot": 0, "on": True, "m": "110", "s": "FFF", "d": ["12", "01", "00"],
             "tx": {"m": "178", "s": "110", "c": 15, "d": ROLL_CALL_REPLY.split()}}),
        (2, {"a": "auto", "slot": 1, "on": True, "m": "110", "s": "FFF", "d": ["12", "01", "01"],
             "tx": {"m": "178", "s": "110", "c": 15, "d": "00 01 12 12 58 28 29 32 60 74 A4 56".split()}}),
        (0, {"a": "avc_cfg", "ack": "178"}),
        (0, {"a": "whoami"}),
    ]
    # host answers as before, and deconfigure clears the ACK
    emu.on_gateway_line({"id": 2, "d": frame("110", "178", "00 12 58 42 02 01")})
    sched.advance(0.01)
    assert host_frames(gw)[:2] == [("110", "00 58 12 52 02 01", False), ("1FF", "58 31 F1 02 00", True)]
    emu.deconfigure()
    assert (0, {"a": "avc_cfg", "ack": "000"}) in gw.cmds


# ── no double answers ────────────────────────────────────────────────────────

def test_firmware_answers_whole_cold_start_host_sends_nothing():
    path = capture("2026-10-14/emu11_cold_lang02.ndjson")
    gw = FakeGateway(flash=(P.nav_blobs(), True, 0x178))
    gw.ack = 0x178
    sched, emu = make(gw)
    connect(gw, emu)
    gw.drain(emu)
    assert emu.pers == "active"
    n = len(gw.cmds)
    t0 = None
    answered = 0
    for ht, obj in records(path):
        t0 = ht if t0 is None else t0
        sched.advance(max(0.0, (ht - t0) - sched.now()))
        d = obj.get("d")
        if obj.get("id") != 2 or not isinstance(d, dict) or "m" not in d or d.get("echo") or d["m"] == "178":
            continue
        line = gw.receive(d)
        answered += bool(line.get("au"))
        emu.on_gateway_line({"id": 2, "ts": 1, "d": line})
    sched.advance(10.0)
    assert answered > 40
    assert host_frames(gw, n) == []                        # not a single double answer
    assert gw.bus_tx[0] == ("110", ROLL_CALL_REPLY, False)
    assert emu.status()["registered"]


def test_grant_answered_by_gateway_means_no_host_heartbeats():
    gw = FakeGateway(flash=(P.nav_blobs(), True, 0x178))
    gw.ack = 0x178
    sched, emu = make(gw)
    connect(gw, emu)
    gw.drain(emu)
    n = len(gw.cmds)
    emu.on_gateway_line({"id": 2, "d": gw.receive(frame("110", "178", "00 12 58 42 02 01"))})
    emu.on_gateway_line({"id": 2, "d": gw.receive(frame("110", "FFF", "12 01 60 58 01 00 05", b=True))})
    sched.advance(12.0)
    assert host_frames(gw, n) == []
    assert emu.screen == 0x58
    assert gw.bus_tx[-2:] == [("110", "00 58 12 52 02 01", False), ("1FF", "58 31 F1 02 00", True)]


def test_unanswered_request_is_still_answered_by_host():
    # personality active, but a request it has no rule for -> no "au" -> host answers
    gw = FakeGateway(flash=(P.nav_blobs()[:5], True, 0x178))
    sched, emu = make(gw)
    emu.on_gateway_line({"id": 2, "d": gw.receive(frame("110", "178", "00 6E 58 E0"))})
    assert host_frames(gw) == [("110", "00 58 6E F0 C9 00 00 01", False)]


def test_au_echo_nak_is_not_retried_by_host():
    gw = FakeGateway()
    sched, emu = make(gw)
    emu.on_gateway_line({"id": 2, "d": {"m": "178", "s": "110", "c": 15, "d": ROLL_CALL_REPLY.split(),
                                        "echo": 1, "nak": 1, "au": 1}})
    assert host_frames(gw) == []
    emu.on_gateway_line({"id": 2, "d": {"m": "178", "s": "110", "c": 15, "d": ROLL_CALL_REPLY.split(),
                                        "echo": 1, "nak": 1}})
    assert host_frames(gw) == [("110", ROLL_CALL_REPLY, False)]


# ── early-frame buffer ───────────────────────────────────────────────────────

def test_early_buffer_sets_session_state_without_answers():
    gw = FakeGateway(flash=(P.nav_blobs(), True, 0x178))
    gw.ack = 0x178
    for ts, fr in [(812, frame("110", "FFF", "12 01 00", b=True)),
                   (1835, frame("110", "FFF", "12 01 01", b=True)),
                   (1916, frame("110", "178", "00 12 58 8F")),
                   (2050, frame("110", "178", "00 12 01 02 11 00 17 82 1C 68")),
                   (5012, frame("110", "178", "00 12 58 42 02 01")),
                   (5040, frame("110", "FFF", "12 01 60 58 01 00 05", b=True))]:
        gw.early.append((ts, gw.receive(fr)))
    sched, emu = make(gw)
    connect(gw, emu)
    gw.drain(emu)
    assert emu.early_rollcall_ms == 812
    assert emu.early["n"] == 6
    assert emu.screen == 0x58 and emu.status()["registered"]
    sched.advance(30.0)
    frames = host_frames(gw)
    assert frames == []                                     # stale requests never answered, no announce
    assert ("1FF", "01 12 13 58 24 85", True) not in frames


def test_early_unanswered_rollcall_leads_to_announce():
    # older personality-less 3.3.0 gateway: the roll call was seen but nobody answered it
    gw = FakeGateway()
    gw.early.append((640, frame("110", "FFF", "12 01 00", b=True)))
    gw.save_result = "busy"
    sched, emu = make(gw, use_personality=False)
    connect(gw, emu)
    gw.drain(emu)
    assert emu.early_rollcall_ms == 640 and not emu._rollcall_seen
    for _ in range(40):                                     # display traffic, nothing to 178
        sched.advance(0.1)
        emu.on_gateway_line({"id": 2, "d": frame("1C6", "110", "00 E4 5F B9 00 00 00")})
    assert ("1FF", "01 12 13 58 24 85", True) in host_frames(gw)


def test_live_screen_wins_over_early_screen():
    gw = FakeGateway(flash=(P.nav_blobs(), True, 0x178))
    sched, emu = make(gw)
    emu.configure()
    emu.on_gateway_line({"id": 2, "d": frame("110", "FFF", "12 01 60 5F 05 00 05", b=True)})
    emu.on_early_frame(AvcFrame.make(0x110, 0xFFF, "12 01 60 58 01 00 05", broadcast=True), 100)
    assert emu.screen == 0x5F


def test_deconfigure_keeps_ack_when_personality_active_and_clock_resync():
    gw = FakeGateway(flash=(P.nav_blobs(), True, 0x178))
    gw.ack = 0x178
    sched, emu = make(gw)
    connect(gw, emu)
    gw.drain(emu)
    clocks = [p for dev, p in gw.cmds if p.get("op") == "clock"]
    assert len(clocks) == 1
    sched.advance(emu.CLOCK_RESYNC_S + 1.0)
    assert len([p for dev, p in gw.cmds if p.get("op") == "clock"]) == 2
    n = len(gw.cmds)
    emu.deconfigure()
    assert (0, {"a": "avc_cfg", "ack": "000"}) not in gw.cmds[n:]
    assert gw.ack == 0x178
