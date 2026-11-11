"""Tests for the SatelliteRegistry (docs/SATELLITES.md §5-6) with fake transports."""

import json
from collections import deque

import pytest

pytest.importorskip("cryptography")

from cyberpunk_computer.api.serialization import serialize_state
from cyberpunk_computer.backend import satellite_keychain as kc_mod
from cyberpunk_computer.backend.satellite_auth import SatelliteAuthClient
from cyberpunk_computer.backend.satellite_keychain import Keychain, b64e
from cyberpunk_computer.backend.satellite_registry import SatelliteRegistry
from cyberpunk_computer.io.satellite_transport import (
    DiscoveredDevice,
    LinkEvent,
    SatelliteTransport,
    TransportStatus,
)
from cyberpunk_computer.state.actions import (
    EnqueueSatelliteCommandAction,
    SatelliteRegistryCommandAction,
    SetPowerboxPowerStatusAction,
)
from cyberpunk_computer.state.store import Store


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeOutputPort:
    def __init__(self):
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd)
        return True


class FakeTransport(SatelliteTransport):
    """In-memory transport: tests push LinkEvents and read sent lines."""

    def __init__(self, name="wifi"):
        self.name = name
        self.events = deque()
        self.sent = []          # (address, decoded obj)
        self.linked = set()
        self.devices = []
        self.disconnected = []
        self.available = True
        self.discover_calls = []
        self.pairable = []
        self.adv = []

    def start(self):
        return True

    def stop(self):
        pass

    def tick(self, now):
        pass

    def poll(self):
        return self.events.popleft() if self.events else None

    def send(self, address, line):
        if address not in self.linked:
            return False
        self.sent.append((address, json.loads(line)))
        return True

    def discover(self, force=False):
        self.discover_calls.append(force)

    def discovered(self):
        return list(self.devices)

    def status(self):
        return TransportStatus(self.name, available=self.available,
                               state="listening" if self.available else "ap-down")

    def disconnect(self, address):
        self.disconnected.append(address)
        self.linked.discard(address)

    def set_pairable(self, seconds):
        self.pairable.append(seconds)

    def set_advertisement(self, interval_ms, flags):
        self.adv.append((interval_ms, flags))

    # helpers
    def up(self, address):
        self.linked.add(address)
        self.events.append(LinkEvent("up", self.name, address))

    def data(self, address, payload):
        self.events.append(LinkEvent("data", self.name, address,
                                     line=json.dumps({"d": payload})))

    def down(self, address):
        self.linked.discard(address)
        self.events.append(LinkEvent("down", self.name, address))

    def last(self, address):
        return [o for a, o in self.sent if a == address][-1]


@pytest.fixture
def kc(tmp_path):
    k = Keychain(str(tmp_path / "keys"))
    k.init_car_key()
    return k


def make(kc, transports, enabled=None, port=None):
    store = Store()
    clock = FakeClock()
    reg = SatelliteRegistry(
        store, output_port=port, transports_enabled=enabled or tuple(transports),
        keychain=kc, transports=transports, clock=clock)
    reg.start()
    return store, reg, clock


def handshake(t, reg, address, blob):
    client = SatelliteAuthClient(blob)
    t.up(address)
    reg.tick()
    challenge = t.last(address)["d"]
    assert challenge["a"] == "auth"
    t.data(address, client.respond(challenge))
    reg.tick()
    return client


