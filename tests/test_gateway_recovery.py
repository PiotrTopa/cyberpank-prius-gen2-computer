"""Tests for BackendService._gateway_recover_tick — gateway VBUS auto-recovery.

A wedged gateway MCU (enumerated but no GW_HB heartbeats) is only fixed by a
cold VBUS cycle on relay ch3 (the CAN hub). The tick must:
  * cycle only when stale long enough AND VBUS is actually on;
  * respect an operator's deliberate port-off (desired=False);
  * respect the cooldown;
  * re-power after the off window (two-phase, non-blocking).
"""

import time
import types

from cyberpunk_computer.backend.service import BackendService, BackendConfig


class FakeGateway:
    def __init__(self, powered=True, desired=None):
        self.relay_ch = 3
        self.powered = powered
        self._desired = desired
        self.calls = []

    @property
    def desired(self):
        return self._desired

    def set(self, on):
        self.calls.append(bool(on))
        self._desired = bool(on)
        self.powered = bool(on)
        return True

    def read(self):
        return self.powered


def make_service(hb_age_s=60.0, stale=True, powered=True, desired=None, hb_seen=True):
    svc = BackendService.__new__(BackendService)
    svc.config = BackendConfig()
    svc._gw_stale = stale
    svc._gw_recover_off_at = None
    svc._gw_recover_last = 0.0
    svc._gw_recover_epoch = time.monotonic() - hb_age_s
    svc._start_time = time.monotonic() - hb_age_s
    svc._gw_recover_attempts = 0
    svc._gw_vbus_on_at = time.monotonic() - hb_age_s   # VBUS on since the last HB
    gw = FakeGateway(powered=powered, desired=desired)
    svc.port_power = types.SimpleNamespace(gateway=gw)
    # The state carries a wall-clock heartbeat stamp (display); the service
    # ages it on the monotonic clock from when it last changed (_mono_seen).
    last_hb = (time.time() - hb_age_s) if hb_seen else None
    svc._seen_marks = {"gateway_hb": (last_hb, time.monotonic() - hb_age_s)} if hb_seen else {}
    conn = types.SimpleNamespace(last_heartbeat_time=last_hb)
    svc.twin = types.SimpleNamespace(
        store=types.SimpleNamespace(state=types.SimpleNamespace(connection=conn))
    )
    return svc, gw


def test_cycles_when_stale_and_powered():
    svc, gw = make_service()
    svc._gateway_recover_tick()
    assert gw.calls == [False]
    assert svc._gw_recover_off_at is not None


def test_no_cycle_right_after_vbus_on_at_key_on():
    """Key-on re-powers relay ch3 after minutes of key-off silence: the stale
    clock restarts at the VBUS edge instead of counting the key-off period
    (a 737 s "stall" cold-cycled a healthy, booting gateway on 2026-11-04)."""
    svc, gw = make_service(hb_age_s=737.0, powered=False)
    svc._gw_vbus_on_at = None
    svc._gateway_recover_tick()          # VBUS off -> nothing
    assert gw.calls == []
    gw.powered = True                    # relay ch3 just came on
    svc._gateway_recover_tick()
    assert gw.calls == []                # 0 s since VBUS-on: no cycle
    svc._gw_vbus_on_at -= 31.0           # 31 s of VBUS, still silent
    svc._gateway_recover_tick()
    assert gw.calls == [False]           # now it is a real stall


def test_vbus_edge_resets_after_recovery_repower():
    """After the two-phase cycle re-powers the port, the next judgement waits
    a full stale window from that re-power (cooldown aside)."""
    svc, gw = make_service()
    svc.config.gateway_recover_cooldown_s = 0.0
    svc._gateway_recover_tick()
    assert gw.calls == [False]
    svc._gw_recover_off_at = time.monotonic() - 10.0   # off window elapsed
    svc._gateway_recover_tick()
    assert gw.calls == [False, True]
    svc._gateway_recover_tick()                   # VBUS-on edge just now
    assert gw.calls == [False, True]


