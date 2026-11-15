"""Run-time synthetic position session (dashboard map view tooling), the raw
fix / pack file REST extras and the scene's synthetic taint flag."""

import os

from cyberpunk_computer.backend.position_feeder import (
    CanMotionTap, HeadunitGnssSource, PositionFeeder, PositionFeederConfig, SyntheticGnssSource,
)
from cyberpunk_computer.backend.roadsense_scene import SceneBuilder, SceneFeedConfig, SyntheticTaint
from cyberpunk_computer.api import position_routes

from tests.test_position_feeder import Clock, FakeFeed, _req, _serve

SPEED_20 = bytes([0, 0, 0, 0, 0, 0x07, 0xD0, 0])      # 0x0B4: 20 km/h
SPEED_0 = bytes([0, 0, 0, 0, 0, 0x00, 0x00, 0])


def _feeder(c, **kw):
    cfg = PositionFeederConfig(enabled=True, **kw)
    src = HeadunitGnssSource(cfg, clock=c.mono, wall=c.wall)
    tap = CanMotionTap()
    feed = FakeFeed()
    f = PositionFeeder(cfg, src, tap=tap, feed=feed, clock=c.mono, wall=c.wall)
    return f, tap, feed


def test_start_refused_unless_active_and_parked():
    c = Clock()
    f, tap, _ = _feeder(c)
    assert f.start_synthetic(now=c.t) == (False, "roadsense_not_running")
    f.set_active(True)
    tap.on_frame(0x0B4, SPEED_20, 0, 0.0, c.t)
    assert f.start_synthetic(now=c.t) == (False, "car_moving")
    tap.on_frame(0x0B4, SPEED_0, 0, 0.0, c.t)
    tap.gear = ("D", c.t)                                 # stopped at a light in D: not parked
    assert f.start_synthetic(now=c.t) == (False, "not_in_park")
    tap.gear = ("P", c.t)
    assert f.start_synthetic(by="dashboard", now=c.t) == (True, None)
    st = f.status(c.t)
    assert st["source"] == "synthetic" and st["synthetic"] and st["base_source"] == "headunit"
    assert st["session"]["by"] == "dashboard" and st["session"]["until"] > c.wall()
    assert f.source is not f.base_source


def test_start_allowed_with_can_dark_and_refused_when_disabled_or_configured():
    c = Clock()
    f, _, _ = _feeder(c)
    f.set_active(True)
    assert f.start_synthetic(now=c.t) == (True, None)     # car off / bench: nothing moves it
    c2 = Clock()
    g, _, _ = _feeder(c2, synthetic_control=False)
    g.set_active(True)
    assert g.start_synthetic(now=c2.t) == (False, "control_disabled")
    assert not g.status(c2.t)["synthetic_control"]
    cfg = PositionFeederConfig(enabled=True, source="synthetic")
    h = PositionFeeder(cfg, SyntheticGnssSource(cfg), tap=CanMotionTap(), feed=FakeFeed())
    h.set_active(True)
    assert h.start_synthetic() == (False, "configured")
    assert not h.status()["synthetic_control"]


def test_session_ends_when_the_car_moves_and_can_motion_takes_over():
    c = Clock()
    f, tap, feed = _feeder(c)
    f.set_active(True)
    assert f.start_synthetic(now=c.t)[0]
    for _ in range(20):
        f.step(c.t)
        c.tick(0.1)
    assert {m["source"] for m in feed.msgs} == {"synthetic"}
    n = len(feed.msgs)
    tap.on_frame(0x0B4, SPEED_20, 0, 0.0, c.t)
    f.step(c.t)
    new = feed.msgs[n:]
    assert f.session is None and f.source is f.base_source
    assert f.last_session_end["reason"] == "car_moving"
    # the real CAN motion goes out, then the head unit's "no fix" (mapd dead-reckons on real motion)
    assert [(m["type"], m.get("source"), m.get("fix")) for m in new] == [
        ("motion", "prius-can", None), ("gnss", "headunit", "none")]
    tap.on_frame(0x0B4, SPEED_0, 0, 0.0, c.t)
    assert f.start_synthetic(now=c.t)[0]                  # standstill again: may start anew


def test_session_expires_and_ends_with_roadsense():
    c = Clock()
    f, _, feed = _feeder(c, synthetic_session_s=60.0)
    f.set_active(True)
    f.start_synthetic(now=c.t)
    f.step(c.t)
    c.tick(30)
    assert f.start_synthetic(now=c.t)[0]                  # renew: 60 s from now
    c.tick(50)
    f.step(c.t)
    assert f.session is not None
    c.tick(11)
    f.step(c.t)
    assert f.session is None and f.last_session_end["reason"] == "expired"
    assert feed.msgs[-1]["fix"] == "none" and feed.msgs[-1]["source"] == "headunit"
    f.start_synthetic(now=c.t)
    f.set_active(False)
    assert f.session is None and f.last_session_end["reason"] == "roadsense_stopped"
    assert not f.stop_synthetic()


