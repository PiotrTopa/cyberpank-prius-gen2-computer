"""Host-side tests of the powerbox FIRMWARE logic (powerbox/pcf8574.py and
powerbox/main.py, MicroPython) — run on CPython with ``machine``/``neopixel``/
``ina219`` stubbed and ``time.ticks_*`` shimmed.

Covers the 2026-11-03 rework (RELAY-CHANGE-PLAN.md): pin map, NC inversion,
restart relay unreachable through "relay", read-back + re-assert with a fake
bus, non-inverted fan duty, secondary POCO rejection, reset cause reporting.
"""

import importlib.util
import json
import os
import sys
import types

import pytest

POWERBOX_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "powerbox")


# ─── MicroPython environment stubs ───────────────────────────────────────────

class FakePin:
    IN, OUT, PULL_UP, IRQ_FALLING = 0, 1, 2, 4
    log = []                      # (pin, mode, value|pull) for every construction
    levels = {}                   # pin -> last driven output level
    readers = {}                  # pin -> callable giving the input level
    writes = []                   # (pin, level) for every value(v) call

    def __init__(self, pin_no, mode=None, pull=None, value=None):
        self.pin_no = pin_no
        FakePin.log.append((pin_no, mode, pull if value is None else value))
        if value is not None:
            FakePin.levels[pin_no] = value

    def value(self, v=None):
        if v is not None:
            FakePin.levels[self.pin_no] = v
            FakePin.writes.append((self.pin_no, v))
            return None
        reader = FakePin.readers.get(self.pin_no)
        return reader() if reader is not None else 1

    def irq(self, **kw):
        pass


class FakePWM:
    instances = []

    def __init__(self, pin):
        self.pin = pin
        self.duties = []
        self._freq = None
        FakePWM.instances.append(self)

    def freq(self, f=None):
        if f is None:
            return self._freq
        self._freq = f

    def duty_u16(self, d):
        self.duties.append(d)


def _install_stubs():
    machine = types.ModuleType("machine")
    machine.Pin = FakePin
    machine.PWM = FakePWM
    machine.I2C = machine.SoftI2C = object
    machine.freq = lambda *a: None
    machine.reset_cause = lambda: 3
    machine.reset = lambda: None
    machine.disable_irq = lambda: 0
    machine.enable_irq = lambda st: None
    machine.WDT = lambda **kw: types.SimpleNamespace(feed=lambda: None)
    sys.modules["machine"] = machine
    ina = types.ModuleType("ina219")
    ina.INA219 = object
    sys.modules["ina219"] = ina
    neo = types.ModuleType("neopixel")
    sys.modules["neopixel"] = neo
    import time
    if not hasattr(time, "ticks_ms"):
        time.ticks_ms = lambda: int(time.monotonic() * 1000)
        time.ticks_diff = lambda a, b: a - b
        time.ticks_add = lambda a, b: a + b
        time.sleep_ms = lambda ms: None
    if not hasattr(time, "ticks_us"):
        time.ticks_us = lambda: int(time.perf_counter() * 1000000)

        def _sleep_us(us):
            end = time.perf_counter() + us / 1e6
            while time.perf_counter() < end:
                pass
        time.sleep_us = _sleep_us


