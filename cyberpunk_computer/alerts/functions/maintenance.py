"""
8. Maintenance, range and new fault codes.

``service_due`` (info): service intervals from the odometer (BEAN 52:3F,
``bean.decoded.odometer_km``, passively heard) and the date. The list is in
tuning ``maintenance.services`` (id, name, every_km, every_days); when each was
last done is recorded from the dashboard (POST /api/v1/alerts/service/{id}/done,
default: the odometer now) and kept in alerts_settings.json. Items never
recorded are "not set" and never alert. At the start of a drive (first 2 min,
below 10 km/h) one notice for the most urgent item within 500 km / 14 days or
overdue.

``range_low`` (notice): range = fuel level (CAN 0x5A4, assumed litres; only
when a real value arrived, the state default is a placeholder) / consumption.
Consumption = our own litres per km over the last 300 km of drives (fuel model
0x520 x rpm, petrol-equivalent), else the meter's average (BEAN 52:A2). Petrol
only: the LPG level and the fuel in use are not visible to the system, so on
LPG the petrol range is wrong (it shrinks with litres burnt that came from the
LPG tank). Notice once per drive below 60 km.

``new_dtc`` (notice): a stored DTC that was not known before, from what the
backend already reads (manual / scheduled scans, the auto-clear monitor's
polls); no polling is added. The first observation becomes the baseline (no
notice for codes already there); a code that disappears is forgotten so a
recurrence is new again. Shown at a standstill.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from ..inputs import AlertInputs
from ..model import AlertFunction, AlertSpec, Candidate, EvalContext

SERVICE = "service_due"
RANGE = "range_low"
DTC = "new_dtc"
DAY = 86400.0
LPG_NOTE = ("petrol only: LPG level and the fuel in use are not visible; litres are petrol-equivalent "
            "(fuel model) and the 0x5A4 scale is assumed")


class MaintenanceFunction(AlertFunction):
    name = "maintenance"

    def __init__(self, tuning, settings=None):
        self.t = tuning.maintenance
        self.settings = settings
        self.specs = (
            AlertSpec(id=SERVICE, title="Service interval due", text="SERVICE DUE", severity="info",
                      priority=10, icon="service", sound="soft", clear_after_s=5.0, min_show_s=3.0,
                      max_show_s=8.0, cooldown_s=0.0, max_per_drive=1, require_ready=True,
                      description="Owner-entered intervals against the odometer (BEAN 52:3F) and the date.",
                      needs=("bean odometer_km (52:3F)", "dashboard: service done records")),
            AlertSpec(id=RANGE, title="Low range (petrol)", text="RANGE LOW", severity="notice",
                      priority=30, icon="fuel", sound="soft", clear_after_s=30.0, min_show_s=3.0,
                      max_show_s=8.0, cooldown_s=0.0, max_per_drive=1, require_ready=True,
                      description="Fuel level / recent consumption below %.0f km. %s" % (self.t.range_warn_km, LPG_NOTE),
                      needs=("vehicle.fuel_level (CAN 0x5A4)", "trip fuel / distance (fuel model)",
                             "meter average 52:A2")),
            AlertSpec(id=DTC, title="New fault code", text="NEW FAULT CODE", severity="notice",
                      priority=35, icon="fault", sound="soft", clear_after_s=5.0, min_show_s=4.0,
                      max_show_s=10.0, cooldown_s=0.0, max_kmh=10.0,
                      description="A stored DTC not seen before (from scans the backend already runs).",
                      needs=("diagnostics.stored_dtcs / ecus[*].dtcs",)),
        )
        self.odo: Optional[float] = None
        self.fuel_l: Optional[float] = None
        self.drive_t: Optional[float] = None
        self._drive_rec: Optional[Tuple[float, float]] = None   # (km, litres) of the running drive
        self.range_km: Optional[float] = None
        self.l100: Optional[float] = None
        self.l100_basis = ""
        self.new_codes: List[str] = []
        self._new_since: Optional[float] = None

    # ── memory ───────────────────────────────────────────────────────────────

    def _mem(self, key: str, default: Any) -> Any:
        return self.settings.memory(key, default) if self.settings is not None else default

    def _set(self, key: str, value: Any) -> None:
        if self.settings is not None:
            self.settings.set_memory(key, value)

    def mark_service_done(self, item: str, km: Optional[float] = None) -> Tuple[bool, Optional[str]]:
        if item not in {s.get("id") for s in self.t.services}:
            return False, "unknown service item"
        km = km if km is not None else self.odo
        if km is None:
            return False, "odometer not known yet (BEAN 52:3F not heard)"
        done = dict(self._mem("service_done", {}))
        done[item] = {"km": float(km), "ts": time.time()}
        self._set("service_done", done)
        return True, None

    def services(self, wall: Optional[float] = None) -> List[Dict[str, Any]]:
        wall = wall or time.time()
        done = self._mem("service_done", {})
        out = []
        for s in self.t.services:
            rec = done.get(s.get("id")) if isinstance(done, dict) else None
            row = {"id": s.get("id"), "name": s.get("name"), "every_km": s.get("every_km"),
                   "every_days": s.get("every_days"), "done_km": None, "remaining_km": None,
                   "remaining_days": None, "status": "unset"}
            if isinstance(rec, dict):
                row["done_km"] = rec.get("km")
                if s.get("every_km") and rec.get("km") is not None and self.odo is not None:
                    row["remaining_km"] = rec["km"] + s["every_km"] - self.odo
                if s.get("every_days") and rec.get("ts"):
                    row["remaining_days"] = (rec["ts"] + s["every_days"] * DAY - wall) / DAY
                rk, rd = row["remaining_km"], row["remaining_days"]
                if (rk is not None and rk < 0) or (rd is not None and rd < 0):
                    row["status"] = "overdue"
                elif (rk is not None and rk <= self.t.warn_km) or (rd is not None and rd <= self.t.warn_days):
                    row["status"] = "due"
                else:
                    row["status"] = "ok"
            out.append(row)
        return out

    # ── drive bookkeeping (consumption history) ─────────────────────────────

    def on_drive_start(self, inp, ctx):
        self._close_drive()
        self.drive_t = inp.now

    def _close_drive(self) -> None:
        if self._drive_rec is None:
            return
        km, litres = self._drive_rec
        self._drive_rec = None
        if km < 0.5 or litres <= 0:
            return
        hist = list(self._mem("fuel_history", []))
        hist.append([round(km, 2), round(litres, 3), round(time.time())])
        # keep enough drives to cover the window
        total = 0.0
        keep = []
        for rec in reversed(hist):
            keep.append(rec)
            total += rec[0]
            if total >= self.t.range_window_km * 1.5:
                break
        self._set("fuel_history", list(reversed(keep)))

    def _consumption(self, inp: AlertInputs) -> Tuple[Optional[float], str]:
        km = litres = 0.0
        if self._drive_rec is not None:
            km, litres = self._drive_rec
        for rec in reversed(self._mem("fuel_history", [])):
            if km >= self.t.range_window_km:
                break
            km += rec[0]
            litres += rec[1]
        if km >= self.t.range_min_km and litres > 0:
            return 100.0 * litres / km, "own (last %.0f km)" % km
        if inp.meter_avg_l100:
            return inp.meter_avg_l100, "meter 52:A2"
        return None, ""

    # ── evaluation ───────────────────────────────────────────────────────────

    def evaluate(self, inp: AlertInputs, ctx: EvalContext) -> Dict[str, Optional[Candidate]]:
        out: Dict[str, Optional[Candidate]] = {SERVICE: None, RANGE: None, DTC: None}
        if inp.odometer_km is not None:
            self.odo = inp.odometer_km
        if inp.drive_km is not None and inp.trip_fuel_l is not None and (inp.ready or inp.ig):
            self._drive_rec = (inp.drive_km, inp.trip_fuel_l)
        self.fuel_l = inp.fuel_l
        early = self.drive_t is not None and inp.now - self.drive_t < 120.0
        slow = inp.real_kmh is None or inp.real_kmh < 10.0
        # service
        if early and slow:
            rows = [r for r in self.services(inp.wall) if r["status"] in ("due", "overdue")]
            if rows:
                r = min(rows, key=lambda x: (x["remaining_km"] if x["remaining_km"] is not None else 1e9))
                if r["status"] == "overdue":
                    txt = "%s OVERDUE" % r["name"]
                elif r["remaining_km"] is not None and r["remaining_km"] <= self.t.warn_km:
                    txt = "%s IN %dkm" % (r["name"], max(0, round(r["remaining_km"])))
                else:
                    txt = "%s IN %dd" % (r["name"], max(0, round(r["remaining_days"] or 0)))
                out[SERVICE] = Candidate(text=txt, key="drive", evidence={
                    "item": r["id"], "odometer_km": self.odo, "remaining_km": r["remaining_km"],
                    "remaining_days": r["remaining_days"]})
        # range
        self.l100, self.l100_basis = self._consumption(inp)
        self.range_km = (inp.fuel_l / self.l100 * 100.0) if (inp.fuel_l is not None and self.l100) else None
        if self.range_km is not None and self.range_km < self.t.range_warn_km:
            out[RANGE] = Candidate(text="RANGE ~%dkm (PETROL)" % round(self.range_km), key="drive", evidence={
                "fuel_l": inp.fuel_l, "l100": round(self.l100, 2), "basis": self.l100_basis,
                "range_km": round(self.range_km)})
        # new DTCs
        if self.t.dtc_notice and inp.dtc_source_seen:
            known = self._mem("known_dtcs", None)
            cur = sorted(inp.dtcs)
            if known is None:
                self._set("known_dtcs", cur)            # baseline: no notice for existing codes
            else:
                new = [c for c in cur if c not in known]
                gone = [c for c in known if c not in inp.dtcs]
                if gone:
                    self._set("known_dtcs", [c for c in known if c in inp.dtcs])
                if new and new != self.new_codes:
                    self.new_codes = new
                    self._new_since = None
                if self.new_codes:
                    if slow:
                        if self._new_since is None:
                            self._new_since = inp.now     # 30 s at a standstill, then known
                        codes = [c.split(":", 1)[-1] for c in self.new_codes]
                        out[DTC] = Candidate(text="NEW FAULT %s" % " ".join(codes[:2]),
                                             key=",".join(self.new_codes),
                                             evidence={"codes": self.new_codes, "mil": inp.mil_on})
                        if self._new_since is not None and inp.now - self._new_since > 30.0:
                            self._set("known_dtcs", sorted(set(self._mem("known_dtcs", [])) | set(self.new_codes)))
                            self.new_codes = []
        return out

    def status(self):
        return {"range_km": None if self.range_km is None else round(self.range_km),
                "l100": None if self.l100 is None else round(self.l100, 2), "l100_basis": self.l100_basis,
                "fuel_l": self.fuel_l, "odometer_km": self.odo, "note": LPG_NOTE,
                "services": self.services(), "known_dtcs": self._mem("known_dtcs", None)}
