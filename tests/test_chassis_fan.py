"""Chassis fan policy (backend/fan_policy.py), its service adapter, and closed-loop
runs on the plant fitted in docs/THERMAL_MODEL.md."""
from types import SimpleNamespace

import pytest

from cyberpunk_computer.backend.fan_policy import (
    ChassisFanPolicy,
    FanPolicyConfig,
    ThermalPlant,
    interp_table,
)

CFG = FanPolicyConfig()

# Measured plant (docs/THERMAL_MODEL.md section 4): GPU zone node.
R_INT = 5.17
R_SINK = {0: 4.82, 10: 4.7, 20: 3.93, 40: 3.02, 70: 2.62, 100: 2.35}
C_DIE = 0.44
C_SINK = 20.0
P_ROADSENSE = 6.6
P_LOW_IDLE = 0.62


def run(policy, seconds, *, cpu=None, gpu=None, amb=25.0, box_in=None, box_out=None,
        mode="full", override=None, dt=2.0, t0=0.0):
    d = None
    t = t0
    while t < t0 + seconds:
        d = policy.step(t, cpu_c=cpu, gpu_c=gpu, ambient_c=amb, box_in_c=box_in,
                        box_out_c=box_out, power_mode=mode, override_pct=override)
        t += dt
    return d


def settle(policy, **kw):
    return run(policy, 40 * CFG.tau_s + 400, **kw)


def test_gpu_leads_the_control_temperature():
    p = ChassisFanPolicy(CFG)
    assert run(p, 10, cpu=90.0, gpu=78.0).control_c == pytest.approx(78.0)
    p = ChassisFanPolicy(CFG)
    assert run(p, 10, cpu=95.0, gpu=50.0).control_c == pytest.approx(95.0 - CFG.cpu_offset_c)
    p = ChassisFanPolicy(CFG)
    assert run(p, 10, cpu=None, gpu=60.0).control_c == pytest.approx(60.0)
    assert ChassisFanPolicy(CFG).step(0.0, cpu_c=None, gpu_c=None, ambient_c=25.0, box_in_c=None,
                                      box_out_c=None, power_mode="full").control_c is None


def test_brief_cpu_spikes_alone_do_not_run_the_fan():
    """A single 2 s sample at 96 °C between cool readings stays under the
    hot-core on-point after its 5 s low-pass: no reaction."""
    p = ChassisFanPolicy(CFG)
    t, duties = 0.0, []
    for i in range(600):
        cpu = 96.0 if i % 20 == 19 else 76.0
        duties.append(p.step(t, cpu_c=cpu, gpu_c=70.0, ambient_c=25.0, box_in_c=None,
                             box_out_c=None, power_mode="full").duty_pct)
        t += 2.0
    assert max(duties) == 0.0 and not p.safety and not p.hot


# ── hot-core term (2026-11-17: hottest core 89-96 °C on the road at 28-50 %) ──

def test_sustained_hot_core_runs_the_fan_to_full_within_seconds():
    """The road case: GPU-led duty modest, hottest core cycling 89..96 °C."""
    p = ChassisFanPolicy(CFG)
    settle(p, cpu=80.0, gpu=80.0)          # GPU-led, below full
    base = p.duty_pct
    assert 0.0 < base < 60.0 and not p.hot
    t, duties = 1e5, []
    for i in range(30):                    # 1 s ticks, hottest core 96 °C
        duties.append(p.step(t + i, cpu_c=96.0, gpu_c=80.0, ambient_c=25.0, box_in_c=None,
                             box_out_c=None, power_mode="full"))
    assert duties[-1].duty_pct == 100.0 and duties[-1].reason == "hot_core"
    # 100 % within about 10 s of the core going hot (5 s filter + fast slew)
    first_full = next(i for i, d in enumerate(duties) if d.duty_pct == 100.0)
    assert first_full <= 10


def test_hot_core_ramp_floor_and_hysteresis():
    p = ChassisFanPolicy(CFG)
    # cool GPU: normal target 0; hot core at the ramp middle
    mid = (CFG.hot_on_c + CFG.hot_full_c) / 2
    d = settle(p, cpu=mid, gpu=40.0)
    assert p.hot and d.duty_pct == pytest.approx(50.0, abs=1.0)
    assert d.control_c < CFG.full.on_c      # the GPU-led part did not engage
    # just above on: the floor
    d = settle(p, cpu=CFG.hot_on_c + 0.2, gpu=40.0, t0=1e5)
    assert p.hot and d.duty_pct >= CFG.hot_floor_pct
    # between release and on: held (hysteresis)
    d = settle(p, cpu=(CFG.hot_on_c + CFG.hot_release_c) / 2, gpu=40.0, t0=2e5)
    assert p.hot and d.duty_pct >= CFG.hot_floor_pct
    # below release: back to the quiet GPU-led behaviour (fan off here)
    d = settle(p, cpu=CFG.hot_release_c - 1.0, gpu=40.0, t0=3e5)
    assert not p.hot and d.duty_pct == 0.0