def test_repowers_after_off_window():
    svc, gw = make_service()
    svc._gateway_recover_tick()
    # Still inside the off window: nothing more happens.
    svc._gateway_recover_tick()
    assert gw.calls == [False]
    # Force the off window to elapse.
    svc._gw_recover_off_at = time.monotonic() - svc.config.gateway_recover_off_s - 1
    svc._gateway_recover_tick()
    assert gw.calls == [False, True]
    assert svc._gw_recover_off_at is None


def test_skips_when_not_stale_enough():
    svc, gw = make_service(hb_age_s=10.0)  # < gateway_recover_stale_s (30)
    svc._gateway_recover_tick()
    assert gw.calls == []


def test_skips_when_vbus_already_off():
    # ACC power-save: gateway legitimately unpowered — never cycle.
    svc, gw = make_service(powered=False)
    svc._gateway_recover_tick()
    assert gw.calls == []


def test_skips_when_operator_wants_port_off():
    svc, gw = make_service(desired=False)
    svc._gateway_recover_tick()
    assert gw.calls == []


def test_respects_cooldown():
    svc, gw = make_service()
    svc._gw_recover_last = time.monotonic()  # a cycle just happened
    svc._gateway_recover_tick()
    assert gw.calls == []


def test_disabled_by_config():
    svc, gw = make_service()
    svc.config.gateway_auto_recover = False
    svc._gateway_recover_tick()
    assert gw.calls == []


def test_watchdog_not_latched():
    svc, gw = make_service(stale=False)
    svc._gateway_recover_tick()
    assert gw.calls == []


def test_no_heartbeat_ever_recovers_from_epoch():
    # Gateway wedged before the backend started: no HB this boot, watchdog
    # never armed — recovery must still trip, measured from backend start.
    svc, gw = make_service(hb_age_s=60.0, stale=False, hb_seen=False)
    svc._gateway_recover_tick()
    assert gw.calls == [False]


def test_no_heartbeat_ever_fresh_epoch_waits():
    svc, gw = make_service(hb_age_s=5.0, stale=False, hb_seen=False)
    svc._gateway_recover_tick()
    assert gw.calls == []


def test_backoff_doubles_and_heartbeat_resets_it():
    # HB long gone (before any cycle) so it never resets the backoff here.
    svc, gw = make_service(hb_age_s=4000.0)
    cfg = svc.config

    def cycle():
        svc._gateway_recover_tick()          # off
        svc._gw_recover_off_at = time.monotonic() - cfg.gateway_recover_off_s - 1
        svc._gateway_recover_tick()          # on
        svc._gateway_recover_tick()          # VBUS-on edge observed...
        svc._gw_vbus_on_at = time.monotonic() - 60.0   # ...a full stale window ago

    cycle()
    assert svc._gw_recover_attempts == 1
    # Base cooldown elapsed but backoff (2x) not: no new cycle.
    svc._gw_recover_last = time.monotonic() - cfg.gateway_recover_cooldown_s - 1
    svc._gateway_recover_tick()
    assert svc._gw_recover_attempts == 1
    # Doubled cooldown elapsed: second attempt fires.
    svc._gw_recover_last = time.monotonic() - 2 * cfg.gateway_recover_cooldown_s - 1
    cycle()
    assert svc._gw_recover_attempts == 2
    # A heartbeat newer than the last cycle resets the backoff.
    new_hb = time.time() - 60.0
    svc.twin.store.state.connection.last_heartbeat_time = new_hb
    svc._seen_marks["gateway_hb"] = (new_hb, time.monotonic() - 60.0)
    svc._gw_recover_last = time.monotonic() - 130.0
    svc._gateway_recover_tick()  # 60s stale again; attempts reset then increment
    assert svc._gw_recover_attempts == 1


def test_wall_clock_step_does_not_trip_recovery():
    """NTP stepping the wall clock forward (no RTC on the phone) must not make
    a heartbeat that arrived 1 s ago look hours old (2026-11-05 14:18 incident)."""
    svc, gw = make_service(hb_age_s=1.0)
    conn = svc.twin.store.state.connection
    conn.last_heartbeat_time += 91117.7      # the stamp itself jumps with the clock
    svc._gw_stale = False
    svc._gateway_recover_tick()
    assert gw.calls == []
