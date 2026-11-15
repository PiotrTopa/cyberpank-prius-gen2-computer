"""
1. Speed-limit warning (ISA-like, information only).

Compares the limit the MFD shows (roadsense's fused limit when the map layer
is usable, else the camera engine's) with the TRUE speed (CAN 0x0B4). The
speedometer reads about 12 % high (BEAN FE:24); it is never compared.

Rules (tunables: tuning.SpeedLimitTuning):

* over = true speed > limit + tolerance (6 km/h; 10 % of the limit above
  100 km/h) for ``delay_s`` (5 s) -> one notice;
* strong sources (``sign``, ``sign_held``, ``map``, the camera's ``sign``) need
  confidence >= ``min_conf``; weak ones (``map_implicit``, ``vision_default``,
  ``default``: statutory defaults) use the higher bar (+15 km/h for 10 s,
  confidence >= 0.6) or nothing (``weak_policy = "off"``); a map/camera
  ``conflict`` and unknown sources never warn;
* nothing in the first 150 m after the limit changed (slip roads, late signs);
* no repeat in the same limit segment until the true speed has been at or
  below the limit for 3 s (or the limit changes);
* the banner text carries the real speed; with ``real_speed_chip = "warning"``
  a REAL nn chip appears under the limit sign while the notice is shown.

Works vision-only: without a map the camera engine's limit is used as is.
Like EU ISA, a long-press mute lasts one drive only.
"""

from __future__ import annotations

from typing import Dict, Optional

from ..inputs import AlertInputs
from ..model import DRIVE_GEARS, AlertFunction, AlertSpec, Candidate, EvalContext

ID = "speed_limit"


class SpeedLimitFunction(AlertFunction):
    name = "speed_limit"

    def __init__(self, tuning, settings=None):
        self.t = tuning.speed_limit
        self.specs = (AlertSpec(
            id=ID, title="Speed-limit warning (ISA-like)", text="SPEED LIMIT", severity="notice",
            priority=60, icon="limit", sound="notice", clear_after_s=1.0, min_show_s=4.0,
            max_show_s=8.0, cooldown_s=15.0, min_kmh=self.t.min_speed_kmh, gears=DRIVE_GEARS,
            description="One notice after a sustained overspeed against the fused / camera limit, "
                        "on the true speed; quiet on weak sources and right after a limit change.",
            needs=("vehicle.real_speed_kmh (CAN 0x0B4)", "scene map.limit (fused) / scene limit (camera)")),)
        self.cur: Optional[int] = None
        self.seg = 0
        self.change_odo = 0.0
        self.over_since: Optional[float] = None
        self.fired_seg: Optional[int] = None
        self.below_since: Optional[float] = None
        self.last_eval: Dict[str, object] = {}

    def on_drive_start(self, inp, ctx):
        self.fired_seg = None
        self.over_since = None

    def _bar(self, lim) -> Optional[tuple]:
        """(tolerance km/h, delay s, class) or None when this limit never warns."""
        t = self.t
        if lim.agree == "conflict":
            return None
        conf = lim.conf
        strong = (lim.strong or (not lim.from_map_layer and lim.src == "sign")) and not (
            lim.variable and lim.src not in ("sign", "sign_held"))
        if strong and (conf is None or conf >= t.min_conf):
            tol = t.tolerance_kmh
            if lim.kmh > 100:
                tol = max(tol, t.tolerance_pct_above_100 * lim.kmh)
            return tol, t.delay_s, "strong"
        weak = lim.weak or strong or lim.variable
        if not weak or t.weak_policy != "high_bar":
            return None
        if conf is not None and conf < t.weak_min_conf:
            return None
        return max(t.weak_tolerance_kmh, t.tolerance_pct_above_100 * lim.kmh), t.weak_delay_s, "weak"

    def evaluate(self, inp: AlertInputs, ctx: EvalContext) -> Dict[str, Optional[Candidate]]:
        lim = inp.limit
        v = inp.real_kmh
        now = inp.now
        if lim is None or v is None:
            self.over_since = None
            return {ID: None}
        if lim.kmh != self.cur:
            self.cur = lim.kmh
            self.seg += 1
            self.change_odo = ctx.odo_m
            self.over_since = None
            self.fired_seg = None
            self.below_since = None
        since_change = ctx.odo_m - self.change_odo
        bar = self._bar(lim)
        self.last_eval = {"limit": lim.kmh, "src": lim.src, "bar": bar[2] if bar else None,
                          "real_kmh": round(v, 1)}
        evidence = {"limit": lim.kmh, "src": lim.src, "conf": lim.conf, "agree": lim.agree,
                    "map_layer": lim.from_map_layer, "real_kmh": round(v, 1),
                    "display_kmh": inp.display_kmh, "since_change_m": round(since_change)}
        chip = {"real_kmh": round(v)} if self.t.real_speed_chip == "warning" else None
        text = "LIMIT %d  REAL %d" % (lim.kmh, round(v))
        # after a fire: latched until at or below the limit for rearm_hold_s
        if self.fired_seg == self.seg:
            if v <= lim.kmh + self.t.rearm_below_kmh:
                if self.below_since is None:
                    self.below_since = now
                if now - self.below_since >= self.t.rearm_hold_s:
                    self.fired_seg = None
                    self.below_since = None
                    return {ID: None}
            else:
                self.below_since = None
            return {ID: Candidate(text=text, key="seg%d" % self.seg, evidence=evidence, chip=chip)}
        if bar is None or since_change < self.t.suppress_after_change_m:
            self.over_since = None
            return {ID: None}
        tol, delay, cls = bar
        if v > lim.kmh + tol:
            if self.over_since is None:
                self.over_since = now
            if now - self.over_since >= delay:
                self.fired_seg = self.seg
                self.below_since = None
                evidence.update(tolerance_kmh=round(tol, 1), delay_s=delay, bar=cls,
                                over_s=round(now - self.over_since, 1))
                return {ID: Candidate(text=text, key="seg%d" % self.seg, evidence=evidence, chip=chip)}
        else:
            self.over_since = None
        return {ID: None}

    def status(self):
        return dict(self.last_eval, segment=self.seg)