def test_hot_core_builds_on_the_normal_target():
    p = ChassisFanPolicy(CFG)
    gpu = (CFG.full.on_c + CFG.full.full_c) / 2
    normal = settle(p, cpu=70.0, gpu=gpu).duty_pct
    q = ChassisFanPolicy(CFG)
    d = settle(q, cpu=CFG.hot_on_c + 0.25 * (CFG.hot_full_c - CFG.hot_on_c), gpu=gpu)
    assert d.duty_pct == pytest.approx(normal + 0.25 * (100.0 - normal), abs=CFG.deadband_pct)


def test_hot_core_exceeds_the_low_ceiling():
    p = ChassisFanPolicy(CFG)
    d = settle(p, cpu=CFG.hot_full_c + 1.0, gpu=60.0, mode="low")
    assert d.duty_pct == 100.0 and d.reason == "hot_core" and not p.safety
    d = settle(p, cpu=70.0, gpu=60.0, mode="low", t0=1e5)
    assert not p.hot and d.duty_pct <= CFG.low.max_pct


def test_hot_core_thresholds_are_config():
    from dataclasses import replace
    cfg = replace(CFG, hot_on_c=70.0, hot_full_c=75.0, hot_release_c=66.0)
    p = ChassisFanPolicy(cfg)
    assert settle(p, cpu=76.0, gpu=40.0).duty_pct == 100.0
    p = ChassisFanPolicy(CFG)
    assert settle(p, cpu=76.0, gpu=40.0).duty_pct == 0.0


def test_filter_time_constant():
    p = ChassisFanPolicy(CFG)
    p.step(0.0, cpu_c=None, gpu_c=30.0, ambient_c=25.0, box_in_c=None, box_out_c=None, power_mode="full")
    d = run(p, CFG.tau_s, gpu=70.0, t0=2.0)
    assert d.control_c == pytest.approx(30.0 + 40.0 * 0.63, abs=2.5)


def test_full_profile_engages_ramps_releases():
    prof = CFG.full
    p = ChassisFanPolicy(CFG)
    assert settle(p, gpu=prof.on_c - 1.0).duty_pct == 0.0
    d = settle(p, gpu=prof.on_c + 0.5, t0=1e5)
    assert p.active and prof.min_pct <= d.duty_pct <= prof.min_pct + 15
    d = settle(p, gpu=(prof.on_c + prof.full_c) / 2, t0=2e5)
    assert prof.min_pct < d.duty_pct < prof.max_pct
    assert settle(p, gpu=prof.full_c + 1.0, t0=3e5).duty_pct == prof.max_pct
    d = settle(p, gpu=(prof.on_c + prof.off_c) / 2, t0=4e5)
    assert p.active and d.duty_pct >= prof.min_pct
    d = settle(p, gpu=prof.off_c - 1.0, t0=5e5)
    assert not p.active and d.duty_pct == 0.0


def test_output_shaping_rises_fast_falls_slowly_and_holds_in_the_deadband():
    p = ChassisFanPolicy(CFG)
    # small target wiggle inside the deadband: duty does not move
    p = ChassisFanPolicy(CFG)
    base = settle(p, gpu=80.0).duty_pct
    d = run(p, 10, gpu=80.1, t0=1e5)
    assert d.duty_pct == base
    # bounded fall per tick
    p = ChassisFanPolicy(CFG)
    settle(p, gpu=CFG.full.full_c)
    prev = p.duty_pct
    for i in range(1, 20):
        d = p.step(1000.0 + 2 * i, cpu_c=None, gpu_c=CFG.full.on_c + 0.1, ambient_c=25.0,
                   box_in_c=None, box_out_c=None, power_mode="full")
        assert prev - d.duty_pct <= CFG.slew_down_pct_s * 2.0 + 1e-6
        prev = d.duty_pct


