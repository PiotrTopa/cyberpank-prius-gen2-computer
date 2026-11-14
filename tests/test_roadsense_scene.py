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
    lights_to_scene,
    limit_to_scene,
    peek_type,
    resample,
    road_to_scene,
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
    assert lights_to_scene(msgs["lights"]) == {"state": "red", "d": 34.0, "y": 2.1}
    assert lights_to_scene({"type": "lights", "ego": None}) is None
    assert limit_to_scene(msgs["speed_limit"]) == {"kmh": 50, "src": "sign"}
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
    assert set(s) == {"seq", "lanes", "edges", "lead"}
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
    assert max(sizes) < 900, sizes                   # ~0.6-0.75 kB per event
    t0 = time.perf_counter()
    for _ in range(200):
        json.loads(json.dumps(env, separators=(",", ":")))
    assert (time.perf_counter() - t0) / 200 < 0.002


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
        assert server.paths[0] == "/ws?topics=road,lights,speed_limit,env"
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