def _load(name, filename):
    path = os.path.join(POWERBOX_DIR, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fw():
    _install_stubs()
    saved = sys.path[:]
    sys.path.insert(0, POWERBOX_DIR)
    try:
        pcf = _load("pcf8574", "pcf8574.py")
        main = _load("powerbox_main", "main.py")
    finally:
        sys.path[:] = saved
    return types.SimpleNamespace(pcf=pcf, main=main)


class FakeBus:
    """PCF8574-like bus: ``read()``/``write()`` with fault injection."""

    def __init__(self, port=0xFF):
        self.port = port
        self.writes = []
        self.fail_writes = 0      # next N writes raise and are NOT applied
        self.fail_reads = 0

    def read(self):
        if self.fail_reads:
            self.fail_reads -= 1
            raise OSError("ENODEV")
        return self.port

    def write(self, value):
        if self.fail_writes:
            self.fail_writes -= 1
            raise OSError("EIO")
        self.port = value & 0xFF
        self.writes.append(self.port)


class Clock:
    def __init__(self):
        self.t = 1000

    def ticks(self):
        return self.t

    @staticmethod
    def diff(a, b):
        return a - b


def _board(fw, bus=None, clock=None, events=None, errors=None):
    bus = bus or FakeBus()
    clock = clock or Clock()
    return fw.pcf.RelayBoard(
        bus, clock.ticks, Clock.diff,
        on_event=(lambda kind, **f: events.append((kind, f))) if events is not None else None,
        on_error=(lambda code, detail: errors.append((code, detail))) if errors is not None else None,
    ), bus, clock


# ─── Relay logic (pcf8574.RelayBoard) ────────────────────────────────────────

class TestRelayPinMap:
    def test_board_relay_n_is_channel_n(self, fw):
        assert fw.pcf.RELAY_PIN_MAP == {n: n - 1 for n in range(1, 9)}
        assert fw.pcf.RELAY_CHANNELS == (1, 2, 3, 4, 5, 6, 7, 8)
        assert fw.main.RELAY_CH_MFD == 2
        assert fw.main.RELAY_CH_CAN_HUB == 3
        assert fw.main.RELAY_CH_CAM_HUB == 4
        assert fw.main.RELAY_CH_RS485 == 5

    def test_no_channel_on_means_pin_low(self, fw):
        assert fw.pcf.relay_byte(0xFF, 3, True) == 0xFF & ~(1 << 2)
        assert fw.pcf.relay_byte(0x00, 3, False) == (1 << 2)
        assert fw.pcf.relay_is_on(3, 0) == 1 and fw.pcf.relay_is_on(3, 1) == 0

    def test_nc_channel_on_means_pin_high(self, fw):
        assert fw.pcf.RELAY_NC == (1,)
        assert fw.pcf.relay_byte(0x00, 1, True) == 0x01       # released = powered
        assert fw.pcf.relay_byte(0xFF, 1, False) == 0xFE      # energised = cut
        assert fw.pcf.relay_is_on(1, 1) == 1 and fw.pcf.relay_is_on(1, 0) == 0


class TestRelayBoard:
    def test_adopts_latch_and_reports_from_chip(self, fw):
        # Warm reboot: chip already holds ch2 on (pin1 low). Restart pin forced high.
        board, bus, _ = _board(fw, FakeBus(port=0xFF & ~0x02))
        assert bus.writes == []                    # nothing to fix
        states = board.states()
        assert states == [None, 1, 0, 0, 0, 0, 0, 0]
        # Restart relay released in the wanted byte even if the latch said otherwise.
        board2, bus2, _ = _board(fw, FakeBus(port=0xFE))
        assert bus2.writes == [0xFF]

    def test_set_writes_then_updates_cache(self, fw):
        board, bus, _ = _board(fw)
        ok, why = board.set(2, True)
        assert ok and why is None
        assert bus.writes == [0xFF & ~0x02]
        assert board.wanted == 0xFD
        assert board.states()[1] == 1
        ok, _ = board.set(2, False)
        assert ok and board.wanted == 0xFF

    def test_failed_write_does_not_touch_cache(self, fw):
        errors = []
        board, bus, _ = _board(fw, errors=errors)
        bus.fail_writes = 1
        ok, why = board.set(3, True)
        assert not ok and why == "write failed"
        assert board.wanted == 0xFF                # cache unchanged
        assert bus.port == 0xFF
        assert errors and errors[0][0] == "RELAY_IO"
        assert board.err_count == 1

    def test_restart_channel_refused_by_set(self, fw):
        board, bus, _ = _board(fw)
        ok, why = board.set(1, True)
        assert not ok and "hard_restart" in why
        ok, why = board.set(1, False)
        assert not ok
        assert bus.writes == []
        ok, why = board.set(9, True)
        assert not ok and "bad channel" in why

    def test_readback_repairs_lost_write(self, fw):
        events = []
        board, bus, _ = _board(fw, events=events)
        board.set(2, True)
        board.set(5, True)
        bus.port = 0xFF & ~0x02                    # ch5 write "lost" on the wire
        states = board.states()
        assert bus.port == board.wanted            # re-asserted
        assert states[4] == 1                      # reported AFTER the repair
        assert board.fix_count == 1
        assert events == [("rly_fix", {"got": 0xFD, "want": 0xED, "pcf_reset": 0})]
        # Converged: another read-back changes nothing and logs nothing.
        board.states()
        assert board.fix_count == 1 and len(events) == 1

    def test_pcf_reset_detected_and_restored_once_logged(self, fw):
        events = []
        board, bus, _ = _board(fw, events=events)
        board.set(2, True)
        board.set(3, True)
        bus.port = 0xFF                            # PCF power-up state: all released
        board.tick()
        assert bus.port == board.wanted
        assert board.pcf_resets == 1 and board.fix_count == 1
        assert events[-1][1]["pcf_reset"] == 1
        # A chip that keeps resetting counts up without an event per hit.
        bus.port = 0xFF
        board.tick()
        assert board.fix_count == 2 and len(events) == 1

    def test_read_failure_returns_none_and_counts(self, fw):
        errors = []
        board, bus, _ = _board(fw, errors=errors)
        bus.fail_reads = 1
        assert board.states() is None
        assert board.err_count == 1 and errors[0][0] == "RELAY_IO"

    def test_restart_energises_relay_1_and_releases_if_still_alive(self, fw):
        errors = []
        board, bus, clock = _board(fw, errors=errors)
        board.set(2, True)
        assert board.restart()
        assert bus.port == board.wanted & ~0x01    # pin0 low = NC coil energised
        # While the cut is in flight the verifier must not "repair" it.
        clock.t += 1000
        board.tick()
        assert bus.port & 0x01 == 0
        # Still alive after RESTART_RELEASE_MS: release + RESTART_FAILED.
        clock.t += fw.pcf.RESTART_RELEASE_MS
        board.tick()
        assert bus.port == board.wanted
        assert bus.port & 0x01 == 1
        assert board.restart_at is None
        assert any(code == "RESTART_FAILED" for code, _ in errors)


# ─── main.py: command surface, fan, buttons, reset cause ─────────────────────

class Recorder:
    """Capture tx() output by patching sys.stdout.write in the firmware module."""

    def __init__(self, fw):
        self.fw = fw
        self.lines = []

    def __enter__(self):
        self._orig = sys.stdout
        sys.stdout = types.SimpleNamespace(write=self._w, flush=lambda: None)
        self.fw.main.set_host_present(True)       # may flush a parked banner
        return self

    def _w(self, s):
        self.lines.append(json.loads(s)["d"])
        return len(s)

    def __exit__(self, *a):
        sys.stdout = self._orig


@pytest.fixture
def pm(fw):
    """A PowerManager with a RelayBoard on a fake bus, installed as main._pm."""
    FakePin.log.clear()
    m = fw.main
    p = m.PowerManager()
    bus = FakeBus()
    clock = Clock()
    p.relays = fw.pcf.RelayBoard(bus, clock.ticks, Clock.diff,
                                 on_event=m.log_event, on_error=m.tx_error)
    m._pm = p
    yield types.SimpleNamespace(pm=p, bus=bus, clock=clock, cfg=m.Config())
    m._pm = None


def _cmd(fw, payload, cfg):
    fw.main.process_command(json.dumps({"id": 0, "d": payload}), cfg)


class TestCommandSurface:
    def test_version_bumped(self, fw):
        assert fw.main.VERSION == "2.2.3"

    def test_relay_command_sets_load_channels(self, fw, pm):
        with Recorder(fw) as rec:
            _cmd(fw, {"a": "relay", "ch": 3, "on": True}, pm.cfg)
        assert pm.bus.port == 0xFF & ~(1 << 2)
        assert {"ack": "relay_3_on"} in rec.lines

    def test_relay_command_cannot_reach_restart_relay(self, fw, pm):
        with Recorder(fw) as rec:
            _cmd(fw, {"a": "relay", "ch": 1, "on": True}, pm.cfg)
            _cmd(fw, {"a": "relay", "ch": "1", "on": False}, pm.cfg)
        assert pm.bus.writes == []
        errs = [l for l in rec.lines if l.get("msg") == "ERROR"]
        assert len(errs) == 2 and all(e["code"] == "RELAY" for e in errs)
        assert not any("ack" in l for l in rec.lines)

    def test_hard_restart_is_scheduled_then_fires(self, fw, pm):
        import time
        pm.pm.on_poco_hb(1)                      # host alive -> tick keeps TX open
        with Recorder(fw) as rec:
            _cmd(fw, {"a": "hard_restart"}, pm.cfg)
            assert {"ack": "hard_restart"} in rec.lines
            assert pm.pm.restart_due is not None
            assert pm.bus.writes == []           # not yet: ack must leave first
            # Advance past RESTART_ACK_DELAY_MS and tick.
            pm.pm.restart_due = time.ticks_add(time.ticks_ms(), -1)
            pm.pm.last_hb_tx_ms = time.ticks_ms()  # keep STATUS out of the way
            pm.pm.tick(12.3)
        assert pm.bus.port & 0x01 == 0           # relay 1 energised = power cut
        assert pm.pm.restart_due is None
        assert any(l.get("msg") == "HARD_RESTART" for l in rec.lines)
        assert any(l.get("ev") == "hard_restart" for l in rec.lines)

    def test_status_reports_readback_rc_uptime_counters(self, fw, pm):
        pm.pm.relays.set(2, True)
        pm.bus.port = 0xFF                       # chip reset behind our back
        fw.main._reset_cause = 1
        with Recorder(fw) as rec:
            pm.pm._tx_status(0)
        st = [l for l in rec.lines if l.get("msg") == "STATUS"][0]
        assert st["rly"] == [None, 1, 0, 0, 0, 0, 0, 0]   # repaired, then reported
        assert st["rly_fix"] == 1 and st["rly_err"] == 0
        assert st["rc"] == 1 and isinstance(st["up"], int)

    def test_boot_banner_carries_reset_cause_and_is_parked_until_host(self, fw):
        m = fw.main
        m.set_host_present(False)
        m._pending_banner = None
        banner = {"msg": "POWERBOX_READY", "ver": m.VERSION, "role": m.ROLE, "rc": 3}
        assert m.tx(m.ID_SYSTEM, banner) is False
        m._pending_banner = banner
        with Recorder(fw) as rec:                # host appears -> banner goes out once
            pass
        assert rec.lines[0] == banner
        assert m._pending_banner is None


class TestFan:
    def test_duty_not_inverted(self, fw):
        assert fw.main.FAN_PWM_INVERT is False
        assert fw.main.fan_duty_u16(0) == 0
        assert fw.main.fan_duty_u16(100) == 65535
        assert fw.main.fan_duty_u16(50) == 50 * 65535 // 100
        assert fw.main.fan_duty_u16(150) == 65535 and fw.main.fan_duty_u16(-5) == 0
        # The inverted form stays available for an open-drain stage.
        assert fw.main.fan_duty_u16(100, invert=True) == 0

    def test_pins_and_frequency(self, fw):
        assert fw.main.FAN_PWM_PIN == 29 and fw.main.FAN_TACH_PIN == 28
        assert fw.main.FAN_PWM_FREQ == 25000
        FakePWM.instances.clear()
        f = fw.main.Fan()
        f.setup()
        assert f.pwm.pin.pin_no == 29 and f.pwm._freq == 25000
        assert f.set_pct(40) and f.pwm.duties[-1] == 40 * 65535 // 100
        assert f.set_pct(0) and f.pwm.duties[-1] == 0

    def test_fan_command_percent_and_legacy_duty(self, fw, pm):
        FakePWM.instances.clear()
        fw.main._fan = fw.main.Fan()
        fw.main._fan.setup()
        with Recorder(fw) as rec:
            _cmd(fw, {"a": "fan", "pct": 30}, pm.cfg)
            _cmd(fw, {"a": "fan", "duty": 65535}, pm.cfg)
        assert fw.main._fan.pwm.duties[-2:] == [30 * 65535 // 100, 65535]
        assert {"ack": "fan"} in rec.lines
        fw.main._fan = None


class TestButtons:
    def test_pins(self, fw):
        assert fw.main.POCO1_BTN_PIN == 27
        assert fw.main.POCO2_BTN_PIN is None
        assert 14 not in (fw.main.POCO1_BTN_PIN, fw.main.FAN_PWM_PIN, fw.main.FAN_TACH_PIN,
                          fw.main.ACC_PIN, fw.main.LED_PIN)
        assert 15 not in (fw.main.POCO1_BTN_PIN, fw.main.FAN_PWM_PIN, fw.main.FAN_TACH_PIN,
                          fw.main.ACC_PIN, fw.main.LED_PIN)
        # Button pins never collide with the fan pins (a fan PWM on a button
        # line presses the phone's power button).
        assert fw.main.POCO1_BTN_PIN not in (fw.main.FAN_PWM_PIN, fw.main.FAN_TACH_PIN)

    def test_main_button_released_as_input_no_pulls(self, fw, pm):
        # PowerManager() constructed GP27 as an input with pulls off; GP28 untouched.
        assert (27, FakePin.IN, None) in FakePin.log
        assert not any(p == 28 for p, _, _ in FakePin.log)

    def test_press_drives_low_then_releases(self, fw, pm):
        FakePin.log.clear()
        with Recorder(fw):
            assert pm.pm.btn1.press(300, "test") is True
        assert FakePin.log == [(27, FakePin.OUT, 0), (27, FakePin.IN, None)]

    def test_secondary_commands_rejected_cleanly(self, fw, pm):
        FakePin.log.clear()
        with Recorder(fw) as rec:
            _cmd(fw, {"a": "button", "poco": 2, "ms": 6000}, pm.cfg)
            _cmd(fw, {"a": "wake", "poco": 2}, pm.cfg)
            pm.pm.tick(None)                     # nothing queued to press
        assert pm.pm.pending == []
        assert FakePin.log == []                 # no GPIO touched
        errs = [l for l in rec.lines if l.get("msg") == "ERROR"]
        assert [e["code"] for e in errs] == ["POCO2_NC", "POCO2_NC"]
        assert not any(l.get("ack") in ("button", "wake") for l in rec.lines)

    def test_main_button_and_wake_still_work(self, fw, pm):
        with Recorder(fw) as rec:
            _cmd(fw, {"a": "button", "poco": 1, "ms": 6000}, pm.cfg)
            _cmd(fw, {"a": "wake"}, pm.cfg)
        assert pm.pm.pending == [(1, 6000)]
        assert {"ack": "button"} in rec.lines and {"ack": "wake"} in rec.lines
        pm.pm.pending.clear()

    def test_p2_liveness_report_still_accepted(self, fw, pm):
        with Recorder(fw) as rec:
            _cmd(fw, {"a": "p2", "alive": True}, pm.cfg)
        assert {"ack": "p2"} in rec.lines
        assert pm.pm.p2_alive() is True


class TestButtonSafety:
    """2026-11-04: an RP2040 reset pulls GP27 down (= button held) until code
    releases it; the phone boot-looped while the firmware was not running."""

    def test_force_and_max_hold(self, fw):
        assert fw.main.POCO_BTN_FORCE_MS == 12000
        assert fw.main.POCO_BTN_MAX_MS <= 15000
        assert fw.main.clamp_press_ms(40000) == fw.main.POCO_BTN_MAX_MS

    def test_press_released_even_if_interrupted(self, fw, pm, monkeypatch):
        def boom(ms):
            raise KeyboardInterrupt
        monkeypatch.setattr(fw.main, "wdt_sleep_ms", boom)
        FakePin.log.clear()
        with Recorder(fw):
            with pytest.raises(KeyboardInterrupt):
                pm.pm.btn1.press(6000, "test")
        assert FakePin.log[-1] == (27, FakePin.IN, None)

    def test_active_high_polarity(self, fw, pm, monkeypatch):
        monkeypatch.setattr(fw.main, "POCO_BTN_ACTIVE_HIGH", True)
        FakePin.log.clear()
        with Recorder(fw):
            pm.pm.btn1.press(300, "test")
        assert FakePin.log == [(27, FakePin.OUT, 1), (27, FakePin.OUT, 0)]

    def test_boot_py_matches_main(self, fw):
        src = open(os.path.join(POWERBOX_DIR, "boot.py")).read()
        ns = {}
        exec(compile(src, "boot.py", "exec"), {"__name__": "boot"}, ns)
        assert ns["ACTIVE_HIGH"] == fw.main.POCO_BTN_ACTIVE_HIGH
        assert ns["BTN_PIN"] == fw.main.POCO1_BTN_PIN
        # boot.py leaves the button released ...
        btn = [e for e in FakePin.log if e[0] == 27]
        assert btn[-1][1:] == (FakePin.IN, None)
        # ... and the BEAN TX (GP7) driven LOW, RX (GP6) a plain input.
        assert (7, FakePin.OUT, 0) in FakePin.log
        assert (6, FakePin.IN, None) in FakePin.log
        assert ns["BEAN_TX_PIN"] == fw.main.BEAN_TX_PIN == 7
        assert ns["BEAN_RX_PIN"] == fw.main.BEAN_RX_PIN == 6


class TestFirmwareUpload:
    def _put(self, fw, cfg, data, name="main.py", sha=None, chunk=100):
        import base64, hashlib
        sha = sha or hashlib.sha256(data).hexdigest()
        _cmd(fw, {"a": "put_begin", "name": name, "size": len(data), "sha": sha}, cfg)
        for k in range(0, (len(data) + chunk - 1) // chunk):
            _cmd(fw, {"a": "put", "seq": k,
                      "b64": base64.b64encode(data[k * chunk:(k + 1) * chunk]).decode()}, cfg)
        _cmd(fw, {"a": "put_end"}, cfg)

    def test_upload_swaps_and_keeps_backup(self, fw, pm, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "main.py").write_bytes(b"old")
        data = bytes(range(256)) * 5
        with Recorder(fw) as rec:
            self._put(fw, pm.cfg, data)
        assert (tmp_path / "main.py").read_bytes() == data
        assert (tmp_path / "main.py.bak").read_bytes() == b"old"
        assert not (tmp_path / "main.py.new").exists()
        assert {"msg": "PUT", "st": "ok", "name": "main.py"} in rec.lines
        assert not any(l.get("msg") == "ERROR" for l in rec.lines)

    def test_bad_hash_leaves_old_file(self, fw, pm, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "main.py").write_bytes(b"old")
        with Recorder(fw) as rec:
            self._put(fw, pm.cfg, b"new firmware", sha="0" * 64)
        assert (tmp_path / "main.py").read_bytes() == b"old"
        assert not (tmp_path / "main.py.new").exists()
        errs = [l for l in rec.lines if l.get("msg") == "ERROR"]
        assert errs and errs[-1]["code"] == "PUT"

    def test_duplicate_chunk_is_reacked_not_rewritten(self, fw, pm, tmp_path, monkeypatch):
        import base64, hashlib
        monkeypatch.chdir(tmp_path)
        data = b"abcdefgh"
        with Recorder(fw) as rec:
            _cmd(fw, {"a": "put_begin", "name": "boot.py", "size": 8,
                      "sha": hashlib.sha256(data).hexdigest()}, pm.cfg)
            for seq, part in ((0, b"abcd"), (0, b"abcd"), (1, b"efgh")):
                _cmd(fw, {"a": "put", "seq": seq, "b64": base64.b64encode(part).decode()}, pm.cfg)
            _cmd(fw, {"a": "put_end"}, pm.cfg)
        assert (tmp_path / "boot.py").read_bytes() == data
        assert {"msg": "PUT", "st": "ok", "name": "boot.py"} in rec.lines

    def test_unknown_name_refused(self, fw, pm, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with Recorder(fw) as rec:
            _cmd(fw, {"a": "put_begin", "name": "../x.py", "size": 1, "sha": "0" * 64}, pm.cfg)
        assert rec.lines[-1]["code"] == "PUT"
        assert list(tmp_path.iterdir()) == []

    def test_reboot_releases_buttons_then_resets(self, fw, pm, monkeypatch):
        called = []
        monkeypatch.setattr(fw.main.machine, "reset", lambda: called.append("reset"))
        FakePin.log.clear()
        with Recorder(fw) as rec:
            _cmd(fw, {"a": "reboot"}, pm.cfg)
        assert {"ack": "reboot"} in rec.lines
        assert (27, FakePin.IN, None) in FakePin.log
        assert called == ["reset"]


class TestBootPyRecovery:
    def test_restores_main_after_interrupted_swap(self, fw, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "main.py.bak").write_bytes(b"old")
        (tmp_path / "main.py.new").write_bytes(b"half")
        src = open(os.path.join(POWERBOX_DIR, "boot.py")).read()
        exec(compile(src, "boot.py", "exec"), {"__name__": "boot"})
        assert (tmp_path / "main.py").read_bytes() == b"old"


class TestWakeLadderRebootGrace:
    """2026-11-04: during a `systemctl reboot` the ladder pressed 6 s ~15 s
    after the last heartbeat, i.e. on a booting phone."""

    def _setup(self, fw, pm, monkeypatch):
        t = [10_000_000]
        monkeypatch.setattr(fw.main.time, "ticks_ms", lambda: t[0])
        presses = []
        monkeypatch.setattr(pm.pm, "_press", lambda poco, ms, why: presses.append((ms, why)))
        p = pm.pm
        p.boot_ms = t[0] - 10 * 60_000          # long past our own boot grace
        p.last_btn_ms = t[0] - 10 * 60_000      # cooled down
        return t, presses

    def test_no_press_while_a_recently_alive_poco_reboots(self, fw, pm, monkeypatch):
        t, presses = self._setup(fw, pm, monkeypatch)
        with Recorder(fw):
            pm.pm.on_poco_hb(1)
            t[0] += 90_000                       # 90 s quiet: rebooting
            pm.pm.tick(12.0)
        assert presses == []

    def test_presses_once_the_lost_grace_expires(self, fw, pm, monkeypatch):
        t, presses = self._setup(fw, pm, monkeypatch)
        with Recorder(fw):
            pm.pm.on_poco_hb(1)
            t[0] += fw.main.POCO_LOST_GRACE_MS + 1000
            pm.pm.tick(12.0)
        assert presses == [(fw.main.POCO_BTN_PRESS_MS, "wake")]

    def test_cold_start_still_presses_after_boot_grace(self, fw, pm, monkeypatch):
        t, presses = self._setup(fw, pm, monkeypatch)
        with Recorder(fw):
            pm.pm.tick(12.0)                     # never saw a heartbeat
        assert presses == [(fw.main.POCO_BTN_PRESS_MS, "wake")]


# ─── fw 2.2.3: relay read-back cadence (5 ms) ────────────────────────────────

class FakeClockMs:
    """Deterministic ms clock: sleep_ms advances it."""

    def __init__(self, t=50_000):
        self.t = t

    def ticks(self):
        return self.t

    def sleep(self, ms):
        self.t += ms


class TestRelayVerifyCadence:
    """2026-11-11: the PCF8574 keeps resetting to 0xFF in the field; the
    read-back must re-assert within the relays' ~5 ms release time. Before
    2.2.3 it only ran once per main-loop pass (~1 s)."""

    def _clock(self, fw, pm, monkeypatch):
        c = FakeClockMs()
        monkeypatch.setattr(fw.main.time, "ticks_ms", c.ticks)
        monkeypatch.setattr(fw.main.time, "sleep_ms", c.sleep)
        pm.pm.relay_verify_ms = c.t
        return c

    def test_constant(self, fw):
        assert fw.main.RELAY_VERIFY_MS == 5

    def test_main_loop_sleep_reads_back_every_5_ms(self, fw, pm, monkeypatch):
        c = self._clock(fw, pm, monkeypatch)
        reads = []
        orig = pm.bus.read
        pm.bus.read = lambda: (reads.append(c.t), orig())[1]
        with Recorder(fw):
            fw.main.wdt_sleep_ms(1000)            # the inter-telemetry sleep
        assert c.t - 50_000 == 1000               # deadline kept exactly
        assert len(reads) == 200
        gaps = {b - a for a, b in zip(reads, reads[1:])}
        assert gaps == {5}

    def test_pcf_reset_repaired_within_5_ms_and_logged_once(self, fw, pm, monkeypatch):
        c = self._clock(fw, pm, monkeypatch)
        pm.pm.relays.set(2, True)
        want = pm.pm.relays.wanted
        reset_at = []

        def sleep(ms):                            # chip resets 3 ms into the wait
            c.t += ms
            if not reset_at and c.t - 50_000 >= 3:
                pm.bus.port = 0xFF
                reset_at.append(c.t)
        monkeypatch.setattr(fw.main.time, "sleep_ms", sleep)
        writes_before = len(pm.bus.writes)
        with Recorder(fw) as rec:
            fw.main.wdt_sleep_ms(50)
        assert pm.bus.port == want
        assert pm.bus.writes[writes_before:] == [want]   # one repair write
        assert pm.pm.relays.pcf_resets == 1
        ev = [l for l in rec.lines if l.get("ev") == "rly_fix"]
        assert len(ev) == 1 and ev[0]["pcf_reset"] == 1

    def test_streak_logs_first_mismatch_only(self, fw, pm, monkeypatch):
        c = self._clock(fw, pm, monkeypatch)
        pm.pm.relays.set(3, True)

        def sleep(ms):                            # the write never sticks
            c.t += ms
            pm.bus.port = 0xFF
        monkeypatch.setattr(fw.main.time, "sleep_ms", sleep)
        with Recorder(fw) as rec:
            fw.main.wdt_sleep_ms(100)
        assert pm.pm.relays.fix_count >= 19
        assert len([l for l in rec.lines if l.get("ev") == "rly_fix"]) == 1

    def test_io_errors_counted_but_reported_at_most_once_a_second(self, fw, pm, monkeypatch):
        c = self._clock(fw, pm, monkeypatch)
        pm.pm.relays._ticks = c.ticks
        pm.bus.fail_reads = 10_000
        with Recorder(fw) as rec:
            fw.main.wdt_sleep_ms(2000)
        errs = [l for l in rec.lines if l.get("code") == "RELAY_IO"]
        assert pm.pm.relays.err_count == 400
        assert len(errs) == 2

    def test_button_hold_keeps_relays_serviced(self, fw, pm, monkeypatch):
        c = self._clock(fw, pm, monkeypatch)
        pm.pm.relays.set(2, True)
        want = pm.pm.relays.wanted

        def sleep(ms):
            c.t += ms
            if c.t - 50_000 == 150:
                pm.bus.port = 0xFF
        monkeypatch.setattr(fw.main.time, "sleep_ms", sleep)
        with Recorder(fw):
            pm.pm.btn1.press(300, "test")
        assert pm.bus.port == want and pm.pm.relays.pcf_resets == 1

    def test_tick_still_paced(self, fw, pm, monkeypatch):
        c = self._clock(fw, pm, monkeypatch)
        reads = []
        orig = pm.bus.read
        pm.bus.read = lambda: (reads.append(c.t), orig())[1]
        pm.pm.last_hb_tx_ms = c.t                 # keep STATUS out of the way
        with Recorder(fw):
            pm.pm.tick(12.0)                      # < 5 ms since last check
            c.t += 5
            pm.pm.tick(12.0)                      # due: one read
            c.t += 3
            pm.pm.tick(12.0)                      # 3 ms later: not yet
        assert reads == [50_005]


# ─── fw 2.2.3: BEAN transceiver bench (GP7 TX, GP6 RX inverted) ─────────────

@pytest.fixture
def bean(fw, pm):
    """A Bean on stub pins; RX mirrors TX inverted (a working transceiver)."""
    FakePin.levels.clear()
    FakePin.writes.clear()
    FakePin.readers.clear()
    FakePin.readers[6] = lambda: 1 - FakePin.levels.get(7, 0)
    b = fw.main.Bean()
    fw.main._bean = b
    yield types.SimpleNamespace(bean=b, cfg=pm.cfg)
    fw.main._bean = None
    FakePin.readers.clear()


def _bean_cmd(fw, cfg, **kw):
    with Recorder(fw) as rec:
        _cmd(fw, dict({"a": "bean"}, **kw), cfg)
    acks = [l for l in rec.lines if l.get("ack") == "bean"]
    errs = [l for l in rec.lines if l.get("msg") == "ERROR"]
    return rec.lines, acks, errs


class TestBean:
    def test_pins_free_and_initialised_safe(self, fw, bean):
        assert fw.main.BEAN_TX_PIN == 7 and fw.main.BEAN_RX_PIN == 6
        used = (fw.main.POCO1_BTN_PIN, fw.main.FAN_PWM_PIN, fw.main.FAN_TACH_PIN,
                fw.main.ACC_PIN, fw.main.LED_PIN, 0, 1, 14, 15)
        assert 6 not in used and 7 not in used
        assert (7, FakePin.OUT, 0) in FakePin.log         # TX output LOW
        assert (6, FakePin.IN, None) in FakePin.log       # RX no pull

    def test_status(self, fw, bean):
        lines, (ack,), errs = _bean_cmd(fw, bean.cfg, op="status")
        assert ack == {"ack": "bean", "op": "status", "tx": 0, "rx": 1, "bus": 0}
        assert not errs
        # PMEVENT echo for the journal, not stored in the PMLOG ring.
        assert any(l.get("msg") == "PMEVENT" and l.get("ev") == "bean" for l in lines)
        assert not any(e.get("ev") == "bean" for e in fw.main._evlog)

    def test_status_bus_dominant(self, fw, bean):
        FakePin.readers[6] = lambda: 0                     # bus held at +B
        _, (ack,), _ = _bean_cmd(fw, bean.cfg, op="status")
        assert ack["rx"] == 0 and ack["bus"] == 1

    def test_tx_samples_driven_and_released_then_forces_low(self, fw, bean):
        _, (ack,), errs = _bean_cmd(fw, bean.cfg, op="tx", v=1, ms=2)
        assert not errs
        assert ack["v"] == 1 and ack["ms"] == 2
        assert ack["rx_on"] == 0 and ack["rx_off"] == 1
        assert FakePin.levels[7] == 0
        assert FakePin.writes[0] == (7, 1) and FakePin.writes[-1] == (7, 0)
        assert bean.bean.level == 0

    def test_tx_default_ms_and_cap(self, fw, bean):
        _, (ack,), _ = _bean_cmd(fw, bean.cfg, op="tx")
        assert ack["ms"] == 1 and ack["v"] == 1
        FakePin.writes.clear()
        _, acks, errs = _bean_cmd(fw, bean.cfg, op="tx", v=1, ms=501)
        assert not acks and errs[0]["code"] == "BEAN_ARGS"
        assert (7, 1) not in FakePin.writes                # never driven
        _, acks, errs = _bean_cmd(fw, bean.cfg, op="tx", v=2, ms=5)
        assert not acks and errs[0]["code"] == "BEAN_ARGS"

    def test_tx_released_when_the_hold_is_interrupted(self, fw, bean, monkeypatch):
        def boom(ms):
            raise RuntimeError("bus fault")
        monkeypatch.setattr(fw.main, "wdt_sleep_ms", boom)
        _, acks, errs = _bean_cmd(fw, bean.cfg, op="tx", v=1, ms=100)
        assert not acks and errs[0]["code"] == "BEAN"
        assert FakePin.levels[7] == 0 and bean.bean.level == 0

    def test_pulse(self, fw, bean):
        _, (ack,), errs = _bean_cmd(fw, bean.cfg, op="pulse", us=200)
        assert not errs
        assert ack["rx_mid"] == 0 and ack["rx_after"] == 1
        assert ack["us"] == 200 and ack["us_real"] >= 200
        assert FakePin.levels[7] == 0
        _, acks, errs = _bean_cmd(fw, bean.cfg, op="pulse", us=2001)
        assert not acks and errs[0]["code"] == "BEAN_ARGS"

    def test_loop_measures_both_edges(self, fw, bean):
        _, (ack,), errs = _bean_cmd(fw, bean.cfg, op="loop", n=5)
        assert not errs
        assert ack["n"] == 5 and ack["timeouts"] == 0 and ack["rx_idle"] == 1
        for edge in ("dom", "rec"):
            assert 0 <= ack[edge + "_min"] <= ack[edge + "_avg"] <= ack[edge + "_max"]
            assert ack[edge + "_max"] < fw.main.BEAN_EDGE_TIMEOUT_US
        assert FakePin.levels[7] == 0

    def test_loop_counts_timeouts_on_a_dead_receiver(self, fw, bean):
        FakePin.readers[6] = lambda: 1                     # RX never follows
        _, (ack,), _ = _bean_cmd(fw, bean.cfg, op="loop", n=2)
        assert ack["to_dom"] == 2 and ack["timeouts"] == 2
        assert ack["dom_min"] is None and ack["dom_avg"] is None
        assert FakePin.levels[7] == 0
        _, acks, errs = _bean_cmd(fw, bean.cfg, op="loop", n=201)
        assert not acks and errs[0]["code"] == "BEAN_ARGS"

    def test_square_counts_every_edge(self, fw, bean):
        _, (ack,), errs = _bean_cmd(fw, bean.cfg, op="square", hz=1000, ms=20)
        assert not errs
        assert ack["cycles"] >= 1
        assert ack["edges"] == ack["expected"] == 2 * ack["cycles"]
        assert ack["hz_real"] is not None and ack["hz_real"] <= 1000
        assert FakePin.levels[7] == 0                      # ends released

    def test_square_dead_receiver_counts_nothing(self, fw, bean):
        FakePin.readers[6] = lambda: 1
        _, (ack,), _ = _bean_cmd(fw, bean.cfg, op="square", hz=2000, ms=5)
        assert ack["edges"] == 0 and ack["expected"] > 0

    def test_square_caps(self, fw, bean):
        for kw in ({"hz": 99}, {"hz": 20001}, {"ms": 1001}, {"ms": 0}, {"hz": "x"}):
            _, acks, errs = _bean_cmd(fw, bean.cfg, op="square", **kw)
            assert not acks and errs[0]["code"] == "BEAN_ARGS", kw

    def test_unknown_op_and_bad_types(self, fw, bean):
        for kw in ({"op": "blast"}, {"op": "tx", "ms": None}, {"op": "tx", "v": True}):
            _, acks, errs = _bean_cmd(fw, bean.cfg, **kw)
            assert not acks and errs[0]["code"] == "BEAN_ARGS", kw
        assert FakePin.levels.get(7, 0) == 0

    def test_main_loop_guard_releases_tx(self, fw, bean):
        bean.bean.tx.value(1)
        fw.main.bean_release()
        assert FakePin.levels[7] == 0
