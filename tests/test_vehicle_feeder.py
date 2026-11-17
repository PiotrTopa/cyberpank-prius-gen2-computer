"""The feeder's roadsense vehicle input API 1.1 side (backend/position_feeder.py).

Decode of the frames behind `controls` / `body` / accelerations (bytes as
recorded on the car, trip_20261117_163831), the negotiation with mapd's hello
(an older roadsense gets exactly the v1 messages), what goes out when, the
hooks (turn signal, range sensors), and one end-to-end pass through the real
roadsense checkout (mapd + api) when it is present.
"""

import pytest

from cyberpunk_computer.backend.position_feeder import (
    PROVIDES,
    SIGNAL_QUALITY,
    CanMotionTap,
    NoGnssSource,
    PositionFeeder,
    PositionFeederConfig,
    vehicle_description,
)
from tests.test_position_feeder import Clock, FakeFeed

V1 = ["gnss", "motion", "reset"]
V11 = V1 + ["controls", "body", "vehicle", "objects"]


def _frames(tap, t, kmh=36.0, gear=0x23, cruise=False, brake=False, pedal=0, main=False, lamp=0x00, door=False,
            gyro=(0x200 - 2, 0x200 - 7), accel=(0x209, 0x1FE)):
    sp = int(kmh * 100)
    tap.on_frame(0x0B4, bytes([0, 0, 0, 0, 0, sp >> 8, sp & 0xFF, 0]), 0, 0.0, t)
    tap.on_frame(0x0B1, bytes([sp >> 8, sp & 0xFF, sp >> 8, sp & 0xFF, 0, 0]), 0, 0.0, t)
    tap.on_frame(0x0B3, bytes([sp >> 8, sp & 0xFF, sp >> 8, sp & 0xFF, 0, 0]), 0, 0.0, t)
    tap.on_frame(0x120, bytes([0, 0, 0, 0, 0x90 if cruise else 0x10, gear, 4, 93]), 0, 0.0, t)
    tap.on_frame(0x025, bytes([0, 18, 0, 0, 122, 122, 122, 173]), 0, 0.0, t)
    tap.on_frame(0x022, bytes([gyro[0] >> 8, gyro[0] & 0xFF, gyro[1] >> 8, gyro[1] & 0xFF, 0, 0, 0, 35]), 0, 0.0, t)
    tap.on_frame(0x023, bytes([accel[0] >> 8, accel[0] & 0xFF, accel[1] >> 8, accel[1] & 0xFF, 0, 0, 52]), 0, 0.0, t)
    tap.on_frame(0x230, bytes([0, 5, 0, 4 if brake else 0, 0, 0, 66]), 0, 0.0, t)
    tap.on_frame(0x244, bytes([16, 0, 0, 0, 0, 0, pedal, 94]), 0, 0.0, t)
    tap.on_frame(0x5C8, bytes([36, 0, 0x10 if main else 0]), 0, 0.0, t)
    tap.on_frame(0x57F, bytes([104, lamp, 16, 0x80 if door else 0, 0, 0, 0]), 0, 0.0, t)


def _feeder(accepts, **cfg):
    c = Clock()
    tap = CanMotionTap()
    feed = FakeFeed(accepts=accepts)
    feed.connects = 1
    f = PositionFeeder(PositionFeederConfig(enabled=True, **cfg), NoGnssSource(), tap=tap, feed=feed, clock=c.mono)
    return c, tap, feed, f


def _of(feed, kind):
    return [m for m in feed.msgs if m["type"] == kind]


# ── decode ───────────────────────────────────────────────────────────────────

