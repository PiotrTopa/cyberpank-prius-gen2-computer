"""ClimateRemote: press/release timing, serialisation, closed-loop setpoint,
and the command path API -> store -> egress -> gateway payloads."""

import pytest

from cyberpunk_computer.comm.avc import (
    AvcFrame, ClimateRemote, ManualScheduler, CLIMATE_BUTTONS, AvcDecoder,
)
from cyberpunk_computer.api.commands import build_command, CommandError, COMMANDS

dec = AvcDecoder()


class Gw:
    """Fake gateway + climate ECU: records frames, answers temp presses."""

    def __init__(self, setpoint_nn=0x06):
        self.sched = ManualScheduler(start=0.0)
        self.sent = []
        self.remote = ClimateRemote(self.send, self.sched)
        self.nn = setpoint_nn
        self.ecu = True
        self.status(0xC3, 0x20)
        self.setpoint()

    def send(self, dev, payload):
        self.sent.append((self.sched.now(), dev, payload))
        data = [int(x, 16) for x in payload["d"]]
        if self.ecu and data[4:] == [0x80, 0, 0, 0, 0, 0]:
            self.sched.call_later(0.08, lambda: self.bump(+1))
        if self.ecu and data[4:] == [0x40, 0, 0, 0, 0, 0]:
            self.sched.call_later(0.08, lambda: self.bump(-1))
        return True

    def bump(self, d):
        self.nn += d
        self.setpoint()

    def setpoint(self):
        self.remote.on_update(dec.decode(AvcFrame.make(0x1C6, 0x110, f"00 E0 5D F5 {self.nn:02X} 38")))

    def status(self, b1, b6, b2=0):
        self.remote.on_update(dec.decode(AvcFrame.make(
            0x1C6, 0x110, f"00 E0 5D F3 {b1:02X} {b2:02X} 00 00 00 {b6:02X}")))

    def frames(self):
        return [(round(t, 3), " ".join(p["d"])) for t, dev, p in self.sent]


def test_press_then_release_100ms_and_serialised():
    gw = Gw()
    gw.remote.press("fan_5")
    gw.remote.press("recirc")
    gw.sched.advance(2.0)
    f = gw.frames()
    assert f == [
        (0.0, "00 25 E0 84 00 00 00 00 08 00"),
        (0.1, "00 25 E0 84 00 00 00 00 00 00"),
        (0.45, "00 25 E0 84 02 00 00 00 00 00"),
        (0.55, "00 25 E0 84 00 00 00 00 00 00"),
    ]
    assert all(p["m"] == "110" and p["s"] == "1C6" and p["c"] == 15 and "b" not in p
               for _, dev, p in gw.sent)
    assert all(dev == 2 for _, dev, _ in gw.sent)


def test_every_button_has_a_verified_code():
    gw = Gw()
    for name, code in CLIMATE_BUTTONS.items():
        gw.sent.clear()
        gw.remote.press(name)
        gw.sched.advance(1.0)
        assert gw.frames()[0][1] == "00 25 E0 84 " + " ".join(f"{b:02X}" for b in code)
        # and the decoder reads the frame back as the same button
        assert dec.decode(AvcFrame.make(0x110, 0x1C6, gw.frames()[0][1])).buttons == (name,)
    with pytest.raises(ValueError):
        gw.remote.press("defrost_everything")


def test_set_temperature_closed_loop_18_5_to_20():
    # AVC_MAP: three remote presses took 18.5 -> 20.0 C
    gw = Gw(setpoint_nn=0x06)
    assert gw.remote.set_temperature(20.0)
    gw.sched.advance(5.0)
    presses = [d for _, d in gw.frames() if d != "00 25 E0 84 00 00 00 00 00 00"]
    assert presses == ["00 25 E0 84 80 00 00 00 00 00"] * 3
    assert gw.nn == 0x09                         # 15.5 + 0.5*9 = 20.0
    assert gw.remote.describe()["temp_target"] is None


def test_set_temperature_gives_up_when_ecu_does_not_move():
    gw = Gw(setpoint_nn=0x06)
    gw.ecu = False
    gw.remote.set_temperature(21.0)
    gw.sched.advance(10.0)
    presses = [d for _, d in gw.frames() if d.endswith("80 00 00 00 00 00")]
    assert len(presses) == 1


def test_toggles_follow_status():
    gw = Gw()
    gw.status(0xC3, 0x20)                    # AUTO, A/C on, fresh air
    gw.remote.set_ac(True)                   # already on -> nothing
    gw.remote.set_recirculation(False)       # already fresh -> nothing
    gw.sched.advance(1.0)
    assert gw.frames() == []
    gw.remote.set_ac(False)
    gw.remote.set_ac(False)                  # queued once only
    gw.sched.advance(1.0)
    assert [d for _, d in gw.frames()][::2] == ["00 25 E0 84 00 20 00 00 00 00"]
    gw.sent.clear()
    gw.remote.set_fan(0)                     # fan 0 = climate OFF button
    gw.remote.set_air_direction(3)           # 3 = windscreen + foot
    gw.sched.advance(2.0)
    assert [d for _, d in gw.frames()][::2] == ["00 25 E0 84 04 00 00 00 00 00",
                                                 "00 25 E0 84 00 01 00 00 00 00"]


def test_unknown_status_never_toggles_blind():
    sched = ManualScheduler()
    sent = []
    r = ClimateRemote(lambda dev, p: sent.append(p) or True, sched)
    assert r.set_ac(True) is False
    assert r.set_temperature(21.0) is False
    sched.advance(1.0)
    assert sent == []


def test_command_registry():
    a = build_command("climate_button", {"button": "temp_up", "count": 3})
    assert (a.button, a.count) == ("temp_up", 3)
    assert build_command("climate_rear_defogger", {}).button == "rear_defogger"
    assert build_command("climate_system_off", {}).button == "off"
    assert build_command("climate_fan_7", {}).button == "fan_7"
    with pytest.raises(CommandError):
        build_command("climate_button", {"button": "nope"})
    for name in CLIMATE_BUTTONS:
        alias = "climate_system_off" if name == "off" else f"climate_{name}"
        assert alias in COMMANDS


def test_api_command_reaches_gateway_payloads():
    """REP command -> StoreBridge -> store -> egress -> ClimateRemote -> output port."""
    from cyberpunk_computer.io.factory import create_test_twin
    from cyberpunk_computer.api.bridge import StoreBridge

    twin = create_test_twin()
    sched = ManualScheduler()
    remote = twin.climate_remote
    remote._sched = sched                    # drive timing by hand
    bridge = StoreBridge(twin.store)
    bridge.submit_command("climate_button", {"button": "auto"})
    bridge.drain_commands()
    sched.advance(1.0)
    sent = [c.to_gateway_json() for c in twin.output_port._sent_commands if c.device_id == 2]
    assert sent[0] == {"id": 2, "d": {"m": "110", "s": "1C6", "c": 15,
                                      "d": ["00", "25", "E0", "84", "08", "00", "00", "00", "00", "00"]}}
    assert sent[1]["d"]["d"] == ["00", "25", "E0", "84"] + ["00"] * 6
    twin.avc_scheduler.stop()
