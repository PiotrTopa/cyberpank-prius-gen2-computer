"""
BEAN ingest: decoded body-bus frames -> vehicle / climate / dynamics actions.

The powerbox parser (io.powerbox.parse_bean_message) turns a device 203 line
into a :class:`SetBeanRxAction` (frame table + ``bean.decoded``). This module
takes the same frames and writes the CONFIRMED signals into the vehicle
state (docs/BEAN_SIGNALS.md §4, survey 2026-11-14):

* signals that also come from another bus go through the shared
  :class:`~cyberpunk_computer.comm.source_priority.SourceArbiter`
  (``rpm``, ``coolant``, ``outside_temp``, ``gear``, ``lights``,
  ``soc_bars``, ``climate``): a BEAN value is dispatched only when no better
  source is fresh, and BEAN being alive lets the solicited OBD rpm request be
  suspended;
* signals only BEAN has go straight into the state, on change only
  (:class:`SetPassiveSignalsAction`);
* PROBABLE decodes (``prov_*`` keys) go to ``bean.provisional`` only;
* the displayed speed (FE:24) and the meter's instantaneous consumption
  (52:90) are cleared to None when BEAN goes silent
  (:meth:`BeanIngest.expire`), never faked from our own values;
* engine-running evidence (accepted rpm, 98:27, 62:20 presence) goes to the
  :class:`~cyberpunk_computer.comm.engine_gate.EngineRunningGate`, and the
  62:20 per-injection word to the ingress fuel-flow computation;
* raw 62:C8 (cruise, no known bit meanings) is recorded on every change
  (:class:`CruiseRawLog`) so the next drive with cruise in use yields data.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from ..comm.bean import decode_known
from ..comm.can_decoder import headlight_state
from ..comm.source_priority import SourceArbiter
from ..state.actions import (
    Action, ActionSource, SetAvcClimateAction, SetGearAction, SetHeadlightStatusAction,
    SetICECoolantTempAction, SetOutsideTempAction, SetPassiveSignalsAction, SetRPMAction,
)
from ..state.app_state import GearPosition

logger = logging.getLogger(__name__)

SRC = "bean"

#: decode_known key -> VehicleState field (BEAN-only signals)
VEHICLE_FIELDS = {
    "acc_on": "acc_on",
    "ig_on": "ig_on",
    "brake_switch": "brake_switch",
    "ice_stopped": "ice_stopped",
    "trip_distance_km": "meter_trip_km",
    "consumption_l100km": "meter_consumption_l100km",
    "consumption_stopped": "meter_consumption_stopped",
    "avg_consumption_l100km": "meter_avg_consumption_l100km",
    "key_out_warning": "key_out_warning",
    "key_out_beep": "key_out_beep",
    "display_speed_kmh": "display_speed_kmh",
}
#: decode_known key -> ClimateState field
CLIMATE_FIELDS = {
    "compressor_target_rpm": "compressor_target_rpm",
    "compressor_rpm": "compressor_rpm",
    "compressor_running": "compressor_running",
    "ac_switch_on": "ac_switch_on",
}
#: decode_known ``prov_*`` key -> ProvisionalBeanSignals field
PROVISIONAL_FIELDS = {
    "prov_ac_engine_heat_request": "ac_engine_heat_request",
    "prov_cabin_temp_raw": "cabin_temp_raw",
    # 52:A2 is CONFIRMED (vehicle.meter_avg_consumption_l100km); the old
    # provisional field is still filled for existing readers.
    "avg_consumption_l100km": "avg_consumption_l100km",
    "prov_hv_ready": "hv_ready",
    "prov_p_locked": "p_locked",
    "prov_out_of_p": "out_of_p",
    "prov_p_ecu_on": "p_ecu_on",
    "prov_power_off_handshake": "power_off_handshake",
    "prov_hv_bus_voltage_v": "hv_bus_voltage_v",
}

GEAR_MAP = {"P": GearPosition.PARK, "R": GearPosition.REVERSE,
            "N": GearPosition.NEUTRAL, "D": GearPosition.DRIVE}

#: FE:24 period is 0.5 s; after this long without it the displayed speed is
#: unknown (None).
DISPLAY_SPEED_STALE_S = 3.0

#: 52:90 period is 0.5 s; after this long without it the meter's
#: instantaneous consumption is unknown (None) and ours is shown instead.
METER_CONSUMPTION_STALE_S = 3.0


class CruiseRawLog:
    """Raw cruise-control frames, recorded on change (PLACEHOLDER decode).

    Nothing was captured with cruise in use: BEAN 62:C8 stayed ``0000`` and
    CAN 0x5C8 (its gateway copy, ``24 00 00``) was constant. No bit meaning is
    assumed here; every change is logged at INFO (journal) and kept in
    ``vehicle.cruise_raw_changes`` (last :attr:`keep`)."""

    def __init__(self, keep: int = 32) -> None:
        self.keep = keep
        self.last: Dict[str, str] = {}
        self.changes: tuple = ()

    def observe(self, src: str, data_hex: str, ts: float) -> bool:
        """Returns True when ``data_hex`` differs from the last value of ``src``."""
        old = self.last.get(src)
        if old == data_hex:
            return False
        self.last[src] = data_hex
        if old is not None:
            logger.info("CRUISE RAW %s: %s -> %s", src, old, data_hex)
        self.changes = (self.changes + ({"ts": round(ts, 3), "src": src, "data": data_hex},))[-self.keep:]
        return True


class BeanIngest:
    def __init__(self, sources: SourceArbiter, cruise_log: Optional[CruiseRawLog] = None,
                 clock: Callable[[], float] = time.monotonic,
                 engine_gate: Optional[Any] = None,
                 fuel_word_sink: Optional[Callable[[int], List[Action]]] = None) -> None:
        self.sources = sources
        self.cruise = cruise_log or CruiseRawLog()
        self._clock = clock
        self.engine_gate = engine_gate
        self.fuel_word_sink = fuel_word_sink
        self._last: Dict[str, Any] = {}           # (group, field) -> value dispatched
        self._display_speed_seen: Optional[float] = None
        self._meter_consumption_seen: Optional[float] = None
        self.frames = 0

    def _changed(self, group: str, name: str, value: Any) -> bool:
        key = group + "." + name
        if key in self._last and self._last[key] == value:
            return False
        self._last[key] = value
        return True

    def process(self, frames: List[dict]) -> List[Action]:
        """CRC-good, not-our-own frames of one BEAN line -> actions."""
        now = self._clock()
        vehicle: Dict[str, Any] = {}
        climate: Dict[str, Any] = {}
        dynamics: Dict[str, Any] = {}
        prov: Dict[str, Any] = {}
        out: List[Action] = []
        for f in frames:
            if not f.get("ok") or f.get("own"):
                continue
            self.frames += 1
            dst, mes, data = f.get("dst", ""), f.get("mes", ""), f.get("data", "")
            v = decode_known(mes, data, dst)
            if not v:
                continue
            for key, field in VEHICLE_FIELDS.items():
                if key in v and self._changed("vehicle", field, v[key]):
                    vehicle[field] = v[key]
            if "display_speed_kmh" in v:
                self._display_speed_seen = now
            if "consumption_stopped" in v:
                self._meter_consumption_seen = now
            gate = self.engine_gate
            if gate is not None and mes.upper() == "27" and "ice_stopped" in v:
                gate.observe_bean_9827(int(data[:2], 16), now)
            if "injection_word" in v:
                if gate is not None:
                    gate.observe_bean_6220(now)
                if self.fuel_word_sink is not None:
                    out.extend(self.fuel_word_sink(v["injection_word"]))
            for key, field in CLIMATE_FIELDS.items():
                if key in v and self._changed("climate", field, v[key]):
                    climate[field] = v[key]
            for key, field in PROVISIONAL_FIELDS.items():
                if key in v and self._changed("provisional", field, v[key]):
                    prov[field] = v[key]

            # ── arbitrated signals (always dispatched when BEAN is the
            #    active source, so a hand-over from a stale source lands) ──
            if "engine_rpm" in v and self.sources.offer("rpm", SRC, now):
                out.append(SetRPMAction(int(v["engine_rpm"]), ActionSource.GATEWAY))
                if gate is not None:
                    gate.observe_rpm(v["engine_rpm"], now)
            if "coolant_c" in v and self.sources.offer("coolant", SRC, now):
                # the 16 °C floor (raw 0x20) is not offered: not a measurement
                out.append(SetICECoolantTempAction(v["coolant_c"], ActionSource.GATEWAY))
            if "outside_temp_c" in v and self.sources.offer("outside_temp", SRC, now):
                out.append(SetOutsideTempAction(float(v["outside_temp_c"]), ActionSource.GATEWAY))
            if v.get("shift") in GEAR_MAP and self.sources.offer("gear", SRC, now):
                out.append(SetGearAction(GEAR_MAP[v["shift"]], ActionSource.GATEWAY))
            if "lamp_tail" in v and "door_open" in v and self.sources.offer("lights", SRC, now):
                raw = int(data[:2], 16)
                out.append(SetHeadlightStatusAction(
                    headlight_state=headlight_state(raw),
                    parking_lights=v["lamp_tail"], low_beam=v["lamp_head"],
                    high_beam=v["lamp_high_beam"], fog_front=v["lamp_fog_front"],
                    door_open=v["door_open"], source=ActionSource.GATEWAY))
            if "soc_bars" in v and self.sources.offer("soc_bars", SRC, now):
                dynamics["soc_bars"] = v["soc_bars"]
            if "climate_fan" in v and self.sources.offer("climate", SRC, now):
                out.append(SetAvcClimateAction(
                    fan_speed=v["climate_fan"], ac_on=v["climate_ac"],
                    auto_mode=v["climate_auto"], recirculation=v["climate_recirc"],
                    fresh_air=v["climate_fresh_air"], defrost=v["climate_front_defrost"],
                    rear_defogger=v["climate_rear_defogger"], air_direction=v["climate_vent"],
                    system_on=v["climate_on"], status_source=SRC))
            if "climate_target_raw" in v and self.sources.offer("climate", SRC, now):
                t = v["climate_target_c"]
                out.append(SetAvcClimateAction(system_on=False, status_source=SRC) if t is None
                           else SetAvcClimateAction(target_temp=t, status_source=SRC))
            if "cruise_raw" in v and self.cruise.observe("bean:62C8", v["cruise_raw"], f.get("ts", 0.0)):
                vehicle["cruise_raw_62c8"] = v["cruise_raw"]
                vehicle["cruise_raw_changes"] = self.cruise.changes
        if vehicle or climate or dynamics or prov:
            out.insert(0, SetPassiveSignalsAction(vehicle=vehicle, climate=climate,
                                                  dynamics=dynamics, provisional=prov))
        return out

    def expire(self) -> List[Action]:
        """Displayed speed -> None once FE:24 has been silent for
        :data:`DISPLAY_SPEED_STALE_S`, the meter's instantaneous consumption
        -> None once 52:90 has been silent for
        :data:`METER_CONSUMPTION_STALE_S` (BEAN off or receiver stopped)."""
        now = self._clock()
        vehicle: Dict[str, Any] = {}
        if self._display_speed_seen is not None \
                and now - self._display_speed_seen > DISPLAY_SPEED_STALE_S:
            self._display_speed_seen = None
            self._last.pop("vehicle.display_speed_kmh", None)
            vehicle["display_speed_kmh"] = None
        if self._meter_consumption_seen is not None \
                and now - self._meter_consumption_seen > METER_CONSUMPTION_STALE_S:
            self._meter_consumption_seen = None
            for f in ("meter_consumption_l100km", "meter_consumption_stopped"):
                self._last.pop("vehicle." + f, None)
                vehicle[f] = None
        return [SetPassiveSignalsAction(vehicle=vehicle)] if vehicle else []
