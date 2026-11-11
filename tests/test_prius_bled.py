"""
Tests for POCO/rootfs/usr/local/sbin/prius-bled: the pure half of the BLE
daemon (line framing, notification chunking, advertisement flag bytes,
command parsing) and the core + control socket with the BlueZ side stubbed.
"""

import importlib.machinery
import importlib.util
import json
import socket
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "POCO/rootfs/usr/local/sbin/prius-bled"


def _load():
    loader = importlib.machinery.SourceFileLoader("prius_bled", str(SCRIPT))
    spec = importlib.util.spec_from_loader("prius_bled", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["prius_bled"] = mod   # dataclasses resolve annotations via sys.modules
    loader.exec_module(mod)
    return mod


bled = _load()
DEV = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_01"
DEV2 = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_02"


# --- framing --------------------------------------------------------------------

def test_assembler_reassembles_across_writes_and_splits_packed_lines():
    a = bled.LineAssembler()
    assert a.feed(b'{"a":"st') == []
    assert a.feed(b'atus"}\n{"b":1}\n{"c"') == ['{"a":"status"}', '{"b":1}']
    assert a.feed(b':2}\r\n') == ['{"c":2}']
    assert a.pending == 0


def test_assembler_drops_blank_lines():
    a = bled.LineAssembler()
    assert a.feed(b"\n\r\n  \n") == []


def test_assembler_caps_line_length():
    a = bled.LineAssembler(limit=16)
    assert a.feed(b"x" * 16) == []
    with pytest.raises(bled.LineTooLong):
        a.feed(b"y")
    assert a.pending == 0
    with pytest.raises(bled.LineTooLong):
        bled.LineAssembler(limit=4).feed(b"12345\n")


def test_assembler_default_limit_is_4k():
    a = bled.LineAssembler()
    assert a.feed(b"x" * 4096 + b"\n") == ["x" * 4096]
    with pytest.raises(bled.LineTooLong):
        a.feed(b"x" * 4097)


def test_chunk_line_uses_mtu_minus_3_and_appends_newline():
    line = '{"a":"auth","nonce":"' + "n" * 60 + '"}'
    chunks = bled.chunk_line(line, 23)
    assert all(len(c) <= 20 for c in chunks)
    assert b"".join(chunks) == line.encode() + b"\n"
    big = bled.chunk_line(line, 247)
    assert len(big) == 1 and big[0].endswith(b"\n")
    # tiny/unknown MTU falls back to the ATT default
    assert all(len(c) <= 20 for c in bled.chunk_line(line, 0))


def test_chunk_line_round_trips_utf8_through_assembler():
    line = '{"name":"żółć ĘĄ"}'
    a = bled.LineAssembler()
    out = []
    for c in bled.chunk_line(line, 23):
        out += a.feed(c)
    assert out == [line]


def test_flag_bytes_little_endian():
    assert bled.flag_bytes(0) == b"\x00\x00"
    assert bled.flag_bytes(bled.FLAG_ACC) == b"\x01\x00"
    assert bled.flag_bytes(bled.FLAG_ACC | bled.FLAG_PAIRING) == b"\x03\x00"
    assert bled.flag_bytes(0x1FF03) == b"\x03\xff"


def test_adv_interval_range():
    assert bled.adv_interval_range(10000) == (10000, 10240)
    assert bled.adv_interval_range(1000) == (1000, 1240)
    assert bled.adv_interval_range(5) == (20, 260)
    assert bled.adv_interval_range(99999) == (10240, 10240)


def test_addr_helpers():
    assert bled.addr_from_path(DEV) == "AA:BB:CC:DD:EE:01"
    assert bled.normalise_addr("aa-bb-cc-dd-ee-ff") == "AA:BB:CC:DD:EE:FF"
    with pytest.raises(ValueError):
        bled.normalise_addr("nope")
    with pytest.raises(ValueError):
        bled.addr_from_path("/org/bluez/hci0")


# --- command parsing ----------------------------------------------------------------

def test_parse_send():
    c = bled.parse_command('{"cmd":"send","addr":"aa:bb:cc:dd:ee:01","line":"{\\"a\\":1}\\n"}')
    assert (c.name, c.addr, c.line) == ("send", "AA:BB:CC:DD:EE:01", '{"a":1}')


@pytest.mark.parametrize("raw,err", [
    ("not json", "bad json"),
    ("[1]", "not an object"),
    ('{"cmd":"reboot"}', "unknown cmd"),
    ('{"cmd":"send","addr":"x","line":"a"}', "bad address"),
    ('{"cmd":"send","addr":"AA:BB:CC:DD:EE:01","line":5}', "line must be"),
    ('{"cmd":"send","addr":"AA:BB:CC:DD:EE:01","line":"a\\nb"}', "newline"),
    ('{"cmd":"scan","seconds":"5"}', "must be a number"),
    ('{"cmd":"adv","interval_ms":true}', "must be a number"),
])
def test_parse_errors(raw, err):
    with pytest.raises(bled.ProtocolError, match=err):
        bled.parse_command(raw)


def test_parse_send_line_cap():
    ok = json.dumps({"cmd": "send", "addr": "AA:BB:CC:DD:EE:01", "line": "x" * 4096})
    assert bled.parse_command(ok).line == "x" * 4096
    with pytest.raises(bled.ProtocolError, match="longer"):
        bled.parse_command(json.dumps({"cmd": "send", "addr": "AA:BB:CC:DD:EE:01", "line": "x" * 4097}))


def test_parse_clamps():
    assert bled.parse_command('{"cmd":"scan","seconds":60}').seconds == 10
    assert bled.parse_command('{"cmd":"scan"}').seconds == 5
    assert bled.parse_command('{"cmd":"pairable","seconds":-3}').seconds == 0
    assert bled.parse_command('{"cmd":"pairable","seconds":9999}').seconds == 600
    c = bled.parse_command('{"cmd":"adv","interval_ms":1,"flags":65537}')
    assert (c.interval_ms, c.flags) == (20, 1)


# --- core with a fake radio ------------------------------------------------------------

class FakeRadio(bled.Radio):
    def __init__(self):
        self.address = "02:00:E8:51:02:58"
        self.powered = True
        self.advertising = False
        self.adv_calls = []
        self.pairable = []
        self.notified = []
        self.disconnected = []
        self.scan_cb = None
        self.infos = {}
        self.discovery = []          # set_central_discovery calls
        self.discovery_ok = True
        self.connects = {}           # path -> done callback
        self.writes = []             # (path, chunks)

    def set_central_discovery(self, on):
        self.discovery.append(on)
        return self.discovery_ok or not on

    def central_connect(self, path, done):
        self.connects[path] = done
        return True

    def central_write(self, path, chunks):
        self.writes.append((path, list(chunks)))
        return True

    def apply_advertisement(self, min_ms, max_ms, flags):
        self.adv_calls.append((min_ms, max_ms, flags))
        self.advertising = True

    def set_pairable(self, on):
        self.pairable.append(on)

    def start_scan(self, seconds, done):
        self.scan_cb = done
        return True

    def notify(self, chunks):
        self.notified.append(list(chunks))
        return True

    def disconnect(self, path):
        self.disconnected.append(path)

    def device_info(self, path):
        return self.infos.get(path, {})


class FakeScheduler(bled.Scheduler):
    def __init__(self):
        self.timers = {}
        self.n = 0

    def call_later(self, seconds, fn):
        self.n += 1
        self.timers[self.n] = (seconds, fn)
        return self.n

    def cancel(self, handle):
        self.timers.pop(handle, None)

    def fire_all(self):
        for h in list(self.timers):
            _, fn = self.timers.pop(h)
            fn()


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def core():
    radio, sched, events = FakeRadio(), FakeScheduler(), []
    c = bled.BledCore(radio, sched, emit=events.append, clock=Clock())
    c.events = events
    c.on_radio_reset()
    return c


def evs(core, kind):
    return [e for e in core.events if e.get("ev") == kind]


def test_default_advertisement(core):
    assert core.radio.adv_calls == [(10000, 10240, b"\x00\x00")]
    hello = core.hello()
    assert hello["adapter"] == "hci0" and hello["address"] == "02:00:E8:51:02:58"
    assert hello["adv"]["interval_ms"] == 10000 and hello["adv"]["name"] == "PRIUS"


def test_adv_command_reapplies_only_on_change(core):
    assert core.handle_line('{"cmd":"adv","interval_ms":1000,"flags":1}') == {"ack": "adv", "ok": True}
    assert core.radio.adv_calls[-1] == (1000, 1240, b"\x01\x00")
    n = len(core.radio.adv_calls)
    core.handle_line('{"cmd":"adv","interval_ms":1000,"flags":1}')
    assert len(core.radio.adv_calls) == n


def test_pairing_window_forces_fast_adv_and_flag_then_expires(core):
    core.handle_line('{"cmd":"adv","interval_ms":10000,"flags":1}')
    core.handle_line('{"cmd":"pairable","seconds":120}')
    assert core.radio.pairable[-1] is True
    assert core.radio.adv_calls[-1] == (1000, 1240, b"\x03\x00")
    st = evs(core, "status")[-1]
    assert st["pairable"] is True and st["pairable_left"] == 120
    core.scheduler.fire_all()            # window expires
    assert core.radio.pairable[-1] is False
    assert core.radio.adv_calls[-1] == (10000, 10240, b"\x01\x00")
    assert evs(core, "status")[-1]["pairable"] is False


def test_pairable_zero_closes_and_cancels_timer(core):
    core.handle_line('{"cmd":"pairable","seconds":60}')
    core.handle_line('{"cmd":"pairable","seconds":0}')
    assert core.scheduler.timers == {}
    assert not core.pairing and core.radio.pairable[-1] is False


def test_link_up_on_first_write_then_data(core):
    core.radio.infos[DEV] = {"addr": "AA:BB:CC:DD:EE:01", "name": "garage", "bonded": True}
    core.on_connected(DEV, {"addr": "AA:BB:CC:DD:EE:01", "uuids": []})
    assert evs(core, "link") == []                    # unknown central: wait for a write
    core.on_rx(DEV, b"\n", 185)                       # bare newline announces the satellite
    (up,) = evs(core, "link")
    assert up == {"ev": "link", "state": "up", "addr": "AA:BB:CC:DD:EE:01", "name": "",
                  "bonded": False, "mtu": 185, "role": "peripheral"}
    core.on_rx(DEV, b'{"a":"auth",', 185)
    core.on_rx(DEV, b'"v":1}\n', 185)
    assert evs(core, "data") == [{"ev": "data", "addr": "AA:BB:CC:DD:EE:01", "line": '{"a":"auth","v":1}'}]
    assert evs(core, "status")[-1]["links"][0]["addr"] == "AA:BB:CC:DD:EE:01"


def test_link_up_on_connect_when_uuid_known(core):
    core.on_connected(DEV, {"addr": "AA:BB:CC:DD:EE:01", "name": "garage", "bonded": True,
                            "uuids": [bled.SERVICE_UUID]})
    (up,) = evs(core, "link")
    assert up["state"] == "up" and up["name"] == "garage" and up["bonded"] is True
    core.on_disconnected(DEV)
    assert evs(core, "link")[-1]["state"] == "down"
    assert evs(core, "status")[-1]["links"] == []


def test_overlong_line_disconnects(core):
    core.on_rx(DEV, b"\n", 247)
    core.on_rx(DEV, b"x" * 4097, 247)
    assert core.radio.disconnected == [DEV]
    assert evs(core, "data") == []


def test_send_chunks_to_min_mtu_and_acks(core):
    assert core.handle_line('{"cmd":"send","addr":"AA:BB:CC:DD:EE:01","line":"x"}') == \
        {"ack": "send", "ok": False, "error": "not linked"}
    core.on_rx(DEV, b"\n", 23)
    core.on_rx(DEV2, b"\n", 247)
    line = "y" * 50
    assert core.handle_line(json.dumps({"cmd": "send", "addr": "aa:bb:cc:dd:ee:01", "line": line})) == \
        {"ack": "send", "ok": True}
    (chunks,) = core.radio.notified
    assert all(len(c) <= 20 for c in chunks)
    assert b"".join(chunks) == b"AA:BB:CC:DD:EE:01 " + line.encode() + b"\n"


def test_frame_tx():
    assert bled.frame_tx("aa:bb:cc:dd:ee:01", '{"a":1}\n') == 'AA:BB:CC:DD:EE:01 {"a":1}'
    framed = bled.frame_tx("AA:BB:CC:DD:EE:01", "{}")
    assert len(framed.split(" ", 1)[0]) == 17 and framed[17] == " "
    with pytest.raises(ValueError):
        bled.frame_tx("bogus", "{}")


def test_framed_line_satellite_side_filter(core):
    """What a satellite does: reassemble, keep lines whose first 17 bytes are its address."""
    core.on_rx(DEV, b"\n", 23)
    core.on_rx(DEV2, b"\n", 23)
    core.handle_line(json.dumps({"cmd": "send", "addr": "AA:BB:CC:DD:EE:02", "line": '{"a":"auth"}'}))
    core.handle_line(json.dumps({"cmd": "send", "addr": "AA:BB:CC:DD:EE:01", "line": '{"x":"żółć"}'}))
    sat1 = bled.LineAssembler()
    got = []
    for chunks in core.radio.notified:
        for c in chunks:
            got += sat1.feed(c)
    mine = [l[18:] for l in got if l[:17] == "AA:BB:CC:DD:EE:01"]
    assert mine == ['{"x":"żółć"}']


def test_max_line_framed_still_chunks(core):
    core.on_rx(DEV, b"\n", 247)
    line = "z" * 4096
    assert core.handle_line(json.dumps({"cmd": "send", "addr": "AA:BB:CC:DD:EE:01", "line": line}))["ok"]
    (chunks,) = core.radio.notified
    assert all(len(c) <= 244 for c in chunks)
    assert b"".join(chunks) == b"AA:BB:CC:DD:EE:01 " + line.encode() + b"\n"


def test_disconnect_command(core):
    assert core.handle_line('{"cmd":"disconnect","addr":"AA:BB:CC:DD:EE:01"}')["ok"] is False
    core.on_connected(DEV, {"addr": "AA:BB:CC:DD:EE:01", "uuids": []})   # pending, not linked
    assert core.handle_line('{"cmd":"disconnect","addr":"AA:BB:CC:DD:EE:01"}')["ok"] is True
    assert core.radio.disconnected == [DEV]


def test_scan_flow(core):
    assert core.handle_line('{"cmd":"scan","seconds":3}') == {"ack": "scan", "ok": True}
    assert core.handle_line('{"cmd":"scan","seconds":3}')["error"] == "scan in progress"
    core.radio.scan_cb([{"addr": "AA:BB:CC:DD:EE:09", "name": "sat", "rssi": -60,
                         "uuids": [bled.SERVICE_UUID]}])
    (scan,) = evs(core, "scan")
    assert scan["devices"][0]["rssi"] == -60
    assert evs(core, "status")[-1]["scanning"] is False


def test_bad_command_ack(core):
    assert core.handle_line('{"cmd":"warp"}') == {"ack": "warp", "ok": False, "error": "unknown cmd 'warp'"}
    assert core.handle_line("{{")["ok"] is False


def test_radio_lost_drops_links_and_reset_reapplies(core):
    core.on_rx(DEV, b"\n", 23)
    core.handle_line('{"cmd":"pairable","seconds":60}')
    core.radio.advertising = False
    core.on_radio_lost()
    assert evs(core, "link")[-1]["state"] == "down" and core.links == {}
    n = len(core.radio.adv_calls)
    core.on_radio_reset()                 # bluetoothd back: pairing state and adv restored
    assert core.radio.pairable[-1] is True
    assert core.radio.adv_calls[n:] == [(1000, 1240, b"\x02\x00")]


def test_status_only_on_change(core):
    core.events.clear()
    core.emit_status()
    assert core.events == []
    core.emit_status(force=True)
    assert len(core.events) == 1


# --- control socket ---------------------------------------------------------------------

def _read_events(sock, n):
    buf = b""
    sock.settimeout(2)
    while buf.count(b"\n") < n:
        buf += sock.recv(65536)
    return [json.loads(l) for l in buf.decode().splitlines()]


def test_control_server_protocol(core, tmp_path):
    path = str(tmp_path / "run" / "bled.sock")
    srv = bled.ControlServer(path, core)
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(path)
        srv.accept_client()
        hello, status = _read_events(c, 2)
        assert hello["ev"] == "hello" and status["ev"] == "status"
        assert status["advertising"] is True and status["pairable"] is False

        c.sendall(b'{"cmd":"pairable","seconds":10}\n{"cmd":"adv","interval_ms":1000,')
        assert srv.on_client_readable()
        c.sendall(b'"flags":1}\n')
        assert srv.on_client_readable()
        got = _read_events(c, 4)
        assert [e.get("ack") or e.get("ev") for e in got] == ["status", "pairable", "status", "adv"]
        assert got[0]["pairable"] is True

        # radio events reach the client asynchronously
        core.on_rx(DEV, b'{"a":1}\n', 64)
        got = _read_events(c, 3)
        assert [e["ev"] for e in got] == ["link", "status", "data"]

        # a second client replaces the first; state persists
        c2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c2.connect(path)
        srv.accept_client()
        hello, status = _read_events(c2, 2)
        assert status["pairable"] is True and status["links"][0]["addr"] == "AA:BB:CC:DD:EE:01"
        assert c.recv(10) == b""          # old client closed

        c2.close()
        assert srv.on_client_readable() is False
        assert srv.client is None
        core.on_disconnected(DEV)          # no client: must not raise
    finally:
        srv.close()


# --- central role (beacon satellites) -------------------------------------------------

BEACON = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_B1"
BEACON_ADDR = "AA:BB:CC:DD:EE:B1"
OTHER = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_B2"


def sighting(addr, name="beacon", rssi=-70):
    return {"addr": addr, "name": name, "rssi": rssi, "uuids": [bled.SERVICE_UUID]}


def test_parse_central():
    c = bled.parse_command('{"cmd":"central","enabled":true,"addresses":["aa:bb:cc:dd:ee:b1",'
                           '"AA:BB:CC:DD:EE:B1","AA-BB-CC-DD-EE-02"]}')
    assert c.name == "central" and c.enabled is True
    assert c.addresses == ("AA:BB:CC:DD:EE:02", "AA:BB:CC:DD:EE:B1")
    assert (c.scan_s, c.period_s) == (8, 20)
    c = bled.parse_command('{"cmd":"central"}')
    assert c.enabled is False and c.addresses == ()
    c = bled.parse_command('{"cmd":"central","enabled":true,"scan_s":0,"period_s":1}')
    assert (c.scan_s, c.period_s) == (1, 1)            # continuous
    c = bled.parse_command('{"cmd":"central","enabled":true,"scan_s":30,"period_s":10}')
    assert c.period_s == 30                            # period never shorter than the scan


@pytest.mark.parametrize("raw,err", [
    ('{"cmd":"central","enabled":1}', "boolean"),
    ('{"cmd":"central","enabled":true,"addresses":"AA:BB:CC:DD:EE:01"}', "list"),
    ('{"cmd":"central","enabled":true,"addresses":["nope"]}', "bad address"),
    ('{"cmd":"central","enabled":true,"scan_s":"8"}', "must be a number"),
])
def test_parse_central_errors(raw, err):
    with pytest.raises(bled.ProtocolError, match=err):
        bled.parse_command(raw)


def test_parse_central_address_cap():
    addrs = ["AA:BB:CC:DD:%02X:%02X" % (i // 256, i % 256) for i in range(bled.CENTRAL_TARGETS_MAX + 1)]
    with pytest.raises(bled.ProtocolError, match="more than"):
        bled.parse_command(json.dumps({"cmd": "central", "enabled": True, "addresses": addrs}))


def test_central_duty_cycle_and_status(core):
    assert core.status()["central"] == {"enabled": False, "scanning": False, "targets": 0, "links": 0}
    ack = core.handle_line(json.dumps({"cmd": "central", "enabled": True, "addresses": [BEACON_ADDR]}))
    assert ack == {"ack": "central", "ok": True}
    assert core.radio.discovery == [True]
    st = evs(core, "status")[-1]["central"]
    assert st == {"enabled": True, "scanning": True, "targets": 1, "links": 0}
    # one timer: the 8 s scan phase
    (secs, fn), = core.scheduler.timers.values()
    assert secs == 8
    core.scheduler.fire_all()                      # scan off, 12 s pause
    assert core.radio.discovery == [True, False] and core.central_scanning is False
    (secs, _), = core.scheduler.timers.values()
    assert secs == 12
    core.scheduler.fire_all()                      # next scan phase
    assert core.radio.discovery == [True, False, True]
    # re-sending the same command (backend reconnect) does not restart the cycle
    core.handle_line(json.dumps({"cmd": "central", "enabled": True, "addresses": [BEACON_ADDR]}))
    assert core.radio.discovery == [True, False, True]
    # the scan phase alone does not produce status events (periodic status carries it)
    n = len(evs(core, "status"))
    core.scheduler.fire_all()
    assert len(evs(core, "status")) == n
    core.handle_line('{"cmd":"central","enabled":false}')
    assert core.scheduler.timers == {} and core.central_enabled is False
    assert evs(core, "status")[-1]["central"]["enabled"] is False


def test_central_continuous_and_retry_when_discovery_fails(core):
    core.radio.discovery_ok = False
    core.handle_line('{"cmd":"central","enabled":true,"scan_s":5,"period_s":5}')
    assert core.central_scanning is False
    (secs, _), = core.scheduler.timers.values()      # retry after one period
    assert secs == 5
    core.radio.discovery_ok = True
    core.scheduler.fire_all()
    assert core.central_scanning is True and core.scheduler.timers == {}   # continuous: no off timer


def test_central_full_flow(core):
    core.handle_line(json.dumps({"cmd": "central", "enabled": True, "addresses": [BEACON_ADDR]}))
    core.events.clear()
    # a non-target sighting is reported but not connected
    core.on_sighting(OTHER, sighting("AA:BB:CC:DD:EE:B2", "stranger"))
    assert core.radio.connects == {}
    (scan,) = evs(core, "scan")
    assert scan["central"] is True and scan["devices"][0]["addr"] == "AA:BB:CC:DD:EE:B2"
    # target sighted: connect; further sightings within 10 s are batched
    core.on_sighting(BEACON, sighting(BEACON_ADDR, rssi=-55))
    assert list(core.radio.connects) == [BEACON]
    assert core.connecting == {BEACON: BEACON_ADDR} and len(evs(core, "scan")) == 1
    core.on_sighting(BEACON, sighting(BEACON_ADDR, rssi=-50))   # no second connect
    assert len(core.radio.connects) == 1
    # BlueZ reports Connected=true while we are setting up: not a peripheral link
    core.on_connected(BEACON, {"addr": BEACON_ADDR, "uuids": [bled.SERVICE_UUID]})
    assert evs(core, "link") == [] and BEACON not in core.pending
    # setup finished -> link up with role central
    core.radio.connects[BEACON](BEACON, True, {"addr": BEACON_ADDR, "name": "door", "bonded": True,
                                               "mtu": 185})
    (up,) = evs(core, "link")
    assert up == {"ev": "link", "state": "up", "addr": BEACON_ADDR, "name": "door", "bonded": True,
                  "mtu": 185, "role": "central"}
    assert evs(core, "status")[-1]["central"]["links"] == 1
    # notifications on the remote TX, split across chunks
    core.on_central_data(BEACON, b'{"a":"au')
    core.on_central_data(BEACON, b'th"}\n\n')
    assert evs(core, "data") == [{"ev": "data", "addr": BEACON_ADDR, "line": '{"a":"auth"}'}]
    # send: written to the remote RX, MTU-3 chunks, no address prefix, no notify
    line = "q" * 400
    assert core.handle_line(json.dumps({"cmd": "send", "addr": BEACON_ADDR, "line": line}))["ok"]
    assert core.radio.notified == []
    (path, chunks), = core.radio.writes
    assert path == BEACON and all(len(c) <= 182 for c in chunks)
    assert b"".join(chunks) == line.encode() + b"\n"
    # the queued sightings come out once the 10 s throttle timer fires
    core.scheduler.timers = {h: t for h, t in core.scheduler.timers.items() if t[0] != 8}
    core.scheduler.fire_all()
    assert [d["addr"] for d in evs(core, "scan")[-1]["devices"]] == [BEACON_ADDR]
    assert evs(core, "scan")[-1]["devices"][0]["rssi"] == -50
    # backend disconnect -> Device1.Disconnect, then link down with role
    assert core.handle_line(json.dumps({"cmd": "disconnect", "addr": BEACON_ADDR}))["ok"]
    assert core.radio.disconnected == [BEACON]
    core.on_disconnected(BEACON)
    down = evs(core, "link")[-1]
    assert down["state"] == "down" and down["role"] == "central"
    # reconnect on a later sighting (after the 5 s retry guard)
    core.on_sighting(BEACON, sighting(BEACON_ADDR))
    assert BEACON not in core.connecting
    core.clock.t += 6
    core.radio.connects.clear()
    core.on_sighting(BEACON, sighting(BEACON_ADDR))
    assert list(core.radio.connects) == [BEACON]


def test_peripheral_send_ignores_central_link_mtu(core):
    core.handle_line(json.dumps({"cmd": "central", "enabled": True, "addresses": [BEACON_ADDR]}))
    core.on_sighting(BEACON, sighting(BEACON_ADDR))
    core.radio.connects[BEACON](BEACON, True, {"mtu": 23})
    core.on_rx(DEV, b"\n", 247)                      # a scanner satellite on our GATT server
    assert core.handle_line(json.dumps({"cmd": "send", "addr": "AA:BB:CC:DD:EE:01", "line": "x" * 100}))["ok"]
    (chunks,) = core.radio.notified
    assert len(chunks) == 1                          # chunked to 247, not the beacon's 23
    core.on_rx(BEACON, b"stray\n", 23)               # writes from our central peer are ignored
    assert evs(core, "data") == []


def test_central_setup_failure_and_disable(core):
    core.handle_line(json.dumps({"cmd": "central", "enabled": True, "addresses": [BEACON_ADDR]}))
    core.on_sighting(BEACON, sighting(BEACON_ADDR))
    core.radio.connects.pop(BEACON)(BEACON, False, {"error": "service not found"})
    assert core.connecting == {} and evs(core, "link") == []
    core.on_sighting(BEACON, sighting(BEACON_ADDR))  # retry guard
    assert core.radio.connects == {}
    core.clock.t += 6
    core.on_sighting(BEACON, sighting(BEACON_ADDR))
    assert BEACON in core.radio.connects
    # disable while connecting: the attempt is cancelled, discovery stops
    core.handle_line('{"cmd":"central","enabled":false}')
    assert core.radio.disconnected == [BEACON] and core.connecting == {}
    assert core.radio.discovery[-1] is False
    core.radio.connects[BEACON](BEACON, True, {})    # late success after cancel -> dropped
    assert evs(core, "link") == [] and core.radio.disconnected == [BEACON, BEACON]
    # sightings while disabled are ignored
    n = len(evs(core, "scan"))
    core.clock.t += 60
    core.on_sighting(OTHER, sighting("AA:BB:CC:DD:EE:B2"))
    assert len(evs(core, "scan")) == n and core.scheduler.timers == {}


def test_central_disable_drops_links_and_target_removal(core):
    core.handle_line(json.dumps({"cmd": "central", "enabled": True, "addresses": [BEACON_ADDR]}))
    core.on_sighting(BEACON, sighting(BEACON_ADDR))
    core.radio.connects[BEACON](BEACON, True, {"mtu": 64})
    core.handle_line('{"cmd":"central","enabled":true,"addresses":[]}')   # un-enrolled
    assert core.radio.disconnected == [BEACON]
    core.on_disconnected(BEACON)
    core.handle_line(json.dumps({"cmd": "central", "enabled": True, "addresses": [BEACON_ADDR]}))
    core.clock.t += 6
    core.on_sighting(BEACON, sighting(BEACON_ADDR))
    core.radio.connects[BEACON](BEACON, True, {"mtu": 64})
    core.handle_line('{"cmd":"central","enabled":false}')
    assert core.radio.disconnected == [BEACON, BEACON]


def test_central_radio_lost_and_reset(core):
    core.handle_line(json.dumps({"cmd": "central", "enabled": True, "addresses": [BEACON_ADDR]}))
    core.on_sighting(BEACON, sighting(BEACON_ADDR))
    core.radio.connects[BEACON](BEACON, True, {"mtu": 64})
    core.on_radio_lost()
    assert core.links == {} and evs(core, "link")[-1]["state"] == "down"
    assert core.central_enabled and not core.central_scanning
    assert all(t[0] != bled.CENTRAL_SCAN_S for t in core.scheduler.timers.values())
    n = len(core.radio.discovery)
    core.on_radio_reset()
    assert core.radio.discovery[n:] == [True] and core.central_scanning


def test_pairing_allowed_only_for_window_or_enrolled_beacon(core):
    assert core.pairing_allowed(BEACON) is False
    core.handle_line(json.dumps({"cmd": "central", "enabled": True, "addresses": [BEACON_ADDR]}))
    assert core.pairing_allowed(BEACON) is True
    assert core.pairing_allowed(OTHER) is False        # not enrolled: refused
    assert core.pairing_allowed("/org/bluez/hci0") is False
    core.handle_line('{"cmd":"central","enabled":false}')
    assert core.pairing_allowed(BEACON) is False       # central off: no exception
    core.handle_line('{"cmd":"pairable","seconds":60}')
    assert core.pairing_allowed(OTHER) is True         # the pairing window admits anyone
