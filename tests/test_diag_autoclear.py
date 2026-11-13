"""Auto-clear rule and monitor (cyberpunk_computer.diag.autoclear)."""

import pytest

from cyberpunk_computer.diag.autoclear import (
    AutoClearConfig, AutoClearMonitor, decide, validate_config,
)
from cyberpunk_computer.diag.ecus import ECUS

from diag_harness import Rig
from test_diag_engine import ecu_of, prius_rig

T0 = 1_700_000_000.0


def cfg(**kw):
    base = dict(enabled=True, codes=[{"ecu": "engine", "code": "P0420", "enabled": True}])
    base.update(kw)
    return AutoClearConfig(**base)


# ── the rule ─────────────────────────────────────────────────────────────────

def test_fires_only_when_every_code_is_whitelisted():
    d = decide(cfg(), "engine", ["P0420"], [], T0, [])
    assert d.action == "clear" and d.codes == ["P0420"]


def test_mixed_codes_block():
    d = decide(cfg(), "engine", ["P0420", "P0171"], [], T0, [])
    assert d.action == "blocked" and d.blocked_by == ["P0171"]
    assert d.reason == "auto-clear blocked by P0171"
    # a pending stranger blocks too: the clear would erase it
    d = decide(cfg(), "engine", ["P0420"], ["P0300"], T0, [])
    assert d.action == "blocked" and d.blocked_by == ["P0300"]


def test_master_switch_and_entry_enable():
    assert decide(cfg(enabled=False), "engine", ["P0420"], [], T0, []).action == "none"
    off = cfg(codes=[{"ecu": "engine", "code": "P0420", "enabled": False}])
    assert decide(off, "engine", ["P0420"], [], T0, []).action == "none"
    assert decide(cfg(), "hv", ["P0420"], [], T0, []).action == "none"       # other ECU: not listed
    assert decide(cfg(), "engine", [], [], T0, []).action == "none"


def test_pending_only_needs_the_pending_trigger():
    assert decide(cfg(), "engine", [], ["P0420"], T0, []).action == "none"
    d = decide(cfg(trigger="pending"), "engine", [], ["P0420"], T0, [])
    assert d.action == "clear"


def test_rate_limits():
    c = cfg(min_interval_s=600, max_per_day=3)
    assert decide(c, "engine", ["P0420"], [], T0, [T0 - 599]).action == "wait"
    assert decide(c, "engine", ["P0420"], [], T0, [T0 - 601]).action == "clear"
    three = [T0 - 80000, T0 - 40000, T0 - 3000]
    d = decide(c, "engine", ["P0420"], [], T0, three)
    assert d.action == "wait" and "24 h" in d.reason
    assert decide(c, "engine", ["P0420"], [], T0, [T0 - 90000, T0 - 40000, T0 - 3000]).action == "clear"
    assert decide(cfg(max_per_day=0), "engine", ["P0420"], [], T0, []).action == "wait"


def test_never_during_a_scan():
    d = decide(cfg(), "engine", ["P0420"], [], T0, [], scanning=True)
    assert d.action == "wait" and "scan" in d.reason


# ── configuration ────────────────────────────────────────────────────────────

def test_validate_config():
    c = validate_config({"enabled": True, "codes": [{"ecu": "engine", "code": "p0420"},
                                                    {"ecu": "engine", "code": "P0420"}]}, ECUS)
    assert c.enabled and c.codes == [{"ecu": "engine", "code": "P0420", "enabled": True}]
    assert AutoClearConfig().enabled is False                         # default OFF
    for bad in ({"enabled": "yes"}, {"codes": "P0420"}, {"codes": [{"ecu": "nope", "code": "P0420"}]},
                {"codes": [{"ecu": "engine", "code": "P04"}]}, {"trigger": "always"},
                {"speed_policy": "fast"}, {"min_interval_s": 5}, {"max_per_day": 999},
                {"codes": [{"ecu": "engine", "code": "P0420", "enabled": 1}]}, {"poll_s": 1}):
        with pytest.raises(ValueError):
            validate_config(bad, ECUS)
    # partial update keeps the rest
    c2 = validate_config({"trigger": "pending"}, ECUS, c)
    assert c2.codes == c.codes and c2.trigger == "pending" and c2.enabled


# ── the monitor on the simulated car ─────────────────────────────────────────

