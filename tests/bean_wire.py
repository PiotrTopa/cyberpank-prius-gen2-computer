"""Test helpers: build BEAN frames on the wire (bits, runs, PIO words).

Used by tests/test_bean_rx.py. The word encoder mirrors the PIO program in
powerbox/bean_rx.py (checked against a cycle-level simulation of that
program in the tests).
"""

import gzip
import os
import random

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "bean_nhw20_frames.txt.gz")


def crc8(data):
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x13) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def _byte_bits(b):
    return [(b >> (7 - i)) & 1 for i in range(8)]


def frame_bits(pri, dst, mes, data, crc=None, ml=None, rsp=(0, 1),
               stuff_after_crc=False, eof=6):
    """Logic bits (1 = dominant) of one frame from SOF to EOF.

    ``stuff_after_crc``: insert the stuff bit that a run of 5 at the very end
    of the CRC would call for (the unresolved case; specadmin/BeanMPX do NOT
    insert it, the default here)."""
    if ml is None:
        ml = 2 + len(data)
    body = [(pri << 4) | (ml & 0x0F), dst, mes] + list(data)
    if crc is None:
        crc = crc8(body)
    raw = [1]
    for b in body + [crc]:
        raw += _byte_bits(b)
    out = []
    run_v, run_n = None, 0
    for i, bit in enumerate(raw):
        out.append(bit)
        if bit == run_v:
            run_n += 1
        else:
            run_v, run_n = bit, 1
        if run_n == 5 and (i < len(raw) - 1 or stuff_after_crc):
            out.append(1 - bit)
            run_v, run_n = 1 - bit, 1
    out += [0, 1, 1, 1, 1, 1, 1, 0]
    out += list(rsp)
    out += [0] * eof
    return out


def needs_stuff_after_crc(pri, dst, mes, data):
    """True when the CRC's last bits complete a run of 5 (ambiguous case)."""
    a = frame_bits(pri, dst, mes, data, stuff_after_crc=False)
    b = frame_bits(pri, dst, mes, data, stuff_after_crc=True)
    return a != b


def bits_to_runs(bits, tbit=100.0):
    """[(logic, dur_us)] with consecutive equal bits merged."""
    runs = []
    for bit in bits:
        if runs and runs[-1][0] == bit:
            runs[-1][1] += tbit
        else:
            runs.append([bit, tbit])
    return [(v, d) for v, d in runs]


def add_idle(runs, us):
    """Append recessive time (merged with a trailing recessive run)."""
    runs = list(runs)
    if runs and runs[-1][0] == 0:
        runs[-1] = (0, runs[-1][1] + us)
    else:
        runs.append((0, us))
    return runs


def jitter(runs, j_us, rng):
    """Move every edge by uniform(-j, +j) us (run lengths stay positive)."""
    t = 0.0
    edges = []
    for _, d in runs:
        t += d
        edges.append(t)
    moved = [e + rng.uniform(-j_us, j_us) for e in edges[:-1]] + [edges[-1]]
    out = []
    prev = 0.0
    for (v, _), e in zip(runs, moved):
        out.append((v, max(1.0, e - prev)))
        prev = e
    return out


def insert_glitch(runs, idx, at_us, width_us):
    """Split run ``idx`` with an opposite-level pulse of ``width_us``."""
    v, d = runs[idx]
    a = min(max(at_us, 1.0), d - width_us - 1.0)
    return runs[:idx] + [(v, a), (1 - v, width_us), (v, d - a - width_us)] + runs[idx + 1:]


def runs_to_words(runs, T=10000, off=3, to_us=10004):
    """PIO words for logic runs. GP6 is inverted: logic 1 (dominant) = GP6
    low = level bit 0. Each run of D us gives k timeout words (D >= ...) and
    one end word with x = T - (D - off - k*to)."""
    words = []
    for v, d in runs:
        lvl = 0 if v else 1
        d = int(round(d))
        rem = d - off
        k = 0
        while rem > T:
            if rem - to_us < 0:
                break
            words.append((lvl << 16) | 0xFFFF)
            rem -= to_us
            k += 1
        x = T - rem if rem >= 0 else T
        if x < 0:
            x = 0
        words.append((lvl << 16) | x)
    return words


def load_fixture():
    """[(file, t_ms, pri, ml, dst, mes, data bytes, crc, ack)]"""
    out = []
    with gzip.open(FIXTURE, "rt") as f:
        for line in f:
            if line.startswith("#"):
                continue
            fn, t, pri, ml, dst, mes, data, crc, ack = line.split()
            out.append((fn, int(t), int(pri), int(ml), int(dst, 16), int(mes, 16),
                        bytes.fromhex(data), int(crc, 16), int(ack)))
    return out


def random_frame(rng):
    n = rng.randint(1, 11)
    return (rng.randint(0, 15), rng.choice([0xFE, 0x52, 0x62, 0x98, rng.randint(0, 255)]),
            rng.randint(0, 255), bytes(rng.randint(0, 255) for _ in range(n)))
