"""roadsense -> MFD road scene feed (backend.roadsense_scene).

Fixture ``tests/fixtures/roadsense_ws_2026-11-14.jsonl.gz``: WebSocket text
frames as roadsense's api sends them on ``/ws?topics=road,lights,speed_limit,env``.
The ``road`` messages are real modeld output (roadsense ``tools/modeld_replay``,
supercombo fp32 on ORT) for two recorded public rides: 12 frames of
warsaw-city (t = 433 s, a lead vehicle ~95 m ahead, prob ~0.9) and 4 frames of
krakow-lights (t = 135 s, no lead). ``env``, ``speed_limit`` and ``lights`` are
the contract examples of roadsense docs/ARCHITECTURE.md (no recording of those
exists in the repositories).
"""

import gzip
import json
import os
import socket
import threading
import time

import pytest

from cyberpunk_computer.backend.roadsense_scene import (
    EVENT_NAME,
    SCENE_XS,
    MiniWebSocket,
    RoadsenseSceneFeed,
    SceneBuilder,
    SceneFeedConfig,
    env_to_scene,
    lanes_to_scene,
    lead_to_scene,
    lights_to_scene,
    limit_to_scene,
    peek_type,
    resample,
    road_to_scene,
    rules_to_scene,
    view_to_scene,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "roadsense_ws_2026-11-14.jsonl.gz")


def load_lines():
    with gzip.open(FIXTURE, "rt") as f:
        return [ln.rstrip("\n") for ln in f if ln.strip()]


def roads():
    return [json.loads(ln) for ln in load_lines() if peek_type(ln) == "road"]


def cfg(**kw):
    c = SceneFeedConfig(host="127.0.0.1", port=1)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ── conversion ────────────────────────────────────────────────────────────────

def test_fixture_is_the_real_schema():
    lines = load_lines()
    types = [peek_type(ln) for ln in lines]
    assert set(types) == {"road", "lights", "speed_limit", "env"}
    r = roads()[0]
    assert len(r["lane_lines"]) == 4 and len(r["road_edges"]) == 2 and len(r["leads"]) == 3
    assert len(r["lane_lines"][0]["x"]) == 33 and r["lane_lines"][0]["x"][-1] == 192
    assert all(peek_type(ln) == json.loads(ln)["type"] for ln in lines)


def test_resample_matches_linear_interpolation():
    xs = [0.0, 10.0, 20.0, 200.0]
    ys = [0.0, 1.0, 3.0, 21.0]
    out = resample(xs, ys)
    assert len(out) == len(SCENE_XS)
    assert out[1] == 0.5 and out[4] == 3.0 and out[-1] == 11.0
    # past the last sample: shorter list; nulls: unusable
    assert resample([0, 10, 30], [0, 1, 3]) == [0.0, 0.5, 1.0, 1.5, 2.0, 3.0]
    assert resample([0, 10, 30], [0, None, 3]) is None
    assert resample([0, None, 30], [0, 1, 3]) is None
    assert resample([0], [1]) is None
    assert resample(None, [1, 2]) is None


def test_road_to_scene_maps_lanes_edges_and_lead():
    c = cfg()
    r = roads()[5]                                   # warsaw-city, lead present
    s = road_to_scene(r, c)
    assert s["seq"] == r["seq"]
    for i in range(4):
        ln = r["lane_lines"][i]
        if ln["prob"] < c.lane_min_prob:
            assert s["lanes"][i] is None
            continue
        assert s["lanes"][i]["prob"] == round(ln["prob"], 2)
        assert s["lanes"][i]["y"][0] == round(ln["y"][0], 2)      # x = 0
        assert len(s["lanes"][i]["y"]) == len(SCENE_XS)
    # y RIGHT positive, as roadsense: the left ego line is left (negative)
    assert s["lanes"][1]["y"][0] < 0 < s["lanes"][2]["y"][0]
    assert s["edges"][0]["y"][0] < s["lanes"][1]["y"][0]
    assert s["edges"][1]["y"][0] > s["lanes"][2]["y"][0]
    ld = r["leads"][0]
    assert s["lead"]["d"] == round(ld["x"][0], 1)
    assert s["lead"]["y"] == round(ld["y"][0], 2)
    assert s["lead"]["prob"] == round(ld["prob"], 2)
    assert s["lead"]["v_rel"] == round(ld["v"][0] - r["v_ego"], 1)
    assert "kind" not in s["lead"]                   # roadsense has no vehicle class


def test_missing_lead_and_lanes():
    c = cfg()
    r = roads()[-1]                                  # krakow-lights: lead prob ~0.15
    assert r["leads"][0]["prob"] < c.lead_min_prob
    assert road_to_scene(r, c)["lead"] is None
    r2 = json.loads(json.dumps(r))
    r2["lane_lines"][0]["prob"] = 0.05               # below the floor -> null
    r2["lane_lines"][1]["y"][5] = None               # non-finite where needed (x 4.69) -> null
    r2["road_edges"] = []
    del r2["leads"]
    s = road_to_scene(r2, c)
    assert s["lanes"][0] is None and s["lanes"][1] is None
    assert s["lanes"][2] is not None
    assert s["edges"] == [None, None] and s["lead"] is None
    # an invalid frame (all nulls) gives an empty but well-formed scene
    s = road_to_scene({"type": "road", "seq": 9, "valid": False}, c)
    assert s == {"seq": 9, "lanes": [None] * 4, "edges": [None] * 2, "lead": None}


def test_v_rel_falls_back_to_pose_vx():
    r = json.loads(json.dumps(roads()[5]))
    del r["v_ego"]
    s = road_to_scene(r, cfg())
    assert s["lead"]["v_rel"] == round(r["leads"][0]["v"][0] - r["pose"]["vx"], 1)
    del r["pose"]
    assert "v_rel" not in road_to_scene(r, cfg())["lead"]


