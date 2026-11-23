"""Host statistics of the POCO computers: temperatures, CPU / memory use, power.

One collector for both phones:

* on the car computer (``poco``) the backend runs :class:`HostStats` in a
  thread of its own;
* on the secondary POCO (``poco2``) the same file runs as an agent. The
  backend starts it over SSH with this file's source on the agent's stdin
  (``ssh user@poco2 python3 -u - --period 2``), so nothing is installed
  there, and reads one JSON line per period from its stdout
  (:class:`RemoteHostStats`). When the link or the phone goes away the SSH
  process ends and the reader starts it again once the phone is wanted.

The file is therefore stdlib-only and must not import the rest of the package.

Temperatures are peak-held: the zones are read every ``sample_s`` (0.19 s, not
a divisor of 1 s) and the reported CPU / GPU figure is the maximum of the last
``hold_s`` (10 s, the fan loop cadence); see backend/thermal_sampler.py for the
2026-11-17 incident that made this necessary.

Record (all fields optional, ``None`` when the kernel does not provide them)::

    {"host": "poco2", "t": 1795360000.0, "up_s": 512,
     "cpu_c": 61.2, "gpu_c": 48.9, "batt_c": 33.0,        # peak-held maxima
     "hot_zone": "cpu5-thermal", "hot_c": 58.1,            # hottest zone right now
     "zones": {"cpu0-thermal": 44.1, ...},                 # every zone right now
     "cpu_pct": 12.5, "little_pct": 9.0, "big_pct": 16.1,  # since the last record
     "cores_online": 8, "little_mhz": 1766, "big_mhz": 2649,
     "load1": 0.42, "mem_total_mb": 5655, "mem_avail_mb": 4900, "mem_used_pct": 13.4,
     "gpu_mhz": 257, "power_w": 1.02, "volt_v": 4.04, "amp_a": 0.25,
     "disk_used_pct": 41.0, "wifi_dbm": -52}
"""
from __future__ import annotations

import glob
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

CPU_PREFIXES = ("cpu", "cluster")
GPU_PREFIXES = ("gpu",)
BATTERY = "/sys/class/power_supply/qcom-battery"
LITTLE = (0, 1, 2, 3)          # SDM845: Silver cores
BIG = (4, 5, 6, 7)             # Gold cores


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except OSError:
        return None


def _num(path: str, scale: float = 1.0) -> Optional[float]:
    raw = _read(path)
    if raw is None:
        return None
    try:
        return float(raw.split()[0]) / scale
    except (ValueError, IndexError):
        return None


def discover_zones(pattern: str = "/sys/class/thermal/thermal_zone*") -> List[Tuple[str, str]]:
    """``(zone type, temp path)`` for every thermal zone."""
    out = []
    for zone_dir in sorted(glob.glob(pattern)):
        ztype = _read(zone_dir + "/type")
        if ztype:
            out.append((ztype, zone_dir + "/temp"))
    return out


def zone_kind(ztype: str) -> Optional[str]:
    if ztype.startswith(CPU_PREFIXES):
        return "cpu"
    if ztype.startswith(GPU_PREFIXES):
        return "gpu"
    if ztype == "qcom-battery":
        return "battery"
    return None


class _Peak:
    """Maximum of the samples of the last ``hold_s`` seconds."""

    def __init__(self, hold_s: float) -> None:
        self.hold_s = hold_s
        self._q: deque = deque()

    def add(self, now: float, value: Optional[float]) -> None:
        if value is not None:
            while self._q and self._q[-1][1] <= value:
                self._q.pop()
            self._q.append((now, value))
        while self._q and now - self._q[0][0] > self.hold_s:
            self._q.popleft()

    def value(self) -> Optional[float]:
        return self._q[0][1] if self._q else None


def parse_proc_stat(text: str) -> Dict[str, Tuple[int, int]]:
    """``{"cpu": (busy, total), "cpu0": ...}`` jiffies from /proc/stat."""
    out: Dict[str, Tuple[int, int]] = {}
    for line in text.splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        try:
            vals = [int(v) for v in parts[1:9]]
        except ValueError:
            continue
        if len(vals) < 5:
            continue
        idle = vals[3] + vals[4]                 # idle + iowait
        total = sum(vals)
        out[parts[0]] = (total - idle, total)
    return out


