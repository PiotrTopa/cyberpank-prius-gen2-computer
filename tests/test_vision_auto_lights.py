"""Auto-light model (vision/auto_lights.py): the real 2026-10-19 drive,
synthetic dusk and tunnel sequences, and the hysteresis rules."""

import csv
import os

import pytest

from cyberpunk_computer.vision.auto_lights import (
    AutoLightModel, LightThresholds, main, replay, sun_at, transitions,
)
from cyberpunk_computer.vision.light_features import LightFeatures, chroma

CSV = os.path.join(os.path.dirname(__file__), "fixtures", "dashcam_drive_home_2026-10-19.csv")
GDANSK = sun_at(54.35, 18.65)


def drive_rows():
    with open(CSV, newline="") as f:
        return list(csv.DictReader(f))


def _hms(s):
    h, m, sec = (int(x) for x in s.split(":"))
    return h * 3600 + m * 60 + sec


def annotation_epoch(rows, hms):
    """Laptop time of a driver note → epoch, via the CSV's own clock pairs."""
    r0 = rows[0]
    return int(r0["epoch"]) + _hms(hms) - _hms(r0["iso_time"][11:])


def segment(timeline, t0, t1=float("inf")):
    return [d for d in timeline if t0 <= d.t < t1]


# ── the real drive ────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def drive():
    rows = drive_rows()
    return rows, replay(rows, sun=GDANSK)


def test_drive_one_decision_per_row(drive):
    rows, tl = drive
    assert len(tl) == len(rows) == 174


def test_drive_garage_is_on(drive):
    rows, tl = drive
    garage = segment(tl, 0, annotation_epoch(rows, "15:25:08"))   # "outside"
    assert len(garage) == 13
    assert all(d.lights for d in garage)


def test_drive_off_within_20s_of_leaving_garage_and_stays_off(drive):
    rows, tl = drive
    outside = annotation_epoch(rows, "15:25:08")
    after = segment(tl, outside + 20)
    assert after and not any(d.lights for d in after)
    # exactly one switch over the whole drive: ON (garage) -> off (daylight)
    tr = transitions(tl)
    assert [d.lights for d in tr] == [True, False]
    assert tr[1].reason == "daylight"
    assert tr[1].t <= outside + 20


@pytest.mark.parametrize("start,end", [("15:35:27", "15:39:12"),   # sun straight in
                                       ("15:48:10", "23:59:59")])  # shade
def test_drive_no_flapping(drive, start, end):
    rows, tl = drive
    seg = segment(tl, annotation_epoch(rows, start), annotation_epoch(rows, end))
    assert len(seg) >= 10
    assert not any(d.lights for d in seg)
    assert not any(d.changed for d in seg)


def test_drive_camera_only_same_result():
    rows = drive_rows()
    tl = replay(rows, sun=None)
    assert [d.lights for d in transitions(tl)] == [True, False]


def test_cli_runs(capsys):
    assert main(["--csv", CSV]) == 0
    out = capsys.readouterr().out
    assert "1 switches" in out and "daylight" in out


# ── synthetic helpers ─────────────────────────────────────────────────────

def feat(t, sky_y, chroma_=None, clip=None, u=None, v=None):
    if chroma_ is not None:
        u, v = 128.0 + chroma_, 128.0
    return LightFeatures(t=t, sky_y=sky_y, sky_u=u, sky_v=v,
                         sky_chroma=chroma(u, v), sky_clip=clip, road_y=70.0)


DAY = dict(sky_y=185.0, chroma_=20.0)
DARK = dict(sky_y=60.0, chroma_=2.0)


def run(model, samples, sun=None):
    return [model.update(f, sun(f.t) if callable(sun) else sun, t=f.t) for f in samples]


# ── dusk: sun elevation crossing -6 deg ───────────────────────────────────

def test_dusk_twilight_switches_on_and_ignores_camera():
    m = AutoLightModel()
    # sun falls 0.1 deg per 5 s sample from -5 to -7; camera still says "day"
    samples = [feat(i * 5.0, **DAY) for i in range(21)]
    sun = lambda t: -5.0 - 0.1 * (t / 5.0)
    tl = run(m, samples, sun)
    assert not tl[0].lights and tl[0].reason == "bootstrap-daylight"
    first_on = next(d for d in tl if d.lights)
    assert first_on.sun_elevation < -6.0
    assert first_on.reason == "twilight" and first_on.confidence == 1.0
    on_from = tl.index(first_on)
    assert all(d.lights and d.reason == "twilight" for d in tl[on_from:])
    assert sum(d.changed for d in tl) == 1


def test_dawn_needs_camera_to_switch_off():
    m = AutoLightModel()
    tl = run(m, [feat(0, **DARK)], -8.0)
    assert tl[0].lights
    # sun rises above -6 and the camera sees daylight: off only after 20 s
    t = 5.0
    out = []
    while t <= 60:
        out.append(m.update(feat(t, **DAY), -5.0, t=t))
        t += 5.0
    first_off = next(d for d in out if not d.lights)
    # median flips once >half the 30 s window is daylight, then 20 s persistence
    assert first_off.reason == "daylight"
    assert 20.0 <= first_off.t <= 40.0


# ── a 40 s tunnel in daylight ─────────────────────────────────────────────

