"""Position feeder -> roadsense mapd (backend.position_feeder, api.position_routes).

Frames in the decoder tests are copied from the 2026-11-13 drive recorded on
the car (trip_20261113_214804_935, panda broadcast CAN). The end-to-end runs
against the real roadsense mapd are in test_position_mapd_e2e.py.
"""

import json
import os
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request

import pytest

from cyberpunk_computer.backend.position_client import PositionFeed
from cyberpunk_computer.backend.position_feeder import (
    DEFAULT_ROUTE,
    CanMotionTap,
    HeadunitGnssSource,
    NoGnssSource,
    PositionFeeder,
    PositionFeederConfig,
    SyntheticDrive,
    SyntheticGnssSource,
    decode_gear,
    decode_speed_kmh,
    decode_steering_deg,
    decode_wheel_pair_kmh,
    load_route,
    make_source,
)
from cyberpunk_computer.backend.roadsense_scene import (
    SceneBuilder,
    SceneFeedConfig,
    fused_to_scene,
    map_input_to_scene,
    map_shape_to_scene,
)


# ── decoders (real frames) ───────────────────────────────────────────────────

def test_decode_real_frames():
    assert decode_speed_kmh(bytes([0, 0, 0, 0, 1, 14, 17, 220])) == pytest.approx(36.01)
    assert decode_speed_kmh(bytes([0, 0, 0, 0, 0, 0, 0, 0])) == 0.0
    # rear pair at 36.08 km/h (0x0B4): right 35.24, left 35.37 km/h
    r, l = decode_wheel_pair_kmh(bytes([13, 196, 13, 209, 145, 249]))
    assert (r, l) == (pytest.approx(35.24), pytest.approx(35.37))
    # 0x025: 0x0FF9 = -7 counts x 1.5 deg
    assert decode_steering_deg(bytes([15, 249, 0, 0, 121, 121, 121, 160])) == -10.5
    assert decode_steering_deg(bytes([0, 35, 0, 0, 9, 106, 121, 60])) == 52.5
    assert decode_gear(bytes([0, 0, 0, 0, 16, 35, 4, 96])) == "D"
    assert decode_gear(bytes([0, 0, 0, 0, 16, 32, 4, 93])) == "P"
    assert decode_gear(bytes([0, 0, 0, 0, 16, 33, 0, 0])) == "R"
    assert decode_gear(bytes([0, 0, 0, 0, 16, 0x2F, 0, 0])) is None
    assert decode_speed_kmh(b"\x00\x00") is None


def _tap_with(t=100.0, kmh=36.0, front=(35.0, 35.1), rear=(35.4, 35.0), steer=-7, gear=3):
    tap = CanMotionTap()
    w = lambda v: [int(round(v * 100)) >> 8, int(round(v * 100)) & 0xFF]   # noqa: E731
    sp = int(round(kmh * 100))
    tap.on_frame(0x0B4, bytes([0, 0, 0, 0, 0, sp >> 8, sp & 0xFF, 0]), 0, 0.0, t)
    tap.on_frame(0x0B1, bytes(w(front[0]) + w(front[1]) + [0, 0]), 0, 0.0, t - 0.005)
    tap.on_frame(0x0B3, bytes(w(rear[0]) + w(rear[1]) + [0, 0]), 0, 0.0, t - 0.003)
    raw = steer & 0xFFF
    tap.on_frame(0x025, bytes([raw >> 8, raw & 0xFF, 0, 0, 0, 0, 0, 0]), 0, 0.0, t - 0.002)
    tap.on_frame(0x120, bytes([0, 0, 0, 0, 16, 0x20 | gear, 0, 0]), 0, 0.0, t - 0.01)
    tap.on_frame(0x3CB, bytes(8), 0, 0.0, t)              # not ours: ignored
    return tap


def test_tap_sample_fields_order_and_timestamp():
    cfg = PositionFeederConfig(enabled=True, yaw_inputs=("wheels", "steer"))
    tap = _tap_with()
    m = tap.sample(100.05, cfg)
    assert m["type"] == "motion" and m["source"] == "prius-can"
    assert m["ts_mono_ns"] == int(100.0 * 1e9)           # the 0x0B4 receipt time, not now
    assert m["speed_mps"] == pytest.approx(10.0, abs=1e-3)
    assert m["gear"] == "D"
    fl, fr, rl, rr = m["wheel_speeds_mps"]
    # [FL, FR, RL, RR]: bytes A,B are the RIGHT wheel, C,D the LEFT one
    assert fr == pytest.approx(35.0 / 3.6, abs=1e-3) and fl == pytest.approx(35.1 / 3.6, abs=1e-3)
    assert rr == pytest.approx(35.4 / 3.6, abs=1e-3) and rl == pytest.approx(35.0 / 3.6, abs=1e-3)
    # rear right faster = turning left = positive yaw for mapd ((RR - RL) / track)
    assert rr - rl > 0
    assert m["steering_wheel_deg"] == -10.5
    assert tap.frames == 5