def cpu_percent(prev: Dict[str, Tuple[int, int]], cur: Dict[str, Tuple[int, int]],
                names: List[str]) -> Optional[float]:
    busy = total = 0
    for n in names:
        if n in prev and n in cur:
            busy += cur[n][0] - prev[n][0]
            total += cur[n][1] - prev[n][1]
    if total <= 0:
        return None
    return round(100.0 * busy / total, 1)


def parse_meminfo(text: str) -> Dict[str, Optional[float]]:
    kb: Dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("MemTotal:", "MemAvailable:"):
            try:
                kb[parts[0]] = float(parts[1])
            except ValueError:
                pass
    total, avail = kb.get("MemTotal:"), kb.get("MemAvailable:")
    used = round(100.0 * (total - avail) / total, 1) if total and avail is not None else None
    return {"mem_total_mb": round(total / 1024.0) if total else None,
            "mem_avail_mb": round(avail / 1024.0) if avail is not None else None,
            "mem_used_pct": used}


def _wifi_dbm(iface: str = "wlan0") -> Optional[float]:
    raw = _read("/proc/net/wireless")
    if not raw:
        return None
    for line in raw.splitlines():
        if line.strip().startswith(iface + ":"):
            try:
                return float(line.split()[3].rstrip("."))
            except (ValueError, IndexError):
                return None
    return None


