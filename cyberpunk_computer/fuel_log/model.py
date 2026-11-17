"""
Fuelling-log maths (pure functions, no I/O). Formulas: docs/FUEL_LOG.md.

Method: full-to-full per fuel. A fill of fuel F that is not full is added to
the next full fill of F; fills of F before the first full fill of F cannot be
attributed and are left out. Between two consecutive full fills of F the
litres added (after the first, up to and including the second) are what the
car burnt of F over the odometer distance between them.

The car burns both fuels on the same kilometres (cold start and warm-up on
petrol, then LPG), so every per-fuel figure is litres of THAT fuel per 100 km
of TOTAL driving. Such rates add: the combined energy-equivalent figure is

    petrol_eq_l100 = petrol_l100 + LPG_TO_PETROL_ENERGY * lpg_l100

Each fuel is measured over its OWN window (its first to its last full fill);
a strict common window is not measurable in general because the litres of a
fuel are known only at its own full fills. The overlap of the two windows is
reported so the reader sees how comparable they are.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

FUELS = ("lpg", "petrol")

#: Volumetric energy ratio LPG : petrol (lower heating values). Autogas
#: (propane/butane mix) ~46 MJ/kg x ~0.54 kg/l = ~24.8 MJ/l; petrol (E5)
#: ~32.2 MJ/l -> 0.77 (literature range 0.74..0.80 with the mix and season).
#: Override with the environment variable PRIUS_LPG_PETROL_ENERGY.
LPG_TO_PETROL_ENERGY = 0.77

#: Rolling averages use the last N full-to-full segments of each fuel.
ROLLING_N = 3

#: A segment shorter than this gives no per-segment l/100 km of its own
#: (a refill after a few km is noise); its litres and distance still count
#: in the rolling and overall sums.
MIN_SEGMENT_KM = 20.0


def energy_factor() -> float:
    raw = os.environ.get("PRIUS_LPG_PETROL_ENERGY")
    if raw:
        try:
            v = float(raw)
            if 0.5 <= v <= 1.0:
                return v
        except ValueError:
            pass
    return LPG_TO_PETROL_ENERGY


def _r(v: Optional[float], nd: int = 2) -> Optional[float]:
    return None if v is None else round(float(v), nd)


def _per100(litres: float, km: float) -> Optional[float]:
    return litres / km * 100.0 if km and km > 0 else None


def sort_entries(entries: Iterable[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """Chronological order of the fills: odometer, then time, then id."""
    return sorted(entries, key=lambda e: (float(e["odometer_km"]), float(e["ts"]), int(e.get("id") or 0)))


def segments(entries: Sequence[Mapping[str, Any]], fuel: str) -> Dict[str, Any]:
    """Full-to-full segments of ``fuel``.

    Returns ``{"segments": [...], "pending_l": litres added after the last
    full fill (partials waiting for the next full one), "pending_fills": n,
    "unattributed_l": litres before the first full fill}``.
    """
    es = [e for e in sort_entries(entries) if e["fuel"] == fuel]
    segs: List[Dict[str, Any]] = []
    prev: Optional[Mapping[str, Any]] = None
    acc_l = 0.0
    acc_priced_l = 0.0
    acc_cost = 0.0
    fills = 0
    unattributed = 0.0
    for e in es:
        litres = float(e["litres"])
        if prev is None:
            if e.get("full"):
                prev = e
            else:
                unattributed += litres
            continue
        acc_l += litres
        fills += 1
        price = e.get("price_per_l")
        if price is not None:
            acc_priced_l += litres
            acc_cost += litres * float(price)
        if e.get("full"):
            d = float(e["odometer_km"]) - float(prev["odometer_km"])
            price_avg = acc_cost / acc_priced_l if acc_priced_l > 0 else None
            est_l = None
            if prev.get("est_total_l") is not None and e.get("est_total_l") is not None:
                est_l = float(e["est_total_l"]) - float(prev["est_total_l"])
            segs.append({
                "fuel": fuel,
                "from_id": prev.get("id"), "to_id": e.get("id"),
                "from_ts": prev["ts"], "to_ts": e["ts"],
                "from_km": float(prev["odometer_km"]), "to_km": float(e["odometer_km"]),
                "distance_km": d,
                "litres": acc_l,
                "fills": fills,
                "l_per_100km": _per100(acc_l, d) if d >= MIN_SEGMENT_KM else None,
                "price_per_l": price_avg,
                "price_coverage": (acc_priced_l / acc_l) if acc_l > 0 else None,
                "cost": acc_l * price_avg if price_avg is not None else None,
                "est_l": est_l,
            })
            prev = e
            acc_l = acc_priced_l = acc_cost = 0.0
            fills = 0
    return {"segments": segs, "pending_l": acc_l, "pending_fills": fills,
            "unattributed_l": unattributed, "last_full": prev}


def aggregate(segs: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Sum of segments: litres / distance (not a mean of ratios)."""
    if not segs:
        return None
    km = sum(s["distance_km"] for s in segs)
    litres = sum(s["litres"] for s in segs)
    priced = [s for s in segs if s["price_per_l"] is not None]
    priced_l = sum(s["litres"] for s in priced)
    price = (sum(s["litres"] * s["price_per_l"] for s in priced) / priced_l) if priced_l > 0 else None
    est = [s for s in segs if s.get("est_l") is not None]
    est_km = sum(s["distance_km"] for s in est)
    return {
        "segments": len(segs),
        "from_km": segs[0]["from_km"], "to_km": segs[-1]["to_km"],
        "from_ts": segs[0]["from_ts"], "to_ts": segs[-1]["to_ts"],
        "distance_km": km, "litres": litres,
        "l_per_100km": _per100(litres, km),
        "price_per_l": price,
        "cost_per_100km": (_per100(litres, km) * price) if (price is not None and km > 0) else None,
        # Backend estimate (petrol-equivalent, BOTH fuels) over the same km.
        "est_l_per_100km": _per100(sum(s["est_l"] for s in est), est_km) if est_km > 0 else None,
        "est_distance_km": est_km,
    }


