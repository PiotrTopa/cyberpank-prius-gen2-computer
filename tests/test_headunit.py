"""Head unit audio over adb (backend/headunit.py) against a fake adb server
speaking the smart-socket protocol on a local TCP port."""

import re
import shlex
import socket
import threading

import pytest

from cyberpunk_computer.backend import headunit as hu
from cyberpunk_computer.backend.headunit import (
    AdbAbsent, AdbClient, HeadunitConfig, HeadunitController, HeadunitSoundSink,
    alert_cmd, parse_dumpsys_audio, volset_cmd,
)

# Real layout of `dumpsys audio` on the unit (2026-11-17, trimmed).
DUMPSYS = """\
Stream volumes (device: index)
- STREAM_VOICE_CALL:
   Muted: false
   Min: 1
   Max: 5
   Current: 8 (headphone): 4, 40000000 (default): 4
   Devices: headphone
- STREAM_MUSIC:
   Muted: {muted}
   Min: 0
   Max: 63
   Current: 8 (headphone): {vol}, 40000000 (default): 30
   Devices: headphone
- STREAM_ALARM:
   Muted: false
   Min: 0
   Max: 30
   Current: 8 (headphone): 15, 40000000 (default): 14
   Devices: headphone
"""


class FakeUnit:
    def __init__(self, volume=31, muted=False, mute_works=True):
        self.volume = volume
        self.muted = muted
        self.mute_works = mute_works
        self.commands = []

    def run(self, cmd):
        self.commands.append(cmd)
        if cmd == "dumpsys audio":
            return DUMPSYS.format(vol=self.volume, muted="true" if self.muted else "false")
        m = re.fullmatch(r"cmd activity broadcast -a xy\.android\.gtpkey\.volset --ei volset (\d+)", cmd)
        if m:
            self.volume = int(m.group(1))
        elif cmd.endswith("xy.android.gtpkey.mute") and self.mute_works:
            self.muted = not self.muted
        elif cmd.endswith("xy.android.gtpkey.voladd"):
            self.volume = min(63, self.volume + 1)
        elif cmd.endswith("xy.android.gtpkey.voldec"):
            self.volume = max(0, self.volume - 1)
        return "Broadcast completed: result=0\r\n"


class FakeAdbServer:
    def __init__(self, devices, unit=None):
        self.devices = list(devices)       # [(serial, state)]
        self.unit = unit or FakeUnit()
        self.requests = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self):
        self._stop = True
        self.sock.close()

    @staticmethod
    def _read_req(conn):
        n = int(conn.recv(4), 16)
        data = b""
        while len(data) < n:
            data += conn.recv(n - len(data))
        return data.decode()

    @staticmethod
    def _fail(conn, msg):
        b = msg.encode()
        conn.sendall(b"FAIL" + b"%04x" % len(b) + b)

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    req = self._read_req(conn)
                    self.requests.append(req)
                    if req == "host:devices":
                        body = "".join("%s\t%s\n" % d for d in self.devices).encode()
                        conn.sendall(b"OKAY" + b"%04x" % len(body) + body)
                        continue
                    if req.startswith("host:transport:"):
                        serial = req.split(":", 2)[2]
                        if (serial, "device") not in self.devices:
                            self._fail(conn, "device '%s' not found" % serial)
                            continue
                        conn.sendall(b"OKAY")
                        sh = self._read_req(conn)
                        self.requests.append(sh)
                        assert sh.startswith("shell:")
                        conn.sendall(b"OKAY")
                        conn.sendall(self.unit.run(sh[len("shell:"):]).encode())
                        continue
                    self._fail(conn, "unknown request")
                except Exception:
                    pass


@pytest.fixture
def server():
    srvs = []

    def make(devices, unit=None):
        s = FakeAdbServer(devices, unit)
        srvs.append(s)
        return s
    yield make
    for s in srvs:
        s.close()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _ctl(port, serial="MJU8DY59WS8LMB85"):
    cfg = HeadunitConfig(enabled=True, port=port, serial=serial)
    return HeadunitController(cfg, AdbClient(port=port, serial=serial))