def test_wifi_auth_then_send_routes_over_link(kc):
    with kc.transaction():
        blob = kc.new_satellite("garage", "wifi", caps=["relay"])
    t = FakeTransport("wifi")
    store, reg, clock = make(kc, {"wifi": t})
    client = handshake(t, reg, "10.42.0.9:8788", blob)
    ok = t.last("10.42.0.9:8788")
    assert client.check_result(ok["d"]) == 400 and ok["id"] == 400
    node = store.state.satellites.nodes[400]
    assert node.online and node.authenticated and node.linked and node.trusted
    assert node.transport == "wifi" and node.fp == blob["fp"] and node.caps == ("relay",)

    reg.send(400, {"a": "relay", "on": True})
    for _ in range(3):
        reg.tick()
    assert t.last("10.42.0.9:8788") == {"id": 400, "d": {"a": "relay", "on": True}}
    # BLE/Wi-Fi queues never touch the RS485 rail wake-lock
    assert "queue" not in store.state.satellites.power_holders
    assert store.state.satellites.transports["wifi"].queue_depth == 0

    t.down("10.42.0.9:8788")
    reg.tick()
    node = store.state.satellites.nodes[400]
    assert not node.online and not node.linked and not node.authenticated


def test_registry_middleware_wiring(kc):
    """EnqueueSatelliteCommandAction reaches registry.send via a middleware
    (the service wiring), RS485 ids still go to the gateway port."""
    port = FakeOutputPort()
    t = FakeTransport("wifi")
    store, reg, clock = make(kc, {"wifi": t}, enabled=("rs485", "wifi"), port=port)

    def mw(action, _s):
        if type(action).__name__ == "EnqueueSatelliteCommandAction":
            reg.send(action.device_id, action.payload, action.priority, action.name)
        elif type(action).__name__ == "SatelliteRegistryCommandAction":
            reg.handle_command(action.op, action.params)
    store.add_middleware(mw)
    store.dispatch(EnqueueSatelliteCommandAction(107, {"a": "read"}))
    assert "queue" in store.state.satellites.power_holders      # rail wake-lock as before
    for _ in range(3):
        reg.tick()
    assert port.sent and port.sent[0].device_id == 107 and port.sent[0].payload == {"a": "read"}
    assert store.state.satellites.queue_depth == 0


def test_unknown_device_refused_without_pairing(kc):
    priv, pub = kc_mod.generate_keypair()
    blob = {"privkey": b64e(priv), "pubkey": b64e(pub), "fp": kc_mod.fingerprint(pub),
            "transport": "wifi", "car_pub": b64e(kc.car_pub), "cert": None}
    t = FakeTransport("wifi")
    store, reg, clock = make(kc, {"wifi": t})
    t.up("a:1")
    reg.tick()
    challenge = t.last("a:1")["d"]
    t.data("a:1", SatelliteAuthClient(blob).respond(challenge))
    reg.tick()
    assert t.sent[-1][1]["d"] == {"a": "auth_fail", "why": "unknown device"}
    assert "a:1" in t.disconnected


def test_nothing_sent_before_auth_and_timeout(kc):
    with kc.transaction():
        kc.new_satellite("garage", "wifi")
    t = FakeTransport("wifi")
    store, reg, clock = make(kc, {"wifi": t})
    t.up("a:1")
    reg.tick()
    assert len(t.sent) == 1                       # only the challenge
    t.data("a:1", {"a": "status", "temp": 3})     # pre-auth traffic is ignored
    reg.tick()
    assert len(t.sent) == 1 and 400 not in store.state.satellites.nodes or \
        not store.state.satellites.nodes[400].online
    clock.advance(10.5)
    reg.tick()
    assert t.sent[-1][1]["d"] == {"a": "auth_fail", "why": "timeout"}
    assert "a:1" in t.disconnected