def _combine(lpg: Optional[Mapping[str, Any]], petrol: Optional[Mapping[str, Any]],
             k: float) -> Dict[str, Any]:
    """Combined figure from per-fuel aggregates (rates over total km)."""
    out: Dict[str, Any] = {"energy_factor": k, "status": "ok", "petrol_eq_l_per_100km": None,
                           "cost_per_100km": None, "lower_bound": False,
                           "overlap_km": None, "est_l_per_100km": None, "measured_over_est": None}
    lr = lpg["l_per_100km"] if lpg else None
    pr = petrol["l_per_100km"] if petrol else None
    if lr is None and pr is None:
        out["status"] = "no full-to-full window yet"
        return out
    if lr is not None and pr is not None:
        out["petrol_eq_l_per_100km"] = pr + k * lr
        if lpg["price_per_l"] is not None and petrol["price_per_l"] is not None:
            out["cost_per_100km"] = pr * petrol["price_per_l"] + lr * lpg["price_per_l"]
        out["overlap_km"] = max(0.0, min(lpg["to_km"], petrol["to_km"]) - max(lpg["from_km"], petrol["from_km"]))
    else:
        # Only one fuel measured: the other contributes >= 0, so this is a
        # lower bound (shown as ">=" on the MFD).
        out["lower_bound"] = True
        out["status"] = ("petrol not measured yet (lower bound)" if pr is None
                         else "LPG not measured yet (lower bound)")
        out["petrol_eq_l_per_100km"] = (k * lr) if lr is not None else pr
        one = lpg if lr is not None else petrol
        if one["price_per_l"] is not None:
            out["cost_per_100km"] = one["l_per_100km"] * one["price_per_l"]
    # Calibration hint: our estimator is petrol-equivalent for BOTH fuels;
    # compare it over the window of the fuel with most measured distance
    # that carries estimator snapshots.
    cands = [a for a in (lpg, petrol) if a and a.get("est_l_per_100km") is not None]
    if cands and not out["lower_bound"]:
        best = max(cands, key=lambda a: a["est_distance_km"])
        out["est_l_per_100km"] = best["est_l_per_100km"]
        if best["est_l_per_100km"]:
            out["measured_over_est"] = out["petrol_eq_l_per_100km"] / best["est_l_per_100km"]
    return out