def test_tap_decodes_the_frames_recorded_on_the_car():
    tap = CanMotionTap()
    t = 100.0
    # parked in P with the brake held, lamps off (first frames of the 2026-11-17 16:38 trip)
    tap.on_frame(0x023, bytes([2, 9, 1, 254, 0, 0, 52]), 0, 0.0, t)
    tap.on_frame(0x230, bytes([0, 5, 0, 4, 0, 0, 66]), 0, 0.0, t)
    tap.on_frame(0x244, bytes([16, 0, 0, 0, 0, 0, 0, 94]), 0, 0.0, t)
    tap.on_frame(0x120, bytes([0, 0, 0, 0, 16, 32, 4, 93]), 0, 0.0, t)
    tap.on_frame(0x5C8, bytes([36, 0, 0]), 0, 0.0, t)
    tap.on_frame(0x57F, bytes([104, 0, 16, 0, 0, 0, 0]), 0, 0.0, t)
    assert tap.accel == (pytest.approx(0.020 * (9 - (-2))), pytest.approx(0.025 * (9 + (-2))), t)
    assert tap.brake == (True, t) and tap.pedal == (0.0, t)
    assert tap.gear == ("P", t) and tap.cruise_engaged == (False, t) and tap.cruise_main == (False, t)
    assert tap.lamps == (0x00, False, t)
    cfg = PositionFeederConfig()
    c = tap.controls(t + 0.1, cfg)
    assert (c["type"], c["brake"], c["accelerator"], c["cruise"], c["source"]) == ("controls", True, 0.0, "off", "prius-can")
    assert c["quality"] == {"accelerator": "unverified"} and c["ts_mono_ns"] == int(t * 1e9)
    b = tap.body(t + 0.1, cfg)
    assert (b["low_beam"], b["high_beam"], b["parking_lights"], b["fog_front"], b["door_open"]) == (False,) * 5
    assert b["quality"] == {"high_beam": "unverified"} and "outside_temp_c" not in b
    # lamps: 0x10 parking, 0x20 low beam, 0x08 high beam, 0x01 front fog; D bit 7 a door is open
    tap.on_frame(0x57F, bytes([104, 0x39, 16, 0x80, 0, 0, 0]), 0, 0.0, t + 1)
    b = tap.body(t + 1.1, cfg)
    assert (b["low_beam"], b["high_beam"], b["parking_lights"], b["fog_front"], b["door_open"]) == (True, True, True, True, True)
    # cruise: main switch on = standby, 0x120 E bit 7 = active (the set speed only then, from the state)
    tap.on_frame(0x5C8, bytes([36, 0, 0x10]), 0, 0.0, t + 1)
    tap.on_frame(0x120, bytes([0, 0, 0, 0, 0x10, 0x23, 4, 93]), 0, 0.0, t + 1)
    tap.set_state_value("cruise_set_speed_mps", 25.0, t + 1)
    c = tap.controls(t + 1.1, cfg)
    assert c["cruise"] == "standby" and "cruise_set_speed_mps" not in c
    tap.on_frame(0x120, bytes([0, 0, 0, 0, 0x90, 0x23, 4, 93]), 0, 0.0, t + 1.2)
    c = tap.controls(t + 1.3, cfg)
    assert (c["cruise"], c["cruise_set_speed_mps"]) == ("active", 25.0)
    assert c["quality"]["cruise_set_speed_mps"] == "uncalibrated"
    tap.on_frame(0x244, bytes([16, 0, 0, 0, 0, 0, 100, 94]), 0, 0.0, t + 1.2)
    assert tap.controls(t + 1.3, cfg)["accelerator"] == 0.5
    tap.on_frame(0x244, bytes([16, 0, 0, 0, 0, 0, 0xFF, 94]), 0, 0.0, t + 1.25)      # out of range: kept out
    assert tap.pedal == (0.5, t + 1.2)


def test_nothing_stale_goes_out():
    tap = CanMotionTap()
    cfg = PositionFeederConfig()
    assert tap.controls(100.0, cfg) is None and tap.body(100.0, cfg) is None
    _frames(tap, 100.0, brake=True, main=True, lamp=0x20)
    tap.set_state_value("outside_temp_c", 4.5, 100.0)
    assert tap.controls(100.2, cfg)["brake"] is True
    c = tap.controls(101.0, cfg)                       # brake / pedal / 0x120 older than 0.5 s: left out
    assert c is None
    b = tap.body(102.5, cfg)                           # the 1 Hz lamp frame lives 3 s, the state value 5 s
    assert b["low_beam"] is True and b["outside_temp_c"] == 4.5
    b = tap.body(104.0, cfg)
    assert "low_beam" not in b and b["outside_temp_c"] == 4.5
    assert tap.body(106.0, cfg) is None
    tap.set_state_value("outside_temp_c", None)        # the state says unknown
    assert "outside_temp_c" not in tap.state_values


# ── negotiation ──────────────────────────────────────────────────────────────

