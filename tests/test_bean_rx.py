"""Host-side tests of the powerbox BEAN receiver (powerbox/bean_rx.py, fw 2.3.0).

The module runs on CPython with a fake ``rp2``: ``asm_pio`` records the PIO
program, and a small cycle-level PIO simulator below runs it against BEAN
waveforms, so the run-length words the decoder sees are the ones the real
state machine would push. Everything after the ring buffer (decoder, batching,
commands) runs unchanged.

Regression vectors: the 8,627 real NHW20 frames of haageduard/bean_tools
(data/*.dat, see docs/BEAN_PROTOCOL.md §7.3), re-encoded on the wire
(stuffing, EOM, RSP from the adapter's ack flag) and decoded again.
"""

import importlib.util
import os
import random
import sys
import types
from array import array

import pytest

import bean_wire as W

POWERBOX_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "powerbox")


# ─── fake rp2: PIO assembler front end + simulator ──────────────────────────

class PioProgram:
    def __init__(self, ins, labels, kw):
        self.ins = ins
        self.labels = labels
        self.kw = kw


def _asm_pio(**kw):
    def deco(f):
        ins, labels = [], {}

        def op(name):
            return lambda *a: ins.append((name, a))

        inject = {
            "pull": op("pull"), "set": op("set"), "mov": op("mov"), "jmp": op("jmp"),
            "in_": op("in"), "push": op("push"), "irq": op("irq"),
            "wrap_target": lambda: labels.__setitem__("__wt", len(ins)),
            "wrap": lambda: labels.__setitem__("__w", len(ins) - 1),
            "label": lambda n: labels.__setitem__(n, len(ins)),
            "x": "x", "y": "y", "osr": "osr", "null": "null", "pin": "pin",
            "x_dec": "x_dec", "block": "block", "noblock": "noblock",
            "rel": lambda i: ("rel", i),
        }
        g = f.__globals__
        saved = {k: g[k] for k in inject if k in g}
        g.update(inject)
        try:
            f()
        finally:
            for k in inject:
                if k in saved:
                    g[k] = saved[k]
                else:
                    del g[k]
        return PioProgram(ins, labels, kw)
    return deco


def pio_simulate(prog, gp6_runs_us, T, cycle_us=0.5):
    """Run the program cycle by cycle; ``gp6_runs_us`` = [(gp6_level, us)].
    Returns the pushed words."""
    edges = []
    t = 0.0
    for lvl, d in gp6_runs_us:
        edges.append((t, lvl))
        t += d
    total = t
    ncyc = int(total / cycle_us)
    out = []
    pc, x, y, osr, isr = 0, 0, 0, 0, 0
    txq = [T]
    ei = 0
    lvl = edges[0][1]
    L = prog.labels
    for cyc in range(ncyc):
        now = cyc * cycle_us
        while ei + 1 < len(edges) and edges[ei + 1][0] <= now:
            ei += 1
        lvl = edges[ei][1]
        name, a = prog.ins[pc]
        nxt = pc + 1
        if name == "pull":
            osr = txq.pop(0)
        elif name == "set":
            if a[0] == "y":
                y = a[1]
            else:
                x = a[1]
        elif name == "mov":
            assert a == ("x", "osr")
            x = osr
        elif name == "jmp":
            if len(a) == 1:
                nxt = L[a[0]]
            else:
                cond, lab = a
                if cond == "pin":
                    take = lvl == 1
                else:
                    assert cond == "x_dec"
                    take = x != 0
                    x = (x - 1) & 0xFFFFFFFF
                if take:
                    nxt = L[lab]
        elif name == "in":
            src, nb = a
            val = {"x": x, "y": y, "null": 0}[src] & ((1 << nb) - 1)
            isr = ((isr << nb) | val) & 0xFFFFFFFF
        elif name == "push":
            out.append(isr)
            isr = 0
        elif name == "irq":
            pass
        else:
            raise AssertionError(name)
        if pc == L["__w"] and nxt == pc + 1:
            nxt = L["__wt"]
        pc = nxt
    return out


