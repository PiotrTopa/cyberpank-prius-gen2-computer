#!/usr/bin/env python3
"""analyze_pulses.py <campaign_dir> -- pulse-series (plan P) analysis.

For every fan level and pulse length: energy input, end temperatures, cooling-curve
metrics (early fall, 2-exp fit of die / mem / pm8998), then per fan level the
saturation of the slow tail vs pulse length, the charging time constant, the
effective heat capacity and the resistances.  Prints Markdown, writes pulses.json.
"""
import csv
import json
import math
import sys
from collections import defaultdict

import numpy as np
from scipy.optimize import curve_fit

CPU = ["cpu%d-thermal" % i for i in range(8)] + ["cluster0-thermal", "cluster1-thermal"]
RPM = {0: 0, 20: 1475, 40: 2505, 60: 3380, 70: 3745, 80: 4085, 100: 4680}


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def load(d):
    rows = list(csv.DictReader(open(d + "/samples.csv")))
    ph = [json.loads(l) for l in open(d + "/phases.jsonl") if l.strip()]
    D = {
        "ts": np.array([fnum(r["ts"]) for r in rows]),
        "cpu": np.array([max(fnum(r[z]) for z in CPU) for r in rows]),
        "gpu": np.array([max(fnum(r["gpu-top-thermal"]), fnum(r["gpu-bottom-thermal"])) for r in rows]),
        "mem": np.array([fnum(r["mem-thermal"]) for r in rows]),
        "pm": np.array([fnum(r["pm8998-thermal"]) for r in rows]),
        "aoss": np.array([fnum(r["aoss0-thermal"]) for r in rows]),
        "p": np.array([fnum(r["p_w"]) for r in rows]),
        "amb": np.array([fnum(r["bmp2_t"]) for r in rows]),
        "rpm": np.array([fnum(r["fan_rpm"]) for r in rows]),
        "f0": np.array([fnum(r["f_cpu0_khz"]) for r in rows]) / 1e6,
        "fg": np.array([fnum(r["f_gpu_hz"]) for r in rows]) / 1e6,
        "online": [r["cpus_online"] for r in rows],
    }
    return D, ph


def seg(D, t0, t1, key):
    m = (D["ts"] >= t0) & (D["ts"] <= t1)
    return D["ts"][m] - t0, D[key][m]


def mean_last(D, ph, key, w=30):
    t, y = seg(D, ph["t_end"] - w, ph["t_end"], key)
    y = y[~np.isnan(y)]
    return float(np.mean(y)) if len(y) else float("nan")


def exp2(t, tinf, a, t1, b, t2):
    return tinf + a * np.exp(-t / t1) + b * np.exp(-t / t2)


def fit2(t, y):
    y0, yend = float(np.mean(y[:3])), float(np.mean(y[-20:]))
    try:
        p, _ = curve_fit(exp2, t, y, p0=[yend, (y0 - yend) * 0.6, 5.0, (y0 - yend) * 0.4, 80.0],
                         bounds=([-20, -100, 0.5, -100, 15], [120, 100, 40, 100, 5000]), maxfev=40000)
        if p[2] > p[4]:
            p = [p[0], p[3], p[4], p[1], p[2]]
        rmse = float(np.sqrt(np.mean((exp2(t, *p) - y) ** 2)))
        return dict(tinf=round(p[0], 2), a_fast=round(p[1], 2), tau_fast=round(p[2], 1),
                    a_slow=round(p[3], 2), tau_slow=round(p[4], 1), rmse=round(rmse, 3))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:60]}


def at(t, y, s):
    i = int(np.searchsorted(t, s))
    return float(y[min(i, len(y) - 1)]) if len(y) else float("nan")