def test_older_roadsense_gets_exactly_v1():
    for accepts in (None, V1, []):
        c, tap, feed, f = _feeder(accepts)
        for _ in range(30):
            _frames(tap, c.t, brake=True)
            f.step(c.t)
            c.tick(0.1)
        assert {m["type"] for m in feed.msgs} == {"motion"}
        m = feed.msgs[-1]
        assert set(m) == {"type", "ts_mono_ns", "speed_mps", "gear", "wheel_speeds_mps", "source"}
        st = f.status(c.t)
        assert st["vehicle_api"] is False and st["controls_sent"] == 0 and st["motion"]["yaw_input"] == "wheels"


def test_vehicle_api_can_be_switched_off():
    c, tap, feed, f = _feeder(V11, vehicle_api=False)
    _frames(tap, c.t, brake=True)
    f.step(c.t)
    assert [m["type"] for m in feed.msgs] == ["motion"] and "quality" not in feed.msgs[0]


def test_vehicle_api_sends_description_motion_controls_body():
    c, tap, feed, f = _feeder(V11)
    _frames(tap, c.t, brake=True, lamp=0x20)
    tap.set_state_value("outside_temp_c", 7.0, c.t)
    f.step(c.t)
    assert [m["type"] for m in feed.msgs] == ["motion", "vehicle", "controls", "body"]
    mo, veh, ctl, body = feed.msgs
    assert (veh["name"], veh["wheelbase_m"], veh["track_m"], veh["steering_ratio"]) == ("Toyota Prius NHW20", 2.70, 1.48, 19.1)
    assert "camera" not in veh and "sensors" not in veh
    assert set(mo) >= {"speed_mps", "gear", "wheel_speeds_mps", "steering_wheel_deg", "yaw_rate_rps",
                       "accel_long_mps2", "accel_lat_mps2", "quality"}
    # the yaw-rate sensor is not calibrated: offered for roadsense's estimate only unless `gyro` is chosen
    assert mo["quality"] == {"wheel_speeds_mps": "uncalibrated", "accel_long_mps2": "uncalibrated",
                             "accel_lat_mps2": "uncalibrated", "yaw_rate_rps": "unverified"}
    assert mo["accel_long_mps2"] == pytest.approx(0.020 * (9 + 2)) and "turn_signal" not in mo
    assert (ctl["brake"], ctl["cruise"], body["low_beam"], body["outside_temp_c"]) == (True, "off", True, 7.0)
    st = f.status(c.t)
    assert st["vehicle_api"] is True and st["motion"]["yaw_input"] == "wheels+steer"
    assert (st["vehicle_sent"], st["controls_sent"], st["body_sent"]) == (1, 1, 1)


def test_gyro_yaw_input_offers_the_sensor_for_use():
    c, tap, feed, f = _feeder(V11, yaw_inputs=("wheels", "gyro"))
    _frames(tap, c.t, gyro=(0x200 + 18, 0x200 + 13))          # 20 counts above the standstill offsets
    f.step(c.t)
    mo = feed.msgs[0]
    assert mo["quality"]["yaw_rate_rps"] == "uncalibrated"
    assert mo["yaw_rate_rps"] == pytest.approx(20 * 0.0044, abs=1e-4)
    assert f.status(c.t)["motion"]["yaw_input"] == "gyro+wheels+steer"


def test_controls_on_change_and_heartbeat_body_every_five_seconds():
    c, tap, feed, f = _feeder(V11)
    for i in range(100):                                       # 10 s, nothing changes
        _frames(tap, c.t, brake=False, lamp=0x20)
        f.step(c.t)
        c.tick(0.1)
    assert len(_of(feed, "vehicle")) == 1
    assert 10 <= len(_of(feed, "controls")) <= 11              # once a second
    assert 2 <= len(_of(feed, "body")) <= 3                    # every 5 s
    n = len(_of(feed, "controls"))
    _frames(tap, c.t, brake=True, lamp=0x20)                   # a change goes out in the same cycle
    f.step(c.t)
    assert len(_of(feed, "controls")) == n + 1 and _of(feed, "controls")[-1]["brake"] is True
    nb = len(_of(feed, "body"))
    c.tick(0.1)
    _frames(tap, c.t, brake=True, lamp=0x30)
    f.step(c.t)
    assert len(_of(feed, "body")) == nb + 1 and _of(feed, "body")[-1]["parking_lights"] is True