def test_slow_topics():
    msgs = {peek_type(ln): json.loads(ln) for ln in load_lines()}
    assert lights_to_scene(msgs["lights"]) == {"state": "red", "d": 34.0, "y": 2.1, "rel": 0.71, "agree": 1}
    assert lights_to_scene({"type": "lights", "ego": None}) is None
    lim = limit_to_scene(msgs["speed_limit"])
    assert lim["kmh"] == 50 and lim["src"] == "sign"
    assert lim.get("conf") == round(msgs["speed_limit"]["confidence"], 2)
    assert limit_to_scene({"limit_kmh": None, "source": {"kind": "unknown"}}) is None
    assert env_to_scene(msgs["env"]) == {"light": "day", "place": "city_street",
                                         "weather": "overcast"}
    assert env_to_scene({"type": "env"}) is None
    fl = dict(msgs["lights"], ego=dict(msgs["lights"]["ego"], state="yellow", flashing=True))
    assert lights_to_scene(fl)["flashing"] is True


def test_peek_type():
    assert peek_type('{"type":"road","seq":1}') == "road"
    assert peek_type('{"type": "env", "seq": 1}') == "env"
    assert peek_type('{"seq":1,"x":[1,2,3]}') is None


# ── builder: rate, staleness, payload ─────────────────────────────────────────

def test_builder_rate_limit_and_seq():
    c = cfg(max_hz=10.0)
    b = SceneBuilder(c)
    rl = [ln for ln in load_lines() if peek_type(ln) == "road"]
    out = []
    t = 0.0
    for ln in rl:                                    # road at 20 Hz
        b.feed(ln, t)
        s = b.build(t)
        if s is not None:
            out.append((t, s))
        t += 0.05
    gaps = [b2[0] - a[0] for a, b2 in zip(out, out[1:])]
    assert min(gaps) >= 0.1 - 1e-9                   # <= 10 Hz
    assert len(out) >= len(rl) // 2 - 1
    # the same frame (a re-sent message) is never published twice
    b.feed(rl[0], t + 1.0)
    assert b.build(t + 1.0) is not None
    b.feed(rl[0], t + 2.0)
    assert b.build(t + 2.0) is None
    seqs = [s["seq"] for _, s in out]
    assert len(set(seqs)) == len(seqs)


def test_builder_sends_nothing_stale():
    c = cfg()
    b = SceneBuilder(c)
    lines = load_lines()
    msgs = {peek_type(ln): ln for ln in lines}
    rl = [ln for ln in lines if peek_type(ln) == "road"]
    b.feed(msgs["env"], 0.0)
    b.feed(msgs["speed_limit"], 0.0)
    b.feed(msgs["lights"], 0.0)
    b.feed(rl[0], 0.1)
    s = b.build(0.1)
    assert s["light"]["state"] == "red" and s["limit"]["kmh"] == 50 and s["env"]["light"] == "day"
    # lights 2.5 s old: key left out; limit/env still fresh
    b.feed(rl[1], 2.5)
    s = b.build(2.5)
    assert "light" not in s and "limit" in s and "env" in s
    # everything old
    b.feed(rl[2], 6.0)
    s = b.build(6.0)
    assert set(s) - {"path"} == {"seq", "lanes", "edges", "lead"}
    # a road frame that waited longer than road_max_age_s is dropped
    b.feed(rl[3], 7.0)
    assert b.build(7.0 + c.road_max_age_s + 0.1) is None
    # no new road frame: nothing, even with fresh slow topics
    b.feed(msgs["lights"], 9.0)
    assert b.build(9.0) is None


def test_payload_size_and_decode_budget():
    """Link budget: one envelope as it goes over ZMQ."""
    b = SceneBuilder(cfg())
    lines = load_lines()
    sizes = []
    t = 0.0
    for ln in lines:
        b.feed(ln, t)
        s = b.build(t)
        if s is not None:
            env = {"type": "event", "ts": 1794665136.123, "name": EVENT_NAME, "data": s}
            sizes.append(len(json.dumps(env, separators=(",", ":"))))
        t += 0.11
    assert sizes
    assert max(sizes) < 1000, sizes                  # ~0.7-0.85 kB per event (with the path)
    t0 = time.perf_counter()
    for _ in range(200):
        json.loads(json.dumps(env, separators=(",", ":")))
    assert (time.perf_counter() - t0) / 200 < 0.002


def rules_msg():
    """A ``rules`` message shaped like roadsense api/roadsense_api/rules.py
    builds it (docs/ARCHITECTURE.md, "api-derived topic rules")."""
    def eff(rid, sign, **kw):
        e = {"id": rid, "sign": sign, "name": rid, "category": "lasting", "value": None,
             "unit": None, "confidence": 0.7, "uncertain": [], "applies_to_car": True,
             "side": None}
        e.update(kw)
        return e
    return {"type": "rules", "seq": 9, "in_effect": [
        eff("speed_limit", "B-33", value=40, unit="km/h"),
        eff("built_up_area", "D-42", uncertain=["entry sign D-42 not observable; area from envd"]),
        eff("no_stopping", "B-36", side="right"),
        eff("no_overtaking_hgv", "B-26", applies_to_car=False),
        eff("living_street", "D-40", value=20, unit="km/h"),
        eff("no_left_turn", "B-21", category="momentary")],
        "recent": [], "ahead": [
            {"id": "roundabout_ahead", "sign": "A-8", "name": "roundabout ahead",
             "category": "warning", "expected_in_m": 60.4},
            {"id": "children_ahead", "sign": "A-17", "name": "children ahead",
             "category": "warning", "expected_in_m": 140.0}]}


LEAD_MSG = {"type": "lead", "seq": 1, "present": True, "mode": "hold", "track_id": 12, "age_s": 31.4,
            "source": "vision+ego", "dist_m": 3.61, "dist_std_m": 0.62, "dist_filter_std_m": 0.021,
            "lateral_m": -0.07, "v_rel_mps": 0.0, "v_rel_std_mps": 0.0, "v_lead_mps": 0.0,
            "a_lead_mps2": 0.0, "time_gap_s": None, "ttc_s": None, "standing": True,
            "pending_change": False, "prob": 0.99, "departed": False, "departure": None, "event": None,
            "ego": {"speed_mps": 0.0, "standstill": True, "source": "vehicle"},
            "vision": {"dist_m": 3.58, "lateral_m": -0.07, "prob": 0.99}}