def test_pairing_hold_then_enrol(kc):
    priv, pub = kc_mod.generate_keypair()
    blob = {"privkey": b64e(priv), "pubkey": b64e(pub), "fp": kc_mod.fingerprint(pub),
            "transport": "ble", "car_pub": b64e(kc.car_pub), "cert": None}
    t = FakeTransport("ble")
    t.devices = [DiscoveredDevice("ble", "AA:BB", name="garage", rssi=-60, linked=True)]
    store, reg, clock = make(kc, {"ble": t})
    # enrol refused while the window is closed
    res = reg.handle_command("enrol", {"transport": "ble", "address": "AA:BB", "name": "garage"})
    assert not res["ok"] and "closed" in res["error"]
    assert store.state.satellites.last_command["ok"] is False

    reg.pair_open(120, "ble")
    assert t.pairable == [120.0]
    reg.tick()
    assert t.adv[-1] == (1000, 2)                 # fast adv + pairing flag
    p = store.state.satellites.pairing
    assert p.open and p.transport == "ble" and p.opened_at > 0 and p.until > p.opened_at

    client = handshake(t, reg, "AA:BB", blob)
    assert t.sent[-1][1]["d"]["a"] == "auth"      # held: no reply yet
    reg._publish(force=True)
    disc = store.state.satellites.discovered
    assert len(disc) == 1 and disc[0].pubkey == blob["pubkey"] and disc[0].fp == blob["fp"]
    assert disc[0].rssi == -60 and disc[0].sat_id is None

    res = reg.handle_command("enrol", {"transport": "ble", "address": "AA:BB",
                                       "name": "garage", "caps": ["relay"]})
    assert res["ok"] and res["sat_id"] == 300
    assert client.check_result(t.last("AA:BB")["d"]) == 300
    node = store.state.satellites.nodes[300]
    assert node.online and node.authenticated and node.rssi == -60
    assert kc.get(300)["cert"] is not None
    reg._publish(force=True)
    assert store.state.satellites.discovered == ()

    reg.pair_open(0)
    assert not store.state.satellites.pairing.open
    assert store.state.satellites.pairing.opened_at == 0


def test_held_link_dropped_when_window_closes(kc):
    priv, pub = kc_mod.generate_keypair()
    blob = {"privkey": b64e(priv), "pubkey": b64e(pub), "fp": kc_mod.fingerprint(pub),
            "transport": "wifi", "car_pub": b64e(kc.car_pub), "cert": None}
    t = FakeTransport("wifi")
    store, reg, clock = make(kc, {"wifi": t})
    reg.pair_open(60)
    handshake(t, reg, "x:1", blob)
    reg.pair_open(0)
    reg.tick()
    assert t.sent[-1][1]["d"] == {"a": "auth_fail", "why": "pairing window closed"}
    assert "x:1" in t.disconnected


def test_cert_satellite_added_to_keychain(kc):
    priv, pub = kc_mod.generate_keypair()
    cert = kc_mod.sign_cert(kc.car_priv, kc_mod.cert_body(433, "trailer", "wifi", b64e(pub)))
    blob = {"privkey": b64e(priv), "pubkey": b64e(pub), "fp": kc_mod.fingerprint(pub),
            "transport": "wifi", "car_pub": b64e(kc.car_pub), "cert": cert}
    t = FakeTransport("wifi")
    store, reg, clock = make(kc, {"wifi": t})
    handshake(t, reg, "y:1", blob)
    assert t.last("y:1")["d"]["a"] == "auth_ok"
    assert kc.get(433)["name"] == "trailer"
    assert store.state.satellites.nodes[433].online


def test_rs485_enrol_forget_revoke(kc):
    t = FakeTransport("wifi")
    store, reg, clock = make(kc, {"wifi": t}, enabled=("rs485", "wifi"), port=FakeOutputPort())
    res = reg.handle_command("enrol", {"transport": "rs485", "address": "120", "name": "horn"})
    assert res["ok"] and res["sat_id"] == 120               # no pairing window needed
    n = store.state.satellites.nodes[120]
    assert n.name == "horn" and n.transport == "rs485" and n.trusted and n.address == "120"
    res = reg.handle_command("revoke", {"sat_id": 120})
    assert not res["ok"] and res["error"] == "nothing to revoke"
    assert reg.handle_command("forget", {"sat_id": 120})["ok"]
    assert 120 not in store.state.satellites.nodes
    assert kc.get(120) is None


def test_revoke_drops_link_and_node(kc):
    with kc.transaction():
        blob = kc.new_satellite("garage", "wifi")
    t = FakeTransport("wifi")
    store, reg, clock = make(kc, {"wifi": t})
    handshake(t, reg, "z:1", blob)
    assert reg.handle_command("revoke", {"sat_id": 400})["ok"]
    assert "z:1" in t.disconnected and 400 not in store.state.satellites.nodes
    assert kc.is_revoked(blob["fp"])


