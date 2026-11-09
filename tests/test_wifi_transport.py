"""WifiTransport over real loopback sockets, end to end through the registry
with a fake satellite that authenticates using a prius-satkey blob."""

import json
import socket
import threading
import time

import pytest

pytest.importorskip("cryptography")

from cyberpunk_computer.backend import satellite_keychain as kc_mod
from cyberpunk_computer.backend.satellite_auth import SatelliteAuthClient, SatelliteAuthError
from cyberpunk_computer.backend.satellite_keychain import Keychain, b64e
from cyberpunk_computer.backend.satellite_registry import SatelliteRegistry
from cyberpunk_computer.io.wifi_transport import WifiTransport
from cyberpunk_computer.state.store import Store


class FakeSat:
    """Satellite side: speaks NDJSON over one TCP socket, answers auth."""

    def __init__(self, blob):
        self.client = SatelliteAuthClient(blob)
        self.received = []
        self.sock = None
        self.closed = threading.Event()
        self.authed = threading.Event()
        self.failed = None
        self._lock = threading.Lock()

    def attach(self, sock):
        self.sock = sock
        threading.Thread(target=self._rx, daemon=True).start()

    def send(self, payload):
        obj = {"d": payload}
        if self.client.authenticated:
            obj["id"] = self.client.sat_id
        with self._lock:
            self.sock.sendall((json.dumps(obj) + "\n").encode())

    def _rx(self):
        buf = b""
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    obj = json.loads(raw)
                    self.received.append(obj)
                    d = obj["d"]
                    try:
                        if d.get("a") == "auth":
                            self.send(self.client.respond(d))
                        elif d.get("a") in ("auth_ok", "auth_fail"):
                            self.client.check_result(d)
                            self.authed.set()
                    except SatelliteAuthError as exc:
                        self.failed = str(exc)
        except OSError:
            pass
        finally:
            self.closed.set()


def pump(reg, cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        reg.tick()
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def kc(tmp_path):
    k = Keychain(str(tmp_path / "keys"))
    k.init_car_key()
    return k


@pytest.fixture
def car(kc):
    wifi = WifiTransport(bind="127.0.0.1", udp_port=0, tcp_port=0,
                         broadcast_addr="127.0.0.1")
    store = Store()
    reg = SatelliteRegistry(store, transports_enabled=("wifi",), keychain=kc,
                            transports={"wifi": wifi})
    reg.start()
    yield store, reg, wifi
    reg.stop()


def test_probe_answered_with_car_fp(car, kc):
    store, reg, wifi = car
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    s.sendto(b'{"prius_car":1}\n', ("127.0.0.1", wifi.udp_port))
    data, _ = s.recvfrom(1024)
    reply = json.loads(data)
    assert reply == {"prius_car": 1, "car_fp": kc.car_fp, "port": wifi.tcp_port}
    s.close()


def test_beacon_then_car_dials_and_authenticates(car, kc):
    store, reg, wifi = car
    with kc.transaction():
        blob = kc.new_satellite("garage", "wifi")
    sat = FakeSat(blob)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    sport = srv.getsockname()[1]

    def accept():
        conn, _ = srv.accept()
        sat.attach(conn)
    threading.Thread(target=accept, daemon=True).start()

    b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    b.sendto(json.dumps({"prius_sat": 1, "fp": blob["fp"], "name": "garage",
                         "port": sport}).encode(), ("127.0.0.1", wifi.udp_port))
    b.close()

    assert pump(reg, lambda: sat.authed.is_set()), "handshake did not complete"
    assert sat.client.sat_id == 400 and sat.failed is None
    assert pump(reg, lambda: 400 in store.state.satellites.nodes
                and store.state.satellites.nodes[400].online)
    node = store.state.satellites.nodes[400]
    assert node.authenticated and node.address == f"127.0.0.1:{sport}"

    # car -> satellite command through the Wi-Fi queue
    reg.send(400, {"a": "relay", "on": True})
    assert pump(reg, lambda: any(o["d"].get("a") == "relay" for o in sat.received))
    cmd = [o for o in sat.received if o["d"].get("a") == "relay"][0]
    assert cmd == {"id": 400, "d": {"a": "relay", "on": True}}
    srv.close()
    sat.sock.shutdown(socket.SHUT_RDWR)
    assert pump(reg, lambda: not store.state.satellites.nodes[400].online)


def test_satellite_dials_car(car, kc):
    store, reg, wifi = car
    with kc.transaction():
        blob = kc.new_satellite("trailer", "wifi")
    sat = FakeSat(blob)
    sat.attach(socket.create_connection(("127.0.0.1", wifi.tcp_port)))
    assert pump(reg, lambda: sat.authed.is_set())
    assert pump(reg, lambda: store.state.satellites.nodes.get(400) is not None
                and store.state.satellites.nodes[400].online)
    st = wifi.status()
    assert st.available and st.state == "listening"
    sat.sock.shutdown(socket.SHUT_RDWR)


def test_unknown_satellite_gets_auth_fail_and_is_closed(car, kc):
    store, reg, wifi = car
    priv, pub = kc_mod.generate_keypair()
    blob = {"privkey": b64e(priv), "pubkey": b64e(pub), "fp": kc_mod.fingerprint(pub),
            "transport": "wifi", "car_pub": b64e(kc.car_pub), "cert": None}
    sat = FakeSat(blob)
    sat.attach(socket.create_connection(("127.0.0.1", wifi.tcp_port)))
    assert pump(reg, lambda: sat.closed.is_set())
    assert sat.failed and "unknown device" in sat.failed
    assert not [i for i in store.state.satellites.nodes if i >= 300]


def test_dead_session_closed(kc):
    wifi = WifiTransport(bind="127.0.0.1", udp_port=0, tcp_port=0, dead_s=0.5)
    store = Store()
    reg = SatelliteRegistry(store, transports_enabled=("wifi",), keychain=kc,
                            transports={"wifi": wifi})
    reg.start()
    try:
        with kc.transaction():
            blob = kc.new_satellite("garage", "wifi")
        sat = FakeSat(blob)
        sat.attach(socket.create_connection(("127.0.0.1", wifi.tcp_port)))
        assert pump(reg, lambda: sat.authed.is_set())
        # satellite goes silent -> car closes after dead_s
        assert pump(reg, lambda: sat.closed.is_set(), timeout=5)
        assert pump(reg, lambda: not store.state.satellites.nodes[400].online)
    finally:
        reg.stop()


def test_ap_down_when_iface_lacks_ap_addr():
    wifi = WifiTransport(bind="127.0.0.1", udp_port=0, tcp_port=0,
                         iface="lo", ap_addr="10.42.0.1")
    assert wifi.start()
    try:
        st = wifi.status()
        assert not st.available and st.state == "ap-down"
        assert not wifi.connect("127.0.0.1:1")
    finally:
        wifi.stop()
