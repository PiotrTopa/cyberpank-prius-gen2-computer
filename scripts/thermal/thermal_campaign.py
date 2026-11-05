#!/usr/bin/env python3
"""thermal_campaign.py -- heat-capacity / dissipation campaign on the POCO F1 car computer.

Runs ON the phone. One process does three things:
  * sampler thread (1 Hz): every /sys/class/thermal zone, CPU/GPU clocks, cooling-device
    states, the supply V*I (the battery is amputated -> this is the total POCO power),
    and the backend state (fan duty/RPM, BMP2 outside ambient, ACC, power mode, roadsense)
    -> one CSV row per second, tagged with the protocol phase;
  * protocol main loop: for each fan setting, idle baseline -> full CPU load until
    saturation -> cooling tail (plan A, synthetic burner, roadsense stopped), then the
    production load (plan B, roadsense running) at a few fan settings;
  * safety: any die zone >= 98 C or battery zone >= 45 C -> load off, fan 100 %, cool, exit.

The fan is driven ONLY through the backend API (set_fan / fan_auto); the powerbox serial
port is never touched.  On exit (normal, abort, SIGTERM) the controller is returned to
automatic mode and roadsense to its ACC-following mode.

Usage:  python3 thermal_campaign.py <out_dir> [A] [B]
"""
import csv
import glob
import subprocess
import json
import math
import os
import signal
import sys
import threading
import time
import urllib.request
from collections import deque

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
import multiprocessing as mp  # noqa: E402

API = "http://127.0.0.1:8080/api/v1"
THERMAL = "/sys/class/thermal"

# ── protocol parameters (seconds / degC) ────────────────────────────────────
PLAN_A_FANS = [100, 80, 60, 40, 20, 0]      # synthetic CPU load, descending
PLAN_B_FANS = [0, 40, 70, 100]              # roadsense production load
IDLE_MIN, IDLE_MAX = 180, 420               # idle baseline: stop when flat, cap
LOAD_MIN, LOAD_MAX = 300, 900               # full load: stop at saturation, cap 15 min
COOL_MIN, COOL_MAX = 120, 900               # cooling tail: stop near baseline, cap 15 min
SMOOTH_S = 60                               # moving-average window for the control series
SAT_WINDOW, SAT_DELTA = 180, 1.0            # saturation: rise over 3 min < 1 C
IDLE_WINDOW, IDLE_DELTA = 120, 0.7          # idle flat: change over 2 min < 0.7 C
COOL_BASELINE_MARGIN = 2.0                  # cooling done: within 2 C of the idle baseline
COOL_SLOPE_WINDOW, COOL_SLOPE_DELTA = 180, 0.5
ABORT_DIE_C, ABORT_BATT_C = 98.0, 45.0
ABORT_COOL_TO_C = 75.0

# Plan P (pulse series): fixed-length load pulses at one fan level, each followed
# by a cool-down to baseline; the mass behind the die is "saturated" when a
# longer pulse no longer changes the cooling curve.  Moderate, constant load:
# 3 little cores + GPU, below the throttling point.
PLAN_P_FANS = [100, 80, 60, 40, 20, 0]
PULSES_S = [60, 120, 240, 480]
LONG_PULSE_S = 900
LONG_PULSE_FANS = (100, 0)
PULSE_CPUS = (0, 1, 2)
P_IDLE_MIN, P_IDLE_MAX = 120, 300
P_COOL_MIN, P_COOL_MAX = 120, 600
P_COOL_MARGIN = 1.5

if "THERMAL_LONG_PULSE_FANS" in os.environ:
    LONG_PULSE_FANS = tuple(int(x) for x in os.environ["THERMAL_LONG_PULSE_FANS"].split(",") if x)
P_IDLE_MAX = int(os.environ.get("THERMAL_P_IDLE_MAX", P_IDLE_MAX))
P_COOL_MAX = int(os.environ.get("THERMAL_P_COOL_MAX", P_COOL_MAX))
B_LOAD_MIN, B_TAIL_S = 480, 180
V_FULL_S, V_LOW_S, V_COOL_S = 1500, int(os.environ.get("THERMAL_V_LOW_S", 1200)), 480
if os.environ.get("THERMAL_FANS_P"):
    PLAN_P_FANS = [int(x) for x in os.environ["THERMAL_FANS_P"].split(",")]
