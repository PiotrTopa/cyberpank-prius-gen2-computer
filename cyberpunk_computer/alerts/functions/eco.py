"""
6. Predictive eco coach: "lift off" hints and a regen-vs-friction braking score.

Lift-off hint (``eco_lift``, info, no sound): targets ahead from the map
look-ahead (usable map only) - a lower speed-limit change (``ahead.limit``),
a roundabout (``ahead.junction.kind``), a stop / give-way (``ahead.feature``) -
and, without the map, the camera's warning signs (``roundabout_ahead``,
``stop_ahead`` in scene ``signs.ahead``). The glide distance to reach the
target speed is d = (v^2 - vt^2) / (2 * 0.7 m/s2); the hint shows while the
target is between d and d + v * 3 s, the true speed is 15 km/h or more above
the target and the accelerator is pressed (CAN 0x244; unknown counts as
pressed). One hint per target. The built-up boundary ahead is not in the scene
(roadsense ``ahead.built_up_change`` is not carried yet): not used.

Braking score (statistic, never shown): per stop (from > 20 km/h to < 3 km/h)
the share of braking torque that was regenerative: sum of 21C3 regen torque
actual over sum of max(master-cylinder torque, regen torque) on the 1 Hz 21C3
samples taken while the brake is pressed. The master-cylinder torque is the
total (regen + friction) request per docs/prius_can.md; units and scale are
not verified on the car, so the share is a relative figure. Logged as
``stat eco_stop`` (alert log + metrics events) and summed per drive.
"""

from __future__ import annotations

import collections
import time
from typing import Deque, Dict, List, Optional, Tuple

from ..inputs import AlertInputs
from ..model import DRIVE_GEARS, AlertFunction, AlertSpec, Candidate, EvalContext

ID = "eco_lift"


