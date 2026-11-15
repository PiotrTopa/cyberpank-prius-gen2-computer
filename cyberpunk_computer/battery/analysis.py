"""Raw ride / rest record -> features. Definitions: docs/BATTERY_HEALTH.md.

Pure functions of a raw record (collector.py), so a stored record can be
re-analysed whenever ``ANALYSIS_VERSION`` changes.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

from . import stats as S
from .model import (
    ANALYSIS_VERSION, COLD_C, I_LOAD, I_REF, I_REST, LEAD_V_PER_SOC, MIN_BLK_FIT,
    MIN_GOOD_S, MIN_LOAD_SPAN_A, MIN_RIDE_S, MIN_STEPS_R, N_BLOCKS, NOMINAL_AH, NONE16,
    OCV_MAX_I, OCV_WINDOW_S, REST_MARKS_H, R_ARRHENIUS_B_PRIOR, SOC_HIGH, SOC_JUMP_PCT,
    SOC_LOW, STEP_MAX_DT, STEP_MIN_DI, T_REF_C, V12_CRIT, V12_LOW,
)

rnd = S.rnd


def _col(table: dict, name: str, scale: float = 1.0) -> List[Optional[float]]:
    return [None if v == NONE16 else v / scale for v in table.get(name, [])]


def _valid(xs) -> List[float]:
    return [x for x in xs if x is not None]


def arrhenius_factor(t_c: float, b: float = R_ARRHENIUS_B_PRIOR) -> float:
    """R(T) / R(25 degC) for the Arrhenius model."""
    return math.exp(b * (1.0 / (t_c + 273.15) - 1.0 / (T_REF_C + 273.15)))


# ── ride ────────────────────────────────────────────────────────────────────

def analyse_ride(rec: dict) -> dict:
    meta, tb = rec["meta"], rec["tables"]
    blk, sec, slow, wake = tb["blk"], tb["sec"], tb["slow"], tb.get("wake", {})
    dur = max(0.0, meta["end_ts"] - meta["start_ts"])
    n_blk = len(blk["t"])
    flags: List[str] = []
    f: dict = {
        "v": ANALYSIS_VERSION, "dur_s": rnd(dur, 1), "ready_s": meta.get("ready_s"),
        "dist_km": meta.get("dist_km"), "n_blk": n_blk, "n_sec": len(sec["t"]),
        "blk_rate_hz": rnd(n_blk / dur, 3) if dur > 0 else None,
        "source": meta.get("source"),
    }
    if not meta.get("closed", True):
        flags.append("open")
    if meta.get("truncated"):
        flags.append("truncated")

    # ── series ──────────────────────────────────────────────────────────────
    bt = [v / 10.0 for v in blk["t"]]
    bsoc = [v / 2.0 for v in blk["soc2"]]
    bi = [v / 100.0 for v in blk["i"]]
    bv = [[blk["v%d" % (k + 1)][r] / 100.0 for k in range(N_BLOCKS)] for r in range(n_blk)]

    st = slow["t"]
    soc = _col(slow, "soc2", 2)
    t1, t2 = _col(slow, "t1"), _col(slow, "t2")
    tbs = [_col(slow, "tb%d" % k, 100) for k in (1, 2, 3)]
    have_tb = any(x is not None for x in tbs[0])
    tbatt: List[Optional[float]] = []
    tgrad: List[Optional[float]] = []
    for r in range(len(st)):
        vals = _valid([tbs[0][r], tbs[1][r], tbs[2][r]]) if have_tb else _valid([t1[r], t2[r]])
        tbatt.append(sum(vals) / len(vals) if vals else None)
        tgrad.append(max(vals) - min(vals) if len(vals) >= 2 else None)
    tv = _valid(tbatt)
    t_mean = S.mean(tv)

    # ── temperature / cooling ───────────────────────────────────────────────
    intake = _col(slow, "intake", 100)
    fan_v = _col(slow, "fan_v", 10)
    fan = _col(slow, "fan")
    amb = _col(slow, "amb", 10)
    pb_t = _col(slow, "pb_t", 100)
    ah_dis, ah_chg = meta.get("ah_dis", 0.0), meta.get("ah_chg", 0.0)
    have_pack = meta.get("n_pack", 0) > 0 and meta.get("pack_s", 0.0) > 0.5 * dur
    if not have_pack and n_blk >= 2:
        # no broadcast stream: integrate the 21CE current (coarse)
        ah_dis = ah_chg = 0.0
        for r in range(1, n_blk):
            dt = bt[r] - bt[r - 1]
            if 0 < dt <= 10:
                q = bi[r - 1] * dt / 3600.0
                if q > 0:
                    ah_dis += q
                else:
                    ah_chg -= q
        flags.append("no_pack_stream")
    ah_thru = ah_dis + ah_chg
    dti = [a - b for a, b, fv in zip(tbatt, intake, fan_v)
           if a is not None and b is not None]
    fan_on = [1.0 if (x is not None and x > 1.0) else 0.0 for x in fan_v if x is not None]
    temp = {
        "source": "21CF TB1-3" if have_tb else "0x3CB T1/T2",
        "t_start": rnd(tv[0], 2) if tv else None, "t_end": rnd(tv[-1], 2) if tv else None,
        "t_min": rnd(min(tv), 2) if tv else None, "t_max": rnd(max(tv), 2) if tv else None,
        "t_mean": rnd(t_mean, 2),
        "grad_max": rnd(max(_valid(tgrad)), 2) if _valid(tgrad) else None,
        "grad_mean": rnd(S.mean(_valid(tgrad)), 2),
        "intake_mean": rnd(S.mean(_valid(intake)), 2),
        "dt_intake_mean": rnd(S.mean(dti), 2),
        "fan_v_mean": rnd(S.mean(_valid(fan_v)), 2),
        "fan_on_frac": rnd(S.mean(fan_on), 3),
        "fan_step_max": max(_valid(fan)) if _valid(fan) else None,
        "amb_mean": rnd(S.mean(_valid(amb)), 1),
        "cabin_mean": rnd(S.mean(_valid(pb_t)), 1),
        "heat_k_per_ah": rnd((tv[-1] - tv[0]) / ah_thru, 3) if tv and ah_thru >= 2.0 else None,
        "inv_max": max(_valid(_col(slow, "inv1")) + _valid(_col(slow, "inv2")), default=None),
    }
    f["temp"] = temp
    if not tv:
        flags.append("no_temperature")
    elif t_mean is not None and t_mean < COLD_C:
        flags.append("cold")

    # ── SOC against ampere-hours ────────────────────────────────────────────
    f["soc"] = _soc_features(st, soc, sec, bt, bsoc, bi, have_pack)

    # ── stress / usage ──────────────────────────────────────────────────────
    ccl, cdl = _valid(_col(slow, "ccl")), _valid(_col(slow, "cdl"))
    pack_s = meta.get("pack_s", 0.0)
    f["stress"] = {
        "ah_dis": rnd(ah_dis, 3), "ah_chg": rnd(ah_chg, 3), "ah_thru": rnd(ah_thru, 3),
        "wh_dis": rnd(meta.get("wh_dis"), 1), "wh_chg": rnd(meta.get("wh_chg"), 1),
        "i_max": meta.get("i_max") if have_pack else rnd(max(bi), 1) if bi else None,
        "i_min": meta.get("i_min") if have_pack else rnd(min(bi), 1) if bi else None,
        "i_rms": rnd(math.sqrt(meta["i2s"] / pack_s), 2) if have_pack and pack_s > 0 else None,
        "p_max_kw": meta.get("p_max_kw") if have_pack else None,
        "p_min_kw": meta.get("p_min_kw") if have_pack else None,
        "hist_s": meta.get("hist_s") if have_pack else None,
        "near_limit_s": meta.get("near_limit_s") if have_pack else None,
        "ccl_min": min(ccl) if ccl else None, "ccl_mean": rnd(S.mean(ccl), 1),
        "cdl_min": min(cdl) if cdl else None, "cdl_mean": rnd(S.mean(cdl), 1),
        "chg_kw_min": min(_valid(_col(slow, "chg_kw")), default=None),
        "dis_kw_min": min(_valid(_col(slow, "dis_kw")), default=None),
        "pack_gap_s": meta.get("pack_gap_s"),
    }

    # ── blocks: balance and resistance ──────────────────────────────────────
    f["bal"], f["r"], f["ocv"] = _block_features(bt, bsoc, bi, bv, dur, meta, t_mean, flags)

    # ── ECU's own figures ───────────────────────────────────────────────────
    d0 = meta.get("d0")
    dsoc = _valid(_col(slow, "dsoc", 100))
    f["ecu"] = {
        "r_mohm": d0["r_mean"] if d0 else None,
        "acc_times_s": d0["last"] if d0 else None,
        "acc_times_delta_s": ({k: d0["last"][k] - d0["first"][k] for k in d0["last"]} if d0 else None),
        "dsoc_max": rnd(max(dsoc), 2) if dsoc else None,
        "cf_full": meta.get("counts", {}).get("cf_full", 0),
        "cf_first": meta.get("counts", {}).get("cf_first", 0),
    }

    # ── 12 V ────────────────────────────────────────────────────────────────
    f["aux"] = _aux_features(slow, wake)

    # ── quality ─────────────────────────────────────────────────────────────
    if n_blk == 0:
        flags.append("no_block_poll")
    elif f["blk_rate_hz"] is not None and f["blk_rate_hz"] < 0.25:
        flags.append("block_poll_gappy")
    if not f["ecu"]["cf_full"]:
        flags.append("21cf_first_frame_only" if f["ecu"]["cf_first"] else "no_21cf")
    if dur < MIN_RIDE_S or (n_blk == 0 and not have_pack):
        grade = "X"
    elif dur < MIN_GOOD_S or n_blk < MIN_BLK_FIT or f["r"]["n_steps"] < MIN_STEPS_R:
        grade = "C"
    elif any(x in flags for x in ("cold", "no_pack_stream", "block_poll_gappy",
                                  "no_temperature", "truncated", "low_current_span")):
        grade = "B"
    else:
        grade = "A"
    f["quality"] = {"grade": grade, "flags": flags}
    return f


def _soc_features(st, soc, sec, bt, bsoc, bi, have_pack) -> dict:
    out: dict = {"start": None, "end": None, "min": None, "max": None, "swing": None,
                 "frac_low": None, "frac_high": None, "pct_per_ah": None, "pct_per_ah_se": None,
                 "implied_ah": None, "jumps": None, "resid_rms": None, "half_cycles_10": None}
    # (time, soc, cumulative net discharge Ah) samples
    pts: List[tuple] = []
    if have_pack and sec["t"]:
        cum = 0.0
        k = 0
        n = len(sec["t"])
        for r, ts in enumerate(st):
            while k < n and sec["t"][k] <= ts:
                cum += sec["i"][k] / 10.0 / 3600.0
                k += 1
            if soc[r] is not None:
                pts.append((ts, soc[r], cum))
    elif len(bt) >= 2:
        cum = 0.0
        for r in range(len(bt)):
            if r:
                dt = bt[r] - bt[r - 1]
                if 0 < dt <= 10:
                    cum += bi[r - 1] * dt / 3600.0
            pts.append((bt[r], bsoc[r], cum))
    if not pts:
        return out
    socs = [p[1] for p in pts]
    out.update(start=socs[0], end=socs[-1], min=min(socs), max=max(socs),
               swing=rnd(max(socs) - min(socs), 1),
               frac_low=rnd(sum(1 for s in socs if s <= SOC_LOW) / len(socs), 3),
               frac_high=rnd(sum(1 for s in socs if s >= SOC_HIGH) / len(socs), 3))
    ahs = [p[2] for p in pts]
    if len(pts) >= 12 and max(socs) - min(socs) >= 4.0 and max(ahs) - min(ahs) >= 0.25:
        fit = S.ols([[-a for a in ahs]], socs)
        if fit:
            beta, se, sd = fit
            out["pct_per_ah"] = rnd(beta[1], 2)
            out["pct_per_ah_se"] = rnd(se[1], 2)
            out["implied_ah"] = rnd(100.0 / beta[1], 2) if beta[1] > 1.0 else None
            out["resid_rms"] = rnd(sd, 2)
    # SOC moves that the ampere-hours do not explain (ECU recalibration)
    jumps = 0
    step = max(1, int(round(20.0 / max(1.0, (pts[-1][0] - pts[0][0]) / max(1, len(pts) - 1)))))
    r = step
    while r < len(pts):
        dsoc = pts[r][1] - pts[r - step][1]
        expect = -(pts[r][2] - pts[r - step][2]) * 100.0 / NOMINAL_AH
        if abs(dsoc - expect) >= SOC_JUMP_PCT and pts[r][0] - pts[r - step][0] <= 45:
            jumps += 1
            r += step
        else:
            r += 1
    out["jumps"] = jumps
    # half cycles of at least 10 % (zigzag)
    hc = 0
    ext = socs[0]
    direction = 0
    for s in socs:
        if direction >= 0 and s > ext:
            ext = s if direction == 1 else ext
        if direction == 0:
            if abs(s - socs[0]) >= 10.0:
                direction = 1 if s > socs[0] else -1
                ext = s
                hc += 1
        elif direction == 1:
            if s > ext:
                ext = s
            elif ext - s >= 10.0:
                direction, ext, hc = -1, s, hc + 1
        else:
            if s < ext:
                ext = s
            elif s - ext >= 10.0:
                direction, ext, hc = 1, s, hc + 1
    out["half_cycles_10"] = hc
    return out


def _regime(bi, dev, spread, cond) -> dict:
    rows = [r for r in range(len(bi)) if cond(bi[r])]
    if not rows:
        return {"n": 0, "dev_mv": None, "spread_mean_mv": None, "spread_p95_mv": None, "i_mean": None}
    return {
        "n": len(rows),
        "dev_mv": [rnd(1000 * S.mean([dev[r][k] for r in rows]), 1) for k in range(N_BLOCKS)],
        "spread_mean_mv": rnd(1000 * S.mean([spread[r] for r in rows]), 1),
        "spread_p95_mv": rnd(1000 * S.quantile([spread[r] for r in rows], 0.95), 1),
        "i_mean": rnd(S.mean([bi[r] for r in rows]), 1),
    }


def _block_features(bt, bsoc, bi, bv, dur, meta, t_mean, flags):
    n = len(bt)
    bal: dict = {"rest": _regime([], [], [], None) if n == 0 else None}
    r_out: dict = {"n_steps": 0, "blk_mohm": None, "blk_se": None, "rel": None, "sum_mohm": None,
                   "med_mohm": None, "max_rel": None, "max_rel_block": None,
                   "pack_fast_mohm": None, "pack_fast_se": None, "pack_fast_n": 0,
                   "pack_1s_mohm": None, "pack_1s_n": 0,
                   "t_mean": rnd(t_mean, 2), "soc_mean": rnd(S.mean(bsoc), 1) if bsoc else None,
                   "sum25_mohm": None, "fast25_mohm": None}
    ocv: dict = {"on": None, "off": None}

    # pack resistance from the 0x03B pairs
    for fp in meta.get("fast", []):
        if fp["n"] >= 8 and fp["sxx"] > 0:
            r_ohm = -fp["sxy"] / fp["sxx"]
            se = math.sqrt(max(0.0, fp["syy"] - fp["sxy"] ** 2 / fp["sxx"]) / ((fp["n"] - 1) * fp["sxx"]))
            if fp["lag"] < 0.5:
                r_out.update(pack_fast_mohm=rnd(1000 * r_ohm, 1), pack_fast_se=rnd(1000 * se, 1),
                             pack_fast_n=fp["n"])
            else:
                r_out.update(pack_1s_mohm=rnd(1000 * r_ohm, 1), pack_1s_n=fp["n"])
    if r_out["pack_fast_mohm"] is not None and t_mean is not None:
        r_out["fast25_mohm"] = rnd(r_out["pack_fast_mohm"] / arrhenius_factor(t_mean), 1)

    if n == 0:
        bal = {"rest": _regime([], [], [], None), "dis": _regime([], [], [], None),
               "chg": _regime([], [], [], None), "fit": None}
        return bal, r_out, ocv

    med = [S.median(row) for row in bv]
    dev = [[row[k] - med[r] for k in range(N_BLOCKS)] for r, row in enumerate(bv)]
    spread = [max(row) - min(row) for row in bv]
    bal = {
        "rest": _regime(bi, dev, spread, lambda i: abs(i) <= I_REST),
        "dis": _regime(bi, dev, spread, lambda i: i >= I_LOAD),
        "chg": _regime(bi, dev, spread, lambda i: i <= -I_LOAD),
        "spread_max_mv": rnd(1000 * max(spread), 0),
        "spread_all_mean_mv": rnd(1000 * S.mean(spread), 1),
        "fit": None,
    }
    # which block is lowest while discharging
    dis_rows = [r for r in range(n) if bi[r] >= I_LOAD]
    if dis_rows:
        cnt = [0] * N_BLOCKS
        for r in dis_rows:
            cnt[min(range(N_BLOCKS), key=lambda k: bv[r][k])] += 1
        bal["sag_first_frac"] = [rnd(c / len(dis_rows), 3) for c in cnt]

    # deviation model per block: dev = a + k*I (+ c*(SOC - mean))
    span = (S.quantile(bi, 0.95) - S.quantile(bi, 0.05)) if n >= 5 else 0.0
    soc_mean = S.mean(bsoc)
    soc_span = max(bsoc) - min(bsoc)
    if n >= MIN_BLK_FIT and span >= MIN_LOAD_SPAN_A:
        use_soc = soc_span >= 8.0
        xs = [bi] + ([[s - soc_mean for s in bsoc]] if use_soc else [])
        a_mv, dr, dr_se, c_soc = [], [], [], []
        ok = True
        for k in range(N_BLOCKS):
            fit = S.ols(xs, [dev[r][k] for r in range(n)])
            if fit is None:
                ok = False
                break
            beta, se, _ = fit
            a_mv.append(rnd(1000 * beta[0], 1))
            dr.append(rnd(-1000 * beta[1], 2))
            dr_se.append(rnd(1000 * se[1], 2))
            c_soc.append(rnd(1000 * beta[2], 2) if use_soc else None)
        if ok:
            dev_ref = [rnd(a_mv[k] - dr[k] * I_REF, 1) for k in range(N_BLOCKS)]
            dev_chg = [rnd(a_mv[k] + dr[k] * I_REF, 1) for k in range(N_BLOCKS)]
            weak = min(range(N_BLOCKS), key=lambda k: dev_ref[k])
            bal["fit"] = {
                "n": n, "i_span": rnd(span, 1), "soc_span": rnd(soc_span, 1),
                "dev0_mv": a_mv,                 # deviation at I = 0
                "dr_mohm": dr,                   # resistance above the median block
                "dr_se": dr_se,
                "soc_mv_per_pct": c_soc if use_soc else None,
                "dev_ref_mv": dev_ref,           # deviation at +I_REF (discharge)
                "dev_chg_mv": dev_chg,           # deviation at -I_REF (charge)
                "spread0_mv": rnd(max(a_mv) - min(a_mv), 1),
                "spread_ref_mv": rnd(max(dev_ref) - min(dev_ref), 1),
                "weak_block": weak + 1, "weak_dev_mv": dev_ref[weak],
            }
    elif n >= MIN_BLK_FIT:
        flags.append("low_current_span")

    # block resistance from current steps between consecutive 21CE samples
    dx: List[float] = []
    dys: List[List[float]] = [[] for _ in range(N_BLOCKS)]
    for r in range(1, n):
        if bt[r] - bt[r - 1] <= STEP_MAX_DT:
            di = bi[r] - bi[r - 1]
            if abs(di) >= STEP_MIN_DI:
                dx.append(di)
                for k in range(N_BLOCKS):
                    dys[k].append(bv[r][k] - bv[r - 1][k])
    r_out["n_steps"] = len(dx)
    if len(dx) >= 5:
        rb, rse = [], []
        for k in range(N_BLOCKS):
            s = S.origin_slope(dx, dys[k])
            rb.append(-1000 * s[0])
            rse.append(1000 * s[1])
        rmed = S.median(rb)
        rel = [x / rmed for x in rb] if rmed and rmed > 0 else None
        r_out.update(blk_mohm=[rnd(x, 2) for x in rb], blk_se=[rnd(x, 2) for x in rse],
                     sum_mohm=rnd(sum(rb), 1), med_mohm=rnd(rmed, 2),
                     rel=[rnd(x, 4) for x in rel] if rel else None)
        if rel:
            kmax = max(range(N_BLOCKS), key=lambda k: rel[k])
            r_out.update(max_rel=rnd(rel[kmax], 4), max_rel_block=kmax + 1)
        if t_mean is not None:
            r_out["sum25_mohm"] = rnd(sum(rb) / arrhenius_factor(t_mean), 1)

    # open-circuit snapshots at key-on and key-off
    rb_ohm = [x / 1000.0 for x in r_out["blk_mohm"]] if r_out["blk_mohm"] else None

    def snap(rows: List[int]) -> Optional[dict]:
        quiet = [r for r in rows if abs(bi[r]) <= OCV_MAX_I]
        corrected = False
        if not quiet:
            quiet = [r for r in rows if abs(bi[r]) <= 30.0] if rb_ohm else []
            corrected = True
        if not quiet:
            return None
        v = []
        for k in range(N_BLOCKS):
            comp = rb_ohm[k] if rb_ohm else 0.0
            v.append(S.mean([bv[r][k] + bi[r] * comp for r in quiet]))
        m = S.median(v)
        return {"n": len(quiet), "i_mean": rnd(S.mean([bi[r] for r in quiet]), 2),
                "ir_corrected": bool(rb_ohm), "loaded": corrected,
                "v_mean": rnd(S.mean(v), 4), "soc": bsoc[quiet[-1]],
                "dev_mv": [rnd(1000 * (x - m), 1) for x in v]}

    ocv["on"] = snap([r for r in range(n) if bt[r] <= OCV_WINDOW_S])
    ocv["off"] = snap([r for r in range(n) if bt[r] >= dur - OCV_WINDOW_S])
    return bal, r_out, ocv


def _aux_features(slow: dict, wake: dict) -> dict:
    ready = _col(slow, "ready")
    obd = _col(slow, "aux_obd", 1000)
    pb = _col(slow, "pb_v", 1000)
    cf = _col(slow, "aux_cf", 10)
    st = slow["t"]
    known = any(x is not None for x in ready)
    rows = [r for r in range(len(st)) if (ready[r] == 1 if known else st[r] >= 60)]
    o = _valid([obd[r] for r in rows])
    p = _valid([pb[r] for r in rows])
    off = [obd[r] - pb[r] for r in rows if obd[r] is not None and pb[r] is not None]
    out = {
        "ready_v_obd": rnd(S.median(o), 3), "ready_v_obd_min": rnd(min(o), 3) if o else None,
        "ready_v_pb": rnd(S.median(p), 3), "pb_offset": rnd(S.median(off), 3),
        "cf_v": rnd(S.median(_valid([cf[r] for r in rows])), 1),
        "wake_pre_v": None, "wake_min_v": None, "wake_sag_v": None,
    }
    wt = wake.get("t", [])
    if wt:
        # t is in 0.1 s relative to the first HV frame (ACC precedes it by a few s)
        pre = [wake["v"][r] / 1000.0 for r in range(len(wt)) if wt[r] < -100]
        post = [wake["v"][r] / 1000.0 for r in range(len(wt)) if -100 <= wt[r] <= 300]
        if post:
            out["wake_min_v"] = rnd(min(post), 3)
        if pre:
            out["wake_pre_v"] = rnd(S.median(pre), 3)
            if post:
                out["wake_sag_v"] = rnd(S.median(pre) - min(post), 3)
    return out


# ── rest ────────────────────────────────────────────────────────────────────

def analyse_rest(rec: dict) -> dict:
    meta, m = rec["meta"], rec["tables"]["min"]
    dur = max(0.0, meta["end_ts"] - meta["start_ts"])
    t = m["t"]
    v = [x / 1000.0 for x in m["v"]]
    vmin = [x / 1000.0 for x in m["vmin"]]
    ma = [None if x < 0 else x / 10.0 for x in m["ma"]]
    temp = [None if x == NONE16 else x / 100.0 for x in m["temp"]]
    acc = m["acc"]
    n = len(t)
    f: dict = {
        "v": ANALYSIS_VERSION, "dur_h": rnd(dur / 3600.0, 3), "n_min": n,
        "coverage": rnd(min(1.0, n * 60.0 / dur), 3) if dur > 0 else None,
        "start_known": bool(meta.get("start_known", True)),
        "open": not meta.get("closed", True),
        "marks": {}, "v_min": None, "v_end": None, "v_start": None,
        "ma_mean": None, "mah": None, "temp_mean": None, "temp_min": None,
        "low_min": 0, "crit_min": 0, "acc_min": sum(1 for a in acc if a),
        "slope_mv_per_h": None, "ah_per_volt": None, "cap_proxy_ah": None,
    }
    if n == 0:
        return f
    quiet = [r for r in range(n) if not acc[r]]
    f["v_start"] = rnd(v[0], 3)
    f["v_min"] = rnd(min(vmin), 3)
    f["v_end"] = rnd(S.mean(v[-5:]), 3)
    f["low_min"] = sum(1 for x in vmin if x < V12_LOW)
    f["crit_min"] = sum(1 for x in vmin if x < V12_CRIT)
    mav = _valid(ma)
    f["ma_mean"] = rnd(S.mean(mav), 1)
    f["mah"] = rnd(sum(mav) / 60.0, 1)             # minute means integrated
    tvals = _valid(temp)
    f["temp_mean"] = rnd(S.mean(tvals), 1)
    f["temp_min"] = rnd(min(tvals), 1) if tvals else None
    if f["start_known"]:
        for h in REST_MARKS_H:
            lo = h * 60.0
            rows = [r for r in quiet if lo <= t[r] < lo + 5]
            if rows:
                f["marks"]["%g" % h] = rnd(S.mean([v[r] for r in rows]), 3)
    # discharge slope and the capacity proxy, after the surface charge is gone
    rows = [r for r in quiet if t[r] >= 120] if f["start_known"] else quiet
    if len(rows) >= 120:
        fit = S.ols([[t[r] / 60.0 for r in rows]], [v[r] for r in rows])
        if fit:
            f["slope_mv_per_h"] = rnd(1000 * fit[0][1], 2)
        v_a = S.mean([v[r] for r in rows[:10]])
        v_b = S.mean([v[r] for r in rows[-10:]])
        ah = sum(ma[r] for r in rows if ma[r] is not None) / 60.0 / 1000.0
        if v_a - v_b >= 0.05 and ah > 0.05:
            f["ah_per_volt"] = rnd(ah / (v_a - v_b), 2)
            f["cap_proxy_ah"] = rnd(ah / (v_a - v_b) * 100.0 * LEAD_V_PER_SOC, 1)
    return f


def pair_rest(prev_ride: dict, next_ride: dict, rest_h: float, mid_h: Optional[float] = None) -> Optional[dict]:
    """HV pack over a rest (features dicts of analyse_ride of two consecutive rides).

    Pack: key-off snapshot against the next key-on snapshot (mean block voltage,
    SOC). Per block: change of the zero-current offset ``bal.fit.dev0_mv``
    between the two rides, per day between their midpoints (``mid_h``). The fit
    intercept averages a whole ride's charge and discharge, so it does not
    carry the relaxation that a snapshot a few seconds after the last load
    does (a high-resistance block relaxes more and would pass for a leaky
    one). Without both fits the snapshots are used (``method`` = "snapshot").
    """
    off = (prev_ride.get("ocv") or {}).get("off")
    on = (next_ride.get("ocv") or {}).get("on")
    fa = ((prev_ride.get("bal") or {}).get("fit") or {}).get("dev0_mv")
    fb = ((next_ride.get("bal") or {}).get("fit") or {}).get("dev0_mv")
    if rest_h <= 0 or not ((off and on) or (fa and fb)):
        return None
    out = {
        "rest_h": rnd(rest_h, 2), "method": None,
        "d_v_mean_mv": rnd(1000 * (on["v_mean"] - off["v_mean"]), 1) if off and on else None,
        "d_soc": rnd(on["soc"] - off["soc"], 1) if off and on else None,
        "d_dev_mv": None,
        "t_on": (next_ride.get("temp") or {}).get("t_start"),
        "sd_rel_mv_per_day": None, "sd_pack_mv_per_day": None,
    }
    if fa and fb:
        out["method"] = "fit"
        d_dev = [fb[k] - fa[k] for k in range(N_BLOCKS)]
        days = (mid_h if mid_h else rest_h) / 24.0
    else:
        out["method"] = "snapshot"
        d_dev = [on["dev_mv"][k] - off["dev_mv"][k] for k in range(N_BLOCKS)]
        days = rest_h / 24.0
    out["d_dev_mv"] = [rnd(x, 1) for x in d_dev]
    if rest_h >= 8.0:
        out["sd_rel_mv_per_day"] = [rnd(x / days, 2) for x in d_dev]
        if out["d_v_mean_mv"] is not None:
            out["sd_pack_mv_per_day"] = rnd(out["d_v_mean_mv"] / (rest_h / 24.0), 2)
    return out
