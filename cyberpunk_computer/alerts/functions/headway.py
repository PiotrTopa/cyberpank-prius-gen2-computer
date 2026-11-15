"""
3. Following distance and closing speed (ADVISORY: the car never brakes).

Two types on the same lead input (roadsense tracked lead when present, else
modeld's ``leads[0]`` with probability >= ``lead_min_prob``), both labelled
advisory everywhere:

``headway_coach`` (notice): time gap = (lead distance - camera-to-bumper) /
true speed below 1.0 s for 5 s at >= 50 km/h, lead within 1.3 m of the camera
axis, steering wheel within 25 deg; one notice, then 2 min cooldown, at most 6
per drive. Dense traffic below 50 km/h never triggers it.

``distance_warning`` (warning): time to collision = gap / closing speed below
2.0 s with a closing speed of at least 15 km/h, 30..150 km/h, lead within 1 m
of the axis, steering within 25 deg (curves: modeld's lead in a bend is often
a parked car or the next lane), not while the brake is pressed (the driver is
already reacting), 0.3 s debounce, 10 s cooldown.

What it cannot see: cut-ins faster than the model's lead hypothesis, lead
class, stationary obstacles (modeld leads are moving-vehicle hypotheses), the
camera's 100-200 ms latency. It is not a certified forward-collision warning.
"""

from __future__ import annotations

from typing import Dict, Optional

from ..inputs import AlertInputs
from ..model import DRIVE_GEARS, AlertFunction, AlertSpec, Candidate, EvalContext

GAP = "headway_coach"
TTC = "distance_warning"


class HeadwayFunction(AlertFunction):
    name = "headway"

    def __init__(self, tuning, settings=None):
        t = self.t = tuning.headway
        self.specs = (
            AlertSpec(id=GAP, title="Following-distance coach (advisory)", text="GAP SHORT",
                      severity="notice", priority=40, icon="lead", sound="notice",
                      debounce_s=t.gap_hold_s, clear_after_s=2.0, min_show_s=3.0, max_show_s=6.0,
                      cooldown_s=t.gap_cooldown_s, max_per_drive=t.gap_max_per_drive,
                      min_kmh=t.gap_min_kmh, gears=DRIVE_GEARS, advisory=True,
                      description="Time gap to the lead below %.1f s for %.0f s above %.0f km/h."
                                  % (t.gap_s, t.gap_hold_s, t.gap_min_kmh),
                      needs=("scene lead (tracked or vision) / map.lead", "vehicle.real_speed_kmh", "dynamics.steering_angle")),
            AlertSpec(id=TTC, title="Distance warning, closing fast (advisory)", text="CLOSING",
                      severity="warning", priority=90, icon="lead", sound="warning",
                      debounce_s=t.ttc_debounce_s, clear_after_s=0.8, min_show_s=2.0, max_show_s=5.0,
                      cooldown_s=t.ttc_cooldown_s, min_kmh=t.ttc_min_kmh, max_kmh=t.ttc_max_kmh,
                      gears=DRIVE_GEARS, advisory=True,
                      description="Time to collision below %.1f s at >= %.0f km/h closing speed; suppressed "
                                  "while braking and in curves. Advisory: the car never brakes."
                                  % (t.ttc_s, t.ttc_closing_kmh),
                      needs=("scene lead (tracked or vision) / map.lead (d, v_rel)", "vehicle.real_speed_kmh",
                             "vehicle.brake_switch / brake_pressed", "dynamics.steering_angle")),
        )
        self.last: Dict[str, object] = {}

    def _lead_ok(self, inp: AlertInputs, max_y: float):
        ld = inp.lead
        if ld is None or inp.scene_age is None or inp.scene_age > self.t.max_scene_age_s:
            return None
        if ld.d <= 0.0:          # the tracker's departed cue without a lead (no distance)
            return None
        if ld.src == "vision" and (ld.prob or 0.0) < self.t.lead_min_prob:
            return None
        if abs(ld.y) > max_y:
            return None
        return ld

    def evaluate(self, inp: AlertInputs, ctx: EvalContext) -> Dict[str, Optional[Candidate]]:
        t = self.t
        out: Dict[str, Optional[Candidate]] = {GAP: None, TTC: None}
        v = inp.real_mps
        if v is None or v < 1.0:
            return out
        curve = inp.steering_deg is not None and abs(inp.steering_deg) > t.max_steer_deg
        # coach
        ld = self._lead_ok(inp, t.coach_max_y_m)
        if ld is not None and not curve:
            gap = max(0.0, ld.d - t.camera_to_bumper_m) / v
            self.last = {"gap_s": round(gap, 2), "d": ld.d, "src": ld.src}
            if gap < t.gap_s:
                out[GAP] = Candidate(text="GAP %.1fs" % gap, evidence={
                    "gap_s": round(gap, 2), "d": ld.d, "y": ld.y, "src": ld.src, "prob": ld.prob,
                    "real_kmh": round(inp.real_kmh, 1), "steer": inp.steering_deg})
        # time to collision
        ld = self._lead_ok(inp, t.lead_max_y_m)
        if ld is None or ld.v_rel is None or curve:
            return out
        if t.suppress_when_braking and inp.brake:
            return out
        closing = -ld.v_rel
        if closing * 3.6 < t.ttc_closing_kmh:
            return out
        ttc = max(0.0, ld.d - t.camera_to_bumper_m) / closing
        if ttc < t.ttc_s:
            out[TTC] = Candidate(text="CLOSING %.1fs" % ttc, evidence={
                "ttc_s": round(ttc, 2), "d": ld.d, "v_rel": ld.v_rel, "a_lead": ld.a, "y": ld.y,
                "src": ld.src, "prob": ld.prob, "real_kmh": round(inp.real_kmh, 1),
                "brake": inp.brake, "steer": inp.steering_deg})
        return out

    def status(self):
        return dict(self.last)
