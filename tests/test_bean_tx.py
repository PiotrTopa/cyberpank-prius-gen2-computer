"""Host-side tests of the powerbox BEAN transmitter (powerbox/bean_tx.py, fw 2.4.0).

The PIO programs are assembled by MicroPython's own assembler and run cycle
by cycle against a model of the pin, the transceiver and the bus
(tests/bean_pio_sim.py); the production ``Hw`` class drives them through
fake ``machine`` / ``rp2`` objects, and the real receiver (bean_rx.BeanRx)
decodes the resulting bus waveform. Nothing below needs hardware.
"""

import random
import sys
import types

import pytest

import bean_pio_sim as S
import bean_wire as W

BASE_US = 5_000_000
TEST = dict(pri=0, dst="FE", mes="48", data="00")


@pytest.fixture(scope="module")
def fwm():
    rp2, brx, btx = S.load_firmware()
    return types.SimpleNamespace(rp2=rp2, brx=brx, btx=btx)


class Rig:
    """Board + receiver + transmitter on simulated time."""

    def __init__(self, fwm, monkeypatch, rise=1.0, fall=5.0, traffic=True, tb=100.0):
        self.m = fwm
        self.board = b = S.Board(rise, fall)
        monkeypatch.setattr(S, "BOARD", b)
        monkeypatch.setitem(sys.modules, "machine", S.fake_machine())
        monkeypatch.setattr(S.FakeTimer, "fail", False)
        self.in_timer = False
        self.guard = True

        def t_us():
            if self.in_timer:
                b.advance(b.t + 5.0)          # a busy-wait moves time on
            return BASE_US + int(b.t)

        def t_ms():
            return (BASE_US + int(b.t)) // 1000
        for mod in (fwm.brx, fwm.btx):
            monkeypatch.setattr(mod, "ticks_us", t_us)
            monkeypatch.setattr(mod, "ticks_ms", t_ms)
        self.cap = S.FakeCapture(fwm.brx.RING)
        self.feeder = S.RxFeeder(self.cap)
        self.l3 = []
        self.l0 = []
        self.events = []
        self.rx = fwm.brx.BeanRx(lambda dev, msg: self.l3.append(msg) or True, 3, 6,
                                 capture=self.cap)
        assert self.rx.enable(True) == "dma"
        self.armed = True
        self.tx = fwm.btx.BeanTx(lambda dev, msg: self.l0.append(msg) or True, 0, self.rx,
                                 lambda: self.armed, self.idle,
                                 log=lambda kind, **f: self.events.append((kind, f)))
        self.tb = tb
        if traffic:
            self.traffic(tb=tb)

    def idle(self, ms):
        b = self.board
        for _ in range(ms):
            b.advance(b.t + 1000.0)
            self.in_timer = True
            for t in list(b.timers):
                t.cb(t)
            self.in_timer = False
            self.feeder.feed(b)
            self.rx.poll()
            if self.guard:
                self.tx.guard()

    def traffic(self, n=3, tb=100.0, gap_ms=9):
        """``n`` frames from other nodes (ACKed): the receiver measures the
        bit time and the bus counts as awake."""
        t = self.board.t + 3000.0
        for i in range(n):
            self.board.add_frame(t, 6, 0xFE, 0x2C, [0x60 + i], tb=tb)
            t += gap_ms * 1000.0
        self.idle(int((t - self.board.t) / 1000) + 25)

    def cmd(self, **kw):
        n = len(self.l0)
        r = self.tx.command(dict({"a": "bean_tx"}, **kw))
        assert self.l0[n:] == [r] and r["ack"] == "bean_tx" and r["op"] == kw.get("op", "status")
        b = self.board
        assert b.pad7() == 0 and b.func7 == "sio", "GP7 not released after %s" % kw
        assert not b.sm[0].on and not b.sm[1].on and not b.timers
        return r

    def ready(self, **kw):
        r = self.cmd(op="edge")
        assert r["ok"] == 1, r
        assert self.cmd(op="allow", v=1)["ok"] == 1
        self.idle(30)
        self.board.responder = "ack"
        return r

    def frames(self):
        out = []
        for m in self.l3:
            out += m.get("bf", [])
        return out

    def flush(self, ms=450):
        self.idle(ms)
        return self.frames()

    def max_gp7_high(self, since=0.0):
        return max([d for lv, _, d in self.board.gp7_runs(since) if lv] or [0.0])


def wire_bits(fwm, pri, dst, mes, data):
    bits, n, hdr, body, amb = fwm.btx.encode(pri, dst, mes, bytes(data))
    return [(bits >> (n - 1 - i)) & 1 for i in range(n)], hdr, amb


# ─── bit-stream builder ─────────────────────────────────────────────────────

class TestEncoder:
    def test_crc(self, fwm):
        assert fwm.btx.crc8(bytes([0x13, 0xFE, 0x24, 0x00])) == 0xB4
        assert fwm.btx.crc8(bytes([0x63, 0xFE, 0x2C, 0x64])) == 0xC1
        assert fwm.btx.crc8(bytes([0x84, 0xFE, 0xC2, 0xE9, 0xA1])) == 0x2B

    def test_doc_examples_bit_exact(self, fwm):
        # docs/BEAN_PROTOCOL.md 3.4: drive2 frame and the NHW20 coolant frame
        bits, hdr, amb = wire_bits(fwm, 1, 0xFE, 0x24, [0x00])
        assert "".join(map(str, bits)) == ("1" "0001" "0011" "111011110" "00100100"
                                           "000100000" "110110100" "01111110")
        assert hdr == 1 + 8 + 9 + 8 and not amb
        bits, _, _ = wire_bits(fwm, 6, 0xFE, 0x2C, [0x64])
        assert "".join(map(str, bits)) == ("1" "0110" "0011" "111011110" "00101100"
                                           "01100100" "110000011" "01111110")

    def test_every_real_nhw20_frame_reencodes_to_its_wire_bits(self, fwm):
        # encode(decode(frame)) == the wire bits (bean_wire.frame_bits is the
        # reference the receiver tests decode all 8,627 frames from)
        fx = W.load_fixture()
        assert len(fx) == 8627
        for _, _, pri, ml, dst, mes, data, crc, _ in fx:
            bits, hdr, amb = wire_bits(fwm, pri, dst, mes, data)
            ref = W.frame_bits(pri, dst, mes, data, crc=crc, rsp=(), eof=0)
            assert bits == ref, (pri, dst, mes, data)
            assert amb == W.needs_stuff_after_crc(pri, dst, mes, data)

    def test_random_frames_match_reference_and_stuffing_rules(self, fwm):
        rng = random.Random(21)
        n_amb = 0
        for _ in range(3000):
            pri, dst, mes, data = W.random_frame(rng)
            bits, hdr, amb = wire_bits(fwm, pri, dst, mes, data)
            assert bits == W.frame_bits(pri, dst, mes, data, rsp=(), eof=0)
            body = bits[:-8]
            assert bits[-8:] == [0, 1, 1, 1, 1, 1, 1, 0]
            run = 0
            for i, v in enumerate(body):          # no run above 5 inside SOF..CRC
                run = run + 1 if i and v == body[i - 1] else 1
                assert run <= 5
            assert fwm.btx.max_dominant_run(*fwm.btx.encode(pri, dst, mes, data)[:2]) <= 6
            assert bits[:hdr] == W.frame_bits(pri, dst, mes, data)[:hdr]
            n_amb += amb
        assert n_amb > 20                         # the unresolved case does occur

    def test_stuff_bit_after_crc_is_not_sent(self, fwm):
        rng = random.Random(7)
        seen = set()
        while len(seen) < 2:
            f = W.random_frame(rng)
            if not W.needs_stuff_after_crc(*f):
                continue
            bits, _, amb = wire_bits(fwm, *f)
            assert amb
            crc_last = bits[-9]
            assert bits[-13:-8] == [crc_last] * 5          # CRC ends in a run of 5
            assert bits == W.frame_bits(*f, rsp=(), eof=0, stuff_after_crc=False)
            assert bits != W.frame_bits(*f, rsp=(), eof=0, stuff_after_crc=True)
            seen.add(crc_last)

    def test_header_length_counts_a_stuff_bit_after_mes(self, fwm):
        # MES 0x1F ends in five 1s: the stuff bit belongs to the header
        bits, hdr, _ = wire_bits(fwm, 2, 0x40, 0x1F, [0x55])
        ref = W.frame_bits(2, 0x40, 0x1F, [0x55])
        # 25 raw bits + the stuff bit inside DST 0x40 + the one after MES
        assert bits[:hdr] == ref[:hdr] and hdr == 1 + 8 + 8 + 8 + 2
        assert bits[hdr - 1] == 0 and bits[hdr - 6:hdr - 1] == [1] * 5

    def test_words_msb_first_zero_padded(self, fwm):
        bits, n, _, _, _ = fwm.btx.encode(1, 0xFE, 0x24, b"\x00")
        words = fwm.btx.to_words(bits, n)
        assert len(words) == 2 and n == 52
        back = []
        for w in words:
            back += [(w >> (31 - i)) & 1 for i in range(32)]
        assert back[:n] == [(bits >> (n - 1 - i)) & 1 for i in range(n)]
        assert back[n:] == [0] * (64 - n)

    def test_longest_frame_fits_the_fifo(self, fwm):
        worst = 0
        rng = random.Random(3)
        for _ in range(2000):
            data = bytes(rng.choice([0x00, 0xFF, 0x0F, 0xF0, 0x1F, 0xE0]) for _ in range(11))
            _, n, _, _, _ = fwm.btx.encode(rng.randint(0, 15), rng.choice([0, 0xFF, 0x1F]),
                                           rng.choice([0, 0xFF, 0xE0]), data)
            worst = max(worst, n)
        assert 150 <= worst <= (fwm.btx.FIFO_WORDS - 2) * 32

    def test_encoder_output_decodes_in_the_receiver(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch, traffic=False)
        rng = random.Random(5)
        frames = [W.random_frame(rng) for _ in range(60)]
        t = 3000.0
        for f in frames:
            bits, _, _ = wire_bits(fwm, *f)
            t = rig.board.add_bits(t, bits + [0, 1] + [0] * 6) + 2500.0
        got = rig.flush(int(t / 1000) + 400)
        assert [g[1:7] for g in got] == [[p, "%02X" % d, "%02X" % m, bytes(x).hex().upper(), 1, 0]
                                         for p, d, m, x in frames]