# ── parsing / command text ───────────────────────────────────────────────────

def test_parse_dumpsys_music_block_only():
    assert parse_dumpsys_audio(DUMPSYS.format(vol=32, muted="false")) == (32, False)
    assert parse_dumpsys_audio(DUMPSYS.format(vol=0, muted="true")) == (0, True)
    assert parse_dumpsys_audio("nothing here") == (None, None)


def test_volset_text_and_range():
    assert volset_cmd(31) == "cmd activity broadcast -a xy.android.gtpkey.volset --ei volset 31"
    for bad in (-1, 64, 100, 101, True, 3.5, "20"):
        with pytest.raises(ValueError):
            volset_cmd(bad)


def test_media_table():
    assert hu.media_cmd("next").endswith("xy.android.gtpkey.frd")
    with pytest.raises(ValueError):
        hu.media_cmd("eject")


def test_alert_quoting_of_say():
    text = "Uwaga; rm -rf / $(reboot) `id` 'x' \"y\""
    cmd = alert_cmd(tone="880:150,0:80,1320:200", say=text, lang="pl", gain=0.9, alert_id="t1")
    toks = shlex.split(cmd)            # what the remote sh sees
    assert toks[:5] == ["cmd", "activity", "start-foreground-service", "-n",
                        "pl.topa.priusagent/.AlertService"]
    assert toks[toks.index("say") + 1] == text
    assert toks[toks.index("gain") + 1] == "0.80"      # capped
    assert toks[toks.index("tone") + 1] == "880:150,0:80,1320:200"


@pytest.mark.parametrize("kw", [
    {"say": "line\nbreak"}, {"say": "bell\x07"}, {"say": "x" * 301}, {"tone": "880;reboot"},
    {"tone": "880:20000"}, {"say": "ok", "lang": "pl;x"}, {}, {"tone": "880:100", "alert_id": "a b"},
])
def test_alert_rejects(kw):
    with pytest.raises(ValueError):
        alert_cmd(**kw)


# ── adb client ───────────────────────────────────────────────────────────────

def test_device_selection(server):
    s = server([("OTHER1", "unauthorized"), ("ABC", "offline"), ("MJU8DY59WS8LMB85", "device")])
    assert AdbClient(port=s.port, serial=None).pick_device() == "MJU8DY59WS8LMB85"
    s2 = server([("X1", "device"), ("MJU8DY59WS8LMB85", "device")])
    assert AdbClient(port=s2.port, serial="MJU8DY59WS8LMB85").pick_device() == "MJU8DY59WS8LMB85"
    assert AdbClient(port=s2.port, serial="GONE").pick_device() == "X1"


def test_absent_no_device_and_no_server(server):
    s = server([("MJU8DY59WS8LMB85", "unauthorized")])
    with pytest.raises(AdbAbsent):
        AdbClient(port=s.port).pick_device()
    with pytest.raises(AdbAbsent):
        AdbClient(port=_free_port()).pick_device()


def test_shell_roundtrip(server):
    s = server([("MJU8DY59WS8LMB85", "device")], FakeUnit(volume=32))
    out = AdbClient(port=s.port).shell("MJU8DY59WS8LMB85", "dumpsys audio")
    assert parse_dumpsys_audio(out) == (32, False)
    assert "host:transport:MJU8DY59WS8LMB85" in s.requests
    assert "shell:dumpsys audio" in s.requests


# ── controller ───────────────────────────────────────────────────────────────

def test_controller_absent_is_quiet():
    c = _ctl(_free_port())
    c.cycle(vol_target=10)
    st = c.status()
    assert st["present"] is False and st["error"] == "" and st["counters"]["errors"] == 0


def test_controller_volume_set_and_readback(server):
    unit = FakeUnit(volume=31)
    s = server([("MJU8DY59WS8LMB85", "device")], unit)
    c = _ctl(s.port)
    c.cycle(vol_target=20)
    assert "cmd activity broadcast -a xy.android.gtpkey.volset --ei volset 20" in unit.commands
    assert unit.commands[-1] == "dumpsys audio"
    st = c.status()
    assert st["present"] and st["volume"] == 20 and st["muted"] is False