if os.environ.get("THERMAL_PULSES"):
    PULSES_S = [int(x) for x in os.environ["THERMAL_PULSES"].split(",")]
if os.environ.get("THERMAL_FANS_A"):        # e.g. "100,80"
    PLAN_A_FANS = [int(x) for x in os.environ["THERMAL_FANS_A"].split(",")]
if os.environ.get("THERMAL_FANS_B"):
    PLAN_B_FANS = [int(x) for x in os.environ["THERMAL_FANS_B"].split(",")]

if os.environ.get("THERMAL_QUICK"):      # smoke test: tiny timings, one fan level
    PLAN_A_FANS, PLAN_B_FANS, PLAN_P_FANS = [100], [100], [100]
    PULSES_S, LONG_PULSE_S, LONG_PULSE_FANS = [10, 20], 30, (100,)
    P_IDLE_MIN, P_IDLE_MAX, P_COOL_MIN, P_COOL_MAX = 10, 20, 10, 25
    IDLE_MIN, IDLE_MAX = 15, 30
    LOAD_MIN, LOAD_MAX = 20, 45
    COOL_MIN, COOL_MAX = 15, 30
    SMOOTH_S, SAT_WINDOW, IDLE_WINDOW, COOL_SLOPE_WINDOW = 10, 15, 15, 15

DIE_PREFIXES = ("cpu", "cluster", "gpu")
CPU_PREFIXES = ("cpu", "cluster")
GPU_PREFIXES = ("gpu",)
BATT_ZONE = "qcom-battery"


def log(msg):
    sys.stdout.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stdout.flush()


# ── sysfs helpers ────────────────────────────────────────────────────────────
def _read(path, default=None):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


def _read_num(path):
    v = _read(path)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def read_dcvsh():
    """Cumulative LMh (hardware limiter) interrupt counts for the two clusters."""
    a = b = float("nan")
    try:
        with open("/proc/interrupts") as fh:
            for line in fh:
                if "dcvsh-irq" in line:
                    n = sum(int(x) for x in line.split()[1:9] if x.isdigit())
                    if line.rstrip().endswith("irq-0"):
                        a = n
                    elif line.rstrip().endswith("irq-4"):
                        b = n
    except OSError:
        pass
    return a, b


def discover_zones():
    zones = {}
    for z in glob.glob(THERMAL + "/thermal_zone*"):
        t = _read(z + "/type")
        if t:
            zones[t] = z + "/temp"
    return dict(sorted(zones.items()))


def discover_cdevs():
    out = {}
    for c in glob.glob(THERMAL + "/cooling_device*"):
        t = _read(c + "/type")
        if t in ("cpufreq-cpu0", "cpufreq-cpu4", "devfreq-5000000.gpu"):
            out[t] = c + "/cur_state"
    return out


# ── backend API ──────────────────────────────────────────────────────────────
def api_state(timeout=1.5):
    with urllib.request.urlopen(API + "/state", timeout=timeout) as r:
        return json.load(r)["state"]


