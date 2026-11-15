"""
7. Winter assistant: ice risk and a rear-defogger hint.

Outside temperature used: ``climate.outside_temp`` = the climate ECU's own
value, heard passively on AVC-LAN (E0 5D F7, about 1 Hz, x - 48 C) with the
BEAN copy FE:CD as fallback (comm/source_priority.py). Whole degrees; the
sensor sits behind the front bumper, so it reads high after parking in the sun
or in slow traffic (heat soak). Exists only with IG on. The solicited OBD PID
0146 (``vehicle.ambient_air_temp``) is NOT used: the owner marked it as not
usably connected.

``ice_risk`` (notice, soft sound): at the start of a drive, after ``settle_s``
(30 s), if the outside temperature is <= 3 C; while driving, when it falls to
<= 3 C for 60 s after having been >= 5 C (hysteresis). At most two per drive.

``rear_defog_hint`` (info, silent): within the first 5 minutes of a drive,
<= 3 C and the rear defogger (AVC F3 b1 0x08) off -> one hint per drive.

Rain / dew / fogging functions wait for the rain-light sensor (VAG RLS,
decoded on the bench only): not built.
"""

from __future__ import annotations

from typing import Dict, Optional

from ..inputs import AlertInputs
from ..model import AlertFunction, AlertSpec, Candidate, EvalContext

ICE = "ice_risk"
DEFOG = "rear_defog_hint"


class WinterFunction(AlertFunction):
    name = "winter"

    def __init__(self, tuning, settings=None):
        self.t = tuning.winter
        self.specs = (
            AlertSpec(id=ICE, title="Ice risk (outside temperature)", text="ICE RISK", severity="notice",
                      priority=45, icon="snow", sound="soft", clear_after_s=5.0, min_show_s=3.0,
                      max_show_s=6.0, cooldown_s=300.0, max_per_drive=2, require_ready=True,
                      description="Outside temperature <= %.0f C at the start of a drive or when it "
                                  "drops there while driving (AVC E0 5D F7 / BEAN FE:CD)." % self.t.ice_c,
                      needs=("climate.outside_temp (AVC E0 5D F7, BEAN FE:CD)",)),
            AlertSpec(id=DEFOG, title="Rear-defogger hint (cold start)", text="REAR DEFOG?", severity="info",
                      priority=15, icon="defog", sound=None, clear_after_s=5.0, min_show_s=2.0,
                      max_show_s=5.0, cooldown_s=0.0, max_per_drive=1, require_ready=True,
                      description="Cold start (<= %.0f C) with the rear defogger off, first minutes only."
                                  % self.t.ice_c,
                      needs=("climate.outside_temp", "climate.rear_defogger (AVC F3)")),
        )
        self.drive_t: Optional[float] = None
        self.armed = False
        self.below_since: Optional[float] = None
        self.start_checked = False
        self.crossings = 0
        self.last_c: Optional[float] = None

    def on_drive_start(self, inp, ctx):
        self.drive_t = inp.now
        self.armed = False
        self.below_since = None
        self.start_checked = False

    def evaluate(self, inp: AlertInputs, ctx: EvalContext) -> Dict[str, Optional[Candidate]]:
        t = self.t
        out: Dict[str, Optional[Candidate]] = {ICE: None, DEFOG: None}
        c = inp.outside_c
        self.last_c = c
        if c is None or self.drive_t is None or not (inp.ready or inp.ig):
            return out
        age = inp.now - self.drive_t
        if age < t.settle_s:
            return out
        ev = {"outside_c": c, "drive_s": round(age)}
        if not self.start_checked:
            self.start_checked = True
            self.armed = c > t.ice_c
            if c <= t.ice_c:
                self.start_key = "start"
        if getattr(self, "start_key", None) == "start" and c <= t.ice_c and age < t.settle_s + 60:
            out[ICE] = Candidate(text="ICE RISK  OUT %dC" % round(c), key="start", evidence=dict(ev, at="start"))
        else:
            self.start_key = None
            if c >= t.rearm_c:
                self.armed = True
                self.below_since = None
            elif self.armed and c <= t.ice_c:
                self.below_since = self.below_since or inp.now
                if inp.now - self.below_since >= t.hold_s:
                    self.crossings += 1
                    self.armed = False
                    out[ICE] = Candidate(text="ICE RISK  OUT %dC" % round(c), key="cross%d" % self.crossings,
                                         evidence=dict(ev, at="crossing"))
            elif c > t.ice_c:
                self.below_since = None
        if (t.defog_hint and age <= t.defog_hint_window_s and c <= t.ice_c and inp.climate_live
                and inp.climate.get("rear_defogger") is False):
            out[DEFOG] = Candidate(key="drive", evidence=dict(ev, rear_defogger=False))
        return out

    def status(self):
        return {"outside_c": self.last_c, "source": "AVC E0 5D F7 / BEAN FE:CD", "armed": self.armed}
