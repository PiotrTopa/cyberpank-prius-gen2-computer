"""
Tests for cyberpunk_computer/io/ble_transport.py against a fake prius-bled on a
temporary Unix socket: hello/status, link up/down, data, send + ack, scan /
discovery, rediscovery gap, and reconnect after the fake bled restarts.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from cyberpunk_computer.io.ble_transport import BleTransport
from cyberpunk_computer.io.satellite_transport import SatelliteTransport


class FakeBled:
    """Minimal bled: accepts one client, sends hello+status, records commands."""

    def __init__(self, path, links=()):
        self.path = path
        self.links = list(links)
        self.commands = []
        self.conn = None
        self.connected = threading.Event()
        self.got = threading.Condition()
        self._stop = False
        if os.path.exists(path):
            os.unlink(path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(1)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def status(self, **kw):
        st = {"ev": "status", "powered": True, "advertising": True, "links": self.links,
              "pairable": False, "adv": {"interval_ms": 10000, "flags": 0, "active": True}}
        st.update(kw)
        return st

    def emit(self, obj):
        self.conn.sendall((json.dumps(obj) + "\n").encode())

    def _run(self):
        try:
            self.conn, _ = self.sock.accept()
        except OSError:
            return
        self.emit({"ev": "hello", "adapter": "hci0", "address": "02:00:E8:51:02:58",
                   "adv": {"interval_ms": 10000, "flags": 0, "active": True}})
        self.emit(self.status())
        self.connected.set()
        buf = b""
        while not self._stop:
            try:
                data = self.conn.recv(4096)
            except OSError:
                return
            if not data:
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                cmd = json.loads(line)
                with self.got:
                    self.commands.append(cmd)
                    self.got.notify_all()
                if cmd["cmd"] == "send":
                    self.emit({"ack": "send", "ok": True})
                elif cmd["cmd"] == "scan":
                    self.emit({"ack": "scan", "ok": True})

    def wait_cmd(self, name, timeout=3.0):
        deadline = time.time() + timeout
        with self.got:
            while True:
                for c in self.commands:
                    if c["cmd"] == name:
                        return c
                left = deadline - time.time()
                if left <= 0:
                    raise AssertionError("no %s command; got %r" % (name, self.commands))
                self.got.wait(left)

    def close(self):
        self._stop = True
        for s in (self.conn, self.sock):
            if s is not None:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()
        if os.path.exists(self.path):
            os.unlink(self.path)


def wait_for(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(0.01)
    raise AssertionError("condition not met")


def drain(t):
    out = []
    while True:
        ev = t.poll()
        if ev is None:
            return out
        out.append(ev)


def wait_events(t, n, timeout=3.0):
    got = []
    deadline = time.time() + timeout
    while len(got) < n and time.time() < deadline:
        got += drain(t)
        time.sleep(0.01)
    return got


@pytest.fixture
def sock_path():
    d = tempfile.mkdtemp(prefix="bled", dir="/tmp")  # AF_UNIX paths max 107 chars
    yield os.path.join(d, "bled.sock")


@pytest.fixture
def transport(sock_path):
    t = BleTransport(sock_path, backoff_min_s=0.05, backoff_max_s=0.2)
    yield t
    t.stop()


def test_is_a_satellite_transport_and_imports_without_dbus():
    assert issubclass(BleTransport, SatelliteTransport)
    # dbus/gi made unimportable: the backend side must not need them
    code = ("import sys; sys.modules['dbus'] = None; sys.modules['gi'] = None; "
            "import cyberpunk_computer.io.ble_transport")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.run([sys.executable, "-c", code], cwd=root, check=True)


def test_unreachable_then_hello(transport, sock_path):
    transport.start()
    st = transport.status()
    assert st.available is False and st.state == "down" and st.detail == "bled unreachable"
    assert transport.send("AA:BB:CC:DD:EE:01", "{}") is False
    bled = FakeBled(sock_path)
    try:
        wait_for(lambda: transport.status().state == "advertising")
        st = transport.status()
        assert (st.available, st.detail) == (True, "10 s")
    finally:
        bled.close()


def test_link_data_send_and_down(transport, sock_path):
    bled = FakeBled(sock_path)
    try:
        transport.start()
        bled.connected.wait(3)
        wait_for(lambda: transport.status().available)
        bled.emit({"ev": "link", "state": "up", "addr": "aa:bb:cc:dd:ee:01", "name": "garage",
                   "bonded": True, "mtu": 247})
        bled.emit({"ev": "data", "addr": "AA:BB:CC:DD:EE:01", "line": '{"a":"auth"}'})
        evs = wait_events(transport, 2)
        assert [(e.kind, e.address) for e in evs] == [("up", "AA:BB:CC:DD:EE:01"),
                                                      ("data", "AA:BB:CC:DD:EE:01")]
        assert evs[0].name == "garage" and evs[0].extra == {"bonded": True, "mtu": 247}
        assert evs[1].line == '{"a":"auth"}' and evs[1].transport == "ble"
        d = transport.discovered()
        assert d[0].address == "AA:BB:CC:DD:EE:01" and d[0].linked

        assert transport.send("aa:bb:cc:dd:ee:01", '{"a":"auth_ok"}\n') is True
        assert bled.wait_cmd("send") == {"cmd": "send", "addr": "AA:BB:CC:DD:EE:01",
                                         "line": '{"a":"auth_ok"}'}
        assert transport.send("AA:BB:CC:DD:EE:99", "{}") is False   # not linked

        transport.disconnect("AA:BB:CC:DD:EE:01")
        assert bled.wait_cmd("disconnect")["addr"] == "AA:BB:CC:DD:EE:01"
        bled.emit({"ev": "link", "state": "down", "addr": "AA:BB:CC:DD:EE:01"})
        (down,) = wait_events(transport, 1)
        assert down.kind == "down"
        assert transport.discovered() == []
    finally:
        bled.close()


def test_status_pairing_and_commands(transport, sock_path):
    bled = FakeBled(sock_path)
    try:
        transport.start()
        wait_for(lambda: transport.status().available)
        transport.set_pairable(120)
        transport.set_advertisement(1000, 3)
        assert bled.wait_cmd("pairable") == {"cmd": "pairable", "seconds": 120}
        assert bled.wait_cmd("adv") == {"cmd": "adv", "interval_ms": 1000, "flags": 3}
        bled.emit(bled.status(pairable=True, adv={"interval_ms": 1000, "flags": 3, "active": True}))
        wait_for(lambda: transport.status().state == "pairing")
        assert transport.status().detail == "1 s"
        bled.emit(bled.status(powered=False, advertising=False))
        wait_for(lambda: transport.status().state == "down")
        assert transport.status().available is False
    finally:
        bled.close()


def test_scan_discovery_gap_and_ttl(sock_path):
    t = BleTransport(sock_path, backoff_min_s=0.05, scan_min_gap_s=60, discovery_ttl_s=600)
    bled = FakeBled(sock_path)
    try:
        t.start()
        wait_for(lambda: t.status().available)
        t.discover()
        assert bled.wait_cmd("scan") == {"cmd": "scan", "seconds": 5}
        t.discover()                       # within 60 s and pending: suppressed
        bled.emit({"ev": "scan", "devices": [
            {"addr": "AA:BB:CC:DD:EE:09", "name": "sat", "rssi": -61, "uuids": ["580e04f7"]}]})
        (ev,) = wait_events(t, 1)
        assert ev.kind == "discovery" and ev.devices[0].address == "AA:BB:CC:DD:EE:09"
        assert ev.devices[0].rssi == -61
        t.discover()                       # gap not elapsed: suppressed
        time.sleep(0.1)
        assert [c["cmd"] for c in bled.commands].count("scan") == 1
        t.discover(force=True)
        wait_for(lambda: [c["cmd"] for c in bled.commands].count("scan") == 2)
        st = t.status()
        assert st.devices_seen == 1 and st.last_discovery > 0
        (d,) = t.discovered()
        assert d.name == "sat" and not d.linked
        d.last_seen -= 601                 # ages out of the TTL
        assert t.discovered() == []
    finally:
        t.stop()
        bled.close()


def test_reconnect_after_bled_restart(transport, sock_path):
    bled = FakeBled(sock_path)
    transport.start()
    wait_for(lambda: transport.status().available)
    bled.connected.wait(3)
    bled.emit({"ev": "link", "state": "up", "addr": "AA:BB:CC:DD:EE:01", "name": "a"})
    bled.emit({"ev": "link", "state": "up", "addr": "AA:BB:CC:DD:EE:02", "name": "b"})
    assert [e.kind for e in wait_events(transport, 2)] == ["up", "up"]
    bled.close()                           # bled dies: links go down, unavailable
    evs = wait_events(transport, 2)
    assert sorted((e.kind, e.address) for e in evs) == [("down", "AA:BB:CC:DD:EE:01"),
                                                        ("down", "AA:BB:CC:DD:EE:02")]
    wait_for(lambda: not transport.status().available)
    # restarted bled still holds link 02: status reconciles it back up
    bled2 = FakeBled(sock_path, links=[{"addr": "AA:BB:CC:DD:EE:02", "name": "b",
                                        "bonded": True, "mtu": 100}])
    try:
        wait_for(lambda: transport.status().available, timeout=5)
        (up,) = wait_events(transport, 1)
        assert (up.kind, up.address, up.extra.get("mtu")) == ("up", "AA:BB:CC:DD:EE:02", 100)
        assert transport.send("AA:BB:CC:DD:EE:02", "{}") is True
        assert bled2.wait_cmd("send")["addr"] == "AA:BB:CC:DD:EE:02"
    finally:
        bled2.close()


def test_tick_rediscovers_on_edge_when_enabled(sock_path):
    t = BleTransport(sock_path, backoff_min_s=0.05, rediscover_s=300)
    bled = FakeBled(sock_path)
    try:
        t.start()
        wait_for(lambda: t.status().available)
        t.tick(time.time())
        assert bled.wait_cmd("scan")["seconds"] == 5
    finally:
        t.stop()
        bled.close()


def test_stop_is_idempotent(sock_path):
    t = BleTransport(sock_path)
    t.stop()
    t.start()
    t.stop()
    t.stop()
