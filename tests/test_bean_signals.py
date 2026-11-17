"""BEAN signals of the 2026-11-14 survey -> vehicle state, the per-signal
source priority (passive before solicited, staleness fallback), the solicited
slot suspender, the BEAN frame-clock estimator, and the CAN 0x5CC / 0x57F
fixes.

Every frame below is a real frame from the 2026-11-13 IG-ON drive capture
(docs/data/bean/2026-11-13_drive_ig_on.ndjson.gz); the comment gives its
CET receive time."""

import random

import pytest

from cyberpunk_computer.comm.bean import BeanClock, decode_known, frames_from_batch
from cyberpunk_computer.comm.can_decoder import CANDecoder, CANMessageType
from cyberpunk_computer.comm.source_priority import (
    DEFAULT_POLICIES, SOLICITED, SignalPolicy, SlotSuspender, Source, SourceArbiter,
)
from cyberpunk_computer.io.bean_ingest import BeanIngest
from cyberpunk_computer.io.ingress import IngressController
from cyberpunk_computer.io.mock_io import MockInputPort
from cyberpunk_computer.io.ports import RawMessage
from cyberpunk_computer.io.powerbox import register_powerbox_ingress
from cyberpunk_computer.state.app_state import GearPosition
from cyberpunk_computer.state.store import Store


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def fr(dst, mes, data, ts=0.0, ok=True):
    return {"ts": ts, "t_ms": 0, "pri": 6, "dst": dst, "mes": mes, "data": data,
            "rsp": 1, "ok": ok, "err": None}


# ─────────────────────────────────────────────────────────────────────────────
# decode_known with real frames
# ─────────────────────────────────────────────────────────────────────────────

