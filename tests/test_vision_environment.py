"""Environment model (vision/environment.py), scene features (vision/scene_features.py)
and the environment mode of the auto-light model.

Fixtures:
* ``env_bdd_val_sample.csv`` — 287 BDD100K val frames (120 day, 60 dawn/dusk, 80 night,
  27 hand-labelled enclosed) with their scene features, BDD labels and the runtime
  outputs at fit time (tools/vision/env_fit.py fixture);
* ``env_drive_home_2026-10-19.csv`` — scene features of our 174 drive frames
  (garage before 15:25:08, then a sunny afternoon).
"""

import csv
import os
import shutil
import time

import pytest

from cyberpunk_computer.api.serialization import serialize_state
from cyberpunk_computer.frontend.deserialize import deserialize_state
from cyberpunk_computer.io.dashcam_ingest import DashcamLightIngest, make_source
from cyberpunk_computer.state.store import Store
from cyberpunk_computer.vision import environment as env
from cyberpunk_computer.vision import light_features, scene_features
from cyberpunk_computer.vision.auto_lights import (
    AutoLightModel, main as lights_main, replay, sun_at, transitions,
)
from cyberpunk_computer.vision.environment import Environment, EnvironmentModel
from cyberpunk_computer.vision.frames import DashcamRtspSource, DashcamStreamSource, Frame

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
BDD = os.path.join(FIXTURES, "env_bdd_val_sample.csv")
DRIVE = os.path.join(FIXTURES, "env_drive_home_2026-10-19.csv")
JPEG = os.path.join(FIXTURES, "dashcam_20261019_153601.jpg")
GDANSK = sun_at(54.35, 18.65)
TOD = {"daytime": "day", "dawn/dusk": "dusk_dawn", "night": "night"}


