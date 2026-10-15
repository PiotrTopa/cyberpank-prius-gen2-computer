"""AVC ingest -> store: AVC priority over CAN/OBD, touch/button event contract,
and gateway-v3 capture replay through the whole ingress."""

import pytest

from cyberpunk_computer.io.avc_ingest import AvcIngest
from cyberpunk_computer.io.file_io import FileInputPort
from cyberpunk_computer.io.ingress import IngressController
from cyberpunk_computer.io.mock_io import MockInputPort
from cyberpunk_computer.io.ports import RawMessage
from cyberpunk_computer.state.store import Store
from avc_captures import capture, hms


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def avc(m, s, data):
    return {"m": m, "s": s, "c": 15, "d": data.split()}


def make_ingress():
    store = Store()
    ing = IngressController(store, MockInputPort())
    clock = Clock()
    ing._avc = AvcIngest(clock=clock)
    events = []
    ing.avc.set_event_sink(lambda name, data: events.append((name, data)))
    return store, ing, clock, events


def feed_avc(ing, d):
    ing._process_message(RawMessage(device_id=2, timestamp=0, data=d))


class FakeCan:
    def __init__(self, msg_type, values):
        self.msg_type = msg_type
        self.values = values
        self.data = b""


def test_outside_temp_avc_wins_while_fresh_then_obd():
    from cyberpunk_computer.comm.can_decoder import CANMessageType
    store, ing, clock, _ = make_ingress()
    feed_avc(ing, avc("1C6", "110", "00 E0 5D F7 46"))          # 22 C
    assert store.state.climate.outside_temp == 22.0
    assert store.state.climate.outside_temp_source == "avc"
    # OBD ambient arrives while AVC is fresh -> ambient_air_temp only
    ing._dispatch_actions(ing._can_message_to_actions(
        FakeCan(CANMessageType.SOLICITED_ENGINE, {"ambient_temp": 19})))
    assert store.state.climate.outside_temp == 22.0
    assert store.state.vehicle.ambient_air_temp == 19.0
    clock.t += 16.0                                             # AVC stale
    ing._dispatch_actions(ing._can_message_to_actions(
        FakeCan(CANMessageType.SOLICITED_ENGINE, {"ambient_temp": 19})))
    assert store.state.climate.outside_temp == 19.0
    assert store.state.climate.outside_temp_source == "obd"


def test_energy_and_engine_from_avc_override_can():
    from cyberpunk_computer.comm.can_decoder import CANMessageType
    store, ing, clock, _ = make_ingress()
    feed_avc(ing, avc("1C6", "110", "00 E4 5F B9 00 AC 00"))   # regen
    e = store.state.energy
    assert (e.flow_wheels_to_mg, e.flow_mg_to_battery, e.regen_active, e.charging) == (True, True, True, True)
    assert e.flow_source == "avc" and not store.state.vehicle.ice_running
    # CAN says ICE running (rpm) while AVC is fresh -> ignored
    ing._dispatch_actions(ing._can_message_to_actions(
        FakeCan(CANMessageType.SOLICITED_ENGINE, {"rpm": 1200})))
    assert not store.state.vehicle.ice_running
    feed_avc(ing, avc("1C6", "110", "00 E4 5F B9 6C 0C 00"))
    e = store.state.energy
    assert store.state.vehicle.ice_running
    assert (e.flow_engine_to_wheels, e.flow_engine_to_mg, e.flow_mg_to_battery, e.flow_mg_to_wheels) == \
        (True, True, True, False)
    assert e.flow_engine_to_battery            # legacy flag: engine charging via MG
    clock.t += 20.0
    ing._dispatch_actions(ing._can_message_to_actions(
        FakeCan(CANMessageType.ENGINE_STATUS, {"ice_running": False})))
    assert not store.state.vehicle.ice_running  # CAN takes over when AVC is stale


