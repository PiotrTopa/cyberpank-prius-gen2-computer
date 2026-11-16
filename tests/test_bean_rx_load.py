"""BEAN receiver under the IG-on load of the car (powerbox fw 2.4.2).

fw 2.4.1 lost frames with IG on (2026-11-16: 284 ring overflows, 129 lost
words/s, ``cpu`` pinned at 940 per mille) where fw 2.4.0 lost none
(2026-11-13). The bus load was the same: 1710 words/s on 16 Nov
(1581 decoded + 129 lost), 1719 words/s on 13 Nov, ~33 words per frame,
~52 frames/s. The decoder time per decoded word, ``cpu`` / edges/s over the
2-s stats windows, was 535 us on 2.4.0 (p10-p90 527-545) and 583 us on 2.4.1
(572-599, also in windows without an overflow): +9 %, with 2.4.0 already
at 924 per mille.

Cause: fw 2.4.1 added four BeanRx attributes (69 -> 73). MicroPython keeps
instance attributes in an open-addressing table with linear probing that
grows only when it is completely full (sizes ..., 59, 73, 97, ...). With 73
attributes the table is 100 % full: a lookup that misses the instance (every
``self.method`` call, ~3 per word in the decoder) scans all 73 slots and
hits probe long chains. Replaying the decoder's attribute accesses through a
model of that table gives 220 probes per word on 2.4.0 and 416 on 2.4.1
(the weighted bytecode work fell by 5 %); fitted to the two car figures a
probe costs ~0.35 us (~34 cycles at 96 MHz).

fw 2.4.2: 74 attributes (table 97, load 0.76), the run decoder called
through a bound method kept in an attribute (no per-run miss), hot constants
folded by const(), the stuffed-data state inline in a native ``_run``.

What is measured and what is modelled here:
* measured on the host: the decoder's output (bit-exact against the frames
  fed in), the MicroPython attribute-table probes of every attribute access
  the decoder makes (``MpMap``, a model of py/map.c), and the CPython
  bytecode it executes, weighted per instruction class (``WEIGHTS``, an
  estimate of relative MicroPython cost; native functions get cheap simple
  instructions).
* calibrated on the car: ``US_PER_CYCLE`` and ``US_PER_PROBE`` are solved
  from the two measured costs (2.4.0: 535 us/word, 2.4.1: 583 us/word) with
  the 2.4.0 / 2.4.1 code measured on this stream.
* modelled: the main loop (``SLICE_US`` relay read-back + 5 ms sleep per
  ``wdt_sleep_ms`` slice, ``BODY_US`` of sensor/telemetry work per second;
  the car's 924 per mille on 2.4.0 with a stable ring bounds them to ~7 %)
  and the DMA ring (the receiver's own overflow check on a 1024-word ring).
  Result for 2.4.2: ~290 us per word, ~50 % duty at the 16 Nov load
  (~450 us and ~78 % when only the attribute-table fix is credited).
The model is not a timing model of the RP2040; it carries the car's two
measurements over to the new code through what changed in it.
"""

import dis
import os
import re
import subprocess
import sys
from array import array

import pytest

import bean_wire as W
import test_bean_rx as T

pytestmark = pytest.mark.skipif(not os.path.exists(W.FIXTURE),
                                reason="real-capture fixture not present")

REPO = os.path.dirname(T.POWERBOX_DIR)

# ─── car numbers (2-s stats windows with > 1000 edges/s) ────────────────────
LOAD_16NOV = 1710            # words/s on the bus, 2026-11-16 (decoded + lost)
LOAD_13NOV_PEAK = 1810       # highest 2-s window, 2026-11-13 (no loss there)
US_WORD_240 = 535.0          # decoder us per decoded word, fw 2.4.0
US_WORD_241 = 583.0          # fw 2.4.1
FW_240 = "c34772b"           # commits of the two firmware versions
FW_241 = "1053d1b"

# ─── main-loop model ────────────────────────────────────────────────────────
SLICE_US = 6000              # per wdt_sleep_ms slice: relay read-back, guard, 5 ms sleep
BODY_US = 15000              # per 1 s main-loop pass: INA219/BMP280 reads, telemetry
BODY_EVERY_US = 1000000

