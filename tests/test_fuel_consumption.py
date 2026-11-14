"""Fuel consumption: flow = K x 0x520 word x rpm, the engine-running gate,
the meter's own 52:90 / 52:A2 as primary figures with our corrected values
as fallback, per-drive totals.

Frames are real: tests/fixtures/fuel_drive_2026-11-13.csv.gz is the
2026-11-13 IG-ON drive (21:47-22:07 CET, 2.44 km true distance, 18.5 min),
CAN from the phone trip recording and BEAN from
docs/data/bean/2026-11-13_drive_ig_on.ndjson.gz aligned onto the CAN clock.
On that drive the old code (0.008 L/h per count, gated on 0x038 b1) gave
0.90 L; the meter-model total (K x word x rpm) is 0.55 L."""

import gzip
import statistics
from pathlib import Path

import pytest

from cyberpunk_computer.comm.engine_gate import EngineRunningGate
from cyberpunk_computer.comm.source_priority import default_arbiter
from cyberpunk_computer.io.ingress import IngressController
from cyberpunk_computer.io.mock_io import MockInputPort
from cyberpunk_computer.io.ports import RawMessage
from cyberpunk_computer.io.powerbox import register_powerbox_ingress
from cyberpunk_computer.state import fuel_model
from cyberpunk_computer.state.rules import RulesEngine
from cyberpunk_computer.state.rules.active_fuel import ActiveFuelRule
from cyberpunk_computer.state.rules.fuel_consumption import FuelConsumptionRule
from cyberpunk_computer.state.rules.trip_fuel import TripFuelConsumptionRule
from cyberpunk_computer.state.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "fuel_drive_2026-11-13.csv.gz"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_twin():
    store = Store()
    ing = IngressController(store, MockInputPort())
    register_powerbox_ingress(ing)
    clk = Clock()
    ing._sources._clock = clk
    ing._bean._clock = clk
    rules = RulesEngine(store)
    trip = TripFuelConsumptionRule(clock=clk)
    for r in (FuelConsumptionRule(), trip, ActiveFuelRule()):
        rules.register(r)
    return store, ing, clk, trip


def feed_can(ing, can_id, data):
    ing._process_message(RawMessage(device_id=1, timestamp=0,
                                    data={"i": "0x%03X" % can_id, "d": list(data)}))


def feed_bean(ing, frames):
    recs = [[1000 + i, 6, d, m, x, 1, 0] for i, (d, m, x) in enumerate(frames)]
    ing._process_message(RawMessage(device_id=203, timestamp=0,
                                    data={"t": 1000 + len(recs), "bf": recs}))


def fe26(rpm):
    """BEAN FE:26 data for an engine rpm (word / 5.12)."""
    return "%04X" % int(round(rpm * 5.12))


def fixture_events():
    with gzip.open(FIXTURE, "rt") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            t, bus, ident, data = line.strip().split(",")
            yield float(t), bus, ident, data


# ─────────────────────────────────────────────────────────────────────────────
# Formula
# ─────────────────────────────────────────────────────────────────────────────

class TestFlowFormula:
    def test_flow_scales_with_rpm(self):
        # 0x520 A4 02 3B (word 571) at 1300 rpm, 2026-11-13 21:50:00
        assert fuel_model.fuel_flow_lh(571, 1300) == pytest.approx(2.69e-6 * 571 * 1300)
        assert fuel_model.fuel_flow_lh(571, 1300) == pytest.approx(2.00, abs=0.01)
        assert fuel_model.fuel_flow_lh(571, 2600) == pytest.approx(2 * fuel_model.fuel_flow_lh(571, 1300))
        assert fuel_model.fuel_flow_lh(571, 0) == 0.0
        assert fuel_model.fuel_flow_lh(571, None) == 0.0
        assert fuel_model.fuel_flow_lh(0, 1300) == 0.0
        assert fuel_model.fuel_flow_lh(571, 1300, k=2.82e-6) == pytest.approx(2.82e-6 * 571 * 1300)
        assert fuel_model.fuel_flow_lh(60000, 6000) == fuel_model.MAX_FLOW_LH

    def test_constants_are_honest(self):
        assert fuel_model.FUEL_FLOW_K_DEFAULT == 2.69e-6
        assert fuel_model.FUEL_BASIS == "petrol_equivalent"
        assert fuel_model.LPG_VOLUME_FACTOR is None          # not applied

    def test_ingress_k_is_configurable(self):
        store, ing, clk, _ = make_twin()
        ing.fuel_flow_k = 2.82e-6
        feed_bean(ing, [("FE", "26", fe26(1300))])
        feed_can(ing, 0x520, [0xA4, 0x02, 0x3B])
        assert store.state.vehicle.fuel_flow_rate == pytest.approx(2.82e-6 * 571 * 1300, abs=1e-3)