def load(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def feats(rows):
    return [scene_features.from_row(r) for r in rows]


@pytest.fixture(scope="module")
def bdd():
    rows = load(BDD)
    return rows, [EnvironmentModel().classify(f) for f in feats(rows)]


@pytest.fixture(scope="module")
def drive():
    rows = load(DRIVE)
    return rows, feats(rows)


# ── BDD100K sample ────────────────────────────────────────────────────────

def test_bdd_fixture_shape(bdd):
    rows, _ = bdd
    assert len(rows) == 287
    assert all(scene_features.from_row(r) is not None for r in rows)


def test_bdd_runtime_reproduces_fit_time_outputs(bdd):
    rows, out = bdd
    assert [e.label for e in out] == [r["p_env"] for r in rows]
    assert [e.weather or "-" for e in out] == [r["p_wx"] for r in rows]


def _recall(rows, out, tod):
    sel = [(r, e) for r, e in zip(rows, out) if r["timeofday"] == tod and r["enc"] == "0"]
    return sum(e.time_of_day == TOD[tod] for _, e in sel) / len(sel)


def test_bdd_time_of_day_recall_floors(bdd):
    rows, out = bdd
    # full-set figures (docs): day 0.88, dusk 0.48, night 0.93
    assert _recall(rows, out, "night") >= 0.88
    assert _recall(rows, out, "daytime") >= 0.80
    assert _recall(rows, out, "dawn/dusk") >= 0.35


def test_bdd_enclosed_and_lights(bdd):
    rows, out = bdd
    enc = [e for r, e in zip(rows, out) if r["enc"] == "1"]
    assert len(enc) == 27
    assert sum(e.enclosed for e in enc) >= 13                 # 15 of 27 at fit time
    assert sum(e.lights_wanted for e in enc) >= 20            # dark / dusk catch most of the rest
    day = [e for r, e in zip(rows, out) if r["timeofday"] == "daytime" and r["enc"] == "0"]
    assert sum(e.enclosed for e in day) / len(day) <= 0.06
    truth = [r["timeofday"] != "daytime" or r["enc"] == "1" for r in rows]
    acc = sum(t == e.lights_wanted for t, e in zip(truth, out)) / len(rows)
    assert acc >= 0.80      # 0.84 on this dusk-heavy sample, 0.88 on all of BDD val


def test_bdd_weather_never_claims_rain_snow_fog(bdd):
    _, out = bdd
    assert {e.weather for e in out} <= {None, "clear", "overcast"}
    assert all(e.weather is None for e in out if e.time_of_day != "day" or e.enclosed)


# ── our drive ─────────────────────────────────────────────────────────────

def _seg(rows, fs, garage):
    return [f for r, f in zip(rows, fs) if (r["scene"] == "garage") == garage]


def test_drive_outdoor_is_day(drive):
    rows, fs = drive
    m = EnvironmentModel()
    out = [m.classify(f, GDANSK(f.t)) for f in _seg(rows, fs, garage=False)]
    assert len(out) == 161
    assert all(e.label == "day" for e in out)
    assert sum(e.weather == "clear" for e in out) >= 120          # sunny afternoon
    assert sum(e.weather == "overcast" for e in out) <= 3


def test_drive_garage_is_enclosed(drive):
    rows, fs = drive
    inside = _seg(rows, fs, garage=True)[:11]      # last two garage rows: the exit ramp
    out = [EnvironmentModel().classify(f) for f in inside]
    assert sum(e.enclosed for e in out) >= 8
    assert sum(e.lights_wanted for e in out) >= 9


def test_drive_lights_in_environment_mode(drive):
    rows, fs = drive
    for sun in (GDANSK, None):
        tl = replay(fs, sun=sun)
        assert len(tl) == 174
        assert tl[0].lights is True
        tr = transitions(tl)
        assert len(tr) == 2                          # bootstrap ON + one switch OFF
        outside = fs[0].t + (15 * 3600 + 25 * 60 + 8) - (15 * 3600 + 22 * 60 + 14)
        assert outside <= tr[1].t <= outside + 20 and tr[1].lights is False
        assert tr[1].reason == "env-day"


def test_drive_threshold_mode_unchanged(drive):
    _, fs = drive
    tl = replay(fs, sun=GDANSK, use_environment=False)
    tr = transitions(tl)
    assert [d.lights for d in tr] == [True, False] and tr[1].reason == "daylight"


def test_lights_cli_features(capsys):
    assert lights_main(["--features", DRIVE]) == 0
    out = capsys.readouterr().out
    assert "env-day" in out and "1 switches" in out


def test_environment_cli(capsys):
    assert env.main(["--csv", DRIVE, "--latlon", "54.35,18.65"]) == 0
    out = capsys.readouterr().out
    assert "174 samples" in out and "enclosed" in out


# ── model mechanics ───────────────────────────────────────────────────────

def _f(**kw):
    """A plain daylight frame (our drive, 15:36), overridable per field."""
    rows = load(DRIVE)
    f = scene_features.from_row(rows[100])
    d = f.as_dict()
    d.update(kw)
    return scene_features.SceneFeatures(**d)


def test_walk_and_derived():
    x = dict(sky_u=140.0, sky_v=120.0, b0_y=180.0, b1_y=150.0, b2_y=90.0,
             sky_high=255.0, sky_low=90.0)
    assert env.derived(x) == {"sky_blue": 10.0, "top_mid": 90.0, "top_drop": 30.0,
                              "sky_spread": 165.0}
    tree = ("a", 1.0, ("L", (1.0, 0.0)), ("b", 2.0, ("RL", (0.0, 1.0)), ("RR", (0.5, 0.5))))
    assert env._walk(tree, {"a": 1.0})[0] == "L"
    assert env._walk(tree, {"a": 1.5, "b": 2.0})[0] == "RL"
    assert env._walk(tree, {"a": 1.5, "b": 3.0})[0] == "RR"


def test_dark_frame_is_night_and_sun_prior_cannot_make_it_day():
    dark = _f(sky_y=15.0, sky_high=30.0, sky_low=3.0, sky_dark=0.9, full_dark=0.8,
              y_high=40.0, sky_u=128.0, sky_v=128.0)
    m = EnvironmentModel()
    assert m.classify(dark).time_of_day == "night"
    assert m.classify(dark, sun_elevation=40.0).time_of_day == "night"   # strong evidence wins
    assert m.classify(dark).lights_wanted


def test_sun_prior_resolves_day_dusk_ambiguity():
    # a weak "day" leaf (p 0.51 / 0.47 / 0.03): low texture, few dark pixels
    amb = _f(edge_full=0.03, full_dark=0.02)
    m = EnvironmentModel()
    e0 = m.classify(amb)
    assert e0.time_of_day == "day" and e0.confidence < 0.6
    assert m.classify(amb, sun_elevation=30.0).confidence > 0.8
    assert m.classify(amb, sun_elevation=-8.0).time_of_day == "dusk_dawn"


def test_window_vote_ignores_one_odd_frame():
    m = EnvironmentModel(window_s=10.0)
    t0 = 1000.0
    day = _f()
    garage = _f(sky_y=105.0, sky_high=150.0, sky_low=58.0, sky_u=128.0, sky_v=127.0,
                b0_y=105.0, b1_y=110.0, sky_sat=3.0)
    assert EnvironmentModel().classify(garage).enclosed
    labels = []
    for i in range(20):
        f = garage if i == 12 else day
        labels.append(m.update(f, t=t0 + i).label)
    assert set(labels) == {"day"}
    for i in range(20, 32):
        e = m.update(garage, t=t0 + i)
    assert e.enclosed and e.label == "enclosed"


def test_update_without_frames_expires():
    m = EnvironmentModel(window_s=10.0)
    assert m.update(_f(), t=100.0).label == "day"
    assert m.update(None, t=105.0).label == "day"
    assert m.update(None, t=111.0) is None and m.last is None


def test_environment_mode_hysteresis():
    lights = AutoLightModel()
    sure_day = Environment(t=0, time_of_day="day", enclosed=False, weather=None,
                           confidence=0.9, label="day")
    tunnel = Environment(t=0, time_of_day="day", enclosed=True, weather=None,
                         confidence=1.0, label="enclosed")
    unsure = Environment(t=0, time_of_day="day", enclosed=False, weather=None,
                         confidence=0.52, label="day")
    lf = _f(sky_clip=0.0).light()          # no sun in the sky band (rule 3 stays out)
    d = lights.update(lf, None, t=0.0, environment=sure_day)
    assert d.lights is False and d.reason == "bootstrap-env-day"
    out = [lights.update(lf, None, t=float(t), environment=tunnel) for t in range(1, 8)]
    assert [o.lights for o in out[:4]] == [False] * 4 and out[4].reason == "pending-on"
    assert out[5].lights is True and out[5].reason == "env-enclosed"   # 5 s after the first
    held = lights.update(lf, None, t=8.0, environment=unsure)
    assert held.lights is True and held.reason == "dead-band-hold"


def test_twilight_still_overrides_environment():
    lights = AutoLightModel()
    sure_day = Environment(t=0, time_of_day="day", enclosed=False, weather="clear",
                           confidence=1.0, label="day")
    d = lights.update(_f().light(), -10.0, t=0.0, environment=sure_day)
    assert d.lights is True and d.reason == "twilight"


# ── scene features ────────────────────────────────────────────────────────

def test_from_row_rejects_incomplete():
    r = load(DRIVE)[0]
    assert scene_features.from_row(r).t == pytest.approx(1792416134.0)
    bad = dict(r)
    bad["sky_high"] = ""
    assert scene_features.from_row(bad) is None


def test_scene_light_subset():
    f = _f()
    lf = f.light()
    assert (lf.sky_y, lf.sky_chroma, lf.sky_clip, lf.road_y) == (f.sky_y, f.sky_chroma,
                                                                   f.sky_clip, f.road_y)


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
def test_scene_extract_matches_light_features():
    f = scene_features.extract(JPEG, t=1792416961)
    lf = light_features.extract(JPEG, t=1792416961)
    assert f is not None and lf is not None
    for k in ("sky_y", "sky_u", "sky_v", "road_y", "y_avg", "sky_clip"):
        assert getattr(f, k) == pytest.approx(getattr(lf, k), abs=0.05)
    assert f.sky_high == 255 and 0.0 <= f.edge_full <= 1.0
    e = EnvironmentModel().classify(f, GDANSK(f.t))
    assert e.label == "day" and e.weather == "clear"


def test_scene_extract_none_without_ffprobe(monkeypatch):
    from cyberpunk_computer.vision import ffmpeg
    monkeypatch.setattr(ffmpeg, "FFPROBE", "/nonexistent/ffprobe")
    assert scene_features.extract(JPEG, t=0) is None


# ── ingest + state ────────────────────────────────────────────────────────

class ListSource:
    def __init__(self, frames):
        self.frames = list(frames)

    def grab(self):
        return self.frames.pop(0) if self.frames else None


def test_ingest_publishes_environment_and_round_trips(drive):
    rows, fs = drive
    by_path = {f"f{i}": f for i, f in enumerate(fs[:3])}
    src = ListSource([Frame(p, f.t) for p, f in by_path.items()])
    ing = DashcamLightIngest(src, extractor=lambda path, t=None: by_path[path],
                             clock=lambda: fs[2].t)
    assert ing.interval_s == 1.0
    store = Store()
    for _ in range(3):
        fields = ing.poll_once()
        for a in ing.drain_actions():
            store.dispatch(a)
    assert fields["camera_ok"] and fields["environment"] in ("enclosed", "day", "dusk_dawn")
    v = store.state.vision
    assert v.environment == fields["environment"] and v.time_of_day in env.TOD_CLASSES
    assert isinstance(v.enclosed, bool) and v.environment_confidence is not None
    assert v.lights_recommended is True
    data = serialize_state(store.state)
    assert data["vision"]["environment"] == v.environment
    assert data["vision"]["enclosed"] == v.enclosed
    assert deserialize_state(data).vision == v


def test_ingest_without_camera_has_no_environment():
    ing = DashcamLightIngest(ListSource([]), clock=lambda: 1e9)
    fields = ing.poll_once()
    assert fields["environment"] is None and fields["weather"] is None


def test_make_source_kinds():
    assert isinstance(make_source("rtsp://x/y"), DashcamStreamSource)
    assert isinstance(make_source("rtsp://x/y", "grab"), DashcamRtspSource)
    assert make_source("rtsp://x/y", None, 2.0).fps == 0.5


# ── 1 fps stream source ───────────────────────────────────────────────────

class FakeProc:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = -15

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


def test_stream_source_restarts_with_backoff(tmp_path):
    now = [0.0]
    procs = []

    def popen(cmd, **kw):
        assert "-update" in cmd and "-atomic_writing" in cmd and cmd[-1].endswith("latest.jpg")
        procs.append(FakeProc())
        return procs[-1]
    s = DashcamStreamSource("rtsp://x/y", out_dir=str(tmp_path), clock=lambda: now[0],
                            popen=popen, restart_backoff_s=5.0, stall_s=15.0)
    assert s.grab() is None and s.starts == 1
    (tmp_path / "latest.jpg").write_bytes(b"x")
    fr = s.grab()
    assert fr is not None and fr.path.endswith("latest.jpg")
    assert s.grab() is None                       # same file, no new frame
    procs[-1].code = 1                            # ffmpeg died (camera asleep)
    now[0] = 1.0
    assert s.grab() is None and s.starts == 1     # back-off
    now[0] = 6.5
    s.grab()
    assert s.starts == 2
    now[0] = 30.0                                 # alive but stalled
    s.grab()
    assert procs[1].code == -15
    s.stop()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_stream_source_real_ffmpeg(tmp_path):
    s = DashcamStreamSource("testsrc2=size=640x360:rate=10", out_dir=str(tmp_path),
                            input_args=["-re", "-f", "lavfi"], fps=4)
    got = []
    t0 = time.time()
    try:
        while time.time() - t0 < 5 and len(got) < 3:
            fr = s.grab()
            if fr is not None:
                got.append(fr)
                assert scene_features.extract(fr.path, t=fr.t) is not None
            time.sleep(0.05)
    finally:
        s.stop()
    assert len(got) >= 2