# ─── relative MicroPython cost per CPython instruction class (estimate) ─────
WEIGHTS = {"simple": 30, "simple_native": 5, "binop": 80, "global": 150,
           "attr": 100, "pycall": 1500, "call": 400, "subscr": 150}
HASH_BYTES = 2               # MICROPY_QSTR_BYTES_IN_HASH
SIZES = (0, 2, 4, 6, 8, 10, 12, 17, 23, 29, 37, 47, 59, 73, 97, 127, 167, 223, 293)


# ─── MicroPython attribute table (py/map.c) ─────────────────────────────────

def qstr_hash(name):
    h = 5381
    for c in name.encode():
        h = ((h << 5) + h) ^ c
    h &= (1 << (8 * HASH_BYTES)) - 1
    return h or 1


class MpMap:
    """Open addressing, linear probing, grows only when full."""

    def __init__(self):
        self.alloc = 0
        self.used = 0
        self.slots = []

    def add(self, key):
        if self.alloc == 0:
            self._grow()
        p0 = p = qstr_hash(key) % self.alloc
        while True:
            if self.slots[p] is None:
                self.slots[p] = key
                self.used += 1
                return
            if self.slots[p] == key:
                return
            p = (p + 1) % self.alloc
            if p == p0:
                self._grow()
                self.add(key)
                return

    def _grow(self):
        old = [k for k in self.slots if k is not None]
        self.alloc = next(s for s in SIZES if s >= self.alloc + 1)
        self.slots = [None] * self.alloc
        self.used = 0
        for k in old:
            self.add(k)

    def probes(self, key):
        """Slots looked at by a lookup of ``key`` (hit or miss)."""
        if self.alloc == 0:
            return 0
        p0 = p = qstr_hash(key) % self.alloc
        n = 0
        while True:
            n += 1
            if self.slots[p] is None or self.slots[p] == key:
                return n
            p = (p + 1) % self.alloc
            if p == p0:
                return n


def mp_table(n_attrs):
    """(alloc, load) of an instance table holding n_attrs attributes."""
    m = MpMap()
    for i in range(n_attrs):
        m.add("a%d" % i)
    return m.alloc, m.used / m.alloc


# ─── receiver variants and the IG-on stream ─────────────────────────────────

def _load(path, name):
    import importlib.util
    rp2 = T._install_stubs()
    saved = sys.modules.get("rp2")
    sys.modules["rp2"] = rp2
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if saved is None:
            del sys.modules["rp2"]
        else:
            sys.modules["rp2"] = saved
    mod._src = open(path).read()
    mod._path = path
    return mod


_VARIANTS = {}


