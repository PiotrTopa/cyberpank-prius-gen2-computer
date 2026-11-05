"""Solicited CAN (gateway) setup lifecycle — io.factory.VirtualTwin.

Before 2026-11-05 the mode+subscriptions were sent exactly once, at backend
start, and dropped silently when the gateway port was not open yet (key-off,
``/dev/prius-gateway-pending``); a gateway reboot (relay ch3 cycle) wiped them
and the watchdog could never fire. Now the setup is (re)sent on every serial
(re)connect, on GATEWAY_READY, on a GW_HB uptime regression, and by a
monotonic watchdog while the gateway heartbeats but nothing answers.
"""

import time

from cyberpunk_computer.io.factory import (
    ExecutionMode, VirtualTwinConfig, create_virtual_twin,
)
from cyberpunk_computer.io.ports import RawMessage, DEVICE_SYSTEM, DEVICE_CAN
from cyberpunk_computer.io.serial_io import SerialPort


class FakeOut:
    def __init__(self, connected=True):
        self.connected = connected
        self.sent = []

    def send(self, cmd):
        if not self.connected:
            return False
        self.sent.append(cmd)
        return True

    def is_connected(self):
        return self.connected

    def payloads(self, action=None):
        # Only the gateway CAN device; the VFD egress (device 110) also sends.
        p = [c.payload for c in self.sent if c.device_id == DEVICE_CAN]
        return [x for x in p if action is None or x.get("a") == action] if action else p

    def clear(self):
        self.sent.clear()


def make_twin(connected=True):
    twin = create_virtual_twin(VirtualTwinConfig(mode=ExecutionMode.TEST, log_commands=False))
    out = FakeOut(connected)
    twin.output_port = out
    twin.egress._output_port = out
    twin._enable_solicited = True
    twin.ingress.add_gateway_reset_listener(
        lambda reason: twin.request_solicited_setup("gateway reset: " + reason))
    return twin, out


def sys_msg(d):
    return RawMessage(device_id=DEVICE_SYSTEM, timestamp=time.time(), data=d)


def test_setup_waits_for_the_port_then_goes_out_once():
    twin, out = make_twin(connected=False)
    assert twin.start()
    twin.update()
    assert out.payloads() == []                    # port down: nothing dropped on the floor
    out.connected = True
    twin.request_solicited_setup("gateway serial connected")
    twin.request_solicited_setup("gateway serial connected")   # coalesces
    twin.update()
    acts = [p["a"] for p in out.payloads()]
    assert acts[0] == "mode" and out.payloads()[0]["m"] == "normal"
    assert acts.count("sub") >= 4                  # core group at least
    assert "unsub" not in acts                     # first setup does not clear
    n = len(out.payloads())
    twin.update()
    assert len(out.payloads()) == n                # sent once
    assert twin.solicited_status["setups"] == 1


def test_gateway_ready_triggers_resetup():
    twin, out = make_twin()
    twin.start(); twin.update()
    out.clear()
    twin.ingress._process_message(sys_msg({"msg": "GATEWAY_READY", "ver": "3.2.0", "can": "CAN_READY"}))
    twin.update()
    assert [p["a"] for p in out.payloads()][0] == "mode"
    assert twin.ingress.stats.gateway_resets == 1


def test_uptime_regression_triggers_resetup_but_normal_heartbeats_do_not():
    twin, out = make_twin()
    twin.start(); twin.update()
    out.clear()
    hb = lambda up: sys_msg({"msg": "GW_HB", "ver": "3.2.0", "n": 1, "up": up, "can": 1})
    twin.ingress._process_message(hb(100))
    twin.ingress._process_message(hb(101))
    twin.update()
    assert out.payloads() == []
    twin.ingress._process_message(hb(3))          # rebooted, banner missed
    twin.update()
    assert [p["a"] for p in out.payloads()][0] == "mode"
    assert twin.ingress.stats.gateway_resets == 1


