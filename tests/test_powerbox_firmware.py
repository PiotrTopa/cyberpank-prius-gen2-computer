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

    def __init__(self, pin_no, mode=None, pull=None, value=None):
        self.pin_no = pin_no
        FakePin.log.append((pin_no, mode, pull if value is None else value))

    def value(self):
        return 1

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
        assert fw.main.VERSION == "2.1.1"

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
