"""
Command registry — maps network command names (used by the Android app and any
REST client) onto the engine's existing Action objects.

Every command builder validates its parameters and raises :class:`CommandError`
on bad input (the API layer turns that into HTTP 400) or :class:`UnknownCommand`
for an unrecognized name (HTTP 404). Built actions use ``ActionSource.UI`` so the
egress middleware forwards them to the vehicle, exactly like a touch on the
pygame UI would.

NOTE: climate commands (``set_climate``, ``climate_button`` / ``climate_*``)
reach the car as verified AVC-LAN touch-button presses (egress ->
comm.avc.ClimateRemote). Ready/start only drive the twin's state; physical
remote-start interlocks are intentionally NOT wired here yet.
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


def _build_powerbox_button(params: Dict[str, Any]) -> Action:
    from ..state.actions import PowerboxButtonAction
    poco = _as_int(params.get("poco", 1), "poco", 1, 2)
    ms = _as_int(params.get("ms", 6000), "ms", 100, 40000)
    return PowerboxButtonAction(poco, ms, source=ActionSource.UI)


def _build_set_relay(params: Dict[str, Any]) -> Action:
    from ..state.actions import SetRelayAction
    from ..state.app_state import RELAY_CH_RESTART
    ch = _as_int(_require(params, "channel"), "channel", 1, 8)
    if ch == RELAY_CH_RESTART:
        raise CommandError(
            "channel %d is the powerbox's own restart relay, not a load; "
            "use powerbox_hard_restart" % ch)
    on = _as_bool(_require(params, "on"), "on")
    return SetRelayAction(ch, on, source=ActionSource.UI)


def _build_roadsense_mode(params: Dict[str, Any]) -> Action:
    from ..state.actions import RoadsenseModeAction
    mode = str(_require(params, "mode")).strip().lower()
    if mode not in ("auto", "on", "off"):
        raise CommandError("'mode' must be one of auto, on, off")
    return RoadsenseModeAction(mode, source=ActionSource.UI)


def _build_roadsense_restart(params: Dict[str, Any]) -> Action:
    from ..state.actions import RoadsenseRestartAction
    return RoadsenseRestartAction(source=ActionSource.UI)


def _build_powerbox_hard_restart(params: Dict[str, Any]) -> Action:
    from ..state.actions import PowerboxHardRestartAction
    return PowerboxHardRestartAction(source=ActionSource.UI)


def _build_input_event(params: Dict[str, Any]) -> Action:
    from ..state.actions import InputEventAction
    device = str(_require(params, "device")).strip()
    event = str(_require(params, "event")).strip()
    if not device or len(device) > 64 or not event or len(event) > 64:
        raise CommandError("'device' and 'event' must be short non-empty strings")
    return InputEventAction(device, event, params.get("value"), source=ActionSource.UI)


def _build_set_fan(params: Dict[str, Any]) -> Action:
    from ..state.actions import SetFanOverrideAction
    pct = _as_float(_require(params, "pct"), "pct", 0.0, 100.0)
    return SetFanOverrideAction(pct, source=ActionSource.UI)


def _build_fan_auto(params: Dict[str, Any]) -> Action:
    from ..state.actions import SetFanOverrideAction
    return SetFanOverrideAction(None, source=ActionSource.UI)


def _build_powerbox_i2c_scan(params: Dict[str, Any]) -> Action:
    from ..state.actions import PowerboxI2cScanAction
    return PowerboxI2cScanAction(source=ActionSource.UI)


def _build_set_undervoltage(params: Dict[str, Any]) -> Action:
    from ..state.actions import SetUndervoltageConfigAction
    threshold = _as_float(_require(params, "threshold"), "threshold", 9.0, 12.5)
    recover = _as_float(_require(params, "recover"), "recover", 9.2, 13.0)
    if recover < threshold + 0.2:
        raise CommandError("'recover' must be at least 0.2 V above 'threshold' (hysteresis)")
    return SetUndervoltageConfigAction(threshold, recover, source=ActionSource.UI)


def _build_satellite_power_hold(params: Dict[str, Any]) -> Action:
    from ..state.actions import SatellitePowerHoldAction
    name = str(_require(params, "name")).strip()
    if not name or len(name) > 32:
        raise CommandError("parameter 'name' must be a short non-empty string")
    on = _as_bool(_require(params, "on"), "on")
    return SatellitePowerHoldAction(f"manual:{name}", acquire=on, source=ActionSource.UI)


def _build_satellite_send(params: Dict[str, Any]) -> Action:
    from ..state.actions import EnqueueSatelliteCommandAction
    device_id = _as_int(_require(params, "device_id"), "device_id", 100, 499)
    if 200 <= device_id <= 299:
        raise CommandError("parameter 'device_id' 200..299 is the powerbox, not a satellite")
    payload = _require(params, "payload")
    if not isinstance(payload, dict):
        raise CommandError("parameter 'payload' must be an object")
    priority = _as_int(params.get("priority", 50), "priority", 0, 100)
    return EnqueueSatelliteCommandAction(
        device_id, payload, priority=priority, source=ActionSource.UI,
    )


_SAT_TRANSPORTS = ("rs485", "ble", "wifi")
_SAT_RANGES = {"rs485": (100, 199), "ble": (300, 399), "wifi": (400, 499)}


def _opt_transport(params: Dict[str, Any]) -> Any:
    t = params.get("transport")
    if t in (None, "", "all"):
        return None
    t = str(t).strip().lower()
    if t not in _SAT_TRANSPORTS:
        raise CommandError("parameter 'transport' must be one of rs485, ble, wifi")
    return t


def _sat_id(params: Dict[str, Any]) -> int:
    sid = _as_int(_require(params, "sat_id"), "sat_id", 100, 499)
    if 200 <= sid <= 299:
        raise CommandError("parameter 'sat_id' 200..299 is the powerbox, not a satellite")
    return sid


def _registry_cmd(op: str, params: Dict[str, Any]) -> Action:
    from ..state.actions import SatelliteRegistryCommandAction
    return SatelliteRegistryCommandAction(op, params, source=ActionSource.UI)


def _build_satellite_discover(params: Dict[str, Any]) -> Action:
    return _registry_cmd("discover", {"transport": _opt_transport(params)})


def _build_satellite_pair_open(params: Dict[str, Any]) -> Action:
    seconds = _as_float(params.get("seconds", 120), "seconds", 0, 3600)
    return _registry_cmd("pair_open", {"seconds": seconds, "transport": _opt_transport(params)})


def _build_satellite_enrol(params: Dict[str, Any]) -> Action:
    transport = _opt_transport({"transport": _require(params, "transport")})
    if transport is None:
        raise CommandError("parameter 'transport' must be one of rs485, ble, wifi")
    name = str(_require(params, "name")).strip()
    if not name or len(name) > 32 or any(c.isspace() for c in name):
        raise CommandError("parameter 'name' must be a short slug without spaces")
    address = str(params.get("address") or "").strip()
    sat_id = params.get("sat_id")
    if sat_id is not None and sat_id != "":
        lo, hi = _SAT_RANGES[transport]
        sat_id = _as_int(sat_id, "sat_id", lo, hi)
    else:
        sat_id = None
    if transport == "rs485" and sat_id is None:
        if not address.isdigit():
            raise CommandError("RS485 enrolment needs 'sat_id' or a numeric 'address'")
        sat_id = _as_int(address, "address", 100, 199)
    if transport != "rs485" and not address:
        raise CommandError("parameter 'address' is required for ble/wifi")
    caps = params.get("caps") or []
    if isinstance(caps, str):
        caps = [c.strip() for c in caps.split(",") if c.strip()]
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
        raise CommandError("parameter 'caps' must be a list of strings")
    return _registry_cmd("enrol", {"transport": transport, "address": address or str(sat_id),
                                   "name": name, "sat_id": sat_id, "caps": caps})


def _build_satellite_forget(params: Dict[str, Any]) -> Action:
    return _registry_cmd("forget", {"sat_id": _sat_id(params)})


def _build_satellite_revoke(params: Dict[str, Any]) -> Action:
    return _registry_cmd("revoke", {"sat_id": _sat_id(params)})


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
    "powerbox_button": {
        "builder": _build_powerbox_button,
        "description": (
            "Press the POCO power button through the powerbox (poco 1 = the car "
            "computer; 2 is a legacy slot the firmware refuses, there is no second "
            "POCO). ms 6000 boots an off phone, 35000 force-resets a frozen one, "
            "300 is a plain key tap."
        ),
        "params": {"poco": "int 1..2 (default 1)", "ms": "int 100..40000 (default 6000)"},
    },
    "input_event": {
        "builder": _build_input_event,
        "description": (
            "Report a human-interface event (frontend touch/keys, satellite "
            "controls). Re-broadcast to all clients as an 'input' event; rules "
            "can react (e.g. force-feedback via satellite_send)."
        ),
        "params": {"device": "string", "event": "string", "value": "any (optional)"},
    },
    "set_relay": {
        "builder": _build_set_relay,
        "description": (
            "Set a relay-board load channel: ch4 = camera hub (dashcam; owned by "
            "the roadsense manager while the stack runs), ch6-8 spare. ch3 (CAN hub: "
            "gateway + panda) redirects to the gateway power path; ch2 (MFD "
            "Pi) is owned by the MFD manager and is refused while it runs; "
            "ch5 (RS485 rail) becomes a manual satellite hold; ch1 is the "
            "powerbox restart relay and is rejected (use powerbox_hard_restart). "
            "Actual state is mirrored in powerbox.relays from the STATUS telemetry."
        ),
        "params": {"channel": "int 2..8", "on": "bool"},
    },
    "powerbox_hard_restart": {
        "builder": _build_powerbox_hard_restart,
        "description": (
            "Hard-restart the powerbox through its own NC supply relay (ch1). "
            "The box is back ~5 s later; EVERY NO relay drops with it (MFD Pi "
            "hard cut, CAN hub with gateway + panda, camera hub) and comes back "
            "OFF until the backend re-asserts the wanted states on the boot banner."
        ),
        "params": {},
    },
    "roadsense_mode": {
        "builder": _build_roadsense_mode,
        "description": (
            "Operator mode of the roadsense perception stack running on this "
            "computer: auto = follow ACC (default), on = keep it running with the "
            "key off (bench), off = keep it stopped. State in `roadsense`."
        ),
        "params": {"mode": "auto | on | off"},
    },
    "roadsense_restart": {
        "builder": _build_roadsense_restart,
        "description": "Restart roadsense.target (only while the stack runs).",
        "params": {},
    },
    "set_fan": {
        "builder": _build_set_fan,
        "description": "Manually pin the chassis fan speed in % (overrides automatic control).",
        "params": {"pct": "float 0..100"},
    },
    "fan_auto": {
        "builder": _build_fan_auto,
        "description": "Clear the chassis fan override and return to automatic control.",
        "params": {},
    },
    "powerbox_i2c_scan": {
        "builder": _build_powerbox_i2c_scan,
        "description": (
            "Diagnostic: ask the powerbox firmware (>=1.9.1) for a live I2C "
            "bus rescan. The ACKing addresses are logged in the backend "
            "journal as 'Powerbox I2C_SCAN'. Device binding still happens at "
            "firmware boot — after fixing wiring, cold-boot the powerbox."
        ),
        "params": {},
    },
    "set_undervoltage": {
        "builder": _build_set_undervoltage,
        "description": (
            "Set the 12 V under-voltage protection thresholds. Applies "
            "immediately and persists across restarts (user_settings.json). "
            "The firmware keeps its own last-resort backstop at 10.0 V."
        ),
        "params": {
            "threshold": "float 9.0..12.5 (V, trip after sustained 5 s below)",
            "recover": "float 9.2..13.0 (V, must be >= threshold + 0.2)",
        },
    },
    "satellite_power_hold": {
        "builder": _build_satellite_power_hold,
        "description": (
            "Acquire/release a named manual wake-lock on the RS485 satellite rail (relay ch5). "
            "The rail stays powered while any holder (acc/queue/manual) is held."
        ),
        "params": {"name": "string (holder name)", "on": "bool"},
    },
    "satellite_send": {
        "builder": _build_satellite_send,
        "description": (
            "Enqueue a command to a satellite on any transport (RS485 100..199, "
            "BLE 300..399, Wi-Fi 400..499). RS485: powers the satellite rail if "
            "needed, waits for the rail, sends the payload, and powers back "
            "down after the linger window. BLE/Wi-Fi: waits for an authenticated "
            "link. Jobs are serialized per transport, FIFO within priority — "
            "lower runs first."
        ),
        "params": {
            "device_id": "int 100..199 | 300..499",
            "payload": "object (raw NDJSON 'd' payload for the satellite)",
            "priority": "int 0..100 (optional, default 50, lower = sooner)",
        },
    },
    "satellite_discover": {
        "builder": _build_satellite_discover,
        "description": (
            "Force a satellite rediscovery pass now (RS485: status poll through the "
            "rail queue, powers the rail; BLE: short scan; Wi-Fi: car announcement)."
        ),
        "params": {"transport": "rs485 | ble | wifi (optional, default all)"},
    },
    "satellite_pair_open": {
        "builder": _build_satellite_pair_open,
        "description": (
            "Open (or close with seconds=0) the satellite pairing window. While open, "
            "unknown BLE/Wi-Fi devices that prove their key are held in "
            "satellites.discovered (with pubkey) for satellite_enrol."
        ),
        "params": {"seconds": "float 0..3600 (default 120, 0 closes)",
                   "transport": "rs485 | ble | wifi (optional, default all)"},
    },
    "satellite_enrol": {
        "builder": _build_satellite_enrol,
        "description": (
            "Enrol a satellite into the keychain. BLE/Wi-Fi: a discovered device that "
            "offered a pubkey, pairing window open. RS485: name + id only, no window "
            "needed (wired). Result in satellites.last_command."
        ),
        "params": {"transport": "rs485 | ble | wifi", "address": "string (as in discovered; RS485: the id)",
                   "name": "slug", "sat_id": "int (optional; free id in the range otherwise)",
                   "caps": "list of strings (optional)"},
    },
    "satellite_forget": {
        "builder": _build_satellite_forget,
        "description": "Remove a satellite from the keychain and drop its link "
                       "(a valid certificate can bring it back; use revoke to ban).",
        "params": {"sat_id": "int"},
    },
    "satellite_revoke": {
        "builder": _build_satellite_revoke,
        "description": "Revoke a satellite's key fingerprint (refused forever, cert or not) "
                       "and drop its link. Error 'nothing to revoke' for auth-none (RS485) nodes.",
        "params": {"sat_id": "int"},
    },
}


# ── AVC-LAN climate buttons (verified frames, AVC_MAP.md climate table) ─────

def _climate_button_names() -> List[str]:
    from ..comm.avc.decoder import CLIMATE_BUTTONS
    return list(CLIMATE_BUTTONS.keys())


def _build_climate_button(params: Dict[str, Any]) -> Action:
    from ..state.actions import ClimateButtonAction
    button = str(_require(params, "button")).strip().lower()
    names = _climate_button_names()
    if button not in names:
        raise CommandError(f"unknown climate button '{button}'; one of: {', '.join(names)}")
    count = _as_int(params.get("count", 1), "count", 1, 10)
    return ClimateButtonAction(button, count, source=ActionSource.UI)


def _climate_alias(button: str) -> CommandBuilder:
    def build(params: Dict[str, Any]) -> Action:
        return _build_climate_button({"button": button, "count": params.get("count", 1)})
    return build


COMMANDS["climate_button"] = {
    "builder": _build_climate_button,
    "description": (
        "Press a climate touch button on the AVC-LAN (110->1C6 00 25 E0 84 ..., "
        "release ~100 ms later). Presses are queued and never overlap; one press "
        "= one step (temp_up/temp_down = 0.5 C). Toggles (ac, recirc, "
        "rear_defogger, front_defrost) flip the current state."
    ),
    "params": {
        "button": "one of: " + ", ".join(_climate_button_names()),
        "count": "int 1..10 (optional, default 1)",
    },
}
for _name in _climate_button_names():
    _alias = "climate_system_off" if _name == "off" else f"climate_{_name}"
    COMMANDS[_alias] = {
        "builder": _climate_alias(_name),
        "description": f"Press the climate '{_name}' button (alias of climate_button).",
        "params": {"count": "int 1..10 (optional, default 1)"},
    }


def _build_gateway_can_tx(params: Dict[str, Any]) -> Action:
    from ..state.actions import GatewayCanTxAction
    raw_id = _require(params, "id")
    try:
        can_id = int(raw_id, 16) if isinstance(raw_id, str) else int(raw_id)
    except (TypeError, ValueError):
        raise CommandError("id must be an int or hex string")
    data = params.get("data") or []
    if not isinstance(data, list) or len(data) > 8:
        raise CommandError("data must be a list of up to 8 bytes")
    try:
        payload = bytes(int(b, 16) if isinstance(b, str) else int(b) for b in data)
    except (TypeError, ValueError):
        raise CommandError("data bytes must be ints or hex strings")
    if any(b < 0 or b > 255 for b in payload):
        raise CommandError("data bytes must be 0..255")
    return GatewayCanTxAction(can_id, payload, bench=bool(params.get("bench", True)))


def _build_panda_bench(params: Dict[str, Any]) -> Action:
    from ..state.actions import PandaBenchAction
    return PandaBenchAction(reset=bool(params.get("reset", True)))


COMMANDS["gateway_can_tx"] = {
    "builder": _build_gateway_can_tx,
    "description": "BENCH ONLY: transmit one raw CAN frame through the gateway "
                   "(isolated segment, never on the car bus). The panda's receipt "
                   "of the same frame is timed into connection.panda_bench.",
    "params": {"id": "int or hex string", "data": "list of 0..8 bytes", "bench": "bool (default true)"},
}
COMMANDS["panda_bench"] = {
    "builder": _build_panda_bench,
    "description": "Reset the gateway->panda bench latency counters.",
    "params": {"reset": "bool (default true)"},
}


def _build_dtc(params: Dict[str, Any]) -> Action:
    from ..state.actions import DtcCommandAction
    op = str(params.get("op", "scan"))
    if op not in DtcCommandAction.OPS:
        raise CommandError("op must be one of %s" % ", ".join(DtcCommandAction.OPS))
    return DtcCommandAction(op, source=ActionSource.UI)


COMMANDS["dtc"] = {
    "builder": _build_dtc,
    "description": "DTC scan (Mode 03), pending scan (Mode 07) or clear (Mode 04 + rescan) "
                   "across the engine, hybrid, HV battery and airbag ECUs through the gateway.",
    "params": {"op": "scan | scan_pending | clear (default scan)"},
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