# ─────────────────────────────────────────────────────────────────────────────
# Engine-running gate
# ─────────────────────────────────────────────────────────────────────────────

class TestEngineGate:
    def test_1300_rpm_low_load_runs_although_038_b1_is_zero(self):
        """21:50:00.5: OBD/FE:26 1289 rpm, 0x520 A4 02 3B, 0x038 b1 = 0."""
        store, ing, clk, _ = make_twin()
        feed_can(ing, 0x038, [192, 0, 8, 13, 0, 0, 20])          # b1 = 0
        feed_bean(ing, [("FE", "26", fe26(1289))])
        feed_can(ing, 0x038, [192, 0, 8, 12, 0, 0, 19])
        feed_can(ing, 0x520, [0xA4, 0x02, 0x3B])
        v = store.state.vehicle
        assert v.ice_running and v.engine_running_source == "rpm"
        assert v.fuel_flow_rate == pytest.approx(2.69e-6 * 571 * 1289, abs=1e-3)   # 1.98 L/h
        assert v.active_fuel.name == "PETROL" and not v.active_fuel_detected

    def test_rpm_zero_stops_and_flow_ages_out(self):
        store, ing, clk, _ = make_twin()
        feed_bean(ing, [("FE", "26", fe26(1300))])
        feed_can(ing, 0x520, [0xA4, 0x02, 0x3B])
        assert store.state.vehicle.fuel_flow_rate > 1.9
        clk.t += 0.5
        feed_bean(ing, [("FE", "26", "0000")])                   # engine stopped
        ing.update()
        v = store.state.vehicle
        assert not v.ice_running and v.fuel_flow_rate == 0.0

    def test_fallback_chain_when_bean_silent(self):
        store, ing, clk, _ = make_twin()
        feed_bean(ing, [("98", "27", "01FF")])                   # READY, ICE turning
        v = store.state.vehicle
        assert v.ice_running and v.engine_running_source == "bean"
        feed_bean(ing, [("98", "27", "09FF")])                   # ICE stopped
        assert not store.state.vehicle.ice_running
        clk.t += 3.5                                             # BEAN silent
        feed_can(ing, 0x038, [192, 0, 8, 13, 0, 0, 20])
        feed_can(ing, 0x520, [0xA4, 0x02, 0x3B])                 # 0x520 present
        v = store.state.vehicle
        assert v.ice_running and v.engine_running_source == "can"
        assert v.fuel_flow_rate == 0.0                           # no fresh rpm: no flow
        obd = int(1300 * 4)
        feed_can(ing, 0x7E8, [4, 0x41, 0x0C, obd >> 8, obd & 0xFF, 0, 0, 0])
        assert store.state.vehicle.engine_running_source == "rpm"
        feed_can(ing, 0x520, [0xA4, 0x02, 0x3B])
        assert store.state.vehicle.fuel_flow_rate == pytest.approx(2.0, abs=0.01)
        clk.t += 2.5                                             # OBD and 0x520 silent
        feed_can(ing, 0x038, [192, 0, 8, 13, 0, 0, 20])          # bus alive, b1 = 0
        ing.update()
        v = store.state.vehicle
        assert not v.ice_running and v.engine_running_source == "can"
        assert v.fuel_flow_rate == 0.0

    def test_6220_presence_and_flow_from_bean_when_can_silent(self):
        store, ing, clk, _ = make_twin()
        feed_bean(ing, [("FE", "26", fe26(1300)), ("62", "20", "023B")])
        v = store.state.vehicle
        assert v.ice_running
        assert v.fuel_flow_rate == pytest.approx(2.69e-6 * 571 * 1300, abs=1e-3)

    def test_gate_without_any_source_has_no_opinion(self):
        gate = EngineRunningGate(default_arbiter(clock=Clock()))
        assert gate.decide() == (None, None)


