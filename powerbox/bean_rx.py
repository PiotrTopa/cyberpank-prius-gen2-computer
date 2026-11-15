"""
BEAN (Toyota MPX body bus) receiver/decoder for the powerbox (fw 2.3.0;
2.4.0: idle fast path, own-frame flag and hooks for bean_tx.py; 2.4.1:
wrap-safe tick arithmetic, frame times anchored to edge stamps).

Listens on GP6 (the transceiver's RX, INVERTED: bus idle 0 V -> GP6 high,
bus dominant +B -> GP6 low). It never touches GP7 (TX) and never ACKs.
Protocol: docs/BEAN_PROTOCOL.md (frame SOF PRI ML DST MES DATA CRC EOM RSP
EOF, MSB first, stuffing after 5 equal bits SOF..CRC, CRC-8 poly 0x13 init 0
over PRI/ML..last data byte, EOM 0x7E unstuffed, 100 us bits).

Capture: a PIO state machine (PIO1 SM0, 2 MHz) measures the length of every
GP6 level run with 1 us resolution and pushes one word per edge:
    bit 16 = GP6 level of the run that just ended, bits 15..0 = loop count
    left (run_us = T_CNT - x + OFF_US). A run longer than T_CNT us pushes a
    "timeout" word (x = 0xFFFF) and keeps counting, so idle and stuck-bus
    periods show up in real time (~100 words/s while the bus sleeps).
The words go to a RAM ring (1024 words): DMA (rp2.DMA, MicroPython >= 1.21)
when available, otherwise a hard IRQ handler that drains the FIFO, otherwise
plain polling of the 4-word RX FIFO (lossy; reported as mode "poll").

Decoding runs in the main loop (poll(), from wdt_sleep_ms slices), at most
MAX_WORDS words per call: run -> bit count with an adaptive bit time (and a
dominant/recessive skew estimate), glitch filter, destuffing at run level,
frame state machine, CRC check, EOM search (tolerant of the unresolved stuff
bit before EOM), two raw RSP bits. Decoded frames are batched to the host on
the BEAN id (3 -> backend 203) at most every BATCH_MS:
    {"t":ms,"bf":[[t_ms,pri,"DST","MES","DATAHEX",rsp,code],...],"st":{...}}
A frame sent by our own transmitter (bean_tx.py sets ``own``) carries an
8th element 1.

Time base (2.4.1): every word advances the word-time cursor ``wt``
(ticks_us domain) by its exact length; a run's end time is the edge stamp
``last_edge``, its start ``end - d`` (only needed for short runs), so no run
length - which saturates at 2^30 us after an 18 min sleep - is ever added
to a tick value. ticks_add() raises OverflowError for |delta| >= 2^29 (537 s
in us, 6.2 days in ms); every delta here is bounded far below that, and
ages reported over longer spans (idle_ms, age_ms) are carried in steps.

Idle cost (2.4.0): while the bus is quiet the ~100 timeout words/s are
consumed by a short fast path (no decoder, no time anchor), a full pass runs
every IDLE_SKIP polls, and once the bus has slept for 1 s the capture is
only looked at every SLEEP_DIV-th poll (wake latency <= 20 ms, far inside
the ring).
"""

import time
from array import array
from binascii import hexlify

try:
    import micropython                         # @micropython.native is a
except ImportError:                            # compile-time directive on
    class micropython:                         # the board; identity here
        @staticmethod
        def native(f):
            return f

ticks_ms = time.ticks_ms
ticks_us = time.ticks_us
ticks_add = time.ticks_add
ticks_diff = time.ticks_diff

# ─── capture constants ──────────────────────────────────────────────────────
SM_ID = 4                    # PIO1 SM0 (PIO0 left alone)
PIO_BASE = 0x50300000        # PIO1 registers
PIO_FDEBUG = PIO_BASE + 0x008
PIO_RXF0 = PIO_BASE + 0x020
DREQ_PIO_RX0 = 12            # DREQ_PIO1_RX0
SM_FREQ = 2000000            # 96 MHz / 48: 0.5 us per PIO cycle
T_CNT = 10000                # loop counts (us) before a timeout word
OFF_US = 3                   # per-run overhead cycles not counted by the loop
TO_US = T_CNT + 4            # length of one timeout segment
TO_MARK = 0xFFFF
RING_BITS = 10
RING = 1 << RING_BITS        # words in the ring
RMASK = RING - 1
M30 = 0x3FFFFFFF             # producer/consumer counters wrap at 2^30
DMA_COUNT = 0x3FFFFFFF       # transfers per DMA arm (small int)
DMA_REARM = 1 << 20