def test_lead_to_scene():
    assert lead_to_scene(LEAD_MSG) == {"d": 3.6, "y": -0.07, "v_rel": 0.0, "a": 0.0, "prob": 0.99,
                                       "src": "tracked", "standing": True, "d_std": 0.62, "id": 12}
    moving = dict(LEAD_MSG, mode="track", standing=False, dist_m=24.3, v_rel_mps=-2.34, a_lead_mps2=-1.04,
                  time_gap_s=1.62, ttc_s=10.4, prob=None)
    assert lead_to_scene(moving) == {"d": 24.3, "y": -0.07, "v_rel": -2.3, "a": -1.0, "src": "tracked",
                                     "gap": 1.62, "d_std": 0.62, "ttc": 10.4, "id": 12}
    gone = dict(LEAD_MSG, present=False, mode="none", dist_m=None, lateral_m=None, v_rel_mps=None,
                a_lead_mps2=None, dist_std_m=None, standing=False, departed=True, track_id=None)
    assert lead_to_scene(gone) is None
    assert lead_to_scene(dict(LEAD_MSG, departed=True))["departed"] is True


def test_builder_prefers_the_tracked_lead_and_falls_back():
    c = cfg()
    b = SceneBuilder(c)
    rl = [ln for ln in load_lines() if peek_type(ln) == "road"]   # warsaw-city frames: raw lead ~95 m
    # older roadsense: no lead topic -> modeld's leads[0]
    b.feed(rl[0], 0.0)
    s = b.build(0.0)
    raw_d = s["lead"]["d"]
    assert raw_d > 50 and "lead_src" not in s and "src" not in s["lead"]
    # the tracker's lead replaces it while fresh
    b.feed(json.dumps(dict(LEAD_MSG, dist_m=94.2, standing=False)), 0.2)
    b.feed(rl[1], 0.2)
    s = b.build(0.2)
    assert s["lead_src"] == "tracked" and s["lead"]["d"] == 94.2 and s["lead"]["src"] == "tracked"
    # tracker says no lead: null, not the raw value
    b.feed(json.dumps(dict(LEAD_MSG, present=False, dist_m=None, departed=True)), 0.4)
    b.feed(rl[2], 0.4)
    s = b.build(0.4)
    assert s["lead"] is None and s["lead_src"] == "tracked" and s["lead_departed"] is True
    # lead topic stops (older than lead_max_age_s): back to leads[0]
    b.feed(rl[3], 0.4 + c.lead_max_age_s + 0.1)
    s = b.build(0.4 + c.lead_max_age_s + 0.1)
    assert "lead_src" not in s and "lead_departed" not in s
    assert s["lead"] is None or "src" not in s["lead"]


def test_rules_become_the_sign_lists():
    s = rules_to_scene(rules_msg())
    assert [e["id"] for e in s["on"]] == ["built_up_area", "no_stopping", "living_street",
                                          "no_left_turn"]          # no limit, no truck rule
    assert s["on"][0] == {"id": "built_up_area", "s": "D-42", "q": True}
    assert s["on"][1] == {"id": "no_stopping", "s": "B-36", "side": "r"}
    assert s["on"][2]["v"] == 20
    assert s["ahead"] == [{"id": "roundabout_ahead", "s": "A-8", "m": 60},
                          {"id": "children_ahead", "s": "A-17", "m": 140}]
    assert rules_to_scene({"type": "rules", "in_effect": [], "ahead": []}) is None
    many = {"in_effect": [{"id": "r%d" % i, "sign": "X"} for i in range(20)],
            "ahead": [{"id": "w%d" % i} for i in range(9)]}
    out = rules_to_scene(many)
    assert len(out["on"]) == 6 and len(out["ahead"]) == 3


def test_path_and_lead_acceleration():
    r = roads()[0]
    s = road_to_scene(r, cfg())
    assert s["path"]["y"][0] == pytest.approx(r["plan"]["y"][0], abs=0.02)
    assert 2 <= len(s["path"]["y"]) <= len(SCENE_XS)
    assert s["lead"]["a"] == round(r["leads"][0]["a"][0], 1)
    del r["plan"]
    assert "path" not in road_to_scene(r, cfg())


def test_signs_ride_along_and_expire():
    b = SceneBuilder(cfg())
    road = [ln for ln in load_lines() if peek_type(ln) == "road"]
    b.feed(json.dumps(rules_msg()), 0.0)
    b.feed(road[0], 0.0)
    s = b.build(0.0)
    assert s["signs"]["on"][0]["id"] == "built_up_area"
    env = {"type": "event", "ts": 1794665136.123, "name": EVENT_NAME, "data": s}
    assert len(json.dumps(env, separators=(",", ":"))) < 1300
    b.feed(road[1], 10.0)                       # rules older than rules_max_age_s
    assert "signs" not in b.build(10.0)


# ── WebSocket client and feed thread against a local server ──────────────────

