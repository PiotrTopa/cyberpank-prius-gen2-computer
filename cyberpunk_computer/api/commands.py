"""
Command registry — maps network command names (used by the Android app and any
REST client) onto the engine's existing Action objects.

Every command builder validates its parameters and raises :class:`CommandError`
on bad input (the API layer turns that into HTTP 400) or :class:`UnknownCommand`
for an unrecognized name (HTTP 404). Built actions use ``ActionSource.UI`` so the
egress middleware forwards them to the vehicle, exactly like a touch on the
pygame UI would.

NOTE: vehicle-actuation commands (ready/start, climate) currently drive the
virtual twin's state and rely on egress middleware for real bus output. Physical
remote-start interlocks are intentionally NOT wired here yet — see backend TODO.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from ..state.actions import (
    Action,
    ActionSource,
    BatchAction,
    SetACAction,
    SetAirDirectionAction,
    SetAutoModeAction,
    SetBalanceAction,
    SetBassAction,
    SetFaderAction,
    SetFanSpeedAction,
    SetMidAction,
    SetMuteAction,
    SetReadyModeAction,
    SetRecirculationAction,
    SetTargetTempAction,
    SetTrebleAction,
    SetVolumeAction,
)


class CommandError(ValueError):
    """Invalid parameters for a known command (-> HTTP 400)."""


class UnknownCommand(KeyError):
    """No command registered under the requested name (-> HTTP 404)."""


CommandBuilder = Callable[[Dict[str, Any]], Action]


# ── parameter helpers ────────────────────────────────────────────────────────

def _require(params: Dict[str, Any], key: str) -> Any:
    if key not in params or params[key] is None:
        raise CommandError(f"missing required parameter '{key}'")
    return params[key]


def _as_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in ("true", "false", "0", "1"):
        return value.lower() in ("true", "1")
    raise CommandError(f"parameter '{key}' must be a boolean")


def _as_int(value: Any, key: str, lo: int, hi: int) -> int:
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        raise CommandError(f"parameter '{key}' must be an integer")
    if not lo <= ivalue <= hi:
        raise CommandError(f"parameter '{key}' must be between {lo} and {hi}")
    return ivalue


def _as_float(value: Any, key: str, lo: float, hi: float) -> float:
    try:
        fvalue = float(value)
    except (TypeError, ValueError):
        raise CommandError(f"parameter '{key}' must be a number")
    if not lo <= fvalue <= hi:
        raise CommandError(f"parameter '{key}' must be between {lo} and {hi}")
    return fvalue


# ── command builders ─────────────────────────────────────────────────────────

def _build_set_volume(params: Dict[str, Any]) -> Action:
    volume = _as_int(_require(params, "value"), "value", 0, 63)
    return SetVolumeAction(volume, source=ActionSource.UI)


def _build_set_mute(params: Dict[str, Any]) -> Action:
    return SetMuteAction(_as_bool(_require(params, "muted"), "muted"), source=ActionSource.UI)


def _build_set_tone(params: Dict[str, Any]) -> Action:
    actions: List[Action] = []
    if params.get("bass") is not None:
        actions.append(SetBassAction(_as_int(params["bass"], "bass", -5, 5), source=ActionSource.UI))
    if params.get("mid") is not None:
        actions.append(SetMidAction(_as_int(params["mid"], "mid", -5, 5), source=ActionSource.UI))
    if params.get("treble") is not None:
        actions.append(SetTrebleAction(_as_int(params["treble"], "treble", -5, 5), source=ActionSource.UI))
    if params.get("balance") is not None:
        actions.append(SetBalanceAction(_as_int(params["balance"], "balance", -7, 7), source=ActionSource.UI))
    if params.get("fader") is not None:
        actions.append(SetFaderAction(_as_int(params["fader"], "fader", -7, 7), source=ActionSource.UI))
    if not actions:
        raise CommandError("set_tone needs at least one of: bass, mid, treble, balance, fader")
    return actions[0] if len(actions) == 1 else BatchAction(actions, source=ActionSource.UI)


def _build_set_climate(params: Dict[str, Any]) -> Action:
    actions: List[Action] = []
    if "ac" in params and params["ac"] is not None:
        actions.append(SetACAction(_as_bool(params["ac"], "ac"), source=ActionSource.UI))
    if "auto" in params and params["auto"] is not None:
        actions.append(SetAutoModeAction(_as_bool(params["auto"], "auto"), source=ActionSource.UI))
    if "target_temp" in params and params["target_temp"] is not None:
        actions.append(
            SetTargetTempAction(_as_float(params["target_temp"], "target_temp", 18.0, 28.0), source=ActionSource.UI)
        )
    if "fan" in params and params["fan"] is not None:
        actions.append(SetFanSpeedAction(_as_int(params["fan"], "fan", 0, 7), source=ActionSource.UI))
    if "recirculation" in params and params["recirculation"] is not None:
        actions.append(
            SetRecirculationAction(_as_bool(params["recirculation"], "recirculation"), source=ActionSource.UI)
        )
    if "air_direction" in params and params["air_direction"] is not None:
        actions.append(
            SetAirDirectionAction(_as_int(params["air_direction"], "air_direction", 0, 3), source=ActionSource.UI)
        )
    if not actions:
        raise CommandError(
            "set_climate needs at least one of: ac, auto, target_temp, fan, recirculation, air_direction"
        )
    if len(actions) == 1:
        return actions[0]
    return BatchAction(actions, source=ActionSource.UI)


def _build_climate_on(params: Dict[str, Any]) -> Action:
    return SetACAction(True, source=ActionSource.UI)


def _build_climate_off(params: Dict[str, Any]) -> Action:
    return SetACAction(False, source=ActionSource.UI)


def _build_set_ready(params: Dict[str, Any]) -> Action:
    return SetReadyModeAction(_as_bool(_require(params, "on"), "on"), source=ActionSource.UI)


def _build_start(params: Dict[str, Any]) -> Action:
    # Remote start request: drives the twin into READY. Physical interlocks and
    # the real ignition sequence are handled by egress/powerbox (TODO).
    return SetReadyModeAction(True, source=ActionSource.UI)


def _build_stop(params: Dict[str, Any]) -> Action:
    return SetReadyModeAction(False, source=ActionSource.UI)


def _build_set_out(params: Dict[str, Any]) -> Action:
    from ..state.actions import SetOutAction
    ch = _as_int(_require(params, "channel"), "channel", 2, 3)
    on = _as_bool(_require(params, "on"), "on")
    return SetOutAction(ch, on, source=ActionSource.UI)


def _build_set_fan(params: Dict[str, Any]) -> Action:
    from ..state.actions import SetFanOverrideAction
    pct = _as_float(_require(params, "pct"), "pct", 0.0, 100.0)
    return SetFanOverrideAction(pct, source=ActionSource.UI)


def _build_fan_auto(params: Dict[str, Any]) -> Action:
    from ..state.actions import SetFanOverrideAction
    return SetFanOverrideAction(None, source=ActionSource.UI)


# name -> (builder, description, params-doc)
COMMANDS: Dict[str, Dict[str, Any]] = {
    "set_volume": {
        "builder": _build_set_volume,
        "description": "Set audio volume.",
        "params": {"value": "int 0..63"},
    },
    "set_mute": {
        "builder": _build_set_mute,
        "description": "Mute or unmute audio.",
        "params": {"muted": "bool"},
    },
    "set_tone": {
        "builder": _build_set_tone,
        "description": "Set one or more audio tone controls.",
        "params": {
            "bass": "int -5..5 (optional)",
            "mid": "int -5..5 (optional)",
            "treble": "int -5..5 (optional)",
            "balance": "int -7..7 (optional)",
            "fader": "int -7..7 (optional)",
        },
    },
    "set_climate": {
        "builder": _build_set_climate,
        "description": "Set one or more climate parameters.",
        "params": {
            "ac": "bool (optional)",
            "auto": "bool (optional)",
            "target_temp": "float 18..28 (optional)",
            "fan": "int 0..7 (optional)",
            "recirculation": "bool (optional)",
            "air_direction": "int 0..3 (optional)",
        },
    },
    "climate_on": {"builder": _build_climate_on, "description": "Turn AC on.", "params": {}},
    "climate_off": {"builder": _build_climate_off, "description": "Turn AC off.", "params": {}},
    "set_ready": {
        "builder": _build_set_ready,
        "description": "Set vehicle READY mode.",
        "params": {"on": "bool"},
    },
    "start": {"builder": _build_start, "description": "Remote start (enter READY).", "params": {}},
    "stop": {"builder": _build_stop, "description": "Leave READY mode.", "params": {}},
    "set_out": {
        "builder": _build_set_out,
        "description": "Set powerbox OUT2 or OUT3.",
        "params": {"channel": "int 2..3", "on": "bool"},
    },
    "set_fan": {
        "builder": _build_set_fan,
        "description": "Manually pin the chassis fan duty (overrides automatic control).",
        "params": {"pct": "float 0..100"},
    },
    "fan_auto": {
        "builder": _build_fan_auto,
        "description": "Clear the chassis fan override and return to automatic control.",
        "params": {},
    },
}


def build_command(name: str, params: Dict[str, Any]) -> Action:
    """Build the Action for ``name`` from ``params``.

    Raises :class:`UnknownCommand` for unknown names and :class:`CommandError`
    for invalid parameters.
    """
    entry = COMMANDS.get(name)
    if entry is None:
        raise UnknownCommand(name)
    return entry["builder"](params or {})


def command_catalog() -> List[Dict[str, Any]]:
    """Return a JSON-friendly description of all available commands."""
    return [
        {"name": name, "description": entry["description"], "params": entry["params"]}
        for name, entry in COMMANDS.items()
    ]