class EcoCoachFunction(AlertFunction):
    name = "eco_coach"

    def __init__(self, tuning, settings=None):
        self.t = tuning.eco
        self.specs = (AlertSpec(
            id=ID, title="Predictive eco coach (lift off)", text="LIFT OFF", severity="info",
            priority=20, icon="lift", sound=None, clear_after_s=2.0, min_show_s=2.0, max_show_s=5.0,
            cooldown_s=8.0, min_kmh=self.t.min_speed_kmh, gears=DRIVE_GEARS,
            description="Lift off early for a lower limit / roundabout / stop ahead (map look-ahead, "
                        "camera warning signs without the map). Silent.",
            needs=("scene map.ahead (limit, junction, feature)", "scene signs.ahead", "vehicle.real_speed_kmh",
                   "vehicle.throttle_position (CAN 0x244)")),)
        self._seen: List[float] = []
        # braking score
        self.phase = "idle"
        self.peak_kmh = 0.0
        self.regen = 0.0
        self.total = 0.0
        self.samples = 0
        self._seq = -1
        self.stops: Deque[Dict] = collections.deque(maxlen=self.t.keep_stops)
        self.drive_regen = 0.0
        self.drive_total = 0.0
        self.drive_stops = 0

    def on_drive_start(self, inp, ctx):
        self.drive_regen = self.drive_total = 0.0
        self.drive_stops = 0
        self.phase = "idle"

    # ── targets ──────────────────────────────────────────────────────────────

    def _targets(self, inp: AlertInputs) -> List[Tuple[str, float, float]]:
        """[(label, distance m, target km/h)]."""
        t = self.t
        out: List[Tuple[str, float, float]] = []
        a = inp.ahead
        if inp.map_ok:
            lc = a.get("limit")
            cur = inp.limit.kmh if inp.limit else None
            if lc and isinstance(lc.get("m"), (int, float)) and isinstance(lc.get("kmh"), (int, float)):
                if cur is None or lc["kmh"] < cur:
                    out.append(("%d" % lc["kmh"], float(lc["m"]), float(lc["kmh"])))
            j = a.get("junction")
            if j and j.get("kind") == "roundabout" and isinstance(j.get("m"), (int, float)):
                out.append(("RBT", float(j["m"]), t.target_roundabout_kmh))
            f = a.get("feature")
            if f and isinstance(f.get("m"), (int, float)):
                if f.get("kind") == "stop":
                    out.append(("STOP", float(f["m"]), t.target_stop_kmh))
                elif f.get("kind") == "give_way":
                    out.append(("YIELD", float(f["m"]), t.target_give_way_kmh))
        elif t.use_vision_signs:
            for s in inp.signs_ahead:
                m = s.get("m")
                if not isinstance(m, (int, float)):
                    continue
                if s.get("id") == "roundabout_ahead":
                    out.append(("RBT", float(m), t.target_roundabout_kmh))
                elif s.get("id") == "stop_ahead":
                    out.append(("STOP", float(m), t.target_stop_kmh))
        return out

    def _lift(self, inp: AlertInputs, ctx: EvalContext) -> Optional[Candidate]:
        t = self.t
        v = inp.real_mps
        if v is None:
            return None
        if inp.throttle_pct is not None and inp.throttle_pct < t.throttle_min_pct:
            return None                     # already off the accelerator
        best = None
        for label, m, vt_kmh in self._targets(inp):
            if inp.real_kmh - vt_kmh < t.min_delta_kmh:
                continue
            vt = vt_kmh / 3.6
            d = (v * v - vt * vt) / (2.0 * t.coast_decel)
            if d <= m <= d + v * t.window_s + v * t.reaction_s:
                if best is None or m < best[1]:
                    best = (label, m, vt_kmh, d)
        if best is None:
            return None
        label, m, vt_kmh, d = best
        pos = ctx.odo_m + m
        near = [p for p in self._seen if abs(p - pos) < 60.0]
        if not near:
            self._seen = [p for p in self._seen if pos - p < 3000.0] + [pos]
        return Candidate(text="LIFT OFF  %s %dm" % (label, int(round(m, -1))),
                         key="lift@%d" % int(near[0] if near else pos), evidence={
                             "target": label, "m": round(m), "target_kmh": vt_kmh, "glide_m": round(d),
                             "real_kmh": round(inp.real_kmh, 1), "throttle": inp.throttle_pct,
                             "map": inp.map_ok})

    # ── braking score ────────────────────────────────────────────────────────

    def _score(self, inp: AlertInputs, ctx: EvalContext) -> None:
        t = self.t
        v = inp.real_kmh
        if v is None:
            return
        new_sample = inp.hybrid_seq != self._seq
        self._seq = inp.hybrid_seq
        if self.phase == "idle":
            if v > t.stop_from_kmh:
                self.phase = "moving"
                self.peak_kmh = v
            return
        self.peak_kmh = max(self.peak_kmh, v) if self.total == 0 else self.peak_kmh
        if new_sample and inp.brake and inp.regen_nm is not None:
            regen = max(0.0, inp.regen_nm)
            mc = max(0.0, inp.master_cyl_nm or 0.0)
            self.regen += regen
            self.total += max(mc, regen)
            self.samples += 1
        if v < t.stop_to_kmh:
            share = (self.regen / self.total) if self.total > 0 else None
            rec = {"ts": round(inp.wall, 1), "from_kmh": round(self.peak_kmh, 1),
                   "regen_share": None if share is None else round(share, 3), "samples": self.samples}
            self.stops.append(rec)
            self.drive_regen += self.regen
            self.drive_total += self.total
            self.drive_stops += 1
            ctx.stat("eco_stop", rec)
            self.phase = "idle"
            self.regen = self.total = 0.0
            self.samples = 0
        elif v > t.stop_from_kmh and not inp.brake and self.total > 0 and v > self.peak_kmh - 2:
            # slowed and sped up again without stopping: start over
            self.regen = self.total = 0.0
            self.samples = 0
            self.peak_kmh = v

    def evaluate(self, inp: AlertInputs, ctx: EvalContext) -> Dict[str, Optional[Candidate]]:
        self._score(inp, ctx)
        return {ID: self._lift(inp, ctx)}

    def status(self):
        share = self.drive_regen / self.drive_total if self.drive_total > 0 else None
        return {"drive": {"stops": self.drive_stops, "regen_share": None if share is None else round(share, 3)},
                "stops": list(self.stops), "phase": self.phase, "updated": time.time()}
