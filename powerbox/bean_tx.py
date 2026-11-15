"""
BEAN (Toyota MPX body bus) transmitter for the powerbox, fw 2.4.0.

GP7 = TX (HIGH drives the bus to +B = dominant = logic 1; LOW = released),
GP6 = RX, inverted (bus dominant -> GP6 LOW). Protocol and the assumptions
made here: docs/BEAN_PROTOCOL.md (section 7.2); design, timing diagram and
the first-transmission procedure: powerbox/README.md "BEAN transmitter".

Hardware side (PIO0; the receiver owns PIO1 and its 27 instructions):

  SM0  frame program (25 instructions) or edge-test program (25), one loaded
       at a time. The whole frame sits in the joined 8-word TX FIFO before
       the state machine starts, so it cannot stall half way. Per bit it
       drives GP7, waits to the sample point and reads GP6 back:
         sent recessive, read dominant  -> stop (arbitration lost / bit error)
         sent dominant, read recessive  -> release, stop (line fault)
       It starts only after IFS_BITS bit times of continuously recessive bus
       (sampled every 2 PIO cycles), cannot drive more than 6 consecutive
       dominant bits whatever the data says (X counts the run; the 7th is
       forced recessive), and samples the two RSP bits after the EOM.
  SM1  watchdog (6 instructions): GP7 continuously high for ~0.8 ms turns
       the pin into an input (released) until the next start, independent
       of SM0 and of the firmware.

Firmware side: a 1 kHz machine.Timer during every operation releases GP7 and
stops both state machines when GP7 stays high for more than WD_HOLD_US;
guard() (every wdt_sleep_ms slice) does the same outside operations; every
operation ends in release() (GP7 back to a plain output, LOW).

Nothing here transmits on its own. Commands ({"a":"bean_tx","op":..}):
    status                       guards, last edge test, budget
    edge   [wake]                one dominant pulse of one bit time; rise and
                                 fall as seen on GP6 (no frame)
    allow  v                     per-boot "frames allowed" flag
    bench  v                     per-boot bench override (block list, CRC
                                 stuffing case, unmeasured bit time)
    sp     pct                   readback sample point, 25..75 % of the bit
    send   pri dst mes data [retries] [wake]
Every reply is {"ack":"bean_tx","op":..,"ok":0|1,...}; a refusal carries
"err" (short code) and "detail".
"""

import time
from binascii import hexlify, unhexlify

ticks_ms = time.ticks_ms
ticks_us = time.ticks_us
ticks_add = time.ticks_add
ticks_diff = time.ticks_diff

# ─── pins, PIO0 registers ───────────────────────────────────────────────────
PIN_TX = 7
PIN_RX = 6
SM_TX = 0                    # PIO0 SM0
SM_WD = 1                    # PIO0 SM1
PIO0_BASE = 0x50200000
PIO_IRQ = PIO0_BASE + 0x030
SM0_SHIFTCTRL = PIO0_BASE + 0x0D0
SM0_INSTR = PIO0_BASE + 0x0D8
OP_PUSH = 0x8000             # push noblock
OP_MOV_ISR_Y = 0xA0C2        # mov isr, y
# PIO0 IRQ flags 4..7 only: they cannot raise a CPU interrupt, so nothing in
# MicroPython ever clears them behind our back.
F_LOST = 0x10                # frame program: recessive sent, dominant read
F_FAULT = 0x20               # frame program: dominant sent, recessive read
F_WDOG = 0x40                # watchdog state machine tripped
F_STOP = 0x80
FIFO_WORDS = 8               # joined TX FIFO, always filled completely

# ─── timing ─────────────────────────────────────────────────────────────────
NCYC = 32                    # PIO cycles per bit (frame program)
EDGE_CYC = 206               # PIO cycles of the edge-test pulse
EDGE_W = 100                 # edge program: width count (25 << 2)
EDGE_T = 800                 # edge program: idle / fall timeout count (100 << 3)
IFS_BITS = 11                # recessive bit times required before SOF
TB16_NOM = 1600              # bit time x16 us when nothing was measured
SP_DEF = 50                  # readback sample point, % of the bit
SP_MIN = 25
SP_MAX = 75
EDGE_MARGIN_US = 10          # rise/fall must end this long before the sample
# Rise and fall limit, whatever the sample point: a node that resyncs on an
# edge and samples half a bit later still has to see a single bit that our
# edge delay shortened to (bit time - delay), so delay < bit/2 - margin.
EDGE_OK_US = 40
WD_FREQ = 80000              # watchdog SM clock: 64 cycles = 0.8 ms
WD_HOLD_US = 800             # firmware timer: GP7 high this long after a tick
ECHO_MS = 40                 # wait for the receiver to report our frame
EDGE_MAX_MS = 150            # edge test: keep trying this long on a busy bus
BACKOFF_MS = 10              # extra idle per failed attempt before a retry
OP_MAX_MS = 400              # one send, everything included
OWN_MS = 500                 # receiver flags our frame this long