def test_rs485_discovered_and_rediscovery_only_on_confirmed_rail(kc):
    from cyberpunk_computer.state.actions import UpdateSatelliteNodeAction
    from cyberpunk_computer.state.app_state import RELAY_CH_RS485
    port = FakeOutputPort()
    store, reg, clock = make(kc, {}, enabled=("rs485",), port=None)
    # build with the real Rs485Transport
    store = Store()
    reg = SatelliteRegistry(store, output_port=port, transports_enabled=("rs485",),
                            keychain=kc, clock=clock, rediscover_s=300)
    reg.start()
    reg.tick()
    # no powerbox -> rail not confirmed -> no background poll (never powers the rail)
    assert not reg.queue.has("disc:rs485") and "queue" not in store.state.satellites.power_holders

    store.dispatch(UpdateSatelliteNodeAction(115, seen=True))   # an unknown wired device
    reg._publish(force=True)
    disc = {d.address: d for d in store.state.satellites.discovered}
    assert "115" in disc and disc["115"].transport == "rs485" and disc["115"].linked

    from cyberpunk_computer.state.actions import SetPowerboxConnectionAction
    store.dispatch(SetPowerboxConnectionAction(True))
    relays = [False] * 8
    relays[RELAY_CH_RS485 - 1] = True
    store.dispatch(SetPowerboxPowerStatusAction(relays=tuple(relays)))
    reg.tick()                                   # availability edge -> poll queued
    assert reg.queue.has("disc:rs485")
    for _ in range(4):
        reg.tick()
    polled = sorted(c.device_id for c in port.sent if c.payload == {"a": "status"})
    assert polled == [106, 107, 110]
    assert store.state.satellites.transports["rs485"].last_discovery > 0


def test_state_shape_serializes(kc):
    with kc.transaction():
        blob = kc.new_satellite("garage", "wifi")
    t = FakeTransport("wifi")
    store, reg, clock = make(kc, {"wifi": t}, enabled=("rs485", "ble", "wifi"),
                             port=FakeOutputPort())
    handshake(t, reg, "q:1", blob)
    reg._publish(force=True)
    sats = json.loads(json.dumps(serialize_state(store.state)))["satellites"]
    assert set(sats["transports"]) == {"rs485", "ble", "wifi"}
    w = sats["transports"]["wifi"]
    assert w["available"] is True and w["require_auth"] is True and "queue_depth" in w
    assert sats["transports"]["rs485"]["require_auth"] is False
    assert sats["transports"]["ble"]["available"] is False
    node = sats["nodes"]["400"]
    assert node["caps"] == [] and node["transport"] == "wifi" and node["authenticated"] is True
    assert sats["pairing"] == {"open": False, "until": 0.0, "transport": None, "opened_at": 0.0}
    assert isinstance(sats["discovered"], list)
    for key in ("power_holders", "power_requested", "queue_depth", "active_job"):
        assert key in sats


def test_keychain_edit_by_cli_is_picked_up(kc):
    with kc.transaction():
        blob = kc.new_satellite("garage", "wifi")
    t = FakeTransport("wifi")
    store, reg, clock = make(kc, {"wifi": t})
    handshake(t, reg, "r:1", blob)
    other = Keychain(kc.directory)              # "prius-satkey revoke garage"
    with other.transaction():
        other.revoke("garage")
    clock.advance(3)
    reg.tick()
    assert "r:1" in t.disconnected
    assert 400 not in store.state.satellites.nodes


def test_keepalive_ping_and_pong(kc):
    with kc.transaction():
        blob = kc.new_satellite("garage", "wifi")
    t = FakeTransport("wifi")
    t.keepalive_s = 15.0
    store, reg, clock = make(kc, {"wifi": t})
    handshake(t, reg, "k:1", blob)
    clock.advance(16)
    reg.tick()
    assert t.last("k:1")["d"] == {"a": "ping"}
    t.data("k:1", {"a": "ping"})
    reg.tick()
    assert t.last("k:1")["d"] == {"a": "pong"}