def test_climate_status_and_setpoint_into_state():
    store, ing, clock, _ = make_ingress()
    feed_avc(ing, avc("1C6", "110", "00 E0 5D F3 52 00 00 00 00 40"))
    feed_avc(ing, avc("1C6", "110", "00 E0 5D F5 09 38"))
    c = store.state.climate
    assert (c.fan_speed, c.ac_on, c.auto_mode, c.defrost, c.target_temp) == (2, True, False, True, 20.0)
    assert c.mode.name == "DEFROST" and c.system_on and c.status_source == "avc"
    feed_avc(ing, avc("1C6", "110", "00 E0 5D F3 41 00 04 00 00 00"))
    feed_avc(ing, avc("1C6", "110", "00 E0 5D F5 39 38"))
    c = store.state.climate
    assert c.mode.name == "OFF" and c.system_on is False and c.target_temp == 20.0


def test_touch_and_button_event_contract():
    store, ing, clock, events = make_ingress()
    feed_avc(ing, avc("110", "178", "00 21 24 78 00 00 00 00"))  # stray release: dropped
    feed_avc(ing, avc("110", "178", "00 21 24 78 04 FF 04 FF"))
    clock.t += 0.08
    feed_avc(ing, avc("110", "178", "00 21 24 78 F9 09 F9 09"))
    feed_avc(ing, avc("110", "178", "00 21 24 78 00 00 00 00"))
    feed_avc(ing, avc("110", "178", "00 25 58 84 01 00"))
    feed_avc(ing, avc("110", "178", "00 25 58 84 00 00"))
    feed_avc(ing, avc("110", "178", "00 25 58 80 08 00 00"))
    touch = [d for n, d in events if n == "touch"]
    assert [(t["x"], t["y"], t["pressed"], t["raw_x"], t["raw_y"]) for t in touch] == [
        (0, 0, True, 4, 255), (479, 239, True, 249, 9), (479, 239, False, 249, 9)]
    assert all(isinstance(t["t"], float) and set(t) == {"x", "y", "pressed", "raw_x", "raw_y", "t"}
               for t in touch)
    btn = [(d["key"], d["pressed"]) for n, d in events if n == "button"]
    assert btn == [("menu", True), ("menu", False), ("calendar", True), ("calendar", False)]
    i = store.state.input
    assert (i.last_touch_x, i.last_touch_y, i.last_touch_type) == (479, 239, "release")
    assert i.last_button_name == "calendar" and store.state.avc.touch_pressed is False


def test_screen_and_lights_into_avc_state():
    store, ing, clock, _ = make_ingress()
    feed_avc(ing, avc("110", "FFF", "12 01 60 58 01 00 05"))
    feed_avc(ing, avc("110", "FFF", "01 01 5B 40"))
    a = store.state.avc
    assert (a.screen, a.screen_name, a.nav_screen_active, a.lights_on) == ("58", "nav", True, True)


def test_file_input_port_reads_v3_capture():
    path = capture("2026-10-14/emu11_cold_lang02.ndjson")
    port = FileInputPort(str(path), speed=0, realtime=False)
    assert port.load()
    raws = [e.raw_dict for e in port._entries]
    assert all("id" in r for r in raws)           # tx/note/header lines skipped
    assert sum(1 for r in raws if r["id"] == 2) > 500
    assert 150 < port.duration < 200              # paced by host time ht (15:09:03-15:11:47)


def test_replay_drive2_through_ingress():
    """Whole drive (40 min, AVC only) through the real ingress + store."""
    path = capture("2026-10-14/drive2_emu_avc.ndjson")
    port = FileInputPort(str(path), speed=0, realtime=False)
    port.start()
    store = Store()
    ing = IngressController(store, port)
    seen_temps, seen_flow = set(), set()
    while True:
        n = ing.update(max_messages=5000)
        st = store.state
        if st.climate.outside_temp is not None:
            seen_temps.add(st.climate.outside_temp)
        seen_flow.add((st.energy.flow_engine_to_wheels, st.energy.flow_battery_to_mg,
                       st.energy.flow_wheels_to_mg))
        if n == 0:
            break
    st = store.state
    assert st.climate.outside_temp_source == "avc" and st.climate.outside_temp in (13.0, 14.0, 15.0)
    assert st.climate.status_source == "avc" and st.climate.target_temp == 20.0
    assert st.energy.flow_source == "avc"
    assert st.avc.screen == "5F"
    assert ing.stats.avc_messages > 30000