def test_delta_gate_and_missing_ambient():
    prof = CFG.low
    p = ChassisFanPolicy(CFG)
    ctrl = prof.on_c + 2.0
    assert settle(p, gpu=ctrl, amb=ctrl - prof.min_delta_c + 1.0, mode="low").duty_pct == 0.0
    assert settle(p, gpu=ctrl, amb=20.0, mode="low", t0=1e5).duty_pct > 0.0
    p = ChassisFanPolicy(CFG)
    assert settle(p, gpu=ctrl, amb=None, mode="low").duty_pct > 0.0


def test_safety_on_filtered_control_and_on_raw_extreme():
    p = ChassisFanPolicy(CFG)
    d = settle(p, gpu=CFG.safety_ctrl_c + 0.5)
    assert d.duty_pct == 100.0 and d.reason == "safety"
    d = settle(p, gpu=CFG.safety_release_c + 1.0, t0=1e5)
    assert p.safety and d.duty_pct == 100.0
    d = settle(p, gpu=CFG.safety_release_c - 1.0, t0=2e5)
    assert not p.safety and d.reason != "safety"
    p = ChassisFanPolicy(CFG)
    run(p, 60, gpu=40.0, cpu=50.0)
    d = p.step(1000.0, cpu_c=CFG.safety_raw_c + 0.1, gpu_c=40.0, ambient_c=25.0, box_in_c=None,
               box_out_c=None, power_mode="low")
    assert d.duty_pct == 100.0 and d.reason == "safety"


def test_override_bypasses_everything_and_is_clamped():
    p = ChassisFanPolicy(CFG)
    d = run(p, 120, cpu=99.0, gpu=95.0, override=0.0)
    assert d.duty_pct == 0.0 and d.reason == "override"
    assert d.control_c is not None
    assert p.step(200.0, cpu_c=30.0, gpu_c=None, ambient_c=25.0, box_in_c=None, box_out_c=None,
                  power_mode="full", override_pct=137.0).duty_pct == 100.0


def test_low_profile_quiet_with_wide_hysteresis():
    low = CFG.low
    p = ChassisFanPolicy(CFG)
    assert settle(p, gpu=low.on_c - 2.0, mode="low").duty_pct == 0.0
    d = settle(p, gpu=CFG.safety_release_c - 1.0, mode="low", t0=1e5)
    assert p.active and 0.0 < d.duty_pct <= low.max_pct
    assert settle(p, gpu=low.off_c + 1.0, mode="low", t0=2e5).duty_pct > 0.0
    assert settle(p, gpu=low.off_c - 1.0, mode="low", t0=3e5).duty_pct == 0.0
    assert low.on_c - low.off_c >= 10.0 and low.max_pct <= 50.0


def test_key_off_slews_down_to_the_low_ceiling():
    p = ChassisFanPolicy(CFG)
    settle(p, gpu=CFG.full.full_c)
    assert p.duty_pct == 100.0
    d = settle(p, gpu=75.0, mode="low", t0=1e5)
    assert d.duty_pct <= CFG.low.max_pct


def test_box_purge_independent_of_the_poco():
    p = ChassisFanPolicy(CFG)
    d = run(p, 60, gpu=35.0, box_in=CFG.box_min_temp + 5.0, box_out=25.0)
    assert p.box_active and d.reason == "box" and 0 < d.duty_pct <= CFG.box_max_pct
    d = run(p, 60, gpu=35.0, box_in=25.0 + CFG.box_stop_delta - 1.0, box_out=25.0, t0=100)
    assert not p.box_active and d.duty_pct == 0.0
    assert run(p, 10, gpu=35.0, box_in=None, box_out=25.0, t0=200).box_pct == 0.0
    # stall floor: (8-5)/10*60 = 18 %; (5.5-5)/... stays latched above the floor
    p = ChassisFanPolicy(CFG)
    run(p, 10, gpu=35.0, box_in=36.0, box_out=28.0)
    d = run(p, 10, gpu=35.0, box_in=33.3, box_out=28.0, t0=20)
    assert d.duty_pct >= CFG.stall_pct


def test_missing_readings_stand_down():
    p = ChassisFanPolicy(CFG)
    settle(p, gpu=80.0)
    assert p.active
    d = p.step(1e5, cpu_c=None, gpu_c=None, ambient_c=25.0, box_in_c=None, box_out_c=None, power_mode="full")
    assert d.duty_pct == 0.0 and d.control_c is None and not p.active


# ── closed loop on the measured plant ───────────────────────────────────────

def plant(amb, **kw):
    return ThermalPlant(t_amb=amb, c_die=C_DIE, r_int=R_INT, c_sink=C_SINK,
                        r_sink=interp_table(R_SINK), **kw)