class TestConfirmedDecodes:
    def test_fe83_acc_ig(self):
        assert decode_known("83", "00000008", "FE")["acc_on"] is False    # 21:47:47.9
        v = decode_known("83", "20000008", "FE")                           # 21:48:04.2 ACC
        assert (v["acc_on"], v["ig_on"], v["dome_lamp"]) == (True, False, True)
        v = decode_known("83", "60000000", "FE")                           # 21:48:06.1 IG
        assert (v["acc_on"], v["ig_on"]) == (True, True)
        v = decode_known("83", "40000000", "FE")                           # 22:06:41.6 off: ACC first
        assert (v["acc_on"], v["ig_on"]) == (False, True)

    def test_fe29_brake_soc_bars_ready_provisional(self):
        v = decode_known("29", "00000B0000", "FE")                         # 21:48:04.8
        assert (v["brake_switch"], v["soc_bars"], v["prov_hv_ready"]) == (True, 3, False)
        v = decode_known("29", "00008C0000", "FE")                         # 21:52:00.5
        assert (v["brake_switch"], v["soc_bars"], v["prov_hv_ready"]) == (True, 4, True)
        v = decode_known("29", "0000830000", "FE")                         # 21:48:26.6
        assert (v["brake_switch"], v["soc_bars"]) == (False, 3)

    def test_9827_ice_stopped(self):
        assert decode_known("27", "09FF", "98")["ice_stopped"] is True    # 21:56:56.4
        v = decode_known("27", "01FF", "98")                               # 21:48:07.2
        assert v["ice_stopped"] is False and v["prov_hv_ready_9827"] is True
        assert decode_known("27", "09FF", "52") == {}                      # DST guard

    def test_52ed_compressor_request(self):
        v = decode_known("ED", "6AA05F0BD2", "52")                         # 21:48:58.5
        assert v["compressor_target_rpm"] == 3026
        assert (v["compressor_running"], v["ac_switch_on"]) == (True, True)
        assert v["prov_ac_engine_heat_request"] is True
        v = decode_known("ED", "2AA05F06A4", "52")                         # 21:53:45.8
        assert (v["compressor_target_rpm"], v["compressor_running"]) == (1700, False)
        v = decode_known("ED", "28807F0000", "52")                         # 21:48:06.1
        assert v["compressor_target_rpm"] == 0 and v["ac_switch_on"] is True

    def test_98ec_compressor_rpm(self):
        v = decode_known("EC", "08AF0E2C063D", "98")                       # 21:48:58.8
        assert v["compressor_rpm"] == 2223
        assert v["prov_hv_bus_voltage_v"] == pytest.approx(1.9 * 0x3D + 102)
        assert decode_known("EC", "000000000B45", "98")["compressor_rpm"] == 0

    def test_meter_trip_computer(self):
        assert decode_known("76", "003180", "52") == {"trip_distance_km": 4.9}   # 21:48:07.2
        assert decode_known("76", "003280", "52") == {"trip_distance_km": 5.0}
        assert decode_known("90", "000040", "52") == {"consumption_l100km": 0.0,
                                                      "consumption_stopped": False}
        assert decode_known("90", "FFFE40", "52") == {"consumption_l100km": None,  # 21:48:21.8
                                                      "consumption_stopped": True}
        assert decode_known("90", "036940", "52")["consumption_l100km"] == 87.3   # 21:48:31.8
        assert decode_known("A2", "00BC40", "52") == {"avg_consumption_l100km": 18.8}
        assert decode_known("20", "0894", "62") == {"injection_word": 0x894}   # = CAN 0x520 b1-2

    def test_engine_data_relayed_by_gateway(self):
        assert decode_known("24", "09", "FE") == {"display_speed_kmh": 9}       # 21:48:33.8
        assert decode_known("26", "11DC", "FE") == {"engine_rpm": 893}          # 21:48:19.6
        assert decode_known("26", "4230", "FE") == {"engine_rpm": 3309}         # drive max
        assert decode_known("2C", "20", "FE") == {"coolant_at_floor": True}     # cold: <= 16 C
        assert decode_known("2C", "21", "FE") == {"coolant_c": 16.5, "coolant_at_floor": False}
        assert decode_known("CD", "34", "FE") == {"outside_temp_c": 4}          # whole drive

    def test_climate(self):
        assert decode_known("FB", "800000000000", "FE") == {"climate_buttons": ("temp_up",)}
        assert decode_known("FB", "000080000000", "FE") == {"climate_buttons": ("fan_1",)}
        assert decode_known("FB", "000000000400", "FE") == {"climate_buttons": ("fan_6",)}
        assert decode_known("FB", "000000000000", "FE") == {}
        v = decode_known("FC", "C22000000000", "FE")                       # 21:48:05.4
        assert (v["climate_auto"], v["climate_fresh_air"], v["climate_ac"]) == (True, True, True)
        assert (v["climate_front_defrost"], v["climate_rear_defogger"]) == (False, False)
        assert v["climate_vent"] == "windscreen_foot" and v["climate_on"] is False
        v = decode_known("FC", "5A00000000A0", "FE")                       # 21:48:55.7 DEF
        assert (v["climate_auto"], v["climate_front_defrost"], v["climate_rear_defogger"]) == \
            (False, True, True)
        assert (v["climate_fan"], v["climate_on"]) == (5, True)
        assert decode_known("FD", "0938", "52")["climate_target_c"] == 20.0
        assert decode_known("FD", "0538", "52")["climate_target_c"] == 18.0
        assert decode_known("FD", "3938", "52")["climate_target_c"] is None  # system off

    def test_lamps_door_key_gear(self):
        v = decode_known("7F", "311800000000", "FE")                       # 22:00:41.1 fog on
        assert (v["lamp_tail"], v["lamp_head"], v["lamp_fog_front"]) == (True, True, True)
        assert v["illumination"] is True and v["door_open"] is False
        assert decode_known("7F", "301880000000", "FE")["door_open"] is True   # 21:53:57.0
        v = decode_known("6E", "8080", "62")                               # 22:01:34.5
        assert (v["key_out_warning"], v["key_out_beep"]) == (True, True)
        assert decode_known("6E", "8000", "62")["key_out_beep"] is False
        assert decode_known("40", "400000", "FE")["shift"] == "R"          # 21:50:01.0
        assert decode_known("40", "000000", "FE")["shift"] == ""           # transit

    def test_provisional_are_prefixed(self):
        assert decode_known("6D", "37807F", "52") == {"prov_cabin_temp_raw": 0x37,
                                                      "cabin_temp_c": 0.25 * 0x37 - 8.0}
        assert decode_known("50", "0C", "FE") == {
            "prov_p_locked": False, "prov_out_of_p": True, "prov_p_ecu_on": True,
            "prov_power_off_handshake": False}
        assert decode_known("C8", "0000", "62") == {"cruise_raw": "0000", "cruise_main": False}
        assert decode_known("C8", "0010", "62")["cruise_main"] is True
        assert "cruise_main" not in decode_known("C8", "001054", "62")   # malformed length