# ─── decoder constants ──────────────────────────────────────────────────────
GLITCH_US = 12               # shorter runs are merged into their neighbours
IDLE_BITS = 7                # >= 7 recessive bit times = bus idle
TB16_NOM = 1600              # bit time x16 (100 us)
TB16_MIN = 80 * 16
TB16_MAX = 125 * 16
SKEW_MAX = 30                # us
MAX_WORDS = 256              # words decoded per poll() call
LEAK_US = 4                  # time-anchor leak per poll

# ─── reporting ──────────────────────────────────────────────────────────────
BATCH_MS = 200
BATCH_MAX = 16               # frames per line
QUEUE_MAX = 64               # frames waiting for the host
STATS_MS = 2000
ERR_PER_S = 5                # failed frames reported per second (all counted)
RAW_MAX = 512
RAW_CHUNK = 64
ERR_LIMIT = 10               # consecutive poll() exceptions before giving up
IDLE_SKIP = 40               # fast-path polls between two full passes (200 ms)
SLEEP_US = 1000000           # recessive this long = bus asleep
SLEEP_DIV = 4                # asleep: look at the capture every 4th poll
SEEN_MAX = 160               # distinct foreign DST:MES pairs remembered
BAD_QUIET_MS = 5000          # health(): no receive error for this long
TB16_TX_MIN = 95 * 16        # bit time accepted for transmitting (x16 us)
TB16_TX_MAX = 108 * 16
ADAPT_MIN = 16               # runs measured before the bit time counts
OFF_MAX = 1 << 28            # |time anchor| bound (us); ticks_add needs < 2^29
LONG_US = 1 << 28            # runs this long have no meaningful start time
AGE_STEP_MS = 1 << 28        # carry step for ms ages (ticks_diff wraps at 2^29)

# frame result codes ("code" in a frame record)
E_OK = 0
E_CRC = 1
E_STUFF = 2                  # 6+ equal bits inside SOF..CRC
E_LEN = 3                    # ML outside 3..13
E_FRAME = 4                  # EOM / RSP / EOF shape wrong, stuck dominant
E_TRUNC = 5                  # bus went idle inside the frame
E_OVF = 6                    # ring overflow aborted the frame
CODE_KEYS = ("ok", "crc", "stuff", "len", "frm", "trunc", "ovf")

# decoder states
ST_HUNT = 0                  # wait for >= IDLE_BITS recessive
ST_ARMED = 1                 # idle seen: next dominant run is SOF
ST_DATA = 2                  # SOF..CRC (stuffed)
ST_POST = 3                  # after CRC: looking for EOM 0111111
ST_EOM = 4                   # EOM ones seen: next recessive run = EOM0 [+RSP]
ST_RSPB = 5                  # RSP1 = 1: next dominant run gives RSP2
ST_RSPC = 6                  # RSP1 = 0, RSP2 = 1 (ACK): dominant run must be 1
ST_EOFW = 7                  # RSP done: next recessive run = EOF

_CRC = bytearray(256)
for _i in range(256):
    _c = _i
    for _k in range(8):
        _c = ((_c << 1) ^ 0x13) & 0xFF if _c & 0x80 else (_c << 1) & 0xFF
    _CRC[_i] = _c


def crc8(buf, n):
    """BEAN CRC-8 (poly 0x13, init 0) over buf[0:n]."""
    c = 0
    t = _CRC
    for i in range(n):
        c = t[c ^ buf[i]]
    return c


# ─── PIO program ────────────────────────────────────────────────────────────
try:
    import rp2

    # No FIFO join: the TX FIFO carries T_CNT to the initial pull(); the
    # 4-word RX FIFO is drained by DMA (or the hard IRQ) within microseconds.
    @rp2.asm_pio(in_shiftdir=rp2.PIO.SHIFT_LEFT)
    def _runlen():
        pull()                     # T_CNT -> OSR (once)
        set(y, 1)                  # level bit for high runs
        wrap_target()
        label("hi")                # GP6 high (bus recessive)
        mov(x, osr)
        label("hi_loop")
        jmp(pin, "hi_cont")
        jmp("hi_end")
        label("hi_cont")
        jmp(x_dec, "hi_loop")
        in_(y, 1)                  # timeout: x = 0xFFFF..., keep counting
        in_(x, 16)
        push()
        irq(rel(0))
        jmp("hi")
        label("hi_end")
        in_(y, 1)
        in_(x, 16)
        push()
        irq(rel(0))
        label("lo")                # GP6 low (bus dominant)
        mov(x, osr)
        label("lo_loop")
        jmp(pin, "lo_end")
        jmp(x_dec, "lo_loop")
        in_(null, 1)               # timeout
        in_(x, 16)
        push()
        irq(rel(0))
        jmp("lo")
        label("lo_end")
        in_(null, 1)
        in_(x, 16)
        push()
        irq(rel(0))
        wrap()
