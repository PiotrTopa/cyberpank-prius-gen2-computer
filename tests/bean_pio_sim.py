"""Test helpers for the powerbox BEAN transmitter (powerbox/bean_tx.py).

* The firmware's PIO programs are assembled by MicroPython's own assembler
  (tests/fixtures/micropython_rp2_v1_21.py, the unchanged rp2.py of v1.21.0)
  and executed, 16-bit opcode by opcode and cycle by cycle, by :class:`SimSM`
  (the RP2040 PIO instruction set as far as the programs use it: jmp, in,
  out with autopull, push/pull, mov, irq with wait, set, delays, wrap).
* :class:`Board` is the RP2040-Zero plus the transceiver and the bus: PIO0
  with two state machines and 32 instructions of program memory, the GP7 pad
  (SIO or PIO function, pin direction), the single-wire bus (our driver with
  rise / fall delays, other nodes' frames, an ACK responder) and GP6.
* ``machine`` (Pin, mem32, Timer) and ``_rp2`` (PIO, StateMachine) fakes sit
  on the board, so the production ``bean_tx.Hw`` runs unchanged.
"""

import bisect
import importlib.util
import os
import sys
import types

import bean_wire as W

HERE = os.path.dirname(__file__)
POWERBOX_DIR = os.path.join(os.path.dirname(HERE), "powerbox")
M32 = 0xFFFFFFFF

PIO0_BASE = 0x50200000
REG_IRQ = PIO0_BASE + 0x030
REG_SM0_SHIFTCTRL = PIO0_BASE + 0x0D0
REG_SM0_INSTR = PIO0_BASE + 0x0D8

BOARD = None                      # the board the fakes act on


# ─── PIO state machine ──────────────────────────────────────────────────────