# ─────────────────────────────────────────────────────────────────────────────
# Source priority
# ─────────────────────────────────────────────────────────────────────────────

class TestSourceArbiter:
    def make(self):
        clk = Clock()
        arb = SourceArbiter([SignalPolicy("x", (Source("bean", 2.0), Source("obd", 3.0, SOLICITED)),
                                          solicited_slots=(0,), suspend_after_s=10.0)], clock=clk)
        return arb, clk

    def test_passive_fresh_wins_stale_falls_back(self):
        arb, clk = self.make()
        assert arb.offer("x", "obd") is True            # nothing better seen yet
        assert arb.offer("x", "bean") is True
        assert arb.active("x") == "bean"
        clk.t += 1.0
        assert arb.offer("x", "obd") is False           # passive fresh -> solicited ignored
        clk.t += 1.5                                    # bean 2.5 s old > 2.0
        assert arb.offer("x", "obd") is True
        assert arb.active("x") == "obd"
        assert arb.offer("x", "bean") is True
        assert arb.status()["x"]["active"] == "bean"

    def test_unknown_signal_and_source_pass(self):
        arb, _ = self.make()
        assert arb.offer("nope", "any") is True
        arb.offer("x", "bean")
        assert arb.offer("x", "other") is True

    def test_probe_delegates_freshness(self):
        clk = Clock()
        arb = SourceArbiter([SignalPolicy("t", (Source("avc", 15.0), Source("bean", 3.0)))], clock=clk)
        avc_fresh = {"v": True}
        arb.set_probe("avc", lambda sig: avc_fresh["v"])
        assert arb.offer("t", "bean") is False
        avc_fresh["v"] = False
        assert arb.offer("t", "bean") is True

    def test_passive_fresh_for(self):
        arb, clk = self.make()
        for _ in range(12):
            arb.offer("x", "bean")
            clk.t += 1.0
        assert arb.passive_fresh_for("x") == pytest.approx(12.0)
        clk.t += 5.0
        assert arb.passive_fresh_for("x") == 0.0

    def test_default_policies_rpm_slot(self):
        rpm = [p for p in DEFAULT_POLICIES if p.signal == "rpm"][0]
        assert rpm.sources[0].name == "bean" and rpm.solicited_slots == (0,)
        coolant = [p for p in DEFAULT_POLICIES if p.signal == "coolant"][0]
        assert [s.name for s in coolant.sources] == ["can", "bean", "can484", "obd"]


class TestSlotSuspender:
    def test_suspend_after_hold_resume_on_stale_and_no_flap(self):
        clk = Clock()
        arb = SourceArbiter([SignalPolicy("x", (Source("bean", 2.0), Source("obd", 3.0, SOLICITED)),
                                          solicited_slots=(0,), suspend_after_s=10.0)], clock=clk)
        sent = []
        sus = SlotSuspender(arb, lambda s: sent.append(("unsub", s)),
                            lambda s: sent.append(("sub", s)), resume_hold_s=30.0)

        def run(seconds, bean=True):
            for _ in range(int(seconds * 2)):
                if bean:
                    arb.offer("x", "bean")
                sus.tick()
                clk.t += 0.5

        run(9.0)
        assert sent == [] and sus.suspended == set()     # not yet 10 s
        run(2.0)
        assert sent == [("unsub", 0)] and sus.suspended == {0}
        run(3.0, bean=False)                             # BEAN silent > 2 s
        assert sent[-1] == ("sub", 0) and sus.suspended == set()
        run(20.0)                                        # back, but within the resume hold
        assert sent.count(("unsub", 0)) == 1
        run(15.0)
        assert sent.count(("unsub", 0)) == 2 and sus.suspended == {0}


# ─────────────────────────────────────────────────────────────────────────────
# Through the ingress: BEAN -> state, priority against CAN / OBD
# ─────────────────────────────────────────────────────────────────────────────

def make_ingress():
    store = Store()
    ing = IngressController(store, MockInputPort())
    register_powerbox_ingress(ing)
    clk = Clock()
    ing._sources._clock = clk
    ing._bean._clock = clk
    ing._cruise._clock = clk
    ing._dynamics._clock = clk
    ing._dynamics.odometer._clock = clk
    return store, ing, clk