def test_tap_stale_and_pairing():
    cfg = PositionFeederConfig(enabled=True, yaw_inputs=("wheels", "steer"))
    tap = _tap_with()
    assert tap.sample(100.31, cfg) is None                # CAN dark for > stale_s
    tap.speed = (36.0, 100.5)                             # speed fresh, others 0.5 s old
    m = tap.sample(100.5, cfg)
    assert "wheel_speeds_mps" not in m and "steering_wheel_deg" not in m
    assert m["gear"] == "D"                               # gear may be up to 1 s old
    tap.speed = (36.0, 101.2)
    assert "gear" not in tap.sample(101.2, cfg)
    cfg2 = PositionFeederConfig(enabled=True, yaw_inputs=("steer",))
    m2 = _tap_with().sample(100.0, cfg2)
    assert "wheel_speeds_mps" not in m2 and m2["steering_wheel_deg"] == -10.5
    m3 = _tap_with().sample(100.0, PositionFeederConfig(enabled=True))     # default: wheels only
    assert "wheel_speeds_mps" in m3 and "steering_wheel_deg" not in m3
    assert CanMotionTap().sample(1.0, cfg) is None


# ── head-unit source ─────────────────────────────────────────────────────────

class Clock:
    def __init__(self, t=1000.0, wall=1.8e9):
        self.t, self.w = t, wall

    def mono(self):
        return self.t

    def wall(self):
        return self.w

    def tick(self, dt):
        self.t += dt
        self.w += dt


def _hu(clock, **kw):
    return HeadunitGnssSource(PositionFeederConfig(enabled=True, **kw), clock=clock.mono, wall=clock.wall)


def test_headunit_age_ms_time_and_fields():
    c = Clock()
    src = _hu(c)
    r = src.ingest({"lat": 50.66, "lon": 17.92, "hacc_m": 4.0, "speed_mps": 13.9, "course_deg": 271.5,
                    "sats": 14, "hdop": 0.8, "utc_ms": int(c.w * 1000) - 300, "age_ms": 250,
                    "unknown_field": 1})
    assert r == {"accepted": 1, "rejected": [], "has_fix": True}
    (m,) = src.drain(c.t)
    assert m["type"] == "gnss" and m["source"] == "headunit" and m["fix"] == "3d"
    assert m["ts_mono_ns"] == int((1000.0 - 0.25 - 0.02) * 1e9)   # age + link delay
    assert m["hacc_m"] == 4.0 and m["sats"] == 14 and m["course_deg"] == 271.5
    assert "unknown_field" not in m and "age_ms" not in m
    assert src.drain(c.t) == []
    assert src.counts["time_age"] == 1


def test_headunit_utc_fallback_and_arrival():
    c = Clock()
    src = _hu(c)
    src.ingest({"lat": 50.0, "lon": 17.0, "utc_ms": int((c.w - 0.4) * 1000)})
    (m,) = src.drain(c.t)
    assert m["ts_mono_ns"] == pytest.approx(int((c.t - 0.4) * 1e9), abs=2e6)
    c.tick(1.0)
    # head unit clock 1 h off: the utc time is not trusted, arrival time used
    src.ingest({"lat": 50.0, "lon": 17.0, "utc_ms": int((c.w - 3600) * 1000)})
    (m,) = src.drain(c.t)
    assert m["ts_mono_ns"] == int((c.t - 0.02) * 1e9)
    assert src.counts["time_utc"] == 1 and src.counts["time_arrival"] == 1


