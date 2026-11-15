"""
4. Curve speed notice from the map look-ahead.

Input: scene ``map.ahead.curve`` = roadsense mapd's nearest curve on the
predicted path (``m`` to its start, ``r`` minimum radius, ``dir``). Only while
the map layer is usable (``ok``, not synthetic) with match confidence >= 0.9.
Without a matched map position the function is inactive (today: no real GNSS
source on the car, so it only runs on synthetic drives).

Comfortable speed = sqrt(a_lat * r) with a_lat 2.5 m/s2 (2.0 when envd says
rain / snow / fog). Fires when the true speed is above that + 5 km/h and the
curve starts in 3..9 s at the current speed; skipped in built-up areas (junction
turns, roundabouts) and when a junction lies within 40 m of the curve start (a
turn, not a bend). One notice per curve (keyed by its position on the drive).
"""

from __future__ import annotations

import math
from typing import Dict, Optional

from ..inputs import AlertInputs
from ..model import DRIVE_GEARS, AlertFunction, AlertSpec, Candidate, EvalContext

ID = "curve_speed"


class CurveFunction(AlertFunction):
    name = "curve"

    def __init__(self, tuning, settings=None):
        self.t = tuning.curve
        self.specs = (AlertSpec(
            id=ID, title="Curve speed notice (map)", text="CURVE AHEAD", severity="notice", priority=50,
            icon="curve", sound="notice", clear_after_s=2.0, min_show_s=3.0, max_show_s=7.0,
            cooldown_s=10.0, min_kmh=self.t.min_speed_kmh, gears=DRIVE_GEARS,
            description="Radius ahead (map) against the true speed and a comfortable lateral "
                        "acceleration; inactive without a matched map position.",
            needs=("scene map.ahead.curve (m, r)", "scene map.ok / conf", "vehicle.real_speed_kmh",
                   "scene env.weather")),)
        self.last: Dict[str, object] = {}
        self._seen = []

    def evaluate(self, inp: AlertInputs, ctx: EvalContext) -> Dict[str, Optional[Candidate]]:
        t = self.t
        c = inp.ahead.get("curve") if inp.map_ok else None
        v = inp.real_mps
        if (not c or v is None or v <= 0 or (inp.map_conf or 0.0) < t.min_match_conf
                or not isinstance(c.get("m"), (int, float)) or not isinstance(c.get("r"), (int, float))):
            self.last = {"active": False, "why": "no matched map" if not inp.map_ok else "no curve"}
            return {ID: None}
        m, r = float(c["m"]), float(c["r"])
        wet = inp.env.get("weather") in ("rain", "snow", "fog")
        a = t.a_lat_wet if wet else t.a_lat
        v_ok = math.sqrt(a * max(r, 1.0))
        tt = m / v
        self.last = {"active": True, "m": m, "r": r, "v_ok_kmh": round(v_ok * 3.6), "t_s": round(tt, 1)}
        if r > t.max_radius_m or not (t.lead_time_min_s <= tt <= t.lead_time_max_s):
            return {ID: None}
        if inp.real_kmh <= v_ok * 3.6 + t.margin_kmh:
            return {ID: None}
        if t.skip_built_up and inp.built_up:
            return {ID: None}
        j = inp.ahead.get("junction")
        if j and isinstance(j.get("m"), (int, float)) and abs(float(j["m"]) - m) <= t.junction_window_m:
            return {ID: None}
        pos = ctx.odo_m + m               # the curve's place along this run (stable while approaching)
        near = [p for p in self._seen if abs(p - pos) < 80.0]
        if not near:
            self._seen = [p for p in self._seen if pos - p < 2000.0] + [pos]
        key = "curve@%d" % int(near[0] if near else pos)
        rec = int(v_ok * 3.6 // 5 * 5)
        return {ID: Candidate(text="CURVE %dm  MAX %d" % (int(round(m, -1)), rec), key=key, evidence={
            "m": m, "r": r, "dir": c.get("dir"), "v_ok_kmh": round(v_ok * 3.6, 1), "a_lat": a,
            "wet": wet, "real_kmh": round(inp.real_kmh, 1), "t_s": round(tt, 1),
            "map_conf": inp.map_conf, "lat_acc_pred": round(v * v / max(r, 1.0), 2)})}

    def status(self):
        return dict(self.last)
