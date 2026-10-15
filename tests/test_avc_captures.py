"""Replay the recorded gateway-v3 captures through the decoder and assert what
the user saw in the car (captures/2026-10-07 and 2026-10-14 notes)."""

from collections import Counter

import pytest

from cyberpunk_computer.comm.avc import (
    AvcDecoder, AvcFrame, ClimateButtonFrame, ClimateSetpoint, ClimateStatus,
    DisplayScreen, EnergyFlow, Lights, NavButton, OutsideTemp, Touch,
)
from avc_captures import avc_records, capture, hms, local_epoch


def decoded(path, kind=None):
    dec = AvcDecoder()
    for ht, d in avc_records(path):
        u = dec.decode(AvcFrame.from_gateway(d))
        if u is not None and (kind is None or isinstance(u, kind)):
            yield ht, u


def changes(seq):
    out = []
    for v in seq:
        if not out or out[-1] != v:
            out.append(v)
    return out


# ── outside temperature ───────────────────────────────────────────────────

def test_outside_temp_drive_home_22c():
    # drive_home.notes: 15:32:26 user "4bars on batt out 22C"
    path = capture("2026-10-07/drive_home.ndjson")
    temps = [(ht, u.celsius) for ht, u in decoded(path, OutsideTemp)]
    assert {c for _, c in temps} == {21, 22}
    t_mark = local_epoch("2026-10-07", "15:32:26")
    near = [c for ht, c in temps if abs(ht - t_mark) <= 5]
    assert near and set(near) == {22}


def test_outside_temp_values_other_captures():
    assert {u.celsius for _, u in decoded(capture("2026-10-07/map3_marked.ndjson"), OutsideTemp)} == {20}
    drive2 = Counter(u.celsius for _, u in decoded(capture("2026-10-14/drive2_emu_avc.ndjson"), OutsideTemp))
    assert set(drive2) == {13, 14, 15}
    assert drive2.most_common(1)[0][0] == 14


# ── climate ───────────────────────────────────────────────────────────────

def test_climate_setpoint_touch_sequence():
    """On-screen temp up ×2, down ×1, down held (2 auto-repeat steps):
    19.0 → 19.5 → 20.0 → 19.5 → 19.0 → 18.5. Every change follows a display
    temp_up/temp_down frame within 150 ms."""
    path = capture("2026-10-14/climate_setpoint_touch.ndjson")
    seq = []
    last_btn = None
    for ht, u in decoded(path):
        if isinstance(u, ClimateButtonFrame) and u.buttons:
            last_btn = (ht, u.buttons)
        if isinstance(u, ClimateSetpoint):
            if seq and seq[-1] != u.celsius:
                assert last_btn is not None and ht - last_btn[0] < 0.6
                want = "temp_up" if u.celsius > seq[-1] else "temp_down"
                assert last_btn[1] == (want,)
            seq.append(u.celsius)
    assert changes(seq) == [19.0, 19.5, 20.0, 19.5, 19.0, 18.5]


def test_remote_steps_between_captures():
    """AVC_MAP: three remote temp-up presses took 18.5 → 20.0 °C. The presses
    were sent between the two captures (the capture tool lost the port), so
    only the endpoints are on record: setpoint_touch ends at 18.5, the next
    capture (windshield_touch, 14:40:56) starts at 20.0 = 3 steps of 0.5."""
    a = [u.celsius for _, u in decoded(capture("2026-10-14/climate_setpoint_touch.ndjson"), ClimateSetpoint)]
    b = [u.celsius for _, u in decoded(capture("2026-10-14/climate_windshield_touch.ndjson"), ClimateSetpoint)]
    assert a[-1] == 18.5 and b[0] == 20.0
    assert round((b[0] - a[-1]) / 0.5) == 3


def test_climate_front_defrost_toggle():
    path = capture("2026-10-14/climate_windshield_touch.ndjson")
    states = changes([(u.auto, u.front_defrost, u.ac, u.fan)
                      for _, u in decoded(path, ClimateStatus)])
    # AUTO fan 1 -> defrost (AUTO off, A/C on, fan 2) -> back to AUTO
    assert states == [(True, False, True, 1), (False, True, True, 2), (True, False, True, 1)]
    presses = [u.buttons for _, u in decoded(path, ClimateButtonFrame) if u.buttons]
    assert presses == [("front_defrost",), ("front_defrost",)]


