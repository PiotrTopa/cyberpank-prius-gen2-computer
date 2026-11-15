"""Exports: CSV tables (one row per ride / rest / block) and the database copy."""

from __future__ import annotations

import csv
import io
from typing import Iterable, List

from .model import N_BLOCKS
from .summary import _g, block_view

RIDE_COLS = [
    ("start_ts", lambda r, f: round(r["start_ts"])), ("end_ts", lambda r, f: round(r["end_ts"])),
    ("source", lambda r, f: r["source"]), ("grade", lambda r, f: _g(f, "quality", "grade")),
    ("flags", lambda r, f: " ".join(_g(f, "quality", "flags", default=[]))),
    ("dur_s", lambda r, f: f.get("dur_s")), ("dist_km", lambda r, f: f.get("dist_km")),
    ("t_batt_mean", lambda r, f: _g(f, "temp", "t_mean")), ("t_batt_max", lambda r, f: _g(f, "temp", "t_max")),
    ("t_grad_max", lambda r, f: _g(f, "temp", "grad_max")), ("t_intake_mean", lambda r, f: _g(f, "temp", "intake_mean")),
    ("fan_on_frac", lambda r, f: _g(f, "temp", "fan_on_frac")), ("heat_k_per_ah", lambda r, f: _g(f, "temp", "heat_k_per_ah")),
    ("soc_start", lambda r, f: _g(f, "soc", "start")), ("soc_end", lambda r, f: _g(f, "soc", "end")),
    ("soc_min", lambda r, f: _g(f, "soc", "min")), ("soc_max", lambda r, f: _g(f, "soc", "max")),
    ("soc_pct_per_ah", lambda r, f: _g(f, "soc", "pct_per_ah")), ("soc_jumps", lambda r, f: _g(f, "soc", "jumps")),
    ("ah_dis", lambda r, f: _g(f, "stress", "ah_dis")), ("ah_chg", lambda r, f: _g(f, "stress", "ah_chg")),
    ("i_max", lambda r, f: _g(f, "stress", "i_max")), ("i_min", lambda r, f: _g(f, "stress", "i_min")),
    ("i_rms", lambda r, f: _g(f, "stress", "i_rms")), ("near_limit_s", lambda r, f: _g(f, "stress", "near_limit_s")),
    ("ccl_min", lambda r, f: _g(f, "stress", "ccl_min")), ("cdl_min", lambda r, f: _g(f, "stress", "cdl_min")),
    ("spread_rest_mv", lambda r, f: _g(f, "bal", "rest", "spread_mean_mv")),
    ("spread_dis_mv", lambda r, f: _g(f, "bal", "dis", "spread_mean_mv")),
    ("spread_chg_mv", lambda r, f: _g(f, "bal", "chg", "spread_mean_mv")),
    ("spread0_mv", lambda r, f: _g(f, "bal", "fit", "spread0_mv")),
    ("spread_ref_mv", lambda r, f: _g(f, "bal", "fit", "spread_ref_mv")),
    ("weak_block", lambda r, f: _g(f, "bal", "fit", "weak_block")),
    ("weak_dev_mv", lambda r, f: _g(f, "bal", "fit", "weak_dev_mv")),
    ("r_steps", lambda r, f: _g(f, "r", "n_steps")), ("r_sum_mohm", lambda r, f: _g(f, "r", "sum_mohm")),
    ("r_sum25_mohm", lambda r, f: _g(f, "r", "sum25_mohm")), ("r_pack_fast_mohm", lambda r, f: _g(f, "r", "pack_fast_mohm")),
    ("r_max_rel", lambda r, f: _g(f, "r", "max_rel")), ("r_max_rel_block", lambda r, f: _g(f, "r", "max_rel_block")),
    ("aux_ready_v", lambda r, f: _g(f, "aux", "ready_v_obd")), ("aux_wake_min_v", lambda r, f: _g(f, "aux", "wake_min_v")),
]

REST_COLS = ["start_ts", "end_ts", "source", "dur_h", "v_start", "v_min", "v_end", "ma_mean", "mah",
             "temp_mean", "low_min", "crit_min", "slope_mv_per_h", "cap_proxy_ah", "coverage",
             "v_0.5h", "v_1h", "v_2h", "v_6h", "v_12h", "v_24h", "v_48h"]


def _csv(header: List[str], rows: Iterable[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for row in rows:
        w.writerow(["" if x is None else x for x in row])
    return buf.getvalue()


def rides_csv(store) -> str:
    rows = store.rides(closed_only=True)
    return _csv([c for c, _ in RIDE_COLS], ([fn(r, r["features"]) for _, fn in RIDE_COLS] for r in rows))


def blocks_csv(store) -> str:
    """Long format: one row per ride and block."""
    header = ["start_ts", "grade", "t_batt_mean", "block", "r_mohm", "r_se", "r_rel", "ecu_r_mohm",
              "dev0_mv", "dev_ref_mv", "dev25_mv", "dr_mohm", "dev_rest_mv", "dev_dis_mv", "dev_chg_mv",
              "ocv_on_dev_mv", "ocv_off_dev_mv"]

    def gen():
        for r in store.rides(closed_only=True):
            f = r["features"]
            if not (f.get("n_blk") or 0):
                continue
            v = block_view(f) or {}
            for k in range(N_BLOCKS):
                def at(*path):
                    x = _g(f, *path)
                    return x[k] if x else None
                yield [round(r["start_ts"]), _g(f, "quality", "grade"), _g(f, "temp", "t_mean"), k + 1,
                       at("r", "blk_mohm"), at("r", "blk_se"), at("r", "rel"), at("ecu", "r_mohm"),
                       at("bal", "fit", "dev0_mv"), at("bal", "fit", "dev_ref_mv"),
                       v["dev25_mv"][k] if v else None, at("bal", "fit", "dr_mohm"),
                       at("bal", "rest", "dev_mv"), at("bal", "dis", "dev_mv"), at("bal", "chg", "dev_mv"),
                       at("ocv", "on", "dev_mv"), at("ocv", "off", "dev_mv")]
    return _csv(header, gen())


def rests_csv(store) -> str:
    def gen():
        for r in store.rests():
            f = r["features"]
            m = f.get("marks") or {}
            yield [round(r["start_ts"]), round(r["end_ts"]), r["source"]] + [f.get(c) for c in REST_COLS[3:15]] + \
                  [m.get(h) for h in ("0.5", "1", "2", "6", "12", "24", "48")]
    return _csv(REST_COLS, gen())


def ride_series_csv(record: dict) -> str:
    """Raw block series of one ride (one row per 21CE sample)."""
    b = record["tables"]["blk"]
    header = ["t_s", "soc_pct", "i_a"] + ["v%d" % (k + 1) for k in range(N_BLOCKS)]
    return _csv(header, ([b["t"][r] / 10.0, b["soc2"][r] / 2.0, b["i"][r] / 100.0] +
                         [b["v%d" % (k + 1)][r] / 100.0 for k in range(N_BLOCKS)]
                         for r in range(len(b["t"]))))


EXPORTS = {"rides": rides_csv, "blocks": blocks_csv, "rests": rests_csv}