# ─── limits ─────────────────────────────────────────────────────────────────
RETRIES_DEF = 2
RETRIES_MAX = 3
RATE_PER_S = 5               # wire events (frame attempts, edge pulses) per second
RATE_WIN_N = 50              # ... and per RATE_WIN_MS
RATE_WIN_MS = 600000

# ─── block / allow lists ────────────────────────────────────────────────────
# Allowed without the bench override: only the test frame, broadcast FE with
# a MES-ID seen neither on this car, nor in the published NHW20 captures, nor
# in the other-model tables (docs/BEAN_PROTOCOL.md 7.2).
TEST_DST = 0xFE
TEST_MES = 0x48
ALLOW = ((TEST_DST, TEST_MES),)
# Destinations of real nodes: NHW20 node addresses (FE AB / FE C2 first byte
# and DST of the captures), other-model destinations, SAE broadcast FF.
DENY_DST = b"\x19\x40\x52\x62\x98\xB5\xC1\xE9\x45\x46\xD1\xD2\xE0\xFF"
# Broadcast (FE) message IDs of real traffic: NHW20 captures and this car
# (periodic status, wake/alive announcements, meter lamps, odometer, speed,
# shift, READY/HV status, power-source and key related), plus IDs with a
# known meaning on other Toyota/Lexus models.
DENY_FE_MES = (b"\x24\x26\x29\x2C\x3F\x40\x50\x53\x54\x62\x7F\x83\x91\xAB\xB6"
               b"\xC2\xCD\xD4\xD8\xDB\xE1\xFC"
               b"\x15\x1A\x1B\x20\x25\x59\x6A\x6D\x6E\x76\x90\x96\xA3\xA4\xD0"
               b"\xD2\xD7\xD9\xDA\xDC\xDD\xDE\xDF\xE0\xE4\xE6\xFA\xFD")


def crc8(buf):
    """BEAN CRC-8 (poly 0x13, init 0)."""
    c = 0
    for b in buf:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x13) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def encode(pri, dst, mes, data):
    """Wire bits of one frame, SOF..EOM (stuffed SOF..CRC, EOM 0x7E raw).

    Returns (bits, nbits, hdr, body, amb): ``bits`` an int with the first
    bit in the MSB of ``nbits``; ``hdr`` the number of wire bits up to the
    end of MES (arbitration field); ``body`` the bytes PRI/ML..DATA; ``amb``
    True when the CRC ends in a run of 5 equal bits (whether a stuff bit
    belongs there is unresolved; none is sent, as specadmin and BeanMPX)."""
    body = bytearray(3 + len(data))
    body[0] = ((pri & 0x0F) << 4) | (2 + len(data))
    body[1] = dst
    body[2] = mes
    for i in range(len(data)):
        body[3 + i] = data[i]
    acc = 1
    n = 1
    last = 1
    run = 1
    hdr = 0
    nbody = len(body)
    for k in range(nbody + 1):
        byte = body[k] if k < nbody else crc8(body)
        for i in range(7, -1, -1):
            if run == 5:                     # stuff bit before the next bit
                last ^= 1
                acc = (acc << 1) | last
                n += 1
                run = 1
            bit = (byte >> i) & 1
            acc = (acc << 1) | bit
            n += 1
            if bit == last:
                run += 1
            else:
                last = bit
                run = 1
        if k == 2:
            hdr = n + (1 if run == 5 else 0)
    acc = (acc << 8) | 0x7E
    n += 8
    return acc, n, hdr, body, run == 5


def to_words(bits, nbits):
    """32-bit words, first wire bit in the MSB of word 0, zero (recessive)
    padded."""
    nw = (nbits + 31) >> 5
    bits <<= (nw << 5) - nbits
    return [(bits >> ((nw - 1 - i) << 5)) & 0xFFFFFFFF for i in range(nw)]