# ─────────────────────────────────────────────────────────────────────────────
# Instantaneous and average figures
# ─────────────────────────────────────────────────────────────────────────────

class TestDisplayedConsumption:
    def drive(self, store, ing, clk, kmh=30.0, rpm=1300):
        raw = int(kmh * 100)
        feed_can(ing, 0x0B4, [0, 0, 0, 0, 1, raw >> 8, raw & 0xFF, 0])
        feed_bean(ing, [("FE", "26", fe26(rpm))])
        feed_can(ing, 0x520, [0xA4, 0x02, 0x3B])

    def test_meter_52_90_is_primary(self):
        store, ing, clk, _ = make_twin()
        self.drive(store, ing, clk)
        feed_bean(ing, [("52", "90", "007D40")])                 # 12.5 L/100 km
        v = store.state.vehicle
        assert (v.instant_consumption, v.consumption_unit, v.instant_consumption_source) == \
            (12.5, "L/100km", "meter")
        # ours, alongside: 2.00 L/h at 30 km/h = 6.65 L/100 km
        assert v.computed_consumption_l100km == pytest.approx(2.69e-6 * 571 * 1300 / 30 * 100, abs=0.01)
        feed_bean(ing, [("52", "90", "07A940")])                 # 196.1: display cap
        v = store.state.vehicle
        assert v.instant_consumption == 99.9 and v.meter_consumption_l100km == 196.1

    def test_fffe_shows_our_l_per_h(self):
        store, ing, clk, _ = make_twin()
        self.drive(store, ing, clk, kmh=0.0)
        feed_bean(ing, [("52", "90", "FFFE40")])                 # 21:48:21.8
        v = store.state.vehicle
        assert v.meter_consumption_stopped is True and v.meter_consumption_l100km is None
        assert v.consumption_unit == "L/h" and v.instant_consumption_source == "computed"
        assert v.instant_consumption == pytest.approx(2.0, abs=0.01)

    def test_0000_is_zero(self):
        store, ing, clk, _ = make_twin()
        self.drive(store, ing, clk, kmh=30.0)
        feed_bean(ing, [("52", "90", "000040")])                 # no fuel (EV / fuel cut)
        v = store.state.vehicle
        assert (v.instant_consumption, v.consumption_unit, v.instant_consumption_source) == \
            (0.0, "L/100km", "meter")

    def test_bean_stale_falls_back_to_computed(self):
        store, ing, clk, _ = make_twin()
        self.drive(store, ing, clk, kmh=30.0)
        feed_bean(ing, [("52", "90", "007D40")])
        assert store.state.vehicle.instant_consumption_source == "meter"
        clk.t += 3.5                                             # 52:90 silent
        ing.update()
        v = store.state.vehicle
        assert v.meter_consumption_l100km is None and v.meter_consumption_stopped is None
        assert v.instant_consumption_source == "computed"
        # rpm is stale too now, so no flow: ours reads 0 at speed
        assert v.instant_consumption == 0.0
        obd = int(1300 * 4)
        feed_can(ing, 0x7E8, [4, 0x41, 0x0C, obd >> 8, obd & 0xFF, 0, 0, 0])
        feed_can(ing, 0x520, [0xA4, 0x02, 0x3B])
        v = store.state.vehicle
        assert v.instant_consumption == pytest.approx(6.65, abs=0.01)
        assert v.consumption_unit == "L/100km"

    def test_meter_average_confirmed_and_cross_check(self):
        store, ing, clk, _ = make_twin()
        feed_bean(ing, [("52", "A2", "00BC40"), ("52", "76", "003180")])   # 18.8, 4.9 km
        v = store.state.vehicle
        assert v.meter_avg_consumption_l100km == 18.8
        assert store.state.bean.provisional.avg_consumption_l100km == 18.8   # kept for readers
        assert v.meter_trip_fuel_l == pytest.approx(18.8 * 4.9 / 100, abs=1e-3)
        assert v.fuel_basis == "petrol_equivalent"


