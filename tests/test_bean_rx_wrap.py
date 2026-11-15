"""BEAN receiver on MicroPython tick semantics (powerbox fw 2.4.1).

The other host tests stub ``ticks_add`` as ``a + b``. MicroPython's ticks
wrap at 2^30 and ``ticks_add`` raises ``OverflowError('ticks interval
overflow')`` for |delta| >= 2^29 (537 s in us, 6.2 days in ms). fw 2.4.0 hit
that at the first edge after a bus sleep of more than ~9 min (a run length
added to the frame cursor), dropped that wake-up's time from the frame
clock (frame ``t_ms`` minutes off ``t``), marked the receiver unhealthy for
5 s (so ``bean_wake``'s send was refused with err "rx"), and reported
``idle_ms`` stuck at 1073741 / an ``age_ms`` that wraps after 6.2 days.
"""

import importlib.util
import os
import sys

import pytest

import test_bean_rx as T

PERIOD = 1 << 30
HALF = 1 << 29
MASK = PERIOD - 1


def ticks_add(t, d):
    if not -HALF < d < HALF:
        raise OverflowError("ticks interval overflow")
    return (t + d) & MASK


def ticks_diff(a, b):
    return ((a - b + HALF) & MASK) - HALF


class MicroClock:
    def __init__(self, us):
        self.us = us

    def ticks_us(self):
        return self.us & MASK

    def ticks_ms(self):
        return (self.us // 1000) & MASK


@pytest.fixture(scope="module")
def brx():
    rp2 = T._install_stubs()
    saved = sys.modules.get("rp2")
    sys.modules["rp2"] = rp2
    try:
        spec = importlib.util.spec_from_file_location(
            "bean_rx_wrap", os.path.join(T.POWERBOX_DIR, "bean_rx.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if saved is None:
            del sys.modules["rp2"]
        else:
            sys.modules["rp2"] = saved
    return mod


class MicroRig(T.Rig):
    def __init__(self, mod, monkeypatch, start_us):
        self.mod = mod
        self.clock = MicroClock(start_us)
        monkeypatch.setattr(mod, "ticks_us", self.clock.ticks_us)
        monkeypatch.setattr(mod, "ticks_ms", self.clock.ticks_ms)
        monkeypatch.setattr(mod, "ticks_add", ticks_add)
        monkeypatch.setattr(mod, "ticks_diff", ticks_diff)
        self.cap = T.FakeCapture(mod.RING)
        self.lines = []
        self.tx_ok = True
        self.rx = mod.BeanRx(self._tx, 3, 6, capture=self.cap)
        assert self.rx.enable(True) == "test"

    def ages(self):
        """(line t - frame t_ms) per frame, ms."""
        out = []
        for _, m in self.lines:
            for f in m.get("bf", []):
                out.append(ticks_diff(m["t"], f[0]))
        return out


FRAMES = [(1, 0xFE, 0x24, [0x00]), (6, 0xFE, 0x2C, [0x64]), (2, 0x52, 0xED, [0, 0x80, 0x7F, 0, 0])]


def test_long_sleep_then_wake_no_overflow(brx, monkeypatch):
    # start 3 s before ticks_us wraps, sleep 20 min (> 2^30 us: run saturates)
    rig = MicroRig(brx, monkeypatch, start_us=(5 << 30) - 3_000_000)
    for _ in range(10):                          # bit time measured
        rig.feed_runs(T._stream(FRAMES))
    rig.feed_runs([(0, 1_200_000_000.0)] + T._stream(FRAMES, lead_us=3000)[1:], poll_ms=10)
    rig.drain(500)
    rx = rig.rx
    assert rx.exc == 0 and rx.last_exc is None
    assert rx.cnt[brx.E_OK] == 33 and len(rig.frames()) == 33
    ages = rig.ages()
    assert all(0 <= a < 1000 for a in ages), ages          # frame clock == ticks_ms
    assert rx.health() is None                             # bean_wake would not be refused


def test_wake_after_sleep_keeps_frame_time(brx, monkeypatch):
    """The frame after a 15 min sleep is dated at its SOF, not minutes off."""
    rig = MicroRig(brx, monkeypatch, start_us=7 << 30)
    rig.feed_runs(T._stream(FRAMES))
    t_wake = rig.clock.us + 900_000_000
    rig.feed_runs([(0, 900_000_000.0)] + T._stream(FRAMES[:1], lead_us=3000)[1:], poll_ms=10)
    rig.drain(300)
    f = rig.frames()[-1]
    sof_ms = (t_wake // 1000) & MASK
    assert abs(ticks_diff(f[0], sof_ms)) <= 2
    assert rig.rx.exc == 0


def test_idle_and_age_do_not_wrap(brx, monkeypatch):
    rig = MicroRig(brx, monkeypatch, start_us=11 << 30)
    rig.feed_runs(T._stream(FRAMES))
    rx = rig.rx
    t_ok = rig.clock.us
    rx.cur = brx.M30                               # bus asleep for > 17.9 min
    idles, ages = [], []
    for _ in range(8 * 24):                       # 8 days, stats every hour
        rig.clock.us += 3_600_000_000
        st = rx.stats(rig.clock.ticks_ms())
        idles.append(st["idle_ms"])
        ages.append(st["age_ms"])
    assert idles == sorted(idles) and ages == sorted(ages)
    days8 = 8 * 24 * 3_600_000
    assert abs(ages[-1] - (rig.clock.us - t_ok) // 1000) <= 200
    assert days8 - 3_600_000 <= idles[-1] <= days8 + 3_600_000


def test_anchor_bounded(brx, monkeypatch):
    rig = MicroRig(brx, monkeypatch, start_us=3 << 30)
    rig.feed_runs(T._stream(FRAMES))
    rig.rx.wt = ticks_add(rig.rx.wt, -(HALF - 1))   # a broken word cursor
    rig.drain(100)
    assert abs(rig.rx.off) <= brx.OFF_MAX
    rig.feed_runs(T._stream(FRAMES))
    assert rig.rx.exc == 0