def test_on_message_receives_authenticated_payloads(kc):
    with kc.transaction():
        blob = kc.new_satellite("garage", "wifi")
    t = FakeTransport("wifi")
    got = []
    store = Store()
    reg = SatelliteRegistry(store, transports_enabled=("wifi",), keychain=kc,
                            transports={"wifi": t}, clock=FakeClock(),
                            on_message=lambda sid, p: got.append((sid, p)))
    reg.start()
    handshake(t, reg, "m:1", blob)
    t.data("m:1", {"a": "status", "temp": 21.5, "ver": "1.2"})
    reg.tick()
    assert got == [(400, {"a": "status", "temp": 21.5, "ver": "1.2"})]
    assert store.state.satellites.nodes[400].fw_version == "1.2"


class TestCommands:
    def test_builders(self):
        from cyberpunk_computer.api.commands import build_command, CommandError
        a = build_command("satellite_pair_open", {})
        assert isinstance(a, SatelliteRegistryCommandAction)
        assert a.op == "pair_open" and a.params == {"seconds": 120.0, "transport": None}
        a = build_command("satellite_enrol", {"transport": "rs485", "address": "120", "name": "horn"})
        assert a.params["sat_id"] == 120
        a = build_command("satellite_enrol", {"transport": "ble", "address": "AA", "name": "g",
                                              "caps": "relay,temp"})
        assert a.params["caps"] == ["relay", "temp"] and a.params["sat_id"] is None
        assert build_command("satellite_discover", {"transport": "wifi"}).params == {"transport": "wifi"}
        assert build_command("satellite_forget", {"sat_id": 300}).op == "forget"
        assert build_command("satellite_revoke", {"sat_id": 400}).op == "revoke"
        assert build_command("satellite_send", {"device_id": 400, "payload": {}}).device_id == 400
        with pytest.raises(CommandError):
            build_command("satellite_enrol", {"transport": "ble", "name": "g"})   # no address
        with pytest.raises(CommandError):
            build_command("satellite_enrol", {"transport": "ble", "address": "A", "name": "g",
                                              "sat_id": 400})                    # wrong range
        with pytest.raises(CommandError):
            build_command("satellite_forget", {"sat_id": 250})
        with pytest.raises(CommandError):
            build_command("satellite_discover", {"transport": "zigbee"})


def test_default_transport_stack_smoke(kc, tmp_path):
    """Real construction path: RS485 + BLE (lazy import, bled socket absent) +
    Wi-Fi on ephemeral loopback ports; start/tick/stop without errors."""
    store = Store()
    reg = SatelliteRegistry(
        store, output_port=FakeOutputPort(), keychain=kc,
        wifi_options={"bind": "127.0.0.1", "udp_port": 0, "tcp_port": 0},
        ble_socket_path=str(tmp_path / "no-bled.sock"))
    reg.start()
    try:
        for _ in range(3):
            reg.tick()
        reg._publish(force=True)
        tr = store.state.satellites.transports
        assert tr["wifi"].available and tr["rs485"].require_auth is False
        assert tr["ble"].available is False          # no bled (or no module)
        assert tr["ble"].state != "disabled"
    finally:
        reg.stop()