class TestDriveTotals:
    def test_integrates_standstill_and_resets_at_ig_on(self):
        store, ing, clk, trip = make_twin()
        feed_bean(ing, [("FE", "83", "60000008")])               # IG-ON
        feed_bean(ing, [("FE", "26", fe26(1300))])
        feed_can(ing, 0x520, [0xA4, 0x02, 0x3B])                 # 2.0 L/h, standing
        for i in range(36):                                      # 36 s idling
            clk.t += 0.5
            feed_bean(ing, [("FE", "26", fe26(1300 + i % 2))])  # FE:26 jitters
            clk.t += 0.5
            feed_can(ing, 0x520, [0xA4, 0x02, 0x3B])
        v = store.state.vehicle
        assert v.trip_fuel_consumed == pytest.approx(2.0 * 36 / 3600, rel=0.05)    # 0.02 L
        assert v.drive_distance_km == 0.0 and v.drive_avg_consumption_l100km is None
        feed_bean(ing, [("FE", "83", "00000008")])               # IG off
        clk.t += 1.0
        feed_bean(ing, [("FE", "83", "60000008")])               # next drive
        assert store.state.vehicle.trip_fuel_consumed == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Replay of the recorded drive through ingress + rules
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def replay():
    store, ing, clk, trip = make_twin()
    t_base = 10_000.0
    pairs = []            # (computed L/100 km, meter 52:90) at each numeric 52:90
    fffe = zero = 0
    fffe_lh, zero_ok, fffe_ok = [], 0, 0
    low_load = [0, 0]     # [samples rpm 1200-1400 with 0x038 b1 = 0, of them ice_running]
    last_038_b1 = None
    for t, bus, ident, data in fixture_events():
        clk.t = t_base + t
        if bus == "C":
            b = bytes.fromhex(data)
            if ident == "038":
                last_038_b1 = b[1]
            feed_can(ing, int(ident, 16), b)
        else:
            dst, mes = ident.split(":")
            feed_bean(ing, [(dst, mes, data)])
            v = store.state.vehicle
            if ident == "52:90":
                if data.startswith("FFFE"):
                    fffe += 1
                    fffe_ok += v.consumption_unit == "L/h" and v.instant_consumption_source == "computed"
                    fffe_lh.append(v.instant_consumption)
                elif data.startswith("0000"):
                    zero += 1
                    zero_ok += v.instant_consumption == 0.0
                elif v.computed_consumption_l100km and v.real_speed_kmh > 5:
                    pairs.append((v.computed_consumption_l100km, v.meter_consumption_l100km))
            if ident == "FE:26" and last_038_b1 == 0 and v.rpm and 1200 <= v.rpm <= 1400:
                low_load[0] += 1
                low_load[1] += v.ice_running
        ing.update()
    return dict(state=store.state, pairs=pairs, fffe=fffe, fffe_ok=fffe_ok, fffe_lh=fffe_lh,
                zero=zero, zero_ok=zero_ok, low_load=low_load)


class TestRecordedDrive:
    def test_trip_total_matches_meter_model(self, replay):
        v = replay["state"].vehicle
        assert v.trip_fuel_consumed == pytest.approx(0.545, rel=0.05)     # old code: 0.90 L
        assert v.drive_distance_km == pytest.approx(2.44, abs=0.03)       # true distance

    def test_drive_average(self, replay):
        v = replay["state"].vehicle
        assert v.drive_avg_consumption_l100km == pytest.approx(
            v.trip_fuel_consumed / v.drive_distance_km * 100, abs=0.05)
        assert 20.0 < v.drive_avg_consumption_l100km < 25.0                # old code: 37

    def test_instantaneous_median_ratio_to_52_90(self, replay):
        ratios = [c / m for c, m in replay["pairs"] if m]
        assert len(ratios) > 400
        assert statistics.median(ratios) == pytest.approx(1.0, abs=0.08)   # old code: 2.1

    def test_fffe_and_0000(self, replay):
        assert replay["fffe"] > 900 and replay["fffe_ok"] == replay["fffe"]
        assert statistics.median(replay["fffe_lh"]) > 0.5                  # idling L/h shown
        assert replay["zero"] > 400 and replay["zero_ok"] == replay["zero"]

    def test_gate_at_low_load(self, replay):
        n, running = replay["low_load"]
        assert n > 50 and running == n

    def test_meter_cross_check(self, replay):
        v = replay["state"].vehicle
        assert v.meter_avg_consumption_l100km is not None and v.meter_trip_km is not None
        assert v.meter_trip_fuel_l == pytest.approx(
            v.meter_avg_consumption_l100km * v.meter_trip_km / 100, abs=1e-3)