def _session_msgs(feed):
    return [(m["type"], m.get("source"), m.get("fix"), m.get("reason")) for m in feed.msgs]


def test_reset_at_session_start_and_end_before_the_new_source_fix():
    c = Clock()
    f, tap, feed = _feeder(c)
    feed.accepts = ["gnss", "motion", "reset"]          # roadsense >= 2026-11-15
    f.set_active(True)
    f.step(c.t)
    n = len(feed.msgs)
    assert not any(m["type"] == "reset" for m in feed.msgs)   # no session: never
    assert f.start_synthetic(now=c.t)[0]
    c.tick(0.1)
    f.step(c.t)
    new = [m for m in feed.msgs[n:] if m["type"] != "motion"]
    # the reset goes before the synthetic drive's first fix
    assert new[0] == {"type": "reset", "reason": "synthetic session started"}
    assert new[1]["type"] == "gnss" and new[1]["source"] == "synthetic"
    assert f.start_synthetic(now=c.t)[0]                # renewing is no source change: no reset
    for _ in range(10):
        c.tick(0.1)
        f.step(c.t)
    assert sum(m["type"] == "reset" for m in feed.msgs) == 1
    n = len(feed.msgs)
    assert f.stop_synthetic("stopped", now=c.t)
    f.step(c.t)
    new = [m for m in feed.msgs[n:] if m["type"] != "motion"]
    assert new[0] == {"type": "reset", "reason": "synthetic session ended: stopped"}
    assert (new[1]["type"], new[1]["source"], new[1]["fix"]) == ("gnss", "headunit", "none")
    st = f.status(c.t)
    assert st["reset_sent"] == 2 and not st["reset_pending"]
    assert st["last_reset"]["sent"] and st["last_reset"]["reason"].endswith("stopped")
    assert st["mapd_accepts"] == ["gnss", "motion", "reset"]


def test_reset_on_every_way_a_session_ends():
    for how in ("car_moving", "expired", "roadsense_stopped"):
        c = Clock()
        f, tap, feed = _feeder(c, synthetic_session_s=60.0)
        feed.accepts = ["gnss", "motion", "reset"]
        f.set_active(True)
        f.start_synthetic(now=c.t)
        f.step(c.t)
        if how == "car_moving":
            tap.on_frame(0x0B4, SPEED_20, 0, 0.0, c.t)
        elif how == "expired":
            c.tick(61)
        else:
            f.set_active(False)
            f.set_active(True)                          # roadsense back: the reset is still owed
        f.step(c.t)
        assert f.last_session_end["reason"] == how
        resets = [m["reason"] for m in feed.msgs if m["type"] == "reset"]
        assert resets == ["synthetic session started", "synthetic session ended: %s" % how], how


def test_no_reset_to_an_older_mapd():
    c = Clock()
    f, _, feed = _feeder(c)
    feed.accepts = ["gnss", "motion"]                    # roadsense before 2026-11-15
    f.set_active(True)
    f.start_synthetic(now=c.t)
    for _ in range(5):
        f.step(c.t)
        c.tick(0.1)
    f.stop_synthetic(now=c.t)
    f.step(c.t)
    assert not any(m["type"] == "reset" for m in feed.msgs)
    st = f.status(c.t)
    assert st["reset_sent"] == 0 and not st["reset_pending"]
    assert st["last_reset"]["sent"] is False and st["last_reset"]["why"] == "not_accepted"


def test_reset_waits_for_the_hello_reply_and_a_connection():
    c = Clock()
    f, _, feed = _feeder(c)
    feed.up = False                                      # mapd away
    f.set_active(True)
    f.start_synthetic(now=c.t)
    f.stop_synthetic(now=c.t)
    f.step(c.t)
    assert feed.msgs == [] and f.status(c.t)["reset_pending"]
    feed.up = True                                       # connected, hello reply not read yet
    f.step(c.t)
    assert not any(m["type"] == "reset" for m in feed.msgs) and f.status(c.t)["reset_pending"]
    feed.hello_after_poll = ["gnss", "motion", "reset"]  # the reply arrives
    c.tick(0.1)
    f.step(c.t)
    assert [m["reason"] for m in feed.msgs if m["type"] == "reset"] == ["synthetic session ended: stopped"]
    assert not f.status(c.t)["reset_pending"]


def test_raw_fix_kept_out_of_status():
    c = Clock()
    f, _, _ = _feeder(c)
    assert f.raw_fix(c.t) is None
    f.source.ingest({"lat": 50.66, "lon": 17.92, "hacc_m": 4.0, "age_ms": 100})
    r = f.raw_fix(c.t)
    assert r["lat"] == 50.66 and r["hacc_m"] == 4.0 and r["source"] == "headunit" and r["age_s"] >= 0.1
    assert "50.66" not in repr(f.status(c.t))


