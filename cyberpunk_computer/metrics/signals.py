"""
Signal catalog and event detection for the metrics sink.

A :class:`Signal` maps a stable name + unit to a getter that extracts a single
numeric value from the immutable :class:`AppState`. The sink samples every
enabled signal at a fixed rate and stores non-``None`` values.

Event detection compares the previous and current ``AppState`` and emits
discrete events (ignition, drivetrain, charging, gear, connectivity). Powerbox
power-source events will be added here once that device's protocol exists.

Everything reads from a frozen ``AppState`` snapshot, so this module is pure and
thread-safe (no Store mutation).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from ..state.app_state import AppState

# A getter returns the current numeric value of a signal, or None if unknown.
Getter = Callable[[AppState], Optional[float]]


@dataclass(frozen=True)
class Signal:
    """One recordable time-series: stable ``name``, display ``unit``, getter."""
    name: str
    unit: str
    get: Getter
    description: str = ""


def _delta_v(state: AppState) -> Optional[float]:
    """Max-min HV block voltage spread (battery health indicator)."""
    bv = state.energy.block_voltages
    if bv and len(bv) >= 2:
        return max(bv) - min(bv)
    return None


# ── Signal catalog ────────────────────────────────────────────────────────
# Ordering is informational only. Names are the stable API/storage keys.
SIGNALS: List[Signal] = [
    # Energy / HV battery
    Signal("battery_soc", "%", lambda s: s.energy.battery_soc * 100.0, "HV battery state of charge"),
    Signal("battery_temp", "°C", lambda s: s.energy.battery_temp, "HV battery pack temperature"),
    Signal("hv_voltage", "V", lambda s: s.energy.hv_battery_voltage, "HV battery voltage"),
    Signal("hv_current", "A", lambda s: s.energy.hv_battery_current, "HV battery current"),
    Signal("hv_power", "kW", lambda s: s.energy.battery_power_kw, "HV battery power (V*I)"),
    Signal("motor_power", "kW", lambda s: s.energy.motor_power_kw, "MG2 traction motor power"),
    Signal("generator_power", "kW", lambda s: s.energy.generator_power_kw, "MG1 generator power"),
    Signal("ice_power", "kW", lambda s: s.energy.ice_power_kw, "Engine power"),
    Signal("delta_v", "V", _delta_v, "HV block voltage spread"),
    # Vehicle / drivetrain
    Signal("speed", "km/h", lambda s: s.vehicle.speed_kmh, "Vehicle speed"),
    Signal("rpm", "rpm", lambda s: s.vehicle.rpm, "Engine RPM"),
    Signal("ice_coolant_temp", "°C", lambda s: s.vehicle.ice_coolant_temp, "Engine coolant temperature"),
    Signal("inverter_temp", "°C", lambda s: s.vehicle.inverter_temp, "Inverter temperature"),
    Signal("mg1_motor_temp", "°C", lambda s: s.vehicle.mg1_motor_temp, "MG1 motor temperature"),
    Signal("mg2_motor_temp", "°C", lambda s: s.vehicle.mg2_motor_temp, "MG2 motor temperature"),
    Signal("converter_temp", "°C", lambda s: s.vehicle.converter_temp, "DC-DC converter temperature"),
    Signal("aux_voltage", "V", lambda s: s.vehicle.aux_battery_voltage, "12V aux battery voltage"),
    # Fuel
    Signal("fuel_level", "L", lambda s: float(s.vehicle.fuel_level), "Petrol level"),
    Signal("lpg_level", "L", lambda s: float(s.vehicle.lpg_level), "LPG level"),
    Signal("fuel_flow", "L/h", lambda s: s.vehicle.fuel_flow_rate, "Instantaneous fuel flow"),
    Signal("trip_fuel", "L", lambda s: s.vehicle.trip_fuel_consumed, "Cumulative trip fuel"),
    # Environment / climate
    Signal("ambient_temp", "°C", lambda s: s.vehicle.ambient_air_temp, "Ambient air temperature"),
    Signal("intake_air_temp", "°C", lambda s: s.vehicle.intake_air_temp, "Intake air temperature"),
    Signal("cabin_temp", "°C", lambda s: s.climate.inside_temp, "Cabin inside temperature"),
    Signal("outside_temp", "°C", lambda s: s.climate.outside_temp, "Outside temperature (climate)"),
    # Powerbox / POCO computer supply (INA219 on the Prius 12 V feed)
    Signal("powerbox_voltage", "V", lambda s: s.powerbox.system_voltage, "Computer 12V supply voltage (INA219)"),
    Signal("powerbox_current", "A", lambda s: s.powerbox.current_draw_a, "Computer current draw (INA219)"),
    Signal("powerbox_power", "W", lambda s: s.powerbox.power_draw_w, "Computer power consumption (INA219)"),
]

# Quick lookup by name.
SIGNALS_BY_NAME = {sig.name: sig for sig in SIGNALS}


def sample_all(state: AppState, ts: float) -> List[Tuple[str, float, float]]:
    """Return ``(name, ts, value)`` tuples for every signal with a known value."""
    out: List[Tuple[str, float, float]] = []
    for sig in SIGNALS:
        try:
            value = sig.get(state)
        except Exception:
            value = None
        if value is not None:
            out.append((sig.name, ts, float(value)))
    return out


# ── Event detection ────────────────────────────────────────────────────────

def _onoff(flag: bool) -> str:
    return "on" if flag else "off"


def detect_events(
    prev: Optional[AppState],
    cur: AppState,
    ts: float,
) -> List[Tuple[float, str, str]]:
    """Return ``(ts, type, detail)`` events for transitions between snapshots.

    On the first call (``prev is None``) no events are emitted; the next call
    establishes the baseline.
    """
    if prev is None:
        return []
    events: List[Tuple[float, str, str]] = []

    v_prev, v_cur = prev.vehicle, cur.vehicle
    e_prev, e_cur = prev.energy, cur.energy
    c_prev, c_cur = prev.connection, cur.connection

    if v_prev.ig_on != v_cur.ig_on:
        events.append((ts, "ignition", _onoff(v_cur.ig_on)))
    if v_prev.acc_on != v_cur.acc_on:
        events.append((ts, "accessory", _onoff(v_cur.acc_on)))
    if v_prev.ready_mode != v_cur.ready_mode:
        events.append((ts, "ready", _onoff(v_cur.ready_mode)))
    if v_prev.ice_running != v_cur.ice_running:
        events.append((ts, "ice", "start" if v_cur.ice_running else "stop"))
    if v_prev.gear != v_cur.gear:
        events.append((ts, "gear", v_cur.gear.name))
    if e_prev.charging != e_cur.charging:
        events.append((ts, "battery", "charge_start" if e_cur.charging else "charge_stop"))
    if c_prev.connected != c_cur.connected:
        events.append((ts, "gateway", "connected" if c_cur.connected else "disconnected"))

    return events