class FakeWsServer:
    """Minimal RFC 6455 server on 127.0.0.1: sends ``lines`` as text frames
    (one every ``period`` s, looping), pings once, records pongs."""

    def __init__(self, lines, period=0.02, port=0):
        self.lines = lines
        self.period = period
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self.pongs = 0
        self.paths = []
        self._stop = threading.Event()
        self._clients = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        import base64
        import hashlib
        self.sock.settimeout(0.1)
        while not self._stop.is_set():
            try:
                c, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            head = b""
            while b"\r\n\r\n" not in head:
                head += c.recv(4096)
            lines = head.decode().split("\r\n")
            self.paths.append(lines[0].split(" ")[1])
            key = [ln.split(":", 1)[1].strip() for ln in lines if ln.lower().startswith("sec-websocket-key")][0]
            acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
            self.accepted += 1
            self._clients.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    @staticmethod
    def frame(opcode, payload):
        n = len(payload)
        if n < 126:
            return bytes([0x80 | opcode, n]) + payload
        if n < 65536:
            return bytes([0x80 | opcode, 126]) + n.to_bytes(2, "big") + payload
        return bytes([0x80 | opcode, 127]) + n.to_bytes(8, "big") + payload

    def _serve(self, c):
        c.settimeout(0.001)
        i = 0
        try:
            c.sendall(self.frame(0x9, b"hb"))
            while not self._stop.is_set():
                ln = self.lines[i % len(self.lines)]
                i += 1
                c.sendall(self.frame(0x1, ln.encode()))
                try:
                    data = c.recv(256)
                    if data and data[0] & 0x0F == 0xA:
                        self.pongs += 1
                except socket.timeout:
                    pass
                time.sleep(self.period)
        except OSError:
            pass

    def drop_clients(self):
        for c in self._clients:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass
        self._clients = []

    def close(self):
        self._stop.set()
        self.drop_clients()
        self.sock.close()


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def server():
    lines = load_lines()
    # serve with fresh, increasing seqs so every loop is "new" road data
    srv = FakeWsServer(lines)
    yield srv
    srv.close()


def test_mini_websocket_reads_frames_and_answers_ping(server):
    ws = MiniWebSocket.connect("127.0.0.1", server.port, "/ws?topics=road")
    got = []
    for _ in range(50):
        m = ws.recv(1.0)
        if m:
            got.append(m)
        if len(got) >= 5:
            break
    ws.close()
    assert server.paths == ["/ws?topics=road"]
    assert [peek_type(m) for m in got[:3]] == ["env", "speed_limit", "road"]
    assert len(got[2]) > 10000                        # 16-bit extended length frames
    assert wait_for(lambda: server.pongs >= 1)


def test_feed_publishes_while_active_and_reconnects(server):
    c = cfg(port=server.port, max_hz=10.0, reconnect_min_s=0.05, reconnect_max_s=0.2,
            road_max_age_s=5.0)
    got = []
    feed = RoadsenseSceneFeed(c, got.append)
    feed.start()
    try:
        time.sleep(0.3)
        assert server.accepted == 0 and not got        # idle: no connection at all
        feed.set_active(True)
        assert wait_for(lambda: len(got) >= 3)
        assert server.paths[0] == "/ws?topics=road,lights,speed_limit,env,rules,fused,map,lead,lanes,view"
        assert all(set(s) >= {"seq", "lanes", "edges", "lead"} for s in got)
        # roadsense restarts: the stream drops, the feed reconnects on its own
        n = len(got)
        server.drop_clients()
        assert wait_for(lambda: server.accepted >= 2)
        assert wait_for(lambda: len(got) > n + 2)
        # stack stopped (MFD off / roadsense not running): socket closed, silence
        feed.set_active(False)
        assert wait_for(lambda: not feed.stats["connected"])
        n = len(got)
        time.sleep(0.4)
        assert len(got) == n
    finally:
        feed.stop()


def test_feed_rate_is_capped(server):
    server.period = 0.01                               # ~100 messages/s offered
    c = cfg(port=server.port, max_hz=10.0, road_max_age_s=5.0)
    stamps = []
    feed = RoadsenseSceneFeed(c, lambda s: stamps.append(time.monotonic()))
    feed.start()
    feed.set_active(True)
    try:
        assert wait_for(lambda: len(stamps) >= 8)
    finally:
        feed.stop()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert min(gaps) >= 0.095


def test_feed_survives_no_server_and_backs_off():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                          # nothing listens there
    c = cfg(port=port, reconnect_min_s=0.05, reconnect_max_s=0.2)
    feed = RoadsenseSceneFeed(c, lambda s: None)
    feed.start()
    feed.set_active(True)
    try:
        assert wait_for(lambda: feed.stats["connect_errors"] >= 3)
        assert feed.stats["connects"] == 0
        # the server comes up: the feed connects without being told
        srv = FakeWsServer(load_lines(), port=port)
        try:
            assert wait_for(lambda: feed.stats["connects"] == 1, timeout=3.0)
        finally:
            srv.close()
    finally:
        feed.stop()


def test_feed_reconnects_on_silence():
    class Silent:
        def __init__(self):
            self.closed = False

        def recv(self, timeout):
            time.sleep(min(timeout, 0.01))
            return None

        def close(self):
            self.closed = True

    conns = []

    def connect():
        conns.append(Silent())
        return conns[-1]

    feed = RoadsenseSceneFeed(cfg(silence_s=0.1, reconnect_min_s=0.01), lambda s: None,
                              connect=connect)
    feed.start()
    feed.set_active(True)
    try:
        assert wait_for(lambda: len(conns) >= 3)
        assert conns[0].closed
    finally:
        feed.stop()


# ── backend wiring ────────────────────────────────────────────────────────────

def test_service_gates_the_feed_on_roadsense_and_mfd():
    from cyberpunk_computer.backend.service import BackendService

    class RS:
        state = "running"

    class MFD:
        state = "on"

    class Feed:
        active = None

        def set_active(self, on):
            self.active = on

    svc = BackendService.__new__(BackendService)
    svc.roadsense_scene = Feed()
    svc.roadsense_power = RS()
    svc.mfd_power = MFD()
    svc._roadsense_scene_tick()
    assert svc.roadsense_scene.active is True
    for rs_state in ("grace", "starting", "stopping", "off", "camera_on"):
        svc.roadsense_power.state = rs_state
        svc._roadsense_scene_tick()
        assert svc.roadsense_scene.active is False, rs_state
    svc.roadsense_power.state = "running"
    for mfd_state in ("off", "booting", "shutting_down", "powering_off"):
        svc.mfd_power.state = mfd_state
        svc._roadsense_scene_tick()
        assert svc.roadsense_scene.active is False, mfd_state
    svc.mfd_power.state = "grace"
    svc._roadsense_scene_tick()
    assert svc.roadsense_scene.active is True
    svc.mfd_power = None                               # no MFD manager (bench): roadsense decides
    svc._roadsense_scene_tick()
    assert svc.roadsense_scene.active is True
    svc.roadsense_power = None
    svc._roadsense_scene_tick()
    assert svc.roadsense_scene.active is False


