"""Dashcam light ingest (io/dashcam_ingest.py) → store ``vision`` section."""

import time

from cyberpunk_computer.api.serialization import serialize_state
from cyberpunk_computer.frontend.deserialize import deserialize_state
from cyberpunk_computer.io.dashcam_ingest import (
    DEFAULT_HOME_LATLON, DashcamLightIngest, parse_latlon, position_from_state,
)
from cyberpunk_computer.state.store import StateSlice, Store
from cyberpunk_computer.vision import ffmpeg
from cyberpunk_computer.vision.frames import DashcamRtspSource, Frame
from cyberpunk_computer.vision.light_features import LightFeatures, chroma


class FakeSource:
    def __init__(self, frames):
        self.frames = list(frames)

    def grab(self):
        return self.frames.pop(0) if self.frames else None


def fake_extract(values):
    def _extract(path, t=None):
        sky_y, c = values[path]
        return LightFeatures(t=t, sky_y=sky_y, sky_u=128 + c, sky_v=128,
                             sky_chroma=chroma(128 + c, 128), sky_clip=0.0, road_y=60.0)
    return _extract


def test_from_env_disabled_by_default():
    assert DashcamLightIngest.from_env(env={}) is None
    assert DashcamLightIngest.from_env(env={"BACKEND_DASHCAM_URL": "  "}) is None


def test_from_env_enabled():
    ing = DashcamLightIngest.from_env(env={
        "BACKEND_DASHCAM_URL": "rtsp://192.168.0.1/liveRTSP/av4",
        "BACKEND_DASHCAM_INTERVAL_S": "7", "BACKEND_HOME_LATLON": "50.06,19.94"})
    assert isinstance(ing.source, DashcamRtspSource)
    assert ing.interval_s == 7.0 and ing.default_latlon == (50.06, 19.94)


def test_parse_latlon():
    assert parse_latlon(None) == DEFAULT_HOME_LATLON == (54.35, 18.65)
    assert parse_latlon("bogus") == DEFAULT_HOME_LATLON
    assert parse_latlon("95,0") == DEFAULT_HOME_LATLON
    assert parse_latlon(" 52.2, 21.0") == (52.2, 21.0)


def test_position_from_state_none_today_but_reads_future_gps():
    assert position_from_state(Store().state) is None

    class Gps:
        latitude, longitude = 52.0, 21.0

    class S:
        gps = Gps()
    assert position_from_state(S()) == (52.0, 21.0)


def test_poll_and_drain_into_store():
    store = Store()
    seen = []
    store.subscribe(StateSlice.VISION, lambda s: seen.append(s.vision))
    noon = 1781870400.0      # 2026-06-19 12:00 UTC: sun high over Gdansk
    src = FakeSource([Frame("a", noon), Frame("b", noon + 5)])
    ing = DashcamLightIngest(src, extractor=fake_extract({"a": (185.0, 20.0), "b": (190.0, 22.0)}),
                             clock=lambda: noon)
    fields = ing.poll_once()
    assert fields["camera_ok"] and fields["lights_recommended"] is False
    assert fields["reason"] == "bootstrap-daylight"
    assert fields["sun_elevation"] > 50
    for a in ing.drain_actions():
        store.dispatch(a)
    v = store.state.vision
    assert v.enabled and v.lights_recommended is False and v.sky_y == 185.0
    assert v.sky_chroma == 20.0 and v.updated_at == noon
    assert seen and seen[-1] == v
    assert ing.drain_actions() == []           # nothing new
    ing.poll_once()
    acts = ing.drain_actions()
    assert len(acts) == 1
    store.dispatch(acts[0])
    assert store.state.vision.sky_y == 187.5  # 30 s median of 185, 190


def test_camera_absent_never_raises_and_reports():
    night = 1792447200.0     # 2026-10-19 22:00 UTC: dark in Gdansk
    ing = DashcamLightIngest(FakeSource([]), clock=lambda: night)
    fields = ing.poll_once()
    assert fields["camera_ok"] is False
    assert fields["lights_recommended"] is True and fields["reason"] == "twilight"


def test_extractor_exception_is_contained():
    def boom(path, t=None):
        raise RuntimeError("decoder exploded")
    ing = DashcamLightIngest(FakeSource([Frame("x", 0.0)]), extractor=boom, clock=lambda: 1e9)
    assert ing.poll_once()["camera_ok"] is False


def test_rtsp_source_without_ffmpeg_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg, "FFMPEG", "/nonexistent/ffmpeg")
    src = DashcamRtspSource("rtsp://192.0.2.1/none", out_dir=str(tmp_path),
                            retries=2, sleep=lambda s: None)
    assert src.grab() is None
    assert src.failures == 1


def test_thread_start_stop():
    ing = DashcamLightIngest(FakeSource([]), interval_s=0.05)
    ing.start()
    deadline = time.time() + 2
    while ing.polls < 2 and time.time() < deadline:
        time.sleep(0.01)
    ing.stop()
    assert ing.polls >= 2
    assert any(a.fields.get("enabled") for a in ing.drain_actions())


def test_vision_state_round_trips_to_frontend():
    store = Store()
    ing = DashcamLightIngest(FakeSource([Frame("a", 1e9)]),
                             extractor=fake_extract({"a": (60.0, 1.0)}), clock=lambda: 1e9)
    ing.poll_once()
    for a in ing.drain_actions():
        store.dispatch(a)
    data = serialize_state(store.state)
    assert data["vision"]["lights_recommended"] is True
    back = deserialize_state(data)
    assert back.vision == store.state.vision


def test_backend_config_from_env(monkeypatch):
    from cyberpunk_computer.backend.__main__ import _build_dashcam
    monkeypatch.delenv("BACKEND_DASHCAM_URL", raising=False)
    monkeypatch.delenv("BACKEND_HOME_LATLON", raising=False)
    cfg = _build_dashcam()
    assert cfg["dashcam_url"] is None and cfg["home_latlon"] == (54.35, 18.65)
    monkeypatch.setenv("BACKEND_DASHCAM_URL", "rtsp://192.168.0.1/liveRTSP/av4")
    assert _build_dashcam()["dashcam_url"] == "rtsp://192.168.0.1/liveRTSP/av4"


def test_backend_service_wiring_and_tick():
    from types import SimpleNamespace
    from cyberpunk_computer.backend.service import BackendConfig, BackendService
    svc = BackendService(BackendConfig())
    assert svc.dashcam is None
    svc._dashcam_tick()                       # disabled: no-op
    twin = SimpleNamespace(store=Store())
    svc = BackendService(BackendConfig(dashcam_url="rtsp://192.0.2.1/none",
                                       home_latlon=(50.0, 20.0)))
    svc._wire_dashcam(twin)
    svc.twin = twin
    assert svc.dashcam is not None and svc.dashcam.default_latlon == (50.0, 20.0)
    svc.dashcam.source = FakeSource([])       # camera absent
    svc.dashcam.poll_once()
    svc._dashcam_tick()
    v = twin.store.state.vision
    assert v.enabled and v.camera_ok is False and v.updated_at > 0
