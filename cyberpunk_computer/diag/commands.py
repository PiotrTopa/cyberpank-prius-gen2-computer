"""
REST / network commands of the diagnostics engine.

Registered into ``api.commands.COMMANDS`` by :func:`register`. Builders
validate everything that can be validated without the car (ECU names, kinds,
the confirm flag, the safety allow-list for ``diag_read``) and raise the API's
``CommandError`` (HTTP 400); the engine applies the run-time gates (vehicle
speed, scan in progress, rate limits).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Type

from . import isotp, services
from .actions import DiagCommandAction
from .autoclear import validate_config
from .ecus import ECUS
from .engine import ALL_KINDS


def _ecu_list(params: Dict[str, Any], err: Type[Exception]) -> Optional[List[str]]:
    raw = params.get("ecus")
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise err("ecus must be a non-empty list of ECU names")
    for k in raw:
        if k not in ECUS:
            raise err("unknown ECU %r (known: %s)" % (k, ", ".join(ECUS)))
    return list(raw)


def _ecu(params: Dict[str, Any], err: Type[Exception]) -> str:
    key = params.get("ecu")
    if key not in ECUS:
        raise err("ecu must be one of: %s" % ", ".join(ECUS))
    return key


def register(commands: Dict[str, Dict[str, Any]], err: Type[Exception]) -> None:
    """Add the ``diag_*`` commands to the API command table."""

    def build_scan(params: Dict[str, Any]) -> DiagCommandAction:
        out: Dict[str, Any] = {}
        ecus = _ecu_list(params, err)
        if ecus is not None:
            out["ecus"] = ecus
        kinds = params.get("kinds")
        if kinds is not None:
            if isinstance(kinds, str):
                kinds = [kinds]
            if not isinstance(kinds, list) or not kinds or any(k not in ALL_KINDS for k in kinds):
                raise err("kinds must be a list out of: %s" % ", ".join(ALL_KINDS))
            out["kinds"] = list(kinds)
        if "probe" in params:
            if not isinstance(params["probe"], bool):
                raise err("probe must be a boolean")
            out["probe"] = params["probe"]
        return DiagCommandAction("scan", out)

    def build_identify(params: Dict[str, Any]) -> DiagCommandAction:
        ecus = _ecu_list(params, err)
        return DiagCommandAction("identify", {"ecus": ecus} if ecus is not None else {})

    def build_read(params: Dict[str, Any]) -> DiagCommandAction:
        key = _ecu(params, err)
        ecu = ECUS[key]
        item = params.get("item")
        if not isinstance(item, str) or not item.strip():
            raise err("item must be a data-list name or a hex request such as '21C3'")
        item = item.strip()
        if not ecu.tx_id:
            raise err("%s has no CAN address" % ecu.name)
        try:
            payload = bytes(ecu.items[item]) if item in ecu.items else services.parse_hex_request(item)
            services.check_request(payload)
            isotp.encode_single(payload, ecu.sub_addr)
        except (ValueError, services.DiagSafetyError) as exc:
            raise err("item: %s (named items of %s: %s)" % (exc, key, ", ".join(ecu.items) or "none"))
        return DiagCommandAction("read", {"ecu": key, "item": item})

    def build_clear(params: Dict[str, Any]) -> DiagCommandAction:
        key = _ecu(params, err)
        if params.get("confirm") is not True:
            raise err("diag_clear erases every code and the freeze frame of the ECU: "
                      "send confirm=true to go ahead")
        if not ECUS[key].clear_requests():
            raise err("no clear service is known for %s on CAN" % ECUS[key].name)
        allow_moving = params.get("allow_moving", False)
        if not isinstance(allow_moving, bool):
            raise err("allow_moving must be a boolean")
        return DiagCommandAction("clear", {"ecu": key, "confirm": True, "allow_moving": allow_moving})

    def build_cancel(_params: Dict[str, Any]) -> DiagCommandAction:
        return DiagCommandAction("cancel", {})

    def build_autoclear(params: Dict[str, Any]) -> DiagCommandAction:
        allowed = {"enabled", "codes", "trigger", "speed_policy", "min_interval_s", "max_per_day", "poll_s"}
        unknown = [k for k in params if k not in allowed]
        if unknown:
            raise err("unknown parameter(s): %s" % ", ".join(unknown))
        if not params:
            raise err("nothing to set (enabled, codes, trigger, speed_policy, min_interval_s, max_per_day, poll_s)")
        try:
            validate_config(params, ECUS.keys())
        except ValueError as exc:
            raise err(str(exc))
        for c in params.get("codes", []):
            if not ECUS[c["ecu"]].clear_requests():
                raise err("no clear service is known for %s on CAN" % ECUS[c["ecu"]].name)
        return DiagCommandAction("autoclear_set", dict(params))

    names = ", ".join(ECUS)
    commands["diag_scan"] = {
        "builder": build_scan,
        "description": "All-ECU diagnostics scan through the gateway (read-only). Results in "
                       "state.diagnostics.ecus / .scan; a JSON report is written per scan "
                       "(GET /api/v1/diag/report).",
        "params": {"ecus": "list of ECU names (optional; default all): " + names,
                   "kinds": "list out of %s (optional; default stored, pending, freeze)" % ", ".join(ALL_KINDS),
                   "probe": "bool (optional): also send one tester-present frame to the unverified CAN addresses"},
    }
    commands["diag_identify"] = {
        "builder": build_identify,
        "description": "Read ECU identification (VIN, calibration ids, Toyota 1A 88 01) — read-only.",
        "params": {"ecus": "list of ECU names (optional; default all)"},
    }
    commands["diag_read"] = {
        "builder": build_read,
        "description": "One read-only request to one ECU: a named data-list item or a hex request "
                       "(read-only services only). Result in state.diagnostics.last_read.",
        "params": {"ecu": "ECU name", "item": "data-list name or hex, e.g. 'hybrid_system', '21C3', '13'"},
    }
    commands["diag_clear"] = {
        "builder": build_clear,
        "description": "Clear ALL DTCs + freeze frame of one ECU (OBD mode 04). Codes and freeze "
                       "frame are read and logged first. Refused unless the vehicle is stopped.",
        "params": {"ecu": "engine | hv | battery", "confirm": "must be true",
                   "allow_moving": "bool (optional, default false): skip the speed-0 gate"},
    }
    commands["diag_cancel"] = {
        "builder": build_cancel,
        "description": "Abandon the running diagnostics scan and drop queued diagnostics jobs.",
        "params": {},
    }
    commands["diag_autoclear_set"] = {
        "builder": build_autoclear,
        "description": "Configure auto-clear of whitelisted recurring DTCs (persisted). An ECU is "
                       "cleared only when every stored and pending code on it is whitelisted.",
        "params": {"enabled": "bool master switch (default off)",
                   "codes": "list of {ecu, code, enabled?} — replaces the whitelist",
                   "trigger": "stored | pending (default stored)",
                   "speed_policy": "stopped | anytime (default stopped)",
                   "min_interval_s": "60..86400 (default 600)", "max_per_day": "0..48 (default 6)",
                   "poll_s": "10..3600 (default 30)"},
    }