def feed_bean(ing, frames, t=None):
    """A device-203 line with these (dst, mes, data) records."""
    recs = [[1000 + i, 6, d, m, x, 1, 0] for i, (d, m, x) in enumerate(frames)]
    ing._process_message(RawMessage(device_id=203, timestamp=0,
                                    data={"t": 1000 + len(recs), "bf": recs}))


def feed_can(ing, can_id, data):
    ing._process_message(RawMessage(device_id=1, timestamp=0,
                                    data={"i": "0x%03X" % can_id, "d": list(data)}))


def obd_rpm(ing, rpm):
    raw = int(rpm * 4)
    feed_can(ing, 0x7E8, [4, 0x41, 0x0C, raw >> 8, raw & 0xFF, 0, 0, 0])


class TestIngressBean:
    def test_confirmed_signals_land_in_state(self):
        store, ing, clk = make_ingress()
        feed_bean(ing, [("FE", "83", "60000008"), ("FE", "29", "00000B0000"),
                        ("98", "27", "09FF"), ("52", "ED", "6AA05F0BD2"),
                        ("98", "EC", "08AF0E2C063D"), ("52", "76", "003180"),
                        ("52", "90", "FFFE40"), ("FE", "24", "09"), ("62", "6E", "8080"),
                        ("52", "6D", "37807F"), ("52", "A2", "00BC40"), ("FE", "50", "84")])
        v, c, p = store.state.vehicle, store.state.climate, store.state.bean.provisional
        assert (v.acc_on, v.ig_on, v.brake_switch, v.ice_stopped) == (True, True, True, True)
        assert (v.meter_trip_km, v.meter_consumption_l100km, v.meter_consumption_stopped) == \
            (4.9, None, True)
        assert v.display_speed_kmh == 9 and v.real_speed_kmh == 0.0 and v.speed_kmh == 0.0
        assert (v.key_out_warning, v.key_out_beep) == (True, True)
        assert (c.compressor_target_rpm, c.compressor_rpm, c.compressor_running, c.ac_switch_on) == \
            (3026, 2223, True, True)
        assert (p.ac_engine_heat_request, p.cabin_temp_raw, p.avg_consumption_l100km) == \
            (True, 0x37, 18.8)
        assert (p.p_locked, p.p_ecu_on, p.out_of_p) == (True, True, False)
        assert store.state.dynamics.soc_bars == 3        # CAN 0x529 / AVC silent
        # the frame table still gets them too
        assert store.state.bean.messages["FE:83"]["data"] == "60000008"

    def test_display_speed_none_when_bean_silent(self):
        store, ing, clk = make_ingress()
        feed_bean(ing, [("FE", "24", "35")])
        assert store.state.vehicle.display_speed_kmh == 0x35
        clk.t += 2.0
        ing.update()
        assert store.state.vehicle.display_speed_kmh == 0x35
        clk.t += 2.0
        ing.update()
        assert store.state.vehicle.display_speed_kmh is None

    def test_real_speed_from_can_display_from_bean(self):
        store, ing, clk = make_ingress()
        raw = 4900                                   # 49.00 km/h on 0x0B4
        feed_can(ing, 0x0B4, [0, 0, 0, 0, 1, raw >> 8, raw & 0xFF, 0])
        feed_bean(ing, [("FE", "24", "37")])         # 55 km/h on the speedometer
        v = store.state.vehicle
        assert v.real_speed_kmh == pytest.approx(49.0) and v.speed_kmh == pytest.approx(49.0)
        assert v.display_speed_kmh == 55

    def test_rpm_bean_preferred_obd_fallback(self):
        store, ing, clk = make_ingress()
        obd_rpm(ing, 1000)
        assert store.state.vehicle.rpm == 1000       # no BEAN yet: OBD used
        feed_bean(ing, [("FE", "26", "11DC")])       # 893 rpm
        assert store.state.vehicle.rpm == 893
        clk.t += 0.5
        obd_rpm(ing, 1200)                           # BEAN fresh: OBD ignored
        assert store.state.vehicle.rpm == 893
        clk.t += 2.0                                 # BEAN 2.5 s old: stale
        obd_rpm(ing, 1200)
        assert store.state.vehicle.rpm == 1200
        assert ing.sources.active("rpm") == "obd"

    def test_coolant_can_preferred_bean_floor_never_used(self):
        store, ing, clk = make_ingress()
        feed_bean(ing, [("FE", "2C", "20")])         # floor: not a measurement
        assert store.state.vehicle.ice_coolant_temp is None
        feed_bean(ing, [("FE", "2C", "6E")])         # 55.0
        assert store.state.vehicle.ice_coolant_temp == 55.0
        feed_can(ing, 0x039, [54, 2, 13, 133])
        assert store.state.vehicle.ice_coolant_temp == 54
        feed_bean(ing, [("FE", "2C", "70")])         # CAN fresh: BEAN ignored
        assert store.state.vehicle.ice_coolant_temp == 54
        clk.t += 2.5
        feed_bean(ing, [("FE", "2C", "70")])
        assert store.state.vehicle.ice_coolant_temp == 56.0

    def test_outside_temp_bean_only_when_avc_stale(self):
        store, ing, clk = make_ingress()
        feed_bean(ing, [("FE", "CD", "34")])
        assert store.state.climate.outside_temp == 4.0
        # AVC fresh -> it wins (AvcIngest's own freshness, probed)
        ing._process_message(RawMessage(device_id=2, timestamp=0, data={
            "m": "1C6", "s": "110", "c": 15, "d": "00 E0 5D F7 46".split()}))
        assert store.state.climate.outside_temp == 22.0
        feed_bean(ing, [("FE", "CD", "34")])
        assert store.state.climate.outside_temp == 22.0

    def test_gear_and_lights_fallback(self):
        store, ing, clk = make_ingress()
        feed_can(ing, 0x120, [0, 0, 0, 0, 16, 0x23, 4, 93])      # D on CAN
        assert store.state.vehicle.gear == GearPosition.DRIVE
        feed_bean(ing, [("FE", "40", "400000")])                 # CAN fresh: ignored
        assert store.state.vehicle.gear == GearPosition.DRIVE
        clk.t += 3.0
        feed_bean(ing, [("FE", "40", "400000")])
        assert store.state.vehicle.gear == GearPosition.REVERSE
        feed_bean(ing, [("FE", "7F", "311880000000")])
        d = store.state.dynamics
        assert (d.headlight_state, d.fog_front, d.door_open, d.parking_lights) == \
            ("LOW", True, True, True)

    def test_climate_from_bean_when_avc_silent(self):
        store, ing, clk = make_ingress()
        feed_bean(ing, [("FE", "FC", "5A00000000A0"), ("52", "FD", "0938")])
        c = store.state.climate
        assert (c.defrost, c.rear_defogger, c.fan_speed, c.target_temp) == (True, True, 5, 20.0)
        assert c.status_source == "bean"

    def test_cruise_raw_changes_recorded(self):
        store, ing, clk = make_ingress()
        feed_bean(ing, [("62", "C8", "0000")])
        feed_bean(ing, [("62", "C8", "0000")])
        feed_bean(ing, [("62", "C8", "0010")])           # main switch on
        feed_can(ing, 0x5C8, [0xA4, 0, 0x10])         # immediate-copy header A4
        feed_can(ing, 0x5C8, [0x24, 0, 0x10])         # periodic copy: no raw change
        v = store.state.vehicle
        assert v.cruise_raw_62c8 == "0010"
        assert v.cruise_main_switch is True
        assert [(c["src"], c["data"]) for c in v.cruise_raw_changes] == [
            ("bean:62C8", "0000"), ("bean:62C8", "0010"), ("can:5C8", "24 00 10")]
        feed_can(ing, 0x5C8, [0xA4, 0, 0x00])         # CAN 0x5C8 leads BEAN (2026-11-17)
        assert store.state.vehicle.cruise_main_switch is False

    def test_stale_frames_not_arbitrated(self):
        """Freshness is the frame's true age, not its arrival time."""
        store, ing, clk = make_ingress()
        bi = BeanIngest(ing.sources, clock=clk, wall=lambda: 5000.0)
        old = fr("FE", "26", "2800")
        old["ts"] = 5000.0 - 3.0
        assert not any(type(a).__name__ == "SetRPMAction" for a in bi.process([old]))
        new = fr("FE", "26", "2800")
        new["ts"] = 5000.0 - 0.3
        assert any(type(a).__name__ == "SetRPMAction" for a in bi.process([new]))

    def test_own_and_bad_frames_ignored(self):
        store, ing, clk = make_ingress()
        bi = BeanIngest(ing.sources, clock=clk)
        own = fr("FE", "83", "60000008")
        own["own"] = True
        assert bi.process([own, fr("FE", "83", "60000008", ok=False)]) == []


