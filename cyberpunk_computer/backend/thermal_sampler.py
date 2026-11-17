"""Peak-hold sampler for the phone's CPU / GPU temperature zones.

Why: on the road the big cores heat in short bursts (one to two per second,
about 0.3 s long, 65 -> 95 °C on the core's own sensor).  A 1 Hz reader is
phase-locked to such a load: for a minute it sees only the troughs (68 °C),
then only the peaks (95 °C).  On 2026-11-17 the fan policy therefore ran the
fan at 0 % for 50 s at a time while the hottest core peaked at 94-96 °C, and
the phone hard-reset shortly after.

The sampler reads the zones every ``period_s`` (not a divisor of 1 s) in its
own thread and reports the maximum over the last ``hold_s`` seconds, so the
policy sees the real peaks whatever their phase.
"""
from __future__ import annotations

import glob
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional, Tuple

CPU_PREFIXES = ("cpu", "cluster")
GPU_PREFIXES = ("gpu",)
BATTERY_NAMES = ("qcom-battery",)


def classify(zone_type: str) -> Optional[str]:
    if zone_type.startswith(CPU_PREFIXES):
        return "cpu"
    if zone_type.startswith(GPU_PREFIXES):
        return "gpu"
    if zone_type in BATTERY_NAMES:
        return "battery"
    return None


def discover_zones(pattern: str = "/sys/class/thermal/thermal_zone*") -> Dict[str, List[str]]:
    """Map kind ('cpu' | 'gpu' | 'battery') -> list of ``temp`` file paths."""
    out: Dict[str, List[str]] = {"cpu": [], "gpu": [], "battery": []}
    for zone_dir in sorted(glob.glob(pattern)):
        try:
            with open(zone_dir + "/type", "r") as f:
                kind = classify(f.read().strip())
        except OSError:
            continue
        if kind:
            out[kind].append(zone_dir + "/temp")
    return out


def read_max(paths: List[str]) -> Optional[float]:
    best: Optional[float] = None
    for p in paths:
        try:
            with open(p, "r") as f:
                v = float(f.read().strip()) / 1000.0
        except (OSError, ValueError):
            continue
        if best is None or v > best:
            best = v
    return best


class PeakHold:
    """Maximum of the samples of the last ``hold_s`` seconds (pure, testable)."""

    def __init__(self, hold_s: float = 2.5) -> None:
        self.hold_s = hold_s
        self._q: Deque[Tuple[float, float]] = deque()

    def add(self, now: float, value: Optional[float]) -> None:
        if value is not None:
            # monotonic deque: drop older samples that can never be the max again
            while self._q and self._q[-1][1] <= value:
                self._q.pop()
            self._q.append((now, value))
        self._expire(now)

    def _expire(self, now: float) -> None:
        while self._q and now - self._q[0][0] > self.hold_s:
            self._q.popleft()

    def value(self, now: float) -> Optional[float]:
        self._expire(now)
        return self._q[0][1] if self._q else None


class ThermalSampler:
    """Background reader: ``read()`` -> (cpu peak, gpu peak, battery) in °C."""

    def __init__(self, period_s: float = 0.19, hold_s: float = 2.5,
                 zones: Optional[Dict[str, List[str]]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.period_s = period_s
        self._clock = clock
        self._zones = zones
        self._cpu = PeakHold(hold_s)
        self._gpu = PeakHold(hold_s)
        self._battery: Optional[float] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.samples = 0

    def sample_once(self) -> None:
        if self._zones is None:
            self._zones = discover_zones()
        cpu = read_max(self._zones["cpu"])
        gpu = read_max(self._zones["gpu"])
        bat = read_max(self._zones["battery"])
        now = self._clock()
        with self._lock:
            self._cpu.add(now, cpu)
            self._gpu.add(now, gpu)
            if bat is not None:
                self._battery = bat
            self.samples += 1

    def read(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        now = self._clock()
        with self._lock:
            return (self._cpu.value(now), self._gpu.value(now), self._battery)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="thermal-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample_once()
            except Exception:
                pass
            self._stop.wait(self.period_s)
