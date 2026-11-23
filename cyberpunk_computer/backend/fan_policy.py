"""
Chassis fan policy for the POCO F1 car computer (pure, hardware-free).

Every number here comes from the bench campaign in docs/THERMAL_MODEL.md. What
the campaign showed, and how it shapes this controller:

* The CPU zones under the production load peak at 95-97 °C at every fan speed,
  100 % included: they follow the instantaneous core power through a fixed
  internal resistance the fan cannot change. They do not drive the GPU-led
  ramp; the hottest core drives the hot-core term below and the override.
* The GPU zone follows the sink the fan does control, and it is the only zone
  that actually throttled (kernel passive trip at 85 °C, road rate 13.5 -> 11
  runs/s). It is the control variable, low-pass filtered.
* Airflow has sharply diminishing returns: 40 -> 100 % buys 2.4 K for 2250 RPM.
  The ramp therefore settles at the lowest speed that keeps the GPU clear.
* Hot-core term (2026-11-17, owner): on the road the hottest raw CPU core cycled
  89-96 °C with the fan at 28-50 % (roadsense pauses itself above 92 °C) and the
  phone later hard-reset. Staying below ~88 °C on the hottest core now ranks
  above quietness: the hottest core, low-passed at 5 s, ramps the duty from the
  normal target at 84 °C to 100 % at 90 °C, reached within seconds (fast slew),
  and holds a floor until it falls below 80 °C. It may exceed the ``low``
  ceiling. Cool cores leave the quiet GPU-led behaviour untouched.

Two profiles, selected by the POCO power mode: ``full`` (ACC on, roadsense
running) holds the GPU below its trip at the least noise; ``low`` (key off,
Gold cluster offline) stays passive unless the temperature saturates high, and
then runs quietly with a wide hysteresis. An independent controller purges the
box when its air is much hotter than the cabin; the duty is the max of both.
Manual override (``set_fan``) bypasses everything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional


@dataclass(frozen=True)
class FanProfile:
    """One operating profile (temperatures are the control temperature, °C)."""
    on_c: float          # engage above this
    off_c: float         # release below this
    full_c: float        # duty reaches max_pct here
    min_pct: float       # duty while just engaged
    max_pct: float       # duty ceiling
    min_delta_c: float   # engage only if control temp - ambient >= this


@dataclass(frozen=True)
class FanPolicyConfig:
    full: FanProfile = FanProfile(on_c=78.0, off_c=68.0, full_c=84.0, min_pct=20.0,
                                  max_pct=100.0, min_delta_c=10.0)
    low: FanProfile = FanProfile(on_c=70.0, off_c=58.0, full_c=80.0, min_pct=20.0,
                                 max_pct=40.0, min_delta_c=15.0)
    tau_s: float = 20.0           # low-pass of both zones (removes scene-driven jitter)
    # The CPU zones run about 15 K above the GPU under production load regardless
    # of the fan: subtracted so the GPU governs, while a CPU-only load still counts.
    cpu_offset_c: float = 15.0
    # Override: filtered control temperature past the GPU trip, or a raw reading
    # above anything the production load reached with the fan running.
    safety_ctrl_c: float = 86.0
    safety_release_c: float = 82.0
    safety_raw_c: float = 98.0
    # Hot-core term: the hottest raw CPU core (cpu_c is the max over all
    # cpu*/cluster* zones), low-passed at hot_tau_s. Engages above hot_on_c,
    # ramps the duty linearly from the normal target at hot_on_c to 100 % at
    # hot_full_c, holds at least hot_floor_pct while engaged and releases below
    # hot_release_c. Rises at hot_slew_up_pct_s (no deadband).
    hot_on_c: float = 84.0
    hot_full_c: float = 90.0
    hot_release_c: float = 80.0
    hot_floor_pct: float = 40.0
    hot_tau_s: float = 5.0
    hot_slew_up_pct_s: float = 30.0
    # Output shaping, so the fan is heard as a steady speed.
    slew_up_pct_s: float = 5.0
    slew_down_pct_s: float = 0.5
    deadband_pct: float = 4.0
    # Box purge (inside vs outside the box).
    box_start_delta: float = 8.0
    box_stop_delta: float = 5.0
    box_min_temp: float = 35.0
    box_stop_temp: float = 32.0
    box_max_pct: float = 60.0
    box_ramp_range: float = 10.0
    stall_pct: float = 15.0       # anything in (0, stall_pct) is raised to this


@dataclass
class FanDecision:
    duty_pct: float
    control_c: Optional[float]
    reason: str
    poco_pct: float = 0.0
    box_pct: float = 0.0
    hot_pct: float = 0.0


class ChassisFanPolicy:
    """Stateful controller: call :meth:`step` once per tick with the latest readings."""

    def __init__(self, cfg: Optional[FanPolicyConfig] = None) -> None:
        self.cfg = cfg or FanPolicyConfig()
        self._gpu_f: Optional[float] = None
        self._cpu_f: Optional[float] = None
        self._hot_f: Optional[float] = None
        self._last_t: Optional[float] = None
        self.control_c: Optional[float] = None
        self.active = False
        self.safety = False
        self.hot = False
        self.hot_c: Optional[float] = None
        self.box_active = False
        self.duty_pct = 0.0

    def _update_control(self, now: float, cpu_c: Optional[float], gpu_c: Optional[float]) -> Optional[float]:
        cfg = self.cfg
        dt = 0.0 if self._last_t is None else max(0.0, now - self._last_t)
        self._last_t = now
        alpha = 1.0 - math.exp(-dt / max(cfg.tau_s, 1e-3))
        if cpu_c is None:
            self._cpu_f = None
        else:
            self._cpu_f = cpu_c if self._cpu_f is None else self._cpu_f + alpha * (cpu_c - self._cpu_f)
        if cpu_c is None:
            self._hot_f = None
        else:
            a_hot = 1.0 - math.exp(-dt / max(cfg.hot_tau_s, 1e-3))
            self._hot_f = cpu_c if self._hot_f is None else self._hot_f + a_hot * (cpu_c - self._hot_f)
        self.hot_c = self._hot_f
        if gpu_c is None:
            self._gpu_f = None
        else:
            self._gpu_f = gpu_c if self._gpu_f is None else self._gpu_f + alpha * (gpu_c - self._gpu_f)
        vals = []
        if self._gpu_f is not None:
            vals.append(self._gpu_f)
        if self._cpu_f is not None:
            vals.append(self._cpu_f - cfg.cpu_offset_c)
        self.control_c = max(vals) if vals else None
        return self.control_c

    def _poco_target(self, ctrl: float, ambient_c: Optional[float], prof: FanProfile) -> float:
        delta = (ctrl - ambient_c) if ambient_c is not None else float("inf")
        if self.active:
            if ctrl < prof.off_c or delta < prof.min_delta_c * 0.5:
                self.active = False
        elif ctrl > prof.on_c and delta >= prof.min_delta_c:
            self.active = True
        if not self.active:
            return 0.0
        t = (ctrl - prof.on_c) / max(prof.full_c - prof.on_c, 0.1)
        return prof.min_pct + max(0.0, min(1.0, t)) * (prof.max_pct - prof.min_pct)

    def _hot_target(self, normal: float) -> float:
        """Hot-core term (0 when not engaged): from the normal target at
        hot_on_c to 100 % at hot_full_c, at least hot_floor_pct while engaged."""
        cfg = self.cfg
        hot = self._hot_f
        if hot is None:
            self.hot = False
            return 0.0
        if self.hot:
            if hot < cfg.hot_release_c:
                self.hot = False
        elif hot > cfg.hot_on_c:
            self.hot = True
        if not self.hot:
            return 0.0
        t = max(0.0, min(1.0, (hot - cfg.hot_on_c) / max(cfg.hot_full_c - cfg.hot_on_c, 0.1)))
        return max(cfg.hot_floor_pct, normal + t * (100.0 - normal))

    def _box_target(self, box_in_c: Optional[float], box_out_c: Optional[float]) -> float:
        cfg = self.cfg
        if box_in_c is None or box_out_c is None:
            self.box_active = False
            return 0.0
        d = box_in_c - box_out_c
        if self.box_active:
            if d < cfg.box_stop_delta or box_in_c < cfg.box_stop_temp:
                self.box_active = False
        elif d >= cfg.box_start_delta and box_in_c >= cfg.box_min_temp:
            self.box_active = True
        if not self.box_active:
            return 0.0
        t = (d - cfg.box_stop_delta) / max(cfg.box_ramp_range, 0.1)
        return max(0.0, min(cfg.box_max_pct, t * cfg.box_max_pct))

    def _shape(self, target: float, dt: float, prof_max: float, fast_up: bool = False) -> float:
        """Slew + deadband. Stopping, starting and reaching the ceiling are exact.
        ``fast_up``: the hot-core term leads; rise at hot_slew_up_pct_s, no deadband."""
        cfg = self.cfg
        cur = self.duty_pct
        if target <= 0.0:
            return 0.0
        if cur <= 0.0:
            return target
        if target > cur:
            if fast_up:
                return min(target, cur + max(cfg.hot_slew_up_pct_s, cfg.slew_up_pct_s) * dt)
            if target - cur < cfg.deadband_pct and target < prof_max:
                return cur
            return min(target, cur + cfg.slew_up_pct_s * dt)
        if cur - target < cfg.deadband_pct and cur <= prof_max:
            return cur
        return max(target, cur - cfg.slew_down_pct_s * dt)

    def step(
        self,
        now: float,
        *,
        cpu_c: Optional[float],
        gpu_c: Optional[float],
        ambient_c: Optional[float],
        box_in_c: Optional[float],
        box_out_c: Optional[float],
        power_mode: Optional[str],
        override_pct: Optional[float] = None,
    ) -> FanDecision:
        cfg = self.cfg
        last_t = self._last_t
        ctrl = self._update_control(now, cpu_c, gpu_c)
        dt = 0.0 if last_t is None else max(0.0, now - last_t)

        if override_pct is not None:
            self.duty_pct = max(0.0, min(100.0, float(override_pct)))
            return FanDecision(self.duty_pct, ctrl, "override")

        prof = cfg.full if power_mode == "full" else cfg.low
        name = "full" if power_mode == "full" else "low"
        poco = 0.0
        reason = "idle"
        if ctrl is None:
            self.active = False
            self.safety = False
        else:
            raw = max(v for v in (cpu_c, gpu_c) if v is not None)
            if self.safety:
                if ctrl < cfg.safety_release_c and raw < cfg.safety_raw_c:
                    self.safety = False
            elif ctrl >= cfg.safety_ctrl_c or raw >= cfg.safety_raw_c:
                self.safety = True
            if self.safety:
                self.active = True
                poco, reason = 100.0, "safety"
            else:
                poco = self._poco_target(ctrl, ambient_c, prof)
                if self.active:
                    reason = name
        hot = 0.0 if self.safety else self._hot_target(poco)
        if self.safety:
            self.hot = False
        box = self._box_target(box_in_c, box_out_c)
        target = max(poco, box, hot)
        if box > max(poco, hot):
            reason = "box"
        elif hot > poco:
            reason = "hot_core"
        if 0.0 < target < cfg.stall_pct:
            target = cfg.stall_pct
        if self.safety:
            self.duty_pct = 100.0
        else:
            # the hot-core term may exceed the profile ceiling (low: 40 %)
            ceiling = max(prof.max_pct, cfg.box_max_pct if box > 0 else 0.0,
                          hot)
            self.duty_pct = self._shape(target, dt, ceiling, fast_up=hot > 0.0)
        return FanDecision(self.duty_pct, ctrl, reason, poco, box, hot)


class SharedFan:
    """One fan for several phones on a common heatpipe.

    Both POCOs sit on one laptop-type heatpipe (two pads, one fan), so the fan
    has to satisfy whichever phone needs more. Every phone gets its own
    :class:`ChassisFanPolicy` (its own filters, hysteresis, hot-core term and
    override, all calibrated per phone on the same SoC) and the fan runs at
    the highest demand. The box purge is part of every policy and identical.

    ``readings`` maps host -> ``{"cpu_c", "gpu_c", "present", "fresh"}``:
    ``present`` = the phone is running, ``fresh`` = its temperatures are
    current. A running phone without fresh temperatures cannot be cooled by
    feedback: with the key on (``full``) the fan then holds ``stale_floor_pct``.
    """

    def __init__(self, cfg: Optional[FanPolicyConfig] = None,
                 hosts=("poco", "poco2"), stale_floor_pct: float = 30.0) -> None:
        self.cfg = cfg or FanPolicyConfig()
        self.hosts = tuple(hosts)
        self.policies: Dict[str, ChassisFanPolicy] = {h: ChassisFanPolicy(self.cfg) for h in self.hosts}
        self.stale_floor_pct = stale_floor_pct

    def step(self, now: float, readings: Dict[str, dict], *, ambient_c: Optional[float],
             box_in_c: Optional[float], box_out_c: Optional[float],
             power_mode: Optional[str], override_pct: Optional[float] = None):
        """Returns ``(duty_pct, decisions, detail)``: the fan duty, the
        :class:`FanDecision` of every host and a flat dict for the state."""
        decisions: Dict[str, FanDecision] = {}
        detail: dict = {}
        duty, source, reason = 0.0, "idle", "idle"
        for host in self.hosts:
            r = readings.get(host) or {}
            fresh = bool(r.get("present")) and bool(r.get("fresh"))
            d = self.policies[host].step(
                now, cpu_c=r.get("cpu_c") if fresh else None,
                gpu_c=r.get("gpu_c") if fresh else None,
                ambient_c=ambient_c, box_in_c=box_in_c, box_out_c=box_out_c,
                power_mode=power_mode, override_pct=override_pct)
            pct = d.duty_pct
            stale = bool(r.get("present")) and not r.get("fresh")
            if stale and override_pct is None and power_mode == "full" \
                    and pct < self.stale_floor_pct:
                pct, d = self.stale_floor_pct, FanDecision(
                    self.stale_floor_pct, d.control_c, "stale", d.poco_pct, d.box_pct, d.hot_pct)
            decisions[host] = d
            detail[host + "_pct"] = round(pct, 1)
            detail[host + "_ctrl_c"] = round(d.control_c, 1) if d.control_c is not None else None
            detail[host + "_reason"] = d.reason
            detail[host + "_stale"] = stale
            if pct > duty or (source == "idle" and d.reason != "idle"):
                duty, source, reason = max(duty, pct), host, d.reason
        if override_pct is not None:
            source = "override"
        elif reason == "box":
            source = "box"
        detail.update(duty_pct=round(duty, 1), source=source, reason=reason)
        return duty, decisions, detail


# Lumped plant used by the closed-loop tests and docs/THERMAL_MODEL.md.
@dataclass
class ThermalPlant:
    """die -R_int- sink -R_sink(fan)- ambient; the die node is the GPU zone."""
    t_amb: float
    c_die: float
    r_int: float
    c_sink: float
    r_sink: Callable[[float], float]
    t_die: float = field(default=float("nan"))
    t_sink: float = field(default=float("nan"))

    def __post_init__(self) -> None:
        if math.isnan(self.t_die):
            self.t_die = self.t_amb
        if math.isnan(self.t_sink):
            self.t_sink = self.t_amb

    def step(self, p_w: float, fan_pct: float, dt: float) -> None:
        h_max = 0.5 * self.c_die * self.r_int
        n = max(1, int(math.ceil(dt / max(h_max, 1e-3))))
        h = dt / n
        for _ in range(n):
            q = (self.t_die - self.t_sink) / self.r_int
            self.t_die += h * (p_w - q) / self.c_die
            self.t_sink += h * (q - (self.t_sink - self.t_amb) / self.r_sink(fan_pct)) / self.c_sink


def interp_table(points: Dict[float, float]) -> Callable[[float], float]:
    """Piecewise-linear interpolator from measured points, clamped at the ends."""
    xs = sorted(points)

    def f(x: float) -> float:
        x = max(xs[0], min(xs[-1], float(x)))
        for a, b in zip(xs, xs[1:]):
            if a <= x <= b:
                return points[a] + (x - a) / (b - a) * (points[b] - points[a])
        return points[xs[-1]]
    return f