def test_description_again_after_a_reconnect_and_mode_follows_the_new_hello():
    c, tap, feed, f = _feeder(V11)
    _frames(tap, c.t)
    f.step(c.t)
    assert len(_of(feed, "vehicle")) == 1
    feed.connects += 1                                         # mapd restarted: a new connection
    c.tick(0.1)
    _frames(tap, c.t)
    f.step(c.t)
    assert len(_of(feed, "vehicle")) == 2 and len(_of(feed, "controls")) == 2
    feed.connects += 1
    feed.accepts = V1                                          # ... and now an older one answers
    n = len(feed.msgs)
    c.tick(0.1)
    _frames(tap, c.t)
    f.step(c.t)
    assert [m["type"] for m in feed.msgs[n:]] == ["motion"] and "quality" not in feed.msgs[-1]
    feed.accepts = None                                        # no hello reply yet on a fresh connection: v1
    c.tick(0.1)
    _frames(tap, c.t)
    f.step(c.t)
    assert feed.msgs[-1]["type"] == "motion" and f.vehicle_api() is False


# ── hooks for what the car cannot offer yet ──────────────────────────────────

def test_turn_signal_hook_goes_out_in_controls():
    c, tap, feed, f = _feeder(V11)
    _frames(tap, c.t)
    tap.set_turn_signal("left", c.t)
    f.step(c.t)
    ctl = _of(feed, "controls")[-1]
    assert ctl["turn_signal"] == "left" and "turn_signal" not in ctl.get("quality", {})
    assert "turn_signal" not in feed.msgs[0]
    c.tick(2.0)                                                # not repeated by the caller: unknown, not "off"
    _frames(tap, c.t)
    f.step(c.t)
    assert "turn_signal" not in _of(feed, "controls")[-1]
    # against an older roadsense the v1 place (the motion message) is kept
    c2, tap2, feed2, f2 = _feeder(V1)
    _frames(tap2, c2.t)
    tap2.set_turn_signal("right", c2.t)
    f2.step(c2.t)
    assert feed2.msgs[0]["turn_signal"] == "right"


def test_range_sensor_hook():
    c, tap, feed, f = _feeder(V11)
    _frames(tap, c.t)
    f.step(c.t)
    f.set_sensors([{"id": "front_radar", "kind": "radar", "x_m": 3.6, "y_m": 0.0, "z_m": 0.5}])
    f.submit_objects("front_radar", [{"id": 1, "range_m": 31.0, "azimuth_deg": -0.5, "range_rate_mps": -1.2}],
                     kind="radar", t_mono=c.t, tracked=True)
    f.submit_objects("front_radar", [], t_mono=c.t + 0.05)
    c.tick(0.1)
    _frames(tap, c.t)
    f.step(c.t)
    veh = _of(feed, "vehicle")
    assert len(veh) == 2 and veh[-1]["sensors"][0]["id"] == "front_radar"
    objs = _of(feed, "objects")
    assert [(o["sensor"], len(o["objects"])) for o in objs] == [("front_radar", 1), ("front_radar", 0)]
    assert objs[0]["kind"] == "radar" and objs[0]["tracked"] is True and objs[0]["ts_mono_ns"] == int((c.t - 0.1) * 1e9)
    assert f.status(c.t)["objects_sent"] == 2
    # an older roadsense: scans are dropped, not queued for ever
    c2, tap2, feed2, f2 = _feeder(V1)
    f2.submit_objects("front_radar", [])
    _frames(tap2, c2.t)
    f2.step(c2.t)
    assert not _of(feed2, "objects") and not f2._objects


def test_description_and_quality_tables_are_consistent():
    d = vehicle_description(PositionFeederConfig(camera_mount=(1.9, 0.0, 1.25)))
    assert d["camera"] == {"x_m": 1.9, "y_m": 0.0, "z_m": 1.25} and d["type"] == "vehicle"
    sent = {s for group in ("motion", "controls", "body") for s in PROVIDES[group]}
    assert sent - {"yaw_rate_rps"} <= set(SIGNAL_QUALITY)
    assert set(SIGNAL_QUALITY.values()) <= {"good", "uncalibrated", "unverified"}