class HostStats:
    """Sampler + collector. ``start()`` runs the fast temperature sampler in
    a thread; :meth:`record` returns one record (CPU use since the previous
    call)."""

    def __init__(self, host: Optional[str] = None, sample_s: float = 0.19,
                 hold_s: float = 10.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.host = host or socket.gethostname()
        self.sample_s = sample_s
        self._clock = clock
        self._zones = discover_zones()
        self._peaks = {"cpu": _Peak(hold_s), "gpu": _Peak(hold_s), "battery": _Peak(hold_s)}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._prev_stat: Dict[str, Tuple[int, int]] = {}
        self._gpu_freq = next(iter(sorted(glob.glob("/sys/class/devfreq/*gpu*/cur_freq"))), None)

    # -- fast temperature sampler ------------------------------------------------

    def _zone_temps(self) -> Dict[str, float]:
        out = {}
        for ztype, path in self._zones:
            v = _num(path, 1000.0)
            if v is not None and -40.0 < v < 200.0:
                out[ztype] = v
        return out

    def sample_once(self) -> Dict[str, float]:
        temps = self._zone_temps()
        best: Dict[str, Optional[float]] = {"cpu": None, "gpu": None, "battery": None}
        for ztype, v in temps.items():
            kind = zone_kind(ztype)
            if kind and (best[kind] is None or v > best[kind]):
                best[kind] = v
        now = self._clock()
        with self._lock:
            for kind, peak in self._peaks.items():
                peak.add(now, best[kind])
        return temps

    def start(self) -> None:
        if self._thread is None:
            self.sample_once()
            self._thread = threading.Thread(target=self._run, name="host-stats", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.sample_s):
            try:
                self.sample_once()
            except Exception:
                pass

    # -- record --------------------------------------------------------------------

    def record(self) -> dict:
        temps = self.sample_once()
        with self._lock:
            peak = {k: p.value() for k, p in self._peaks.items()}
        rec: dict = {"host": self.host, "t": round(time.time(), 2)}
        rec["up_s"] = _num("/proc/uptime")
        if rec["up_s"] is not None:
            rec["up_s"] = int(rec["up_s"])
        r1 = lambda v: round(v, 1) if v is not None else None
        rec["cpu_c"], rec["gpu_c"], rec["batt_c"] = r1(peak["cpu"]), r1(peak["gpu"]), r1(peak["battery"])
        if temps:
            hot = max(temps, key=temps.get)
            rec["hot_zone"], rec["hot_c"] = hot, r1(temps[hot])
        rec["zones"] = {k: r1(v) for k, v in temps.items()}

        stat = parse_proc_stat(_read("/proc/stat") or "")
        prev, self._prev_stat = self._prev_stat, stat
        rec["cpu_pct"] = cpu_percent(prev, stat, ["cpu"])
        rec["little_pct"] = cpu_percent(prev, stat, ["cpu%d" % i for i in LITTLE])
        rec["big_pct"] = cpu_percent(prev, stat, ["cpu%d" % i for i in BIG])
        rec["cores_online"] = sum(1 for k in stat if k != "cpu") or None
        for name, core in (("little_mhz", LITTLE[0]), ("big_mhz", BIG[0])):
            f = _num("/sys/devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq" % core, 1000.0)
            rec[name] = int(f) if f is not None else None
        rec["load1"] = _num("/proc/loadavg")
        rec.update(parse_meminfo(_read("/proc/meminfo") or ""))
        if self._gpu_freq:
            f = _num(self._gpu_freq, 1e6)
            rec["gpu_mhz"] = int(f) if f is not None else None
        v, a = _num(BATTERY + "/voltage_now", 1e6), _num(BATTERY + "/current_now", 1e6)
        rec["volt_v"] = round(v, 3) if v is not None else None
        rec["amp_a"] = round(abs(a), 3) if a is not None else None
        rec["power_w"] = round(abs(v * a), 2) if v is not None and a is not None else None
        try:
            st = os.statvfs("/")
            rec["disk_used_pct"] = round(100.0 * (1.0 - st.f_bavail / st.f_blocks), 1)
        except (OSError, ZeroDivisionError):
            rec["disk_used_pct"] = None
        rec["wifi_dbm"] = _wifi_dbm()
        return rec


class RemoteHostStats:
    """Reads the agent's JSON lines from an SSH process it keeps alive.

    ``ssh_cmd``: argv up to and including ``user@host``. ``wanted``: callable,
    True while the remote host is meant to be up (the reader does not dial a
    phone that is off). :meth:`latest` returns ``(record, age_s)``.
    """

    def __init__(self, ssh_cmd: List[str], wanted: Callable[[], bool] = lambda: True,
                 period_s: float = 2.0, retry_s: float = 10.0, host: str = "",
                 popen: Callable = subprocess.Popen,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ssh_cmd = list(ssh_cmd)
        self.wanted = wanted
        self.period_s = period_s
        self.retry_s = retry_s
        self.host = host
        self._popen = popen
        self._clock = clock
        self._lock = threading.Lock()
        self._rec: Optional[dict] = None
        self._rec_t: Optional[float] = None
        self._proc = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.starts = 0
        self.lines = 0
        self.last_error = ""

    def argv(self) -> List[str]:
        cmd = list(self.ssh_cmd)
        user_host = cmd.pop()
        cmd += ["-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3", user_host,
                "python3", "-u", "-", "--period", str(self.period_s)]
        if self.host:
            cmd += ["--host", self.host]
        return cmd

    @staticmethod
    def agent_source() -> bytes:
        with open(__file__.replace(".pyc", ".py"), "rb") as f:
            return f.read()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="host-stats-remote", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

    def latest(self) -> Tuple[Optional[dict], Optional[float]]:
        with self._lock:
            if self._rec is None or self._rec_t is None:
                return None, None
            return self._rec, self._clock() - self._rec_t

    def feed_line(self, line: str) -> bool:
        """Take one agent output line; False when it is not a record."""
        try:
            rec = json.loads(line)
        except ValueError:
            return False
        if not isinstance(rec, dict) or "t" not in rec:
            return False
        with self._lock:
            self._rec, self._rec_t = rec, self._clock()
        self.lines += 1
        return True

    def _session(self) -> None:
        self.starts += 1
        proc = self._popen(self.argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, start_new_session=True)
        self._proc = proc
        try:
            proc.stdin.write(self.agent_source())
            proc.stdin.close()
            for raw in proc.stdout:
                if self._stop.is_set():
                    break
                self.feed_line(raw.decode("utf-8", "replace"))
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
            self._proc = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self.wanted():
                    self._session()
            except Exception as exc:                 # ssh missing, pipe errors
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
            if self._stop.wait(self.retry_s):
                break


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    period, host = 2.0, None
    while args:
        a = args.pop(0)
        if a == "--period" and args:
            period = float(args.pop(0))
        elif a == "--host" and args:
            host = args.pop(0)
    stats = HostStats(host=host)
    stats.start()
    stats.record()                      # primes the CPU-use delta
    try:
        while True:
            time.sleep(period)
            sys.stdout.write(json.dumps(stats.record(), separators=(",", ":")) + "\n")
            sys.stdout.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        return 0


if __name__ == "__main__":
    sys.exit(main())