def closed_loop(pol, pl, p_w, mode, seconds, dt=2.0):
    duties, die, t = [], [], 0.0
    while t < seconds:
        d = pol.step(t, cpu_c=None, gpu_c=pl.t_die, ambient_c=pl.t_amb, box_in_c=None,
                     box_out_c=None, power_mode=mode)
        pl.step(p_w, d.duty_pct, dt)
        duties.append(d.duty_pct)
        die.append(pl.t_die)
        t += dt
    return die, duties


def test_plant_steady_state():
    pl = plant(25.0)
    for _ in range(4000):
        pl.step(2.0, 100.0, 1.0)
    assert pl.t_die == pytest.approx(25.0 + 2.0 * (R_INT + R_SINK[100]), abs=0.2)
    assert interp_table(R_SINK)(55) == pytest.approx((R_SINK[40] + R_SINK[70]) / 2)


@pytest.mark.parametrize("amb,max_duty", [(25.0, 55.0), (30.0, 80.0)])
def test_full_holds_the_gpu_below_its_trip_at_moderate_duty(amb, max_duty):
    die, duties = closed_loop(ChassisFanPolicy(CFG), plant(amb), P_ROADSENSE, "full", 3600)
    tail = len(die) // 2
    assert max(die[tail:]) < 85.0
    assert max(duties[tail:]) <= max_duty
    assert max(duties[tail:]) - min(duties[tail:]) <= 2 * CFG.deadband_pct


def test_full_reaches_full_speed_when_the_cabin_is_hot():
    die, duties = closed_loop(ChassisFanPolicy(CFG), plant(38.0), P_ROADSENSE, "full", 3600)
    assert duties[-1] == 100.0


def test_low_idle_stays_passive_even_in_a_hot_parked_car():
    for amb in (-10.0, 25.0, 45.0):
        die, duties = closed_loop(ChassisFanPolicy(CFG), plant(amb), P_LOW_IDLE, "low", 7200)
        assert max(duties) == 0.0


def test_after_a_drive_low_does_not_cycle():
    pl = plant(25.0, t_die=80.0, t_sink=45.0)
    _, duties = closed_loop(ChassisFanPolicy(CFG), pl, P_LOW_IDLE, "low", 3600)
    edges = sum(1 for a, b in zip(duties, duties[1:]) if (a > 0) != (b > 0))
    assert edges <= 2


# ── service adapter ─────────────────────────────────────────────────────────

class _Store:
    def __init__(self, **pb):
        base = dict(connected=True, poco_core_temp=None, poco_gpu_temp=None, bmp_t=None,
                    bmp2_t=None, aht_t=None, power_mode="full", fan_override_pct=None)
        base.update(pb)
        self.state = SimpleNamespace(powerbox=SimpleNamespace(**base))
        self.dispatched = []

    def dispatch(self, action):
        self.dispatched.append(action)


class _Commander:
    def __init__(self):
        self.sent = []

    def set_fan(self, pct):
        self.sent.append(pct)
        return True


def _service(**pb):
    from cyberpunk_computer.backend.service import BackendConfig, BackendService
    svc = BackendService(BackendConfig(chassis_fan_tick_s=0.0))
    store = _Store(**pb)
    svc.twin = SimpleNamespace(store=store)
    svc.powerbox_commander = _Commander()
    return svc, store, svc.powerbox_commander


def test_service_tick_sends_policy_duty_and_publishes():
    svc, store, cmd = _service(poco_core_temp=99.0, poco_gpu_temp=70.0, bmp2_t=25.0)
    svc._chassis_fan_tick()
    assert cmd.sent == [100.0]
    assert store.dispatched[-1].fan_duty_pct == 100.0
    assert store.dispatched[-1].poco_ema_temp == pytest.approx(84.0)


def test_service_tick_override_bypasses_even_when_disconnected():
    svc, _, cmd = _service(connected=False, poco_core_temp=50.0, fan_override_pct=33.0)
    svc._chassis_fan_tick()
    assert cmd.sent == [33.0]
    svc2, _, cmd2 = _service(connected=False, poco_core_temp=50.0)
    svc2._chassis_fan_tick()
    assert cmd2.sent == []


def test_service_tick_dedups_unchanged_duty():
    svc, _, cmd = _service(poco_core_temp=40.0, poco_gpu_temp=38.0, bmp2_t=25.0)
    for _ in range(5):
        svc._chassis_fan_tick()
    assert cmd.sent == [0.0]