def test_climate_off_and_auto():
    path = capture("2026-10-14/climate_off_touch.ndjson")
    seq = []
    for _, u in decoded(path):
        if isinstance(u, ClimateStatus):
            seq.append(("on" if u.system_on else "OFF", u.fan))
        elif isinstance(u, ClimateSetpoint):
            seq.append(("sp", u.celsius))
        elif isinstance(u, ClimateButtonFrame) and u.buttons:
            seq.append(("btn",) + u.buttons)
    seq = changes(seq)
    i = seq.index(("btn", "off"))
    assert ("OFF", 0) in seq[i:i + 4] and ("sp", None) in seq[i:i + 4]
    j = seq.index(("btn", "auto"))
    assert ("on", 1) in seq[j:j + 4] and ("sp", 20.0) in seq[j:j + 4]   # AUTO restores 20.0


def test_rear_window_heater_drive_home():
    # AVC_MAP: rear window heater on 15:29:38 - 15:29:55 (drive home)
    path = capture("2026-10-07/drive_home.ndjson")
    first = []
    for ht, u in decoded(path, ClimateStatus):
        if u.rear_defogger:
            first.append(ht)
        elif first:
            break
    assert first
    assert hms(first[0]) == "15:29:38" and hms(first[-1]) == "15:29:53"
    # (two unexplained single-frame CB blips follow at 15:40:56 and 15:41:00;
    # not in the drive notes)


# ── energy monitor ────────────────────────────────────────────────────────

def test_energy_states_drive2():
    path = capture("2026-10-14/drive2_emu_avc.ndjson")
    states = Counter((u.f1, u.f2) for _, u in decoded(path, EnergyFlow))
    assert set(states) == {(0x00, 0x00), (0x0C, 0x0C), (0x6C, 0x0C), (0x6C, 0x6C),
                           (0x6C, 0x60), (0x6C, 0x74), (0x00, 0x74), (0x00, 0xAC)}
    # engine-running transitions (start/stop) are many but finite
    eng = changes([u.engine_running for _, u in decoded(path, EnergyFlow)])
    assert eng.count(True) >= 5


# ── display / touch / buttons ─────────────────────────────────────────────

def strokes(path):
    out, cur = [], []
    for ht, u in decoded(path, Touch):
        if u.pressed:
            cur.append((u.x, u.y))
        elif cur:
            out.append((ht, cur))
            cur = []
    return out


def test_touch_corner_strokes_emu8():
    """The first 10 strokes (14:23:41-14:24:53) are the calibration corners;
    each must land within 25 px of a screen corner, all four corners hit."""
    s = strokes(capture("2026-10-14/emu8_release_ack.ndjson"))
    corners = {"tl": (0, 0), "tr": (479, 0), "br": (479, 239), "bl": (0, 239)}
    hit = Counter()
    for _, pts in s[:10]:
        mx = sum(p[0] for p in pts) / len(pts)
        my = sum(p[1] for p in pts) / len(pts)
        name, (cx, cy) = min(corners.items(), key=lambda kv: (kv[1][0] - mx) ** 2 + (kv[1][1] - my) ** 2)
        assert abs(mx - cx) <= 25 and abs(my - cy) <= 25, (mx, my)
        hit[name] += 1
    assert set(hit) == set(corners)
    # 11th stroke: drag left -> right through the middle
    _, drag = s[10]
    assert drag[0][0] <= 2 and drag[-1][0] >= 470 and all(100 <= y <= 140 for _, y in drag)
    # 12th: drag top -> bottom through the middle
    _, drag = s[11]
    assert drag[0][1] <= 5 and drag[-1][1] >= 230 and all(220 <= x <= 260 for x, _ in drag)


def test_screens_and_buttons_emu8():
    path = capture("2026-10-14/emu8_release_ack.ndjson")
    screens = changes([u.name for _, u in decoded(path, DisplayScreen)])
    assert {"energy", "nav", "climate", "info"} <= set(screens)
    btns = [(u.key, u.pressed, u.soft) for _, u in decoded(path, NavButton)]
    assert ("dest", True, False) in btns and (None, False, False) in btns
    assert ("language", True, True) in btns


def test_lights_map3():
    events = [u.on for _, u in decoded(capture("2026-10-07/map3_marked.ndjson"), Lights)]
    assert events == [True, False]