def test_headunit_validation():
    c = Clock()
    src = _hu(c)
    bad = [
        "x", {"lat": 91, "lon": 0}, {"lat": 50}, {"lat": 0.0, "lon": 0.0}, {"lat": True, "lon": 17},
        {"lat": 50, "lon": 17, "fix": "4d"}, {"lat": 50, "lon": 17, "speed_mps": -1},
        {"lat": 50, "lon": 17, "sats": 3.5}, {"lat": 50, "lon": 17, "utc_ms": 12},
        {"lat": 50, "lon": 17, "age_ms": 5000},              # too old
        {"lat": 50, "lon": 17, "hacc_m": float("nan")},
    ]
    r = src.ingest(bad)
    assert r["accepted"] == 0 and len(r["rejected"]) == len(bad) and r["has_fix"] is False
    assert src.drain(c.t) == []
    with pytest.raises(ValueError):
        src.ingest([{"lat": 50, "lon": 17}] * 21)
    # not newer than the previous fix
    assert src.ingest({"lat": 50, "lon": 17, "age_ms": 100})["accepted"] == 1
    assert src.ingest({"lat": 50, "lon": 17, "age_ms": 200})["accepted"] == 0


def test_headunit_fix_state():
    c = Clock()
    src = _hu(c)
    assert not src.has_fix(c.t)                           # nothing ever arrived
    src.ingest({"lat": 50, "lon": 17})
    assert src.has_fix(c.t)
    c.tick(3.5)
    assert not src.has_fix(c.t)                           # silent > fix_timeout_s
    src.ingest({"lat": 50, "lon": 17})
    assert src.has_fix(c.t)
    c.tick(0.1)
    assert src.ingest({"fix": "none"})["has_fix"] is False    # the receiver lost it
    c.tick(0.9)
    assert src.ingest({"lat": 50, "lon": 17})["has_fix"] is True
    st = src.status(c.t)
    assert st["has_fix"] and st["last_fix_age_s"] == pytest.approx(0.0, abs=0.05)
    assert "lat" not in st and "lon" not in st            # no coordinates in the state mirror


# ── feeder cycle (fake socket) ───────────────────────────────────────────────

class FakeFeed:
    def __init__(self, up=True, accepts=None):
        self.up, self.msgs, self.path = up, [], "/fake/position.sock"
        self.sent = self.dropped = self.connects = self.errors_total = 0
        self.errors = []
        self.accepts = accepts          # mapd's hello "accepts"; None = no reply read yet
        self.hello_after_poll = None    # poll() sets accepts to this (a reply that arrives)

    def poll(self):
        if self.hello_after_poll is not None and self.up:
            self.accepts = self.hello_after_poll

    @property
    def connected(self):
        return self.up

    def send(self, m):
        if not self.up:
            self.dropped += 1
            return False
        self.msgs.append(dict(m))
        self.sent += 1
        return True

    def close(self):
        pass


def test_feeder_headunit_no_fix_then_fix():
    c = Clock()
    cfg = PositionFeederConfig(enabled=True, yaw_inputs=("wheels", "steer"))
    src = HeadunitGnssSource(cfg, clock=c.mono, wall=c.wall)
    tap = _tap_with(t=c.t)
    feed = FakeFeed()
    statuses = []
    f = PositionFeeder(cfg, src, tap=tap, feed=feed, clock=c.mono, publish_status=statuses.append)
    f.step(c.t)
    kinds = [(m["type"], m.get("fix")) for m in feed.msgs]
    assert kinds == [("motion", None), ("gnss", "none")]       # no head unit: fix "none" at once
    assert feed.msgs[1]["source"] == "headunit"
    assert f.yaw_input == "wheels+steer"
    c.tick(0.1)
    f.step(c.t)                                                 # same speed frame: no motion; none not repeated
    assert len(feed.msgs) == 2
    c.tick(5.0)
    tap.speed = (36.0, c.t)
    f.step(c.t)
    assert [m.get("fix") for m in feed.msgs[2:]] == [None, "none"]    # repeated every none_repeat_s
    src.ingest({"lat": 50.66, "lon": 17.92, "age_ms": 100})
    c.tick(0.1)
    tap.speed = (36.0, c.t)
    f.step(c.t)
    assert feed.msgs[-1]["type"] == "gnss" and feed.msgs[-1]["fix"] == "3d"
    st = f.status(c.t)
    assert st["source"] == "headunit" and st["connected"] and st["gnss"]["has_fix"]
    assert st["motion_sent"] == 3 and st["gnss_sent"] == 1 and st["none_sent"] == 2
    assert st["motion"]["yaw_input"] is None                 # wheel / steering frames are old now
    assert statuses and statuses[0]["enabled"]