# ─── PIO programs ───────────────────────────────────────────────────────────

class TestPrograms:
    def test_sizes_fit_pio0(self, fwm):
        btx = fwm.btx
        frame = btx._frame
        assert len(frame[0]) == 25 and len(btx._edge[0]) == 25 and len(btx._wdog[0]) == 6
        assert len(frame[0]) + len(btx._wdog[0]) <= 32
        assert len(btx._edge[0]) + len(btx._wdog[0]) <= 32
        assert len(fwm.brx._runlen[0]) == 27          # PIO1: no room for a transmitter

    def test_configuration_words(self, fwm):
        btx = fwm.btx
        frame = btx._frame
        sc = frame[4]
        assert sc >> 30 == 1                           # FJOIN_TX: 8-word TX FIFO
        assert sc & (1 << 17) and not sc & (1 << 16)   # autopull, no autopush
        assert not sc & (1 << 19) and not sc & (1 << 18)   # both shift left (MSB first)
        assert frame[6] == btx._edge[6] == btx._wdog[6] == S.FakePIO.OUT_LOW
        assert btx._edge[4] >> 30 == 0                 # edge program pushes results
        # readout opcodes used through SMx_INSTR
        assert fwm.rp2.asm_pio_encode("push(noblock)", 0) == btx.OP_PUSH
        assert fwm.rp2.asm_pio_encode("mov(isr, y)", 0) == btx.OP_MOV_ISR_Y
        assert btx.SM0_INSTR == S.REG_SM0_INSTR and btx.PIO_IRQ == S.REG_IRQ

    def test_delay_fields(self, fwm):
        btx = fwm.btx
        assert btx.frame_delays(50) == (16, 8, 21)
        base = btx._frame
        delay = lambda ins: (ins >> 8) & 0x1F
        assert not btx.patch_frame(btx.frame_delays(50))           # as assembled
        assembled = list(base[0])
        assert [delay(base[0][i]) for i in (11, 12, 19, 21, 22, 23)] == [1, 16, 16, 8, 21, 31]
        assert sum(delay(i) != 0 for i in base[0]) == 6
        try:
            for sp in range(btx.SP_MIN, btx.SP_MAX + 1):
                for fall in (0, 5, 20, 40, 60):
                    d1, d2, d3 = btx.frame_delays(sp, fall, 1653)
                    assert 8 + d1 + d2 == btx.NCYC and d2 >= 0 and d3 <= 31
                    assert abs(d1 * 100 / btx.NCYC - sp) <= 2
                    # RSP1: half a bit after the bus edge (our edge + fall),
                    # at most 26 cycles after ours
                    want = 16 + fall / (103.3 / 32)
                    assert d3 - 5 == min(26, round(want)) or abs(d3 - 5 - want) <= 0.51
                    btx.patch_frame((d1, d2, d3))
                    # same instructions, only the four delay fields differ
                    assert [i & 0xE0FF for i in base[0]] == [i & 0xE0FF for i in assembled]
                    assert [delay(base[0][i]) for i in (11, 12, 19, 21, 22, 23)] == [1, d1, d1, d2, d3, 31]
            assert btx.patch_frame((24, 0, 31)) is False            # nothing changed
            assert btx.patch_frame(btx.frame_delays(50)) is True
        finally:
            btx.patch_frame(btx.frame_delays(50))
        assert list(base[0]) == assembled

    def test_terminal_instructions_cannot_run_away(self, fwm):
        # the two blocking irqs are followed by harmless code, the last
        # instruction wraps to "stop"
        frame = fwm.btx._frame
        code = list(frame[0])
        wrap_target = (frame[3] >> 7) & 0x1F
        wrap = (frame[3] >> 12) & 0x1F
        assert wrap == len(code) - 1
        assert code[wrap_target] == 0xE000                       # stop: set pins, 0
        assert code[wrap_target + 1] == 0xC027                   # irq wait 7
        assert code[wrap_target - 1] == 0xC024                   # irq wait 4 (lost)
        assert code[wrap_target + 2] == 0xC005                   # fault: irq 5
        assert code[wrap_target + 3] == wrap_target              # jmp stop
        e = fwm.btx._edge
        assert ((e[3] >> 12) & 0x1F) == len(e[0]) - 1 and e[0][-1] == 0xC027


    def test_only_irq_flags_4_to_7(self, fwm):
        # flags 0..3 can be routed to a CPU interrupt (MicroPython's handler
        # clears them); the programs must not depend on those
        btx = fwm.btx
        used = set()
        for prog in (btx._frame, btx._edge, btx._wdog):
            for ins in prog[0]:
                if ins >> 13 == 6:
                    assert not ins & 0x10                # no "rel"
                    used.add(ins & 7)
        assert used == {4, 5, 6, 7}
        assert (btx.F_LOST, btx.F_FAULT, btx.F_WDOG, btx.F_STOP) == (0x10, 0x20, 0x40, 0x80)


def run_frame_prog(fwm, monkeypatch, words, sp=50, tb=100.0, board=None, wd=True, ms=30,
                   fall_us=0):
    """The frame program alone (through the production Hw) on a board."""
    btx = fwm.btx
    b = board or S.Board()
    monkeypatch.setattr(S, "BOARD", b)
    monkeypatch.setitem(sys.modules, "machine", S.fake_machine())
    b.wd_disabled = not wd
    hw = btx.Hw()
    btx.patch_frame(btx.frame_delays(sp, fall_us, int(tb * 16)))
    hw.start(btx._frame, int(btx.NCYC * 1e6 / tb), words)
    b.advance(b.t + ms * 1000.0)
    return b, hw