def test_env_switch(monkeypatch):
    from cyberpunk_computer.backend.__main__ import _build_roadsense_scene
    monkeypatch.delenv("BACKEND_ROADSENSE_SCENE", raising=False)
    monkeypatch.delenv("BACKEND_ROADSENSE_SCENE_HZ", raising=False)
    c = _build_roadsense_scene()
    assert c.enabled and c.max_hz == 10.0
    monkeypatch.setenv("BACKEND_ROADSENSE_SCENE", "0")
    monkeypatch.setenv("BACKEND_ROADSENSE_SCENE_HZ", "25")
    c = _build_roadsense_scene()
    assert not c.enabled and c.max_hz == 10.0
    monkeypatch.setenv("BACKEND_ROADSENSE_SCENE_HZ", "5")
    assert _build_roadsense_scene().max_hz == 5.0


def test_zmq_latest_event_slot_replaces_unsent():
    from cyberpunk_computer.backend.zmq_server import ZmqServer
    z = ZmqServer(bridge=None)
    z.enqueue_latest_event({"type": "event", "name": EVENT_NAME, "data": {"seq": 1}})
    z.enqueue_latest_event({"type": "event", "name": EVENT_NAME, "data": {"seq": 2}})
    assert z.stats["latest_replaced"] == 1
    assert z._latest_events[EVENT_NAME]["data"]["seq"] == 2
    assert z._event_box.empty()                        # the FIFO event queue is untouched


def test_zmq_publishes_scene_to_a_subscriber():
    zmq = pytest.importorskip("zmq")
    from cyberpunk_computer.backend.zmq_server import ZmqServer
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    pub = s.getsockname()[1]
    s.close()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    rep = s.getsockname()[1]
    s.close()
    z = ZmqServer(bridge=None, host="127.0.0.1", pub_port=pub, rep_port=rep)
    z.start()
    ctx = zmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.setsockopt(zmq.RCVTIMEO, 3000)
    sub.connect("tcp://127.0.0.1:%d" % pub)
    try:
        raw = None
        end = time.monotonic() + 3.0
        while raw is None and time.monotonic() < end:     # slow-joiner: resend
            z.enqueue_latest_event({"type": "event", "ts": 1.0, "name": EVENT_NAME,
                                    "data": {"seq": 7, "lead": None}})
            try:
                raw = sub.recv_string(flags=zmq.NOBLOCK)
            except zmq.Again:
                time.sleep(0.1)
        assert raw == '{"type":"event","ts":1.0,"name":"roadsense_scene","data":{"seq":7,"lead":null}}'
    finally:
        sub.close(linger=0)
        z.stop()


LANES_X = [0.0, 3.0, 12.0, 27.0, 48.0, 75.0, 108.0]


def lanes_msg(present=(False, True, True, False), conf=(0.1, 0.92, 0.22, 0.05), edges=(True, False), seq=7):
    def line(i, on, c=None):
        return {"present": on, "mode": "track" if on else "none", "conf": c, "prob": 0.5,
                "y": [(-5.25, -1.75, 1.75, 5.25, -6.5, 6.5)[i]] * len(LANES_X) if on else None, "y_std": None}
    return {"type": "lanes", "seq": seq, "ts_mono_ns": 1, "x": LANES_X,
            "lines": [line(i, present[i], conf[i]) for i in range(4)],
            "edges": [line(4 + i, edges[i]) for i in range(2)],
            "lane_to_left": False, "lane_to_right": False, "lane_width_m": 3.5, "prior": None, "source": "vision"}


def test_lanes_to_scene():
    s = lanes_to_scene(lanes_msg())
    assert s["lanes"][0] is None and s["lanes"][3] is None
    assert s["lanes"][1] == {"y": [-1.75] * len(SCENE_XS), "prob": 0.92}
    # a present line is never sent below the screen's drawing threshold: its presence is the tracker's
    assert s["lanes"][2] == {"y": [1.75] * len(SCENE_XS), "prob": 0.3}
    assert s["edges"] == [{"y": [-6.5] * len(SCENE_XS)}, None]
    assert lanes_to_scene({"type": "lanes"}) is None
    assert lanes_to_scene({"type": "lanes", "x": LANES_X, "lines": "x", "edges": []}) is None


def test_builder_prefers_the_tracked_lanes_and_falls_back():
    c = cfg(max_hz=1000.0)
    b = SceneBuilder(c)
    rl = [ln for ln in load_lines() if peek_type(ln) == "road"]
    # older roadsense: no lanes topic -> the raw lane lines and edges
    b.feed(rl[0], 0.0)
    s = b.build(0.0)
    raw = s["lanes"]
    assert "lanes_src" not in s and s["edges"][0] is not None
    # the tracker's geometry replaces both while fresh, including "this line is not there"
    b.feed(json.dumps(lanes_msg()), 0.2)
    b.feed(rl[1], 0.2)
    s = b.build(0.2)
    assert s["lanes_src"] == "tracked"
    assert s["lanes"][0] is None and s["lanes"][1]["y"][0] == -1.75 and s["lanes"][2]["prob"] == 0.3
    assert s["edges"] == [{"y": [-6.5] * len(SCENE_XS)}, None]
    # stale tracker output (roadsense api restarted, topic gone): back to the raw lines
    b.feed(rl[2], 1.0)
    s = b.build(1.0)
    assert "lanes_src" not in s and [x is None for x in s["lanes"]] == [x is None for x in raw]
    # an unusable lanes message never blanks the road
    b.feed(json.dumps({"type": "lanes", "seq": 9}), 1.2)
    b.feed(rl[3], 1.2)
    s = b.build(1.2)
    assert "lanes_src" not in s and any(x is not None for x in s["lanes"])