def _round_tree(obj: Any) -> Any:
    if isinstance(obj, float):
        return round(obj, 3)
    if isinstance(obj, dict):
        return {k: _round_tree(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_tree(v) for v in obj]
    return obj


def compute_figures(entries: Sequence[Mapping[str, Any]], odometer_now: Optional[float] = None,
                    est_total_now: Optional[float] = None, k: Optional[float] = None,
                    rolling_n: int = ROLLING_N) -> Dict[str, Any]:
    """Every figure the state section, the MFD and the docs talk about."""
    k = energy_factor() if k is None else k
    per_fuel: Dict[str, Any] = {}
    for fuel in FUELS:
        seg = segments(entries, fuel)
        segs = seg["segments"]
        mine = [e for e in sort_entries(entries) if e["fuel"] == fuel]
        last = mine[-1] if mine else None
        last_full = seg["last_full"]
        since: Dict[str, Any] = {"km": None, "km_full": None, "est_l": None}
        if odometer_now is not None:
            if last is not None:
                since["km"] = max(0.0, odometer_now - float(last["odometer_km"]))
            if last_full is not None:
                since["km_full"] = max(0.0, odometer_now - float(last_full["odometer_km"]))
        if est_total_now is not None and last is not None and last.get("est_total_l") is not None:
            since["est_l"] = max(0.0, est_total_now - float(last["est_total_l"]))
        per_fuel[fuel] = {
            "fills": len(mine),
            "last_fill": ({"id": last.get("id"), "ts": last["ts"], "litres": last["litres"],
                           "price_per_l": last.get("price_per_l"), "full": bool(last.get("full")),
                           "odometer_km": last["odometer_km"]} if last else None),
            "since_last": since,
            "pending_l": seg["pending_l"], "pending_fills": seg["pending_fills"],
            "unattributed_l": seg["unattributed_l"],
            "last": aggregate(segs[-1:]) if segs else None,
            "rolling": aggregate(segs[-rolling_n:]) if segs else None,
            "overall": aggregate(segs) if segs else None,
            "segments": segs[-10:],
        }
    combined = {
        "last": _combine(per_fuel["lpg"]["last"], per_fuel["petrol"]["last"], k),
        "rolling": _combine(per_fuel["lpg"]["rolling"], per_fuel["petrol"]["rolling"], k),
        "overall": _combine(per_fuel["lpg"]["overall"], per_fuel["petrol"]["overall"], k),
    }
    return _round_tree({
        "method": "full-to-full per fuel; rates are litres of that fuel per 100 km of total driving",
        "energy_factor": k, "rolling_n": rolling_n, "min_segment_km": MIN_SEGMENT_KM,
        "odometer_km": odometer_now,
        "lpg": per_fuel["lpg"], "petrol": per_fuel["petrol"],
        "combined": combined,
    })


def defaults(entries: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Add-flow defaults per fuel: last price, litres and total of that fuel."""
    out: Dict[str, Any] = {}
    es = sort_entries(entries)
    for fuel in FUELS:
        mine = [e for e in es if e["fuel"] == fuel]
        priced = [e for e in mine if e.get("price_per_l") is not None]
        last = mine[-1] if mine else None
        out[fuel] = {
            "litres": float(last["litres"]) if last else (25.0 if fuel == "lpg" else 30.0),
            "price_per_l": float(priced[-1]["price_per_l"]) if priced else None,
            "total": (round(float(priced[-1]["litres"]) * float(priced[-1]["price_per_l"]), 2)
                      if priced else None),
        }
    return out
