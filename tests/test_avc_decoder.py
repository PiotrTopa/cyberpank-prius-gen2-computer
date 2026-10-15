"""Unit tests of the table-driven AVC-LAN decoder against AVC_MAP.md rows."""

import pytest

from cyberpunk_computer.comm.avc import (
    AvcDecoder, AvcFrame, CLIMATE, DISPLAY, NAV, BCAST_FFF,
    ClimateButtonFrame, ClimateSetpoint, ClimateStatus, DisplayScreen,
    EnergyFlow, Lights, NavButton, OutsideTemp, Touch, BatteryInfo,
    DEFAULT_CALIBRATION, tx_payload,
)

dec = AvcDecoder()


def d(m, s, data):
    return dec.decode(AvcFrame.make(m, s, data))


def test_from_gateway_parses_flags_and_ignores_non_frames():
    f = AvcFrame.from_gateway({"m": "110", "s": "FFF", "c": 15, "d": ["12", "01", "00"], "b": 1})
    assert (f.master, f.slave, f.data, f.broadcast) == (0x110, 0xFFF, (0x12, 0x01, 0x00), True)
    assert AvcFrame.from_gateway({"auto": 0, "on": True}) is None
    assert dec.decode_gateway({"auto": 1, "on": True}) is None


def test_climate_status_auto():
    u = d(CLIMATE, DISPLAY, "00 E0 5D F3 C3 00 00 00 00 20")
    assert isinstance(u, ClimateStatus)
    assert u.system_on and u.auto and u.fresh_air and u.ac and u.fan == 1
    assert u.vent == "face" and not u.recirculation and not u.button_ack


def test_climate_status_flags():
    # recirc + A/C + windscreen+foot, fan 7 (map3 mark7)
    u = d(CLIMATE, DISPLAY, "00 E0 5D F3 22 20 00 00 00 E0")
    assert u.recirculation and u.ac and not u.auto and u.fan == 7 and u.vent == "windscreen_foot"
    # rear window heater (drive home)
    assert d(CLIMATE, DISPLAY, "00 E0 5D F3 AB 00 00 00 00 20").rear_defogger
    # front defrost (2026-10-14): AUTO off, A/C on, fan 2
    u = d(CLIMATE, DISPLAY, "00 E0 5D F3 52 00 00 00 00 40")
    assert u.front_defrost and u.ac and not u.auto and u.fan == 2
    # OFF (climate_off_touch)
    u = d(CLIMATE, DISPLAY, "00 E0 5D F3 41 00 04 00 00 00")
    assert not u.system_on and u.fan == 0 and u.button_ack
    # vent codes
    assert d(CLIMATE, DISPLAY, "00 E0 5D F3 22 80 00 00 00 20").vent == "face_foot"
    assert d(CLIMATE, DISPLAY, "00 E0 5D F3 22 40 00 00 00 20").vent == "foot"


@pytest.mark.parametrize("nn,c", [(0x07, 19.0), (0x0A, 20.5), (0x0B, 21.0), (0x0F, 23.0), (0x39, None)])
def test_setpoint(nn, c):
    u = d(CLIMATE, DISPLAY, f"00 E0 5D F5 {nn:02X} 38")
    assert isinstance(u, ClimateSetpoint) and u.celsius == c


@pytest.mark.parametrize("tt,c", [(0x44, 20), (0x45, 21), (0x46, 22), (0x30, 0), (0x2B, -5)])
def test_outside_temp(tt, c):
    u = d(CLIMATE, DISPLAY, f"00 E0 5D F7 {tt:02X}")
    assert isinstance(u, OutsideTemp) and u.celsius == c


# AVC_MAP "Energy monitor" table: F1 F2 -> arrows the user saw on the MFD
ENERGY_TABLE = [
    ("00 00", set()),
    ("0C 0C", {"engine_to_mg", "mg_to_battery"}),
    ("6C 0C", {"engine_to_wheels", "engine_to_mg", "mg_to_battery"}),
    ("6C 6C", {"engine_to_wheels", "engine_to_mg", "mg_to_wheels", "mg_to_battery"}),
    ("6C 60", {"engine_to_wheels", "engine_to_mg", "mg_to_wheels"}),
    ("6C 74", {"engine_to_wheels", "engine_to_mg", "battery_to_mg", "mg_to_wheels"}),
    ("00 74", {"battery_to_mg", "mg_to_wheels"}),
    ("00 AC", {"wheels_to_mg", "mg_to_battery"}),
]
ARROWS = ("engine_to_wheels", "engine_to_mg", "mg_to_wheels", "wheels_to_mg",
          "battery_to_mg", "mg_to_battery")