def test_light_lane_assignment_fields():
    """roadsense >= 2026-11-17: two signal groups, the ego light chosen by lane."""
    ego = {"track_id": 1, "state": "green", "dist_m": 31.0, "lateral_m": -0.7, "relevance": 0.6, "agree": 1,
           "held": False, "amb": False,
           "assign": {"conflict": True, "confidence": 0.88, "basis": "lanes", "slot": -0.2,
                      "alt": {"track_id": 3, "state": "red", "side": "left", "slot": -1.6}}}
    out = lights_to_scene({"type": "lights", "ego": ego})
    assert out["state"] == "green" and out["c"] == 0.88 and out["alt"] == {"s": "red", "side": "l"}
    assert "q" not in out and "held" not in out
    amb = lights_to_scene({"type": "lights", "ego": dict(ego, amb=True, held=True)})
    assert amb["q"] is True and amb["held"] is True
    # no conflict: no alt, the confidence still rides along
    one = lights_to_scene({"type": "lights", "ego": dict(ego, assign={"conflict": False, "confidence": 1.0, "alt": None})})
    assert "alt" not in one and one["c"] == 1.0


def _map_msg(jv):
    return {"type": "map", "available": True, "state": "good", "input": {"gnss": {"source": "headunit", "fix": "3d"}},
            "ahead": {"junction_view": jv}}


def test_junction_view_goes_out_compact_and_only_with_a_usable_map():
    from cyberpunk_computer.backend.roadsense_scene import map_junction_to_scene
    arm = lambda ang, **kw: dict({"angle_deg": ang, "class": "secondary", "name": None, "ref": None,   # noqa: E731
                                  "exit": True, "entry": True, "path": False, "minor": False}, **kw)
    jv = {"kind": "junction", "dist_m": 171.4, "confidence": 0.58, "control": "signals",
          "approach": {"lanes": 2, "lanes_source": "tagged"},
          "arms": [arm(-91.0, name="1 Maja", path=True), arm(7.0, **{"class": "tertiary"}),
                   arm(90.0, exit=False, **{"class": "service"}, minor=True)], "ring": None}
    out = map_junction_to_scene(_map_msg(jv))
    assert out == {"k": "j", "m": 171, "c": 0.58, "ctl": "s", "ln": 2, "to": "1 Maja",
                   "arms": [[-91, "xep", 0, 0], [7, "xe", 1, 0], [90, "em", 3, 0]]}
    rb = {"kind": "roundabout", "dist_m": 52.0, "confidence": 1.0, "control": "give_way",
          "approach": {"lanes": 1, "lanes_source": "assumed"},
          "arms": [arm(124.0, exit_no=1), arm(-87.0, path=True, ref="DW414", exit_no=2),
                   arm(180.0, exit=False, approach=True)],
          "ring": {"radius_m": 8.2, "exits": 2, "path_exit_no": 2, "on_ring": False, "car_angle_deg": None}}
    out = map_junction_to_scene(_map_msg(rb))
    assert out["k"] == "r" and out["ctl"] == "g" and "ln" not in out and out["to"] == "DW414"
    assert out["arms"] == [[124, "xe", 0, 1], [-87, "xep", 0, 2], [180, "ea", 0, 0]]
    assert out["ring"] == {"r": 8, "n": 2, "p": 2}
    on = dict(rb, ring=dict(rb["ring"], on_ring=True, car_angle_deg=70.0))
    assert map_junction_to_scene(_map_msg(on))["ring"] == {"r": 8, "n": 2, "p": 2, "car": 70, "on": True}
    for bad in (None, {}, {"kind": "junction", "dist_m": 10, "confidence": 0.9, "arms": []}):
        assert map_junction_to_scene(_map_msg(bad)) is None
    assert map_junction_to_scene({"type": "map", "ahead": None}) is None
    # roadsense 2.0: the junction comes with the display channel (view.road.junction: the same object with the
    # distance carried to the frame and an "approximate" mark); it rides inside "map" while the fused map layer
    # is in use and roadsense trusts the position, never without. The `map` topic alone draws nothing.
    from cyberpunk_computer.backend.roadsense_scene import junction_to_scene
    assert junction_to_scene(dict(jv, approximate=True, why=["dead_reckoning"]))["ap"] is True
    assert "ap" not in junction_to_scene(dict(jv, approximate=False))
    for ok, trusted, want in ((True, True, True), (False, True, False), (True, False, False)):
        b = SceneBuilder(SceneFeedConfig())
        b.feed(json.dumps({"type": "fused", "map": {"available": ok, "state": "good" if ok else "no_source"}}), 0.0)
        b.feed(json.dumps(_map_msg(jv)), 0.0)
        b.feed(json.dumps(view_msg(1, 1_000_000_000, road=view_road(junction=dict(jv, approximate=True),
                                                                    trusted=trusted))), 0.0)
        scene = b.build(0.0)
        assert ("jv" in scene["map"]) is want
        assert "map_jv" not in scene
        if want:
            assert scene["map"]["jv"]["ap"] is True and scene["map"]["jv"]["m"] == 171
    b = SceneBuilder(SceneFeedConfig())                       # without the display channel: no junction picture
    b.feed(json.dumps({"type": "fused", "map": {"available": True, "state": "good"}}), 0.0)
    b.feed(json.dumps(_map_msg(jv)), 0.0)
    b.feed(next(ln for ln in load_lines() if peek_type(ln) == "road"), 0.0)
    assert "jv" not in b.build(0.0)["map"]


# ── fused road geometry (roadsense view.road -> scene "geo") ─────────────────

