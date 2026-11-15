"""
2. Stop-and-go helper: lead departure, and the green-light half.

Lead departure (``lead_departure``): the car stands (true speed < 0.5 km/h for
2 s, READY, gear D/B), a lead was close (<= 10 m) and standing, and it moves
off while the car still stands -> one notice per stop.

* With roadsense's tracked lead (``fused.lead`` -> scene ``map.lead``; lane in
  roadsense today) its ``departed`` cue is used: the tracker freezes the gap
  while both stand and runs a change detector on the lead's speed.
* Otherwise the vision lead (modeld ``leads[0]``, probability >= 0.6) is used:
  the gap while both stood is the reference; the lead counts as gone when it is
  ``moved_m`` (3.5 m) farther for ``moved_s`` (1 s). A lead that only vanishes
  (occlusion, probability drop) never fires.

Green light (``green_light``): the car stands, the ego light was red / red-
yellow within 30 s, it is green now and no lead is within 15 m for 2 s. This
half is built but cannot be switched on (``enable_check``): roadsense's
traffic-light accuracy is only qualitatively checked on one replay (roadsense
docs/lanes/L7-traffic-lights.md: wrong ego assignment at lane splits and in
curves, red-yellow read as red), so it stays shadow-only to collect data. The
scene now carries the light's ``rel`` (relevance) and ``agree`` for that log.
"""

from __future__ import annotations

from typing import Dict, Optional

from ..inputs import AlertInputs
from ..model import DRIVE_GEARS, AlertFunction, AlertSpec, Candidate, EvalContext

LEAD = "lead_departure"
GREEN = "green_light"
GREEN_BLOCKED = ("traffic-light reliability on this car is unvalidated (roadsense L7: qualitative "
                 "check on one replay; wrong ego light at lane splits / curves): shadow only")


class StopGoFunction(AlertFunction):
    name = "stop_go"

    def __init__(self, tuning, settings=None):
        self.t = tuning.stop_go
        self.specs = (
            AlertSpec(id=LEAD, title="Lead departure (stop-and-go)", text="LEAD MOVED OFF",
                      severity="notice", priority=55, icon="go", sound="notice", clear_after_s=2.0,
                      min_show_s=3.0, max_show_s=6.0, cooldown_s=10.0, gears=DRIVE_GEARS,
                      require_ready=True,
                      description="Car stands behind a standing lead and the lead moves off. Uses "
                                  "roadsense's tracked lead + departed cue when present, else own "
                                  "logic on the vision lead.",
                      needs=("vehicle.real_speed_kmh", "vehicle.gear", "scene map.lead (tracked) / scene lead")),
            AlertSpec(id=GREEN, title="Green light, nothing ahead", text="GREEN LIGHT",
                      severity="info", priority=50, icon="light", sound="soft", clear_after_s=2.0,
                      min_show_s=3.0, max_show_s=5.0, cooldown_s=20.0, gears=DRIVE_GEARS,
                      require_ready=True, enable_check=lambda: GREEN_BLOCKED,
                      description="Ego light red -> green while standing, no lead within 15 m. "
                                  "Shadow only (traffic-light reliability unvalidated).",
                      needs=("scene light (state, rel, agree)", "scene lead")),
        )
        self._reset_stop()
        self.stops = 0
        self.last_red: Optional[float] = None
        self.green_since: Optional[float] = None

    def _reset_stop(self):
        self.stop_since: Optional[float] = None
        self.close_tracked = False
        self.ref_d: Optional[float] = None
        self.still_since: Optional[float] = None
        self.moved_since: Optional[float] = None
        self.fired_stop = False
        self.ref_samples = []

    def evaluate(self, inp: AlertInputs, ctx: EvalContext) -> Dict[str, Optional[Candidate]]:
        t = self.t
        now = inp.now
        out: Dict[str, Optional[Candidate]] = {LEAD: None, GREEN: None}
        stopped = (inp.real_kmh is not None and inp.real_kmh < t.stop_kmh and (inp.ready or inp.ig)
                   and inp.gear in DRIVE_GEARS)
        if not stopped:
            if self.stop_since is not None:
                self._reset_stop()
            self.green_since = None
            return out
        if self.stop_since is None:
            self.stop_since = now
            self.stops += 1
        standing_s = now - self.stop_since
        ld = inp.lead
        key = "stop%d" % self.stops
        # ── lead departure ──
        if t.use_tracked and inp.lead_tracked_available:
            if ld is not None and ld.src == "tracked":
                if ld.standing and ld.d <= t.lead_close_m:
                    self.close_tracked = True
                    self.ref_d = ld.d
                if ld.departed and self.close_tracked and standing_s >= t.stopped_min_s:
                    out[LEAD] = Candidate(key=key, evidence={
                        "src": "tracked", "d_at_stop": self.ref_d, "d": ld.d, "standing_s": round(standing_s, 1),
                        "track": ld.track_id})
        elif ld is not None and ld.src == "vision" and (ld.prob or 0.0) >= t.lead_min_prob:
            still = ld.v_rel is None or abs(ld.v_rel) < t.lead_still_mps
            if self.ref_d is None:
                if ld.d <= t.lead_close_m and still:
                    if self.still_since is None:
                        self.still_since = now
                    self.ref_samples.append(ld.d)
                    if now - self.still_since >= t.lead_still_s and standing_s >= t.stopped_min_s:
                        s = sorted(self.ref_samples)
                        self.ref_d = s[len(s) // 2]
                else:
                    self.still_since = None
                    self.ref_samples = []
            else:
                if ld.d > self.ref_d + t.moved_m:
                    if self.moved_since is None:
                        self.moved_since = now
                    if now - self.moved_since >= t.moved_s:
                        out[LEAD] = Candidate(key=key, evidence={
                            "src": "vision", "d_at_stop": round(self.ref_d, 1), "d": ld.d,
                            "v_rel": ld.v_rel, "prob": ld.prob, "standing_s": round(standing_s, 1)})
                else:
                    self.moved_since = None
        # ── green light (shadow only) ──
        lt = inp.light
        st = lt.get("state") if lt else None
        if st in ("red", "red_yellow"):
            self.last_red = now
            self.green_since = None
        elif (t.green_enabled and st == "green" and self.last_red is not None
              and now - self.last_red <= t.green_after_red_s and standing_s >= t.stopped_min_s
              and (ld is None or ld.d > t.green_no_lead_m)):
            if self.green_since is None:
                self.green_since = now
            if now - self.green_since >= t.green_hold_s:
                out[GREEN] = Candidate(key=key, evidence={
                    "light_d": lt.get("d"), "rel": lt.get("rel"), "agree": lt.get("agree"),
                    "red_ago_s": round(now - self.last_red, 1), "lead_d": None if ld is None else ld.d})
        else:
            self.green_since = None
        return out

    def status(self):
        return {"stops": self.stops, "standing": self.stop_since is not None,
                "ref_d": self.ref_d, "tracked_close": self.close_tracked}