def monitor_rig(config=None, **kw):
    rig = prius_rig(**kw)
    rig.logged = []
    rig.saved = []
    rig.mon = AutoClearMonitor(rig.eng, config=config or cfg(), save=rig.saved.append,
                               log=rig.logged.append, context=lambda: {"odometer_km": 245678},
                               clock=lambda: rig.t, wall=lambda: T0 + rig.t)
    return rig


def poll(rig, seconds=1.0):
    end = rig.t + seconds
    while rig.t < end:
        rig.mon.tick()
        rig.eng.tick()
        rig.gw.step()
        rig.t += 0.01
    rig.run()


def test_monitor_clears_a_whitelisted_code_and_logs_the_freeze_frame_first():
    rig = monitor_rig()
    poll(rig)
    ecm = ecu_of(rig, 0x7E0)
    assert ecm.clears == 1 and ecm.stored == []
    (rec,) = rig.logged
    assert rec["origin"] == "autoclear" and rec["ok"] and rec["whitelisted"] == ["P0420"]
    assert rec["before"]["freeze_frame"]["dtc"] == "P0420"
    assert rec["before"]["freeze_frame"]["values"]["long_fuel_trim"]["value"] == 7.8
    assert rec["before"]["monitor_status"]["mil"] is True
    assert rec["context"] == {"odometer_km": 245678} and rec["after"] == []
    # order on the wire: freeze frame (mode 02) before the clear (mode 04)
    sids = [r[1] for r in rig.requests(0x7E0)]
    assert sids.index(0x02) < sids.index(0x04)
    st = rig.mon.state()
    assert st["counters"]["clears"] == 1 and st["counters"]["today"] == 1
    assert st["last_action"]["codes"] == ["P0420"] and st["last_action"]["ok"]
    assert rig.eng.state()["autoclear"]["enabled"] is True
    # the HV ECU has no whitelist entry: never polled, never cleared
    assert rig.requests(0x7E2) == []


def test_monitor_is_off_by_default():
    rig = monitor_rig(config=AutoClearConfig())
    poll(rig, 2.0)
    assert rig.gw.sent == []


def test_monitor_blocked_by_another_code():
    rig = monitor_rig()
    ecu_of(rig, 0x7E0).stored = ["P0420", "P0171"]
    poll(rig)
    assert ecu_of(rig, 0x7E0).clears == 0 and rig.logged == []
    st = rig.mon.state()
    assert st["blocked"]["engine"]["by"] == ["P0171"]
    assert st["blocked"]["engine"]["reason"] == "auto-clear blocked by P0171"
    assert not any(r[1] == 0x04 for r in rig.requests())
    # the stranger goes away -> next poll clears
    ecu_of(rig, 0x7E0).stored = ["P0420"]
    ecu_of(rig, 0x7E0).toyota = ["P0420"]
    rig.t += 31
    poll(rig)
    assert ecu_of(rig, 0x7E0).clears == 1 and rig.mon.state()["blocked"] == {}


def test_monitor_blocked_by_a_toyota_only_code():
    rig = monitor_rig()
    ecu_of(rig, 0x7E0).toyota = ["P0420", "P1300"]      # not in mode 03, but the clear would erase it
    poll(rig)
    assert ecu_of(rig, 0x7E0).clears == 0
    assert rig.mon.state()["blocked"]["engine"]["by"] == ["P1300"]


def test_monitor_speed_gate_defers_without_logging():
    rig = monitor_rig()
    ecu_of(rig, 0x7E0).speed = 50
    poll(rig)
    assert ecu_of(rig, 0x7E0).clears == 0 and rig.logged == []
    st = rig.mon.state()
    assert st["counters"]["deferred"] == 1 and "50 km/h" in st["waiting"]["engine"]
    # stopped at the next poll: cleared (a deferral does not use up the rate limit)
    ecu_of(rig, 0x7E0).speed = 0
    rig.t += 31
    poll(rig)
    assert ecu_of(rig, 0x7E0).clears == 1 and len(rig.logged) == 1


def test_monitor_anytime_policy_clears_while_moving():
    rig = monitor_rig(config=cfg(speed_policy="anytime"))
    ecu_of(rig, 0x7E0).speed = 50
    poll(rig)
    assert ecu_of(rig, 0x7E0).clears == 1 and rig.logged[0]["speed"] == 50.0


