"""Health indicators, trends and the precomputed aggregates the UI reads.

``build_summary(store)`` reads every ride's and rest's features (not the raw
records) and returns one JSON-able dict; the service stores it in the ``cache``
table after each ride, so the dashboard only fetches a ready document.

Thresholds: THRESHOLDS below. Each carries its origin; "own" marks this
project's estimate, not a manufacturer figure (docs/BATTERY_HEALTH.md §6).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from . import stats as S
from .analysis import arrhenius_factor, pair_rest
from .model import (
    ANALYSIS_VERSION, COLD_C, I_REF, MIN_REST_SD_H, MIN_STEPS_R, N_BLOCKS, NOMINAL_AH,
    R_ARRHENIUS_B_PRIOR, SCHEMA_VERSION, T_REF_C, V12_LOW,
)
from .trends import analyse_trend, DAY

rnd = S.rnd

# watch / act limits; "dir" = which side is bad; "trend_min" = slope per 30 days
# from which a statistically significant worsening trend alone raises "watch".
THRESHOLDS: Dict[str, dict] = {
    "balance_rest": {"trend_min": 10.0, "watch": 200.0, "act": 300.0, "dir": "high", "unit": "mV",
                     "origin": "own; scale of the 0.3 V block-difference figure reported for DTC P0A80/P3011-P3024 (forum-reported, not confirmed by a manual excerpt)"},
    "weak_block": {"trend_min": 10.0, "watch": -200.0, "act": -300.0, "dir": "low", "unit": "mV",
                   "origin": "own; same 0.3 V scale, at 50 A and 25 °C"},
    "r_spread": {"trend_min": 1.0, "watch": 20.0, "act": 35.0, "dir": "high", "unit": "%",
                 "origin": "own; 0.3 V at 50-100 A on a ~20-30 mΩ block is 10-30 %"},
    "r_pack": {"trend_min": 0.03, "watch": 25.0, "act": 50.0, "dir": "high", "unit": "% over own baseline",
               "origin": "own"},
    "soc_stability": {"trend_min": 0.3, "watch": 20.0, "act": 26.0, "dir": "high", "unit": "% SOC per Ah",
                      "origin": "own; nominal 15.4 %/Ah (6.5 Ah); 20 / 26 %/Ah = 5.0 / 3.8 Ah apparent"},
    "self_discharge": {"trend_min": 2.0, "watch": -15.0, "act": -40.0, "dir": "low", "unit": "mV/day",
                       "origin": "own"},
    "thermal": {"watch": 45.0, "act": 55.0, "dir": "high", "unit": "°C",
                "origin": "own; the ECU derates and runs the fan at full speed in this range"},
    "aux_rest": {"trend_min": 0.01, "watch": 12.2, "act": 11.9, "dir": "low", "unit": "V",
                 "origin": "general lead-acid open-circuit figures (about 50 % / 25 % charge); Toyota bulletins expect 12.8 V when charged"},
    "aux_low": {"watch": 11.8, "act": 11.5, "dir": "low", "unit": "V",
                "origin": "own; general lead-acid figures: below about 11.8 V under a light load the battery is nearly empty"},
    "aux_charge": {"watch": 13.5, "act": 13.0, "dir": "low", "unit": "V", "origin": "own"},
}

LEVELS = ("unknown", "ok", "watch", "act")


def level(key: str, value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    th = THRESHOLDS[key]
    if th["dir"] == "high":
        return "act" if value >= th["act"] else "watch" if value >= th["watch"] else "ok"
    return "act" if value <= th["act"] else "watch" if value <= th["watch"] else "ok"


def _g(d: Optional[dict], *path, default=None):
    for p in path:
        if not isinstance(d, dict):
            return default
        d = d.get(p)
    return d if d is not None else default


def arr_x(t_c: float) -> float:
    return 1.0 / (t_c + 273.15) - 1.0 / (T_REF_C + 273.15)


def block_view(f: dict, b: float = R_ARRHENIUS_B_PRIOR) -> Optional[dict]:
    """Per-block figures of one ride, normalised to 25 °C where it applies
    (``b`` = Arrhenius coefficient of the resistance, K)."""
    fit = _g(f, "bal", "fit")
    r = f.get("r") or {}
    t = r.get("t_mean")
    if not fit or t is None:
        return None
    fac = arrhenius_factor(t, b)
    dev25 = [rnd(fit["dev0_mv"][k] - fit["dr_mohm"][k] * I_REF / fac, 1) for k in range(N_BLOCKS)]
    rmed = r.get("med_mohm")
    dr_pct = [rnd(100.0 * fit["dr_mohm"][k] / rmed, 2) for k in range(N_BLOCKS)] if rmed else None
    weak = min(range(N_BLOCKS), key=lambda k: dev25[k])
    return {"dev0_mv": fit["dev0_mv"], "dev25_mv": dev25, "dr_pct": dr_pct,
            "dr_mohm": fit["dr_mohm"], "weak_block": weak + 1, "weak_dev25_mv": dev25[weak],
            "r_max_pct": max(dr_pct) if dr_pct else None,
            "r_max_block": (max(range(N_BLOCKS), key=lambda k: dr_pct[k]) + 1) if dr_pct else None}


def _good(f: dict) -> bool:
    return _g(f, "quality", "grade") in ("A", "B")


def _r_ok(f: dict) -> bool:
    r = f.get("r") or {}
    return (_good(f) and (r.get("n_steps") or 0) >= MIN_STEPS_R and r.get("t_mean") is not None
            and r["t_mean"] >= COLD_C)


def build_summary(store, now: Optional[float] = None) -> dict:
    t_start = time.perf_counter()
    now = now if now is not None else time.time()
    rides = store.rides(closed_only=True)
    rests = store.rests(closed_only=False)
    feats = [(r, r["features"]) for r in rides]
    good = [(r, f) for r, f in feats if _good(f)]
    trends: Dict[str, dict] = {}

    def trend(key: str, pts: List[tuple], **kw) -> dict:
        trends[key] = analyse_trend(pts, **kw)
        return trends[key]

    # ── HV trends ────────────────────────────────────────────────────────────
    tr_rsum = trend("r_sum", [(r["start_ts"], f["r"]["sum_mohm"], arr_x(f["r"]["t_mean"]))
                              for r, f in good if _r_ok(f) and f["r"].get("sum_mohm")],
                    log=True, prior=R_ARRHENIUS_B_PRIOR, min_dx=3.5e-5)
    trend("r_fast", [(r["start_ts"], f["r"]["pack_fast_mohm"], arr_x(f["r"]["t_mean"]))
                     for r, f in good if _r_ok(f) and f["r"].get("pack_fast_mohm")],
          log=True, prior=R_ARRHENIUS_B_PRIOR, min_dx=3.5e-5)
    arr_b = _g(tr_rsum, "coef", "value", default=R_ARRHENIUS_B_PRIOR)
    views = [(r, f, block_view(f, arr_b)) for r, f in good]
    views = [(r, f, v) for r, f, v in views if v]
    tr_rspread = trend("r_spread", [(r["start_ts"], v["r_max_pct"], None) for r, f, v in views
                                    if v["r_max_pct"] is not None and _r_ok(f)])
    tr_weak = trend("weak_block", [(r["start_ts"], v["weak_dev25_mv"], None) for r, f, v in views])
    tr_bal = trend("balance_rest", [(r["start_ts"], f["bal"]["fit"]["spread0_mv"], None) for r, f, v in views])
    tr_soc = trend("soc_per_ah", [(r["start_ts"], f["soc"]["pct_per_ah"], (f["r"]["t_mean"] or 0) - T_REF_C)
                                  for r, f in good if _g(f, "soc", "pct_per_ah") and _g(f, "r", "t_mean") is not None],
                   min_dx=3.0, prior=0.0)
    trend("heat_k_per_ah", [(r["start_ts"], f["temp"]["heat_k_per_ah"], None)
                            for r, f in good if _g(f, "temp", "heat_k_per_ah") is not None])
    trend("dt_intake", [(r["start_ts"], f["temp"]["dt_intake_mean"], None)
                        for r, f in good if _g(f, "temp", "dt_intake_mean") is not None])
    trend("spread_all", [(r["start_ts"], f["bal"]["spread_all_mean_mv"], None)
                         for r, f in feats if _g(f, "bal", "spread_all_mean_mv") is not None], min_points=6)
    tr_tmax = trend("t_max", [(r["start_ts"], f["temp"]["t_max"], None)
                              for r, f in feats if _g(f, "temp", "t_max") is not None], normalise=False)

    # ── rests: HV pairs ──────────────────────────────────────────────────────
    pairs = []
    hv_rides = [(r, f) for r, f in feats if _g(f, "ocv", "on") or _g(f, "ocv", "off")]
    for (ra, fa), (rb, fb) in zip(hv_rides, hv_rides[1:]):
        p = pair_rest(fa, fb, (rb["start_ts"] - ra["end_ts"]) / 3600.0,
                      (rb["start_ts"] + rb["end_ts"] - ra["start_ts"] - ra["end_ts"]) / 7200.0)
        if p:
            p["ts"] = round(rb["start_ts"])
            p["ride_id"] = rb["id"]
            if p["sd_rel_mv_per_day"]:
                k = min(range(N_BLOCKS), key=lambda j: p["sd_rel_mv_per_day"][j])
                p["worst_block"] = k + 1
                p["worst_mv_per_day"] = p["sd_rel_mv_per_day"][k]
            pairs.append(p)
    long_pairs = [p for p in pairs if p["rest_h"] >= MIN_REST_SD_H and p["sd_rel_mv_per_day"]]
    # Charge-imbalance drift: Theil-Sen slope of each block's zero-current
    # offset over the last 90 days. Only a real divergence accumulates; the
    # ride-to-ride differences above also carry a temperature-dependent fit
    # artefact of a high-resistance block (cold start after a long rest), so
    # they serve only until there is enough history for the slope.
    recent_v = [(r, v) for r, f, v in views if r["start_ts"] >= now - 90 * DAY]
    drift = [analyse_trend([(r["start_ts"], v["dev0_mv"][k], None) for r, v in recent_v], series_max=1)
             for k in range(N_BLOCKS)]
    sd_block = None
    if all(d["slope_30d"] is not None for d in drift):
        per = [d["slope_30d"] / 30.0 for d in drift]
        kmin = min(range(N_BLOCKS), key=lambda k: per[k])
        sd_block = {"block": kmin + 1, "mv_per_day": rnd(per[kmin], 2), "per_block": [rnd(x, 2) for x in per],
                    "n": len(recent_v), "method": "drift", "significant": drift[kmin]["significant"],
                    "lo": rnd(drift[kmin]["lo_30d"] / 30.0, 2), "hi": rnd(drift[kmin]["hi_30d"] / 30.0, 2)}
    elif long_pairs:
        recent = long_pairs[-10:]
        sd_med = [S.median([p["sd_rel_mv_per_day"][k] for p in recent]) for k in range(N_BLOCKS)]
        kmin = min(range(N_BLOCKS), key=lambda k: sd_med[k])
        sd_block = {"block": kmin + 1, "mv_per_day": rnd(sd_med[kmin], 2),
                    "per_block": [rnd(x, 2) for x in sd_med], "n": len(recent), "method": "pairs"}
    tr_sd = trend("self_discharge", [(p["ts"], p["worst_mv_per_day"], None) for p in long_pairs])

    # ── 12 V trends ──────────────────────────────────────────────────────────
    rest_rows = []
    for r in rests:
        f = r["features"]
        if not f or not f.get("n_min"):
            continue
        v6 = _g(f, "marks", "6")
        rested = v6 if v6 is not None else (f.get("v_end") if (f.get("dur_h") or 0) >= 6 else None)
        rest_rows.append({
            "id": r["id"], "start_ts": round(r["start_ts"]), "end_ts": round(r["end_ts"]),
            "open": not r["closed"], "source": r["source"], "dur_h": f.get("dur_h"),
            "marks": f.get("marks"), "v_min": f.get("v_min"), "v_end": f.get("v_end"),
            "v_rested": rested, "ma_mean": f.get("ma_mean"), "mah": f.get("mah"),
            "temp_mean": f.get("temp_mean"), "low_min": f.get("low_min"), "crit_min": f.get("crit_min"),
            "slope_mv_per_h": f.get("slope_mv_per_h"), "cap_proxy_ah": f.get("cap_proxy_ah"),
            "coverage": f.get("coverage"), "acc_min": f.get("acc_min"),
        })
    tr_aux_rest = trend("aux_rest_v", [(x["end_ts"], x["v_rested"], (x["temp_mean"] if x["temp_mean"] is not None else 15.0) - 15.0)
                                       for x in rest_rows if x["v_rested"] is not None],
                        min_dx=4.0, prior=0.0, min_points=6)
    trend("aux_min_v", [(x["end_ts"], x["v_min"], None) for x in rest_rows
                        if x["v_min"] is not None and (x["dur_h"] or 0) >= 1], min_points=6)
    tr_aux_chg = trend("aux_ready_v", [(r["start_ts"], f["aux"]["ready_v_obd"], None)
                                       for r, f in feats if _g(f, "aux", "ready_v_obd") is not None], min_points=6)
    tr_drain = trend("aux_drain_ma", [(x["end_ts"], x["ma_mean"], None) for x in rest_rows
                                      if x["ma_mean"] is not None and (x["dur_h"] or 0) >= 1], min_points=6)
    trend("aux_wake_sag", [(r["start_ts"], f["aux"]["wake_sag_v"], None)
                           for r, f in feats if _g(f, "aux", "wake_sag_v") is not None], min_points=6)
    trend("aux_cap_proxy", [(x["end_ts"], x["cap_proxy_ah"], None) for x in rest_rows
                            if x["cap_proxy_ah"] is not None], min_points=6)
    offs = [f["aux"]["pb_offset"] for _, f in feats if _g(f, "aux", "pb_offset") is not None]
    pb_offset = S.median(offs[-20:]) if offs else None

    # ── blocks ───────────────────────────────────────────────────────────────
    blocks = _blocks(views, feats, arr_b)

    # ── indicators ───────────────────────────────────────────────────────────
    ind: List[dict] = []

    def add(key, title, value, unit, lvl, basis, can, cannot, tr=None, extra=None, sigma=None, n=None):
        if tr is not None and tr.get("significant") and lvl == "ok":
            th = THRESHOLDS.get(extra.get("th") if extra else key) or THRESHOLDS.get(key)
            if th:
                worse = tr["slope_30d"] > 0 if th["dir"] == "high" else tr["slope_30d"] < 0
                # "trend_min": smallest slope per 30 days that counts (for a log
                # trend a fraction); below it a significant slope is noted only
                if worse and abs(tr["slope_30d"]) >= th.get("trend_min", 0.0):
                    lvl = "watch"
                    basis += "; significant worsening trend"
        tkey = (extra or {}).get("trend_key", key if key in trends else None)
        ind.append({"key": key, "title": title, "value": rnd(value, 2), "unit": unit, "level": lvl,
                    "sigma": rnd(sigma if sigma is not None else (tr or {}).get("value_sigma"), 2),
                    "n": n if n is not None else (tr or {}).get("n", 0),
                    "basis": basis, "can": can, "cannot": cannot,
                    "threshold": THRESHOLDS.get((extra or {}).get("th", key)),
                    "trend": tkey, **({k: v for k, v in (extra or {}).items() if k not in ("th", "trend_key")})})

    pers = blocks.get("persistent")
    add("weak_block", "Weakest block under load", tr_weak["value"], "mV",
        level("weak_block", tr_weak["value"]),
        "lowest block against the pack median at 50 A discharge, 25 °C; median of the last 5 graded rides",
        "a module pair that sags under load: the Gen2 failure pattern. A persistent block here is the one to watch"
        + ("; block %d was the weakest in %d of the last %d rides" % (pers["block"], pers["count"], pers["of"]) if pers else ""),
        "says nothing about which of the two modules of the block is weak; a few tens of mV between blocks are normal",
        tr_weak, {"block": pers["block"] if pers else None, "trend_key": "weak_block"})
    add("r_spread", "Block resistance spread", tr_rspread["value"], "%",
        level("r_spread", tr_rspread["value"]),
        "highest block resistance above the median block; ratio, so insensitive to temperature",
        "one block ageing faster than the rest (resistance rise precedes the voltage codes)",
        "not the absolute resistance; sensor-wire or bus-bar corrosion at one block looks the same",
        tr_rspread, {"block": blocks.get("r_max_block")})
    r_base, r_val = tr_rsum.get("baseline"), tr_rsum.get("value")
    r_rise = 100.0 * (r_val / r_base - 1.0) if r_val and r_base and tr_rsum["n"] >= 10 else None
    add("r_pack", "Pack resistance at 25 °C", r_val, "mΩ",
        level("r_pack", r_rise) if r_rise is not None else ("unknown" if r_val is None else "ok"),
        "sum of the 14 block resistances from current steps over 2 s, normalised to 25 °C (%s temperature coefficient); level = rise over this pack's own first rides" % (
            _g(tr_rsum, "coef", "source", default="prior")),
        "ageing of the whole pack (power capability); read the trend, not the single value",
        "a 2 s value includes polarisation, so it is higher than the ECU's own figure; until a full temperature range is seen the normalisation rests on a prior and winter may still show as a rise",
        tr_rsum, {"trend_key": "r_sum", "rise_pct": rnd(r_rise, 1), "ecu_mohm": blocks.get("ecu_sum_mohm"),
                  "fast_mohm": trends["r_fast"].get("value")})
    jumps = [f["soc"]["jumps"] / max(0.1, (f.get("dur_s") or 0) / 3600.0)
             for _, f in good[-10:] if _g(f, "soc", "jumps") is not None]
    add("soc_stability", "SOC movement per Ah", tr_soc["value"], "%/Ah",
        level("soc_stability", tr_soc["value"]),
        "slope of the ECU's SOC against integrated ampere-hours within a ride; nominal %.1f %%/Ah" % (100.0 / NOMINAL_AH),
        "a pack whose usable capacity has shrunk: the SOC swings faster than the ampere-hours explain, the ECU corrects it in jumps",
        "not a capacity measurement: the car holds SOC in a narrow window and the SOC itself is the ECU's estimate, mostly its own ampere-hour count",
        tr_soc, {"trend_key": "soc_per_ah", "implied_ah": rnd(100.0 / tr_soc["value"], 2) if tr_soc["value"] else None,
                 "jumps_per_h": rnd(S.median(jumps), 2) if jumps else None})
    add("balance_rest", "Block balance at rest", tr_bal["value"], "mV",
        level("balance_rest", tr_bal["value"]),
        "spread (max - min) of the block voltage offsets at zero current, from the per-ride fit",
        "state-of-charge imbalance between blocks (unequal self-discharge or capacity)",
        "12-cell blocks hide a single weak cell until it is far gone",
        tr_bal)
    add("self_discharge", "Charge imbalance drift", sd_block["mv_per_day"] if sd_block else None, "mV/day",
        level("self_discharge", sd_block["mv_per_day"] if sd_block else None),
        ("slope of each block's zero-current voltage offset (against the pack median) over the last 90 days; the fastest-falling block"
         if sd_block and sd_block["method"] == "drift" else
         "provisional (under 3 weeks of history): change of the zero-current offset from one ride to the next over rests of at least %g h, median of the last 10" % MIN_REST_SD_H),
        "a block that keeps losing charge against the others: higher self-discharge (internal leakage) or lower capacity",
        "does not separate loss while parked from divergence while driving; the whole pack's self-discharge is not measured (the pack voltage after a rest is dominated by relaxation and temperature)",
        None, {"block": sd_block["block"] if sd_block else None, "method": sd_block["method"] if sd_block else None,
               "trend_key": "self_discharge"}, n=sd_block["n"] if sd_block else 0)
    add("thermal", "Battery temperature", tr_tmax["value"], "°C",
        level("thermal", tr_tmax["value"]),
        "highest battery temperature of a ride; median of the last 5 rides",
        "heat stress and cooling problems (fan, intake filter): compare with the intake temperature and the gradient",
        "without the full 21CF answer only two coarse sensors (1 °C) are read; module-level hot spots are invisible",
        None, {"grad": rnd(S.median([f["temp"]["grad_max"] for _, f in feats[-5:] if _g(f, "temp", "grad_max") is not None]), 1),
               "trend_key": "t_max"}, n=tr_tmax["n"])
    v_rest = tr_aux_rest["value"]
    v_batt = v_rest + pb_offset if v_rest is not None and pb_offset is not None else v_rest
    add("aux_rest", "12 V rested voltage", v_batt, "V", level("aux_rest", v_batt),
        "powerbox voltage 6 h after key-off (or at the end of a rest of 6 h or more)%s; the computer draws current the whole time" % (
            ", plus the %.2f V offset to the ECM reading" % pb_offset if pb_offset is not None else ""),
        "a 12 V battery that no longer holds its charge between drives: the classic no-READY cause",
        "not an open-circuit voltage (0.1-0.4 A load); cold lowers it; a short drive before leaves a surface charge",
        tr_aux_rest, {"trend_key": "aux_rest_v", "pb_offset": rnd(pb_offset, 3),
                      "v_min_recent": rnd(min([x["v_min"] for x in rest_rows[-10:] if x["v_min"] is not None], default=None), 2)})
    lows = [x for x in rest_rows if x["v_min"] is not None and x["end_ts"] >= now - 14 * DAY]
    v_low = min((x["v_min"] for x in lows), default=None)
    add("aux_low", "12 V lowest voltage, 14 days", v_low, "V", level("aux_low", v_low),
        "lowest one-minute minimum at the powerbox between rides in the last 14 days (includes ACC-on time without READY)",
        "how close the 12 V battery came to not starting the car; %d min below %.1f V in that time" % (
            sum(x["low_min"] or 0 for x in lows), V12_LOW),
        "a dip under a brief load (ACC accessories, the computer booting) counts like a slow sag: open the rest to see which",
        None, {"trend_key": "aux_min_v"}, n=len(lows))
    add("aux_charge", "12 V charge voltage in READY", tr_aux_chg["value"], "V",
        level("aux_charge", tr_aux_chg["value"]),
        "ECM supply voltage (OBD 0142), median over READY",
        "DC-DC converter output and wiring: a low value means the 12 V battery is not being charged properly",
        "measured at the ECM, not at the battery posts in the boot",
        None, {"trend_key": "aux_ready_v"}, n=tr_aux_chg["n"])
    drain = tr_drain["value"]
    add("aux_drain", "Parked drain of the computer", drain, "mA",
        "unknown" if drain is None else "ok",
        "mean current of the car computer while parked (powerbox INA219)",
        "how fast the computer alone empties the 12 V battery: %s Ah per day" % (
            rnd(drain * 24 / 1000.0, 2) if drain is not None else "?"),
        "the car's own quiescent current is not measured (it does not flow through the powerbox)",
        None, {"ah_per_day": rnd(drain * 24 / 1000.0, 2) if drain is not None else None,
               "trend_key": "aux_drain_ma"}, n=tr_drain["n"])

    # ── usage, limits, quality ───────────────────────────────────────────────
    usage = _usage(feats)
    quality = _quality(feats, rests, now)
    recent = [_ride_row(r, f) for r, f in feats[-200:]][::-1]
    stats = store.stats()
    order = {"act": 3, "watch": 2, "ok": 1, "unknown": 0}
    hv = [i for i in ind if not i["key"].startswith("aux")]
    aux = [i for i in ind if i["key"].startswith("aux")]
    return {
        "generated_ts": round(now, 1), "analysis_ver": ANALYSIS_VERSION, "schema": SCHEMA_VERSION,
        "compute_s": round(time.perf_counter() - t_start, 3),
        "overall": {"hv": max((i["level"] for i in hv), key=lambda x: order[x], default="unknown"),
                    "aux": max((i["level"] for i in aux), key=lambda x: order[x], default="unknown")},
        "indicators": ind, "trends": trends, "blocks": blocks,
        "rest_pairs": pairs[-120:], "self_discharge": sd_block,
        "aux": {"rests": rest_rows[-200:][::-1], "pb_offset": rnd(pb_offset, 3)},
        "usage": usage, "quality": quality, "rides": recent, "stats": stats,
        "thresholds": THRESHOLDS,
    }


def _blocks(views, feats, arr_b: float = R_ARRHENIUS_B_PRIOR) -> dict:
    out: dict = {"latest": None, "heatmaps": {}, "weakest": [], "weak_share": None, "persistent": None,
                 "r_max_block": None, "ecu_sum_mohm": None, "block_trends": None}
    ecu = [f["ecu"]["r_mohm"] for _, f in feats if _g(f, "ecu", "r_mohm")]
    if ecu:
        out["ecu_latest_mohm"] = ecu[-1]
        out["ecu_sum_mohm"] = rnd(sum(ecu[-1]), 1)
    if not views:
        return out
    r, f, v = views[-1]
    out["arrhenius_b"] = rnd(arr_b, 0)
    out["latest"] = {"ride_id": r["id"], "ts": round(r["start_ts"]), "t_mean": f["r"].get("t_mean"),
                     "dev0_mv": v["dev0_mv"], "dev25_mv": v["dev25_mv"], "dr_pct": v["dr_pct"],
                     "r_mohm": f["r"].get("blk_mohm"), "r_se": f["r"].get("blk_se"),
                     "ecu_mohm": _g(f, "ecu", "r_mohm"),
                     "rest_dev_mv": _g(f, "bal", "rest", "dev_mv"), "dis_dev_mv": _g(f, "bal", "dis", "dev_mv"),
                     "chg_dev_mv": _g(f, "bal", "chg", "dev_mv")}
    out["weakest"] = [[round(r["start_ts"]), r["id"], v["weak_block"], v["weak_dev25_mv"]]
                      for r, f, v in views[-200:]]
    last = views[-20:]
    cnt = [0] * N_BLOCKS
    for _, _, v in last:
        cnt[v["weak_block"] - 1] += 1
    out["weak_share"] = [rnd(c / len(last), 3) for c in cnt]
    top = max(range(N_BLOCKS), key=lambda k: cnt[k])
    out["persistent"] = {"block": top + 1, "count": cnt[top], "of": len(last)}
    rmax = [v["r_max_block"] for _, _, v in last if v["r_max_block"]]
    if rmax:
        out["r_max_block"] = max(set(rmax), key=rmax.count)

    metrics = ("dev25_mv", "dev0_mv", "dr_pct")
    # per ride (last 60)
    cols = views[-60:]
    out["heatmaps"]["rides"] = {
        "cols": [{"ts": round(r["start_ts"]), "ride_id": r["id"], "n": 1} for r, _, _ in cols],
        **{m: [[(v[m][k] if v[m] else None) for _, _, v in cols] for k in range(N_BLOCKS)] for m in metrics}}
    # per week (all history, last 156 weeks)
    weeks: Dict[int, list] = {}
    for r, f, v in views:
        weeks.setdefault(int(r["start_ts"] // (7 * DAY)), []).append(v)
    wk = sorted(weeks)[-156:]
    out["heatmaps"]["weeks"] = {
        "cols": [{"ts": w * 7 * DAY, "n": len(weeks[w])} for w in wk],
        **{m: [[rnd(S.median([v[m][k] for v in weeks[w] if v[m]]), 2) for w in wk]
               for k in range(N_BLOCKS)] for m in metrics}}
    # per-block slopes (deviation at load and resistance), light version
    bt = []
    for k in range(N_BLOCKS):
        t1 = analyse_trend([(r["start_ts"], v["dev25_mv"][k], None) for r, _, v in views], series_max=1)
        t2 = analyse_trend([(r["start_ts"], v["dr_pct"][k], None) for r, _, v in views if v["dr_pct"]], series_max=1)
        bt.append({"block": k + 1,
                   "dev25": {"value": t1["value"], "slope_30d": t1["slope_30d"], "significant": t1["significant"]},
                   "dr_pct": {"value": t2["value"], "slope_30d": t2["slope_30d"], "significant": t2["significant"]}})
    out["block_trends"] = bt
    return out


def _usage(feats) -> dict:
    tot = {"rides": 0, "hours": 0.0, "km": 0.0, "ah_thru": 0.0, "kwh_thru": 0.0, "near_limit_s": 0.0}
    hist: Optional[List[float]] = None
    months: Dict[str, dict] = {}
    limits = []
    for r, f in feats:
        if _g(f, "quality", "grade") == "X":
            continue
        st = f.get("stress") or {}
        tot["rides"] += 1
        tot["hours"] += (f.get("dur_s") or 0) / 3600.0
        tot["km"] += f.get("dist_km") or 0.0
        tot["ah_thru"] += st.get("ah_thru") or 0.0
        tot["kwh_thru"] += ((st.get("wh_dis") or 0.0) + (st.get("wh_chg") or 0.0)) / 1000.0
        tot["near_limit_s"] += st.get("near_limit_s") or 0.0
        if st.get("hist_s"):
            hist = [a + b for a, b in zip(hist, st["hist_s"])] if hist else list(st["hist_s"])
        key = time.strftime("%Y-%m", time.gmtime(r["start_ts"]))
        m = months.setdefault(key, {"month": key, "rides": 0, "ah_thru": 0.0, "hours": 0.0, "km": 0.0,
                                    "frac_low": [], "frac_high": [], "half_cycles": 0})
        m["rides"] += 1
        m["ah_thru"] += st.get("ah_thru") or 0.0
        m["hours"] += (f.get("dur_s") or 0) / 3600.0
        m["km"] += f.get("dist_km") or 0.0
        m["half_cycles"] += _g(f, "soc", "half_cycles_10", default=0) or 0
        if _g(f, "soc", "frac_low") is not None:
            m["frac_low"].append(f["soc"]["frac_low"])
            m["frac_high"].append(f["soc"]["frac_high"])
        if st.get("ccl_min") is not None and _g(f, "temp", "t_mean") is not None:
            limits.append([f["temp"]["t_mean"], st["ccl_min"], st["cdl_min"]])
    mlist = []
    for key in sorted(months):
        m = months[key]
        mlist.append({"month": key, "rides": m["rides"], "ah_thru": rnd(m["ah_thru"], 1),
                      "hours": rnd(m["hours"], 1), "km": rnd(m["km"], 0), "half_cycles": m["half_cycles"],
                      "frac_low": rnd(S.mean(m["frac_low"]), 3), "frac_high": rnd(S.mean(m["frac_high"]), 3)})
    return {"total": {k: rnd(v, 1) for k, v in tot.items()},
            "full_cycles_equiv": rnd(tot["ah_thru"] / (2 * NOMINAL_AH), 0),
            "hist_s": [rnd(x, 0) for x in hist] if hist else None,
            "months": mlist[-36:], "limits": limits[-300:]}


def _quality(feats, rests, now) -> dict:
    grades: Dict[str, int] = {}
    flags: Dict[str, int] = {}
    for _, f in feats:
        g = _g(f, "quality", "grade", default="?")
        grades[g] = grades.get(g, 0) + 1
        for fl in _g(f, "quality", "flags", default=[]):
            flags[fl] = flags.get(fl, 0) + 1
    n = len(feats) or 1
    last = feats[-1] if feats else None
    cov = [r["features"].get("coverage") for r in rests if r["features"].get("coverage") is not None]
    rates = [f["blk_rate_hz"] for _, f in feats[-50:] if f.get("blk_rate_hz")]
    return {
        "grades": grades, "flags": flags, "rides": len(feats),
        "with_blocks": sum(1 for _, f in feats if (f.get("n_blk") or 0) > 0),
        "with_pack_stream": sum(1 for _, f in feats if (f.get("n_sec") or 0) > 0),
        "with_21cf_full": sum(1 for _, f in feats if _g(f, "ecu", "cf_full")),
        "with_21d0": sum(1 for _, f in feats if _g(f, "ecu", "r_mohm")),
        "lite": sum(1 for r, _ in feats if r["source"].startswith("metrics1m")),
        "blk_rate_hz": rnd(S.median(rates), 3),
        "last_ride_ts": round(last[0]["end_ts"]) if last else None,
        "last_ride_age_h": rnd((now - last[0]["end_ts"]) / 3600.0, 1) if last else None,
        "rests": len(rests), "rest_coverage_median": rnd(S.median(cov), 3),
        "grade_meaning": {
            "A": "at least 5 min, enough block samples and current steps, no flag: used for all trends",
            "B": "usable with a flag (cold, gappy polls, no broadcast stream, truncated)",
            "C": "too short or too few current steps: counted for usage only",
            "X": "under 1 min or no battery data: ignored",
            "L": "known only from the 1-min metrics (backfill): no block data",
        },
    }


def _ride_row(r: dict, f: dict) -> dict:
    fit = _g(f, "bal", "fit") or {}
    return {
        "id": r["id"], "start_ts": round(r["start_ts"]), "dur_s": f.get("dur_s"), "source": r["source"],
        "dist_km": f.get("dist_km"), "grade": _g(f, "quality", "grade"), "flags": _g(f, "quality", "flags", default=[]),
        "t_mean": _g(f, "temp", "t_mean"), "soc_start": _g(f, "soc", "start"), "soc_end": _g(f, "soc", "end"),
        "soc_min": _g(f, "soc", "min"), "soc_max": _g(f, "soc", "max"),
        "ah_thru": _g(f, "stress", "ah_thru"), "spread_ref_mv": fit.get("spread_ref_mv"),
        "spread0_mv": fit.get("spread0_mv"), "weak_block": fit.get("weak_block"),
        "weak_dev_mv": fit.get("weak_dev_mv"), "r_sum_mohm": _g(f, "r", "sum_mohm"),
        "r_sum25_mohm": _g(f, "r", "sum25_mohm"), "max_rel": _g(f, "r", "max_rel"),
        "pct_per_ah": _g(f, "soc", "pct_per_ah"), "ready_v": _g(f, "aux", "ready_v_obd"),
        "n_steps": _g(f, "r", "n_steps"), "n_blk": f.get("n_blk"),
    }


# ── per-ride detail for the UI ──────────────────────────────────────────────

def ride_detail(row: dict, record: Optional[dict], buckets: int = 180,
                arr_b: float = R_ARRHENIUS_B_PRIOR) -> dict:
    """Features plus chart-ready decimated series of one ride."""
    f = row["features"]
    out = {"id": row["id"], "start_ts": row["start_ts"], "end_ts": row["end_ts"], "source": row["source"],
           "features": f, "view": block_view(f, arr_b), "series": None}
    if record is None:
        return out
    tb = record["tables"]
    blk, sec, slow = tb["blk"], tb["sec"], tb["slow"]
    dur = max(1.0, row["end_ts"] - row["start_ts"])
    w = dur / buckets
    # pack: current mean/min/max and voltage per bucket
    pk: List[Optional[list]] = [None] * buckets
    for k in range(len(sec["t"])):
        b = min(buckets - 1, int(sec["t"][k] / w))
        e = pk[b]
        i, lo, hi, v = sec["i"][k] / 10.0, sec["imin"][k] / 10.0, sec["imax"][k] / 10.0, sec["v"][k] / 10.0
        if e is None:
            pk[b] = [i, lo, hi, v, 1]
        else:
            e[0] += i
            e[1] = min(e[1], lo)
            e[2] = max(e[2], hi)
            e[3] += v
            e[4] += 1
    # blocks: deviation from the median per bucket
    bd: List[Optional[list]] = [None] * buckets
    for k in range(len(blk["t"])):
        b = min(buckets - 1, int(blk["t"][k] / 10.0 / w))
        vs = [blk["v%d" % (j + 1)][k] / 100.0 for j in range(N_BLOCKS)]
        med = S.median(vs)
        e = bd[b]
        if e is None:
            e = bd[b] = [[0.0] * N_BLOCKS, 0, 0.0, 0.0]
        for j in range(N_BLOCKS):
            e[0][j] += vs[j] - med
        e[1] += 1
        e[2] += blk["i"][k] / 100.0
        e[3] = max(e[3], max(vs) - min(vs))
    sl = {"t": slow["t"]}
    for name, scale in (("soc2", 2), ("t1", 1), ("t2", 1), ("ccl", 1), ("cdl", 1), ("speed", 1),
                        ("intake", 100), ("fan_v", 10), ("aux_obd", 1000), ("pb_v", 1000),
                        ("tb1", 100), ("tb2", 100), ("tb3", 100)):
        sl[name] = [None if x == -32768 else round(x / scale, 2) for x in slow.get(name, [])]
    # scatter of the weakest and the strongest block against current (decimated)
    view = out["view"]
    scatter = None
    if view and blk["t"]:
        kw = view["weak_block"] - 1
        ks = max(range(N_BLOCKS), key=lambda k: view["dev25_mv"][k])
        step = max(1, len(blk["t"]) // 400)
        pts = []
        for k in range(0, len(blk["t"]), step):
            vs = [blk["v%d" % (j + 1)][k] / 100.0 for j in range(N_BLOCKS)]
            med = S.median(vs)
            pts.append([round(blk["i"][k] / 100.0, 1), round(1000 * (vs[kw] - med)), round(1000 * (vs[ks] - med))])
        scatter = {"weak": kw + 1, "strong": ks + 1, "pts": pts}
    out["series"] = {
        "bucket_s": round(w, 2),
        "pack": [None if e is None else [round(e[0] / e[4], 1), e[1], e[2], round(e[3] / e[4], 1)] for e in pk],
        "dev_mv": [None if e is None else [round(1000 * x / e[1]) for x in e[0]] for e in bd],
        "blk_i": [None if e is None else round(e[2] / e[1], 1) for e in bd],
        "spread_mv": [None if e is None else round(1000 * e[3]) for e in bd],
        "slow": sl, "scatter": scatter,
        "wake": {"t": [x / 10.0 for x in tb.get("wake", {}).get("t", [])],
                 "v": [x / 1000.0 for x in tb.get("wake", {}).get("v", [])]},
    }
    return out


def rest_detail(row: dict, record: Optional[dict], max_points: int = 360) -> dict:
    out = {"id": row["id"], "start_ts": row["start_ts"], "end_ts": row["end_ts"], "source": row["source"],
           "closed": row["closed"], "features": row["features"], "series": None}
    if record is None:
        return out
    m = record["tables"]["min"]
    n = len(m["t"])
    step = max(1, n // max_points)
    ser = {"t_min": [], "v": [], "vmin": [], "ma": [], "temp": [], "acc": []}
    for k in range(0, n, step):
        seg = range(k, min(n, k + step))
        ser["t_min"].append(m["t"][k])
        ser["v"].append(round(sum(m["v"][j] for j in seg) / len(seg) / 1000.0, 3))
        ser["vmin"].append(round(min(m["vmin"][j] for j in seg) / 1000.0, 3))
        mas = [m["ma"][j] / 10.0 for j in seg if m["ma"][j] >= 0]
        ser["ma"].append(round(sum(mas) / len(mas), 1) if mas else None)
        ts = [m["temp"][j] / 100.0 for j in seg if m["temp"][j] != -32768]
        ser["temp"].append(round(sum(ts) / len(ts), 1) if ts else None)
        ser["acc"].append(1 if any(m["acc"][j] for j in seg) else 0)
    out["series"] = ser
    return out
