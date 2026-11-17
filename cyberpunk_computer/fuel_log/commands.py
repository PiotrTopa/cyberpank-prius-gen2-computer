"""
REST / ZMQ commands of the fuelling log, registered into
``api.commands.COMMANDS`` by :func:`register`:

    fuel_log_add        {fuel, litres | total + price_per_l, price_per_l?, full?, ts?, odometer_km?, note?}
    fuel_log_undo       {}            remove the most recently entered fill
    fuel_log_edit_last  {any add field}  change the most recently entered fill

Builders validate everything that needs no car state; the service applies
the rest (odometer available, ordering warnings) and reports the result in
``fuel_log.last_command``.
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple, Type

from .actions import FuelLogCommandAction

LOCAL_TZ = "Europe/Warsaw"
LITRES_RANGE = (0.5, 200.0)
TOTAL_RANGE = (0.5, 3000.0)
PRICE_RANGE = (0.5, 20.0)
ODO_RANGE = (0.0, 2_000_000.0)
MAX_AGE_S = 400 * 86400


def parse_ts(value: Any) -> float:
    """Epoch seconds, or an ISO-8601 string; a string without an offset is
    local time (Europe/Warsaw), e.g. '2026-11-17 16:30'."""
    if isinstance(value, bool):
        raise ValueError("ts must be epoch seconds or an ISO date-time")
    if isinstance(value, (int, float)):
        ts = float(value)
    else:
        s = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            raise ValueError("ts must be epoch seconds or an ISO date-time like '2026-11-17T16:30'")
        if dt.tzinfo is None:
            from zoneinfo import ZoneInfo
            dt = dt.replace(tzinfo=ZoneInfo(LOCAL_TZ))
        ts = dt.timestamp()
    now = time.time()
    if not math.isfinite(ts) or ts > now + 600:
        raise ValueError("ts lies in the future")
    if ts < now - MAX_AGE_S:
        raise ValueError("ts is more than 400 days old")
    return ts


def resolve_amounts(litres: Optional[float], total: Optional[float],
                    price: Optional[float]) -> Tuple[float, Optional[float], Optional[float]]:
    """(litres, price_per_l, total) from what was given: litres directly, or
    total / price_per_l. With all three they must agree within 1 %."""
    if litres is None:
        if total is None or price is None:
            raise ValueError("give 'litres', or 'total' together with 'price_per_l'")
        litres = round(total / price, 2)
    elif total is not None and price is not None:
        if abs(litres * price - total) > max(0.05, 0.01 * total):
            raise ValueError("litres x price_per_l (%.2f) does not match total %.2f"
                             % (litres * price, total))
    elif total is not None and price is None:
        price = round(total / litres, 3)
    if not LITRES_RANGE[0] <= litres <= LITRES_RANGE[1]:
        raise ValueError("litres must be between %g and %g" % LITRES_RANGE)
    if total is None and price is not None:
        total = round(litres * price, 2)
    return float(litres), price, total


def register(commands: Dict[str, Dict[str, Any]], err: Type[Exception]) -> None:
    """Add the ``fuel_log_*`` commands to the API command table."""

    def num(params: Dict[str, Any], key: str, rng: Tuple[float, float]) -> Optional[float]:
        v = params.get(key)
        if v is None or v == "":
            return None
        if isinstance(v, bool):
            raise err("parameter '%s' must be a number" % key)
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise err("parameter '%s' must be a number" % key)
        if not (math.isfinite(f) and rng[0] <= f <= rng[1]):
            raise err("parameter '%s' must be between %g and %g" % (key, rng[0], rng[1]))
        return f

    def boolean(params: Dict[str, Any], key: str) -> Optional[bool]:
        v = params.get(key)
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)) and v in (0, 1):
            return bool(v)
        if isinstance(v, str) and v.strip().lower() in ("true", "false", "1", "0", "yes", "no"):
            return v.strip().lower() in ("true", "1", "yes")
        raise err("parameter '%s' must be a boolean" % key)

    def common(params: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {"fuel", "litres", "total", "price_per_l", "full", "ts", "odometer_km", "note"}
        unknown = [k for k in params if k not in allowed]
        if unknown:
            raise err("unknown parameter(s): %s" % ", ".join(sorted(unknown)))
        out: Dict[str, Any] = {}
        if params.get("fuel") is not None:
            fuel = str(params["fuel"]).strip().lower()
            if fuel not in ("lpg", "petrol"):
                raise err("'fuel' must be 'lpg' or 'petrol'")
            out["fuel"] = fuel
        for key, rng in (("litres", LITRES_RANGE), ("total", TOTAL_RANGE),
                         ("price_per_l", PRICE_RANGE), ("odometer_km", ODO_RANGE)):
            v = num(params, key, rng)
            if v is not None:
                out[key] = v
        full = boolean(params, "full")
        if full is not None:
            out["full"] = full
        if params.get("ts") not in (None, ""):
            try:
                out["ts"] = parse_ts(params["ts"])
            except ValueError as exc:
                raise err(str(exc))
        if params.get("note") is not None:
            note = str(params["note"]).strip()
            if len(note) > 200:
                raise err("'note' must be at most 200 characters")
            out["note"] = note
        return out

    def build_add(params: Dict[str, Any]) -> FuelLogCommandAction:
        p = common(params or {})
        if "fuel" not in p:
            raise err("missing required parameter 'fuel' (lpg | petrol)")
        try:
            litres, price, total = resolve_amounts(p.get("litres"), p.get("total"), p.get("price_per_l"))
        except ValueError as exc:
            raise err(str(exc))
        p.update(litres=litres, price_per_l=price, total=total)
        p.setdefault("full", True)
        return FuelLogCommandAction("add", p)

    def build_undo(params: Dict[str, Any]) -> FuelLogCommandAction:
        p: Dict[str, Any] = {}
        if (params or {}).get("id") is not None:
            try:
                p["id"] = int(params["id"])
            except (TypeError, ValueError):
                raise err("'id' must be an integer")
        return FuelLogCommandAction("undo", p)

    def build_edit(params: Dict[str, Any]) -> FuelLogCommandAction:
        p = common(params or {})
        if not p:
            raise err("give at least one field to change")
        if p.get("litres") is not None and p.get("total") is not None and p.get("price_per_l") is not None:
            try:
                resolve_amounts(p["litres"], p["total"], p["price_per_l"])
            except ValueError as exc:
                raise err(str(exc))
        return FuelLogCommandAction("edit_last", p)

    fields_doc = {
        "fuel": "lpg | petrol",
        "litres": "float 0.5..200 (or give total + price_per_l)",
        "total": "float, amount paid (PLN); litres = total / price_per_l",
        "price_per_l": "float 0.5..20 (optional with litres)",
        "full": "bool (default true: tank filled to the cut-off)",
        "ts": "epoch s or ISO date-time; no offset = Europe/Warsaw (optional, default now)",
        "odometer_km": "float, backend odometer at the fill (optional; default live "
                       "vehicle.odometer_km, or for a back-dated ts the live value minus "
                       "the km driven since, from the metrics speed record)",
        "note": "string <= 200 (optional)",
    }
    commands["fuel_log_add"] = {
        "builder": build_add,
        "description": (
            "Fuelling log: register a fill-up (docs/FUEL_LOG.md). Stored with the "
            "backend's own odometer (the meter's is frozen at 299,999 km) and what the car "
            "knows at that moment. Result in fuel_log.last_command, figures in fuel_log.figures."
        ),
        "params": fields_doc,
    }
    commands["fuel_log_undo"] = {
        "builder": build_undo,
        "description": "Fuelling log: remove the most recently entered fill (kept in the "
                       "database as deleted). 'id', when given, must be that fill.",
        "params": {"id": "int (optional safety check)"},
    }
    commands["fuel_log_edit_last"] = {
        "builder": build_edit,
        "description": "Fuelling log: change fields of the most recently entered fill. "
                       "Changing total or price_per_l without litres recomputes litres.",
        "params": {k: v.replace(" (default true: tank filled to the cut-off)", "") for k, v in fields_doc.items()},
    }