def test_controller_step_uses_current_level(server):
    unit = FakeUnit(volume=31)
    s = server([("MJU8DY59WS8LMB85", "device")], unit)
    c = _ctl(s.port)
    c.cycle(vol_delta=-3)
    assert unit.volume == 28
    c.cycle(vol_delta=+100)
    assert unit.volume == 63


def test_volume_coalescing_latest_wins():
    c = _ctl(_free_port())
    c.set_volume(10)
    c.set_volume(25)
    c.step_volume(2)
    assert c._vol_target == 27 and c._vol_delta == 0


def test_mute_absolute(server):
    unit = FakeUnit(muted=False)
    s = server([("MJU8DY59WS8LMB85", "device")], unit)
    c = _ctl(s.port)
    c.cycle(mute_want=False)                      # already unmuted: no toggle
    assert not any(x.endswith(".mute") for x in unit.commands)
    c.cycle(mute_want=True)
    assert unit.muted is True
    assert sum(x.endswith(".mute") for x in unit.commands) == 1
    c.cycle(mute_want=True)                       # idempotent
    assert sum(x.endswith(".mute") for x in unit.commands) == 1
    assert c.status()["muted"] is True
    c.cycle(mute_want="toggle")
    assert unit.muted is False


def test_mute_not_following_is_reported(server):
    unit = FakeUnit(muted=False, mute_works=False)
    s = server([("MJU8DY59WS8LMB85", "device")], unit)
    c = _ctl(s.port)
    c.cycle(mute_want=True)
    st = c.status()
    assert "mute did not follow" in st["error"] and st["counters"]["errors"] == 1
    assert sum(x.endswith(".mute") for x in unit.commands) == 1     # no toggle loop


def test_media_and_alert_ops(server):
    unit = FakeUnit()
    s = server([("MJU8DY59WS8LMB85", "device")], unit)
    c = _ctl(s.port)
    c.cycle(ops=[("media", {"key": "play_pause"}),
                 ("alert", {"tone": "880:100", "say": "Test", "lang": "pl", "gain": 0.5,
                            "alert_id": "a1", "file": None}),
                 ("alert_stop", {})])
    assert "cmd activity broadcast -a xy.android.gtpkey.playpause" in unit.commands
    assert any("--es say Test" in x and "--es id a1" in x for x in unit.commands)
    assert any(x.endswith("--ez stop true") for x in unit.commands)
    assert c.status()["agent"]["id"] == "a1"


def test_worker_thread_runs(server):
    unit = FakeUnit(volume=12)
    s = server([("MJU8DY59WS8LMB85", "device")], unit)
    c = _ctl(s.port)
    c.start()
    try:
        c.set_volume(40)
        import time
        t0 = time.time()
        while time.time() - t0 < 3 and c.status()["volume"] != 40:
            time.sleep(0.02)
        assert c.status()["volume"] == 40
    finally:
        c.stop()


# ── alert sound sink ─────────────────────────────────────────────────────────

class _FakeCtl:
    def __init__(self, present=True):
        self.present = present
        self.alerts = []

    def alert(self, **kw):
        self.alerts.append(kw)


def test_sound_sink_rate_limit_and_presets():
    now = [100.0]
    ctl = _FakeCtl()
    sink = HeadunitSoundSink(ctl, min_gap_s=3.0, per_id_gap_s=30.0, clock=lambda: now[0])
    assert sink.play("warning", "warning", "headway_warn") is True
    assert ctl.alerts[-1]["tone"] == hu.PRESETS["warning"]["tone"]
    assert ctl.alerts[-1]["gain"] <= hu.GAIN_MAX
    now[0] += 1.0
    assert sink.play("notice", "notice", "curve") is False          # global gap
    now[0] += 3.0
    assert sink.play("notice", "notice", "curve") is True
    now[0] += 5.0
    assert sink.play("notice", "notice", "curve") is False          # same id within 30 s
    now[0] += 30.0
    assert sink.play("soft", "info", "curve") is True
    assert ctl.alerts[-1]["tone"] == hu.PRESETS["chime"]["tone"]
    assert sink.skipped == {"absent": 0, "rate": 1, "same_id": 1}


