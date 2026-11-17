"""
Backend side of the fuelling log: command middleware, SQLite store, the
cumulative estimator counter and the ``fuel_log`` state section.

``wire_fuel_log(twin, db_path, metrics_db)`` is called by the backend service
(live data only). All work runs on the engine thread (store middleware and
subscriber callbacks).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from . import model
from .actions import FuelLogCommandAction, SetFuelLogStateAction
from .commands import resolve_amounts
from .store import FuelLogStore

logger = logging.getLogger(__name__)

#: Entries published in the state section (newest first).
STATE_ENTRIES = 12
#: Republish the figures at most this often while the odometer moves.
REFRESH_S = 30.0
#: Persist the estimator counter at most this often.
EST_SAVE_S = 30.0
#: Back-dated odometer from the metrics speed record: raw 1 Hz samples are
#: kept 2 days; a gap longer than this between samples counts as standstill.
SPEED_GAP_S = 5.0
RAW_SPEED_MAX_AGE_S = 2 * 86400 - 600

EST_KEY = "est_total_l"


def car_snapshot(state: Any) -> Dict[str, Any]:
    """What the car / backend knows at the moment an entry is made."""
    v = state.vehicle
    af = getattr(v, "active_fuel", None)
    return {
        "odometer_km": v.odometer_km,
        "distance_total_km": round(float(v.distance_total_km or 0.0), 3),
        "fuel_level": v.fuel_level,                      # petrol gauge (0x5A4), litres
        "active_fuel": getattr(af, "name", af),
        "active_fuel_detected": bool(getattr(v, "active_fuel_detected", False)),
        "fuel_basis": getattr(v, "fuel_basis", None),
        "trip_fuel_consumed_l": round(float(v.trip_fuel_consumed or 0.0), 3),
        "drive_distance_km": round(float(v.drive_distance_km or 0.0), 3),
        "meter_trip_km": v.meter_trip_km,
        "meter_trip_fuel_l": v.meter_trip_fuel_l,
        "meter_avg_consumption_l100km": v.meter_avg_consumption_l100km,
    }


class FuelLogService:
    def __init__(self, db: FuelLogStore, store: Any, metrics_db: Any = None,
                 clock=time.time) -> None:
        self.db = db
        self.store = store
        self.metrics_db = metrics_db
        self._clock = clock
        raw = db.kv_get(EST_KEY)
        self.est_total_l: float = float(raw) if raw else 0.0
        self._est_last_trip: Optional[float] = None
        self._est_saved_at = 0.0
        self._published_odo: Optional[float] = None
        self._published_at = 0.0
        self.last_command: Optional[Dict[str, Any]] = None

    # ── estimator counter ────────────────────────────────────────────────

    def observe(self, state: Any) -> None:
        """Integrate ``vehicle.trip_fuel_consumed`` (resets at IG-ON and at
        backend start) into a monotonic counter; refresh figures now and then."""
        try:
            trip = float(state.vehicle.trip_fuel_consumed or 0.0)
        except Exception:
            return
        last = self._est_last_trip
        if last is not None:
            if trip >= last:
                self.est_total_l += trip - last
            else:                       # reset: the new drive starts at 0
                self.est_total_l += trip
        self._est_last_trip = trip
        now = self._clock()
        if now - self._est_saved_at >= EST_SAVE_S:
            self._est_saved_at = now
            self.db.kv_set(EST_KEY, "%.4f" % self.est_total_l)
        odo = state.vehicle.odometer_km
        if (now - self._published_at >= REFRESH_S and odo is not None
                and (self._published_odo is None or abs(odo - self._published_odo) >= 0.1)):
            self.publish()

    def on_state(self, state: Any) -> None:
        try:
            self.observe(state)
        except Exception:
            logger.exception("fuel log: observe failed")

    # ── odometer ─────────────────────────────────────────────────────────

    def odometer_at(self, ts: float, odo_now: float, now: Optional[float] = None) -> Optional[float]:
        """Backend odometer at past ``ts``: the live value minus the km driven
        since, integrated from the 1 Hz ``speed`` metric (same coverage as the
        backend odometer: both only advance while the backend runs). None when
        the raw record does not reach back that far."""
        now = self._clock() if now is None else now
        if self.metrics_db is None or now - ts > RAW_SPEED_MAX_AGE_S:
            return None
        try:
            conn = self.metrics_db.connect()
            try:
                rows = conn.execute(
                    "SELECT ts, value FROM metrics_raw WHERE signal = 'speed' AND ts >= ? "
                    "ORDER BY ts", (ts,)).fetchall()
                oldest = conn.execute("SELECT MIN(ts) FROM metrics_raw").fetchone()
            finally:
                conn.close()
        except Exception:
            logger.exception("fuel log: metrics speed record unreadable")
            return None
        if not oldest or oldest[0] is None or oldest[0] > ts + 60:
            return None
        km = 0.0
        for (t0, v0), (t1, _v1) in zip(rows, rows[1:]):
            dt = t1 - t0
            if 0 < dt <= SPEED_GAP_S and v0 and v0 > 0:
                km += v0 * dt / 3600.0
        return odo_now - km

    # ── commands ─────────────────────────────────────────────────────────

    def _result(self, op: str, ok: bool, error: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
        r = {"op": op, "ok": ok, "error": error, "ts": self._clock()}
        r.update(extra)
        self.last_command = r
        (logger.info if ok else logger.warning)("fuel log %s: %s", op, "ok" if ok else error)
        return r

    def handle(self, action: FuelLogCommandAction) -> Dict[str, Any]:
        op, p = action.op, dict(action.params)
        if op == "add":
            r = self._add(p)
        elif op == "undo":
            r = self._undo(p)
        elif op == "edit_last":
            r = self._edit_last(p)
        else:
            r = self._result(op, False, "unknown op")
        self.publish()
        return r

    def _ordering_warnings(self, fid: Optional[int], ts: float, odo: float) -> List[str]:
        warn = []
        for e in self.db.entries():
            if e["id"] == fid:
                continue
            if (e["ts"] < ts and e["odometer_km"] > odo + 0.05) or (e["ts"] > ts and e["odometer_km"] < odo - 0.05):
                warn.append("odometer order disagrees with time order against fill #%d" % e["id"])
        return warn

    def _add(self, p: Dict[str, Any]) -> Dict[str, Any]:
        st = self.store.state
        now = self._clock()
        if p.get("fuel") not in model.FUELS:
            return self._result("add", False, "'fuel' must be 'lpg' or 'petrol'")
        try:   # actions built in-process (local MFD) skip the command builder
            litres, price, total = resolve_amounts(p.get("litres"), p.get("total"), p.get("price_per_l"))
        except ValueError as exc:
            return self._result("add", False, str(exc))
        p.update(litres=litres, price_per_l=price, total=total)
        ts = float(p.get("ts") or now)
        back_dated = now - ts > 300
        odo_live = st.vehicle.odometer_km
        if p.get("odometer_km") is not None:
            odo, source = float(p["odometer_km"]), "given"
        elif not back_dated:
            if odo_live is None:
                return self._result("add", False, "backend odometer not known yet; give odometer_km")
            odo, source = float(odo_live), "live"
        else:
            odo = self.odometer_at(ts, float(odo_live)) if odo_live is not None else None
            if odo is None:
                return self._result("add", False, "back-dated fill: no speed record that far back; give odometer_km")
            source = "metrics_speed"
        entry = {
            "ts": ts, "fuel": p["fuel"], "litres": float(p["litres"]),
            "price_per_l": p.get("price_per_l"), "total": p.get("total"),
            "full": bool(p.get("full", True)),
            "odometer_km": round(odo, 2), "odometer_source": source,
            # Estimator counter and car snapshot only describe the fill when it
            # is entered at the pump; a back-dated entry gets none.
            "est_total_l": None if back_dated else round(self.est_total_l, 4),
            "car": None if back_dated else car_snapshot(st),
            "note": p.get("note"), "created_at": now,
        }
        warnings = self._ordering_warnings(None, ts, odo)
        fid = self.db.add(entry)
        return self._result("add", True, id=fid, odometer_km=entry["odometer_km"],
                            odometer_source=source, litres=entry["litres"], warnings=warnings)

    def _undo(self, p: Dict[str, Any]) -> Dict[str, Any]:
        fid = self.db.last_id()
        if fid is None:
            return self._result("undo", False, "the log is empty")
        if p.get("id") is not None and int(p["id"]) != fid:
            return self._result("undo", False, "fill #%s is not the last one (#%d)" % (p["id"], fid))
        self.db.soft_delete(fid)
        return self._result("undo", True, id=fid)

    def _edit_last(self, p: Dict[str, Any]) -> Dict[str, Any]:
        fid = self.db.last_id()
        if fid is None:
            return self._result("edit_last", False, "the log is empty")
        cur = self.db.get(fid)
        merged = dict(cur)
        merged.update(p)
        litres = p.get("litres")
        if litres is None and ("total" in p or "price_per_l" in p):
            if merged.get("total") is not None and merged.get("price_per_l") is not None:
                litres = None           # recompute from total / price
            else:
                litres = merged["litres"]
        elif litres is None:
            litres = merged["litres"]
        total = merged.get("total")
        if "litres" in p and "total" not in p:
            total = None               # paid amount no longer matches: recompute from price
        try:
            litres, price, total = resolve_amounts(litres, total, merged.get("price_per_l"))
        except ValueError as exc:
            return self._result("edit_last", False, str(exc))
        fields = dict(p)
        fields.update(litres=litres, price_per_l=price, total=total)
        if "odometer_km" in p:
            fields["odometer_source"] = "given"
        self.db.update(fid, fields)
        new = self.db.get(fid)
        warnings = self._ordering_warnings(fid, new["ts"], new["odometer_km"])
        return self._result("edit_last", True, id=fid, warnings=warnings)

    def middleware(self, action: Any, _store: Any) -> None:
        if isinstance(action, FuelLogCommandAction):
            try:
                self.handle(action)
            except Exception as exc:
                logger.exception("fuel log command %s failed", action.op)
                self._result(action.op, False, "internal error: %s" % exc)
                self.publish()

    # ── state ────────────────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        entries = self.db.entries()
        st = self.store.state
        odo = st.vehicle.odometer_km
        figures = model.compute_figures(entries, odometer_now=odo, est_total_now=self.est_total_l)
        figures["est_total_l"] = round(self.est_total_l, 3)
        newest = sorted(model.sort_entries(entries), key=lambda e: (e["ts"], e["id"]), reverse=True)
        slim = []
        for e in newest[:STATE_ENTRIES]:
            d = {k: e[k] for k in ("id", "ts", "fuel", "litres", "price_per_l", "total", "full",
                                   "odometer_km", "odometer_source", "est_total_l", "note")}
            d["car"] = e.get("car")
            slim.append(d)
        return {
            "enabled": True, "entries": tuple(slim), "figures": figures,
            "defaults": model.defaults(entries), "last_command": self.last_command,
            "count": len(entries), "updated_at": self._clock(),
        }

    def publish(self) -> None:
        try:
            snap = self.snapshot()
        except Exception:
            logger.exception("fuel log: snapshot failed")
            return
        self._published_odo = self.store.state.vehicle.odometer_km
        self._published_at = self._clock()
        self.store.dispatch(SetFuelLogStateAction(snap))


def wire_fuel_log(twin: Any, db_path: str, metrics_db: Any = None) -> FuelLogService:
    from ..state.store import StateSlice
    store = twin.store
    svc = FuelLogService(FuelLogStore(db_path), store, metrics_db)
    store.add_middleware(svc.middleware)
    store.subscribe(StateSlice.VEHICLE, svc.on_state)
    svc.publish()
    logger.info("Fuel log wired (db=%s, %d fills, estimator counter %.2f l)",
                db_path, len(svc.db.entries()), svc.est_total_l)
    return svc