def test_feeder_silent_when_can_dark_and_source_none():
    c = Clock()
    cfg = PositionFeederConfig(enabled=True, source="none")
    feed = FakeFeed()
    f = PositionFeeder(cfg, NoGnssSource(), tap=CanMotionTap(), feed=feed, clock=c.mono)
    for _ in range(20):
        f.step(c.t)
        c.tick(0.1)
    assert feed.msgs == []                                   # car off: nothing at all


def test_feeder_drops_quietly_when_mapd_absent():
    c = Clock()
    cfg = PositionFeederConfig(enabled=True, rundir=tempfile.mkdtemp())
    src = HeadunitGnssSource(cfg, clock=c.mono, wall=c.wall)
    f = PositionFeeder(cfg, src, tap=_tap_with(t=c.t), clock=c.mono)     # real PositionFeed, no socket
    f.step(c.t)
    assert f.feed.sent == 0 and f.feed.dropped == 2 and not f.feed.connected


def test_feeder_synthetic_suspends_while_car_moves():
    c = Clock()
    cfg = PositionFeederConfig(enabled=True, source="synthetic")
    src = SyntheticGnssSource(cfg, wall=c.wall)
    tap = CanMotionTap()
    feed = FakeFeed()
    f = PositionFeeder(cfg, src, tap=tap, feed=feed, clock=c.mono)
    for _ in range(30):
        f.step(c.t)
        c.tick(0.1)
    assert {m["source"] for m in feed.msgs} == {"synthetic"}
    assert any(m["type"] == "gnss" and m["fix"] == "3d" for m in feed.msgs)
    n = len(feed.msgs)
    tap.on_frame(0x0B4, bytes([0, 0, 0, 0, 0, 0x07, 0xD0, 0]), 0, 0.0, c.t)   # 20 km/h on CAN
    f.step(c.t)
    assert len(feed.msgs) == n and f.suspended == "car_moving"
    c.tick(1.0)
    f.step(c.t)                                                # CAN went stale: resume
    assert f.suspended is None and len(feed.msgs) > n


# ── synthetic drive ──────────────────────────────────────────────────────────

def test_builtin_route_and_drive_consistency():
    pts = load_route(DEFAULT_ROUTE)
    assert len(pts) > 500
    src = SyntheticGnssSource(PositionFeederConfig(enabled=True, source="synthetic"))
    d = src.drive
    assert d.closed and 5000 < d.length < 6500
    t, prev = 0.0, None
    dist = 0.0
    lat_acc = 0.0
    for i in range(20 * 120):
        t += 0.05
        m = src.motion(t)
        fl, fr, rl, rr = m["wheel_speeds_mps"]
        assert fl == rl and fr == rr
        yaw = (rr - rl) / 1.48
        lat_acc = max(lat_acc, abs(yaw * m["speed_mps"]))
        dist += m["speed_mps"] * 0.05
    assert 600 < dist < 1600                                 # ~city speeds over 2 min
    assert abs(d.pos_s - dist) < 30.0
    assert lat_acc < 3.5                                     # curve speeds are limited
    g = src.drain(t)
    assert g and g[0]["source"] == "synthetic" and g[0]["fix"] == "3d"


def test_synthetic_open_route_goes_there_and_back(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps([[50.0, 17.0], [50.0, 17.01]]))
    d = SyntheticDrive(load_route(str(p)))
    assert not d.closed and d.length == pytest.approx(2 * 0.01 * 111319.49 * 0.6428, rel=0.01)


def test_make_source():
    assert make_source(PositionFeederConfig(source="none")).name == "none"
    assert make_source(PositionFeederConfig(source="headunit")).name == "headunit"
    with pytest.raises(ValueError):
        make_source(PositionFeederConfig(source="gps"))


# ── PositionFeed against a Unix socket server ────────────────────────────────

def test_position_feed_unix_socket_reconnects():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "position.sock")
    feed = PositionFeed(d, name="t", retry_s=0.05)
    assert not feed.send({"type": "motion", "speed_mps": 1.0})       # nobody listens: dropped
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    time.sleep(0.06)
    assert feed.send({"type": "motion", "speed_mps": 2.0})
    conn, _ = srv.accept()
    conn.settimeout(2)
    buf = b""
    while buf.count(b"\n") < 2:
        buf += conn.recv(4096)
    hello, msg = [json.loads(x) for x in buf.splitlines()[:2]]
    assert hello == {"type": "hello", "api": 1, "name": "t"}
    assert msg["speed_mps"] == 2.0 and isinstance(msg["ts_mono_ns"], int)
    conn.sendall(b'{"type":"error","line":2,"reason":"speed_mps missing"}\n')
    time.sleep(0.05)
    feed.send({"type": "motion", "speed_mps": 3.0})
    assert feed.errors_total == 1 and feed.errors[-1]["reason"] == "speed_mps missing"
    conn.close()
    srv.close()
    os.unlink(path)
    for _ in range(3):
        feed.send({"type": "motion", "speed_mps": 4.0})
    assert not feed.connected and feed.dropped >= 2
    feed.close()


