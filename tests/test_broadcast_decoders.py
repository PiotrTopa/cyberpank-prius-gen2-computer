"""Broadcast CAN decodes of the 2026-11-17 drive (docs/prius_can.md §2):
decoder values on real frames, and the stateful ingress part
(io.dynamics_ingest: yaw offsets, distance, own odometer, expiry; source
arbitration of speed, rpm, MG2 rpm, HV voltage/current, brake switch)."""

import json

import pytest

from cyberpunk_computer.comm.can_decoder import (
    CANDecoder, CANMessageType, YAW_DEG_S_PER_COUNT, decode_yaw_counts,
)
from cyberpunk_computer.io.dynamics_ingest import (
    METER_ODOMETER_FROZEN_KM, OwnOdometer, STALE_S,
)

from test_bean_signals import feed_bean, feed_can, make_ingress, obd_rpm


def dec(can_id, data):
    return CANDecoder().decode({"i": "0x%03X" % can_id, "d": list(data)})


# ── decoder: real frames of trip_20261117_100957_645 ─────────────────────────

def test_real_frames_decode():
    m = dec(0x022, [1, 254, 1, 249, 0, 0, 0, 35])
    assert m.msg_type == CANMessageType.YAW_RATE
    assert (m.values["yaw_raw_1"], m.values["yaw_raw_2"]) == (-2, -7)   # standstill offsets
    m = dec(0x023, [2, 1, 2, 17, 0, 0, 64])
    assert m.msg_type == CANMessageType.ACCELERATION
    assert (m.values["accel_raw_p"], m.values["accel_raw_q"]) == (1, 17)
    assert m.values["longitudinal_accel_ms2"] == pytest.approx(-0.32)
    assert m.values["lateral_accel_ms2"] == pytest.approx(0.45)
    m = dec(0x244, [16, 0, 0, 0, 6, 255, 18, 117])
    assert m.values["mg2_rpm"] == 0x6FF and m.values["throttle"] == 18
    assert dec(0x244, [16, 0, 0, 0, 0xFF, 0x5F, 0, 0]).values["mg2_rpm"] == -161   # reverse
    m = dec(0x230, [141, 121, 0, 4, 0, 0, 63])
    assert m.msg_type == CANMessageType.DISTANCE
    assert m.values["distance_counter"] == 141 * 256 + 121 and m.values["brake_flag"] is True
    m = dec(0x038, [192, 13, 12, 0, 0, 0, 24])
    assert m.values["ice_running"] and m.values["ice_power_request_kw"] == pytest.approx(2.99)
    m = dec(0x348, [4, 96, 52, 0, 32, 9])
    assert m.msg_type == CANMessageType.ICE_SPEED and m.values["ice_rpm"] == 1300
    m = dec(0x3C8, [0, 52, 40, 0, 44])
    assert m.msg_type == CANMessageType.ICE_SPEED and m.values["ice_rpm"] == 1280
    assert "soc_alt" not in m.values
    m = dec(0x3CA, [0, 32, 51, 0, 37])
    assert m.msg_type == CANMessageType.VEHICLE_SPEED_KMH and m.values["speed_kph"] == 51
    assert dec(0x3CD, [0, 0, 0, 239, 196]).values["pack_voltage_3cd"] == 239
    m = dec(0x484, [0, 91, 78, 12, 32])
    assert m.msg_type == CANMessageType.COOLANT_ALT and m.values["coolant_temp"] == pytest.approx(56.4)
    m = dec(0x03A, [0, 0, 0, 0, 0x34, 230, 91])
    assert m.msg_type == CANMessageType.POWERTRAIN_FLAGS and m.values["accelerator_pressed"]
    assert not dec(0x03A, [0, 0, 0, 0, 0x24, 230, 91]).values["accelerator_pressed"]
    m = dec(0x0B4, [0, 0, 0, 0, 1, 19, 142, 94])
    assert m.values["speed_kph"] == pytest.approx(50.06) and m.values["moving"] is True
    # 0x120 byte H is a checksum: only E bit 7 is "engaged"
    assert dec(0x120, [0, 0, 0, 0, 0x10, 0x23, 0x04, 0xE0]).values["cruise_active"] is False


def test_yaw_helper_shared_with_feeders():
    assert decode_yaw_counts(bytes([2, 0, 1, 0])) == (0, -256)
    assert decode_yaw_counts(b"\x01") is None
    assert YAW_DEG_S_PER_COUNT == pytest.approx(0.2521, abs=1e-4)