# ─────────────────────────────────────────────────────────────────────────────
# Solicited slot suspension in the virtual twin
# ─────────────────────────────────────────────────────────────────────────────

class TestTwinSuspendsObdRpm:
    def test_obd_rpm_slot_dropped_while_bean_fresh(self):
        from cyberpunk_computer.io.factory import VirtualTwin, ExecutionMode
        from cyberpunk_computer.io.egress import EgressController
        from cyberpunk_computer.state.rules import RulesEngine

        class Out:
            def __init__(self):
                self.sent = []

            def is_connected(self):
                return True

            def send(self, cmd):
                self.sent.append(cmd.payload)
                return True

        store, ing, clk = make_ingress()
        out = Out()
        twin = VirtualTwin(store=store, input_port=ing.input_port, output_port=out, ingress=ing,
                           egress=EgressController(store, out), rules_engine=RulesEngine(store),
                           mode=ExecutionMode.DEVELOPMENT, _enable_solicited=True)
        twin._slot_suspender._clock = clk
        for _ in range(25):                      # 12.5 s of FE:26 at 2 Hz
            feed_bean(ing, [("FE", "26", "11DC")])
            twin._solicited_tick()
            clk.t += 0.5
        assert {"a": "unsub", "slot": 0} in out.sent
        assert twin.solicited_status["suspended_slots"] == [0]
        # a full (re)setup now leaves slot 0 out
        out.sent.clear()
        twin._init_solicited_subscriptions()
        assert all(p.get("slot") != 0 for p in out.sent if p.get("a") == "sub")
        # BEAN goes silent: slot 0 comes back with its OBD 010C payload
        out.sent.clear()
        clk.t += 3.0
        twin._solicited_tick()
        subs = [p for p in out.sent if p.get("a") == "sub"]
        assert len(subs) == 1 and subs[0]["slot"] == 0 and subs[0]["d"][2] == 0x0C


