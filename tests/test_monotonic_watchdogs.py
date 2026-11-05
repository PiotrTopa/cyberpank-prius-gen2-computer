"""Link watchdogs must age on the monotonic clock.

2026-11-05 14:18 CET: the phone (no RTC) booted with a clock 25 h behind; the
NTP step forward made the backend log "Powerbox link STALE: no frame for
91117.7s", force-reset the powerbox USB and cold-cycle relay ch3 (gateway +
panda) although both links were streaming. The stamps the reducers keep in
the state stay wall clock (display); the service ages them on
``time.monotonic()`` from the moment each stamp was last seen to change.
"""

import time
import types

from cyberpunk_computer.backend.service import BackendService, BackendConfig


class FakeStore:
    def __init__(self, powerbox=None, connection=None):
        self.state = types.SimpleNamespace(
            powerbox=powerbox or types.SimpleNamespace(last_update_time=0.0, connected=True),
            connection=connection or types.SimpleNamespace(last_heartbeat_time=None, connected=True),
        )
        self.dispatched = []

    def dispatch(self, action):
        self.dispatched.append(action)


def make_service(monkeypatch, mono_start=5000.0):
    svc = BackendService.__new__(BackendService)
    svc.config = BackendConfig(powerbox_auto_recover=False)
    svc._pb_stale = False
    svc._gw_stale = False
    svc._pb_recover_attempts = 0
    svc._seen_marks = {}
    svc._start_time = mono_start
    svc._powerbox_serial = None
    store = FakeStore()
    svc.twin = types.SimpleNamespace(store=store)
    clock = {"mono": mono_start}
    monkeypatch.setattr(time, "monotonic", lambda: clock["mono"])
    return svc, store, clock


def test_powerbox_watchdog_ignores_wall_clock_step(monkeypatch):
    svc, store, clock = make_service(monkeypatch)
    pb = store.state.powerbox
    pb.last_update_time = 1_700_000_000.0          # a frame "now" (stale wall clock)
    svc._powerbox_watchdog_tick()
    assert not svc._pb_stale
    # NTP steps the clock by +91117 s; the next frame carries the new time.
    clock["mono"] += 1.0
    pb.last_update_time = 1_700_000_000.0 + 1.0 + 91117.7
    svc._powerbox_watchdog_tick()
    assert not svc._pb_stale and store.dispatched == []
    # Frames keep coming: still fine 20 s later.
    for i in range(20):
        clock["mono"] += 1.0
        pb.last_update_time += 1.0
        svc._powerbox_watchdog_tick()
    assert not svc._pb_stale


def test_powerbox_watchdog_still_detects_a_real_stall(monkeypatch):
    svc, store, clock = make_service(monkeypatch)
    pb = store.state.powerbox
    pb.last_update_time = 1_700_000_000.0
    svc._powerbox_watchdog_tick()
    clock["mono"] += svc.config.powerbox_stale_s + 0.5   # no new frame
    svc._powerbox_watchdog_tick()
    assert svc._pb_stale
    assert any(getattr(a, "connected", True) is False for a in store.dispatched)
    # A fresh frame clears it.
    clock["mono"] += 1.0
    pb.last_update_time += 20.0
    svc._powerbox_watchdog_tick()
    assert not svc._pb_stale


def test_powerbox_never_seen_ages_from_backend_start(monkeypatch):
    svc, store, clock = make_service(monkeypatch)
    svc._powerbox_watchdog_tick()
    assert not svc._pb_stale
    clock["mono"] += svc.config.powerbox_stale_s + 0.5
    svc._powerbox_watchdog_tick()
    assert svc._pb_stale


def test_gateway_watchdog_ignores_wall_clock_step(monkeypatch):
    svc, store, clock = make_service(monkeypatch)
    conn = store.state.connection
    svc._gateway_watchdog_tick()                   # never armed without a heartbeat
    conn.last_heartbeat_time = 1_700_000_000.0
    svc._gateway_watchdog_tick()
    clock["mono"] += 1.0
    conn.last_heartbeat_time += 1.0 + 91117.7      # stepped clock
    svc._gateway_watchdog_tick()
    assert not svc._gw_stale and store.dispatched == []
    clock["mono"] += svc.config.gateway_stale_s + 0.5   # real silence (key-off)
    svc._gateway_watchdog_tick()
    assert svc._gw_stale
    clock["mono"] += 1.0
    conn.last_heartbeat_time += 60.0
    svc._gateway_watchdog_tick()
    assert not svc._gw_stale


def test_powerbox_telemetry_fresh_uses_monotonic_age(monkeypatch):
    svc, store, clock = make_service(monkeypatch)
    pb = store.state.powerbox
    pb.last_update_time = 1_700_000_000.0
    assert svc._powerbox_telemetry_fresh()
    pb.last_update_time += 91117.7
    clock["mono"] += 1.0
    assert svc._powerbox_telemetry_fresh()
    clock["mono"] += svc.config.powerbox_stale_s + 1.0
    assert not svc._powerbox_telemetry_fresh()