def main(d):
    D, ph = load(d)
    runs = defaultdict(dict)
    for p in ph:
        runs[p["run"]][p["phase"]] = p
    out = {}
    print("## Pulse series (plan P): per fan level and pulse\n")
    print("| fan % | RPM | pulse s | P_idle W | P_load W | E_in J | die end | mem end | pm end | die fall 30 s | die fall 60 s "
          "| sink (die@15-20 s) | die 120 s - base | mem 120 s - base | die tail fast K/s | die tail slow K/s | mem tail slow K/s | pm tail slow K/s | T_inf die | cool s | tainted |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for run in sorted(runs, key=lambda r: -int(r[1:])):
        if not run.startswith("P"):
            continue
        R = runs[run]
        idle = R.get("idle")
        if not idle:
            continue
        fan = idle["fan"]
        t_amb = mean_last(D, idle, "amb", 60)
        base_die = mean_last(D, idle, "cpu", 60)
        base_mem = mean_last(D, idle, "mem", 60)
        base_pm = mean_last(D, idle, "pm", 60)
        p_idle = mean_last(D, idle, "p", 60)
        rpm = mean_last(D, idle, "rpm", 60)
        res = dict(fan=fan, rpm=rpm, t_amb=t_amb, base_die=base_die, base_mem=base_mem, base_pm=base_pm,
                   p_idle=p_idle, pulses=[])
        pulses = sorted([k for k in R if k.startswith("pulse")], key=lambda k: int(k[5:]))
        for pk in pulses:
            dur = int(pk[5:])
            pulse, cool = R[pk], R.get("cool%d" % dur)
            if not cool:
                continue
            t, pw = seg(D, pulse["t_start"], pulse["t_end"], "p")
            e_in = float(np.nansum((pw - p_idle))) if len(pw) else float("nan")   # 1 Hz -> J
            p_load = float(np.nanmean(pw[5:])) if len(pw) > 5 else float("nan")
            die_end, mem_end, pm_end = (mean_last(D, pulse, k, 10) for k in ("cpu", "mem", "pm"))
            tc, yc = seg(D, cool["t_start"], cool["t_end"], "cpu")
            tm, ym = seg(D, cool["t_start"], cool["t_end"], "mem")
            tp, yp = seg(D, cool["t_start"], cool["t_end"], "pm")
            fits = {k: fit2(tt, yy) for k, tt, yy in (("die", tc, yc), ("mem", tm, ym), ("pm", tp, yp)) if len(tt) > 60}
            rec = dict(pulse_s=dur, p_load=p_load, e_in_j=e_in, die_end=die_end, mem_end=mem_end, pm_end=pm_end,
                       die_fall_30=at(tc, yc, 0) - at(tc, yc, 30), die_fall_60=at(tc, yc, 0) - at(tc, yc, 60),
                       die_120_excess=at(tc, yc, 120) - base_die, mem_120_excess=at(tm, ym, 120) - base_mem,
                       sink_c=float(np.mean(yc[(tc >= 15) & (tc <= 20)])) if ((tc >= 15) & (tc <= 20)).any() else float("nan"),
                       die_300_excess=at(tc, yc, 300) - base_die if tc[-1] >= 300 else float("nan"),
                       fits=fits, cool_s=cool["duration_s"], cool_reason=cool["end_reason"],
                       tainted=sorted(set(pulse["tainted"] + cool["tainted"])),
                       f0_ghz=mean_last(D, pulse, "f0", 30), fg_mhz=mean_last(D, pulse, "fg", 30),
                       online=pulse.get("cpus_online"))
            res["pulses"].append(rec)
            fd, fm, fp = fits.get("die", {}), fits.get("mem", {}), fits.get("pm", {})
            print("| %d | %.0f | %d | %.2f | %.2f | %.0f | %.1f | %.1f | %.1f | %.1f | %.1f | %.1f | %.1f | %.1f | %s/%s | %s/%s | %s/%s | %s/%s | %s | %.0f | %s |" % (
                fan, rpm, dur, p_idle, p_load, e_in, die_end, mem_end, pm_end, rec["die_fall_30"], rec["die_fall_60"],
                rec["sink_c"], rec["die_120_excess"], rec["mem_120_excess"],
                fd.get("a_fast", "-"), fd.get("tau_fast", "-"), fd.get("a_slow", "-"), fd.get("tau_slow", "-"),
                fm.get("a_slow", "-"), fm.get("tau_slow", "-"), fp.get("a_slow", "-"), fp.get("tau_slow", "-"),
                fd.get("tinf", "-"), rec["cool_s"], ",".join(rec["tainted"]) or "-"))
        # charging curve of the slow tail amplitude vs pulse length -> tau_charge, A_inf
        xs = np.array([r["pulse_s"] for r in res["pulses"]], dtype=float)
        for key in ("die", "mem", "pm"):
            ys = np.array([r["fits"].get(key, {}).get("a_slow", np.nan) for r in res["pulses"]], dtype=float)
            ok = ~np.isnan(ys)
            if ok.sum() >= 3:
                try:
                    f = lambda x, a, tau: a * (1 - np.exp(-x / tau))
                    p, _ = curve_fit(f, xs[ok], ys[ok], p0=[max(ys[ok]), 120.0], bounds=([0, 5], [200, 5000]))
                    res["charge_" + key] = dict(a_inf=round(p[0], 2), tau_charge=round(p[1], 1))
                except Exception as exc:  # noqa: BLE001
                    res["charge_" + key] = {"error": str(exc)[:60]}
        ys = np.array([r["sink_c"] - base_die for r in res["pulses"]], dtype=float)
        ok = ~np.isnan(ys)
        if ok.sum() >= 3:
            try:
                f = lambda x, a, tau: a * (1 - np.exp(-x / tau))
                p, _ = curve_fit(f, xs[ok], ys[ok], p0=[max(ys[ok]), 120.0], bounds=([0, 5], [200, 5000]))
                res["charge_sink"] = dict(a_inf=round(p[0], 2), tau_charge=round(p[1], 1),
                                          points={int(x): round(float(y), 2) for x, y in zip(xs[ok], ys[ok])})
            except Exception as exc:  # noqa: BLE001
                res["charge_sink"] = {"error": str(exc)[:60]}
        # steady-state resistances from the longest pulse
        if res["pulses"]:
            longest = res["pulses"][-1]
            pn = longest["p_load"] - p_idle
            res["r_tot_die"] = (longest["die_end"] - t_amb) / longest["p_load"] if longest["p_load"] else float("nan")
            res["r_step_die"] = (longest["die_end"] - base_die) / pn if pn else float("nan")
            res["r_step_mem"] = (longest["mem_end"] - base_mem) / pn if pn else float("nan")
            res["r_step_pm"] = (longest["pm_end"] - base_pm) / pn if pn else float("nan")
            res["p_net"] = pn
            ch = res.get("charge_sink") or res.get("charge_die") or {}
            if "a_inf" in ch and pn:
                r_m = ch["a_inf"] / pn
                res["r_mass"] = r_m
                res["c_mass"] = ch["tau_charge"] / r_m if r_m else float("nan")
        out[run] = res
    print("\n## Per fan level: resistances and the mass behind the die\n")
    print("| fan % | RPM | T_amb | base die | P_idle | P_net W | R_tot die K/W | R_step die K/W | R_step mem | R_step pm "
          "| sink excess A_inf K | τ_charge sink s | slow-tail A_inf die K | τ_charge die s | R_sink K/W | C_sink J/K |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for run, r in sorted(out.items(), key=lambda kv: -kv[1]["fan"]):
        cd, cm = r.get("charge_die", {}), r.get("charge_sink", {})
        print("| %d | %.0f | %.1f | %.1f | %.2f | %.2f | %.2f | %.2f | %.2f | %.2f | %s | %s | %s | %s | %s | %s |" % (
            r["fan"], r["rpm"], r["t_amb"], r["base_die"], r["p_idle"], r.get("p_net", float("nan")),
            r.get("r_tot_die", float("nan")), r.get("r_step_die", float("nan")), r.get("r_step_mem", float("nan")),
            r.get("r_step_pm", float("nan")), cm.get("a_inf", "-"), cm.get("tau_charge", "-"),
            cd.get("a_inf", "-"), cd.get("tau_charge", "-"),
            "%.2f" % r["r_mass"] if "r_mass" in r else "-", "%.0f" % r["c_mass"] if "c_mass" in r else "-"))
    json.dump(out, open(d + "/pulses.json", "w"), indent=1, default=float)
    print("\nwrote", d + "/pulses.json")


if __name__ == "__main__":
    main(sys.argv[1])