def test_service_wiring_preserves_rs485_path(kc):
    """BackendService._wire_satellites builds the registry; satellite_send on an
    RS485 id still takes the rail wake-lock and reaches the gateway port, the
    power rules are registered, and registry commands are routed."""
    from types import SimpleNamespace
    from cyberpunk_computer.api.commands import build_command
    from cyberpunk_computer.backend.service import BackendConfig, BackendService
    from cyberpunk_computer.state.rules.satellite_power import (
        SatelliteAccHoldRule, SatellitePowerRule)

    svc = BackendService(BackendConfig(
        satellite_transports=("rs485", "wifi"), satellite_keychain_dir=kc.directory,
        satellite_wifi_bind="127.0.0.1", satellite_wifi_udp_port=0,
        satellite_wifi_tcp_port=0))
    rules = []
    port = FakeOutputPort()
    store = Store()
    twin = SimpleNamespace(store=store, output_port=port,
                           rules_engine=SimpleNamespace(register=rules.append),
                           ingress=SimpleNamespace(_satellite_handlers={}))
    svc._wire_satellites(twin)
    try:
        assert {type(r) for r in rules} == {SatelliteAccHoldRule, SatellitePowerRule}
        assert svc.satellite_queue is svc.satellite_registry.queue
        assert 107 in store.state.satellites.nodes                  # seeded as before
        store.dispatch(build_command("satellite_send", {"device_id": 107, "payload": {"a": "x"}}))
        assert "queue" in store.state.satellites.power_holders
        for _ in range(4):
            svc._satellite_tick()
        assert [(c.device_id, c.payload) for c in port.sent] == [(107, {"a": "x"})]
        store.dispatch(build_command("satellite_enrol",
                                     {"transport": "rs485", "address": "121", "name": "horn"}))
        assert store.state.satellites.last_command["ok"] is True
        assert store.state.satellites.nodes[121].name == "horn"
    finally:
        svc.satellite_registry.stop()


def test_ble_central_follows_profile_pairing_and_keychain(kc):
    from cyberpunk_computer.state.actions import (
        SetPowerboxIgnitionAction,
        SetPowerboxPowerModeAction,
    )

    class CentralTransport(FakeTransport):
        def __init__(self):
            super().__init__("ble")
            self.central = []

        def set_central(self, enabled, addresses=()):
            self.central.append((enabled, list(addresses)))

    with kc.transaction():
        kc.new_satellite("door", "ble", ble_role="beacon", address="aa:bb:cc:dd:ee:b1")
        kc.new_satellite("garage", "ble", address="AA:BB:CC:DD:EE:01")   # scanner: not a target
        kc.new_satellite("cam", "wifi")
    t = CentralTransport()
    store, reg, clock = make(kc, {"ble": t})
    reg.tick()
    assert t.central == [(False, ["AA:BB:CC:DD:EE:B1"])]      # low profile: off
    reg.tick()
    clock.advance(5)
    reg.tick()
    assert len(t.central) == 1                                # on change only

    store.dispatch(SetPowerboxPowerModeAction("full"))
    clock.advance(1)
    reg.tick()
    assert t.central[-1] == (True, ["AA:BB:CC:DD:EE:B1"])
    store.dispatch(SetPowerboxPowerModeAction("low"))
    clock.advance(1)
    reg.tick()
    assert t.central[-1][0] is False

    # a pairing window turns it on at once (no 1 s wait), closing turns it off
    reg.pair_open(60)
    assert t.central[-1][0] is True
    reg.pair_open(0)
    assert t.central[-1][0] is False

    # ACC on counts as well (power-mode rules disabled)
    store.dispatch(SetPowerboxIgnitionAction(acc_on=True))
    clock.advance(1)
    reg.tick()
    assert t.central[-1][0] is True

    # a beacon enrolled by prius-satkey shows up in the address list
    other = Keychain(kc.directory)
    with other.transaction():
        other.enrol_pubkey("window", "ble", b64e(b"\x05" * 32), ble_role="beacon",
                           address="AA:BB:CC:DD:EE:B2")
    clock.advance(3)
    reg.tick()
    assert t.central[-1] == (True, ["AA:BB:CC:DD:EE:B1", "AA:BB:CC:DD:EE:B2"])


def test_central_noop_for_transports_without_it(kc):
    t = FakeTransport("ble")            # no set_central override: ABC default no-op
    store, reg, clock = make(kc, {"ble": t})
    reg.tick()
    reg.pair_open(30)
    reg.tick()