def max_dominant_run(bits, nbits):
    best = 0
    run = 0
    for i in range(nbits - 1, -1, -1):
        if (bits >> i) & 1:
            run += 1
            if run > best:
                best = run
        else:
            run = 0
    return best


def frame_delays(sp, fall_us=0, tb16=TB16_NOM):
    """(d1, d2, d3): the delay fields of the frame program.

    d1: readback sample point, ``sp`` % of the bit. GP7 changes at the end
    of the ``set`` cycle; the ``jmp pin`` after its d1 delay cycles samples
    GP6 at the start of its own cycle, i.e. d1 cycles after the edge.
    d2: rest of the bit (8 + d1 + d2 = NCYC cycles).
    d3: RSP sample point. The answering node times its bits from the bus
    edge of the EOM, which follows our release by the fall time. RSP1 is
    sampled 1.5 bit times after that edge (one bit + d3 - 5 cycles after
    our release, at most one bit + 26 cycles), RSP2 one bit later."""
    d1 = (sp * NCYC + 50) // 100
    d3 = 5 + NCYC // 2 + (fall_us * 16 * NCYC + tb16 // 2) // tb16
    return d1, NCYC - 8 - d1, d3 if d3 < 31 else 31


# ─── PIO programs ───────────────────────────────────────────────────────────
try:
    import rp2

    # Delays as assembled: sample point 50 % (d1 = 16, d2 = 8), RSP half a
    # bit after our edge (d3 = 21). patch_frame() rewrites the four fields.
    @rp2.asm_pio(set_init=rp2.PIO.OUT_LOW, autopull=True, pull_thresh=32,
                 out_shiftdir=rp2.PIO.SHIFT_LEFT,
                 in_shiftdir=rp2.PIO.SHIFT_LEFT, fifo_join=rp2.PIO.JOIN_TX)
    def _frame():
        out(x, 32)                       # 0  idle check count
        label("idle")
        jmp(pin, "ic")                   # 1  GP6 high = bus recessive
        jmp("stop")                      # 2  bus busy: nothing was driven
        label("ic")
        jmp(x_dec, "idle")               # 3
        out(y, 32)                       # 4  wire bits - 1
        set(x, 6)                        # 5  dominant bits allowed in a row
        label("bit")
        mov(isr, x)                      # 6
        out(x, 1)                        # 7
        jmp(not_x, "rec")                # 8
        mov(x, isr)                      # 9
        jmp(x_dec, "dom")                # 10 allowance used up: forced recessive
        label("rec")
        set(x, 6)[1]                     # 11
        set(pins, 0)[16]                 # 12 [d1]
        jmp(pin, "next")                 # 13 reads recessive: fine
        irq(block, 4)                    # 14 reads dominant: lost / bit error
        wrap_target()
        label("stop")
        set(pins, 0)                     # 15
        irq(block, 7)                    # 16
        label("fault")
        irq(5)                           # 17
        jmp("stop")                      # 18
        label("dom")
        set(pins, 1)[16]                 # 19 [d1]
        jmp(pin, "fault")                # 20 reads recessive while driving
        label("next")
        jmp(y_dec, "bit")[8]             # 21 [d2]
        nop()[21]                        # 22 [d3] EOM done: RSP1 sample point
        in_(pins, 1)[31]                 # 23 one bit later
        in_(pins, 1)                     # 24 RSP2
        wrap()

    @rp2.asm_pio(set_init=rp2.PIO.OUT_LOW, in_shiftdir=rp2.PIO.SHIFT_LEFT)
    def _edge():
        set(x, 25)
        mov(isr, x)
        in_(null, 2)                     # 100
        mov(x, isr)                      # pulse width count
        in_(null, 3)                     # 800
        mov(y, isr)                      # idle check: 7.8 bit times
        label("idle")
        jmp(pin, "ic")
        jmp("stop")                      # busy: no pulse, nothing pushed
        label("ic")
        jmp(y_dec, "idle")
        mov(y, isr)                      # fall timeout count
        set(pins, 1)
        label("r")
        jmp(pin, "rc")                   # GP6 still high: not dominant yet
        jmp("seen")
        label("rc")
        jmp(x_dec, "r")
        jmp("rel")                       # never seen: release on time anyway
        label("seen")
        mov(isr, x)                      # count left when GP6 went low
        label("h")
        jmp(x_dec, "h")[1]
        label("rel")
        set(pins, 0)
        push(noblock)                    # rise word (800 = never seen)
        label("f")
        jmp(pin, "fd")                   # GP6 high again: bus released
        jmp(y_dec, "f")
        label("fd")
        mov(isr, y)
        push(noblock)                    # fall word (0xFFFFFFFF = timeout)
        wrap_target()
        label("stop")
        set(pins, 0)
        irq(block, 7)
        wrap()

    @rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
    def _wdog():
        wrap_target()
        label("top")
        set(x, 31)
        label("loop")
        jmp(pin, "hi")                   # jmp pin = GP7 itself
        jmp("top")
        label("hi")
        jmp(x_dec, "loop")
        set(pindirs, 0)                  # 64 cycles high: GP7 -> input
        irq(block, 6)
        wrap()
