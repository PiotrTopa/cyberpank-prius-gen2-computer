"""Tests for BackendService._gateway_recover_tick — gateway VBUS auto-recovery.

A wedged gateway MCU (enumerated but no GW_HB heartbeats) is only fixed by a
cold VBUS cycle on relay ch4. The tick must:
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
        self.relay_ch = 4
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
    svc._gw_recover_epoch = time.time() - hb_age_s
    svc._gw_recover_attempts = 0
    gw = FakeGateway(powered=powered, desired=desired)
    svc.port_power = types.SimpleNamespace(gateway=gw)
    last_hb = (time.time() - hb_age_s) if hb_seen else None
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


def test_repowers_after_off_window():
    svc, gw = make_service()
    svc._gateway_recover_tick()
    # Still inside the off window: nothing more happens.
    svc._gateway_recover_tick()
    assert gw.calls == [False]
    # Force the off window to elapse.
    svc._gw_recover_off_at = time.time() - svc.config.gateway_recover_off_s - 1
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
    svc._gw_recover_last = time.time()  # a cycle just happened
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
        svc._gw_recover_off_at = time.time() - cfg.gateway_recover_off_s - 1
        svc._gateway_recover_tick()          # on

    cycle()
    assert svc._gw_recover_attempts == 1
    # Base cooldown elapsed but backoff (2x) not: no new cycle.
    svc._gw_recover_last = time.time() - cfg.gateway_recover_cooldown_s - 1
    svc._gateway_recover_tick()
    assert svc._gw_recover_attempts == 1
    # Doubled cooldown elapsed: second attempt fires.
    svc._gw_recover_last = time.time() - 2 * cfg.gateway_recover_cooldown_s - 1
    cycle()
    assert svc._gw_recover_attempts == 2
    # A heartbeat newer than the last cycle resets the backoff.
    svc.twin.store.state.connection.last_heartbeat_time = time.time() - 60.0
    svc._gw_recover_last = time.time() - 130.0
    svc._gateway_recover_tick()  # 60s stale again; attempts reset then increment
    assert svc._gw_recover_attempts == 1