# ─────────────────────────────────────────────────────────────────────────────
# Frame clock
# ─────────────────────────────────────────────────────────────────────────────

class TestBeanClock:
    def simulate(self, seconds=300.0, drift=-380e-6, ahead=236.7, seed=1):
        """Frames every 25 ms on a frame clock that runs ``ahead`` s ahead of
        the true time and ``drift`` slow, delivered in batches every ~0.3 s
        with 10-60 ms transport latency."""
        rnd = random.Random(seed)
        clk = BeanClock()
        errs, raw = [], []
        t, next_batch, batch = 1_000.0, 1_000.3, []
        while t < 1_000.0 + seconds:
            t_ms = int(((t - 1_000.0) * (1 + drift) + 5_000 + ahead) * 1000) % (1 << 30)
            batch.append((t, t_ms))
            t += 0.025
            if t >= next_batch:
                recv = t + rnd.uniform(0.010, 0.060)
                recs = [[m, 6, "FE", "24", "00", 1, 0] for _, m in batch]
                out = frames_from_batch(recs, line_t=10, recv_ts=recv, clock=clk)
                for (true_t, _), f in zip(batch, out):
                    errs.append(f["ts"] - true_t)
                    raw.append(recv - true_t)
                    assert f["ts"] <= recv + 0.001     # ts is rounded to ms
                batch, next_batch = [], t + rnd.uniform(0.2, 0.8)
        return errs, raw

    def test_lower_envelope_tracks_offset_and_drift(self):
        errs, raw = self.simulate()
        tail = errs[len(errs) // 4:]
        assert max(abs(e) for e in tail) < 0.08            # within transport jitter
        assert sorted(raw)[len(raw) // 2] > 0.2            # raw receive time was late

    def test_wrap_and_reset(self):
        clk = BeanClock()
        top = (1 << 30) - 50
        a = clk.observe(100.0, [top])
        b = clk.observe(100.1, [30])                       # wrapped: +80 ms, not -12 days
        assert b[0] - a[0] == pytest.approx(0.080)
        clk.observe(101.0, [900])
        clk.observe(102.0, [5_000_000])                    # firmware restart: jump
        assert clk.resets == 1

    def test_wall_clock_step_does_not_shift_frames(self):
        """2026-11-15: a wall step (NTP after the RTC-less phone boots) or an
        upward frame-clock jump left the old, lower buckets in the envelope
        and dated ~2 min of frames ~24 s too early. The estimator now runs on
        the monotonic clock and restarts on a jump in either direction."""
        clk = BeanClock()
        worst = 0.0
        for k in range(2400):                     # 10 min, batches every 0.25 s
            mono = 1000.0 + k * 0.25
            wall = mono + (24.0 if k > 1200 else 0.0)
            t_ms = 7_000_000 + int((mono - 1000.0 - 0.05) * 1000)
            (f,) = frames_from_batch([[t_ms, 6, "FE", "24", "00", 1, 0]], 10, wall,
                                     clock=clk, recv_mono=mono)
            # constant latency: the lower envelope dates frames at receipt
            worst = max(worst, abs(f["ts"] - wall))
        assert worst < 0.01

    def test_upward_frame_clock_jump_restarts(self):
        clk = BeanClock()
        errs = []
        for k in range(2400):
            recv = 1000.0 + k * 0.25
            jump = 24_000 if k > 1200 else 0      # pre-2.4.1 wake-up jump: t_ms falls behind
            t_ms = 7_000_000 + int((recv - 1000.0 - 0.05) * 1000) - jump
            (f,) = frames_from_batch([[t_ms, 6, "FE", "24", "00", 1, 0]], 10, recv,
                                     clock=clk, recv_mono=recv)
            errs.append(f["ts"] - recv)
        assert sum(1 for e in errs if abs(e) > 1.0) <= 1
        assert clk.resets >= 1

    def test_no_extrapolation_across_a_parked_gap(self):
        clk = BeanClock()
        for k in range(400):                      # drive 1: frame clock slow 1 %
            recv = 1000.0 + k * 0.25
            clk.observe(recv, [int((recv - 1000.0) * 990)])
        recv = 1000.0 + 122_400.0                 # 34 h later, same clock base
        clk.observe(recv, [int((recv - 1000.0) * 990)])
        assert clk.offset(recv) == pytest.approx(recv - (recv - 1000.0) * 0.990, abs=0.01)

    def test_plausible_line_age_still_used(self):
        (f,) = frames_from_batch([[10_000, 6, "FE", "2C", "64", 1, 0]], 10_500, 50.0,
                                 clock=BeanClock())
        assert f["ts"] == pytest.approx(49.5)


# ─────────────────────────────────────────────────────────────────────────────
# CAN decoder fixes (0x5CC, 0x57F)
# ─────────────────────────────────────────────────────────────────────────────

class TestCanGatewayCopies:
    def test_5cc_is_not_ambient(self):
        m = CANDecoder().decode({"i": "0x5CC", "d": [0x24, 0x02, 0x01]})   # trip recording
        assert "ambient_temp" not in m.values
        assert m.msg_type == CANMessageType.SYSTEM_STATUS
        assert m.values["bean_98cc_raw"] == 0x201

    def test_57f_door_open_and_fog(self):
        m = CANDecoder().decode({"i": "0x57F", "d": [0x68, 0x31, 0x18, 0x80, 0, 0, 0]})
        v = m.values
        assert v["door_open"] is True and "drl_active" not in v
        assert (v["fog_front"], v["low_beam"], v["parking_lights"], v["illumination"]) == \
            (True, True, True, True)
        assert v["headlight_state"] == "LOW"                 # fog bit no longer -> "OFF"
        v = CANDecoder().decode({"i": "0x57F", "d": [0x68, 0x10, 0x18, 0, 0, 0, 0]}).values
        assert (v["headlight_state"], v["door_open"]) == ("PARK", False)

    def test_57f_reaches_dynamics(self):
        store, ing, clk = make_ingress()
        feed_can(ing, 0x57F, [0x68, 0x30, 0x18, 0x80, 0, 0, 0])
        d = store.state.dynamics
        assert d.door_open is True and d.headlight_state == "LOW"


def test_cabin_temperature_from_52_6d_and_its_expiry():
    store, ing, clk = make_ingress()
    feed_bean(ing, [("52", "6D", "6C807F")])          # 0x6C = 108 -> 19.0 °C
    assert store.state.climate.inside_temp == 19.0
    clk.t += 11.0
    ing.update()
    assert store.state.climate.inside_temp is None