except ImportError:
    rp2 = None
    _frame = None
    _edge = None
    _wdog = None


def patch_frame(delays):
    """Write (d1, d2, d3) into the delay fields (bits 12..8) of the four
    frame-program instructions that carry them. True when something
    changed: the program must then be loaded into the PIO again."""
    d1, d2, d3 = delays
    code = _frame[0]
    changed = False
    for i, d in ((12, d1), (19, d1), (21, d2), (22, d3)):
        w = (code[i] & 0xE0FF) | (d << 8)
        if w != code[i]:
            code[i] = w
            changed = True
    return changed


class Hw:
    """The RP2040 side: pins, the two PIO0 state machines, the timer."""

    def __init__(self):
        import machine
        self.machine = machine
        self.mem = machine.mem32
        self.ptx = machine.Pin(PIN_TX, machine.Pin.OUT, value=0)
        self.prx = machine.Pin(PIN_RX)
        self.sm = None
        self.wd = None
        self.prog = None
        self.timer = None

    def rx(self):
        return self.prx.value()

    def tx_level(self):
        return self.ptx.value()

    def release(self):
        """GP7 back to a plain output, LOW (takes the pin away from PIO),
        then both state machines off. Never raises."""
        m = self.machine
        try:
            self.ptx = m.Pin(PIN_TX, m.Pin.OUT, value=0)
        except Exception:
            pass
        for s in (self.sm, self.wd):
            if s is not None:
                try:
                    s.active(0)
                except Exception:
                    pass

    def start(self, prog, freq, words, reload=False):
        """Load ``prog`` on SM0 (PIO0 holds one TX program at a time;
        ``reload``: its instructions changed since it was loaded), fill the
        FIFO, start the watchdog SM and then SM0."""
        m = self.machine
        if rp2 is None:
            raise RuntimeError("no rp2 module")
        if prog is not self.prog or reload:
            if self.prog is not None:
                try:
                    rp2.PIO(0).remove_program(self.prog)
                except Exception:
                    pass
            self.prog = prog
        self.mem[PIO_IRQ] = 0xFF
        ptx = m.Pin(PIN_TX)
        prx = m.Pin(PIN_RX)
        self.wd = rp2.StateMachine(SM_WD, _wdog, freq=WD_FREQ, set_base=ptx,
                                   jmp_pin=ptx)
        sm = rp2.StateMachine(SM_TX, prog, freq=freq, set_base=ptx,
                              in_base=prx, jmp_pin=prx)
        self.sm = sm
        k = 0
        for w in words:
            if sm.tx_fifo() != k:        # a put on a full FIFO would block
                raise RuntimeError("fifo %d/%d" % (sm.tx_fifo(), k))
            sm.put(w)
            k += 1
        self.wd.active(1)
        sm.active(1)

    def flags(self):
        return self.mem[PIO_IRQ] & 0xFF

    def tx_fifo(self):
        return self.sm.tx_fifo()

    def readout(self):
        """(ISR, Y) of the stopped frame program, or None: un-join the
        FIFOs, execute ``push`` / ``mov isr, y`` / ``push`` and read both
        words."""
        try:
            mem = self.mem
            mem[SM0_SHIFTCTRL] = mem[SM0_SHIFTCTRL] & 0x3FFFFFFF
            mem[SM0_INSTR] = OP_PUSH
            mem[SM0_INSTR] = OP_MOV_ISR_Y
            mem[SM0_INSTR] = OP_PUSH
            return self.rx_words(2)
        except Exception:
            return None

    def rx_words(self, n):
        sm = self.sm
        if sm.rx_fifo() < n:
            return None
        return [sm.get() & 0xFFFFFFFF for _ in range(n)]

    def timer_start(self, cb):
        T = self.machine.Timer
        self.timer = T(period=1, mode=T.PERIODIC, callback=cb)

    def timer_stop(self):
        t = self.timer
        self.timer = None
        if t is not None:
            try:
                t.deinit()
            except Exception:
                pass


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "on", "yes")
    return bool(v)