# ── yaw offsets, accelerations ───────────────────────────────────────────────

def speed(kmh, moving=None):
    w = int(round(kmh * 100))
    mv = (kmh > 5) if moving is None else moving
    return [0, 0, 0, 0, 1 if mv else 0, w >> 8, w & 0xFF, 0]


def yaw(c1, c2):
    a, b = c1 + 0x200, c2 + 0x200
    return [a >> 8, a & 0xFF, b >> 8, b & 0xFF, 0, 0, 0, 0]


def test_yaw_standstill_offset_learnt_and_removed():
    store, ing, clk = make_ingress()
    for _ in range(100):                     # 10 s standing, channels at -3 / -6
        feed_can(ing, 0x0B4, speed(0.0))
        feed_can(ing, 0x022, yaw(-3, -6))
        clk.t += 0.1
    d = store.state.dynamics
    assert d.yaw_rate_dps == 0.0
    assert d.yaw_offsets[0] == pytest.approx(-3, abs=0.1)
    assert d.yaw_offsets[1] == pytest.approx(-6, abs=0.1)
    feed_can(ing, 0x0B4, speed(40.0))
    feed_can(ing, 0x022, yaw(17, 14))        # +20 counts on both channels: left turn
    d = store.state.dynamics
    assert d.yaw_rate_dps == pytest.approx(20 * YAW_DEG_S_PER_COUNT, abs=0.05)
    assert (d.yaw_rate_raw, d.yaw_rate_raw_2) == (17, 14)


def test_yaw_not_learnt_while_rolling_or_settling():
    store, ing, clk = make_ingress()
    feed_can(ing, 0x0B4, speed(0.0))
    feed_can(ing, 0x022, yaw(30, 30))        # just stopped (< 1 s): not learnt
    assert ing.dynamics.offsets == [-2.0, -7.0]
    feed_can(ing, 0x0B4, speed(3.0, moving=False))
    clk.t += 2
    feed_can(ing, 0x022, yaw(30, 30))        # creeping: not learnt
    assert ing.dynamics.offsets == [-2.0, -7.0]


def test_accelerations_and_legacy_raw_view():
    store, ing, clk = make_ingress()
    feed_can(ing, 0x023, [2, 50, 1, 206, 0, 0, 0])   # P = 50, Q = -50
    d = store.state.dynamics
    assert d.longitudinal_accel_ms2 == pytest.approx(2.0)
    assert d.lateral_accel_ms2 == pytest.approx(0.0)
    assert (d.longitudinal_accel_raw, d.lateral_accel_raw) == (100, 0)


def test_values_expire_when_the_bus_goes_silent():
    store, ing, clk = make_ingress()
    feed_can(ing, 0x022, yaw(5, 5))
    feed_can(ing, 0x023, [2, 10, 2, 10, 0, 0, 0])
    feed_can(ing, 0x03A, [0, 0, 0, 0, 0x34, 0, 0])
    feed_can(ing, 0x038, [192, 13, 12, 0, 0, 0, 24])
    feed_can(ing, 0x0B4, speed(20.0))
    v, d = store.state.vehicle, store.state.dynamics
    assert d.yaw_rate_dps is not None and d.lateral_accel_ms2 is not None
    assert v.accelerator_pressed is True and v.ice_power_request_kw == pytest.approx(2.99)
    assert v.moving is True
    clk.t += STALE_S + 0.5
    ing.update()
    v, d = store.state.vehicle, store.state.dynamics
    assert d.yaw_rate_dps is None and d.lateral_accel_ms2 is None
    assert v.accelerator_pressed is None and v.ice_power_request_kw is None and v.moving is None


# ── distance and own odometer ────────────────────────────────────────────────

def counter(c, brake=False):
    return [(c >> 8) & 0xFF, c & 0xFF, 0, 0x04 if brake else 0, 0, 0, 0]