def test_pack_file(tmp_path, monkeypatch):
    pack = tmp_path / "poland-20261114.rsmap"
    pack.write_bytes(b"x" * 1234)
    link = tmp_path / "current.rsmap"
    os.symlink(pack, link)
    p = position_routes.pack_file(str(link))
    assert p["file"] == "poland-20261114.rsmap" and p["size_bytes"] == 1234
    assert position_routes.pack_file(str(tmp_path / "missing")) is None
    monkeypatch.setenv("BACKEND_ROADSENSE_MAP_PACK", str(link))
    assert position_routes.pack_file()["size_bytes"] == 1234


def test_rest_synthetic_needs_auth_and_parked():
    cfg = PositionFeederConfig(enabled=True)
    tap = CanMotionTap()
    feeder = PositionFeeder(cfg, HeadunitGnssSource(cfg), tap=tap, feed=FakeFeed())
    server, t, base = _serve(feeder, token="s3cret")
    try:
        assert _req("POST", base + "/synthetic", {"on": True})[0] == 401
        code, r = _req("POST", base + "/synthetic", {"on": True}, token="s3cret")
        assert code == 409 and r["detail"]["reason"] == "roadsense_not_running"
        feeder.set_active(True)
        import time as _t
        tap.on_frame(0x0B4, SPEED_20, 0, 0.0, _t.monotonic())
        code, r = _req("POST", base + "/synthetic", {"on": True}, token="s3cret")
        assert code == 409 and r["detail"]["reason"] == "car_moving"
        tap.speed = None
        assert _req("POST", base + "/synthetic", {"on": "yes"}, token="s3cret")[0] == 400
        code, st = _req("POST", base + "/synthetic", {"on": True}, token="s3cret")
        assert code == 200 and st["source"] == "synthetic" and st["session"]["by"] == "dashboard"
        # the head-unit ingest is refused while the synthetic session runs
        assert _req("POST", base + "/gnss", {"lat": 50.6, "lon": 17.9}, token="s3cret")[0] == 503
        code, st = _req("GET", base, token="s3cret")
        assert code == 200 and "raw_fix" in st and "pack_file" in st
        code, st = _req("POST", base + "/synthetic", {"on": False}, token="s3cret")
        assert code == 200 and st["source"] == "headunit" and st["last_session_end"]["reason"] == "stopped"
    finally:
        server.should_exit = True
        t.join(5)


def test_rest_synthetic_forbidden_when_disabled():
    cfg = PositionFeederConfig(enabled=True, synthetic_control=False)
    feeder = PositionFeeder(cfg, HeadunitGnssSource(cfg), tap=CanMotionTap(), feed=FakeFeed())
    feeder.set_active(True)
    server, t, base = _serve(feeder)
    try:
        code, r = _req("POST", base + "/synthetic", {"on": True})
        assert code == 403 and r["detail"]["reason"] == "control_disabled"
    finally:
        server.should_exit = True
        t.join(5)


# ── scene: synthetic taint ───────────────────────────────────────────────────

def _map(state, src, fix):
    return {"type": "map", "state": state, "input": {"gnss": {"source": src, "fix": fix}}}


def test_synthetic_taint_rules():
    t = SyntheticTaint()
    assert not t.update(_map("good", "headunit", "3d"))
    assert t.update(_map("good", "synthetic", "3d"))
    assert t.update(_map("dead_reckoning", "headunit", "none"))   # residue: mapd still on the synthetic place
    assert t.update(_map("dead_reckoning", "synthetic", "3d"))    # suspended drive (stale synthetic fix)
    assert not t.update(_map("unknown", "headunit", "none"))      # position gone
    t.update(_map("good", "synthetic", "3d"))
    assert not t.update(_map("good", "headunit", "3d"))           # a real fix is in use
    assert not t.update({"type": "map", "state": "no_source", "input": {"gnss": None}})


def test_builder_marks_map_syn():
    import json
    b = SceneBuilder(SceneFeedConfig())
    fused = {"type": "fused", "map": {"available": True, "state": "dead_reckoning", "match_confidence": 0.9},
             "speed_limit": {"limit_kmh": 30, "source": "map"}}
    road = {"type": "road", "seq": 5, "lane_lines": [], "road_edges": []}
    b.feed(json.dumps(_map("good", "synthetic", "3d")), 0.0)
    b.feed(json.dumps(_map("dead_reckoning", "headunit", "none")), 0.1)
    b.feed(json.dumps(fused), 0.2)
    b.feed(json.dumps(road), 0.3)
    sc = b.build(0.3)
    assert sc["map"]["syn"] is True and sc["map"]["limit"]["src"] == "map"
    b.feed(json.dumps(_map("unknown", "headunit", "none")), 0.4)
    b.feed(json.dumps(dict(road, seq=6)), 0.5)
    assert "syn" not in b.build(1.0)["map"]