def _byte(v, name):
    if isinstance(v, bool) or v is None:
        raise ValueError(name)
    if isinstance(v, str):
        if not 1 <= len(v) <= 2:
            raise ValueError(name)
        v = int(v, 16)
    v = int(v)
    if v < 0 or v > 255:
        raise ValueError(name)
    return v


def _int(d, key, default, lo, hi):
    v = d.get(key, default)
    if isinstance(v, bool) or v is None or isinstance(v, str):
        raise ValueError("%s=%s" % (key, v))
    if int(v) != v or v < lo or v > hi:
        raise ValueError("%s=%s not in %d..%d" % (key, v, lo, hi))
    return int(v)


class Refuse(Exception):
    """A guard said no: args = (code, detail)."""


class BeanTx:
    """Transmitter with its guards. ``tx(dev_id, payload)`` writes NDJSON,
    ``brx`` is the running BeanRx, ``armed()`` the 60 s arming guard of
    main.py, ``idle(ms)`` sleeps while feeding the WDT / relay read-back /
    receiver (main.wdt_sleep_ms), ``log(kind, **fields)`` the event ring."""

    def __init__(self, tx, dev_id, brx, armed, idle, log=None, hw=None):
        self.tx = tx
        self.dev = dev_id
        self.brx = brx
        self.armed = armed
        self.idle = idle
        self.log = log
        self.hw = hw if hw is not None else Hw()
        self.allow = False           # frames allowed (per boot)
        self.bench = False           # bench override (per boot)
        self.sp = SP_DEF
        self.edge = None             # last edge test: dict
        self.fault = None            # latched: no TX until a passing edge test
        self.busy = False            # an operation (or a bench op) owns GP7
        self.killed = None           # set by the timer watchdog / guard()
        self.stamps = []             # ticks_ms of wire events
        self.n_frames = 0            # wire attempts since boot
        self.n_edges = 0
        self._dirty = True           # frame program delays not loaded yet
        self.hw.release()

    # -- safety net --------------------------------------------------------------
    def release(self):
        self.hw.release()

    def guard(self):
        """Outside an operation GP7 must be low (called every few ms). Also
        drops rate stamps older than the window, so they are gone long
        before ticks_diff could wrap (6.2 days) and count as recent."""
        if not self.busy and self.hw.tx_level():
            self.hw.release()
            self._latch("gp7 high outside tx")
        st = self.stamps
        if st and ticks_diff(ticks_ms(), st[0]) >= RATE_WIN_MS:
            st.pop(0)

    def _tick(self, _t):
        """1 kHz timer during an operation. A legal frame holds GP7 high for
        at most 6 bit times (648 us); longer = release and stop."""
        try:
            hw = self.hw
            if hw.tx_level():
                t0 = ticks_us()
                while hw.tx_level():
                    if ticks_diff(ticks_us(), t0) > WD_HOLD_US:
                        hw.release()
                        self.killed = "gp7 high > 1 ms"
                        return
        except Exception:
            self.killed = "timer"
            try:
                self.hw.release()
            except Exception:
                pass

    def _latch(self, why):
        self.fault = why
        self.allow = False
        self.edge = None
        if self.log is not None:
            try:
                self.log("bean_tx_fault", why=why)
            except Exception:
                pass

    # -- guards ------------------------------------------------------------------
    def _rate(self, now):
        """Raise unless one more wire event fits the budgets."""
        st = self.stamps
        while st and ticks_diff(now, st[0]) >= RATE_WIN_MS:
            st.pop(0)
        if len(st) >= RATE_WIN_N:
            raise Refuse("rate", "%d in %d s" % (RATE_WIN_N, RATE_WIN_MS // 1000))
        if len(st) >= RATE_PER_S and ticks_diff(now, st[-RATE_PER_S]) < 1000:
            raise Refuse("rate", "%d per s" % RATE_PER_S)

    def budget(self, now):
        st = self.stamps
        n = 0
        for t in st:
            if ticks_diff(now, t) < RATE_WIN_MS:
                n += 1
        return RATE_WIN_N - n

    def _tb16(self):
        """Bit time x16 us for a transmission, or refuse."""
        brx = self.brx
        if brx is None:
            raise Refuse("rx", "no receiver")
        why = brx.health()
        if why:
            raise Refuse("rx", why)
        if not brx.tb_measured():
            if not self.bench:
                raise Refuse("tb", "bit time not measured on the bus yet")
            return TB16_NOM
        return brx.tb16

    def _wake(self, d):
        if self.brx.bus() == 1 and not _truthy(d.get("wake", 0)):
            raise Refuse("asleep", "bus asleep; add wake=1 to wake the car")

    def blocked(self, dst, mes):
        """Reason a DST/MES pair is refused without the bench override."""
        if (dst << 8) | mes in self.brx.seen:
            return "%02X:%02X seen on this bus from another node" % (dst, mes)
        if bytes((dst,)) in DENY_DST or (dst == 0xFE and bytes((mes,)) in DENY_FE_MES):
            return "%02X:%02X is real node traffic" % (dst, mes)
        if (dst, mes) not in ALLOW:
            return "%02X:%02X not on the allow list" % (dst, mes)
        return None

    def edge_fit(self, tb16, sp):
        """Why the measured edges do not fit sample point ``sp``, or None."""
        e = self.edge
        if e is None or not e.get("ok_meas"):
            return "no edge test since boot (op edge)"
        s_us = frame_delays(sp)[0] * tb16 // (16 * NCYC)
        if e["rise_us"] > EDGE_OK_US or e["fall_us"] > EDGE_OK_US:
            return "rise %d / fall %d us, limit %d" % (e["rise_us"], e["fall_us"], EDGE_OK_US)
        if e["rise_us"] + EDGE_MARGIN_US > s_us or e["fall_us"] + EDGE_MARGIN_US > s_us:
            return "rise %d / fall %d us + %d > sample point %d us" % (
                e["rise_us"], e["fall_us"], EDGE_MARGIN_US, s_us)
        return None

    # -- commands ----------------------------------------------------------------
    def command(self, d):
        op = str(d.get("op", "status")).lower()
        try:
            if op == "status":
                r = self.status()
            elif op == "edge":
                r = self._op_edge(d)
            elif op == "send":
                r = self._op_send(d)
            elif op == "allow":
                r = self._op_allow(d)
            elif op == "bench":
                r = self._op_bench(d)
            elif op == "sp":
                self.sp = _int(d, "pct", SP_DEF, SP_MIN, SP_MAX)
                r = {"ok": 1, "sp": self.sp}
            else:
                raise Refuse("args", "op=%s" % op)
        except Refuse as e:
            r = {"ok": 0, "err": e.args[0], "detail": e.args[1]}
        except (ValueError, TypeError) as e:
            r = {"ok": 0, "err": "args", "detail": str(e)}
        except Exception as e:
            r = {"ok": 0, "err": "exc", "detail": repr(e)[:80]}
        finally:
            self._end()
        r["ack"] = "bean_tx"
        r["op"] = op
        self.tx(self.dev, r)
        return r

    def _begin(self):
        self.killed = None
        self.busy = True
        self.brx.hold = True
        self.hw.timer_start(self._tick)

    def _end(self):
        """Every path out of a command: GP7 low, state machines and timer off."""
        try:
            self.hw.timer_stop()
        except Exception:
            pass
        self.hw.release()
        brx = self.brx
        if brx is not None:
            brx.hold = False
            if getattr(brx, "tapping", False):
                brx.tap_take()
        self.busy = False

    def status(self):
        now = ticks_ms()
        brx = self.brx
        r = {"ok": 1, "armed": 1 if self.armed() else 0,
             "allow": 1 if self.allow else 0, "bench": 1 if self.bench else 0,
             "sp": self.sp, "fault": self.fault, "edge": self.edge,
             "budget": self.budget(now), "frames": self.n_frames,
             "edges": self.n_edges, "test": "%02X:%02X" % (TEST_DST, TEST_MES)}
        if brx is not None:
            r["bus"] = brx.bus()
            r["rx"] = brx.health()
            r["tb"] = (brx.tb16 * 10) >> 4
            r["tb_meas"] = 1 if brx.tb_measured() else 0
        return r

    def _need_armed(self):
        if not self.armed():
            raise Refuse("disarmed", "send bean_tx_enable v=1 first (60 s)")

    def _op_allow(self, d):
        v = _truthy(d.get("v", 0))
        if v:
            self._need_armed()
            if self.fault:
                raise Refuse("fault", self.fault)
            if self.edge is None or not self.edge.get("ok_meas"):
                raise Refuse("edge", "no edge test since boot (op edge)")
        self.allow = v
        return {"ok": 1, "allow": 1 if v else 0}

    def _op_bench(self, d):
        v = _truthy(d.get("v", 0))
        if v:
            self._need_armed()
        self.bench = v
        if self.log is not None:
            self.log("bean_tx_bench", v=1 if v else 0)
        return {"ok": 1, "bench": 1 if v else 0}

    # -- edge test ---------------------------------------------------------------
    def _op_edge(self, d):
        self._need_armed()
        tb16 = self._tb16()
        self._wake(d)
        hw = self.hw
        brx = self.brx
        now = ticks_ms()
        self._rate(now)
        self._begin()
        brx.own = bytearray()
        words = None
        while True:
            t0 = ticks_ms()
            if ticks_diff(t0, now) > EDGE_MAX_MS:
                raise Refuse("busy", "bus not idle for 7.8 bit times; no pulse sent")
            if not self.armed():
                raise Refuse("disarmed", "arming expired")
            if not hw.rx():                          # dominant right now
                self.idle(1)
                continue
            brx.own_until = ticks_add(t0, OWN_MS)
            brx.tap(8)
            hw.start(_edge, EDGE_CYC * 16000000 // tb16, ())
            f = 0
            while True:
                self.idle(1)
                f = hw.flags()
                if f & (F_STOP | F_WDOG) or self.killed:
                    break
                if ticks_diff(ticks_ms(), t0) > 6:
                    self.killed = "edge timeout"
                    break
            words = hw.rx_words(2)
            hw.release()
            if self.killed or f & F_WDOG:
                self._latch(self.killed or "watchdog sm")
                raise Refuse("fault", self.fault)
            if words is not None:
                break
            self.idle(2)                             # busy: nothing was driven
        hw.timer_stop()
        self.stamps.append(t0)
        self.n_edges += 1
        # The receiver's view of the pulse: the first dominant run after at
        # least 7 bit times of recessive (a run that ended before the tap
        # started may still come through first).
        idle_us = 7 * tb16 // 16
        dom = None
        t1 = ticks_ms()
        while True:
            raw = brx.raw
            prev = 0
            if raw is not None:
                for i in range(brx.raw_n):
                    v = raw[i]
                    if v > 0 and -prev >= idle_us:
                        dom = v
                        break
                    prev = v
            if dom is not None or ticks_diff(ticks_ms(), t1) >= ECHO_MS:
                break
            self.idle(1)
        runs = brx.tap_take()
        cyc16 = tb16                                 # one PIO cycle = tb16/(16*EDGE_CYC) us
        rise = None if words[0] > EDGE_W else (2 * (EDGE_W - words[0]) * cyc16) // (16 * EDGE_CYC)
        fall = None if words[1] > EDGE_T else ((1 + 2 * (EDGE_T - words[1])) * cyc16) // (16 * EDGE_CYC)
        meas = rise is not None and fall is not None
        ok = meas and rise <= EDGE_OK_US and fall <= EDGE_OK_US
        e = {"rise_us": rise, "fall_us": fall, "w_us": (tb16 * 10 >> 4) / 10,
             "rx_dom_us": dom, "ok_meas": 1 if meas else 0, "t": t0}
        self.edge = e
        if ok:
            self.fault = None                        # a clean pulse clears a latched fault
        r = {"ok": 1 if ok else 0, "runs": runs}
        r.update(e)
        if not ok:
            r["err"] = "edge"
            r["detail"] = ("no rise seen on GP6 within the pulse" if rise is None else
                           "bus still dominant %d us after the release" % (
                               EDGE_T * tb16 // (8 * EDGE_CYC)) if fall is None else
                           "rise %d / fall %d us, limit %d" % (rise, fall, EDGE_OK_US))
        return r

    # -- frame -------------------------------------------------------------------
    def _op_send(self, d):
        pri = _int(d, "pri", 0, 0, 15)
        dst = _byte(d.get("dst"), "dst")
        mes = _byte(d.get("mes"), "mes")
        data = d.get("data")
        if not isinstance(data, str):
            raise ValueError("data")
        data = unhexlify(data)
        if not 1 <= len(data) <= 11:
            raise ValueError("data: 1..11 bytes")
        retries = _int(d, "retries", RETRIES_DEF, 0, RETRIES_MAX)
        if self.fault:
            raise Refuse("fault", self.fault)
        self._need_armed()
        if not self.allow:
            raise Refuse("frames_off", "frames not allowed this boot (op allow v=1)")
        tb16 = self._tb16()
        why = self.edge_fit(tb16, self.sp)
        if why:
            raise Refuse("edge", why)
        bits, nbits, hdr, body, amb = encode(pri, dst, mes, data)
        if not self.bench:
            why = self.blocked(dst, mes)
            if why:
                raise Refuse("blocked", why)
            if amb:
                raise Refuse("crc_stuff", "CRC ends in 5 equal bits (stuffing rule unresolved)")
        if max_dominant_run(bits, nbits) > 6 or nbits > (FIFO_WORDS - 2) * 32:
            raise Refuse("args", "stream")
        self._wake(d)
        self._rate(ticks_ms())
        if patch_frame(frame_delays(self.sp, self.edge["fall_us"], tb16)):
            self._dirty = True
        words = [IFS_BITS * NCYC // 2 - 1, nbits - 1] + to_words(bits, nbits)
        while len(words) < FIFO_WORDS:
            words.append(0)
        freq = NCYC * 16000000 // tb16
        budget_ms = (IFS_BITS + nbits + 6) * tb16 // 16000 + 4
        brx = self.brx
        hw = self.hw
        res = {"ok": 0, "acked": None, "attempts": 0, "lost_arb": 0, "bit_err": 0,
               "fault": 0, "busy": 0, "pri": pri, "dst": "%02X" % dst,
               "mes": "%02X" % mes, "data": hexlify(data).decode().upper()}
        t_op = ticks_ms()
        self._begin()
        brx.own = body
        err = None
        while True:
            now = ticks_ms()
            brx.own_until = ticks_add(now, OWN_MS)
            if ticks_diff(now, t_op) > OP_MAX_MS:
                err = err or "busy"
                break
            if not self.armed():
                err = "disarmed"
                break
            try:
                self._rate(now)
            except Refuse:
                err = "rate"
                break
            if not hw.rx():                          # dominant right now
                self.idle(1)
                continue
            own0 = brx.own_n
            hw.start(_frame, freq, words, self._dirty)
            self._dirty = False
            f = 0
            while True:
                self.idle(1)
                f = hw.flags()
                if f & (F_STOP | F_WDOG | F_LOST) or self.killed:
                    break
                if ticks_diff(ticks_ms(), now) > budget_ms:
                    self.killed = "frame timeout"
                    break
            pulled = FIFO_WORDS - hw.tx_fifo()
            hw.release()
            ro = hw.readout()
            if self.killed or f & F_WDOG:
                self._latch(self.killed or "watchdog sm")
                res["fault"] = 1
                err = "fault"
                break
            if pulled < 3 and not f & (F_LOST | F_FAULT):
                res["busy"] += 1                     # never left the idle check
                self.idle(1)
                continue
            self.stamps.append(now)
            self.n_frames += 1
            res["attempts"] += 1
            if f & F_FAULT:
                # dominant sent, recessive read: never retried, TX latched off
                self._latch("line fault")
                res["fault"] = 1
                err = "fault"
                break
            if f & F_LOST:
                pos = None
                if ro is not None and ro[1] < nbits:
                    pos = nbits - 1 - ro[1]
                    arb = pos < hdr
                else:
                    arb = pulled <= 3                # first 32 wire bits
                res["pos"] = pos
                res["lost_arb" if arb else "bit_err"] += 1
                err = "lost_arb" if arb else "bit_err"
            else:
                # complete: RSP as sampled by the PIO (GP6: 1 = recessive)
                rsp = None if ro is None else (~ro[0]) & 3
                t1 = ticks_ms()
                while brx.own_n == own0 and ticks_diff(ticks_ms(), t1) < ECHO_MS:
                    self.idle(1)
                if brx.own_n != own0 and brx.own_res[0] == 0:
                    res["rx_rsp"] = brx.own_res[1]
                    if rsp is None:
                        rsp = brx.own_res[1]
                res["acked"] = rsp
                if rsp == 1:
                    res["ok"] = 1
                    err = None
                    break
                err = "no_ack" if not rsp else ("nak" if rsp == 2 else "rsp")
            if res["attempts"] > retries:
                break
            self.idle(BACKOFF_MS * res["attempts"])
        if err:
            res["err"] = err
        res["t_ms"] = ticks_diff(ticks_ms(), t_op)
        return res