def _install_stubs():
    import time
    if not hasattr(time, "ticks_ms"):
        time.ticks_ms = lambda: int(time.monotonic() * 1000)
        time.ticks_diff = lambda a, b: a - b
        time.ticks_add = lambda a, b: a + b
        time.sleep_ms = lambda ms: None
    if not hasattr(time, "ticks_us"):
        time.ticks_us = lambda: int(time.perf_counter() * 1000000)
    if not hasattr(time, "sleep_us"):           # same shim as test_powerbox_firmware
        def _sleep_us(us):
            end = time.perf_counter() + us / 1e6
            while time.perf_counter() < end:
                pass
        time.sleep_us = _sleep_us
    rp2 = types.ModuleType("rp2")
    rp2.asm_pio = _asm_pio
    rp2.PIO = types.SimpleNamespace(SHIFT_LEFT=0, SHIFT_RIGHT=1, JOIN_RX=2, JOIN_NONE=0)
    return rp2


@pytest.fixture(scope="module")
def brx():
    rp2 = _install_stubs()
    saved = sys.modules.get("rp2")
    sys.modules["rp2"] = rp2
    try:
        spec = importlib.util.spec_from_file_location("bean_rx", os.path.join(POWERBOX_DIR, "bean_rx.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if saved is None:
            del sys.modules["rp2"]
        else:
            sys.modules["rp2"] = saved
    mod._fake_rp2 = rp2
    return mod


# ─── harness ────────────────────────────────────────────────────────────────

class Clock:
    def __init__(self, us=5_000_000):
        self.us = us

    def ticks_us(self):
        return self.us

    def ticks_ms(self):
        return self.us // 1000


class FakeCapture:
    def __init__(self, ring):
        self.mode = "test"
        self.ring = ring
        self.buf = array("I", [0] * ring)
        self.base = 0
        self.head = 0
        self.stalls = 0
        self.starts = 0
        self.fail = None

    def start(self, want="auto"):
        self.starts += 1
        self.head = 0
        return self.mode

    def stop(self):
        pass

    def produced(self):
        if self.fail:
            raise self.fail
        return self.head

    def push(self, words):
        for w in words:
            self.buf[self.head % self.ring] = w
            self.head = (self.head + 1) & 0x3FFFFFFF


class Rig:
    """BeanRx on a fake capture + fake clock, fed in (simulated) real time."""

    def __init__(self, mod, monkeypatch, tx_ok=True):
        self.mod = mod
        self.clock = Clock()
        monkeypatch.setattr(mod, "ticks_us", self.clock.ticks_us)
        monkeypatch.setattr(mod, "ticks_ms", self.clock.ticks_ms)
        self.cap = FakeCapture(mod.RING)
        self.lines = []
        self.tx_ok = tx_ok
        self.rx = mod.BeanRx(self._tx, 3, 6, capture=self.cap)
        assert self.rx.enable(True) == "test"

    def _tx(self, dev, msg):
        assert dev == 3
        self.lines.append((self.clock.us, msg))
        return self.tx_ok

    def feed_runs(self, runs, poll_ms=5, settle_ms=60):
        """Push the words of ``runs`` as the bus would produce them, polling
        every ``poll_ms``; then idle-poll ``settle_ms``."""
        words = []
        t = self.clock.us
        for v, d in runs:
            ws = W.runs_to_words([(v, d)])
            # timeout words appear every ~10 ms inside the run, end word at the end
            for k, w in enumerate(ws[:-1]):
                words.append((t + (k + 1) * 10004, w))
            t += d
            words.append((t, ws[-1]))
        self.sof_times = None
        i = 0
        end = max(t, self.clock.us) + settle_ms * 1000
        while self.clock.us < end:
            self.clock.us += poll_ms * 1000
            batch = []
            while i < len(words) and words[i][0] <= self.clock.us:
                batch.append(words[i][1])
                i += 1
            self.cap.push(batch)
            self.rx.poll()
        return t

    def drain(self, ms=3000):
        for _ in range(ms // 5):
            self.clock.us += 5000
            self.rx.poll()

    def frames(self):
        out = []
        for _, m in self.lines:
            out += m.get("bf", [])
        return out


def _frame_runs(pri, dst, mes, data, idle_us=2000, tbit=100.0, **kw):
    return W.add_idle(W.bits_to_runs(W.frame_bits(pri, dst, mes, data, **kw), tbit), idle_us)


def _stream(frames, gap_us=3000, tbit=100.0, lead_us=2000, **kw):
    """Recessive lead-in, then each frame (SOF..EOF) followed by ``gap_us``."""
    runs = [(0, float(lead_us))]
    for f in frames:
        runs = runs + W.bits_to_runs(W.frame_bits(*f, **kw), tbit)
        runs = W.add_idle(runs, gap_us)
    return runs


def _key(f):
    return (f[1], f[2], f[3], f[4], f[5], f[6])


# ─── CRC and wire encoding sanity ───────────────────────────────────────────

class TestWire:
    def test_crc_doc_examples(self, brx):
        assert brx.crc8(bytes([0x13, 0xFE, 0x24, 0x00]), 4) == 0xB4
        assert brx.crc8(bytes([0x63, 0xFE, 0x2C, 0x64]), 4) == 0xC1
        assert brx.crc8(bytes([0x84, 0xFE, 0xC2, 0xE9, 0xA1]), 5) == 0x2B
        assert W.crc8([0x25, 0xFE, 0x7F, 0x01, 0x00, 0x00]) == 0x3A

    def test_encoder_reproduces_drive2_bit_string(self):
        # docs/BEAN_PROTOCOL.md §3.4 example 1 (stuff bits in brackets there)
        want = "1" "0001" "0011" "111011110" "00100100" "000100000" "110110100" "01111110"
        bits = W.frame_bits(1, 0xFE, 0x24, [0x00], rsp=(0, 1), eof=0)
        assert "".join(map(str, bits[:-2])) == want

    def test_fixture_complete(self):
        fx = W.load_fixture()
        assert len(fx) == 8627
        assert all(W.crc8([(p << 4) | ml, d, m] + list(data)) == crc
                   for _, _, p, ml, d, m, data, crc, _ in fx)


# ─── PIO program (cycle-level simulation) ───────────────────────────────────

class TestPio:
    def test_program_shape(self, brx):
        prog = brx._runlen
        assert isinstance(prog, PioProgram)
        assert len(prog.ins) <= 32
        # the initial pull() needs the TX FIFO: joining it into RX would
        # leave sm.put(T_CNT) with nowhere to go
        assert prog.kw.get("fifo_join", brx._fake_rp2.PIO.JOIN_NONE) == brx._fake_rp2.PIO.JOIN_NONE
        assert prog.ins[0][0] == "pull"
        assert prog.kw["in_shiftdir"] == brx._fake_rp2.PIO.SHIFT_LEFT
        names = {n for n, _ in prog.ins}
        assert names <= {"pull", "set", "mov", "jmp", "in", "push", "irq"}

    def test_run_lengths_and_timeouts(self, brx):
        T = 2000                               # short timeout keeps the sim fast
        runs = [(1, 250.0), (0, 100.0), (1, 37.5), (0, 600.0), (1, 4500.0),
                (0, 333.0), (1, 2007.0), (0, 100.0)]
        words = pio_simulate(brx._runlen, runs, T)
        got = []
        cur = 0
        for w in words:
            x = w & 0xFFFF
            if x == 0xFFFF:
                cur += T + 4
                continue
            got.append(((w >> 16) & 1, cur + T - x + brx.OFF_US))
            cur = 0
        # first word: the 250 us high run; the last run never ends in the sim
        assert len(got) == len(runs) - 1
        for i, ((lvl, d), (glvl, gd)) in enumerate(zip(runs, got)):
            assert glvl == lvl
            # the very first run starts before the loop does (pull/set)
            assert abs(gd - d) <= (2.0 if i == 0 else 1.0), (i, d, gd)
        assert sum(1 for w in words if w & 0xFFFF == 0xFFFF) == 2 + 1   # 4500 -> 2, 2007 -> 1

    def test_frame_through_pio_simulation(self, brx, monkeypatch):
        T = brx.T_CNT
        rig = Rig(brx, monkeypatch)
        runs = [(0, 3000.0)] + W.bits_to_runs(W.frame_bits(6, 0xFE, 0x2C, [0x64], rsp=(0, 1)))
        runs = W.add_idle(runs, 25000.0)
        gp6 = [(1 - v, d) for v, d in runs]
        words = pio_simulate(brx._runlen, gp6, T)
        assert any(w & 0xFFFF == 0xFFFF for w in words)   # idle timeouts
        rig.cap.push(words)
        rig.drain(500)
        f, = rig.frames()
        assert _key(f) == (6, "FE", "2C", "64", 1, 0)


# ─── decoder ────────────────────────────────────────────────────────────────

class TestDecode:
    def test_single_frame(self, brx, monkeypatch):
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(_stream([(1, 0xFE, 0x24, [0x00])]))
        rig.drain(500)
        f, = rig.frames()
        assert _key(f) == (1, "FE", "24", "00", 1, 0)
        assert rig.rx.cnt[brx.E_OK] == 1

    def test_timestamp_is_sof_time(self, brx, monkeypatch):
        rig = Rig(brx, monkeypatch)
        t0 = rig.clock.us
        rig.feed_runs(_stream([(6, 0xFE, 0x2C, [0x64])], lead_us=40000))
        rig.drain(300)
        f, = rig.frames()
        assert abs(f[0] - (t0 + 40000) // 1000) <= 2

    @pytest.mark.parametrize("rsp", [(0, 0), (0, 1), (1, 0), (1, 1)])
    def test_rsp_raw_bits(self, brx, monkeypatch, rsp):
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(_stream([(2, 0xFE, 0x7F, [0, 0x10, 0x80, 0, 0, 0])], rsp=rsp, gap_us=30000))
        rig.drain(300)
        f, = rig.frames()
        assert f[5] == rsp[0] * 2 + rsp[1] and f[6] == 0

    def test_all_lengths_random_payloads(self, brx, monkeypatch):
        rng = random.Random(1)
        frames = []
        for ml in range(3, 14):
            for _ in range(6):
                pri, dst, mes, _ = W.random_frame(rng)
                frames.append((pri, dst, mes, bytes(rng.randint(0, 255) for _ in range(ml - 2))))
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(_stream(frames))
        rig.drain(3000)
        got = rig.frames()
        assert [g[1:5] for g in got] == [[p, "%02X" % d, "%02X" % m, bytes(x).hex().upper()]
                                         for p, d, m, x in frames]
        assert all(g[6] == 0 for g in got)

    def _ambiguous(self, last5):
        """Frames whose CRC ends in a run of 5 of ``last5`` bits."""
        rng = random.Random(7)
        out = []
        while len(out) < 6:
            f = W.random_frame(rng)
            if W.needs_stuff_after_crc(*f):
                crc = W.crc8([(f[0] << 4) | (2 + len(f[3])), f[1], f[2]] + list(f[3]))
                if crc & 1 == last5:
                    out.append(f)
        return out

    @pytest.mark.parametrize("last5", [0, 1])
    @pytest.mark.parametrize("stuffed", [False, True])
    def test_stuff_bit_before_eom_either_way(self, brx, monkeypatch, last5, stuffed):
        frames = self._ambiguous(last5)
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(_stream(frames, stuff_after_crc=stuffed))
        rig.drain(2000)
        got = rig.frames()
        assert len(got) == len(frames) and all(g[6] == 0 for g in got)

    def test_stuff_bit_right_before_crc(self, brx, monkeypatch):
        # drive2 frame: DATA 00 ends in five 0s -> stuffed 1 is the first CRC-region bit
        bits = W.frame_bits(1, 0xFE, 0x24, [0x00])
        assert "".join(map(str, bits)).startswith("10001001111101111000100100000100000110110100")
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(_stream([(1, 0xFE, 0x24, [0x00])] * 3))
        rig.drain(500)
        assert [g[6] for g in rig.frames()] == [0, 0, 0]

    def test_jitter_20_percent(self, brx, monkeypatch):
        # every edge moved by up to +-20 us (a run by up to +-40 us)
        rng = random.Random(3)
        frames = [W.random_frame(rng) for _ in range(300)]
        runs = W.jitter(_stream(frames), 20.0, rng)
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(runs)
        rig.drain(3000)
        got = rig.frames()
        assert len(got) == 300 and all(g[6] == 0 for g in got)

    @pytest.mark.parametrize("tbit", [90.0, 104.0, 110.0])
    def test_bit_clock_offset(self, brx, monkeypatch, tbit):
        rng = random.Random(4)
        frames = [W.random_frame(rng) for _ in range(30)]
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(_stream(frames, tbit=tbit))
        rig.drain(3000)
        got = rig.frames()
        assert len(got) == 30 and all(g[6] == 0 for g in got)
        assert abs(rig.rx.tb16 / 16 - tbit) < 3

    def test_dominant_skew_compensated(self, brx, monkeypatch):
        # slow comparator: every dominant run 30 us longer, recessive 30 us shorter
        rng = random.Random(5)
        frames = [W.random_frame(rng) for _ in range(30)]
        runs = [(v, d + 30.0 if v else max(1.0, d - 30.0)) for v, d in _stream(frames)]
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(runs)
        rig.drain(3000)
        got = rig.frames()
        assert len(got) == 30 and all(g[6] == 0 for g in got)
        assert 20 <= rig.rx.skew <= 30

    def test_glitches_filtered(self, brx, monkeypatch):
        rng = random.Random(6)
        frames = [W.random_frame(rng) for _ in range(20)]
        runs = _stream(frames)
        for _ in range(40):
            i = rng.randrange(1, len(runs) - 1)
            if runs[i][1] >= 60:
                runs = W.insert_glitch(runs, i, rng.uniform(10, runs[i][1] - 20), 6.0)
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(runs)
        rig.drain(3000)
        got = rig.frames()
        assert len(got) == 20 and all(g[6] == 0 for g in got)
        assert rig.rx.glitches >= 30

    def test_back_to_back_minimal_gap(self, brx, monkeypatch):
        # EOF (6) + 1 bit of IFS, then the next SOF
        rng = random.Random(8)
        frames = [W.random_frame(rng) for _ in range(10)]
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(_stream(frames, gap_us=100.0, rsp=(0, 1)))
        rig.drain(1000)
        got = rig.frames()
        assert len(got) == 10 and all(g[6] == 0 for g in got)

    def test_truncated_frame_then_recovery(self, brx, monkeypatch):
        bits = W.frame_bits(6, 0xFE, 0x91, [0xA0, 0x3F, 0x06])[:30]
        runs = [(0, 2000.0)] + W.bits_to_runs(bits)
        runs = W.add_idle(runs, 20000.0)
        runs += _stream([(6, 0xFE, 0x2C, [0x64])], lead_us=10)[1:]
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(runs)
        rig.drain(500)
        got = rig.frames()
        assert [g[6] for g in got] == [brx.E_TRUNC, 0]
        assert got[0][1] == 6 and got[0][2] == "FE"

    def test_crc_error_reported_with_received_crc(self, brx, monkeypatch):
        good = W.crc8([0x63, 0xFE, 0x2C, 0x64])
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(_stream([(6, 0xFE, 0x2C, [0x65])], crc=good))
        rig.drain(500)
        f, = rig.frames()
        assert f[6] == brx.E_CRC and f[4] == "65%02X" % good
        assert rig.rx.cnt[brx.E_CRC] == 1

    def test_stuff_error(self, brx, monkeypatch):
        bits = W.frame_bits(6, 0xFE, 0x2C, [0x64])
        i = 20
        bits = bits[:i] + [bits[i]] * 6 + bits[i + 1:]      # 6 equal bits in DST/MES
        runs = W.add_idle([(0, 2000.0)] + W.bits_to_runs(bits), 3000.0)
        runs += _stream([(6, 0xFE, 0x2C, [0x64])], lead_us=10)[1:]
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(runs)
        rig.drain(500)
        assert [g[6] for g in rig.frames()] == [brx.E_STUFF, 0]

    @pytest.mark.parametrize("ml", [2, 14])
    def test_length_error(self, brx, monkeypatch, ml):
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(_stream([(6, 0xFE, 0x2C, [0x64]), (6, 0xFE, 0x2C, [0x64])], ml=ml)
                      + _stream([(6, 0xFE, 0x2C, [0x64])])[1:])
        rig.drain(500)
        got = rig.frames()
        assert [g[6] for g in got] == [brx.E_LEN, brx.E_LEN, 0]

    def test_stuck_dominant_then_recovery(self, brx, monkeypatch):
        runs = [(0, 2000.0), (1, 25000.0), (0, 3000.0)] + _stream([(1, 0xFE, 0x24, [0])], lead_us=10)[1:]
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(runs)
        rig.drain(500)
        got = rig.frames()
        assert [g[6] for g in got] == [brx.E_FRAME, 0] and got[-1][3] == "24"
        assert rig.rx.cnt[brx.E_FRAME] == 1               # bus stuck dominant


# ─── real NHW20 captures ────────────────────────────────────────────────────

def _capture_runs(fx, rng=None, tbit=100.0, j_us=0.0):
    """The captured frames back on the wire; gaps from the adapter's t_ms
    (clamped to 1..50 ms), RSP 01 when the adapter saw an ACK, else 00."""
    runs = [(0, 5000.0)]
    prev_t = None
    for fn, t, pri, ml, dst, mes, data, crc, ack in fx:
        bits = W.frame_bits(pri, dst, mes, data, crc=crc, rsp=(0, 1) if ack else (0, 0))
        fr = W.bits_to_runs(bits, tbit)
        if prev_t is not None:
            gap = (t - prev_t) * 1000.0 - len(bits) * tbit
            runs[-1] = (0, runs[-1][1] + min(max(gap, 1000.0), 50000.0))
        runs.extend(fr)
        prev_t = t
    runs[-1] = (0, runs[-1][1] + 30000.0)
    if j_us:
        runs = W.jitter(runs, j_us, rng)
    return runs


class TestRealCaptures:
    def test_all_8627_frames_decode_exactly(self, brx, monkeypatch):
        fx = W.load_fixture()
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(_capture_runs(fx))
        rig.drain(3000)
        got = rig.frames()
        want = [(p, "%02X" % d, "%02X" % m, data.hex().upper(), 1 if ack else 0, 0)
                for _, _, p, ml, d, m, data, crc, ack in fx]
        assert len(got) == 8627
        assert [_key(g) for g in got] == want
        st = rig.rx
        assert st.cnt == [8627, 0, 0, 0, 0, 0, 0]
        assert st.ovf == 0 and st.qdrop == 0 and st.esup == 0

    def test_real_captures_with_jitter_and_slow_clock(self, brx, monkeypatch):
        fx = W.load_fixture()[:3000]
        rng = random.Random(11)
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(_capture_runs(fx, rng, tbit=103.8, j_us=15.0))
        rig.drain(3000)
        got = rig.frames()
        assert len(got) == 3000 and all(g[6] == 0 for g in got)


# ─── capture path: ring overflow, poll budget ───────────────────────────────

class TestRing:
    def test_overflow_counted_and_recovered(self, brx, monkeypatch):
        rng = random.Random(9)
        frames = [W.random_frame(rng) for _ in range(60)]
        words = W.runs_to_words(_stream(frames))
        assert len(words) > brx.RING
        rig = Rig(brx, monkeypatch)
        rig.cap.push(words)                      # main loop "blocked" too long
        rig.rx.poll()
        assert rig.rx.ovf == 1 and rig.rx.lost == len(words)
        rig.feed_runs(_stream([(1, 0xFE, 0x24, [0])]))
        rig.drain(500)
        got = rig.frames()
        assert got[-1][3] == "24" and got[-1][6] == 0

    def test_100_ms_block_loses_nothing(self, brx, monkeypatch):
        # 45 frames/s of 8-byte frames: ~100 ms of words in one go, then polls
        rng = random.Random(10)
        frames = [(2, 0xFE, rng.randint(0, 255), bytes(rng.randint(0, 255) for _ in range(8)))
                  for _ in range(5)]
        words = W.runs_to_words(_stream(frames, gap_us=12000))
        assert len(words) < brx.RING - 16
        rig = Rig(brx, monkeypatch)
        rig.cap.push(words)
        rig.drain(1000)
        assert rig.rx.ovf == 0 and len(rig.frames()) == 5
        assert rig.rx.lag == len(words)

    def test_poll_budget_bounded(self, brx, monkeypatch):
        rng = random.Random(12)
        words = W.runs_to_words(_stream([W.random_frame(rng) for _ in range(12)]))
        rig = Rig(brx, monkeypatch)
        rig.cap.push(words[:900])
        rig.rx.poll()
        assert rig.rx.tail == brx.MAX_WORDS


# ─── reporting ──────────────────────────────────────────────────────────────

class TestReporting:
    def test_batches_rate_and_size_limited(self, brx, monkeypatch):
        rng = random.Random(13)
        frames = [W.random_frame(rng) for _ in range(120)]
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(_stream(frames, gap_us=1500))         # ~100 frames/s burst
        rig.drain(3000)
        lines = [(t, m) for t, m in rig.lines if "bf" in m]
        assert all(len(m["bf"]) <= brx.BATCH_MAX for _, m in lines)
        gaps = [b[0] - a[0] for a, b in zip(lines, lines[1:])]
        assert min(gaps) >= brx.BATCH_MS * 1000
        got = rig.frames()
        assert rig.rx.qdrop == 0 and len(got) == 120
        assert [g[3] for g in got] == ["%02X" % f[2] for f in frames]

    def test_failed_frames_rate_limited_but_counted(self, brx, monkeypatch):
        bad = [(6, 0xFE, 0x2C, [0x64])] * 40
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(_stream(bad, crc=0x00, gap_us=10000))  # ~400 ms of bad frames
        rig.drain(2500)
        got = rig.frames()
        assert rig.rx.cnt[brx.E_CRC] == 40
        assert len(got) == brx.ERR_PER_S * 1 or len(got) <= brx.ERR_PER_S * 2
        assert rig.rx.esup == 40 - len(got)

    def test_stats_every_2_s(self, brx, monkeypatch):
        rig = Rig(brx, monkeypatch)
        rig.feed_runs(_stream([(1, 0xFE, 0x24, [0])]))
        rig.drain(4500)
        sts = [m["st"] for _, m in rig.lines if "st" in m]
        assert len(sts) == 2
        st = sts[0]
        for k in ("on", "mode", "ok", "crc", "stuff", "len", "frm", "trunc", "ovf",
                  "stall", "edges", "idle_ms", "age_ms", "tb", "skew", "qdrop",
                  "txdrop", "esup", "cpu", "lag", "lost", "glitch"):
            assert k in st, k
        assert st["ok"] == 1 and st["mode"] == "test" and 990 <= st["tb"] <= 1010
        assert st["idle_ms"] > 100 and st["age_ms"] > 100

    def test_host_absent_counts_txdrop(self, brx, monkeypatch):
        rig = Rig(brx, monkeypatch, tx_ok=False)
        rig.feed_runs(_stream([(1, 0xFE, 0x24, [0])] * 3))
        rig.drain(500)
        assert rig.rx.txdrop == 3 and rig.rx.q == []


# ─── commands and robustness ────────────────────────────────────────────────

class TestCommands:
    def test_on_off(self, brx, monkeypatch):
        rig = Rig(brx, monkeypatch)
        reply, err = rig.rx.command({"on": 0})
        assert err is None and reply == {"ack": "bean_rx", "on": 0, "mode": None}
        rig.feed_runs(_stream([(1, 0xFE, 0x24, [0])]))
        assert rig.frames() == [] and rig.rx.edges == 0
        reply, err = rig.rx.command({"on": 1})
        assert reply == {"ack": "bean_rx", "on": 1, "mode": "test"} and rig.cap.starts == 2

    def test_stats_op_sends_now(self, brx, monkeypatch):
        rig = Rig(brx, monkeypatch)
        reply, err = rig.rx.command({"op": "stats"})
        assert reply == {"ack": "bean_rx", "op": "stats"}
        assert "st" in rig.lines[-1][1]
        assert rig.rx.command({"op": "nope"}) == (None, "op=nope")

    def test_raw_dump(self, brx, monkeypatch):
        rig = Rig(brx, monkeypatch)
        reply, err = rig.rx.command({"raw": 100})
        assert reply == {"ack": "bean_rx", "raw": 100}
        rig.feed_runs(_stream([(1, 0xFE, 0x24, [0])] * 6))
        rig.drain(200)
        raws = [m for _, m in rig.lines if "raw" in m]
        assert [m["ri"] for m in raws] == [0, 64] and raws[0]["rn"] == 100
        vals = raws[0]["raw"] + raws[1]["raw"]
        assert len(vals) == 100
        assert vals[0] == -2000 and vals[1] == 100        # idle (recessive), SOF
        for bad in ({"raw": 0}, {"raw": 513}, {"raw": "x"}):
            assert rig.rx.command(bad)[0] is None

    def test_poll_never_raises_and_gives_up(self, brx, monkeypatch):
        rig = Rig(brx, monkeypatch)
        rig.cap.fail = RuntimeError("boom")
        for _ in range(brx.ERR_LIMIT + 3):
            rig.rx.poll()
        assert rig.rx.on is False and "stopped" in rig.rx.err
        assert rig.rx.exc == brx.ERR_LIMIT


# ─── Capture: DMA / IRQ / poll selection on a fake rp2 ──────────────────────

class FakeSM:
    def __init__(self, sm_id, prog, freq=None, jmp_pin=None, **kw):
        self.id, self.prog, self.freq, self.jmp_pin = sm_id, prog, freq, jmp_pin
        self.fifo, self.puts, self.active_v, self.irq_h = [], [], 0, None
        FakeSM.last = self

    def put(self, v):
        self.puts.append(v)

    def active(self, v=None):
        self.active_v = v

    def irq(self, handler=None, hard=False, **kw):
        if FakeSM.irq_fails and handler is not None:
            raise OSError("no irq")
        self.irq_h, self.hard = handler, hard

    def rx_fifo(self):
        return len(self.fifo)

    def get(self):
        return self.fifo.pop(0)


class FakeDMA:
    def __init__(self):
        self.count = 0
        self.cfg = None
        self.closed = False

    @staticmethod
    def pack_ctrl(**kw):
        return kw

    def config(self, read=None, write=None, count=None, ctrl=None, trigger=False):
        self.cfg = dict(read=read, write=write, count=count, ctrl=ctrl, trigger=trigger)
        self.count = count

    def active(self, v=None):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def fake_hw(brx, monkeypatch):
    rp2 = brx._fake_rp2
    monkeypatch.setattr(rp2, "StateMachine", FakeSM, raising=False)
    monkeypatch.setattr(rp2, "DMA", FakeDMA, raising=False)
    FakeSM.irq_fails = False
    machine = types.ModuleType("machine")
    machine.Pin = type("Pin", (), {"IN": 0, "__init__": lambda s, n, m=None: None})
    regs = {}

    class Mem:
        def __getitem__(self, a):
            return regs.get(a, 0)

        def __setitem__(self, a, v):
            regs[("w", a)] = v
    machine.mem32 = Mem()
    monkeypatch.setitem(sys.modules, "machine", machine)
    ut = types.ModuleType("uctypes")
    ut.addressof = lambda buf: 0x20001234
    monkeypatch.setitem(sys.modules, "uctypes", ut)
    return types.SimpleNamespace(rp2=rp2, regs=regs)


class TestCapture:
    def test_dma_ring_configuration(self, brx, fake_hw):
        cap = brx.Capture(6)
        assert cap.start() == "dma"
        sm = FakeSM.last
        assert sm.id == 4 and sm.freq == 2_000_000 and sm.puts == [brx.T_CNT] and sm.active_v == 1
        cfg = cap.dma.cfg
        assert cfg["read"] == 0x50300020 and cfg["trigger"] is True
        assert cfg["write"] % (4 * brx.RING) == 0 and cfg["write"] >= 0x20001234
        assert cfg["write"] == 0x20001234 + 4 * cap.base
        assert cfg["ctrl"]["ring_size"] == brx.RING_BITS + 2 and cfg["ctrl"]["ring_sel"] is True
        assert cfg["ctrl"]["treq_sel"] == 12 and cfg["ctrl"]["inc_read"] is False
        cap.dma.count -= 37
        assert cap.produced() == 37
        # re-arm near the end keeps the ring position and the counter
        cap.dma.count = 1000
        p = cap.produced()
        assert p == (brx.DMA_COUNT - 1000) & brx.M30 and cap.rearms == 1
        assert cap.dma.cfg["write"] == cap.aligned + 4 * (p & brx.RMASK)
        cap.dma.count -= 5
        assert cap.produced() == (p + 5) & brx.M30

    def test_irq_fallback_without_dma(self, brx, fake_hw, monkeypatch):
        monkeypatch.delattr(fake_hw.rp2, "DMA")
        cap = brx.Capture(6)
        assert cap.start() == "irq"
        sm = FakeSM.last
        assert sm.hard is True
        sm.fifo = [0x10005, 0x00007]
        sm.irq_h(sm)
        assert cap.produced() == 2 and list(cap.buf[:2]) == [0x10005, 0x00007]

    def test_poll_fallback_and_stall_counter(self, brx, fake_hw, monkeypatch):
        monkeypatch.delattr(fake_hw.rp2, "DMA")
        FakeSM.irq_fails = True
        cap = brx.Capture(6)
        assert cap.start() == "poll"
        sm = FakeSM.last
        sm.fifo = [1, 2, 3]
        fake_hw.regs[brx.PIO_FDEBUG] = 1
        assert cap.produced() == 3 and cap.stalls == 1
        assert fake_hw.regs[("w", brx.PIO_FDEBUG)] == 1

    def test_no_rp2_reports_error(self, brx, monkeypatch):
        monkeypatch.setattr(brx, "rp2", None)
        rx = brx.BeanRx(lambda d, m: True, 3, 6)
        assert rx.enable(True) is None and rx.on is False and "no rp2" in rx.err
        rx.poll()                                 # harmless