def test_camera_mount_env(monkeypatch):
    from cyberpunk_computer.backend.__main__ import _build_position
    monkeypatch.setenv("BACKEND_POSITION_FEEDER", "1")
    cfg = _build_position()
    assert cfg.vehicle_api is True and cfg.camera_mount is None
    monkeypatch.setenv("BACKEND_POSITION_CAMERA_MOUNT", "1.9, 0, 1.25")
    monkeypatch.setenv("BACKEND_POSITION_VEHICLE_API", "0")
    cfg = _build_position()
    assert cfg.vehicle_api is False and cfg.camera_mount == (1.9, 0.0, 1.25)
    monkeypatch.setenv("BACKEND_POSITION_CAMERA_MOUNT", "front")
    assert _build_position().camera_mount is None


# ── end to end: the real roadsense (mapd + api) ──────────────────────────────

def test_vehicle_signals_reach_roadsense(stack):
    """The feeder against the roadsense checkout: mapd's hello offers the vehicle
    API, the car's signals appear on roadsense's `vehicle` topic with their
    quality and source, and the brake reaches the lead message."""
    import threading
    import time

    if stack.api_port is None:
        pytest.skip("no Python with aiohttp for the roadsense api")
    cfg = PositionFeederConfig(enabled=True, source="none", rundir=stack.rundir, motion_hz=20.0, retry_s=0.2)
    tap = CanMotionTap()
    feeder = PositionFeeder(cfg, NoGnssSource(), tap=tap)
    stop = threading.Event()

    def can():
        while not stop.is_set():
            _frames(tap, time.monotonic(), kmh=36.0, brake=True, pedal=40, main=True, cruise=True, lamp=0x20)
            tap.set_state_value("cruise_set_speed_mps", 20.0)
            tap.set_state_value("outside_temp_c", 6.5)
            stop.wait(0.014)

    th = threading.Thread(target=can, daemon=True)
    th.start()
    feeder.set_active(True)
    feeder.start()
    try:
        deadline = time.monotonic() + 15
        v = None
        while time.monotonic() < deadline:
            v = stack.get("/v1/vehicle")
            if v["controls"]["brake"] is True and v["body"]["low_beam"] is True and v["description"].get("name"):
                break
            time.sleep(0.2)
        st = feeder.status()
        assert st["vehicle_api"] is True and "controls" in st["mapd_accepts"], st
        assert st["errors"] == 0, st["last_error"]
        assert v["present"] is True
        assert v["feeders"][0]["name"] == "prius-backend" and "brake" in v["feeders"][0]["provides"]["controls"]
        assert (v["description"]["name"], v["description"]["sources"]["track_m"]) == ("Toyota Prius NHW20", "feeder")
        mo = v["motion"]
        assert mo["speed_mps"] == pytest.approx(10.0) and mo["gear"] == "D" and mo["yaw_source"] == "wheel_speeds"
        assert mo["steering_wheel_deg"] == 27.0 and mo["accel_long_mps2"] == pytest.approx(0.22)
        assert v["signals"]["yaw_rate_rps"]["quality"] == "unverified"      # shown and calibrated, not used
        assert v["signals"]["speed_mps"] == {"age_s": pytest.approx(0.0, abs=0.5), "quality": "good", "source": "prius-can"}
        ctl = v["controls"]
        assert (ctl["brake"], ctl["accelerator"], ctl["cruise"], ctl["cruise_set_speed_mps"]) == (True, 0.2, "active", 20.0)
        assert v["signals"]["accelerator"]["quality"] == "unverified"
        assert (v["body"]["low_beam"], v["body"]["door_open"], v["body"]["outside_temp_c"]) == (True, False, 6.5)
        assert v["calibration"]["yaw_rate"]["bias_n"] == 0                   # moving: no standstill yet
        m = stack.latest_map()
        assert m["input"]["motion"]["tag"] == "prius-can" and m["input"]["errors"] == 0
        deadline = time.monotonic() + 8
        ld = None
        while time.monotonic() < deadline:
            ld = stack.get("/v1/lead")
            if ld["ego"]["brake"] is True and ld["ego"]["source"] == "vehicle":
                break
            time.sleep(0.2)
        assert ld["ego"]["brake"] is True and ld["source"] == "vision+ego"
    finally:
        stop.set()
        feeder.stop()
        th.join(2)


from tests.test_position_mapd_e2e import HAVE_REPO, PACK, stack  # noqa: E402,F401  (the module's roadsense stack fixture)

test_vehicle_signals_reach_roadsense = pytest.mark.skipif(
    not (HAVE_REPO and PACK), reason="roadsense checkout or map pack absent")(test_vehicle_signals_reach_roadsense)