def roadgeom_msg(**kw):
    xs = [0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0] + [60.0 + 4.0 * i for i in range(1, 60)]
    n = len(xs)
    msg = {"type": "roadgeom", "seq": 5, "source": "vision+map", "lane_width_m": 3.42,
           "map": {"used": True, "why": None, "synthetic": False},
           "centre": {"s": xs, "x": xs, "y": [0.0] * 7 + [0.0 if x < 150 else round((x - 150) ** 2 / 300.0, 2) for x in xs[7:]],
                      "conf": [0.9] * 30 + [0.55] * (n - 30), "src": "vvvvvbb" + "m" * (n - 7), "vision_to_m": 60.0,
                      "end": "horizon"},
           "branches": [{"dist_m": 80.0, "pts": [[80.0, 0.0], [80.0, 6.0], [80.0, 12.0], [80.0, 45.0]], "class": "tertiary",
                         "minor": False, "exit": True, "entry": False, "lanes": 2, "ring": False, "way": False},
                        {"dist_m": 150.0, "pts": [[150.0, 0.0], [150.0, -30.0]], "class": "service", "minor": True,
                         "exit": True, "entry": True, "lanes": 1, "ring": False, "way": False}],
           "ring": None, "way": {"source": "map", "confidence": 0.55}}
    msg.update(kw)
    return msg


def test_roadgeom_becomes_the_compact_geo_block():
    from cyberpunk_computer.backend.roadsense_scene import roadgeom_to_scene
    g = roadgeom_to_scene(roadgeom_msg())
    assert g["v"] == 60.0 and g["w"] == 3.4
    assert g["p"][0][:2] == [60.0, 0.0]                      # starts where the camera's part ends
    assert len(g["p"]) <= 28 and g["p"][-1][0] > 280.0
    assert [q for _, _, q in g["p"]][0] == 9 and g["p"][-1][2] == 5
    assert any(a[2] == 9 and b[2] == 5 and b[0] - a[0] <= 40.0 for a, b in zip(g["p"], g["p"][1:]))   # the confidence step
    assert g["s"] > 150.0                                    # the path steers the picture into the bend
    assert g["br"] == [["x", 1, 6.0, [80.0, 0.0, 80.0, 6.0, 80.0, 12.0, 80.0, 45.0]]]     # the far service road is left out
    assert "ring" not in g and "way" not in g
    assert len(json.dumps(g, separators=(",", ":"))) < 500
    # a ring: the steering ends at its entry; intent is passed on
    ring = roadgeom_msg(ring={"centre": [104.0, 0.0], "radius_m": 14.0, "lanes": None, "on_ring": False, "entry_m": 90.0,
                              "exit_m": 134.0}, way={"source": "intent", "turn": "right", "branch": 0})
    g = roadgeom_to_scene(ring)
    assert g["ring"] == [104.0, 0.0, 14.0, 4.5, 0] and g["s"] <= 92.0 and g["way"] == "i"
    # not used, synthetic, or the camera's part alone: no block
    assert roadgeom_to_scene(roadgeom_msg(map={"used": False, "why": "disagree"})) is None
    assert roadgeom_to_scene(roadgeom_msg(map={"used": True, "synthetic": True})) is None
    vis = roadgeom_msg()
    vis["centre"]["src"] = "v" * len(vis["centre"]["x"])
    g = roadgeom_to_scene(vis)                               # ... but the junction's roads still come (turn signal: the lane ends there)
    assert len(g["p"]) == 2 and g["br"]
    vis["branches"] = []
    assert roadgeom_to_scene(vis) is None
    assert roadgeom_to_scene({"type": "roadgeom"}) is None


def view_road(junction=None, trusted=True, approximate=False, synthetic=False, lanes=None, **geom):
    """A `view.road` object (roadsense 2.0): the fused geometry of roadgeom_msg() plus position, junction, lanes."""
    rd = roadgeom_msg(**geom)
    rd.pop("type")
    rd.update(position={"state": "good", "trusted": trusted, "approximate": approximate, "synthetic": synthetic,
                        "hacc_m": 4.0, "why": ["dead_reckoning"] if approximate else []},
              junction=junction,
              lanes=lanes or {"count": 2, "forward": 1, "backward": 1, "ego_index": 0, "likely_index": None,
                              "probs": [1.0], "probs_from": "left", "confidence": 1.0, "oncoming_left": True,
                              "count_source": "map:tagged", "turn": None})
    return rd


def test_builder_attaches_geo_only_with_a_usable_fresh_map():
    """The road picture comes with the display channel (view.road), of the same frame as the lanes."""
    b = SceneBuilder(cfg(max_hz=50.0))
    fused = {"type": "fused", "map": {"available": True, "state": "good", "match_confidence": 0.9}}

    def scene_at(t, seq, **road):
        b.feed(json.dumps(view_msg(seq, int(t * 1e9), road=view_road(**road))), t)
        return b.build(t)

    assert "geo" not in scene_at(0.0, 1)                     # no map block yet
    b.feed(json.dumps(fused), 0.1)
    assert scene_at(0.1, 2)["geo"]["v"] == 60.0
    assert "geo" not in scene_at(0.2, 3, trusted=False)      # roadsense does not trust the position
    assert "geo" not in scene_at(0.3, 4, map={"used": False, "why": "disagree"})    # camera and map disagree
    g = scene_at(0.4, 5, approximate=True)["geo"]
    assert g["ap"] is True                                   # dead reckoning: side roads are "about there"
    assert "geo" not in scene_at(0.5, 6, synthetic=True)     # a synthetic position never reaches the picture
    b.feed(json.dumps({"type": "map", "state": "good", "input": {"gnss": {"source": "synthetic"}, "synthetic": True}}), 0.6)
    assert "geo" not in scene_at(0.6, 7)