def test_position_feed_reads_the_hello_accepts_list():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "position.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    feed = PositionFeed(d, name="t", retry_s=0.05)
    assert feed.accepts is None
    assert feed.send({"type": "motion", "speed_mps": 1.0})
    conn, _ = srv.accept()
    conn.sendall(b'{"type":"hello","api":1,"server":"roadsense-mapd","accepts":["gnss","motion","reset"]}\n')
    time.sleep(0.05)
    feed.poll()
    assert feed.accepts == ["gnss", "motion", "reset"] and feed.server["server"] == "roadsense-mapd"
    assert feed.reset("test over")
    conn.settimeout(2)
    buf = b""
    while buf.count(b"\n") < 3:
        buf += conn.recv(4096)
    assert json.loads(buf.splitlines()[2])["type"] == "reset"
    assert json.loads(buf.splitlines()[2])["reason"] == "test over"
    feed.close()
    assert feed.accepts is None and feed.server is None          # per connection
    conn.close()
    srv.close()
    os.unlink(path)


# ── scene: fused + map ───────────────────────────────────────────────────────

FUSED_MAP = {
    "type": "fused",
    "map": {"available": True, "state": "good", "reason": None, "age_s": 0.2, "match_confidence": 0.97},
    "speed_limit": {"limit_kmh": 50, "source": "map_implicit", "confidence": 0.5, "rule": "built_up",
                    "sign": None, "agreement": "map_only", "variable": False,
                    "vision": {"limit_kmh": None}, "map": {"limit_kmh": 50}},
    "built_up": {"value": True, "source": "map_tagged", "confidence": 0.9},
    "lanes": {"count": 2, "forward": 1, "backward": 1, "ego_index": 0, "oncoming_left": True,
              "source": "map:tagged+vision", "confidence": 0.8},
    "tunnel": {"in_tunnel": False, "exit_in_m": None, "entry_in_m": 840.0, "length_m": 2310.0, "source": "map"},
    "road": {"class": "primary", "name": "Ozimska", "ref": "DK46", "oneway": False, "roundabout": False,
             "link": False, "expressway": False},
    "ahead": {"horizon_m": 1500, "path_confidence": 0.62,
              "features": [{"kind": "crossing", "dist_m": 400.0},
                           {"kind": "traffic_signals", "dist_m": 230.0}],
              "junctions": [{"dist_m": 120.0, "kind": "junction", "turn_deg": 0.0, "minor": True, "confidence": 1.0},
                            {"dist_m": 412.0, "kind": "junction", "turn_deg": -2.0, "minor": False,
                             "confidence": 0.9},
                            {"dist_m": 655.4, "kind": "roundabout", "turn_deg": 10.0, "minor": False,
                             "confidence": 0.62},
                            {"dist_m": 910.0, "kind": "junction", "turn_deg": 0.0, "minor": False,
                             "confidence": 0.5}],
              "curves": [{"start_m": 300.0, "min_radius_m": 150.0, "direction": "left"}],
              "speed_limit_changes": [{"dist_m": 655.0, "limit_kmh": 90, "source": "maxspeed",
                                       "confidence": 0.81}]},
    "position": {"lat": 50.6, "lon": 17.9, "source": "gnss", "hacc_m": 2.13},
}

FUSED_NOMAP = {
    "type": "fused",
    "map": {"available": False, "state": "no_source", "reason": "no_position", "age_s": 0.3,
            "match_confidence": None},
    "speed_limit": {"limit_kmh": 40, "source": "sign", "confidence": 0.71, "sign": "B-33",
                    "agreement": "vision_only"},
    "built_up": {"value": True, "source": "env", "confidence": 0.4},
    "lanes": {"count": None, "ego_index": None, "source": "vision"},
    "tunnel": {"in_tunnel": False, "source": "vision"},
    "road": None, "ahead": None, "position": None,
}