def variant(tag, tmp_dir):
    """Receiver module: "2.4.2" = powerbox/bean_rx.py, older ones from git."""
    if tag in _VARIANTS:
        return _VARIANTS[tag]
    if tag == "2.4.2":
        path = os.path.join(T.POWERBOX_DIR, "bean_rx.py")
    else:
        commit = {"2.4.0": FW_240, "2.4.1": FW_241}[tag]
        try:
            src = subprocess.run(["git", "show", "%s:powerbox/bean_rx.py" % commit],
                                 cwd=REPO, capture_output=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            pytest.skip("git history for fw %s not available" % tag)
        path = os.path.join(str(tmp_dir), "bean_rx_%s.py" % tag.replace(".", ""))
        with open(path, "wb") as f:
            f.write(src)
    _VARIANTS[tag] = _load(path, "bean_rx_load_" + tag.replace(".", ""))
    return _VARIANTS[tag]


@pytest.fixture(scope="module")
def tmp_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("bean_rx_variants")


def ig_on_runs(nframes, words_per_s, start=0):
    """Real NHW20 frames (fixture order) spaced so the bus carries
    ``words_per_s`` capture words per second. Returns (runs, words, frames)."""
    fx = W.load_fixture()[start:start + nframes]
    frames = []
    for _, _, pri, ml, dst, mes, data, crc, ack in fx:
        bits = W.frame_bits(pri, dst, mes, data, crc=crc, rsp=(0, 1) if ack else (0, 0))
        frames.append(W.bits_to_runs(bits))
    nwords = sum(len(r) for r in frames)
    period = 1e6 * nwords / len(frames) / words_per_s      # us per frame
    runs = [(0, 5000.0)]
    for fr in frames:
        dur = sum(d for _, d in fr)
        runs.extend(fr)
        runs[-1] = (0, runs[-1][1] + max(period - dur, 1000.0))
    want = [(p, "%02X" % d, "%02X" % m, data.hex().upper())
            for _, _, p, ml, d, m, data, crc, ack in fx]
    return runs, W.runs_to_words(runs), want


def _timed_words(runs, t0):
    out = []
    t = t0
    for v, d in runs:
        ws = W.runs_to_words([(v, d)])
        for k, w in enumerate(ws[:-1]):
            out.append((t + (k + 1) * 10004, w))
        t += d
        out.append((t, ws[-1]))
    return out


class _Clock:
    us = 5_000_000


def _receiver(mod, clock, lines):
    mod.ticks_us = lambda: clock.us
    mod.ticks_ms = lambda: clock.us // 1000
    cap = T.FakeCapture(mod.RING)
    rx = mod.BeanRx(lambda dev, m: lines.append(m) or True, 3, 6, capture=cap)
    assert rx.enable(True) == "test"
    return rx, cap


# ─── per-word cost of a variant (host measurement + car calibration) ────────

_MEASURED = {}


def measure(mod, nframes=120):
    """Weighted VM cycles and attribute-table probes per capture word while
    decoding real frames at the 16 Nov load (memoised)."""
    key = (mod._path, nframes)
    if key not in _MEASURED:
        _MEASURED[key] = _measure(mod, nframes)
    return _MEASURED[key]


def _measure(mod, nframes):
    runs, _, _ = ig_on_runs(nframes, LOAD_16NOV)
    native = set(re.findall(r"@micropython\.native\s+def (\w+)", mod._src))
    folded = set(re.findall(r"^(\w+) *= *const\(", mod._src, re.M))
    methods = {k for k, v in vars(mod.BeanRx).items() if callable(v)} | {"crc8"}
    path = mod._path
    clock = _Clock()
    lines = []
    table = MpMap()
    names = []

    class Probe(mod.BeanRx):
        def __getattribute__(self, k):
            if k != "__dict__" and object.__getattribute__(self, "__dict__").get("_on"):
                names.append(k)
            return object.__getattribute__(self, k)

        def __setattr__(self, k, v):
            d = object.__getattribute__(self, "__dict__")
            if k != "_on":
                if k not in d:
                    table.add(k)
                if d.get("_on"):
                    names.append(k)
            object.__setattr__(self, k, v)

    real = mod.BeanRx
    mod.BeanRx = Probe
    try:
        rx, cap = _receiver(mod, clock, lines)
    finally:
        mod.BeanRx = real
    object.__setattr__(rx, "_on", 1)

    code_info = {}
    cycles = [0]

    def info(code):
        m = code_info.get(code)
        if m is not None:
            return m
        ins = list(dis.get_instructions(code))
        nat = code.co_name in native
        m = {}
        for j, i in enumerate(ins):
            op = i.opname
            if op.startswith("CALL"):
                py = False
                for k in range(j - 1, max(j - 12, -1), -1):
                    p = ins[k]
                    if p.opname.startswith(("LOAD_ATTR", "LOAD_GLOBAL", "LOAD_FAST")):
                        py = p.argval in methods or p.argval == "run"
                        break
                w = WEIGHTS["pycall" if py else "call"]
            elif op.startswith(("LOAD_ATTR", "STORE_ATTR")):
                w = WEIGHTS["attr"]
            elif op == "LOAD_GLOBAL":
                w = WEIGHTS["simple_native" if nat else "simple"] if i.argval in folded \
                    else WEIGHTS["global"]
            elif op.startswith(("BINARY_OP", "COMPARE_OP")):
                w = WEIGHTS["binop"]
            elif "SUBSCR" in op:
                w = WEIGHTS["subscr"]
            else:
                w = WEIGHTS["simple_native" if nat else "simple"]
            m[i.offset] = w
        code_info[code] = m
        return m

    def tracer(frame, event, arg):
        if frame.f_code.co_filename != path:
            return None
        frame.f_trace_opcodes = True
        frame.f_trace_lines = False
        m = info(frame.f_code)

        def t(fr, ev, a):
            if ev == "opcode":
                cycles[0] += m.get(fr.f_lasti, 0)
            return t
        return t

    words = _timed_words(runs, clock.us)
    i = 0
    end = words[-1][0] + 300_000
    sys.settrace(tracer)
    try:
        while clock.us < end:
            clock.us += 5000
            batch = []
            while i < len(words) and words[i][0] <= clock.us:
                batch.append(words[i][1])
                i += 1
            cap.push(batch)
            rx.poll()
    finally:
        sys.settrace(None)
    assert rx.cnt[0] == nframes and rx.ovf == 0
    probes = sum(table.probes(k) for k in names)
    return cycles[0] / len(words), probes / len(words), table


_CAL = {}


def calibration(tmp_dir):
    """(us per model cycle, us per probe) from the car's 2.4.0 / 2.4.1 costs."""
    if not _CAL:
        c0, p0, _ = measure(variant("2.4.0", tmp_dir))
        c1, p1, _ = measure(variant("2.4.1", tmp_dir))
        # US_WORD_240 = s*c0 + q*p0 ; US_WORD_241 = s*c1 + q*p1
        det = c0 * p1 - c1 * p0
        s = (US_WORD_240 * p1 - US_WORD_241 * p0) / det
        q = (c0 * US_WORD_241 - c1 * US_WORD_240) / det
        _CAL.update(s=s, q=q, m240=(c0, p0), m241=(c1, p1))
    return _CAL["s"], _CAL["q"]


def us_per_word(tag, tmp_dir):
    s, q = calibration(tmp_dir)
    c, p, _ = measure(variant(tag, tmp_dir))
    return s * c + q * p


# ─── main loop + DMA ring replay ────────────────────────────────────────────

def replay(mod, us_word, words_per_s, seconds, stall_us=0, stall_every_us=0):
    """Feed IG-on traffic in real time; every poll() costs ``us_word`` per
    word it decoded. Returns the receiver, decoder duty, the frames out and
    the frames fed in."""
    nframes = int(seconds * words_per_s / 33) + 1
    runs, _, want = ig_on_runs(nframes, words_per_s)
    clock = _Clock()
    lines = []
    rx, cap = _receiver(mod, clock, lines)
    words = _timed_words(runs, clock.us)
    i = 0
    t_end = words[-1][0] + 500_000
    busy = 0
    next_body = clock.us + BODY_EVERY_US
    next_stall = clock.us + stall_every_us if stall_every_us else None
    while clock.us < t_end:
        batch = []
        while i < len(words) and words[i][0] <= clock.us:
            batch.append(words[i][1])
            i += 1
        cap.push(batch)
        tail0 = rx.tail
        rx.poll()
        done = (rx.tail - tail0) & 0x3FFFFFFF
        cost = int(done * us_word)
        busy += cost
        clock.us += cost + SLICE_US
        if clock.us >= next_body:                    # wdt_sleep_ms() returned
            clock.us += BODY_US
            next_body += BODY_EVERY_US
        if next_stall is not None and clock.us >= next_stall:
            clock.us += stall_us
            next_stall += stall_every_us
    span = words[-1][0] - words[0][0]
    got = [(f[1], f[2], f[3], f[4]) for m in lines for f in m.get("bf", []) if f[6] == 0]
    return rx, busy / span, got, want


# ─── tests ──────────────────────────────────────────────────────────────────

class TestAttributeTable:
    def test_micropython_table_sizes(self):
        assert mp_table(69) == (73, 69 / 73)            # fw 2.4.0
        assert mp_table(73) == (73, 1.0)                # fw 2.4.1: full
        assert mp_table(74)[0] == 97                    # fw 2.4.2

    def test_receiver_table_not_full(self, tmp_dir):
        """All BeanRx attributes exist after a decode (no growth mid-drive)
        and their table is <= 85 % full."""
        mod = variant("2.4.2", tmp_dir)
        _, _, table = measure(mod, nframes=20)
        clock = _Clock()
        rx, _ = _receiver(mod, clock, [])
        n_init = len(vars(rx))
        assert table.used == n_init, "attributes created after __init__"
        alloc, load = mp_table(n_attrs=n_init)
        assert load <= 0.85, "%d attributes fill %.0f %% of a %d-slot table" % (
            n_init, 100 * load, alloc)

    def test_fw241_table_was_full(self, tmp_dir):
        _, _, table = measure(variant("2.4.1", tmp_dir), nframes=20)
        assert (table.used, table.alloc) == (73, 73)


class TestCostModel:
    def test_calibration_reproduces_the_car(self, tmp_dir):
        s, q = calibration(tmp_dir)
        assert s > 0 and 0.1 < q < 1.0                   # us per probe: 10-96 cycles
        assert us_per_word("2.4.0", tmp_dir) == pytest.approx(US_WORD_240, rel=1e-6)
        assert us_per_word("2.4.1", tmp_dir) == pytest.approx(US_WORD_241, rel=1e-6)

    def test_fw242_probes_and_cost(self, tmp_dir):
        calibration(tmp_dir)
        c2, p2, _ = measure(variant("2.4.2", tmp_dir))
        c1, p1 = _CAL["m241"]
        assert p2 < 0.2 * p1                              # attribute-table probes
        assert c2 < 0.7 * c1                              # weighted VM work
        assert us_per_word("2.4.2", tmp_dir) < 0.6 * US_WORD_241


class TestIgOnLoad:
    """16 Nov IG-on load through the receiver, the ring and the main loop."""

    def test_fw242_keeps_up(self, tmp_dir):
        mod = variant("2.4.2", tmp_dir)
        rx, duty, got, want = replay(mod, us_per_word("2.4.2", tmp_dir), LOAD_16NOV, 20)
        assert rx.ovf == 0 and rx.lost == 0
        assert got == want
        assert duty < 0.65, duty
        assert rx.lag < mod.RING // 2

    def test_fw242_peak_load_with_stalls(self, tmp_dir):
        """13 Nov peak 2-s rate and a 300 ms main-loop stall every 3 s."""
        mod = variant("2.4.2", tmp_dir)
        rx, duty, got, want = replay(mod, us_per_word("2.4.2", tmp_dir), LOAD_13NOV_PEAK,
                                     20, stall_us=300_000, stall_every_us=3_000_000)
        assert rx.ovf == 0 and rx.lost == 0 and got == want

    def test_fw241_overflows_on_the_same_load(self, tmp_dir):
        mod = variant("2.4.1", tmp_dir)
        rx, duty, got, want = replay(mod, us_per_word("2.4.1", tmp_dir), LOAD_16NOV, 20)
        assert rx.ovf > 0 and rx.lost > 0
        assert len(got) < len(want)

    def test_fw242_without_credit_for_vm_work(self, tmp_dir):
        """Bound: only the attribute-table fix credited (2.4.1's weighted VM
        work, 2.4.2's probes) still keeps up with headroom."""
        s, q = calibration(tmp_dir)
        _, p2, _ = measure(variant("2.4.2", tmp_dir))
        c1, _ = _CAL["m241"]
        mod = variant("2.4.2", tmp_dir)
        rx, duty, got, want = replay(mod, s * c1 + q * p2, LOAD_16NOV, 20)
        assert rx.ovf == 0 and rx.lost == 0 and got == want
        assert duty < 0.85, duty

    def test_fw240_was_at_the_edge(self, tmp_dir):
        """Calibration check: 2.4.0 on the 16 Nov load sits above 90 % duty
        (car: 924 per mille, ring backlog up to 690 words). The model is a
        little pessimistic there: at the 13 Nov mean (1719 words/s) it
        overflows a few times per minute, the car did not."""
        mod = variant("2.4.0", tmp_dir)
        rx, duty, got, want = replay(mod, us_per_word("2.4.0", tmp_dir), LOAD_16NOV, 20)
        assert duty > 0.9
        assert rx.ovf == 0 and got == want