def test_lane_boxes_come_from_the_view_with_turn_arrows():
    from cyberpunk_computer.backend.roadsense_scene import road_lanes_to_scene
    lanes = {"count": 4, "forward": 3, "backward": 1, "ego_index": None, "likely_index": 1,
             "probs": [0.1, 0.6, 0.3], "probs_from": "left", "confidence": 0.0, "oncoming_left": None,
             "count_source": "map:tagged",
             "turn": {"lanes": [["left"], ["through"], ["through", "right"]], "arrows": ["l", "t", "tr"], "count": 3,
                      "dist_m": 80.0, "junction_m": 160.0, "path": [False, True, True], "here": False}}
    ln = road_lanes_to_scene({"lanes": lanes})
    assert ln == {"n": 4, "fwd": 3, "back": 1, "lk": 1, "src": "map:tagged", "c": 0.0, "p": [0.1, 0.6, 0.3],
                  "tl": ["l", "t", "tr"], "tp": [0, 1, 1]}
    far = dict(lanes, turn=dict(lanes["turn"], dist_m=300.0))
    assert "tl" not in road_lanes_to_scene({"lanes": far})           # announced, but not near yet
    other = dict(lanes, turn=dict(lanes["turn"], arrows=["l", "t"], dist_m=0.0))
    assert "tl" not in road_lanes_to_scene({"lanes": other})         # another number of lanes than the boxes
    assert road_lanes_to_scene({"lanes": dict(lanes, forward=None)}) is None
    # in the builder they replace the fused block's lanes (the display channel decides what is lit)
    b = SceneBuilder(cfg(max_hz=50.0))
    b.feed(json.dumps({"type": "fused", "map": {"available": True, "state": "good"},
                       "lanes": {"count": 2, "forward": 2, "backward": 0, "ego_index": 0}}), 0.0)
    b.feed(json.dumps(view_msg(1, 1_000_000_000, road=view_road(lanes=lanes))), 0.0)
    sc = b.build(0.0)
    assert sc["map"]["lanes"]["fwd"] == 3 and sc["map"]["lanes"]["tl"] == ["l", "t", "tr"]
    b.feed(json.dumps(view_msg(2, 1_100_000_000, road=view_road(lanes=lanes, trusted=False))), 0.1)
    assert b.build(0.1)["map"]["lanes"] is None


# ── roadsense 2.0 display channel (topic view, lane L19) ─────────────────────

VIEW_X = [0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 100.0, 120.0]


def view_msg(seq, ts_ns, lead_x=12.0, road=None):
    return {"type": "view", "seq": seq, "ts_mono_ns": ts_ns, "x": VIEW_X,
            "lines": [{"id": "lane:1", "kind": "lane_line", "y": [-1.7] * 12 + [None, None], "alpha": 1.0, "conf": 0.9},
                      {"id": "lane:2", "kind": "lane_line", "y": [1.7] * 14, "alpha": 0.4, "conf": 0.5},
                      {"id": "edge:1", "kind": "road_edge", "y": [4.0] * 14, "alpha": 1.0}],
            "path": {"id": "path", "kind": "path", "y": [0.0] * 8 + [None] * 6},
            "objects": [{"id": "lead:3", "kind": "vehicle", "role": "lead", "x": lead_x, "y": 0.2, "vx": -1.0,
                         "alpha": 1.0, "attrs": {}},
                        {"id": "light:7", "kind": "traffic_light", "role": "ego", "x": 41.0, "y": 2.5, "vx": -10.0,
                         "vy": 0.0, "alpha": 0.6, "attrs": {"state": "red", "ego_state": "red", "held": True,
                                                            "alt": {"state": "green", "side": "left"}}},
                        {"id": "light:8", "kind": "traffic_light", "role": "other", "x": 45.0, "y": -3.0,
                         "attrs": {"state": "green"}}],
            "road": road}


def test_view_to_scene():
    vw = view_to_scene(view_msg(5, 123_456_789_000))
    assert vw["t"] == 123_456
    assert vw["lanes"][0] is None and vw["lanes"][1]["y"][:3] == [-1.7, -1.7, -1.7]
    assert len(vw["lanes"][1]["y"]) == 11 - 1                       # cut where the line ends (100 m not drawn)
    assert vw["lanes"][2]["f"] == 0.4 and "f" not in vw["lanes"][1]
    assert vw["edges"][1]["y"][0] == 4.0 and vw["edges"][0] is None
    assert vw["path"]["y"][-1] == 0.0 and len(vw["path"]["y"]) == 7    # to 40 m
    assert vw["lead"] == {"id": "lead:3", "d": 12.0, "y": 0.2, "v": -1.0}
    lt = vw["light"]
    assert (lt["id"], lt["state"], lt["d"], lt["y"], lt["vx"], lt["f"], lt["held"]) == ("light:7", "red", 41.0, 2.5,
                                                                                  -10.0, 0.6, True)
    assert lt["alt"] == {"s": "green", "side": "l"}


def test_scene_is_built_per_view_message_with_its_own_frame():
    """roadsense sends road, lead, lanes, view per frame. Built on the road message the scene used to
    carry the previous frame's lead and lanes now and then (4.8 % of the scenes on the 2026-11-17 drive); built on
    the view message everything is of one frame, and the 15 kB road message is not decoded at all."""
    b = SceneBuilder(cfg(max_hz=50.0))
    lead = {"type": "lead", "seq": 1, "present": True, "dist_m": 12.0, "lateral_m": 0.2, "v_rel_mps": -1.0,
            "track_id": 3}
    lanes = {"type": "lanes", "seq": 1, "x": VIEW_X, "lines": [{"present": False}] * 4, "edges": [{"present": False}] * 2}
    b.feed('{"type":"road","seq":1,"broken json', 10.0)        # never decoded in view mode
    b.feed(json.dumps(view_msg(0, 9_900_000_000)), 9.95)        # an earlier frame: view mode on
    b.build(9.95)
    b.feed('{"type":"road","seq":1,"broken json', 10.0)
    assert b.build(10.0) is None                                # the frame is not complete yet
    b.feed(json.dumps(lead), 10.001)
    b.feed(json.dumps(lanes), 10.002)
    b.feed(json.dumps(view_msg(1, 10_000_000_000)), 10.003)
    sc = b.build(10.003)
    assert sc["seq"] == 1 and sc["lead"]["d"] == 12.0 and sc["lead_src"] == "tracked"
    assert sc["vw"]["t"] == 10_000 and sc["vw"]["light"]["state"] == "red"
    assert sc["path"]["y"][0] == 0.0
    assert b.decode_errors == 0
    assert b.build(10.05) is None                               # nothing new