def test_fused_to_scene_with_map():
    s = fused_to_scene(FUSED_MAP)
    assert s["ok"] is True and s["state"] == "good" and s["conf"] == 0.97 and "why" not in s
    assert s["limit"] == {"kmh": 50, "src": "map_implicit", "conf": 0.5, "agree": "map_only",
                          "rule": "built_up"}
    assert s["road"] == {"cls": "primary", "name": "Ozimska", "ref": "DK46"}
    assert s["lanes"] == {"n": 2, "fwd": 1, "back": 1, "ego": 0, "src": "map:tagged+vision", "c": 0.8,
                          "onc_left": True}
    assert s["tunnel"] == {"in": False, "entry_m": 840, "len_m": 2310, "src": "map"}
    assert s["built_up"] == {"v": True, "src": "map_tagged"}
    assert s["ahead"] == {"pc": 0.62,
                          "junction": {"m": 120, "kind": "junction", "turn": 0, "minor": True, "c": 1.0},
                          # the junctions that decide how far the path is trusted: no minor ones, within 700 m
                          "jn": [[412, 0.9, "j"], [655, 0.62, "r"]],
                          "curve": {"m": 300, "r": 150, "dir": "l"},
                          "limit": {"m": 655, "kmh": 90, "src": "maxspeed", "c": 0.81},
                          "feature": {"m": 230, "kind": "traffic_signals"}}
    assert s["pos"] == {"src": "gnss", "hacc": 2.1}
    assert len(json.dumps(s, separators=(",", ":"))) < 800


def test_fused_to_scene_without_map_keeps_vision_values():
    s = fused_to_scene(FUSED_NOMAP)
    assert s["ok"] is False and s["why"] == "no_position" and s["state"] == "no_source"
    assert s["limit"]["kmh"] == 40 and s["limit"]["src"] == "sign" and s["limit"]["agree"] == "vision_only"
    assert s["tunnel"] == {"in": False, "src": "vision"}
    assert "road" not in s and "ahead" not in s and "pos" not in s
    assert fused_to_scene({"type": "fused"}) is None


def test_map_input_to_scene():
    m = {"type": "map", "input": {"gnss": {"source": "synthetic", "fix": "3d", "age_s": 0.4},
                                  "motion": {"source": "vehicle", "tag": "synthetic", "age_s": 0.03}}}
    assert map_input_to_scene(m) == {"gnss": "synthetic", "fix": "3d", "fix_age": 0.4,
                                     "motion": "vehicle", "tag": "synthetic"}
    assert map_input_to_scene({"type": "map", "input": {"gnss": None, "motion": None}}) is None


MAP_SHAPE = {"s": [0.0, 20.0, 40.0, 60.0, 80.0, 100.0, 120.0],
             "x": [0.0, 20.0, 39.9, 59.6, 78.9, 97.4, 114.6],
             "y": [0.0, 0.2, 1.4, 4.6, 9.8, 17.3, 27.4],
             "curvature": [0.0] * 7}


def test_map_shape_to_scene():
    m = {"type": "map", "ahead": {"shape": MAP_SHAPE}}
    # every second point of mapd's 20 m grid, whole metres, y right as on the wire
    assert map_shape_to_scene(m) == {"ds": 40, "x": [0, 40, 79, 115], "y": [0, 1, 10, 27]}
    assert len(json.dumps(map_shape_to_scene(m), separators=(",", ":"))) < 60
    for bad in ({"type": "map"}, {"type": "map", "ahead": None}, {"type": "map", "ahead": {"shape": None}},
                {"type": "map", "ahead": {"shape": {"s": [0, 20], "x": [0, 20], "y": [0, 0]}}},
                {"type": "map", "ahead": {"shape": dict(MAP_SHAPE, y=[0.0, 1, None, 2, 3, 4, 5])}}):
        assert map_shape_to_scene(bad) is None, bad
    # a full shape (600 m) stays small
    full = {"s": [20.0 * i for i in range(31)], "x": [20.0 * i for i in range(31)], "y": [-123.4] * 31}
    assert len(json.dumps(map_shape_to_scene({"ahead": {"shape": full}}), separators=(",", ":"))) < 170