class SimSM:
    def __init__(self, board, sm_id):
        self.board = board
        self.id = sm_id
        self.on = False
        self.x = 0xDEADBEEF        # X/Y survive a restart on the chip
        self.y = 0xFEEDF00D
        self.code = []
        self.period = 1.0
        self.next_t = 0.0
        self.txf = []
        self.rxf = []
        self.shiftctrl = 0
        self.steps = 0
        self.hist = []             # optional pc trace

    def init(self, prog, freq, jmp_pin, in_pin):
        self.code = list(prog[0])
        execctrl = prog[3]
        self.wrap_target = (execctrl >> 7) & 0x1F
        self.wrap = (execctrl >> 12) & 0x1F
        self.shiftctrl = prog[4]
        self.jmp_pin = jmp_pin
        self.in_pin = in_pin
        self.period = 1e6 / freq
        self.pc = 0
        self.osr = 0
        self.osr_cnt = 32
        self.isr = 0
        self.isr_cnt = 0
        self.delay = 0
        self.irq_wait = False
        self.txf = []
        self.rxf = []
        self.on = False
        self.snap = {}

    def sample(self):
        """Latch the input pins: an instruction sees the level its cycle
        started with (= at the end of the previous cycle)."""
        b = self.board
        self.snap = {n: b.pin_level(n) for n in (self.jmp_pin, self.in_pin) if n is not None}

    # shiftctrl fields
    @property
    def autopull(self):
        return bool(self.shiftctrl & (1 << 17))

    @property
    def pull_thresh(self):
        return ((self.shiftctrl >> 25) & 0x1F) or 32

    @property
    def out_right(self):
        return bool(self.shiftctrl & (1 << 19))

    @property
    def in_right(self):
        return bool(self.shiftctrl & (1 << 18))

    @property
    def tx_cap(self):
        return 8 if self.shiftctrl & (1 << 30) else (0 if self.shiftctrl & (1 << 31) else 4)

    @property
    def rx_cap(self):
        return 8 if self.shiftctrl & (1 << 31) else (0 if self.shiftctrl & (1 << 30) else 4)

    def put(self, v):
        assert len(self.txf) < self.tx_cap, "sm.put() on a full FIFO would block"
        self.txf.append(v & M32)

    def step(self):
        self.steps += 1
        try:
            if self.delay:
                self.delay -= 1
                return
            assert 0 <= self.pc < len(self.code), "pc %d ran off the program" % self.pc
            ins = self.code[self.pc]
            pc = self.pc
            stalled, jumped = self.execute(ins)
            if stalled:
                return
            if not jumped:
                self.pc = self.wrap_target if pc == self.wrap else pc + 1
            self.delay = (ins >> 8) & 0x1F
        finally:
            self.sample()

    def _pin(self, n):
        return self.snap[n]

    def execute(self, ins):
        """One instruction; returns (stalled, jumped)."""
        op = ins >> 13
        b = self.board
        if op == 0:                                   # JMP
            cond = (ins >> 5) & 7
            if cond == 0:
                take = True
            elif cond == 1:
                take = self.x == 0
            elif cond == 2:
                take = self.x != 0
                self.x = (self.x - 1) & M32
            elif cond == 3:
                take = self.y == 0
            elif cond == 4:
                take = self.y != 0
                self.y = (self.y - 1) & M32
            elif cond == 5:
                take = self.x != self.y
            elif cond == 6:
                take = self._pin(self.jmp_pin) == 1
            else:
                take = self.osr_cnt < self.pull_thresh
            if take:
                self.pc = ins & 0x1F
            return False, take
        if op == 2:                                   # IN
            src = (ins >> 5) & 7
            n = (ins & 0x1F) or 32
            val = {0: self._pin(self.in_pin) if src == 0 else 0, 1: self.x, 2: self.y,
                   3: 0, 6: self.isr, 7: self.osr}[src] & ((1 << n) - 1)
            if self.in_right:
                self.isr = ((self.isr >> n) | (val << (32 - n))) & M32
            else:
                self.isr = ((self.isr << n) | val) & M32
            self.isr_cnt = min(32, self.isr_cnt + n)
            assert not self.shiftctrl & (1 << 16), "autopush not simulated"
            return False, False
        if op == 3:                                   # OUT
            dst = (ins >> 5) & 7
            n = (ins & 0x1F) or 32
            assert self.autopull, "out without autopull not used"
            if self.osr_cnt >= self.pull_thresh:      # empty: pull, lose a cycle
                if self.txf:
                    self.osr = self.txf.pop(0)
                    self.osr_cnt = 0
                return True, False
            if self.out_right:
                val = self.osr & ((1 << n) - 1)
                self.osr >>= n
            else:
                val = (self.osr >> (32 - n)) & ((1 << n) - 1)
                self.osr = (self.osr << n) & M32
            self.osr_cnt += n
            if self.osr_cnt >= self.pull_thresh and self.txf:
                self.osr = self.txf.pop(0)            # refill, no stall
                self.osr_cnt = 0
            if dst == 1:
                self.x = val
            elif dst == 2:
                self.y = val
            elif dst == 3:
                pass
            else:
                raise AssertionError("out dst %d" % dst)
            return False, False
        if op == 4:                                   # PUSH / PULL
            if ins & 0x80:
                raise AssertionError("pull not used")
            if len(self.rxf) < self.rx_cap:
                self.rxf.append(self.isr)
            elif ins & 0x20:
                return True, False
            self.isr = 0
            self.isr_cnt = 0
            return False, False
        if op == 5:                                   # MOV
            dst = (ins >> 5) & 7
            src = ins & 7
            assert not (ins >> 3) & 3
            val = {1: self.x, 2: self.y, 3: 0, 6: self.isr, 7: self.osr}[src]
            if dst == 1:
                self.x = val
            elif dst == 2:
                self.y = val
            elif dst == 6:
                self.isr = val
                self.isr_cnt = 0
            else:
                raise AssertionError("mov dst %d" % dst)
            return False, False
        if op == 6:                                   # IRQ
            bit = 1 << (ins & 7)
            assert not ins & 0x10, "irq rel not simulated"
            if ins & 0x40:
                b.flags &= ~bit
                return False, False
            if not self.irq_wait:
                b.flags |= bit
                if not ins & 0x20:
                    return False, False
                self.irq_wait = True
                return True, False
            if b.flags & bit:
                return True, False
            self.irq_wait = False
            return False, False
        if op == 7:                                   # SET
            dst = (ins >> 5) & 7
            data = ins & 0x1F
            if dst == 0:
                b.pio_set_pin(data & 1)
            elif dst == 1:
                self.x = data
            elif dst == 2:
                self.y = data
            elif dst == 4:
                b.pio_set_dir(data & 1)
            else:
                raise AssertionError("set dst %d" % dst)
            return False, False
        raise AssertionError("opcode %04X not simulated" % ins)


# ─── board, transceiver, bus ────────────────────────────────────────────────