def test_sound_sink_absent_unit_silent():
    ctl = _FakeCtl(present=False)
    sink = HeadunitSoundSink(ctl)
    assert sink.play("warning", "warning", "x") is False
    assert ctl.alerts == [] and sink.skipped["absent"] == 1


def test_preset_fallback_by_severity():
    assert hu.preset_for("unknown", "warning") == "warning"
    assert hu.preset_for(None, None) == "info"


# ── store + command registry ─────────────────────────────────────────────────

def test_state_mirror_into_audio():
    from cyberpunk_computer.state.actions import SetHeadunitStateAction
    from cyberpunk_computer.state.store import Store
    st = Store()
    st.dispatch(SetHeadunitStateAction({"enabled": True, "present": True, "volume": 32,
                                        "muted": True, "counters": {"polls": 1}}))
    assert st.state.headunit.volume == 32 and st.state.audio.volume == 32
    assert st.state.audio.muted is True
    st.dispatch(SetHeadunitStateAction({"present": False, "volume": 5}))
    assert st.state.audio.volume == 32          # absent: no mirror


def test_commands_build():
    from cyberpunk_computer.api.commands import CommandError, build_command
    a = build_command("headunit_volume", {"level": 30})
    assert a.op == "volume" and a.params == {"level": 30}
    assert build_command("headunit_mute", {"on": "toggle"}).params == {"on": "toggle"}
    assert build_command("headunit_media", {"key": "next"}).params == {"key": "next"}
    al = build_command("headunit_alert", {"sound": "chime", "id": "x1"})
    assert al.params["tone"] == hu.PRESETS["chime"]["tone"] and al.params["alert_id"] == "x1"
    for name, p in (("headunit_volume", {"level": 64}), ("headunit_volume", {}),
                    ("headunit_media", {"key": "eject"}), ("headunit_alert", {"say": "a\nb"}),
                    ("headunit_alert", {"sound": "siren"})):
        with pytest.raises(CommandError):
            build_command(name, p)


def test_backend_wiring_routes_ui_audio_to_headunit(server):
    """BackendService._wire_headunit: UI volume/mute + headunit_* commands reach
    the controller, the alert framework gets the head-unit sound sink."""
    import time
    import types
    from cyberpunk_computer.api.commands import build_command
    from cyberpunk_computer.backend.service import BackendService
    from cyberpunk_computer.state.store import Store

    unit = FakeUnit(volume=31)
    s = server([("MJU8DY59WS8LMB85", "device")], unit)
    store = Store()
    alerts = types.SimpleNamespace(sound=None, manager=types.SimpleNamespace(sound=None))
    fake = types.SimpleNamespace(
        config=types.SimpleNamespace(headunit_config=HeadunitConfig(
            enabled=True, port=s.port, alert_sounds=True)),
        alerts=alerts, headunit=None, headunit_sound=None, twin=types.SimpleNamespace(store=store),
        _headunit_version=-1, _headunit_sound_last=None, _headunit_sound_t=0.0)
    BackendService._wire_headunit(fake, fake.twin)
    assert isinstance(alerts.manager.sound, HeadunitSoundSink)
    assert store.state.headunit.enabled is True
    store.dispatch(build_command("set_volume", {"value": 22}))
    store.dispatch(build_command("headunit_media", {"key": "prev"}))
    fake.headunit.start()
    try:
        t0 = time.time()
        while time.time() - t0 < 3 and not any(c.endswith(".rev") for c in unit.commands):
            time.sleep(0.02)
        time.sleep(0.1)
        BackendService._headunit_tick(fake)
    finally:
        fake.headunit.stop()
    assert unit.volume == 22
    assert store.state.headunit.present and store.state.headunit.volume == 22
    assert store.state.audio.volume == 22