class TestFrameProgram:
    def _words(self, fwm, bits, ifs_bits=11):
        btx = fwm.btx
        acc = 0
        for v in bits:
            acc = (acc << 1) | v
        w = [ifs_bits * btx.NCYC // 2 - 1, len(bits) - 1] + btx.to_words(acc, len(bits))
        return w + [0] * (8 - len(w))

    def test_clean_frame_waveform_and_timing(self, fwm, monkeypatch):
        bits, _, _ = wire_bits(fwm, 6, 0xFE, 0x2C, [0x64])
        b, hw = run_frame_prog(fwm, monkeypatch, self._words(fwm, bits), tb=103.3)
        assert b.flags == fwm.btx.F_STOP                    # complete, no lost/fault
        runs = b.gp7_runs()
        sof = runs[1][1]
        # idle check: 11 bit times before SOF (+ ~10 start-up cycles)
        assert 11 * 103.3 <= sof <= 11 * 103.3 + 40
        on_wire = S.runs_to_bits(runs[1:-1], 103.3)
        assert on_wire == bits[:-1]                         # last EOM bit is the released tail
        for lv, _, d in runs[1:-1]:                         # every run a whole number of bits
            n = round(d / 103.3)
            assert abs(d - n * 103.3) < 0.2, d
        assert max(d for lv, _, d in runs if lv) == pytest.approx(6 * 103.3, abs=0.2)
        assert hw.tx_fifo() == 8 - 2 - len(fwm.btx.to_words(0, len(bits)))
        isr, y = hw.readout()
        assert y == 0xFFFFFFFF and isr & 3 == 3             # nobody answered: both recessive

    def test_reads_back_at_the_sample_point(self, fwm, monkeypatch):
        # a release edge that reaches GP6 before the sample point is fine,
        # one that arrives after it reads as "dominant while sending recessive"
        bits, _, _ = wire_bits(fwm, 6, 0xFE, 0x2C, [0x64])
        for sp, fall, ok in ((50, 48.0, True), (50, 52.0, False), (75, 73.0, True),
                             (75, 77.0, False), (25, 23.0, True), (25, 27.0, False)):
            b, hw = run_frame_prog(fwm, monkeypatch, self._words(fwm, bits), sp=sp,
                                   board=S.Board(rise=1.0, fall=fall))
            assert bool(b.flags & fwm.btx.F_LOST) != ok, (sp, fall)
            if not ok:
                _, y = hw.readout()
                assert len(bits) - 1 - y == bits.index(0)   # the first recessive bit
        # the dominant edge: seen late = line fault at the SOF
        for sp, rise, ok in ((50, 48.0, True), (50, 52.0, False)):
            b, _ = run_frame_prog(fwm, monkeypatch, self._words(fwm, bits), sp=sp,
                                  board=S.Board(rise=rise, fall=5.0))
            assert bool(b.flags & fwm.btx.F_FAULT) != ok

    def test_waits_for_a_continuously_idle_bus(self, fwm, monkeypatch):
        bits, _, _ = wire_bits(fwm, 0, 0xFE, 0x48, [0x00])
        # a 20 us dominant glitch anywhere in the idle check: nothing is driven
        for at in (50.0, 500.0, 1090.0):
            b = S.Board()
            b.add_dominant(at, at + 20.0)
            b, hw = run_frame_prog(fwm, monkeypatch, self._words(fwm, bits), board=b)
            assert b.flags == fwm.btx.F_STOP and len(b.gp7_log) == 1, at
            assert hw.tx_fifo() >= 6                        # still in the idle check
        # started inside another node's frame (in a recessive stretch): the
        # next dominant bit ends the attempt
        other = W.frame_bits(6, 0xFE, 0x2C, [0x64], rsp=(0, 0), eof=0)
        b = S.Board()
        b.add_bits(0.0, other)
        b.advance(550.0)                                    # inside "0011" of PRI/ML
        b, hw = run_frame_prog(fwm, monkeypatch, self._words(fwm, bits), board=b)
        assert b.flags == fwm.btx.F_STOP and len(b.gp7_log) == 1
        # started right after that frame's last dominant edge (no ACK came):
        # SOF follows 11 bit times of silence, not earlier
        b = S.Board()
        end = b.add_bits(0.0, other) - 100.0                # end of the EOM ones
        b.advance(end + 20.0)
        b, hw = run_frame_prog(fwm, monkeypatch, self._words(fwm, bits), board=b)
        sof = b.gp7_runs()[1][1]
        assert end + 11 * 100.0 <= sof <= end + 11 * 100.0 + 70.0

    def test_blind_window_before_sof_is_short(self, fwm, monkeypatch):
        # from the last idle sample to our SOF edge: <= 8 PIO cycles (26 us)
        bits, _, _ = wire_bits(fwm, 0, 0xFE, 0x48, [0x00])
        b, _ = run_frame_prog(fwm, monkeypatch, self._words(fwm, bits))
        sof = b.gp7_runs()[1][1]
        for lead, drove in ((40.0, False), (20.0, True)):
            b = S.Board()
            b.add_dominant(sof - lead, sof - lead + 100.0)
            b, _ = run_frame_prog(fwm, monkeypatch, self._words(fwm, bits), board=b)
            assert (len(b.gp7_log) > 1) == drove, lead

    def test_cannot_hold_dominant_longer_than_6_bits_whatever_the_data(self, fwm, monkeypatch):
        # a stream of 150 ones (never built by the firmware): the 7th bit of
        # every run is forced recessive by the program itself
        b, _ = run_frame_prog(fwm, monkeypatch, self._words(fwm, [1] * 150), tb=108.0, ms=40)
        highs = [d for lv, _, d in b.gp7_runs() if lv]
        assert len(highs) >= 20
        # 6 bit times; the forced release comes 2 PIO cycles (6.75 us) late
        assert max(highs) == pytest.approx(6 * 108.0 + 2 * 108.0 / 32, abs=0.5)
        assert not b.flags & fwm.btx.F_WDOG                 # legal maximum: no watchdog trip

    def test_runs_with_garbage_in_x_and_y(self, fwm, monkeypatch):
        bits, _, _ = wire_bits(fwm, 0, 0xFE, 0x48, [0x00])
        for x, y in ((0, 0), (0xFFFFFFFF, 0xFFFFFFFF), (7, 123456)):
            b = S.Board()
            b.sm[0].x, b.sm[0].y, b.sm[1].x = x, y, x
            b, _ = run_frame_prog(fwm, monkeypatch, self._words(fwm, bits), board=b)
            assert b.flags == fwm.btx.F_STOP
            assert S.runs_to_bits(b.gp7_runs()[1:-1], 100.0) == bits[:-1]

    def test_rsp_bits_sampled_mid_bit(self, fwm, monkeypatch):
        bits, _, _ = wire_bits(fwm, 0, 0xFE, 0x48, [0x00])
        for resp, tb_r, want in (("ack", 100.0, 0b01), ("nak", 100.0, 0b10), (None, 100.0, 0),
                                 ("ack", 95.0, 0b01), ("ack", 108.0, 0b01)):
            b = S.Board()
            b.responder, b.resp_tb = resp, tb_r
            b, hw = run_frame_prog(fwm, monkeypatch, self._words(fwm, bits), board=b, tb=103.3)
            isr, y = hw.readout()
            assert (~isr) & 3 == want and y == 0xFFFFFFFF, (resp, tb_r)

    def test_rsp_sample_follows_the_measured_fall_time(self, fwm, monkeypatch):
        # The answering node times its ACK from the bus edge, which lags our
        # release by the fall time. A slow node (108 us bits) behind a 38 us
        # fall starts its ACK 254 us after our edge: a sample 2.5 of our bit
        # times after our edge would sit 4 us inside it, or miss it.
        bits, _, _ = wire_bits(fwm, 0, 0xFE, 0x48, [0x00])
        for tb_r in (95.0, 100.0, 108.0):
            for fall in (2.0, 20.0, 38.0):
                b = S.Board(rise=1.0, fall=fall)
                b.responder, b.resp_tb = "ack", tb_r
                b, hw = run_frame_prog(fwm, monkeypatch, self._words(fwm, bits), board=b,
                                       tb=103.3, fall_us=int(fall))
                isr, y = hw.readout()
                assert (~isr) & 3 == 0b01 and y == 0xFFFFFFFF, (tb_r, fall)
                # where the RSP2 sample fell inside the ACK pulse
                ack_on, ack_off = b.f_on[-1], b.f_off[-1]
                eom_fall = [t for t, lv in b.gp7_log if not lv][-1]
                d3 = fwm.btx.frame_delays(50, int(fall), int(103.3 * 16))[2]
                sample = eom_fall + (d3 - 5) * 103.3 / 32 + 2 * 103.3
                assert ack_on + 15.0 <= sample <= ack_off - 15.0, (tb_r, fall)
        # a slow release at a late sample point: the 26-cycle limit still
        # lands inside the pulse
        for tb_r in (95.0, 108.0):
            b = S.Board(rise=1.0, fall=58.0)
            b.responder, b.resp_tb = "ack", tb_r
            b, hw = run_frame_prog(fwm, monkeypatch, self._words(fwm, bits), board=b,
                                   tb=103.3, sp=75, fall_us=58)
            assert (~hw.readout()[0]) & 3 == 0b01, tb_r
        fwm.btx.patch_frame(fwm.btx.frame_delays(50))

    def test_readout_does_not_block_without_data(self, fwm, monkeypatch):
        bits, _, _ = wire_bits(fwm, 0, 0xFE, 0x48, [0x00])
        b, hw = run_frame_prog(fwm, monkeypatch, self._words(fwm, bits))
        monkeypatch.setattr(S.FakeMem, "__setitem__", lambda self, a, v: None)   # exec does nothing
        assert hw.readout() is None


class TestWatchdogSm:
    def test_trips_at_0_8_ms_and_latches_the_pin_off(self, fwm, monkeypatch):
        btx = fwm.btx
        b = S.Board()
        b.freeze_on_rise = True                            # SM0 dies with GP7 high
        words = TestFrameProgram()._words(fwm, [1] * 40)
        b, hw = run_frame_prog(fwm, monkeypatch, words, board=b, ms=10)
        highs = [(t0, d) for lv, t0, d in b.gp7_runs() if lv]
        assert len(highs) == 1
        assert 780.0 <= highs[0][1] <= 840.0               # 64..66 cycles of 12.5 us
        assert b.flags & btx.F_WDOG and b.dir7 == 0 and b.pad7() == 0
        # ... and stays off even if SM0 comes back and drives again
        b.sm[0].on = True
        b.sm[0].next_t = b.t
        b.advance(b.t + 5000.0)
        assert b.pad7() == 0 and len([1 for lv, _, _ in b.gp7_runs() if lv]) == 1

    def test_silent_during_legal_frames(self, fwm, monkeypatch):
        bits, _, _ = wire_bits(fwm, 15, 0xFF, 0xFF, [0xFF] * 11)
        b, _ = run_frame_prog(fwm, monkeypatch, TestFrameProgram()._words(fwm, bits), tb=108.0)
        assert b.flags == btx_stop(fwm) and b.dir7 == 1


def btx_stop(fwm):
    return fwm.btx.F_STOP


# ─── edge test ──────────────────────────────────────────────────────────────

class TestEdge:
    @pytest.mark.parametrize("rise,fall", [(1.0, 5.0), (3.0, 22.0), (12.0, 38.0)])
    def test_measures_rise_and_fall(self, fwm, monkeypatch, rise, fall):
        rig = Rig(fwm, monkeypatch, rise=rise, fall=fall)
        t0 = rig.board.t
        r = rig.cmd(op="edge")
        assert r["ok"] == 1 and r["ok_meas"] == 1
        assert abs(r["rise_us"] - rise) <= 1.5 and abs(r["fall_us"] - fall) <= 1.5
        (lv, start, width), = [x for x in rig.board.gp7_runs(t0) if x[0]]
        assert width == pytest.approx(rig.rx.tb16 / 16, abs=0.6)     # exactly one bit time
        assert r["w_us"] == pytest.approx(width, abs=0.6)
        # idle for 7.8 bit times before the pulse
        assert start - t0 >= 7.7 * width
        # the receiver's raw view: idle run, then our pulse as GP6 saw it
        assert r["runs"][0] < -5000 and r["rx_dom_us"] == r["runs"][1]
        assert abs(r["rx_dom_us"] - (width - rise + fall)) <= 2
        assert rig.tx.n_edges == 1 and rig.tx.edge["fall_us"] == r["fall_us"]

    def test_pulse_shows_in_the_frame_stream_flagged_own(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        n = len(rig.flush())
        rig.cmd(op="edge")
        got = rig.flush()[n:]
        assert len(got) == 1 and got[0][6] == fwm.brx.E_TRUNC and got[0][7] == 1
        assert rig.rx.health() is None                      # our own pulse is no receive error

    def test_slow_fall_is_measured_but_not_ok(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch, fall=55.0)
        r = rig.cmd(op="edge")
        assert r["ok"] == 0 and r["err"] == "edge" and abs(r["fall_us"] - 55) <= 2
        assert rig.cmd(op="allow", v=1)["ok"] == 1          # measured: sample point decides
        n = len(rig.board.gp7_log)
        r = rig.cmd(op="send", **TEST)
        assert r["err"] == "edge" and "limit 40" in r["detail"]
        assert len(rig.board.gp7_log) == n
        rig.cmd(op="sp", pct=75)                            # no sample point helps
        assert rig.cmd(op="send", **TEST)["err"] == "edge"
        assert len(rig.board.gp7_log) == n

    def test_open_bus_fall_time_times_out(self, fwm, monkeypatch):
        # the bench case: nothing pulls the line down (~340 us is measured;
        # beyond 800 us the program stops waiting)
        rig = Rig(fwm, monkeypatch, fall=340.0)
        r = rig.cmd(op="edge")
        assert r["ok"] == 0 and abs(r["fall_us"] - 340) <= 2
        rig2 = Rig(fwm, monkeypatch, fall=2000.0)
        r = rig2.cmd(op="edge")
        assert r["ok"] == 0 and r["fall_us"] is None and r["ok_meas"] == 0
        assert rig2.cmd(op="allow", v=1)["err"] == "edge"

    def test_dead_driver_no_rise(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.board.driver_ok = False
        r = rig.cmd(op="edge")
        assert r["ok"] == 0 and r["rise_us"] is None and "no rise" in r["detail"]
        assert rig.max_gp7_high() == pytest.approx(100.0, abs=1.5)    # released on time anyway

    def test_busy_bus_waits_for_the_gap(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        b = rig.board
        t0 = b.t
        end = b.add_frame(t0 + 300.0, 6, 0xFE, 0x2C, [0x64])       # incl. ACK + EOF
        r = rig.cmd(op="edge")
        assert r["ok"] == 1 and rig.tx.n_edges == 1
        (lv, start, width), = [x for x in b.gp7_runs(t0) if x[0]]
        last_dom = max(t + d for v, t, d in b.bus_runs(t0) if v and t < start)
        assert start - last_dom >= 7.7 * 100.0                    # idle check after the frame
        # our pulse is told apart from the frame's last dominant run
        assert abs(r["rx_dom_us"] - (width - 1.0 + 5.0)) <= 2
        assert all(g[6] == 0 for g in rig.flush() if len(g) == 7)  # the frame was not disturbed

    def test_permanently_busy_bus_no_pulse(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        b = rig.board
        t = b.t + 200.0
        for i in range(40):                                 # EOF + 1 bit time apart
            t = b.add_frame(t, 6, 0xFE, 0x2C, [i]) + 100.0
        t0 = b.t
        r = rig.cmd(op="edge")
        assert r["ok"] == 0 and r["err"] == "busy"
        assert not [1 for lv, _, _ in b.gp7_runs(t0) if lv]
        assert rig.tx.n_edges == 0 and rig.tx.edge is None and rig.tx.budget(0) == 50
        assert 150 <= (b.t - t0) / 1000 <= 165

    def test_stale_run_before_the_tap_is_not_taken_for_the_pulse(self, fwm, monkeypatch):
        # a frame ended 3 ms ago: its last dominant run reaches the tap first
        rig = Rig(fwm, monkeypatch)
        b = rig.board
        end = b.add_frame(b.t + 200.0, 6, 0xFE, 0x2C, [0x64], tb=100.0)
        rig.idle(int((end - b.t) / 1000) + 3)
        r = rig.cmd(op="edge")
        assert r["ok"] == 1
        assert r["runs"][0] > 0 and r["runs"][1] < -700            # stale ACK bit, then the gap
        assert r["rx_dom_us"] == r["runs"][2] and abs(r["rx_dom_us"] - 104) <= 2

    def test_needs_arming_and_a_healthy_receiver(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.armed = False
        assert rig.cmd(op="edge")["err"] == "disarmed"
        rig.armed = True
        rig.rx.enable(False)
        r = rig.cmd(op="edge")
        assert r["err"] == "rx" and r["detail"] == "rx off"
        assert len(rig.board.gp7_log) == 1

    def test_fault_during_the_pulse_latches_and_cleans_up(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        b = rig.board
        b.wd_disabled = True
        b.freeze_on_rise = True                             # SM0 dies with GP7 high
        t0 = b.t
        r = rig.cmd(op="edge")
        assert r["ok"] == 0 and r["err"] == "fault" and rig.tx.fault == "gp7 high > 1 ms"
        assert max(d for lv, _, d in b.gp7_runs(t0) if lv) <= 1900.0
        assert rig.rx.raw is None and not rig.rx.tapping and not rig.rx.hold
        assert rig.tx.edge is None and rig.cmd(op="allow", v=1)["err"] == "fault"

    def test_bench_allows_the_nominal_bit_time(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch, traffic=False)
        rig.idle(50)
        assert rig.cmd(op="edge")["err"] == "tb"
        assert rig.cmd(op="bench", v=1)["ok"] == 1
        r = rig.cmd(op="edge")
        assert r["ok"] == 1 and r["w_us"] == 100.0


# ─── frames: arbitration, errors, ACK, retries ──────────────────────────────

def _first_loss(ours, theirs):
    for i, (a, b) in enumerate(zip(ours, theirs)):
        if a == 0 and b == 1:
            return i
    return None


class TestSend:
    def test_test_frame_acked_first_time(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch, tb=103.3)
        rig.ready()
        n = len(rig.flush())
        t0 = rig.board.t
        r = rig.cmd(op="send", **TEST)
        assert r["ok"] == 1 and r["acked"] == 1 and r["rx_rsp"] == 1 and r["attempts"] == 1
        assert r["lost_arb"] == r["bit_err"] == r["fault"] == r["busy"] == 0
        assert "err" not in r and 5 <= r["t_ms"] <= 40
        assert (r["pri"], r["dst"], r["mes"], r["data"]) == (0, "FE", "48", "00")
        # on the wire: exactly the frame, at the receiver's measured bit time
        bits, _, _ = wire_bits(fwm, 0, 0xFE, 0x48, [0])
        tb = rig.rx.tb16 / 16
        assert abs(tb - 103.3) < 0.5
        runs = rig.board.gp7_runs(t0)
        assert S.runs_to_bits(runs[1:-1], tb) == bits[:-1]
        # ... and in the receiver's stream, in sequence, flagged as ours
        got = rig.flush()[n:]
        assert got == [[got[0][0], 0, "FE", "48", "00", 1, 0, 1]]
        assert 0xFE48 not in rig.rx.seen                    # our own frame is not "seen"

    def test_foreign_frames_carry_no_own_flag(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        got = rig.flush()
        assert len(got) == 3 and all(len(g) == 7 for g in got)
        assert rig.rx.seen == {0xFE2C}

    @pytest.mark.parametrize("field,contender", [
        ("pri", (8, 0xFE, 0x48, [0x00])),
        ("ml", (0, 0xFE, 0x48, [0x00, 0x00])),
        ("dst", (0, 0xFF, 0x48, [0x00])),
        ("mes", (0, 0xFE, 0x49, [0x00])),
    ])
    def test_arbitration_lost_in_each_header_field(self, fwm, monkeypatch, field, contender):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        b = rig.board
        ours, hdr, _ = wire_bits(fwm, 0, 0xFE, 0x48, [0])
        theirs = W.frame_bits(*contender)
        pos = _first_loss(ours, theirs)
        assert pos is not None and pos < hdr
        marks = {}

        def contend(t):
            marks["sof"] = t
            b.add_bits(t + 1.0, theirs)                     # same start, their rise delay
        b.on_rise = contend
        n = len(rig.flush())
        r = rig.cmd(op="send", retries=0, **TEST)
        assert r["ok"] == 0 and r["err"] == "lost_arb" and r["lost_arb"] == 1 and r["bit_err"] == 0
        assert r["attempts"] == 1 and r["pos"] == pos and r["acked"] is None
        # we stopped driving within the bit we lost in: GP7 low from before
        # the losing bit's sample point on, for good
        highs = [(t0, d) for lv, t0, d in b.gp7_runs(marks["sof"] - 1) if lv]
        assert highs and highs[-1][0] + highs[-1][1] <= marks["sof"] + pos * 100.0 + 1.0
        sent = S.runs_to_bits(b.gp7_runs(marks["sof"])[:-1], 100.0)
        assert sent == ours[:len(sent)] and set(ours[len(sent):pos]) <= {0}
        # the winner's frame went through undisturbed
        got = rig.flush()[n:]
        assert [g[1:7] for g in got] == [[contender[0], "%02X" % contender[1], "%02X" % contender[2],
                                          bytes(contender[3]).hex().upper(), 1, 0]]
        assert len(got[0]) == 7                             # not ours

    def test_lost_then_retried_after_the_winner(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        b = rig.board
        theirs = W.frame_bits(8, 0xFE, 0x7F, [0, 0x10, 0, 0, 0, 0])
        b.on_rise = lambda t: b.add_bits(t + 1.0, theirs)
        n = len(rig.flush())
        r = rig.cmd(op="send", **TEST)
        assert r["ok"] == 1 and r["attempts"] == 2 and r["lost_arb"] == 1 and r["acked"] == 1
        got = rig.flush()[n:]
        assert [(g[3], len(g)) for g in got] == [("7F", 7), ("48", 8)]
        # our retry started only after the winner's frame + 11 idle bit times
        t_7f_end = max(t0 + d for lv, t0, d in b.bus_runs() if lv and t0 < b.gp7_log[-2][0] - 5000)
        sof2 = [t0 for lv, t0, d in b.gp7_runs() if lv and t0 > t_7f_end][0]
        assert sof2 - t_7f_end >= 11 * 100.0

    def test_collision_after_the_header_counts_as_bit_error(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        b = rig.board
        ours, hdr, _ = wire_bits(fwm, 0, 0xFE, 0x48, [0])
        theirs = W.frame_bits(0, 0xFE, 0x48, [0x80])
        pos = _first_loss(ours, theirs)
        assert pos >= hdr
        b.on_rise = lambda t: b.add_bits(t + 1.0, theirs)
        r = rig.cmd(op="send", retries=0, **TEST)
        assert r["err"] == "bit_err" and r["bit_err"] == 1 and r["lost_arb"] == 0 and r["pos"] == pos

    def test_noise_on_a_recessive_bit_then_clean_retry(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        b = rig.board
        ours, hdr, _ = wire_bits(fwm, 0, 0xFE, 0x48, [0])
        k = max(i for i, v in enumerate(ours[:-8]) if v == 0)        # a late recessive bit
        b.on_rise = lambda t: b.add_dominant(t + k * 100.0 + 30.0, t + k * 100.0 + 70.0)
        r = rig.cmd(op="send", **TEST)
        assert r["ok"] == 1 and r["attempts"] == 2 and r["bit_err"] == 1 and r["pos"] == k

    def test_position_falls_back_to_the_fifo_level(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        b = rig.board
        monkeypatch.setattr(fwm.btx.Hw, "readout", lambda self: None)
        b.on_rise = lambda t: b.add_bits(t + 1.0, W.frame_bits(8, 0xFE, 0x2C, [0]))
        r = rig.cmd(op="send", retries=0, **TEST)
        assert r["err"] == "lost_arb" and r["pos"] is None
        b.responder = "ack"
        long = dict(pri=0, dst="FE", mes="48", data="00" * 8)
        ours, hdr, _ = wire_bits(fwm, 0, 0xFE, 0x48, [0] * 8)
        b.on_rise = lambda t: b.add_dominant(t + 60 * 100.0 + 30.0, t + 60 * 100.0 + 70.0)
        assert ours[60] == 0
        rig.idle(1200)
        r = rig.cmd(op="send", retries=0, wake=1, **long)
        assert r["err"] == "bit_err" and r["pos"] is None
        # ... and the RSP comes from the receiver's decode of our frame
        rig.idle(1200)
        r = rig.cmd(op="send", wake=1, **TEST)
        assert r["ok"] == 1 and r["acked"] == 1 and r["rx_rsp"] == 1

    def test_line_fault_never_retried_and_latched(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        b = rig.board
        b.driver_ok = False                                 # GP7 no longer moves the bus
        t0 = b.t
        r = rig.cmd(op="send", **TEST)
        assert r["ok"] == 0 and r["err"] == "fault" and r["fault"] == 1 and r["attempts"] == 1
        highs = [d for lv, _, d in b.gp7_runs(t0) if lv]
        assert len(highs) == 1 and highs[0] <= 70.0         # released right after the first sample
        assert rig.tx.fault == "line fault" and not rig.tx.allow
        assert ("bean_tx_fault", {"why": "line fault"}) in rig.events
        n = len(b.gp7_log)
        assert rig.cmd(op="send", **TEST)["err"] == "fault"
        assert rig.cmd(op="allow", v=1)["err"] == "fault"
        assert len(b.gp7_log) == n
        # a clean edge test clears it; frames need "allow" again
        b.driver_ok = True
        rig.idle(30)
        assert rig.cmd(op="edge")["ok"] == 1 and rig.tx.fault is None
        assert rig.cmd(op="send", **TEST)["err"] == "frames_off"

    def test_no_ack_retries_with_backoff(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        b = rig.board
        b.responder = None
        t0 = b.t
        n = len(rig.flush())
        t0 = b.t
        r = rig.cmd(op="send", **TEST)
        assert r["ok"] == 0 and r["err"] == "no_ack" and r["acked"] == 0 and r["attempts"] == 3
        ours, _, _ = wire_bits(fwm, 0, 0xFE, 0x48, [0])
        sofs = [t for (t, lv), (tp, _) in zip(b.gp7_log[1:], b.gp7_log) if lv and t > t0 and t - tp > 1000]
        assert len(sofs) == 3
        gaps = [b_ - a for a, b_ in zip(sofs, sofs[1:])]
        frame_us = len(ours) * 100.0
        assert all(g >= frame_us + 10000 for g in gaps)     # never back to back
        assert gaps[1] >= gaps[0] + 9000                    # backoff grows per attempt
        got = rig.flush()[n:]
        assert [g[1:] for g in got] == [[0, "FE", "48", "00", 0, 0, 1]] * 3

    @pytest.mark.parametrize("retries", [0, 1, 3])
    def test_retry_count_configurable(self, fwm, monkeypatch, retries):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        rig.board.responder = None
        r = rig.cmd(op="send", retries=retries, **TEST)
        assert r["attempts"] == retries + 1 and rig.tx.n_frames == retries + 1

    def test_nak_is_retried_and_reported_raw(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        rig.board.responder = "nak"
        r = rig.cmd(op="send", retries=1, **TEST)
        assert r["ok"] == 0 and r["err"] == "nak" and r["acked"] == 2 and r["rx_rsp"] == 2
        assert r["attempts"] == 2

    def test_wake_up_first_frame_unanswered_second_acked(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        rig.idle(1500)                                      # bus falls asleep
        assert rig.rx.bus() == 1
        n = len(rig.board.gp7_log)
        r = rig.cmd(op="send", **TEST)
        assert r["err"] == "asleep" and len(rig.board.gp7_log) == n
        rig.board.resp_skip = 1                             # ECUs still waking up
        r = rig.cmd(op="send", wake=1, **TEST)
        assert r["ok"] == 1 and r["attempts"] == 2 and r["acked"] == 1
        assert rig.rx.bus() == 0
        r = rig.cmd(op="edge")
        assert r["ok"] == 1
        rig.idle(1500)
        assert rig.cmd(op="edge")["err"] == "asleep"
        assert rig.cmd(op="edge", wake=1)["ok"] == 1

    def test_busy_bus_waits_and_then_sends(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        b = rig.board
        t = b.t + 200.0
        for i in range(4):                                  # a burst: EOF + 3 bit times apart
            t = b.add_frame(t, 6, 0xFE, 0x2C, [0x70 + i]) + 300.0
        n = len(rig.flush(0))
        r = rig.cmd(op="send", **TEST)
        assert r["ok"] == 1 and r["attempts"] == 1 and r["busy"] >= 1
        got = rig.flush()
        assert [g[3] for g in got[-5:]] == ["2C"] * 4 + ["48"]
        assert all(g[6] == 0 for g in got[-5:])             # nobody's frame was damaged

    def test_gives_up_on_a_permanently_busy_bus(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        b = rig.board
        t = b.t + 200.0
        for i in range(120):                                # back to back for 0.7 s
            t = b.add_frame(t, 6, 0xFE, 0x2C, [i]) + 300.0
        t0 = b.t
        r = rig.cmd(op="send", **TEST)
        assert r["ok"] == 0 and r["err"] == "busy" and r["attempts"] == 0
        assert not [1 for lv, _, _ in b.gp7_runs(t0) if lv]
        assert 390 <= r["t_ms"] <= 420

    def test_sample_point_setting_is_used(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch, fall=30.0)
        r = rig.cmd(op="edge")
        assert r["ok"] == 1 and abs(r["fall_us"] - 30) <= 2
        rig.cmd(op="allow", v=1)
        rig.board.responder = "ack"
        assert rig.cmd(op="sp", pct=30) == {"ok": 1, "sp": 30, "ack": "bean_tx", "op": "sp"}
        r = rig.cmd(op="send", **TEST)                          # 30 + 10 > 31 us
        assert r["err"] == "edge" and "sample point" in r["detail"]
        rig.cmd(op="sp", pct=50)
        r = rig.cmd(op="send", **TEST)
        assert r["ok"] == 1, r
        for bad in (24, 76, "50", None, 50.5):
            assert rig.cmd(op="sp", pct=bad)["err"] == "args"

    def test_changed_delays_are_loaded_into_the_pio_again(self, fwm, monkeypatch):
        # the PIO keeps its own copy of a program: after the sample point or
        # the measured fall time changes, the program must be re-added
        rig = Rig(fwm, monkeypatch, fall=5.0)
        rig.ready()
        rig.cmd(op="sp", pct=25)
        assert rig.cmd(op="send", **TEST)["ok"] == 1            # loaded with d1 = 8 (25 us)
        loaded = [e[1] for e in rig.board.loaded if e[0] is fwm.btx._frame][0]
        assert (loaded[12] >> 8) & 0x1F == 8
        rig.board.fall = 35.0                                   # the bus got slower
        rig.idle(300)
        r = rig.cmd(op="edge")
        assert r["ok"] == 1 and abs(r["fall_us"] - 35) <= 2
        assert rig.cmd(op="allow", v=1)["ok"] == 1
        assert rig.cmd(op="send", **TEST)["err"] == "edge"      # 35 + 10 > 25 us
        rig.cmd(op="sp", pct=50)
        rig.idle(300)
        r = rig.cmd(op="send", retries=0, **TEST)               # a stale d1 = 8 would lose bit 1
        assert r["ok"] == 1 and r["attempts"] == 1, r
        loaded = [e[1] for e in rig.board.loaded if e[0] is fwm.btx._frame][0]
        d3 = fwm.btx.frame_delays(50, r_fall := rig.tx.edge["fall_us"], rig.rx.tb16)[2]
        assert (loaded[12] >> 8) & 0x1F == 16 and (loaded[22] >> 8) & 0x1F == d3 > 21
        assert r_fall >= 33


# ─── safety layers ──────────────────────────────────────────────────────────

class TestGuards:
    def _untouched(self, rig, n):
        assert len(rig.board.gp7_log) == n and rig.tx.n_frames == 0

    def test_boot_state_everything_off(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        st = rig.cmd(op="status")
        assert (st["allow"], st["bench"], st["fault"], st["edge"]) == (0, 0, None, None)
        assert st["budget"] == 50 and st["test"] == "FE:48" and st["sp"] == 50
        assert st["armed"] == 1 and st["bus"] == 0 and st["rx"] is None and st["tb_meas"] == 1

    def test_arming_required_for_everything_that_drives(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        rig.armed = False
        n = len(rig.board.gp7_log)
        assert rig.cmd(op="send", **TEST)["err"] == "disarmed"
        assert rig.cmd(op="edge")["err"] == "disarmed"
        assert rig.cmd(op="allow", v=1)["err"] == "disarmed"
        assert rig.cmd(op="bench", v=1)["err"] == "disarmed"
        assert len(rig.board.gp7_log) == n
        assert rig.cmd(op="status")["armed"] == 0

    def test_arming_expiring_mid_send_stops_the_retries(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        rig.board.responder = None
        calls = []

        def armed():
            calls.append(1)
            return len(calls) < 3
        rig.tx.armed = armed
        r = rig.cmd(op="send", **TEST)
        assert r["err"] == "disarmed" and r["attempts"] == 1

    def test_frames_need_the_per_boot_flag(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        assert rig.cmd(op="edge")["ok"] == 1
        n = len(rig.board.gp7_log)
        r = rig.cmd(op="send", **TEST)
        assert r["err"] == "frames_off"
        self._untouched(rig, n)
        assert rig.cmd(op="allow", v=1)["allow"] == 1
        assert rig.cmd(op="allow", v=0)["allow"] == 0
        assert rig.cmd(op="send", **TEST)["err"] == "frames_off"

    def test_allow_needs_an_edge_test_first(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        r = rig.cmd(op="allow", v=1)
        assert r["err"] == "edge" and not rig.tx.allow

    def test_block_list(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        n = len(rig.board.gp7_log)
        real = [("FE", "AB"), ("FE", "C2"), ("FE", "7F"), ("FE", "3F"), ("FE", "B6"),
                ("FE", "E1"), ("FE", "83"), ("FE", "40"), ("FE", "24"), ("FE", "91"),
                ("FE", "62"), ("52", "3F"), ("52", "48"), ("B5", "D9"), ("62", "6E"),
                ("40", "59"), ("98", "CC"), ("FF", "48"), ("FE", "DA")]
        for dst, mes in real:
            r = rig.cmd(op="send", pri=0, dst=dst, mes=mes, data="00")
            assert r["err"] == "blocked" and "real node traffic" in r["detail"], (dst, mes)
        for dst, mes in (("FE", "47"), ("FE", "49"), ("00", "48"), ("A0", "01")):
            r = rig.cmd(op="send", pri=0, dst=dst, mes=mes, data="00")
            assert r["err"] == "blocked" and "allow list" in r["detail"], (dst, mes)
        self._untouched(rig, n)

    def test_every_published_nhw20_pair_is_refused(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        pairs = {(d, m) for _, _, _, _, d, m, _, _, _ in W.load_fixture()}
        assert len(pairs) == 42 and (0xFE, 0x48) not in pairs
        assert 0x48 not in {m for _, m in pairs}            # the test MES: no DST uses it
        for d, m in pairs:
            assert rig.tx.blocked(d, m) is not None
        assert rig.tx.blocked(0xFE, 0x48) is None

    def test_every_pair_captured_on_the_owners_car_is_refused(self, fwm, monkeypatch):
        import glob
        import gzip
        import json
        import os
        files = glob.glob(os.path.join(os.path.dirname(S.HERE), "docs", "data", "bean", "*.ndjson.gz"))
        if not files:
            pytest.skip("no on-car captures in docs/data/bean")
        pairs = set()
        for fn in files:
            with gzip.open(fn, "rt") as fh:
                for line in fh:
                    d = json.loads(line)
                    if d.get("type") == "frame" and d.get("ok") and not d.get("own"):
                        pairs.add((int(d["dst"], 16), int(d["mes"], 16)))
        assert len(pairs) >= 16 and (0xFE, 0xE1) in pairs and (0xFE, 0x48) not in pairs
        rig = Rig(fwm, monkeypatch)
        for dst, mes in pairs:
            why = rig.tx.blocked(dst, mes)
            # Refused either as known node traffic or, for IDs first seen on
            # the IG-ON drive (2026-11-13) that the firmware list does not
            # carry yet, by the allow list. TODO(fw): add those to the list.
            assert why, (hex(dst), hex(mes))

    def test_an_id_seen_on_this_bus_is_refused_even_if_allowed(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        rig.board.add_frame(rig.board.t + 500.0, 2, 0xFE, 0x48, [0x01])     # a real node uses it
        rig.idle(40)
        r = rig.cmd(op="send", **TEST)
        assert r["err"] == "blocked" and "seen on this bus" in r["detail"]

    def test_bench_override_lifts_the_block_list(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        assert rig.cmd(op="bench", v=1)["bench"] == 1
        assert ("bean_tx_bench", {"v": 1}) in rig.events
        r = rig.cmd(op="send", pri=2, dst="FE", mes="7F", data="001000000000")
        assert r["ok"] == 1 and r["acked"] == 1
        assert rig.cmd(op="bench", v=0)["bench"] == 0
        assert rig.cmd(op="send", pri=2, dst="FE", mes="7F", data="00")["err"] == "blocked"

    def test_crc_stuffing_case_refused(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        data = next("%02X" % v for v in range(256)
                    if W.needs_stuff_after_crc(0, 0xFE, 0x48, [v]))
        n = len(rig.board.gp7_log)
        r = rig.cmd(op="send", pri=0, dst="FE", mes="48", data=data)
        assert r["err"] == "crc_stuff"
        self._untouched(rig, n)
        assert not W.needs_stuff_after_crc(0, 0xFE, 0x48, [0])      # the documented test frame

    def test_rate_limit_5_per_second(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()                                         # the edge pulse counts too
        t0 = rig.board.t
        oks = []
        while rig.board.t - t0 < 900000 and len(oks) < 8:
            oks.append(rig.cmd(op="send", retries=0, **TEST))
        assert [r["ok"] for r in oks[:4]] == [1, 1, 1, 1]
        assert oks[4]["err"] == "rate" and oks[4]["detail"] == "5 per s"
        rig.idle(1000)
        assert rig.cmd(op="send", retries=0, wake=1, **TEST)["ok"] == 1

    def test_retries_stop_when_the_budget_runs_out(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        rig.board.responder = None
        rig.idle(1100)
        r1 = rig.cmd(op="send", wake=1, **TEST)             # 3 attempts
        r2 = rig.cmd(op="send", **TEST)                     # 2 fit, then the 5/s budget is used up
        assert r1["attempts"] == 3 and r2["attempts"] == 2 and r2["err"] == "rate"

    def test_rate_limit_50_per_10_minutes(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        n = 1                                               # the edge pulse
        while n < 50:
            r = rig.cmd(op="send", retries=0, wake=1, **TEST)
            assert r["ok"] == 1, (n, r)
            n += 1
            rig.idle(230)
        assert rig.cmd(op="status")["budget"] == 0
        r = rig.cmd(op="send", retries=0, wake=1, **TEST)
        assert r["err"] == "rate" and "600 s" in r["detail"]
        assert rig.cmd(op="edge", wake=1)["err"] == "rate"
        assert rig.tx.n_frames == 49
        # the window slides: 10 minutes after the first event one more fits
        rig.tx.stamps[0] -= 600000 - 15000
        assert rig.cmd(op="status")["budget"] == 0
        rig.tx.stamps[0] -= 15000
        assert rig.cmd(op="status")["budget"] == 1

    def test_receiver_off_or_failed(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        n = len(rig.board.gp7_log)
        rig.rx.err = "stopped after 10 errors"
        assert rig.cmd(op="send", **TEST)["detail"] == "rx failed"
        rig.rx.err = None
        rig.cap.mode = "poll"
        assert "poll" in rig.cmd(op="send", **TEST)["detail"]
        rig.cap.mode = "dma"
        rig.rx.enable(False)
        r = rig.cmd(op="send", **TEST)
        assert r["err"] == "rx" and r["detail"] == "rx off"
        self._untouched(rig, n)

    @pytest.mark.parametrize("what", ["ovf", "stall", "exc", "crc"])
    def test_receive_errors_block_tx_for_5_s(self, fwm, monkeypatch, what):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        b = rig.board
        if what == "ovf":
            rig.cap.push([0x10005] * (fwm.brx.RING + 5))
            rig.idle(2)
        elif what == "stall":
            rig.cap.stalls += 1
        elif what == "exc":
            monkeypatch.setattr(rig.cap, "produced", lambda: 1 / 0)
            rig.rx.hold = True
            rig.rx.poll()
            rig.rx.hold = False
            monkeypatch.setattr(rig.cap, "produced", lambda: rig.cap.head)
        else:
            b.add_frame(b.t + 500.0, 6, 0xFE, 0x2C, [0x64], crc=0x00)
            rig.idle(30)
        n = len(b.gp7_log)
        r = rig.cmd(op="send", **TEST)
        assert r["err"] == "rx" and r["detail"] == "rx errors", what
        assert rig.cmd(op="edge")["err"] == "rx"
        assert len(b.gp7_log) == n
        rig.idle(4000)
        assert rig.cmd(op="send", wake=1, **TEST)["err"] == "rx"
        rig.idle(1100)
        assert rig.cmd(op="send", wake=1, **TEST)["ok"] == 1

    @pytest.mark.parametrize("tb,ok", [(94.0, False), (96.0, True), (107.0, True), (109.5, False)])
    def test_bit_time_range(self, fwm, monkeypatch, tb, ok):
        rig = Rig(fwm, monkeypatch, traffic=False)
        rig.traffic(n=12, tb=tb)
        assert abs(rig.rx.tb16 / 16 - tb) < 0.7
        r = rig.cmd(op="edge")
        if ok:
            assert r["ok"] == 1 and abs(r["w_us"] - tb) < 0.7
        else:
            assert r["err"] == "rx" and r["detail"].startswith("bit time")
            assert len(rig.board.gp7_log) == 1

    def test_unmeasured_bit_time_refused(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch, traffic=False)
        rig.idle(20)
        r = rig.cmd(op="edge")
        assert r["err"] == "tb" and len(rig.board.gp7_log) == 1

    def test_stuck_dominant_bus_refused(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        b = rig.board
        b.add_dominant(b.t + 100.0, b.t + 200000.0)
        rig.idle(60)
        n = len(b.gp7_log)
        r = rig.cmd(op="send", **TEST)
        assert r["err"] == "rx" and len(b.gp7_log) == n

    @pytest.mark.parametrize("bad", [
        dict(pri=16), dict(pri=-1), dict(pri="1"), dict(pri=True), dict(dst="FEE"), dict(dst=256),
        dict(dst=None), dict(mes="zz"), dict(mes=""), dict(data=""), dict(data="0"),
        dict(data="00" * 12), dict(data="xx"), dict(data=0), dict(retries=4), dict(retries=-1),
        dict(retries=1.5),
    ])
    def test_bad_arguments(self, fwm, monkeypatch, bad):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        n = len(rig.board.gp7_log)
        r = rig.cmd(op="send", **dict(TEST, **bad))
        assert r["ok"] == 0 and r["err"] == "args", (bad, r)
        assert len(rig.board.gp7_log) == n
        assert rig.cmd(op="nope")["err"] == "args"

    def test_integer_ids_accepted(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        r = rig.cmd(op="send", pri=0, dst=0xFE, mes=0x48, data="00")
        assert r["ok"] == 1 and r["dst"] == "FE" and r["mes"] == "48"


class TestFirmwareWatchdogs:
    def test_timer_kills_a_stuck_dominant_state_machine(self, fwm, monkeypatch):
        # SM0 freezes with GP7 high and the watchdog SM is absent: the 1 kHz
        # firmware timer releases the pin
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        b = rig.board
        b.wd_disabled = True
        b.freeze_on_rise = True
        t0 = b.t
        r = rig.cmd(op="send", **TEST)
        assert r["ok"] == 0 and r["err"] == "fault" and r["fault"] == 1
        highs = [d for lv, _, d in b.gp7_runs(t0) if lv]
        assert len(highs) == 1 and 800.0 <= highs[0] <= 1900.0
        assert rig.tx.fault == "gp7 high > 1 ms" and not rig.tx.allow
        assert rig.cmd(op="send", **TEST)["err"] == "fault"

    def test_watchdog_sm_trip_is_reported_as_a_fault(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        b = rig.board
        b.freeze_on_rise = True
        t0 = b.t
        r = rig.cmd(op="send", **TEST)
        assert r["err"] == "fault" and rig.tx.fault in ("watchdog sm", "gp7 high > 1 ms")
        highs = [d for lv, _, d in b.gp7_runs(t0) if lv]
        assert len(highs) == 1 and highs[0] <= 840.0

    def test_timer_tolerates_legal_frames(self, fwm, monkeypatch):
        # longest dominant runs (EOM, 648 us at 108 us bits) on every attempt
        rig = Rig(fwm, monkeypatch, traffic=False)
        rig.traffic(n=12, tb=108.0)
        rig.ready()
        rig.cmd(op="bench", v=1)
        for _ in range(3):
            rig.idle(400)
            r = rig.cmd(op="send", pri=15, dst="FF", mes="FF", data="FF" * 11, retries=0)
            assert r["ok"] == 1 and rig.tx.fault is None, r

    def test_guard_releases_gp7_outside_an_operation(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        b = rig.board
        S.FakePin(7, S.FakePin.OUT, value=1)                # something drove it
        t0 = b.t
        rig.idle(3)
        assert b.pad7() == 0 and rig.tx.fault == "gp7 high outside tx" and not rig.tx.allow
        assert max(d for lv, _, d in b.gp7_runs(t0) if lv) <= 1000.0
        # a bench op (main.py sets busy) is left alone
        rig.tx.fault = None
        rig.tx.busy = True
        S.FakePin(7, S.FakePin.OUT, value=1)
        rig.idle(3)
        assert b.pad7() == 1 and rig.tx.fault is None
        rig.tx.busy = False
        rig.tx.release()
        assert b.pad7() == 0

    def test_exception_inside_an_operation_releases(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        b = rig.board
        calls = []
        real_idle = rig.tx.idle

        def idle(ms):
            real_idle(ms)
            calls.append(1)
            if len(calls) == 4:                             # mid-frame
                assert b.sm[0].on and b.timers
                raise RuntimeError("boom")
        rig.tx.idle = idle
        r = rig.cmd(op="send", **TEST)                      # cmd() asserts the released state
        assert r["ok"] == 0 and r["err"] == "exc" and "boom" in r["detail"]

    def test_start_failure_is_a_refusal(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        monkeypatch.setattr(S.FakeTimer, "fail", True)
        r = rig.cmd(op="send", **TEST)
        assert r["err"] == "exc" and rig.tx.n_frames == 0
        monkeypatch.setattr(S.FakeTimer, "fail", False)
        # PIO0 memory: frame and edge programs are swapped, never both loaded
        assert rig.cmd(op="send", **TEST)["ok"] == 1
        rig.idle(300)
        assert rig.cmd(op="edge")["ok"] == 1
        rig.idle(300)
        assert rig.cmd(op="send", **TEST)["ok"] == 1
        assert sum(len(e[1]) for e in rig.board.loaded) <= 32

    def test_nothing_is_ever_sent_unasked(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.ready()
        n = len(rig.board.gp7_log)
        rig.board.add_frame(rig.board.t + 5000.0, 6, 0xFE, 0x2C, [0x64], rsp=(0, 0))
        rig.idle(3000)                                      # traffic, un-ACKed frames, sleep
        assert len(rig.board.gp7_log) == n                  # no ACK, no frame, no pulse


# ─── receiver left-overs: idle cost ─────────────────────────────────────────

class TestReceiverIdle:
    def test_sleeping_bus_costs_almost_nothing(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.flush()
        full = []
        orig = rig.rx._poll
        monkeypatch.setattr(rig.rx, "_poll", lambda: full.append(1) or orig())
        rig.idle(2000)                                      # falls asleep
        full.clear()
        c0 = rig.cap.calls
        w0 = rig.cap.head
        rig.idle(10000)                                     # 10 s asleep, polled every ms
        assert rig.cap.head - w0 >= 990                     # ~100 timeout words/s arrived
        assert len(full) <= 10000 // fwm.brx.IDLE_SKIP + 2  # one full pass per 40 polls
        assert rig.cap.calls - c0 <= 10000 // fwm.brx.SLEEP_DIV + len(full) + 2
        assert (rig.cap.head - rig.rx.tail) & 0x3FFFFFFF <= 12   # nothing piles up
        st = [m["st"] for m in rig.l3 if "st" in m][-1]
        assert 9000 <= st["idle_ms"] <= 13000 and rig.rx.bus() == 1

    def test_wake_after_a_long_sleep_decodes_the_first_frame(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        n = len(rig.flush())
        rig.idle(30000)
        b = rig.board
        t = b.t + 700.0
        frames = [(8, 0xFE, 0xC2, [0xE9, 0xA1]), (8, 0xFE, 0xC2, [0xC1, 0x80]),
                  (9, 0xFE, 0x3F, [2, 0x87, 0xE9, 0x80])]
        for f in frames:
            t = b.add_frame(t, *f) + 700.0
        got = rig.flush()[n:]
        assert [(g[3], g[4], g[6]) for g in got] == [("C2", "E9A1", 0), ("C2", "C180", 0),
                                                     ("3F", "0287E980", 0)]
        assert rig.rx.bus() == 0 or rig.rx.cur < 1000000
        assert rig.rx.ovf == 0 and rig.rx.exc == 0

    def test_run_counter_saturates_instead_of_growing(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.flush()
        rig.rx.cur = fwm.brx.M30 - 5000
        rig.idle(200)
        assert rig.rx.cur == fwm.brx.M30
        st = rig.rx.stats(0)
        assert st["idle_ms"] == fwm.brx.M30 // 1000

    def test_raw_tap_is_not_sent_to_the_host(self, fwm, monkeypatch):
        rig = Rig(fwm, monkeypatch)
        rig.flush()
        rig.rx.tap(4)
        rig.board.add_frame(rig.board.t + 500.0, 6, 0xFE, 0x2C, [0x64])
        rig.idle(40)
        assert not [m for m in rig.l3 if "raw" in m]
        runs = rig.rx.tap_take()
        assert len(runs) == 4 and runs[0] < 0 and runs[1] == pytest.approx(100, abs=3)
        assert rig.rx.raw is None and rig.rx.tap_take() == []
