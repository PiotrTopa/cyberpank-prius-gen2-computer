"""
10. Lane departure warning: built, OFF by default, and it cannot be switched on.

The turn-signal state is not decoded on any bus (BEAN_SIGNALS section 6,
MFD_MAIN_SCREEN section 9), so every deliberate lane change would trigger it.
The logic therefore takes a required ``turn_signal`` input, which is
"unknown" today; ``enable_check`` refuses mode ``on`` while it is unknown.
Shadow is allowed (data collection: every would-be warning is logged with
"turn_signal: unknown"); the default mode is ``off``.

What closes the gap: an opto-isolated input on the indicator lamp lines (or
the flasher relay output) on a satellite or a spare powerbox input, published
as ``turn_signal`` = off | left | right | hazard; or a future CAN/BEAN decode.
The input builder (alerts/inputs.py) is the only place to change.

Logic (scene lanes = modeld lane lines, y right, metres): the left and right
ego lines (index 1 and 2) at the camera (x = 0) with probability >= 0.6; the
distance of the wheel to a line = |y| - half the car width (0.86 m); warns when
it is below 0.15 m while moving towards the line at >= 0.25 m/s, true speed
>= 60 km/h, steering wheel within 30 deg, the line on that side not signalled.
"""

from __future__ import annotations

from typing import Dict, Optional

from ..inputs import AlertInputs, lane_line_y0, lane_prob
from ..model import DRIVE_GEARS, AlertFunction, AlertSpec, Candidate, EvalContext

ID = "lane_departure"
BLOCKED = ("turn-signal state is not decoded on any bus: every lane change would warn. Needs an "
           "indicator input (opto on the lamp lines) or a decode; shadow only for data collection")


class LaneDepartureFunction(AlertFunction):
    name = "lane_departure"

    def __init__(self, tuning, settings=None):
        self.t = tuning.lane
        self.turn_signal_known = False
        self.specs = (AlertSpec(
            id=ID, title="Lane departure warning", text="LANE", severity="warning", priority=70,
            icon="lane", sound="warning", default_mode="off", debounce_s=self.t.debounce_s,
            clear_after_s=1.0, min_show_s=2.0, max_show_s=4.0, cooldown_s=8.0,
            min_kmh=self.t.min_speed_kmh, gears=DRIVE_GEARS,
            enable_check=lambda: None if self.turn_signal_known else BLOCKED,
            description="Drifting over an ego lane line above 60 km/h. Cannot be switched on until "
                        "the turn-signal state is available.",
            needs=("scene lanes[1], lanes[2]", "turn_signal (NOT AVAILABLE)", "dynamics.steering_angle")),)
        self.prev: Dict[str, Optional[float]] = {"l": None, "r": None}
        self.prev_t: Optional[float] = None
        self.drifts = 0

    def evaluate(self, inp: AlertInputs, ctx: EvalContext) -> Dict[str, Optional[Candidate]]:
        t = self.t
        self.turn_signal_known = inp.turn_signal not in (None, "unknown")
        now = inp.now
        if not inp.scene_fresh or not inp.lanes:
            self.prev = {"l": None, "r": None}
            return {ID: None}
        dists = {}
        for side, idx in (("l", 1), ("r", 2)):
            y = lane_line_y0(inp.lanes, idx, 0)
            if y is None or lane_prob(inp.lanes, idx) < t.min_prob:
                dists[side] = None
                continue
            dists[side] = abs(y) - t.half_width_m
        dt = None if self.prev_t is None else now - self.prev_t
        cand = None
        for side in ("l", "r"):
            d, p = dists[side], self.prev[side]
            if d is None or p is None or not dt or dt <= 0:
                continue
            rate = (p - d) / dt                  # > 0: approaching the line
            signalled = inp.turn_signal in (("left",) if side == "l" else ("right",)) or inp.turn_signal == "hazard"
            if (d < t.margin_m and rate >= t.min_rate_mps and not signalled
                    and (inp.steering_deg is None or abs(inp.steering_deg) <= t.max_steer_deg)):
                cand = Candidate(text="LANE %s" % ("LEFT" if side == "l" else "RIGHT"), key=side, evidence={
                    "side": side, "dist_m": round(d, 2), "rate_mps": round(rate, 2),
                    "turn_signal": inp.turn_signal, "real_kmh": inp.real_kmh, "steer": inp.steering_deg})
        self.prev = dists
        self.prev_t = now
        return {ID: cand}

    def status(self):
        return {"turn_signal_known": self.turn_signal_known}