def test_monitor_rate_limit_once_per_interval():
    rig = monitor_rig(config=cfg(min_interval_s=600, max_per_day=2))
    ecm = ecu_of(rig, 0x7E0)
    poll(rig)
    assert ecm.clears == 1
    for _ in range(5):                       # the code comes straight back
        ecm.stored = ["P0420"]; ecm.toyota = ["P0420"]
        rig.t += 31
        poll(rig)
    assert ecm.clears == 1 and "rate limit" in rig.mon.state()["waiting"]["engine"]
    rig.t += 600
    ecm.stored = ["P0420"]; ecm.toyota = ["P0420"]
    poll(rig)
    assert ecm.clears == 2
    rig.t += 700
    ecm.stored = ["P0420"]; ecm.toyota = ["P0420"]
    poll(rig)
    assert ecm.clears == 2                   # max_per_day reached
    assert "24 h" in rig.mon.state()["waiting"]["engine"]


def test_monitor_rate_limit_survives_a_restart():
    rig = prius_rig()
    past = [{"origin": "autoclear", "ok": True, "ecu": "engine", "ts": T0 - 100}]
    mon = AutoClearMonitor(rig.eng, config=cfg(), past_clears=past,
                           clock=lambda: rig.t, wall=lambda: T0 + rig.t)
    rig.mon, rig.logged = mon, []
    poll(rig)
    assert ecu_of(rig, 0x7E0).clears == 0 and "rate limit" in mon.state()["waiting"]["engine"]


def test_monitor_ecu_refusal_counts_and_backs_off():
    rig = monitor_rig()
    ecu_of(rig, 0x7E0).refuse_clear = True
    poll(rig)
    rig.t += 31
    poll(rig)
    st = rig.mon.state()
    assert st["counters"]["refused"] == 1 and st["counters"]["clears"] == 0
    assert sum(1 for r in rig.requests(0x7E0) if r[1] == 0x04) == 1       # not retried every poll
    assert rig.logged[0]["ok"] is False and "conditions not correct" in rig.logged[0]["result"]


def test_monitor_does_not_poll_during_a_scan_and_never_overlaps():
    rig = monitor_rig()
    rig.eng.start_scan()
    rig.mon.tick()
    assert not rig.mon._queued
    poll(rig, 0.5)
    assert rig.max_outstanding == 1


def test_monitor_idle_poll_is_one_request():
    rig = monitor_rig()
    ecm = ecu_of(rig, 0x7E0)
    ecm.stored, ecm.toyota = [], []
    poll(rig)                                # first poll reads the lists once (full check)
    n = len(rig.gw.sent)
    rig.t += 31
    poll(rig)
    assert len(rig.gw.sent) - n == 1         # steady state: only 01 01
    assert rig.requests(0x7E0)[-1][:3] == bytes([0x02, 0x01, 0x01])


def test_monitor_ignition_off_is_quiet():
    rig = monitor_rig()
    ecu_of(rig, 0x7E0).alive = False
    poll(rig)
    assert len(rig.gw.sent) == 1 and rig.logged == [] and rig.mon.state()["blocked"] == {}


def test_set_config_persists_and_validates():
    rig = monitor_rig(config=AutoClearConfig())
    ok, _ = rig.mon.set_config({"enabled": True, "codes": [{"ecu": "engine", "code": "P0420"}]})
    assert ok and rig.saved[-1]["enabled"] is True and rig.saved[-1]["codes"][0]["code"] == "P0420"
    ok, msg = rig.mon.set_config({"codes": [{"ecu": "engine", "code": "nope"}]})
    assert not ok and "not a DTC" in msg and len(rig.saved) == 1
    poll(rig)
    assert ecu_of(rig, 0x7E0).clears == 1


def test_monitor_does_not_poll_a_sleeping_bus():
    rig = prius_rig()
    awake = [False]
    rig.logged = []
    rig.mon = AutoClearMonitor(rig.eng, config=cfg(), active=lambda: awake[0],
                               clock=lambda: rig.t, wall=lambda: T0 + rig.t)
    poll(rig, 3.0)
    assert rig.gw.sent == []
    awake[0] = True
    rig.t += 6
    poll(rig)
    assert ecu_of(rig, 0x7E0).clears == 1