@pytest.mark.parametrize("f1f2,arrows", ENERGY_TABLE)
def test_energy_table(f1f2, arrows):
    u = d(CLIMATE, DISPLAY, f"00 E4 5F B9 {f1f2} 00")
    assert isinstance(u, EnergyFlow)
    assert {a for a in ARROWS if getattr(u, a)} == arrows
    assert u.engine_running == (f1f2[:2] != "00")


def test_battery_info():
    u = d(CLIMATE, DISPLAY, "00 E4 5F B8 00 00 83 40 00")
    assert isinstance(u, BatteryInfo) and u.bars == 4 and u.ev_mode and not u.ev_cancelled
    u = d(CLIMATE, DISPLAY, "00 E4 5F B8 00 00 82 C0 00")
    assert u.bars == 3 and u.ev_cancelled


def test_screen_buttons_lights():
    u = d(DISPLAY, BCAST_FFF, "12 01 60 58 01 00 05")
    assert isinstance(u, DisplayScreen) and u.name == "nav" and u.code == 0x58
    assert d(DISPLAY, BCAST_FFF, "12 01 60 5D 05 00 05").name == "climate"
    for code, key in ((0x01, "menu"), (0x04, "dest"), (0x02, "map")):
        u = d(DISPLAY, NAV, f"00 25 58 84 {code:02X} 00")
        assert isinstance(u, NavButton) and u.key == key and u.pressed and not u.soft
    u = d(DISPLAY, NAV, "00 25 58 84 00 00")
    assert not u.pressed and u.key is None
    for code, key in ((0x01, "maintenance"), (0x08, "calendar"), (0x40, "language")):
        u = d(DISPLAY, NAV, f"00 25 58 80 {code:02X} 00 00")
        assert u.key == key and u.soft
    assert d(DISPLAY, BCAST_FFF, "01 01 5B 40") == Lights(on=True)
    assert d(DISPLAY, BCAST_FFF, "01 01 5A 80") == Lights(on=False)
    assert d(DISPLAY, BCAST_FFF, "01 01 5F 09") is None     # unknown, not decoded


@pytest.mark.parametrize("raw,xy", [
    ((4, 255), (0, 0)), ((249, 255), (479, 0)), ((249, 9), (479, 239)), ((4, 9), (0, 239)),
    ((126, 132), (239, 120)),
])
def test_touch_calibration(raw, xy):
    assert DEFAULT_CALIBRATION.to_screen(*raw) == xy


def test_touch_frames():
    u = d(DISPLAY, NAV, "00 21 24 78 F7 11 F7 11")
    assert isinstance(u, Touch) and u.pressed and (u.raw_x, u.raw_y) == (0xF7, 0x11)
    assert u.x > 470 and u.y > 225                      # bottom-right
    u = d(DISPLAY, NAV, "00 21 24 78 00 00 00 00")
    assert not u.pressed


def test_climate_button_frames():
    u = d(DISPLAY, CLIMATE, "00 25 E0 84 80 00 00 00 00 00")
    assert isinstance(u, ClimateButtonFrame) and u.buttons == ("temp_up",)
    assert d(DISPLAY, CLIMATE, "00 25 E0 84 00 00 08 00 00 00").buttons == ("fan_7",)
    assert d(DISPLAY, CLIMATE, "00 25 E0 84 00 00 00 00 04 00").buttons == ("fan_6",)
    assert d(DISPLAY, CLIMATE, "00 25 E0 84 00 00 00 00 00 00").buttons == ()


def test_tx_payload_format():
    assert tx_payload(0x178, 0x1FF, "58 31 F1 02 00", broadcast=True) == {
        "m": "178", "s": "1FF", "c": 15, "d": ["58", "31", "F1", "02", "00"], "b": True}
    assert tx_payload(0x110, 0x1C6, [0, 0x25, 0xE0, 0x84, 8, 0, 0, 0, 0, 0]) == {
        "m": "110", "s": "1C6", "c": 15, "d": ["00", "25", "E0", "84", "08", "00", "00", "00", "00", "00"]}