def test_distance_from_the_counter_with_wrap_and_gap():
    store, ing, clk = make_ingress()
    feed_can(ing, 0x0B4, speed(50.0))
    c = 65000
    feed_can(ing, 0x230, counter(c))
    for _ in range(100):                     # 10 s at 50 km/h = 138.9 m
        clk.t += 0.1
        c = (c + 143) & 0xFFFF               # 1.39 m per 0.1 s x 102.8 counts/m
        feed_can(ing, 0x0B4, speed(50.0))
        feed_can(ing, 0x230, counter(c))
    v = store.state.vehicle
    assert v.distance_source == "can230"
    assert v.distance_total_km * 1000 == pytest.approx(100 * 143 / 102.8, rel=1e-3)
    before = v.distance_total_km
    clk.t += 5                               # gap: re-anchor, nothing counted
    feed_can(ing, 0x230, counter(c + 30000))
    assert store.state.vehicle.distance_total_km == before
    clk.t += 0.1
    feed_can(ing, 0x0B4, speed(50.0))
    feed_can(ing, 0x230, counter(c + 30000 + 5000))   # 48 m in 0.1 s at 50 km/h: rejected
    assert store.state.vehicle.distance_total_km == before


def test_distance_falls_back_to_speed_and_brake_flag():
    store, ing, clk = make_ingress()
    for _ in range(11):                      # no 0x230: 1 s at 36 km/h = 10 m
        feed_can(ing, 0x0B4, speed(36.0))
        clk.t += 0.1
    v = store.state.vehicle
    assert v.distance_source == "speed"
    assert v.distance_total_km == pytest.approx(0.010, abs=0.0005)
    feed_can(ing, 0x230, counter(100, brake=True))
    assert store.state.vehicle.brake_switch is True
    feed_bean(ing, [("FE", "29", "0000000000")])  # BEAN brake off: CAN is fresher
    assert store.state.vehicle.brake_switch is True


def test_own_odometer_anchors_to_the_frozen_meter_and_persists(tmp_path):
    path = tmp_path / "odometer.json"
    store, ing, clk = make_ingress()
    ing.set_odometer_file(str(path))
    assert store.state.vehicle.odometer_km is None          # no file, no 52:3F yet
    feed_can(ing, 0x0B4, speed(50.0))
    feed_can(ing, 0x230, counter(0))
    clk.t += 0.1
    feed_can(ing, 0x230, counter(1028))                     # distance before any anchor
    assert store.state.vehicle.odometer_km is None
    feed_bean(ing, [("52", "3F", "0493DF80")])              # meter: 299,999 (frozen)
    assert store.state.vehicle.odometer_km == METER_ODOMETER_FROZEN_KM
    c = 1028
    for _ in range(30):
        clk.t += 0.1
        c += 140
        feed_can(ing, 0x0B4, speed(50.0))
        feed_can(ing, 0x230, counter(c))
    km = store.state.vehicle.odometer_km
    assert km == pytest.approx(299999 + 30 * 140 / 102.8 / 1000, abs=0.015)  # shown per 10 m
    feed_bean(ing, [("52", "3F", "0493DF80")])              # frozen meter: ours stays ahead
    assert store.state.vehicle.odometer_km == km
    clk.t += STALE_S + 1                                    # car off: flushed
    ing.update()
    saved = json.loads(path.read_text())
    assert saved["km"] == pytest.approx(km, abs=0.015) and saved["meter_km"] == 299999
    store2, ing2, _ = make_ingress()
    ing2.set_odometer_file(str(path))
    assert store2.state.vehicle.odometer_km == pytest.approx(saved["km"], abs=0.006)


def test_odometer_unreadable_file_and_meter_ahead(tmp_path):
    p = tmp_path / "odo.json"
    p.write_text("\0\0\0garbage")
    o = OwnOdometer(str(p))
    assert o.km is None
    o.anchor_meter(310000)
    assert o.km == 310000
    o.add(1.5)
    assert o.km == 310001.5
    o.anchor_meter(320000)                   # meter ahead (drives we missed)
    assert o.km == 320000


# ── source arbitration ───────────────────────────────────────────────────────

def test_wheel_mean_does_not_override_true_speed():
    store, ing, clk = make_ingress()
    feed_can(ing, 0x0B4, speed(50.0))
    feed_can(ing, 0x0B3, [0x13, 0x88, 0x13, 0x60, 0, 0, 0, 0])   # 50.00 / 49.60
    assert store.state.vehicle.real_speed_kmh == pytest.approx(50.0)
    clk.t += 1.0                                            # 0x0B4 stale
    feed_can(ing, 0x3CA, [0, 0, 48, 0, 0])
    assert store.state.vehicle.real_speed_kmh == 48
    clk.t += 1.5                                            # 0x3CA stale too
    feed_can(ing, 0x0B3, [0x13, 0x88, 0x13, 0x60, 0, 0, 0, 0])
    assert store.state.vehicle.real_speed_kmh == pytest.approx(49.8)


