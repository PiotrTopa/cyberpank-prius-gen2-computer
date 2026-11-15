"""Synthetic NHW20 pack + 12 V battery: feeds a Collector with the same events
the car produces. Used by the tests (planted faults must be found) and for
demo data (``python -m cyberpunk_computer.battery synth``). Not a validated
battery model: first-order equivalent circuit per block, coulomb-counting ECU.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .analysis import arrhenius_factor
from .collector import Collector
from .model import N_BLOCKS, NOMINAL_AH

DAY = 86400.0


@dataclass
class SynthConfig:
    seed: int = 1
    start_ts: float = 1_780_000_000.0       # 2026-06
    days: int = 120
    rides_per_day: float = 2.0
    ride_s: float = 900.0
    r25_mohm: float = 20.0                  # block resistance at 25 degC (ohmic part)
    r_pol_frac: float = 0.5                 # polarisation resistance / ohmic
    tau_s: float = 4.0
    r_scatter: float = 0.03                 # block-to-block resistance scatter (fraction)
    pack_r_growth_per_year: float = 0.0     # whole-pack resistance growth (fraction / year)
    weak_block: Optional[int] = None        # 1-based block with a growing resistance
    weak_start_day: float = 0.0
    weak_r_growth_per_day: float = 0.004    # fraction of r25 per day after weak_start_day
    weak_capacity_frac: float = 1.0         # capacity of the weak block (fraction)
    leaky_block: Optional[int] = None       # 1-based block with extra self-discharge
    leak_soc_per_day: float = 0.25          # extra % SOC per day lost by the leaky block
    arrhenius_b: float = 2260.0             # temperature coefficient of the resistance (K)
    self_discharge_soc_per_day: float = 0.3
    capacity_ah: float = 6.0
    capacity_fade_per_year: float = 0.0
    temp_mean: float = 10.0
    temp_amp: float = 11.0                  # annual amplitude (degC)
    aux_ah: float = 35.0
    aux_fade_per_year: float = 0.0
    aux_drain_ma: float = 120.0             # computer (measured by the powerbox)
    aux_hidden_ma: float = 25.0             # car quiescent (not measured)
    poll_period_s: float = 2.0
    pack_hz: float = 4.0
    full_21cf: bool = True


def _ocv(soc: float) -> float:
    """Block open-circuit voltage (12 cells) against SOC in %."""
    return 16.2 + 0.012 * (soc - 55.0) + 0.25 * math.tanh((soc - 55.0) / 30.0)


def _aux_ocv(soc: float) -> float:
    return 11.6 + 0.0115 * max(0.0, min(100.0, soc))


class SynthCar:
    def __init__(self, cfg: SynthConfig, collector: Collector) -> None:
        self.cfg = cfg
        self.c = collector
        self.rng = random.Random(cfg.seed)
        self.r_base = [cfg.r25_mohm / 1000.0 * (1 + self.rng.gauss(0, cfg.r_scatter)) for _ in range(N_BLOCKS)]
        self.soc = [58.0 + self.rng.gauss(0, 0.4) for _ in range(N_BLOCKS)]
        self.vp = [0.0] * N_BLOCKS
        self.soc_ecu = 58.0
        self.t_batt = cfg.temp_mean
        self.aux_soc = 85.0
        self.t = cfg.start_ts
        self.mah = 0.0

    # ── environment ──────────────────────────────────────────────────────────

    def day(self) -> float:
        return (self.t - self.cfg.start_ts) / DAY

    def ambient(self) -> float:
        cfg = self.cfg
        yearly = cfg.temp_amp * math.sin(2 * math.pi * (self.t / DAY - 110) / 365.25)
        daily = 3.0 * math.sin(2 * math.pi * ((self.t % DAY) / DAY - 0.3))
        return cfg.temp_mean + yearly + daily

    def r25(self, k: int) -> float:
        cfg = self.cfg
        d = self.day()
        r = self.r_base[k] * (1 + cfg.pack_r_growth_per_year * d / 365.25)
        if cfg.weak_block == k + 1 and d > cfg.weak_start_day:
            r *= 1 + cfg.weak_r_growth_per_day * (d - cfg.weak_start_day)
        return r

    def cap(self, k: int) -> float:
        cfg = self.cfg
        c = cfg.capacity_ah * (1 - cfg.capacity_fade_per_year * self.day() / 365.25)
        if cfg.weak_block == k + 1:
            c *= cfg.weak_capacity_frac
        return c

    # ── rest ─────────────────────────────────────────────────────────────────

    def rest(self, seconds: float) -> None:
        cfg, c = self.cfg, self.c
        days = seconds / DAY
        for k in range(N_BLOCKS):
            rate = cfg.self_discharge_soc_per_day + (cfg.leak_soc_per_day if cfg.leaky_block == k + 1 else 0.0)
            self.soc[k] -= rate * days
            self.vp[k] = 0.0
        self.soc_ecu -= cfg.self_discharge_soc_per_day * days
        aux_cap = cfg.aux_ah * (1 - cfg.aux_fade_per_year * self.day() / 365.25)
        end = self.t + seconds
        c.acc(self.t, False)
        while self.t < end:
            amb = self.ambient()
            self.t_batt += (amb - self.t_batt) * min(1.0, 60.0 / 7200.0)
            ma = cfg.aux_drain_ma * (1 + self.rng.gauss(0, 0.03))
            self.aux_soc -= (ma + cfg.aux_hidden_ma) / 1000.0 / 60.0 / aux_cap * 100.0
            self.aux_soc = max(0.0, self.aux_soc)
            v = _aux_ocv(self.aux_soc) - ma / 1000.0 * 0.08 - 0.004 * (20 - amb) + self.rng.gauss(0, 0.004)
            self.mah += ma / 60.0
            c.powerbox(self.t, round(v, 3), round(ma, 1), round(self.mah, 2), round(amb, 2))
            self.t += 60.0
        self.t = end

    # ── ride ─────────────────────────────────────────────────────────────────

    def ride(self, seconds: float) -> None:
        cfg, c, rng = self.cfg, self.c, self.rng
        dt = 1.0 / cfg.pack_hz
        t0 = self.t
        c.acc(self.t, True)
        amb = self.ambient()
        seg_end = 0.0
        i_target = 2.0
        cur = 2.0
        next_poll = 1.0
        next_slow = 0.0
        speed = 0.0
        aux_cap = cfg.aux_ah
        n = int(seconds / dt)
        for step in range(n):
            el = step * dt
            self.t = t0 + el
            if el >= seg_end:
                mean_soc = sum(self.soc) / N_BLOCKS
                bias = (58.0 - mean_soc) * 2.0          # controller pulls the SOC back
                kind = rng.random()
                if el < 6 or el > seconds - 8:
                    i_target, seg = 2.5, 6.0
                elif kind < 0.28:
                    i_target, seg = rng.uniform(25, 95) - bias, rng.uniform(3, 9)
                elif kind < 0.55:
                    i_target, seg = -rng.uniform(20, 75) - bias, rng.uniform(3, 9)
                elif kind < 0.85:
                    i_target, seg = rng.uniform(-12, 12) - bias, rng.uniform(5, 20)
                else:
                    i_target, seg = 2.0, rng.uniform(4, 15)
                seg_end = el + seg
                speed = max(0.0, min(110.0, speed + rng.uniform(-25, 25))) if abs(i_target) > 5 else speed * 0.6
            cur += (i_target - cur) * min(1.0, dt / 0.4)
            i = cur + rng.gauss(0, 0.3)
            fac = arrhenius_factor(self.t_batt, cfg.arrhenius_b)
            vs = []
            for k in range(N_BLOCKS):
                r0 = self.r25(k) * fac
                rp = r0 * cfg.r_pol_frac
                self.vp[k] += (i * rp - self.vp[k]) * min(1.0, dt / cfg.tau_s)
                self.soc[k] -= i * dt / 3600.0 / self.cap(k) * 100.0
                self.soc[k] = max(5.0, min(95.0, self.soc[k]))
                vs.append(_ocv(self.soc[k]) - i * r0 - self.vp[k])
            self.soc_ecu -= i * dt / 3600.0 / NOMINAL_AH * 100.0
            self.soc_ecu += (sum(self.soc) / N_BLOCKS - self.soc_ecu) * dt / 120.0
            heat = i * i * sum(self.r25(k) for k in range(N_BLOCKS)) * fac * 1.5
            self.t_batt += heat * dt / 30000.0 - (self.t_batt - amb) * dt / 4000.0
            pack_v = sum(vs)
            c.pack(self.t, float(round(pack_v)), round(i, 1))
            if step % int(cfg.pack_hz) == 0:
                lim = max(20.0, min(105.0, 60.0 + 2.4 * self.t_batt))
                c.ecu(self.t, round(self.soc_ecu * 2) / 2.0, round(lim), round(lim),
                      round(self.t_batt - 0.4), round(self.t_batt + 0.4))
                c.ready(self.t, el > 4)
                c.speed(self.t, speed)
                self.aux_soc += (97.0 - self.aux_soc) / 240.0      # DC-DC recharge
                c.powerbox(self.t, round(13.85 + rng.gauss(0, 0.01), 3), 900.0, round(self.mah, 2), round(amb + 4, 2))
            if el >= next_poll:
                next_poll += cfg.poll_period_s
                c.blocks(self.t, round(self.soc_ecu * 2) / 2.0, round(i, 2),
                         [round(v + rng.gauss(0, 0.004), 2) for v in vs])
            if el >= next_slow:
                next_slow += 5.0
                fan_v = 0.0 if self.t_batt < 33 else min(12.0, (self.t_batt - 30) * 1.2)
                d = {"intake": amb + 3.0, "fan_v": fan_v, "aux_v": 14.0}
                if cfg.full_21cf:
                    d.update(chg_kw=25, dis_kw=21, dsoc=max(self.soc) - min(self.soc), fan=int(fan_v / 2),
                             tb1=self.t_batt - 0.5, tb2=self.t_batt + 0.6, tb3=self.t_batt)
                c.cf(self.t, d)
                c.d0(self.t, {"t_low": 0, "t_dcinh": 0, "t_high": 0, "t_hot": 0,
                              "r": [int(round(self.r25(k) * 1000)) for k in range(N_BLOCKS)]})
                c.obd(self.t, "aux_v", 13.95 + rng.gauss(0, 0.01))
                c.obd(self.t, "ambient", round(amb))
        self.t = t0 + seconds
        c.tick(self.t + 30.0)       # IG-OFF: the HV frames stop


def simulate(cfg: SynthConfig, collector: Collector) -> SynthCar:
    car = SynthCar(cfg, collector)
    rng = car.rng
    end = cfg.start_ts + cfg.days * DAY
    car.rest(3600.0)
    while car.t < end:
        car.ride(cfg.ride_s * rng.uniform(0.7, 1.4))
        gap = DAY / cfg.rides_per_day
        # short stop or a longer rest (night); sometimes several days
        r = rng.random()
        rest = gap * rng.uniform(0.15, 0.5) if r < 0.45 else gap * rng.uniform(0.9, 1.6) if r < 0.93 \
            else gap * rng.uniform(3, 8)
        car.rest(max(600.0, rest))
    collector.finish()
    return car
