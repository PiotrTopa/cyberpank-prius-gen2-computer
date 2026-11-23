"""
Obstacle ahead: what chris-integrator's radar raises as the view's ``alert``
(scene ``vw.alert``).

``obstacle_ahead`` (warning): a thing stands on the car's predicted path
(level ``obstacle``), or the car reaches a thing on its path within seconds at
the present speeds whether it stands or creeps (level ``threat``). Parked
vehicles beside the path never raise it: the radar marks those ``parked`` and
the road view draws them dimmed.

The radar decides (object tracking over the camera's boxes, later more
sensors; its own hold times and its certainty that the thing is inside the
path's corridor). This function only gates: speed, gear, a fresh scene, and
silence while the driver brakes.

Limits: a camera-only radar confirms a standing car late at motorway speed
(tens of metres); the lead's ``distance_warning`` and this one overlap on a
standing lead, the manager shows one. Advisory: the car never brakes.
"""

from __future__ import annotations

from typing import Dict, Optional

from ..inputs import AlertInputs
from ..model import DRIVE_GEARS, AlertFunction, AlertSpec, Candidate, EvalContext

OBSTACLE = "obstacle_ahead"


class ObstacleFunction(AlertFunction):
    name = "obstacle"

    def __init__(self, tuning, settings=None):
        t = self.t = tuning.obstacle
        self.specs = (
            AlertSpec(id=OBSTACLE, title="Obstacle on the path (advisory)", text="OBSTACLE",
                      severity="warning", priority=95, icon="lead", sound="warning",
                      debounce_s=t.debounce_s, clear_after_s=0.8, min_show_s=2.0, max_show_s=6.0,
                      cooldown_s=t.cooldown_s, min_kmh=t.min_kmh, max_kmh=t.max_kmh,
                      gears=DRIVE_GEARS, advisory=True,
                      description="chris-integrator's radar: something stands on the predicted path, or is "
                                  "reached within seconds at the present speeds. Parked vehicles beside the "
                                  "path do not count. Advisory: the car never brakes.",
                      needs=("scene vw.alert (chris-integrator view.alert, from its radar)", "vehicle.real_speed_kmh",
                             "vehicle.brake_switch / brake_pressed")),
        )
        self.last: Dict[str, object] = {}

    def evaluate(self, inp: AlertInputs, ctx: EvalContext) -> Dict[str, Optional[Candidate]]:
        t = self.t
        out: Dict[str, Optional[Candidate]] = {OBSTACLE: None}
        ob = inp.obstacle
        if ob is None or inp.scene_age is None or inp.scene_age > t.max_scene_age_s:
            self.last = {}
            return out
        self.last = dict(ob)
        if t.suppress_when_braking and inp.brake:
            return out
        ttc = ob.get("ttc")
        text = "OBSTACLE %dm" % int(ob["d"] + 0.5) if ttc is None else "OBSTACLE %.1fs" % ttc
        out[OBSTACLE] = Candidate(text=text, evidence={
            "level": ob["level"], "d": ob["d"], "y": ob["y"], "ttc_s": ttc, "lead": ob.get("lead", False),
            "real_kmh": round(inp.real_kmh, 1), "brake": inp.brake})
        return out

    def status(self):
        return dict(self.last)