def test_builder_carries_map_key_with_staleness():
    b = SceneBuilder(SceneFeedConfig(max_hz=10.0))
    road = json.dumps({"type": "road", "seq": 1, "lane_lines": [], "road_edges": [], "leads": []})
    b.feed(json.dumps(FUSED_MAP), 10.0)
    b.feed(json.dumps({"type": "map", "input": {"gnss": {"source": "headunit", "fix": "3d"}},
                       "ahead": {"shape": MAP_SHAPE}}), 10.0)
    b.feed(road, 10.1)
    s = b.build(10.1)
    assert s["map"]["ok"] and s["map"]["in"] == {"gnss": "headunit", "fix": "3d"}
    assert s["map"]["shape"] == {"ds": 40, "x": [0, 40, 79, 115], "y": [0, 1, 10, 27]}
    assert "map_in" not in s and "map_shape" not in s
    # the road shape rides only while the fused map layer is in use
    b2 = SceneBuilder(SceneFeedConfig(max_hz=10.0))
    b2.feed(json.dumps(FUSED_NOMAP), 10.0)
    b2.feed(json.dumps({"type": "map", "ahead": {"shape": MAP_SHAPE}}), 10.0)
    b2.feed(road, 10.1)
    s2 = b2.build(10.1)
    assert s2["map"]["ok"] is False and "shape" not in s2["map"] and "map_shape" not in s2
    b.feed(road.replace('"seq": 1', '"seq": 2'), 14.0)       # fused / map now 4 s old
    s = b.build(14.0)
    assert "map" not in s


# ── REST route ───────────────────────────────────────────────────────────────

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _serve(feeder, token=None):
    import uvicorn
    from cyberpunk_computer.api import app as app_mod

    class Bridge:
        def attach_loop(self, loop):
            pass

        def latest_snapshot(self):
            return {"state": {}}

    app = app_mod.create_app(Bridge(), db=object(), auth_token=token, position_feeder=feeder)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           loop="asyncio"))
    server.install_signal_handlers = lambda: None
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return server, t, "http://127.0.0.1:%d/api/v1/position" % port