def test_can_rpm_behind_bean_ahead_of_obd():
    store, ing, clk = make_ingress()
    feed_can(ing, 0x348, [4, 96, 52, 0, 32, 9])             # 1300 rpm
    assert store.state.vehicle.rpm == 1300
    obd_rpm(ing, 1500)                                      # OBD ignored while CAN fresh
    assert store.state.vehicle.rpm == 1300
    feed_bean(ing, [("FE", "26", "2800")])                  # BEAN FE:26 wins
    bean_rpm = store.state.vehicle.rpm
    feed_can(ing, 0x348, [4, 96, 60, 0, 32, 9])
    assert store.state.vehicle.rpm == bean_rpm
    assert ing.sources.passive_fresh("rpm")                 # slot 0 may be suspended


def test_mg2_rpm_from_broadcast_beats_21c3():
    store, ing, clk = make_ingress()
    feed_can(ing, 0x244, [16, 0, 0, 0, 0x07, 0xD0, 18, 0])   # 2000 rpm
    assert store.state.vehicle.mg2_rpm == 2000
    payload = [0x61, 0xC3] + [0] * 36
    payload[2], payload[3] = 0x3F + 0x08, 0xFF               # 21C3 MG2 = 2048 - 16383 + ...
    ing._process_message(_resp(0x7EA, payload))
    assert store.state.vehicle.mg2_rpm == 2000
    clk.t += 1.5
    ing._process_message(_resp(0x7EA, payload))
    assert store.state.vehicle.mg2_rpm != 2000


def test_hv_voltage_and_current_not_overwritten_by_21c3():
    store, ing, clk = make_ingress()
    feed_can(ing, 0x03B, [0, 18, 0, 220, 46])               # 1.8 A, 220 V
    payload = [0x61, 0xC3] + [0] * 36
    payload[2 + 28] = 120                                    # 21C3 AC: 240 V
    payload[2 + 30] = 140                                    # 21C3 AE: 24 A
    ing._process_message(_resp(0x7EA, payload))
    e = store.state.energy
    assert e.hv_battery_voltage == 220 and e.hv_battery_current == pytest.approx(1.8)
    clk.t += 1.5                                             # 0x03B stale: 0x3CD, then 21C3
    feed_can(ing, 0x3CD, [0, 0, 0, 239, 196])
    assert store.state.energy.hv_battery_voltage == 239
    clk.t += 1.5
    ing._process_message(_resp(0x7EA, payload))
    e = store.state.energy
    assert e.hv_battery_voltage == 240 and e.hv_battery_current == 24


def test_coolant_third_source():
    store, ing, clk = make_ingress()
    feed_can(ing, 0x484, [0, 91, 78, 12, 32])
    assert store.state.vehicle.ice_coolant_temp == pytest.approx(56.4)
    feed_can(ing, 0x039, [57, 0, 0, 0])
    feed_can(ing, 0x484, [0, 100, 78, 12, 32])
    assert store.state.vehicle.ice_coolant_temp == 57


def _resp(can_id, data):
    from cyberpunk_computer.io.ports import RawMessage
    return RawMessage(device_id=1, timestamp=0,
                      data={"i": "0x%03X" % can_id, "d": data, "a": "sub", "slot": 2})


def test_isotp_consecutive_frame_is_not_a_reply():
    """The panda sees every frame of a multi-frame 21C3 answer; a consecutive
    frame whose bytes read 61 C3 must not decode as a reply."""
    m = dec(0x7EA, [0x21, 0x61, 0xC3, 0x50, 0x00, 0x11, 0x22, 0x33])
    assert "mg2_rpm" not in m.values and "obd2_mode" not in m.values
    m = dec(0x7EA, [0x10, 0x26, 0x61, 0xC3, 0x50, 0x00, 0x11, 0x22])   # first frame
    assert "obd2_mode" not in m.values
    m = dec(0x7E8, [0x04, 0x41, 0x0C, 0x14, 0x50, 0x55, 0x55, 0x55])   # padding not payload
    assert m.values["rpm"] == 0x1450 / 4 and m.values["obd2_raw"] == [0x14, 0x50]
    assert dec(0x7E8, [0x01, 0x44, 0, 0, 0, 0, 0, 0]).values.get("dtc_cleared")
    v = dec(0x7EA, [7, 0x61, 0xD3, 50, 50, 40, 0xCF, 0x10]).values
    assert v["cruise_set_speed"] == 50 and v["cruise_switch_set_coast"]