class Board:
    """Time in us. ``rise`` / ``fall``: delay from a GP7 edge to the bus
    level change as GP6 sees it."""

    def __init__(self, rise=1.0, fall=5.0):
        self.t = 0.0
        self.rise = rise
        self.fall = fall
        self.sm = {0: SimSM(self, 0), 1: SimSM(self, 1)}
        self.loaded = []                  # (program, instructions as loaded) in PIO0
        self.flags = 0
        self.func7 = "sio"
        self.sio7 = 0
        self.latch7 = 0
        self.dir7 = 0
        self.gp7_log = [(0.0, 0)]
        self.ours = 0
        self.our_events = []
        self.driver_ok = True             # False: GP7 does not move the bus
        self.wd_disabled = False          # True: SM1 never runs (tests)
        self.f_on = []                    # other drivers: sorted interval starts
        self.f_off = []
        self.bus = 0
        self.bus_log = [(0.0, 0)]
        self.dom_since = None
        self.responder = None             # None | "ack" | "nak"
        self.resp_tb = 100.0
        self.resp_skip = 0                # frames the responder ignores first
        self.on_rise = None               # called once at the next GP7 rise
        self.freeze_on_rise = False       # stop SM0 at the next GP7 rise
        self.timers = []
        self.pins_built = []

    # pins
    def pad7(self):
        if self.func7 == "sio":
            return self.sio7
        return self.latch7 if self.dir7 else 0

    def pin_level(self, n):
        if n == 7:
            return self.pad7()
        assert n == 6
        return 0 if self.bus else 1

    def gp7_changed(self):
        lv = self.pad7()
        if lv == self.gp7_log[-1][1]:
            return
        self.gp7_log.append((self.t, lv))
        te = self.t + (self.rise if lv else self.fall)
        self.our_events = [e for e in self.our_events if e[0] < te] + [(te, lv)]
        if lv:
            hook, self.on_rise = self.on_rise, None
            if hook is not None:
                hook(self.t)
            if self.freeze_on_rise:
                self.freeze_on_rise = False
                self.sm[0].on = False

    def pio_set_pin(self, v):
        self.latch7 = v
        self.gp7_changed()

    def pio_set_dir(self, v):
        self.dir7 = v
        self.gp7_changed()

    # other nodes
    def add_dominant(self, t_on, t_off):
        i = bisect.bisect_left(self.f_on, t_on)
        self.f_on.insert(i, t_on)
        self.f_off.insert(i, t_off)

    def add_bits(self, t0, bits, tb=100.0):
        t = t0
        for v, d in W.bits_to_runs(bits, tb):
            if v:
                self.add_dominant(t, t + d)
            t += d
        return t

    def add_frame(self, t0, pri, dst, mes, data, tb=100.0, **kw):
        return self.add_bits(t0, W.frame_bits(pri, dst, mes, data, **kw), tb)

    def _foreign(self, t):
        i = bisect.bisect_right(self.f_on, t) - 1
        # intervals may overlap (contender + responder): look a few back
        while i >= 0 and i >= bisect.bisect_right(self.f_on, t) - 4:
            if self.f_off[i] > t:
                return 1
            i -= 1
        return 0

    def _next_foreign(self, t):
        best = None
        i = bisect.bisect_right(self.f_on, t)
        if i < len(self.f_on):
            best = self.f_on[i]
        j = i - 1
        while j >= 0 and j >= i - 4:
            if self.f_off[j] > t and (best is None or self.f_off[j] < best):
                best = self.f_off[j]
            j -= 1
        return best

    def _bus_update(self):
        while self.our_events and self.our_events[0][0] <= self.t:
            self.ours = self.our_events.pop(0)[1]
        lv = 1 if ((self.ours and self.driver_ok) or self._foreign(self.t)) else 0
        if lv != self.bus:
            self.bus = lv
            self.bus_log.append((self.t, lv))
            if lv:
                self.dom_since = self.t
            elif self.dom_since is not None:
                if self.t - self.dom_since >= 5.5 * self.resp_tb and self.responder:
                    if self.resp_skip:
                        self.resp_skip -= 1
                    else:
                        k = 2 if self.responder == "ack" else 1
                        self.add_dominant(self.t + k * self.resp_tb, self.t + (k + 1) * self.resp_tb)

    def advance(self, t_end):
        while True:
            tn = t_end + 1
            for s in self.sm.values():
                if s.on and s.next_t < tn:
                    tn = s.next_t
            if self.our_events and self.our_events[0][0] < tn:
                tn = self.our_events[0][0]
            nf = self._next_foreign(self.t)
            if nf is not None and nf < tn:
                tn = nf
            if tn > t_end:
                break
            if tn > self.t:
                self.t = tn
            self._bus_update()
            for s in (self.sm[1], self.sm[0]):
                if s.on and s.next_t <= self.t:
                    s.next_t += s.period
                    s.step()
                    self._bus_update()
        self.t = t_end
        self._bus_update()

    # analysis
    def gp7_runs(self, since=0.0):
        """[(level, start, duration)] of the GP7 pad from ``since`` on."""
        out = []
        log = self.gp7_log + [(self.t, None)]
        for (t0, lv), (t1, _) in zip(log, log[1:]):
            if t1 > since:
                out.append((lv, max(t0, since), t1 - max(t0, since)))
        return out

    def bus_runs(self, since=0.0):
        out = []
        log = self.bus_log + [(self.t, None)]
        for (t0, lv), (t1, _) in zip(log, log[1:]):
            if t1 > since:
                out.append((lv, max(t0, since), t1 - max(t0, since)))
        return out


