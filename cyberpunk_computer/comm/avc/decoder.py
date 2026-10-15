"""
Table-driven AVC-LAN decoder (gateway fw 3.x).

Source of truth: ``cyberpunk-prius-gateway/captures/2026-10-07/AVC_MAP.md``
(climate, energy monitor, display, touch calibration, buttons, lights) and
``NAV_EMULATION_SPEC.md``. The reference implementation the tables were
checked against is ``gateway-rp2350/tools/avc_dashboard.py``.

Every rule in :data:`RULES` matches (master, slave, data prefix, min length)
and turns the frame into one typed update. Frames that match nothing decode
to ``None``. Nothing in here is a guess: rows that AVC_MAP.md marks as
provisional say so in their comment and are only used for display, never for
transmission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from .frames import AvcFrame, BCAST_FFF, CLIMATE, DISPLAY, NAV
from .touch import TouchCalibration, DEFAULT_CALIBRATION


# ─────────────────────────────────────────────────────────────────────────────
# Typed updates
# ─────────────────────────────────────────────────────────────────────────────

#: AVC_MAP "Climate status" b2 vent codes (b2 = 00 with b1 bit 0x01 = face).
VENT_MODES: Dict[int, str] = {
    0x80: "face_foot",
    0x40: "foot",
    0x20: "windscreen_foot",
}


@dataclass(frozen=True)
class ClimateStatus:
    """``1C6->110 00 E0 5D F3 b1 b2 b3 b4 b5 b6`` (every 5 s + on change)."""
    system_on: bool          # False = climate OFF (fan 0; mark3, climate_off_touch)
    fan: int                 # 0..7 (b6 >> 5)
    auto: bool               # b1 0x80
    fresh_air: bool          # b1 0x40
    recirculation: bool      # b1 0x20
    front_defrost: bool      # b1 0x10 (2026-10-14 on-screen button)
    rear_defogger: bool      # b1 0x08 (drive home 15:29:38-15:29:55)
    ac: bool                 # b1 0x02
    vent: Optional[str]      # face / face_foot / foot / windscreen_foot / None (unknown code)
    button_ack: bool         # b3 0x04: one frame after each accepted press
    raw: Tuple[int, ...]     # b1..b6


@dataclass(frozen=True)
class ClimateSetpoint:
    """``1C6->110 00 E0 5D F5 NN 38``: °C = 15.5 + 0.5·NN; NN = 39 while OFF."""
    celsius: Optional[float]  # None while the climate system is OFF
    raw: int


@dataclass(frozen=True)
class OutsideTemp:
    """``1C6->110 00 E0 5D F7 TT`` (1 Hz, always): °C = TT − 48."""
    celsius: int
    raw: int


@dataclass(frozen=True)
class EnergyFlow:
    """``1C6->110 00 E4 5F B9 F1 F2 00`` — MFD energy monitor arrows.

    Bit model confirmed against the MFD on the 2026-10-14 drive (AVC_MAP.md
    "Energy monitor"). The MFD draws an MG node; there is no direct
    engine→battery arrow.
    """
    engine_to_wheels: bool   # F1 0x40
    engine_to_mg: bool       # F1 0x08 (generator)
    mg_to_wheels: bool       # F2 0x40 (motor driving)
    wheels_to_mg: bool       # F2 0x80 (regen)
    battery_to_mg: bool      # F2 0x10 (discharging)
    mg_to_battery: bool      # F2 0x08 (charging: generator or regen)
    engine_running: bool     # F1 != 0
    f1: int
    f2: int


@dataclass(frozen=True)
class BatteryInfo:
    """``1C6->110 00 E4 5F B8 00 00 XX YY 00``.

    AVC_MAP "Earlier provisional notes": XX & 0x07 + 1 = battery bars
    (user-confirmed 4→3→4→5 on the 2026-10-07 drive home); YY 0x40 = EV mode
    active, 0x80 = "EV cancelled" notice (both user-confirmed live). The
    remaining bits are unknown.
    """
    bars: int
    ev_mode: bool
    ev_cancelled: bool
    raw: Tuple[int, ...]


#: ``110->FFF 12 01 60 XX ...`` active screen codes (user-confirmed).
SCREENS: Dict[int, str] = {
    0x58: "nav",
    0x5F: "energy",
    0x5D: "climate",
    0x5E: "audio",
    0x56: "info",
    0x00: "none",
}


@dataclass(frozen=True)
class DisplayScreen:
    code: int
    name: str


#: ``110->178 00 25 58 84 KK 00`` hardware buttons to the nav (00 = release).
NAV_BUTTONS: Dict[int, str] = {0x01: "menu", 0x04: "dest", 0x02: "map"}

#: ``110->178 00 25 58 80 KK 00 00`` info-screen soft buttons drawn by the nav.
INFO_BUTTONS: Dict[int, str] = {0x01: "maintenance", 0x08: "calendar", 0x40: "language"}


@dataclass(frozen=True)
class NavButton:
    """Hardware button (menu/dest/map) or info soft button.

    ``key`` is None for the release frame (``KK = 00``) of the hardware keys
    and for codes that are not in the map.
    """
    key: Optional[str]
    code: int
    pressed: bool
    soft: bool = False       # True for 25 58 80 (info soft buttons; no release frame seen)


@dataclass(frozen=True)
class Touch:
    """``110->178 00 21 24 78 X Y X Y`` (00 00 00 00 = release)."""
    pressed: bool
    raw_x: int
    raw_y: int
    x: int                   # calibrated 0..479 (0 on release, see TouchTracker)
    y: int                   # calibrated 0..239


@dataclass(frozen=True)
class Lights:
    """``110->FFF 01 01 5B 40`` lights on (display dims) / ``01 01 5A 80`` lights off.

    One event for parking→low→high and one at off (mark9, map2). Fog lights
    and indicators send nothing.
    """
    on: bool


#: Climate touch buttons ``110->1C6 00 25 E0 84 B1..B6`` (AVC_MAP climate table).
CLIMATE_BUTTONS: Dict[str, Tuple[int, int, int, int, int, int]] = {
    "auto":            (0x08, 0, 0, 0, 0, 0),
    "off":             (0x04, 0, 0, 0, 0, 0),
    "recirc":          (0x02, 0, 0, 0, 0, 0),
    "front_defrost":   (0x01, 0, 0, 0, 0, 0),
    "temp_up":         (0x80, 0, 0, 0, 0, 0),
    "temp_down":       (0x40, 0, 0, 0, 0, 0),
    "vent_windscreen_foot": (0, 0x01, 0, 0, 0, 0),
    "vent_foot":       (0, 0x02, 0, 0, 0, 0),
    "vent_face_foot":  (0, 0x04, 0, 0, 0, 0),
    "vent_face":       (0, 0x08, 0, 0, 0, 0),
    "ac":              (0, 0x20, 0, 0, 0, 0),
    "rear_defogger":   (0, 0x80, 0, 0, 0, 0),
    "fan_1":           (0, 0, 0x80, 0, 0, 0),
    "fan_2":           (0, 0, 0x40, 0, 0, 0),
    "fan_3":           (0, 0, 0x20, 0, 0, 0),
    "fan_4":           (0, 0, 0x10, 0, 0, 0),
    "fan_5":           (0, 0, 0, 0, 0x08, 0),
    "fan_6":           (0, 0, 0, 0, 0x04, 0),
    "fan_7":           (0, 0, 0x08, 0, 0, 0),
}


@dataclass(frozen=True)
class ClimateButtonFrame:
    """A climate touch-button frame on the bus (the display's own presses, or
    our echoes). ``buttons`` is empty for the all-00 release."""
    buttons: Tuple[str, ...]
    raw: Tuple[int, ...]
    echo: bool


@dataclass(frozen=True)
class NavLink:
    """Display↔nav dialogue frames the backend's emulator cares about.

    ``kind``: ``roll_call`` (12 01 00), ``screen_request`` (12 58 42 02 01),
    ``screen_release`` (12 58 43 02 00), ``ping`` (12 01 20 XX).
    """
    kind: str
    raw: Tuple[int, ...]


AvcUpdate = Union[ClimateStatus, ClimateSetpoint, OutsideTemp, EnergyFlow,
                  BatteryInfo, DisplayScreen, NavButton, Touch, Lights,
                  ClimateButtonFrame, NavLink]


# ─────────────────────────────────────────────────────────────────────────────
# Parsers
# ─────────────────────────────────────────────────────────────────────────────

def _climate_status(f: AvcFrame, _cal: TouchCalibration) -> ClimateStatus:
    b1, b2, b3, _b4, _b5, b6 = f.data[4:10]
    fan = b6 >> 5
    if b2 == 0 and b1 & 0x01:
        vent: Optional[str] = "face"
    else:
        vent = VENT_MODES.get(b2)
    return ClimateStatus(
        system_on=fan != 0,
        fan=fan,
        auto=bool(b1 & 0x80),
        fresh_air=bool(b1 & 0x40),
        recirculation=bool(b1 & 0x20),
        front_defrost=bool(b1 & 0x10),
        rear_defogger=bool(b1 & 0x08),
        ac=bool(b1 & 0x02),
        vent=vent,
        button_ack=bool(b3 & 0x04),
        raw=tuple(f.data[4:10]),
    )


def _climate_setpoint(f: AvcFrame, _cal) -> ClimateSetpoint:
    nn = f.data[4]
    return ClimateSetpoint(celsius=None if nn == 0x39 else 15.5 + 0.5 * nn, raw=nn)


def _outside_temp(f: AvcFrame, _cal) -> OutsideTemp:
    return OutsideTemp(celsius=f.data[4] - 48, raw=f.data[4])


def _energy(f: AvcFrame, _cal) -> EnergyFlow:
    f1, f2 = f.data[4], f.data[5]
    return EnergyFlow(
        engine_to_wheels=bool(f1 & 0x40),
        engine_to_mg=bool(f1 & 0x08),
        mg_to_wheels=bool(f2 & 0x40),
        wheels_to_mg=bool(f2 & 0x80),
        battery_to_mg=bool(f2 & 0x10),
        mg_to_battery=bool(f2 & 0x08),
        engine_running=f1 != 0,
        f1=f1,
        f2=f2,
    )


def _battery(f: AvcFrame, _cal) -> BatteryInfo:
    xx, yy = f.data[6], f.data[7]
    return BatteryInfo(bars=(xx & 0x07) + 1, ev_mode=bool(yy & 0x40),
                       ev_cancelled=bool(yy & 0x80), raw=tuple(f.data[4:]))


def _screen(f: AvcFrame, _cal) -> DisplayScreen:
    code = f.data[3]
    return DisplayScreen(code=code, name=SCREENS.get(code, f"{code:02X}"))


def _nav_button(f: AvcFrame, _cal) -> NavButton:
    kk = f.data[4]
    return NavButton(key=NAV_BUTTONS.get(kk), code=kk, pressed=kk != 0)


def _info_button(f: AvcFrame, _cal) -> NavButton:
    kk = f.data[4]
    return NavButton(key=INFO_BUTTONS.get(kk), code=kk, pressed=kk != 0, soft=True)


def _touch(f: AvcFrame, cal: TouchCalibration) -> Touch:
    rx, ry = f.data[4], f.data[5]
    if (rx, ry) == (0, 0) and f.data[6:8] == (0, 0):
        return Touch(pressed=False, raw_x=0, raw_y=0, x=0, y=0)
    x, y = cal.to_screen(rx, ry)
    return Touch(pressed=True, raw_x=rx, raw_y=ry, x=x, y=y)


def _lights_on(_f, _cal) -> Lights:
    return Lights(on=True)


def _lights_off(_f, _cal) -> Lights:
    return Lights(on=False)


def _climate_button(f: AvcFrame, _cal) -> ClimateButtonFrame:
    bytes6 = f.data[4:10]
    names = tuple(
        name for name, code in CLIMATE_BUTTONS.items()
        if any(code[i] and (bytes6[i] & code[i]) == code[i] for i in range(6))
    )
    return ClimateButtonFrame(buttons=names, raw=tuple(bytes6), echo=f.echo)


def _nav_link(kind: str) -> Callable[[AvcFrame, TouchCalibration], NavLink]:
    def parse(f: AvcFrame, _cal) -> NavLink:
        return NavLink(kind=kind, raw=tuple(f.data))
    return parse


ANY = -1


@dataclass(frozen=True)
class Rule:
    name: str
    master: int
    slave: int                 # ANY = any slave
    prefix: Tuple[int, ...]
    min_len: int
    parse: Callable[[AvcFrame, TouchCalibration], AvcUpdate]
    exact: bool = False        # data must equal prefix exactly


def _p(s: str) -> Tuple[int, ...]:
    return tuple(int(x, 16) for x in s.split())


#: The decoding table. Order matters only for overlapping prefixes (none today).
RULES: Tuple[Rule, ...] = (
    Rule("climate_status", CLIMATE, DISPLAY, _p("00 E0 5D F3"), 10, _climate_status),
    Rule("climate_setpoint", CLIMATE, DISPLAY, _p("00 E0 5D F5"), 5, _climate_setpoint),
    Rule("outside_temp", CLIMATE, DISPLAY, _p("00 E0 5D F7"), 5, _outside_temp),
    Rule("energy_flow", CLIMATE, DISPLAY, _p("00 E4 5F B9"), 6, _energy),
    Rule("battery_info", CLIMATE, DISPLAY, _p("00 E4 5F B8"), 8, _battery),
    Rule("display_screen", DISPLAY, ANY, _p("12 01 60"), 4, _screen),
    Rule("nav_button", DISPLAY, NAV, _p("00 25 58 84"), 5, _nav_button),
    Rule("info_button", DISPLAY, NAV, _p("00 25 58 80"), 5, _info_button),
    Rule("touch", DISPLAY, NAV, _p("00 21 24 78"), 8, _touch),
    Rule("lights_on", DISPLAY, ANY, _p("01 01 5B 40"), 4, _lights_on, exact=True),
    Rule("lights_off", DISPLAY, ANY, _p("01 01 5A 80"), 4, _lights_off, exact=True),
    Rule("climate_button", DISPLAY, CLIMATE, _p("00 25 E0 84"), 10, _climate_button),
    Rule("roll_call", DISPLAY, ANY, _p("12 01 00"), 3, _nav_link("roll_call"), exact=True),
    Rule("screen_request", DISPLAY, NAV, _p("00 12 58 42 02 01"), 6, _nav_link("screen_request"), exact=True),
    Rule("screen_release", DISPLAY, NAV, _p("00 12 58 43 02 00"), 6, _nav_link("screen_release"), exact=True),
    Rule("ping", DISPLAY, ANY, _p("12 01 20"), 4, _nav_link("ping")),
)


def match_rule(frame: AvcFrame) -> Optional[Rule]:
    for rule in RULES:
        if frame.master != rule.master:
            continue
        if rule.slave != ANY and frame.slave != rule.slave:
            continue
        if len(frame.data) < rule.min_len:
            continue
        if rule.exact:
            if frame.data != rule.prefix:
                continue
        elif not frame.startswith(rule.prefix):
            continue
        return rule
    return None


class AvcDecoder:
    """Stateless frame → typed update decoder (one update per frame)."""

    def __init__(self, calibration: TouchCalibration = DEFAULT_CALIBRATION) -> None:
        self.calibration = calibration
        self.counts: Dict[str, int] = {}

    def decode(self, frame: AvcFrame) -> Optional[AvcUpdate]:
        rule = match_rule(frame)
        if rule is None:
            return None
        self.counts[rule.name] = self.counts.get(rule.name, 0) + 1
        return rule.parse(frame, self.calibration)

    def decode_gateway(self, d: dict) -> Optional[AvcUpdate]:
        frame = AvcFrame.from_gateway(d)
        return self.decode(frame) if frame is not None else None