except ImportError:
    rp2 = None
    _runlen = None


class Capture:
    """GP6 run-length words into a RAM ring. ``produced()`` returns the
    producer counter (mod 2^30); ``buf[base + (k & RMASK)]`` is word k."""

    def __init__(self, pin_no):
        self.pin_no = pin_no
        self.mode = None
        self.sm = None
        self.dma = None
        self.buf = None
        self.base = 0
        # NB: arrays are built from a bytearray (memcpy on MicroPython; a
        # bytes initializer would be iterated element by element).
        self.head = array("i", [0])   # IRQ/poll producer counter
        self.dbase = 0                # producer count at the last DMA arm
        self.stalls = 0
        self.rearms = 0
        try:
            import machine
            self.mem = machine.mem32
        except (ImportError, AttributeError):
            self.mem = None

    def start(self, want="auto"):
        """Start the state machine and the best drain path. Returns the
        mode ("dma" | "irq" | "poll"); raises when PIO is unavailable."""
        if rp2 is None:
            raise RuntimeError("no rp2 module")
        from machine import Pin
        self.stop()
        pin = Pin(self.pin_no, Pin.IN)
        sm = rp2.StateMachine(SM_ID, _runlen, freq=SM_FREQ, jmp_pin=pin)
        self.sm = sm
        self.head[0] = 0
        sm.put(T_CNT)
        mode = None
        if want in ("auto", "dma") and hasattr(rp2, "DMA"):
            try:
                self._start_dma()
                mode = "dma"
            except Exception:
                self._close_dma()
        if mode is None and want in ("auto", "dma", "irq"):
            try:
                self.buf = array("I", bytearray(4 * RING))
                self.base = 0
                buf = self.buf
                head = self.head

                def _isr(s, buf=buf, head=head, m=RMASK, w=M30):
                    # hard IRQ: no allocation (words < 2^17 are small ints)
                    h = head[0]
                    while s.rx_fifo():
                        buf[h & m] = s.get()
                        h = (h + 1) & w
                    head[0] = h
                sm.irq(_isr, hard=True)
                mode = "irq"
            except Exception:
                mode = None
        if mode is None:
            if self.buf is None:
                self.buf = array("I", bytearray(4 * RING))
                self.base = 0
            mode = "poll"
        self.mode = mode
        self._clear_stall()
        sm.active(1)
        return mode

    def _start_dma(self):
        import uctypes
        nb = 4 * RING
        self.buf = array("I", bytearray(2 * nb))    # aligned window inside
        addr = uctypes.addressof(self.buf)
        aligned = (addr + nb - 1) & ~(nb - 1)
        self.base = (aligned - addr) >> 2
        self.dbase = 0
        self.aligned = aligned
        self.dma = rp2.DMA()
        self._arm(0)

    def _arm(self, at):
        d = self.dma
        ctrl = d.pack_ctrl(size=2, inc_read=False, inc_write=True,
                           ring_size=RING_BITS + 2, ring_sel=True,
                           treq_sel=DREQ_PIO_RX0 + (SM_ID & 3))
        self.dbase = at
        d.config(read=PIO_RXF0 + 4 * (SM_ID & 3),
                 write=self.aligned + 4 * (at & RMASK),
                 count=DMA_COUNT, ctrl=ctrl, trigger=True)

    def _close_dma(self):
        if self.dma is not None:
            try:
                self.dma.active(0)
                self.dma.close()
            except Exception:
                pass
        self.dma = None

    def stop(self):
        if self.sm is not None:
            try:
                self.sm.active(0)
                self.sm.irq(None)
            except Exception:
                pass
        self._close_dma()
        self.sm = None
        self.mode = None

    def _clear_stall(self):
        if self.mem is not None:
            self.mem[PIO_FDEBUG] = 1 << (SM_ID & 3)

    def produced(self):
        """Producer counter; also counts PIO RX stalls (= lost edges)."""
        mem = self.mem
        if mem is not None:
            bit = 1 << (SM_ID & 3)
            if mem[PIO_FDEBUG] & bit:
                self.stalls += 1
                mem[PIO_FDEBUG] = bit
        mode = self.mode
        if mode == "dma":
            d = self.dma
            left = d.count
            p = (self.dbase + DMA_COUNT - left) & M30
            if left < DMA_REARM:
                d.active(0)                  # FIFO buffers the gap
                p = (self.dbase + DMA_COUNT - d.count) & M30
                self._arm(p)
                self.rearms += 1
            return p
        if mode == "poll":
            sm = self.sm
            h = self.head[0]
            buf = self.buf
            while sm.rx_fifo():
                buf[h & RMASK] = sm.get()
                h = (h + 1) & M30
            self.head[0] = h
        return self.head[0]


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "on", "yes")
    return bool(v)