def test_tunnel_40s():
    m = AutoLightModel()
    samples = []
    t = 0.0
    while t < 60:
        samples.append(feat(t, **DAY)); t += 5
    tunnel_in = t
    while t < tunnel_in + 40:
        samples.append(feat(t, **DARK)); t += 5
    tunnel_out = t
    while t < tunnel_out + 90:
        samples.append(feat(t, **DAY)); t += 5
    tl = run(m, samples, 30.0)
    on = [d for d in tl if d.lights]
    assert on, "tunnel must switch the lights on"
    assert on[0].reason == "dark-sky"
    # 30 s median needs > half the window dark (15 s), then 5 s persistence
    assert tunnel_in + 15 <= on[0].t <= tunnel_in + 25
    # ... and off again after the exit: median flip + 20 s persistence
    off_again = next(d for d in tl if d.t > on[0].t and not d.lights)
    assert off_again.reason == "daylight"
    assert tunnel_out + 20 <= off_again.t <= tunnel_out + 40
    assert sum(d.changed for d in tl) == 2


# ── hysteresis rules ──────────────────────────────────────────────────────

def test_bootstrap_dead_band_starts_on():
    d = AutoLightModel().update(feat(0, sky_y=135.0, chroma_=10.0))
    assert d.lights and d.reason == "bootstrap-dead-band"


def test_on_needs_both_dark_and_grey():
    m = AutoLightModel()
    m.update(feat(0, **DAY))
    # dark but blue (chroma 9 > 8): not an ON candidate -> dead band hold
    for t in range(5, 120, 5):
        d = m.update(feat(t, sky_y=100.0, chroma_=9.0))
        assert not d.lights
    assert d.reason == "dead-band-hold"


def test_off_by_chroma_alone():
    m = AutoLightModel()
    m.update(feat(0, **DARK))
    tl = [m.update(feat(t, sky_y=130.0, chroma_=15.0)) for t in range(5, 120, 5)]
    assert not tl[-1].lights and tl[-1].reason == "daylight"


def test_on_persistence_5s_and_off_persistence_20s():
    th = LightThresholds(window_s=1.0)          # no median smoothing: raw samples
    m = AutoLightModel(th)
    assert not m.update(feat(0, **DAY)).lights
    assert m.update(feat(1, **DARK)).reason == "pending-on"
    assert not m.update(feat(5.9, **DARK)).lights
    d = m.update(feat(6, **DARK))
    assert d.lights and d.changed and d.reason == "dark-sky"
    assert m.update(feat(7, **DAY)).reason == "pending-off"
    assert m.update(feat(26.9, **DAY)).lights
    d = m.update(feat(27, **DAY))
    assert not d.lights and d.changed


def test_dead_band_resets_persistence():
    th = LightThresholds(window_s=1.0)
    m = AutoLightModel(th)
    m.update(feat(0, **DAY))
    m.update(feat(1, **DARK))                    # pending-on since 1
    m.update(feat(4, sky_y=135.0, chroma_=10.0))  # dead band: timer reset
    assert m.update(feat(6, **DARK)).reason == "pending-on"   # since 6 now
    assert m.update(feat(10, **DARK)).reason == "pending-on"
    assert m.update(feat(11, **DARK)).lights


def test_single_outlier_does_not_flip_with_median():
    m = AutoLightModel()
    tl = run(m, [feat(t, **DAY) for t in range(0, 60, 5)])
    tl += run(m, [feat(60, **DARK)])                       # one dark frame
    tl += run(m, [feat(t, **DAY) for t in range(65, 120, 5)])
    assert not any(d.lights for d in tl)


def test_clip_blocks_on_but_not_off():
    th = LightThresholds(window_s=1.0)
    m = AutoLightModel(th)
    m.update(feat(0, **DAY))
    # sun in frame darkens the rest: dark+grey readings but 20 % clipped -> hold off
    for t in range(1, 30):
        d = m.update(feat(t, sky_y=100.0, chroma_=2.0, clip=0.2))
        assert not d.lights and d.reason == "clip-hold"
    # from ON, a clipped daylight sky still switches off (default)
    m2 = AutoLightModel(th)
    m2.update(feat(0, **DARK))
    tl = [m2.update(feat(t, sky_y=185.0, chroma_=20.0, clip=0.3)) for t in range(1, 30)]
    assert not tl[-1].lights and tl[-1].reason in ("daylight", "clip-hold")


def test_clip_blocks_off_when_configured():
    th = LightThresholds(window_s=1.0, clip_blocks_off=True)
    m = AutoLightModel(th)
    m.update(feat(0, **DARK))
    tl = [m.update(feat(t, sky_y=185.0, chroma_=20.0, clip=0.3)) for t in range(1, 30)]
    assert all(d.lights and d.reason == "clip-hold" for d in tl)


def test_no_camera_holds_or_falls_back_to_sun():
    m = AutoLightModel()
    assert m.update(None, 20.0, t=0).reason == "sun-only"
    assert not m.lights
    assert AutoLightModel().update(None, None, t=0).reason == "no-data"
    m = AutoLightModel()
    m.update(feat(0, **DARK), 20.0)
    d = m.update(None, 20.0, t=100)            # window empty, camera gone
    assert d.lights and d.reason == "no-camera-hold"
    assert m.update(None, -10.0, t=105).reason == "twilight"


def test_decision_carries_features():
    d = AutoLightModel().update(feat(0, sky_y=185.0, chroma_=20.0, clip=0.01), 12.0)
    assert d.features["sky_y"] == 185.0
    assert d.features["sky_chroma"] == pytest.approx(20.0)
    assert d.features["sky_clip"] == 0.01
    assert d.sun_elevation == 12.0
    assert 0.5 <= d.confidence <= 1.0