def runs_to_bits(runs, tb):
    """Bits of [(level, start, dur)] runs, each rounded to whole bit times."""
    bits = []
    for lv, _, d in runs:
        bits += [lv] * int(round(d / tb))
    return bits


# ─── fakes: machine, _rp2 ───────────────────────────────────────────────────

class FakePin:
    IN, OUT = 0, 1

    def __init__(self, n, mode=None, pull=None, value=None):
        self.pin_no = n
        b = BOARD
        b.pins_built.append((n, mode, value))
        if n == 7 and mode == FakePin.OUT:
            b.func7 = "sio"
            if value is not None:
                b.sio7 = value
            b.gp7_changed()

    def value(self, v=None):
        b = BOARD
        if v is None:
            return b.pin_level(self.pin_no)
        assert self.pin_no == 7
        b.sio7 = v
        b.gp7_changed()


class FakeMem:
    def __getitem__(self, addr):
        b = BOARD
        if addr == REG_IRQ:
            return b.flags
        if addr == REG_SM0_SHIFTCTRL:
            return b.sm[0].shiftctrl
        raise AssertionError("mem32 read %08X" % addr)

    def __setitem__(self, addr, v):
        b = BOARD
        if addr == REG_IRQ:
            b.flags &= ~v
        elif addr == REG_SM0_SHIFTCTRL:
            s = b.sm[0]
            if (s.shiftctrl ^ v) >> 30:
                s.txf = []
                s.rxf = []
            s.shiftctrl = v
        elif addr == REG_SM0_INSTR:
            b.sm[0].execute(v)
        else:
            raise AssertionError("mem32 write %08X" % addr)


class FakeTimer:
    PERIODIC = 1
    fail = False

    def __init__(self, period=None, mode=None, callback=None):
        if FakeTimer.fail:
            raise OSError("no timer")
        assert period == 1 and mode == FakeTimer.PERIODIC
        self.cb = callback
        BOARD.timers.append(self)

    def deinit(self):
        if self in BOARD.timers:
            BOARD.timers.remove(self)


class FakePIO:
    IN_LOW, IN_HIGH, OUT_LOW, OUT_HIGH = 0, 1, 2, 3
    SHIFT_LEFT, SHIFT_RIGHT = 0, 1
    JOIN_NONE, JOIN_TX, JOIN_RX = 0, 1, 2

    def __init__(self, n):
        assert n == 0

    def remove_program(self, prog):
        b = BOARD
        b.loaded = [e for e in b.loaded if e[0] is not prog]


class FakeStateMachine:
    def __init__(self, sm_id, prog=None, freq=None, set_base=None, in_base=None,
                 jmp_pin=None, **kw):
        assert sm_id in (0, 1) and not kw, (sm_id, kw)
        b = BOARD
        entry = [e for e in b.loaded if e[0] is prog]
        if not entry:
            if sum(len(e[1]) for e in b.loaded) + len(prog[0]) > 32:
                raise OSError(12, "ENOMEM: PIO0 program memory full")
            # the instructions are copied into the PIO when the program is
            # added: later changes to prog[0] need remove_program first
            entry = [(prog, list(prog[0]))]
            b.loaded.append(entry[0])
        s = b.sm[sm_id]
        s.init(prog, freq, jmp_pin.pin_no, None if in_base is None else in_base.pin_no)
        s.code = list(entry[0][1])
        if prog[6] is not None and set_base is not None:      # set_init
            assert set_base.pin_no == 7 and prog[6] == FakePIO.OUT_LOW
            b.func7 = "pio"
            b.latch7 = 0
            b.dir7 = 1
            b.gp7_changed()
        self.s = s

    def put(self, v):
        self.s.put(v)

    def get(self):
        assert self.s.rxf, "sm.get() on an empty FIFO would block"
        return self.s.rxf.pop(0)

    def tx_fifo(self):
        return len(self.s.txf)

    def rx_fifo(self):
        return len(self.s.rxf)

    def active(self, v=None):
        b = BOARD
        if v and self.s.id == 1 and b.wd_disabled:
            return
        if v and not self.s.on:
            self.s.next_t = b.t + self.s.period
            self.s.sample()
        self.s.on = bool(v)