def test_watchdog_resends_with_unsub_all_and_backs_off():
    twin, out = make_twin()
    twin.start(); twin.update()
    out.clear()
    st = twin.ingress.stats
    now = time.monotonic()
    # Gateway alive (heartbeat 1 s ago), setup 20 s ago, never a reply.
    st.last_heartbeat_mono = now - 1.0
    twin._last_setup_mono = now - 20.0
    twin.update()
    acts = [p["a"] for p in out.payloads()]
    assert acts[:2] == ["unsub", "mode"] and out.payloads()[0]["slot"] == "all"
    assert twin.solicited_status["watchdog_resubs"] == 1
    out.clear()
    twin._last_setup_mono = now - 40.0
    twin.update()
    assert out.payloads() == []                    # cooldown (30 s) not elapsed
    twin._last_resub_mono = now - 31.0
    twin._last_setup_mono = now - 40.0
    twin.update()
    assert out.payloads() == []                    # backoff doubled to 60 s
    twin._last_resub_mono = now - 61.0
    twin.update()
    assert [p["a"] for p in out.payloads()][:2] == ["unsub", "mode"]
    assert twin.solicited_status["watchdog_resubs"] == 2


def test_watchdog_silent_while_replies_flow_or_gateway_down():
    twin, out = make_twin()
    twin.start(); twin.update()
    out.clear()
    st = twin.ingress.stats
    now = time.monotonic()
    twin._last_setup_mono = now - 100.0
    # Gateway not heartbeating (key-off): nothing.
    st.last_heartbeat_mono = now - 30.0
    twin.update()
    assert out.payloads() == []
    # Alive and replies arriving: nothing.
    st.last_heartbeat_mono = now - 1.0
    st.last_solicited_mono = now - 2.0
    twin.update()
    assert out.payloads() == []
    # A reply resets the backoff counter.
    twin._resub_count = 3
    twin._last_resub_mono = now - 500.0
    twin.update()
    assert twin.solicited_status["watchdog_resubs"] == 0


def test_solicited_reply_stamps_fw3_tags():
    twin, out = make_twin()
    ing = twin.ingress
    ing._process_message(RawMessage(device_id=DEVICE_CAN, timestamp=0,
                                    data={"a": "resp", "err": "TIMEOUT", "i": "0x7EA"}))
    assert ing.stats.last_solicited_mono == 0.0
    ing._process_message(RawMessage(device_id=DEVICE_CAN, timestamp=0,
                                    data={"a": "sub", "slot": 0, "i": "0x7E8", "d": [4, 65, 12, 0, 0]}))
    assert ing.stats.last_solicited_mono > 0.0
    t = ing.stats.last_solicited_mono
    ing._process_message(RawMessage(device_id=DEVICE_CAN, timestamp=0,
                                    data={"a": "resp", "i": "0x7E8", "d": [4, 65, 12, 0, 0]}))
    assert ing.stats.last_solicited_mono >= t


def test_stop_returns_gateway_to_listen_mode():
    twin, out = make_twin()
    twin.start(); twin.update()
    out.clear()
    twin.stop()
    acts = [(p.get("a"), p.get("slot"), p.get("m")) for p in out.payloads()]
    assert acts == [("unsub", "all", None), ("mode", None, "listen")]


def test_production_twin_wires_connect_and_reset_hooks():
    twin = create_virtual_twin(VirtualTwinConfig(
        mode=ExecutionMode.PRODUCTION, serial_port="/dev/prius-gateway-pending",
        enable_vfd_satellite=False, log_commands=False))
    try:
        port = twin.input_port
        assert isinstance(port, SerialPort)
        assert len(port._connect_hooks) == 1
        assert len(twin.ingress._gateway_reset_listeners) == 1
        # The hook requests a setup; a tick on a closed port sends nothing and
        # leaves nothing pending to retry (the next connect requests again).
        port._connect_hooks[0]()
        assert twin.solicited_status["pending"] == "gateway serial connected"
        twin.update()
        assert twin.solicited_status["setups"] == 0
        assert twin.solicited_status["pending"] is None
    finally:
        twin.avc_scheduler.stop()