def _req(method, url, body=None, token=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    hdrs = {"Content-Type": "application/json"}
    if token:
        hdrs["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def test_rest_gnss_ingest_and_status():
    cfg = PositionFeederConfig(enabled=True)
    feeder = PositionFeeder(cfg, HeadunitGnssSource(cfg), tap=CanMotionTap(), feed=FakeFeed())
    server, t, base = _serve(feeder, token="s3cret")
    try:
        assert _req("POST", base + "/gnss", {"lat": 50.6, "lon": 17.9})[0] == 401
        code, r = _req("POST", base + "/gnss", [{"lat": 50.6, "lon": 17.9, "hacc_m": 4, "age_ms": 120},
                                                {"lat": 99, "lon": 17.9}], token="s3cret")
        assert code == 200 and r["accepted"] == 1 and r["rejected"][0]["index"] == 1 and r["has_fix"]
        assert _req("POST", base + "/gnss", raw=b"not json", token="s3cret")[0] == 400
        assert _req("POST", base + "/gnss", [{"lat": 1, "lon": 1}] * 21, token="s3cret")[0] == 400
        code, st = _req("GET", base, token="s3cret")
        assert code == 200 and st["source"] == "headunit" and st["gnss"]["accepted"] == 1
    finally:
        server.should_exit = True
        t.join(5)


def test_rest_gnss_refused_for_other_sources():
    cfg = PositionFeederConfig(enabled=True, source="none")
    feeder = PositionFeeder(cfg, NoGnssSource(), feed=FakeFeed())
    server, t, base = _serve(feeder)
    try:
        code, r = _req("POST", base + "/gnss", {"lat": 50.6, "lon": 17.9})
        assert code == 503 and r["detail"]["reason"] == "source_not_headunit"
    finally:
        server.should_exit = True
        t.join(5)


# ── env config ───────────────────────────────────────────────────────────────

def test_env_config(monkeypatch):
    from cyberpunk_computer.backend.__main__ import _build_position
    for k in list(os.environ):
        if k.startswith("BACKEND_POSITION_"):
            monkeypatch.delenv(k)
    assert _build_position() is None                          # off by default
    monkeypatch.setenv("BACKEND_POSITION_FEEDER", "1")
    c = _build_position()
    assert c.enabled and c.source == "headunit" and c.rundir == "/run/roadsense"
    assert c.yaw_inputs == ("wheels",) and c.motion_hz == 10.0
    monkeypatch.setenv("BACKEND_POSITION_SOURCE", "synthetic")
    monkeypatch.setenv("BACKEND_POSITION_YAW", "steer")
    monkeypatch.setenv("BACKEND_POSITION_MOTION_HZ", "100")
    monkeypatch.setenv("BACKEND_POSITION_SYNTHETIC_DROPOUTS", "10:20, bad, 30:35")
    c = _build_position()
    assert c.source == "synthetic" and c.yaw_inputs == ("steer",) and c.motion_hz == 50.0
    assert c.synthetic_dropouts == ((10.0, 20.0), (30.0, 35.0))
    monkeypatch.setenv("BACKEND_POSITION_SOURCE", "gps")
    assert _build_position() is None


# ── service wiring ───────────────────────────────────────────────────────────

def test_service_wiring_and_gating():
    from cyberpunk_computer.backend.service import BackendConfig, BackendService
    from cyberpunk_computer.state.store import Store

    class Twin:
        store = Store()

    assert BackendService(BackendConfig())._wire_position_feeder(Twin()) is None     # off by default
    svc = BackendService(BackendConfig(position_config=PositionFeederConfig(
        enabled=True, rundir=tempfile.mkdtemp())))
    twin = Twin()
    svc.twin = twin
    tap = svc._wire_position_feeder(twin)
    assert isinstance(tap, CanMotionTap) and svc.position_feeder.tap is tap
    pos = twin.store.state.roadsense.position
    assert pos["enabled"] and pos["source"] == "headunit" and pos["active"] is False

    class Rs:
        state = "off"
    svc.roadsense_power = Rs()
    svc._position_feeder_tick()
    assert not svc.position_feeder.active
    Rs.state = "running"
    svc._position_feeder_tick()
    assert svc.position_feeder.active
    svc.roadsense_power = None                 # no roadsense manager (dev): always on
    svc._position_feeder_tick()
    assert svc.position_feeder.active


def _gyro_frame(ab: int, cd: int) -> bytes:
    a, c = ab + 0x200, cd + 0x200
    return bytes([a >> 8, a & 0xFF, c >> 8, c & 0xFF, 0, 0, 0, 0])


def test_gyro_yaw_rate_opt_in_and_standstill_zero():
    from cyberpunk_computer.backend.position_feeder import decode_yaw_counts, GYRO_RAD_PER_COUNT
    assert decode_yaw_counts(_gyro_frame(-2, -7)) == (-2, -7)
    assert decode_yaw_counts(_gyro_frame(450, 0)) is None             # not a plausible yaw rate
    cfg = PositionFeederConfig(enabled=True, yaw_inputs=("wheels", "gyro"))
    tap = _tap_with()
    # turning left at 40 counts above the standstill offsets of both channels
    tap.on_frame(0x022, _gyro_frame(-2 + 40, -7 + 40), 0, 0.0, 100.0)
    m = tap.sample(100.02, cfg)
    assert m["yaw_rate_rps"] == pytest.approx(40 * GYRO_RAD_PER_COUNT, abs=1e-4)
    assert "wheel_speeds_mps" in m                                    # both ride along; mapd takes the gyro
    assert "yaw_rate_rps" not in _tap_with().sample(100.0, cfg)       # no 0x022 frame: field left out
    assert "yaw_rate_rps" not in tap.sample(100.02, PositionFeederConfig(enabled=True))   # default: not sent
    # standing: the offsets follow the reading, the yaw rate goes to zero
    tap = CanMotionTap()
    t = 200.0
    for _ in range(200):
        tap.speed = (0.0, t)
        tap.front = (0.0, 0.0, t)
        tap.rear = (0.0, 0.0, t)
        tap.on_frame(0x022, _gyro_frame(1, -4), 0, 0.0, t)            # the offsets drifted by +3
        m = tap.sample(t, cfg)
        t += 0.1
    assert abs(m["yaw_rate_rps"]) < 0.5 * GYRO_RAD_PER_COUNT
    assert tap.gyro_offsets[0] == pytest.approx(1.0, abs=0.1) and tap.gyro_offsets[1] == pytest.approx(-4.0, abs=0.1)
    # moving again: the learned offsets stay
    tap.speed = (30.0, t)
    tap.front = (30.0, 30.0, t)
    tap.rear = (30.0, 30.0, t)
    tap.on_frame(0x022, _gyro_frame(1 - 20, -4 - 20), 0, 0.0, t)
    assert tap.sample(t, cfg)["yaw_rate_rps"] == pytest.approx(-20 * GYRO_RAD_PER_COUNT, abs=3e-4)