def install_time_shims():
    import time
    if not hasattr(time, "ticks_ms"):
        time.ticks_ms = lambda: int(time.monotonic() * 1000)
        time.ticks_diff = lambda a, b: a - b
        time.ticks_add = lambda a, b: a + b
        time.sleep_ms = lambda ms: None
    if not hasattr(time, "ticks_us"):
        time.ticks_us = lambda: int(time.perf_counter() * 1000000)
    if not hasattr(time, "sleep_us"):               # same shim as the other firmware tests
        def _sleep_us(us):
            end = time.perf_counter() + us / 1e6
            while time.perf_counter() < end:
                pass
        time.sleep_us = _sleep_us


def fake_machine():
    m = types.ModuleType("machine")
    m.Pin = FakePin
    m.mem32 = FakeMem()
    m.Timer = FakeTimer
    return m


def load_firmware():
    """(rp2 assembler module, bean_rx, bean_tx) with the real MicroPython
    PIO assembler and the fakes above behind it."""
    install_time_shims()
    saved = {k: sys.modules.get(k) for k in ("_rp2", "micropython", "rp2")}
    lo = types.ModuleType("_rp2")
    lo.PIO = FakePIO
    lo.StateMachine = FakeStateMachine
    lo.__all__ = ["PIO", "StateMachine"]
    mp = types.ModuleType("micropython")
    mp.const = lambda v: v
    mp.native = lambda f: f
    sys.modules["_rp2"] = lo
    sys.modules["micropython"] = mp
    try:
        def load(name, path):
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        rp2 = load("rp2", os.path.join(HERE, "fixtures", "micropython_rp2_v1_21.py"))
        sys.modules["rp2"] = rp2
        brx = load("bean_rx_real_asm", os.path.join(POWERBOX_DIR, "bean_rx.py"))
        btx = load("bean_tx_real_asm", os.path.join(POWERBOX_DIR, "bean_tx.py"))
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return rp2, brx, btx


# ─── receiver feed ──────────────────────────────────────────────────────────

class FakeCapture:
    """Stands for bean_rx.Capture in DMA mode (the newest word is consumed
    one word late, as on the board)."""

    def __init__(self, ring):
        self.mode = "dma"
        self.ring = ring
        self.buf = [0] * ring
        self.base = 0
        self.head = 0
        self.stalls = 0
        self.calls = 0

    def start(self, want="auto"):
        self.head = 0
        return self.mode

    def stop(self):
        pass

    def produced(self):
        self.calls += 1
        return self.head

    def push(self, words):
        for w in words:
            self.buf[self.head % self.ring] = w
            self.head = (self.head + 1) & 0x3FFFFFFF


class RxFeeder:
    """Bus level changes -> the receiver's PIO words, in (simulated) real
    time, with the word format of tests/bean_wire.runs_to_words."""
    T, OFF, TO = 10000, 3, 10004

    def __init__(self, cap):
        self.cap = cap
        self.start = 0.0
        self.level = 0
        self.k = 0
        self.i = 1

    def _timeouts(self, d):
        lvl = (0 if self.level else 1) << 16
        while True:
            rem = d - self.OFF - self.k * self.TO
            if rem <= self.T or rem - self.TO < 0:
                return rem
            self.cap.push([lvl | 0xFFFF])
            self.k += 1

    def feed(self, board):
        log = board.bus_log
        while self.i < len(log):
            t, lv = log[self.i]
            rem = self._timeouts(int(round(t - self.start)))
            x = self.T - rem if rem >= 0 else self.T
            self.cap.push([((0 if self.level else 1) << 16) | max(0, x)])
            self.start, self.level, self.k = t, lv, 0
            self.i += 1
        self._timeouts(int(board.t - self.start))