def api_cmd(name, params=None, timeout=3.0):
    req = urllib.request.Request(
        API + "/commands/" + name, data=json.dumps(params or {}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def set_fan(pct):
    for _ in range(3):
        try:
            api_cmd("set_fan", {"pct": float(pct)})
            log("fan override -> %s %%" % pct)
            return True
        except Exception as exc:  # noqa: BLE001
            log("set_fan failed: %s" % exc)
            time.sleep(2)
    return False


def fan_auto():
    for _ in range(5):
        try:
            api_cmd("fan_auto")
            log("fan -> automatic")
            return True
        except Exception as exc:  # noqa: BLE001
            log("fan_auto failed: %s" % exc)
            time.sleep(2)
    return False


def roadsense_mode(mode):
    for _ in range(3):
        try:
            api_cmd("roadsense_mode", {"mode": mode})
            log("roadsense mode -> %s" % mode)
            return True
        except Exception as exc:  # noqa: BLE001
            log("roadsense_mode failed: %s" % exc)
            time.sleep(2)
    return False


# ── burner ───────────────────────────────────────────────────────────────────
def _burn(cpu, stop):
    try:
        os.sched_setaffinity(0, {cpu})
    except OSError:
        pass
    import numpy as np
    rng = np.random.default_rng(cpu)
    a = rng.random((128, 128), dtype=np.float32)
    b = rng.random((128, 128), dtype=np.float32)
    while not stop.is_set():
        for _ in range(40):
            a = (a @ b) * np.float32(1e-3) + b


# GPU load: ncnn's benchncnn on the Turnip/Vulkan device (gpu_device 0, 2 host
# threads, no cooling-down pauses), restarted in a loop so the GPU never idles.
GPU_BENCH_DIR = "/home/user/vulkan-work/ncnn/benchmark"
GPU_BENCH_CMD = ("while :; do ../build/benchmark/benchncnn 500 2 0 0 0 >/dev/null 2>&1; done")


class Burner:
    """CPU burner (one pinned NumPy matmul process per allowed CPU) + GPU burner
    (benchncnn Vulkan loop in its own process group)."""

    def __init__(self, gpu=True, cpus=None):
        self.ctx = mp.get_context("fork")
        self.stop = self.ctx.Event()
        self.procs = []
        self.gpu = gpu
        self.gpu_proc = None
        allowed = os.sched_getaffinity(0)
        self.cpus = sorted(allowed if cpus is None else (set(cpus) & allowed))

    def start(self):
        self.stop.clear()
        self.procs = [self.ctx.Process(target=_burn, args=(c, self.stop), daemon=True)
                      for c in self.cpus]
        for p in self.procs:
            p.start()
        if self.gpu:
            self.gpu_proc = subprocess.Popen(["sh", "-c", GPU_BENCH_CMD], cwd=GPU_BENCH_DIR,
                                             start_new_session=True,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("burner started on cpus %s, gpu=%s" % (self.cpus, self.gpu))

    def halt(self):
        if self.gpu_proc is not None:
            try:
                os.killpg(self.gpu_proc.pid, signal.SIGKILL)
            except OSError:
                pass
            self.gpu_proc.wait(5)
            self.gpu_proc = None
        if not self.procs:
            return
        self.stop.set()
        for p in self.procs:
            p.join(5)
            if p.is_alive():
                p.terminate()
        self.procs = []
        log("burner stopped")


# ── sampler ──────────────────────────────────────────────────────────────────
class Sampler(threading.Thread):
    def __init__(self, csv_path):
        super().__init__(daemon=True)
        self.zones = discover_zones()
        self.cdevs = discover_cdevs()
        self.phase = "init"
        self.run_id = ""
        self.fan_set = float("nan")
        self.lock = threading.Lock()
        self.hist = deque(maxlen=COOL_MAX + 60)   # (ts, cpu_max, gpu_max, p_w, mem)
        self.last = None
        self.api_fail = 0
        self._stop = threading.Event()
        self.fields = (["ts", "phase", "run", "fan_set"] + list(self.zones)
                       + ["f_cpu0_khz", "f_cpu4_khz", "f_gpu_hz", "cd_cpu0", "cd_cpu4", "cd_gpu",
                          "cpus_online", "v_sup", "i_sup", "p_w", "load1",
                          "fan_duty", "fan_rpm", "fan_override", "bmp2_t", "bmp_t", "aht_t",
                          "acc_on", "power_mode", "rs_state", "box_power_w",
                          "f_hw_cpu0_khz", "f_hw_cpu4_khz", "dcvsh0", "dcvsh4",
                          "road_fps", "camera_fps", "signs_fps", "model_ms", "detect_ms"])
        self.fh = open(csv_path, "a", newline="")
        self.w = csv.writer(self.fh)
        if self.fh.tell() == 0:
            self.w.writerow(self.fields)

    def stop(self):
        self._stop.set()

    def sample(self):
        row = {"ts": round(time.time(), 2), "phase": self.phase, "run": self.run_id,
               "fan_set": self.fan_set}
        cpu_max = gpu_max = -1.0
        for name, path in self.zones.items():
            t = _read_num(path) / 1000.0
            row[name] = round(t, 2)
            if not math.isnan(t):
                if name.startswith(CPU_PREFIXES):
                    cpu_max = max(cpu_max, t)
                elif name.startswith(GPU_PREFIXES):
                    gpu_max = max(gpu_max, t)
        row["f_cpu0_khz"] = _read_num("/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq")
        row["f_cpu4_khz"] = _read_num("/sys/devices/system/cpu/cpufreq/policy4/scaling_cur_freq")
        row["f_gpu_hz"] = _read_num("/sys/class/devfreq/5000000.gpu/cur_freq")
        row["cd_cpu0"] = _read_num(self.cdevs.get("cpufreq-cpu0", "/nonexistent"))
        row["cd_cpu4"] = _read_num(self.cdevs.get("cpufreq-cpu4", "/nonexistent"))
        row["cd_gpu"] = _read_num(self.cdevs.get("devfreq-5000000.gpu", "/nonexistent"))
        row["cpus_online"] = _read("/sys/devices/system/cpu/online", "?")
        v = _read_num("/sys/class/power_supply/qcom-battery/voltage_now") / 1e6
        i = _read_num("/sys/class/power_supply/qcom-battery/current_now") / 1e6
        row["v_sup"], row["i_sup"] = round(v, 4), round(i, 4)
        p_w = abs(v * i)
        row["p_w"] = round(p_w, 3)
        try:
            row["load1"] = float(_read("/proc/loadavg", "nan nan").split()[0])
        except ValueError:
            row["load1"] = float("nan")
        st = None
        try:
            st = api_state()
        except Exception:  # noqa: BLE001
            self.api_fail += 1
        pb = (st or {}).get("powerbox", {}) or {}
        rs = (st or {}).get("roadsense", {}) or {}
        row["fan_duty"] = pb.get("fan_duty_pct")
        row["fan_rpm"] = pb.get("fan_rpm")
        row["fan_override"] = pb.get("fan_override_pct")
        row["bmp2_t"] = pb.get("bmp2_t")
        row["bmp_t"] = pb.get("bmp_t")
        row["aht_t"] = pb.get("aht_t")
        row["acc_on"] = pb.get("acc_on")
        row["power_mode"] = pb.get("power_mode")
        row["rs_state"] = rs.get("state")
        row["box_power_w"] = pb.get("power_draw_w")
        row["f_hw_cpu0_khz"] = _read_num("/sys/devices/system/cpu/cpufreq/policy0/cpuinfo_cur_freq")
        row["f_hw_cpu4_khz"] = _read_num("/sys/devices/system/cpu/cpufreq/policy4/cpuinfo_cur_freq")
        row["dcvsh0"], row["dcvsh4"] = read_dcvsh()
        h = rs.get("health") or {}
        fps = h.get("fps") or {}
        row["road_fps"], row["camera_fps"], row["signs_fps"] = fps.get("road"), fps.get("camera"), fps.get("signs")
        row["model_ms"], row["detect_ms"] = h.get("model_ms"), h.get("detect_ms")
        row["batt_zone"] = row.get(BATT_ZONE, float("nan"))
        row["cpu_max"], row["gpu_max"] = cpu_max, gpu_max
        return row

    def run(self):  # noqa: A003
        next_t = time.time()
        while not self._stop.is_set():
            try:
                row = self.sample()
                self.w.writerow([row.get(f) for f in self.fields])
                self.fh.flush()
                with self.lock:
                    self.last = row
                    self.hist.append((row["ts"], row["cpu_max"], row["gpu_max"], row["p_w"], row.get("mem-thermal", float("nan"))))
            except Exception as exc:  # noqa: BLE001
                log("sampler error: %r" % exc)
            next_t += 1.0
            time.sleep(max(0.05, next_t - time.time()))
        self.fh.close()

    # control-series helpers (smoothed max CPU temperature)
    def smoothed(self, at_offset=0, window=SMOOTH_S, key="cpu"):
        """Mean cpu_max (or mem-thermal) over [now-at_offset-window, now-at_offset]."""
        with self.lock:
            h = list(self.hist)
        if not h:
            return float("nan")
        now = h[-1][0]
        hi, lo = now - at_offset, now - at_offset - window
        idx = 1 if key == "cpu" else 4
        vals = [r[idx] for r in h if lo <= r[0] <= hi and r[idx] >= 0 and not math.isnan(r[idx])]
        return sum(vals) / len(vals) if vals else float("nan")

    def slope_ok(self, window, delta, key):
        a, b = self.smoothed(0, key=key), self.smoothed(window, key=key)
        return not math.isnan(a) and not math.isnan(b) and abs(a - b) < delta

    def mean_power(self, window):
        with self.lock:
            h = list(self.hist)
        if not h:
            return float("nan")
        now = h[-1][0]
        vals = [r[3] for r in h if r[0] >= now - window and not math.isnan(r[3])]
        return sum(vals) / len(vals) if vals else float("nan")


# ── protocol ─────────────────────────────────────────────────────────────────
class Campaign:
    def __init__(self, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        self.out = out_dir
        self.sampler = Sampler(os.path.join(out_dir, "samples.csv"))
        self.phases_path = os.path.join(out_dir, "phases.jsonl")
        self.burner = Burner()
        self.aborted = False
        self.rs_mode_touched = False
        self._term = False

    def note_phase(self, rec):
        with open(self.phases_path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def die_any_max(self):
        last = self.sampler.last
        if not last:
            return float("nan"), float("nan")
        return max(last["cpu_max"], last["gpu_max"]), last["batt_zone"]

    def check_safety(self):
        die, batt = self.die_any_max()
        if not math.isnan(die) and die >= ABORT_DIE_C:
            log("SAFETY ABORT: die=%.1f batt=%.1f" % (die, batt))
            self.abort()
            return True
        return False

    def abort(self):
        self.aborted = True
        self.burner.halt()
        set_fan(100)
        if self.rs_mode_touched:
            roadsense_mode("off")
        self.sampler.phase = "abort"
        t0 = time.time()
        while time.time() - t0 < 900:
            die, _ = self.die_any_max()
            if not math.isnan(die) and die < ABORT_COOL_TO_C:
                break
            time.sleep(2)
        raise SystemExit(3)

    def wait_phase(self, name, run, fan, done, t_min, t_max, extra=None):
        """Block in `name` until done(t) after t_min, or t_max; returns the record."""
        self.sampler.phase, self.sampler.run_id, self.sampler.fan_set = name, run, fan
        t0 = time.time()
        rec = {"phase": name, "run": run, "fan": fan, "t_start": round(t0, 1), "tainted": []}
        if extra:
            rec.update(extra)
        reason = "cap"
        while True:
            time.sleep(1.0)
            if self._term:
                reason = "sigterm"
                break
            die, _ = self.die_any_max()
            if not math.isnan(die) and die >= ABORT_DIE_C:
                rec["t_end"] = round(time.time(), 1)
                rec["duration_s"] = round(rec["t_end"] - t0, 1)
                rec["end_reason"] = "abort"
                self.note_phase(rec)
                self.check_safety()
            last = self.sampler.last
            if last:
                if last.get("acc_on") is False and "acc_off" not in rec["tainted"]:
                    rec["tainted"].append("acc_off")
                    log("note: ACC went off during %s/%s" % (run, name))
                if last.get("power_mode") not in (None, "full") and "power_mode_low" not in rec["tainted"]:
                    rec["tainted"].append("power_mode_low")
                if fan is not None and last.get("fan_override") is None and "override_lost" not in rec["tainted"]:
                    rec["tainted"].append("override_lost")
                    log("note: fan override lost (backend restart?) -- re-pinning")
                    set_fan(fan)
            el = time.time() - t0
            if el >= t_max:
                reason = "cap"
                break
            if el >= t_min and done(el):
                reason = "criterion"
                break
        rec["t_end"] = round(time.time(), 1)
        rec["duration_s"] = round(rec["t_end"] - t0, 1)
        rec["end_reason"] = reason
        rec["cpu_smoothed_end"] = round(self.sampler.smoothed(), 2)
        rec["p_w_last60"] = round(self.sampler.mean_power(60), 3)
        last = self.sampler.last or {}
        rec["fan_rpm_end"] = last.get("fan_rpm")
        rec["bmp2_t_end"] = last.get("bmp2_t")
        rec["gpu_max_end"] = last.get("gpu_max")
        rec["cpus_online"] = last.get("cpus_online")
        rec["power_mode"] = last.get("power_mode")
        self.note_phase(rec)
        log("phase %s/%s done: %s after %.0f s, Tcpu~%.1f, P~%.2f W" %
            (run, name, reason, rec["duration_s"], rec["cpu_smoothed_end"], rec["p_w_last60"]))
        if reason == "sigterm":
            raise SystemExit(4)          # cleanup() runs in the caller's finally
        return rec

    # criteria
    def _flat(self, window, delta):
        def f(_el):
            return (self.sampler.slope_ok(window, delta, "cpu")
                    and self.sampler.slope_ok(window, delta * 0.5, "mem"))
        return f

    def _saturated(self, _el):
        return (self.sampler.slope_ok(SAT_WINDOW, SAT_DELTA, "cpu")
                and self.sampler.slope_ok(SAT_WINDOW, SAT_DELTA * 0.5, "mem"))

    def _cooled(self, baseline):
        def f(_el):
            a = self.sampler.smoothed(0)
            if math.isnan(a):
                return False
            if not math.isnan(baseline) and a <= baseline + COOL_BASELINE_MARGIN \
                    and self.sampler.slope_ok(COOL_SLOPE_WINDOW, COOL_SLOPE_DELTA, "mem"):
                return True
            return (_el >= 300 and self.sampler.slope_ok(COOL_SLOPE_WINDOW, COOL_SLOPE_DELTA, "cpu")
                    and self.sampler.slope_ok(COOL_SLOPE_WINDOW, COOL_SLOPE_DELTA * 0.5, "mem"))
        return f

    def plan_a(self):
        log("PLAN A: synthetic CPU+GPU load (numpy burner + benchncnn vulkan), roadsense off, fans %s" % PLAN_A_FANS)
        roadsense_mode("off")
        self.rs_mode_touched = True
        # let the stack stop (state goes off within ~60 s) and the die relax a bit
        self.sampler.phase, self.sampler.run_id = "rs_stopping", "A"
        t0 = time.time()
        while time.time() - t0 < 120:
            last = self.sampler.last
            if last and last.get("rs_state") == "off":
                break
            time.sleep(2)
        for fan in PLAN_A_FANS:
            run = "A%03d" % fan
            set_fan(fan)
            time.sleep(5)
            self.wait_phase("idle", run, fan, self._flat(IDLE_WINDOW, IDLE_DELTA), IDLE_MIN, IDLE_MAX)
            baseline = self.sampler.smoothed()
            p_idle = self.sampler.mean_power(60)
            self.burner.start()
            self.wait_phase("load", run, fan, self._saturated, LOAD_MIN, LOAD_MAX,
                            {"baseline_c": round(baseline, 2), "p_idle_w": round(p_idle, 3)})
            self.burner.halt()
            self.wait_phase("cool", run, fan, self._cooled(baseline), COOL_MIN, COOL_MAX,
                            {"baseline_c": round(baseline, 2)})

    def plan_p(self):
        log("PLAN P: pulse series %s s (+%s s at %s), load = cpus %s + benchncnn vulkan, fans %s"
            % (PULSES_S, LONG_PULSE_S, LONG_PULSE_FANS, PULSE_CPUS, PLAN_P_FANS))
        roadsense_mode("off")
        self.rs_mode_touched = True
        self.sampler.phase, self.sampler.run_id = "rs_stopping", "P"
        t0 = time.time()
        while time.time() - t0 < 120:
            last = self.sampler.last
            if last and last.get("rs_state") == "off":
                break
            time.sleep(2)
        burner = Burner(gpu=True, cpus=PULSE_CPUS)
        self.burner = burner
        for fan in PLAN_P_FANS:
            run = "P%03d" % fan
            set_fan(fan)
            time.sleep(5)
            self.wait_phase("idle", run, fan, self._flat(IDLE_WINDOW, IDLE_DELTA), P_IDLE_MIN, P_IDLE_MAX)
            baseline = self.sampler.smoothed()
            base_mem = self.sampler.smoothed(key="mem")
            p_idle = self.sampler.mean_power(60)
            pulses = list(PULSES_S) + ([LONG_PULSE_S] if fan in LONG_PULSE_FANS else [])
            for dur in pulses:
                burner.start()
                self.wait_phase("pulse%d" % dur, run, fan, lambda _el: False, dur, dur,
                                {"baseline_c": round(baseline, 2), "baseline_mem_c": round(base_mem, 2),
                                 "p_idle_w": round(p_idle, 3), "pulse_s": dur})
                burner.halt()
                self.wait_phase("cool%d" % dur, run, fan, self._cooled_to(baseline, base_mem),
                                P_COOL_MIN, P_COOL_MAX,
                                {"baseline_c": round(baseline, 2), "baseline_mem_c": round(base_mem, 2),
                                 "pulse_s": dur})

    def _cooled_to(self, baseline, base_mem):
        def f(_el):
            a = self.sampler.smoothed(0)
            m = self.sampler.smoothed(0, key="mem")
            if math.isnan(a) or math.isnan(m):
                return False
            if a <= baseline + P_COOL_MARGIN and m <= base_mem + P_COOL_MARGIN:
                return True
            return (_el >= 300 and self.sampler.slope_ok(COOL_SLOPE_WINDOW, COOL_SLOPE_DELTA, "cpu")
                    and self.sampler.slope_ok(COOL_SLOPE_WINDOW, COOL_SLOPE_DELTA * 0.5, "mem"))
        return f

    def plan_v(self):
        """Live verification with the fan in automatic mode: production load in
        the `full` profile, then the worst-case synthetic load in the real `low`
        profile. The fan is never overridden."""
        log("PLAN V: live verification, fan automatic")
        fan_auto()
        self.rs_mode_touched = True
        roadsense_mode("on")
        self.sampler.phase, self.sampler.run_id, self.sampler.fan_set = "rs_starting", "Vfull", float("nan")
        t0 = time.time()
        while time.time() - t0 < 180:
            last = self.sampler.last
            if last and last.get("rs_state") == "running":
                break
            time.sleep(2)
        never = lambda _el: False
        self.wait_phase("load", "Vfull", None, never, V_FULL_S, V_FULL_S)
        roadsense_mode("off")
        self.wait_phase("cool", "Vfull", None, never, V_COOL_S, V_COOL_S)
        self.low_cycle("Vlow", V_LOW_S)

    def plan_w(self):
        """`low` profile engage/release on real hardware (thresholds lowered by CLI
        flags for this run only: the bench cannot reach the real ones)."""
        log("PLAN W: low profile with lowered thresholds, fan automatic")
        fan_auto()
        self.rs_mode_touched = True
        roadsense_mode("off")
        self.low_cycle("Wlow", V_LOW_S)

    def low_cycle(self, run, load_s):
        never = lambda _el: False
        self.burner = Burner(gpu=True, cpus=PULSE_CPUS)
        self.burner.start()
        self.wait_phase("load", run, None, never, load_s, load_s)
        self.burner.halt()
        self.wait_phase("cool", run, None, never, V_COOL_S, V_COOL_S)

    def plan_b(self):
        log("PLAN B: roadsense production load, fans %s" % PLAN_B_FANS)
        self.rs_mode_touched = True
        for fan in PLAN_B_FANS:
            run = "B%03d" % fan
            set_fan(fan)
            roadsense_mode("on")
            self.sampler.phase, self.sampler.run_id, self.sampler.fan_set = "rs_starting", run, fan
            t0 = time.time()
            while time.time() - t0 < 180:
                last = self.sampler.last
                if last and last.get("rs_state") == "running":
                    break
                time.sleep(2)
            self.wait_phase("rs_load", run, fan, self._saturated, B_LOAD_MIN, LOAD_MAX)
            # Sink read-out: stack off, same fan; die 15-20 s later = sink temperature.
            roadsense_mode("off")
            self.wait_phase("rs_tail", run, fan, lambda _el: False, B_TAIL_S, B_TAIL_S)

    def cleanup(self):
        self.burner.halt()
        if self.rs_mode_touched:
            roadsense_mode("auto")
        fan_auto()
        self.sampler.phase = "done"
        time.sleep(3)
        self.sampler.stop()

    def run(self, plans):
        def _sig(*_a):
            self._term = True
        signal.signal(signal.SIGTERM, _sig)
        signal.signal(signal.SIGINT, _sig)
        self.sampler.start()
        time.sleep(3)
        log("zones: %s" % list(self.sampler.zones))
        log("cdevs: %s" % list(self.sampler.cdevs))
        try:
            if "A" in plans and not self._term:
                self.plan_a()
            if "P" in plans and not self._term:
                self.plan_p()
            if "B" in plans and not self._term:
                self.plan_b()
            if "V" in plans and not self._term:
                self.plan_v()
            if "W" in plans and not self._term:
                self.plan_w()
        finally:
            self.cleanup()
            log("campaign finished (aborted=%s, term=%s, api_fail=%d)" %
                (self.aborted, self._term, self.sampler.api_fail))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    Campaign(sys.argv[1]).run(sys.argv[2:] or ["P", "B"])