class BeanRx:
    """Receiver + decoder + reporter. ``tx(dev_id, payload) -> bool`` is the
    firmware's NDJSON writer; frames go out on ``dev_id``."""

    def __init__(self, tx, dev_id, pin_no, capture=None):
        self.tx = tx
        self.dev = dev_id
        self.cap = capture if capture is not None else Capture(pin_no)
        self.on = False
        self.err = None
        self.fbuf = bytearray(16)
        self.cnt = [0, 0, 0, 0, 0, 0, 0]
        self.q = []
        self.edges = 0
        self.glitches = 0
        self.ovf = 0
        self.lost = 0
        self.lag = 0
        self.qdrop = 0
        self.txdrop = 0
        self.esup = 0
        self.cpu_us = 0
        self.exc = 0
        self.exc_run = 0
        self.last_exc = None
        self.raw = None
        self.raw_n = 0
        self.raw_sent = 0
        now = ticks_ms()
        self.t_tx = now
        self.t_st = now
        self.t_err = now
        self.t_cpu = now
        self.last_ok = None
        self.ok_carry = 0            # ms of age_ms carried out of last_ok
        self.idle_ref = None         # ticks_ms reference while cur is saturated
        self.idle_base = 0           # idle ms up to idle_ref
        self.own = None              # bytearray PRI/ML..DATA of our TX frame
        self.own_until = 0           # ticks_ms: ``own`` is ignored afterwards
        self.own_n = 0               # own frames/fragments seen
        self.own_res = None          # (code, rsp) of the last one
        self.seen = set()            # (dst << 8) | mes of foreign good frames
        self.bad_ms = None           # ticks_ms of the last receive error
        self.stall0 = 0
        self.tapping = False         # raw buffer belongs to bean_tx
        self.hold = False            # bean_tx busy: no idle fast path
        self._reset_decoder()
        self.tb16 = TB16_NOM
        self.nadapt = 0
        self.s0 = 0
        self.s1 = 0
        self.skew = 0

    # -- control ---------------------------------------------------------------
    def _reset_decoder(self):
        self.tail = 0
        self.cur = 0
        self.pl = -1
        self.pd = 0
        self.gl = 0
        self.idled = False
        self.ilv = 0                 # level of the run that went idle
        self.nskip = 0
        self.ndiv = 0
        self.st = ST_HUNT
        self.rsp = None
        self.nby = 0
        self.etok = ERR_PER_S
        self.sof_t = 0
        now_us = ticks_us()
        self.wt = now_us             # word-time cursor (ticks_us domain)
        self.et = now_us             # emitted-run cursor
        self.off = 0                 # min-filtered (now - wt): time anchor
        self.last_edge = now_us      # wt at the newest edge
        self.dhalf = 0               # dominant runs measure 0.5 us short
        self.now_us = now_us
        self.now_ms = ticks_ms()

    def enable(self, on):
        """Start/stop capture. Returns the mode or None (error in .err)."""
        on = bool(on)
        if not on:
            self.cap.stop()
            self.on = False
            return None
        try:
            mode = self.cap.start()
        except Exception as e:
            self.err = "start: %r" % (e,)
            self.on = False
            return None
        self._reset_decoder()
        self.err = None
        self.exc_run = 0
        self.on = True
        return mode

    def command(self, d):
        """{"a":"bean_rx",...}: returns (reply, error)."""
        if "on" in d:
            mode = self.enable(_truthy(d.get("on")))
            if self.on is False and _truthy(d.get("on")):
                return None, self.err or "start failed"
            return {"ack": "bean_rx", "on": 1 if self.on else 0,
                    "mode": mode}, None
        if "raw" in d:
            try:
                n = int(d.get("raw"))
            except (TypeError, ValueError):
                return None, "raw=%s" % (d.get("raw"),)
            if n < 1 or n > RAW_MAX:
                return None, "raw=%d not in 1..%d" % (n, RAW_MAX)
            self.raw = array("i", bytearray(4 * n))
            self.raw_n = 0
            self.raw_sent = 0
            self.tapping = False
            return {"ack": "bean_rx", "raw": n}, None
        op = str(d.get("op", "stats")).lower()
        if op == "stats":
            now = ticks_ms()
            self.tx(self.dev, {"t": now, "st": self.stats(now)})
            return {"ack": "bean_rx", "op": "stats"}, None
        return None, "op=%s" % op

    def stats(self, now):
        c = self.cnt
        dt = ticks_diff(now, self.t_cpu)
        cpu = (self.cpu_us // dt) if dt > 0 else 0        # per mille
        self.cpu_us = 0
        self.t_cpu = now
        st = {"on": 1 if self.on else 0, "mode": self.cap.mode,
              "edges": self.edges, "glitch": self.glitches,
              "ovf": self.ovf, "lost": self.lost, "stall": self.cap.stalls,
              "lag": self.lag, "qdrop": self.qdrop, "txdrop": self.txdrop,
              "esup": self.esup, "cpu": cpu, "exn": self.exc,
              "tb": (self.tb16 * 10) >> 4, "skew": self.skew,
              "idle_ms": self._idle_ms(now), "age_ms": self._age_ms(now)}
        for i in range(7):
            st[CODE_KEYS[i]] = c[i]
        if self.err:
            st["err"] = self.err
        if self.last_exc:
            st["exc"] = self.last_exc
        self.lag = 0
        return st

    def _idle_ms(self, now):
        """Time since the last edge. ticks_us wraps after 17.9 min: up to
        60 s from the anchored edge stamp, then the word-domain run length;
        once that saturates (2^30 us) on the ms clock, carried in
        AGE_STEP_MS steps (stats() runs every STATS_MS while on)."""
        cur = self.cur
        if cur >= M30:
            if self.idle_ref is None:
                self.idle_ref = now
                self.idle_base = M30 // 1000
            elif ticks_diff(now, self.idle_ref) >= AGE_STEP_MS:
                self.idle_ref = ticks_add(self.idle_ref, AGE_STEP_MS)
                self.idle_base += AGE_STEP_MS
            return self.idle_base + ticks_diff(now, self.idle_ref)
        self.idle_ref = None
        if cur > 60000000:
            return cur // 1000
        return ticks_diff(self.now_us, ticks_add(self.last_edge, self.off)) // 1000

    def _age_ms(self, now):
        """Time since the last good frame, -1 before the first; carried in
        AGE_STEP_MS steps so it does not wrap after 6.2 days."""
        if self.last_ok is None:
            return -1
        a = ticks_diff(now, self.last_ok)
        if a >= AGE_STEP_MS:
            self.last_ok = ticks_add(self.last_ok, AGE_STEP_MS)
            self.ok_carry += AGE_STEP_MS
            a -= AGE_STEP_MS
        return self.ok_carry + a

    # -- main-loop entry -------------------------------------------------------
    def poll(self):
        """Decode what the capture produced (bounded) and report. Never
        raises; ERR_LIMIT consecutive failures stop the receiver."""
        if not self.on:
            return
        t0 = ticks_us()
        try:
            if not self._quick():
                self._poll()
            self.exc_run = 0
        except Exception as e:
            self.exc += 1
            self.exc_run += 1
            self.bad_ms = ticks_ms()
            self.last_exc = repr(e)[:60]
            self._abort()
            if self.exc_run >= ERR_LIMIT:
                self.err = "stopped after %d errors" % self.exc_run
                self.enable(False)
        self.cpu_us += ticks_diff(ticks_us(), t0)

    @micropython.native
    def _quick(self):
        """Idle fast path. True = nothing for the decoder: the bus is quiet,
        nothing is queued and the new words (if any) are pure timeouts."""
        if not self.idled or self.hold or self.q or self.raw is not None:
            return False
        skip = self.nskip
        if skip >= IDLE_SKIP or self.gl or self.pl >= 0:
            return False
        self.nskip = skip + 1
        cur = self.cur
        if cur >= SLEEP_US:                     # asleep: decimate
            d = self.ndiv + 1
            if d < SLEEP_DIV:
                self.ndiv = d
                return True
            self.ndiv = 0
        cap = self.cap
        p = cap.produced()
        tail = self.tail
        n = (p - tail) & M30
        if n == 0:
            return True
        if cap.mode == "dma":
            n -= 1                              # newest slot may be in flight
        if n > 8:
            return False
        buf = cap.buf
        base = cap.base
        i = 0
        while i < n:
            if buf[base + ((tail + i) & RMASK)] & 0xFFFF != TO_MARK:
                return False                    # an edge: full decode
            i += 1
        if n:
            cur += n * TO_US
            self.cur = cur if cur < M30 else M30
            self.wt = ticks_add(self.wt, n * TO_US)
            self.tail = (tail + n) & M30
        return True

    def _poll(self):
        cap = self.cap
        p = cap.produced()
        tail = self.tail
        n = (p - tail) & M30
        self.now_us = ticks_us()
        self.now_ms = ticks_ms()
        skipped = self.nskip
        self.nskip = 0
        if n > RING - 16:
            self.ovf += 1
            self.bad_ms = self.now_ms
            self.lost += n
            self._abort()
            tail = p
            n = 0
            self.wt = self.et = self.last_edge = self.now_us   # dropped words' time is lost
            self.off = 0
        if n > self.lag:
            self.lag = n
        if cap.mode == "dma" and n:
            n -= 1                       # newest slot may still be in flight
        if n > MAX_WORDS:
            n = MAX_WORDS
        if n > 0:
            self._drain(cap.buf, cap.base, tail, n)
            tail = (tail + n) & M30
        self.tail = tail
        # Time anchor: (now - word cursor) >= the unfinished run; its running
        # minimum (with a slow leak for clock drift) maps words to ticks.
        # Bounded to +-OFF_MAX so ticks_add(x, off) can never overflow.
        obs = ticks_diff(ticks_us(), self.wt)
        if obs < self.off:
            self.off = obs if obs > -OFF_MAX else -OFF_MAX
        else:
            off = self.off + LEAK_US * (1 + skipped)
            self.off = off if off < OFF_MAX else OFF_MAX
        now = self.now_ms
        # Stamps compared by ticks_diff go stale before they can wrap.
        if self.own is not None and ticks_diff(now, self.own_until) >= 0:
            self.own = None
        bad = self.bad_ms
        if bad is not None and not 0 <= ticks_diff(now, bad) < BAD_QUIET_MS:
            self.bad_ms = None
        self._flush(now)

    def _abort(self):
        if ST_DATA <= self.st <= ST_RSPC:
            self._fin(E_OVF)
        self.st = ST_HUNT
        self.cur = 0
        self.pl = -1
        self.gl = 0
        self.idled = False

    # -- words -> runs ---------------------------------------------------------
    @micropython.native
    def _drain(self, buf, base, tail, n):
        cur = self.cur
        pl = self.pl
        pd = self.pd
        gl = self.gl
        wt = self.wt
        last = self.last_edge
        dh = self.dhalf
        raw = self.raw
        i = 0
        while i < n:
            w = buf[base + ((tail + i) & RMASK)]
            i += 1
            x = w & 0xFFFF
            lv = 0 if w & 0x10000 else 1      # GP6 high = recessive = 0
            if x == TO_MARK:
                if cur < M30:                 # saturate (no big ints)
                    cur += TO_US
                wt = ticks_add(wt, TO_US)
                if gl:                        # held run still growing
                    cur += pd + gl
                    gl = 0
                    pl = -1
                elif pl >= 0:
                    self._run(pl, pd, last)      # it ended at the newest edge
                    pl = -1
                if not self.idled and cur > 2000:
                    self.idled = True
                    self.ilv = lv
                    self._idle(lv)
                continue
            r = T_CNT - x + OFF_US
            d = cur + r
            if lv:                            # a dominant run takes 3.5 us of
                dh ^= 1                       # loop overhead, a recessive one 3:
                r += dh                       # the time base gets the half us
            cur = 0
            self.idled = False
            wt = ticks_add(wt, r)
            prev = last
            last = wt
            self.edges += 1
            if raw is not None and self.raw_n < len(raw):
                rd = d if d < M30 else M30
                raw[self.raw_n] = rd if lv else -rd
                self.raw_n += 1
            if gl:
                pd += gl + d
                gl = 0
                continue
            if d < GLITCH_US and pl >= 0:
                gl = d
                self.glitches += 1
                continue
            if pl >= 0:
                self._run(pl, pd, prev)
            pl = lv
            pd = d
        self.cur = cur
        self.pl = pl
        self.pd = pd
        self.gl = gl
        self.wt = wt
        self.last_edge = last
        self.dhalf = dh

    # -- runs -> frames ----------------------------------------------------------
    def _run(self, v, d, end):
        """One run of level ``v`` (1 = dominant), ``d`` us long, ending at
        word time ``end`` (an edge stamp). ``d`` may be up to 2^30 us after
        a long sleep: it is never added to a tick value."""
        self.et = end
        tb = self.tb16
        dc = d - self.skew if v else d + self.skew
        n = (dc * 16 + (tb >> 1)) // tb
        if n < 1:
            n = 1
        st = self.st
        if st >= ST_DATA and n <= 4:
            # Adaptive bit time (least-squares style: an n-bit run weighs n)
            # and dominant/recessive skew; fast for the first runs after
            # start, slow afterwards so edge jitter does not move the
            # rounding thresholds.
            k = 7
            if self.nadapt < 256:
                self.nadapt += 1
                k = 4 if self.nadapt < 48 else 5
            r = d * 16 - n * tb
            if v:
                self.s1 += (r - self.s1 + (1 << (k - 1))) >> k
            else:
                self.s0 += (r - self.s0 + (1 << (k - 1))) >> k
            sk = (self.s1 - self.s0) >> 5
            self.skew = SKEW_MAX if sk > SKEW_MAX else (-SKEW_MAX if sk < -SKEW_MAX else sk)
            tb += (n * (dc * 16 - n * tb) + (1 << (k + 1))) >> (k + 2)
            self.tb16 = TB16_MIN if tb < TB16_MIN else (TB16_MAX if tb > TB16_MAX else tb)
        if st == ST_DATA:
            self._data(v, n)
        elif st == ST_HUNT:
            if not v and n >= IDLE_BITS:
                self.st = ST_ARMED
        elif st == ST_ARMED:
            if v:
                self._sof(n, ticks_add(end, -d) if d < LONG_US else end)
        elif st == ST_POST:
            self._post(v, n)
        elif st == ST_EOM:
            if v:
                self._fin(E_FRAME)
                self.st = ST_HUNT
            elif n >= 3:
                self.rsp = 0
                self._fin(E_OK)
                self.st = ST_ARMED if n >= IDLE_BITS else ST_HUNT
            else:
                self.st = ST_RSPB if n == 1 else ST_RSPC
        elif st == ST_RSPB:
            self.rsp = 2 if n == 1 else 3
            self._fin(E_OK if n <= 2 else E_FRAME)
            self.st = ST_EOFW
        elif st == ST_RSPC:
            self.rsp = 1
            self._fin(E_OK if n == 1 else E_FRAME)
            self.st = ST_EOFW
        elif st == ST_EOFW:
            self.st = ST_ARMED if n >= 6 else ST_HUNT

    def _idle(self, lv):
        """The unfinished run has lasted > 2 ms: recessive = bus idle,
        dominant = bus stuck."""
        st = self.st
        if lv:                                   # stuck dominant
            if not ST_DATA <= st <= ST_RSPC:
                self.nby = 0
                self.rsp = None
                self.sof_t = self.et
            self._fin(E_FRAME)
            self.st = ST_HUNT
            return
        if st == ST_EOM:
            self.rsp = 0
            self._fin(E_OK)
        elif ST_DATA <= st <= ST_RSPC:
            self._fin(E_TRUNC)
        self.st = ST_ARMED

    def _sof(self, n, start):
        self.sof_t = start
        self.rsp = None
        self.nby = 0
        if n > 5:
            self._fin(E_STUFF)
            self.st = ST_HUNT
            return
        self.acc = (1 << (n - 1)) - 1           # PRI bits after SOF
        self.nb = n - 1
        self.got = n - 1
        self.need = 8
        self.skip = 1 if n == 5 else 0
        self.st = ST_DATA

    def _bits(self, v, k):
        """Append k bits of value v; False on a bad ML."""
        acc = (self.acc << k) | (((1 << k) - 1) if v else 0)
        nb = self.nb + k
        self.got += k
        while nb >= 8:
            nb -= 8
            b = (acc >> nb) & 0xFF
            acc &= (1 << nb) - 1
            self.fbuf[self.nby] = b
            self.nby += 1
            if self.nby == 1:
                ml = b & 0x0F
                if ml < 3 or ml > 13:
                    self._fin(E_LEN)
                    self.st = ST_HUNT
                    return False
                self.need = 16 + 8 * ml
        self.acc = acc
        self.nb = nb
        return True

    def _data(self, v, n):
        r = n - self.skip
        left = 0
        if self.nby:
            take = self.need - self.got
            if r > take:
                left = r - take
        if n - left > 5:
            if not v and left == 0 and n >= IDLE_BITS:
                self._fin(E_TRUNC)
                self.st = ST_ARMED
            else:
                self._fin(E_STUFF)
                self.st = ST_HUNT
            return
        if r - left > 0 and not self._bits(v, r - left):
            return
        self.skip = 1 if (n == 5 and left == 0) else 0
        if self.got == self.need:
            self.st = ST_POST
            self.pz = 0
            self.po = 0
            if left:
                self._post(v, left)

    def _post(self, v, m):
        if v:
            if m == 6 and self.pz:
                self.st = ST_EOM
            elif m == 1 and self.po == 0 and self.pz == 0:
                self.po = 1                      # stuff bit after five 0s
            else:
                self._fin(E_FRAME)
                self.st = ST_HUNT
        else:
            self.pz += m
            if self.pz > 2:
                if m >= IDLE_BITS:
                    self._fin(E_TRUNC)
                    self.st = ST_ARMED
                else:
                    self._fin(E_FRAME)
                    self.st = ST_HUNT

    def _fin(self, code):
        nby = self.nby
        b = self.fbuf
        if code == E_OK and crc8(b, nby - 1) != b[nby - 1]:
            code = E_CRC
        self.cnt[code] += 1
        now = self.now_ms
        mine = 0
        own = self.own
        if own is not None and ticks_diff(now, self.own_until) >= 0:
            own = self.own = None
        if own is not None:
            # ours: the complete frame, or (failed) a prefix of it, e.g. the
            # single pulse of the edge test or an attempt we aborted
            k = nby - 1 if code == E_OK else nby
            lo = len(own)
            if (k == lo if code == E_OK else k <= lo) and b[:k] == own[:k]:
                mine = 1
                self.own_n += 1
                self.own_res = (code, self.rsp)
        if code == E_OK:
            self.last_ok = now
            self.ok_carry = 0
            if not mine and len(self.seen) < SEEN_MAX:
                self.seen.add((b[1] << 8) | b[2])
        else:
            if not mine and code <= E_FRAME:
                self.bad_ms = now
            if ticks_diff(now, self.t_err) >= 1000:
                self.t_err = now
                self.etok = ERR_PER_S
            if self.etok <= 0:
                self.esup += 1
                return
            self.etok -= 1
        if len(self.q) >= QUEUE_MAX:
            self.qdrop += 1
            return
        age = ticks_diff(self.now_us, ticks_add(self.sof_t, self.off))
        t = ticks_add(now, -((age + 500) // 1000))
        end = nby - 1 if code == E_OK else nby
        rec = [
            t,
            b[0] >> 4 if nby else None,
            "%02X" % b[1] if nby > 1 else "",
            "%02X" % b[2] if nby > 2 else "",
            hexlify(b[3:end]).decode().upper() if end > 3 else "",
            self.rsp,
            code]
        if mine:
            rec.append(1)
        self.q.append(rec)

    # -- hooks for the transmitter (bean_tx.py) ----------------------------------
    def bus(self):
        """0 = active, 1 = asleep (recessive >= 1 s), 2 = stuck dominant."""
        if not self.idled:
            return 0
        if self.ilv:
            return 2
        return 1 if self.cur >= SLEEP_US else 0

    def health(self):
        """None when the receiver is fit to supervise a transmission, else
        the reason: off, failed, lossy capture, a receive error (overflow,
        stall, exception, bad frame) in the last BAD_QUIET_MS, stuck bus,
        bit time outside 95..108 us."""
        if not self.on:
            return "rx off"
        if self.err:
            return "rx failed"
        if self.cap.mode == "poll":
            return "rx lossy (poll mode)"
        now = ticks_ms()
        if self.cap.stalls != self.stall0:
            self.stall0 = self.cap.stalls
            self.bad_ms = now
        if self.bad_ms is not None:
            if 0 <= ticks_diff(now, self.bad_ms) < BAD_QUIET_MS:
                return "rx errors"
            self.bad_ms = None
        if self.bus() == 2:
            return "bus stuck dominant"
        if self.tb16 < TB16_TX_MIN or self.tb16 > TB16_TX_MAX:
            return "bit time %d" % ((self.tb16 * 10) >> 4)
        return None

    def tb_measured(self):
        return self.nadapt >= ADAPT_MIN

    def tap(self, n):
        """Record the next ``n`` raw runs for bean_tx (not sent to the host)."""
        self.raw = array("i", bytearray(4 * n))
        self.raw_n = 0
        self.raw_sent = 0
        self.tapping = True

    def tap_take(self):
        raw = self.raw
        out = [] if raw is None or not self.tapping else list(raw[:self.raw_n])
        if self.tapping:
            self.raw = None
            self.tapping = False
        return out

    # -- host reporting ----------------------------------------------------------
    def _flush(self, now):
        raw = self.raw
        if raw is not None and self.raw_n >= len(raw) and not self.tapping:
            k = self.raw_sent
            self.tx(self.dev, {"t": now, "raw": list(raw[k:k + RAW_CHUNK]),
                               "ri": k, "rn": len(raw)})
            self.raw_sent = k + RAW_CHUNK
            if self.raw_sent >= len(raw):
                self.raw = None
            return
        due = ticks_diff(now, self.t_st) >= STATS_MS
        if not due and (not self.q or ticks_diff(now, self.t_tx) < BATCH_MS):
            return
        self.t_tx = now
        msg = {"t": now}
        q = self.q
        batch = None
        if q:
            batch = q[:BATCH_MAX]
            self.q = q[BATCH_MAX:]
            msg["bf"] = batch
        if due:
            self.t_st = now
            msg["st"] = self.stats(now)
        if not self.tx(self.dev, msg) and batch:
            self.txdrop += len(batch)
