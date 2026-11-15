"""The alert functions on realistic input sequences (manager in shadow, the
log is what the owner reviews). Map-driven ones also run on the real mapd in
test_alerts_mapd_e2e.py; the replay tool on the recorded drives below."""

from pathlib import Path

import pytest

from cyberpunk_computer.alerts.functions import default_functions
from cyberpunk_computer.alerts.functions.curve import CurveFunction
from cyberpunk_computer.alerts.functions.eco import EcoCoachFunction
from cyberpunk_computer.alerts.functions.guardian import GuardianFunction
from cyberpunk_computer.alerts.functions.headway import HeadwayFunction
from cyberpunk_computer.alerts.functions.lane import LaneDepartureFunction
from cyberpunk_computer.alerts.functions.maintenance import MaintenanceFunction
from cyberpunk_computer.alerts.functions.speed_limit import SpeedLimitFunction
from cyberpunk_computer.alerts.functions.stop_go import StopGoFunction
from cyberpunk_computer.alerts.functions.tunnel import TunnelRecircFunction
from cyberpunk_computer.alerts.functions.winter import WinterFunction
from cyberpunk_computer.alerts.inputs import AlertInputs, Lead, Limit
from cyberpunk_computer.alerts.manager import AlertManager
from cyberpunk_computer.alerts.persist import AlertSettings
from cyberpunk_computer.alerts.tuning import AlertTuning

FIX = Path(__file__).parent / "fixtures"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Rig:
    def __init__(self, cls, tuning=None, settings=None, modes=None):
        self.clk = Clock()
        self.tuning = tuning or AlertTuning()
        self.settings = settings or AlertSettings(None)
        self.fn = cls(self.tuning, self.settings)
        self.acts = []
        self.m = AlertManager([self.fn], self.settings, clock=self.clk, wall=self.clk,
                              actuator=lambda a, b, c: self.acts.append(b) or True)
        for aid, mode in (modes or {}).items():
            self.settings.set_mode(aid, mode)
        self.base = dict(real_kmh=50.0, gear="DRIVE", ready=True, scene_age=0.1)

    def run(self, secs, step=0.1, **kw):
        end = self.clk.t + secs
        while self.clk.t < end - 1e-9:
            d = dict(self.base)
            d.update(kw)
            vals = {k: (v(self.clk.t) if callable(v) else v) for k, v in d.items()}
            self.m.tick(AlertInputs(now=self.clk.t, wall=self.clk.t, **vals))
            self.clk.t += step

    def fires(self, aid=None):
        return [e for e in reversed(self.m.log.recent_entries(500)) if e.get("event") == "fire"
                and (aid is None or e["id"] == aid)]

    def entries(self, event):
        return [e for e in reversed(self.m.log.recent_entries(500)) if e.get("event") == event]


def test_every_type_ships_in_shadow_except_lane_departure_off():
    m = AlertManager(default_functions(AlertTuning(), AlertSettings(None)))
    modes = {a: m.mode(a) for a in m.specs}
    assert modes.pop("lane_departure") == "off"
    assert set(modes.values()) == {"shadow"} and len(modes) == 18


# ── 1. speed limit ───────────────────────────────────────────────────────────

SIGN50 = Limit(50, "sign", 0.8, "agree", False, True)


def test_speed_limit_uses_true_speed_and_delay():
    r = Rig(SpeedLimitFunction)
    r.run(10.0, limit=SIGN50, real_kmh=50.0)            # pass the change zone (150 m)
    # speedometer says 62 (+12 %) but true 55.5: within tolerance, nothing
    r.run(10.0, limit=SIGN50, real_kmh=55.5, display_kmh=62.0)
    assert r.fires() == []
    r.run(4.5, limit=SIGN50, real_kmh=57.0)
    assert r.fires() == []                               # < 5 s
    r.run(1.0, limit=SIGN50, real_kmh=57.0)
    f = r.fires()
    assert len(f) == 1 and f[0]["text"] == "LIMIT 50  REAL 57" and f[0]["evidence"]["bar"] == "strong"
    r.run(30.0, limit=SIGN50, real_kmh=58.0)
    r.run(2.0, limit=SIGN50, real_kmh=52.0)              # above the limit: still latched
    r.run(30.0, limit=SIGN50, real_kmh=58.0)
    assert len(r.fires()) == 1
    r.run(4.0, limit=SIGN50, real_kmh=48.0)              # re-armed after 3 s at/below the limit
    r.run(6.0, limit=SIGN50, real_kmh=58.0)
    assert len(r.fires()) == 2


def test_speed_limit_quiet_after_change_and_on_weak_sources():
    r = Rig(SpeedLimitFunction)
    r.run(5.0, limit=SIGN50, real_kmh=80.0)              # 110 m since the change: suppressed
    assert r.fires() == []
    weak = Limit(50, "vision_default", 0.7, None, False, False)
    r2 = Rig(SpeedLimitFunction)
    r2.run(12.0, limit=weak, real_kmh=60.0)              # +10: below the weak bar (+15)
    assert r2.fires() == []
    r2.run(11.0, limit=weak, real_kmh=70.0)
    assert len(r2.fires()) == 1 and r2.fires()[0]["evidence"]["bar"] == "weak"
    t = AlertTuning()
    t.speed_limit.weak_policy = "off"
    r3 = Rig(SpeedLimitFunction, tuning=t)
    r3.run(30.0, limit=Limit(50, "map_implicit", 0.9, "map_only", False, True), real_kmh=90.0)
    assert r3.fires() == []
    r4 = Rig(SpeedLimitFunction)
    r4.run(30.0, limit=Limit(50, "sign", 0.9, "conflict", False, True), real_kmh=90.0)
    r4.run(30.0, limit=Limit(50, "unknown", 0.9, None, False, True), real_kmh=90.0)
    assert r4.fires() == []


def test_speed_limit_tolerance_above_100_and_chip():
    r = Rig(SpeedLimitFunction, modes={"speed_limit": "on"})
    lim = Limit(120, "map", 0.9, "map_only", False, True)
    r.run(10.0, limit=lim, real_kmh=118.0)
    r.run(6.0, limit=lim, real_kmh=130.0)                # +10 < 12 (10 %)
    assert r.fires() == []
    r.run(6.0, limit=lim, real_kmh=133.0)
    assert len(r.fires()) == 1
    cur = r.m.current()
    assert cur["id"] == "speed_limit" and cur["chip"] == {"real_kmh": 133}


# ── 2. stop and go ───────────────────────────────────────────────────────────

def _lead_track(t, start, d0=6.0):
    if t < start:
        return Lead(d=d0, v_rel=0.0, prob=0.9, src="vision")
    return Lead(d=d0 + 2.0 * (t - start), v_rel=2.0, prob=0.9, src="vision")


def test_lead_departure_vision_fallback_once_per_stop():
    r = Rig(StopGoFunction)
    r.run(5.0, real_kmh=0.0, lead=lambda t: _lead_track(t, 1e9))
    assert r.fires() == []
    start = r.clk.t
    r.run(4.0, real_kmh=0.0, lead=lambda t: _lead_track(t, start))
    f = r.fires("lead_departure")
    assert len(f) == 1 and f[0]["evidence"]["src"] == "vision" and f[0]["evidence"]["d_at_stop"] == 6.0
    r.run(5.0, real_kmh=0.0, lead=lambda t: _lead_track(t, start))
    assert len(r.fires("lead_departure")) == 1


def test_lead_departure_needs_a_close_standing_lead_and_ignores_vanishing():
    r = Rig(StopGoFunction)
    r.run(5.0, real_kmh=0.0, lead=Lead(d=25.0, v_rel=0.0, prob=0.9))        # far
    r.run(5.0, real_kmh=0.0, lead=Lead(d=35.0, v_rel=2.0, prob=0.9))
    assert r.fires() == []
    r2 = Rig(StopGoFunction)
    r2.run(5.0, real_kmh=0.0, lead=Lead(d=6.0, v_rel=0.0, prob=0.9))
    r2.run(5.0, real_kmh=0.0, lead=None)                                  # occluded / lost
    assert r2.fires() == []
    r3 = Rig(StopGoFunction)                                              # moving: never
    r3.run(10.0, real_kmh=20.0, lead=lambda t: _lead_track(t, 1003.0))
    assert r3.fires() == []


def test_lead_departure_uses_the_tracked_cue_when_present():
    r = Rig(StopGoFunction)
    stand = Lead(d=5.5, src="tracked", standing=True, departed=False)
    r.run(4.0, real_kmh=0.0, lead=stand, lead_tracked_available=True)
    r.run(0.5, real_kmh=0.0, lead=Lead(d=7.0, src="tracked", standing=False, departed=True),
          lead_tracked_available=True)
    f = r.fires("lead_departure")
    assert len(f) == 1 and f[0]["evidence"]["src"] == "tracked"


def test_departed_cue_without_a_lead_fires_departure_and_never_headway_or_blocks_green():
    # roadsense's tracker lost the lead after it pulled away: scene lead null + lead_departed
    # -> Lead(d=0, departed) (alerts.inputs._scene_lead)
    gone = Lead(d=0.0, src="tracked", departed=True, standing=False)
    r = Rig(StopGoFunction)
    r.run(4.0, real_kmh=0.0, lead=Lead(d=4.0, src="tracked", standing=True), lead_tracked_available=True)
    r.run(0.5, real_kmh=0.0, lead=gone, lead_tracked_available=True)
    assert len(r.fires("lead_departure")) == 1
    r2 = Rig(StopGoFunction)
    r2.run(3.0, real_kmh=0.0, lead=gone, lead_tracked_available=True,
           light={"state": "red", "d": 20.0, "rel": 0.7, "agree": 2})
    r2.run(3.0, real_kmh=0.0, lead=gone, lead_tracked_available=True,
           light={"state": "green", "d": 20.0, "rel": 0.7, "agree": 2})
    assert len(r2.fires("green_light")) == 1
    r3 = Rig(HeadwayFunction)
    r3.run(10.0, real_kmh=70.0, lead=gone, lead_tracked_available=True)
    assert r3.fires() == []


def test_green_light_cannot_be_switched_on_but_logs_in_shadow():
    r = Rig(StopGoFunction)
    ok, why = r.m.set_mode("green_light", "on")
    assert not ok and "unvalidated" in why
    r.run(3.0, real_kmh=0.0, light={"state": "red", "d": 20.0, "rel": 0.7, "agree": 2})
    r.run(3.0, real_kmh=0.0, light={"state": "green", "d": 20.0, "rel": 0.7, "agree": 2})
    f = r.fires("green_light")
    assert len(f) == 1 and f[0]["evidence"]["rel"] == 0.7


# ── 3. headway / distance ────────────────────────────────────────────────────

def test_headway_coach_sustained_short_gap_only_above_50():
    r = Rig(HeadwayFunction)
    # 70 km/h = 19.4 m/s; d 18 m -> gap (18-1.8)/19.4 = 0.83 s
    r.run(4.0, real_kmh=70.0, lead=Lead(d=18.0, v_rel=0.0, prob=0.9))
    assert r.fires() == []
    r.run(2.0, real_kmh=70.0, lead=Lead(d=18.0, v_rel=0.0, prob=0.9))
    f = r.fires("headway_coach")
    assert len(f) == 1 and f[0]["text"] == "GAP 0.8s"
    r2 = Rig(HeadwayFunction)
    r2.run(20.0, real_kmh=40.0, lead=Lead(d=8.0, v_rel=0.0, prob=0.9))   # city traffic
    r2.run(20.0, real_kmh=70.0, lead=Lead(d=18.0, v_rel=0.0, prob=0.5))  # weak lead
    r2.run(20.0, real_kmh=70.0, lead=Lead(d=18.0, v_rel=0.0, prob=0.9), steering_deg=40.0)  # curve
    assert r2.fires() == []


def test_distance_warning_ttc_gates():
    closing = Lead(d=20.0, y=0.2, v_rel=-10.0, prob=0.9)              # 36 km/h closing, ttc 1.8 s
    r = Rig(HeadwayFunction, modes={"distance_warning": "on"})
    r.run(0.5, real_kmh=80.0, lead=closing)
    f = r.fires("distance_warning")
    assert len(f) == 1 and f[0]["text"] == "CLOSING 1.8s" and r.m.current()["advisory"] is True
    for kw in (dict(brake=True), dict(steering_deg=35.0), dict(real_kmh=25.0),
               dict(lead=Lead(d=20.0, y=1.6, v_rel=-10.0, prob=0.9)),
               dict(lead=Lead(d=20.0, v_rel=-10.0, prob=0.6)),
               dict(lead=Lead(d=20.0, v_rel=-3.0, prob=0.9)),
               dict(scene_age=1.5)):
        rr = Rig(HeadwayFunction)
        args = dict(real_kmh=80.0, lead=closing)
        args.update(kw)
        rr.run(2.0, **args)
        assert rr.fires("distance_warning") == [], kw


# ── 4. curve ─────────────────────────────────────────────────────────────────

def _map_curve(m0, r, speed_kmh, junction=None):
    def ahead(t):
        m = m0 - speed_kmh / 3.6 * (t - 1000.0)
        a = {"curve": {"m": round(m), "r": r, "dir": "l"}}
        if junction is not None:
            a["junction"] = {"m": round(m + junction), "kind": "junction"}
        return a
    return ahead


def test_curve_notice_once_per_curve_and_inactive_without_map():
    r = Rig(CurveFunction)
    r.run(12.0, real_kmh=90.0, map_ok=True, map_conf=0.95, ahead=_map_curve(300, 120, 90.0))
    f = r.fires("curve_speed")
    # sqrt(2.5*120) = 17.3 m/s = 62 km/h -> "MAX 60"; fired 3..9 s before the curve
    assert len(f) == 1 and f[0]["text"].endswith("MAX 60") and 3.0 <= f[0]["evidence"]["t_s"] <= 9.0
    for kw in (dict(map_ok=False), dict(map_conf=0.8), dict(built_up=True)):
        rr = Rig(CurveFunction)
        args = dict(real_kmh=90.0, map_ok=True, map_conf=0.95, ahead=_map_curve(300, 120, 90.0))
        args.update(kw)
        rr.run(12.0, **args)
        assert rr.fires() == [], kw
    rj = Rig(CurveFunction)
    rj.run(12.0, real_kmh=90.0, map_ok=True, map_conf=0.95, ahead=_map_curve(300, 120, 90.0, junction=10))
    assert rj.fires() == []
    rs = Rig(CurveFunction)
    rs.run(12.0, real_kmh=60.0, map_ok=True, map_conf=0.95, ahead=_map_curve(300, 120, 60.0))
    assert rs.fires() == []                                           # already slow enough


# ── 5. tunnel recirculation ──────────────────────────────────────────────────

FRESH = {"system_on": True, "recirculation": False, "fresh_air": True, "ac_on": True, "auto_mode": True,
         "defrost": False, "rear_defogger": False, "fan_speed": 3, "air_direction": "face",
         "target_temp": 21.0, "status_source": "avc"}
RECIRC = dict(FRESH, recirculation=True, fresh_air=False)
IN_MAP = {"in": True, "src": "map", "map_usable": True}
OUT = {"in": False, "src": "none", "map_usable": True}


def test_tunnel_shadow_logs_would_press_and_restore():
    r = Rig(TunnelRecircFunction)
    base = dict(map_ok=True, map_conf=0.95, climate_live=True, climate=FRESH, real_kmh=60.0)
    r.run(5.0, tunnel=OUT, **base)
    r.run(30.0, tunnel=IN_MAP, **base)
    r.run(30.0, tunnel=OUT, **base)
    acts = r.entries("action")
    assert [(a["action"], a["executed"]) for a in acts] == [("recirc_on", False), ("recirc_off", False)]
    assert r.acts == []


def test_tunnel_on_presses_restores_and_respects_feedback():
    r = Rig(TunnelRecircFunction, modes={"tunnel_recirc": "on"})
    base = dict(map_ok=True, map_conf=0.95, climate_live=True, real_kmh=60.0)
    r.run(2.0, tunnel=OUT, climate=FRESH, **base)
    r.run(2.0, tunnel=IN_MAP, climate=FRESH, **base)
    assert r.acts == ["recirc_on"]
    r.run(40.0, tunnel=IN_MAP, climate=RECIRC, **base)            # the car confirms
    r.run(10.0, tunnel=OUT, climate=RECIRC, **base)
    assert r.acts == ["recirc_on"]                                 # waits 5 + 20 s
    r.run(20.0, tunnel=OUT, climate=RECIRC, **base)
    assert r.acts == ["recirc_on", "recirc_off"]


def test_tunnel_steps_back_when_the_driver_touches_the_climate():
    r = Rig(TunnelRecircFunction, modes={"tunnel_recirc": "on"})
    base = dict(map_ok=True, map_conf=0.95, climate_live=True, real_kmh=60.0)
    r.run(2.0, tunnel=OUT, climate=FRESH, **base)
    r.run(2.0, tunnel=IN_MAP, climate=FRESH, **base)
    r.run(10.0, tunnel=IN_MAP, climate=RECIRC, **base)
    r.run(10.0, tunnel=IN_MAP, climate=dict(RECIRC, target_temp=23.0), **base)   # driver
    r.run(60.0, tunnel=OUT, climate=dict(RECIRC, target_temp=23.0), **base)
    assert r.acts == ["recirc_on"]                                 # never restored
    assert any("stepped back" in e["what"] for e in r.entries("note"))


def test_tunnel_skips_when_not_fresh_or_defrost_or_cold_without_ac():
    for clim, extra in ((RECIRC, {}), (dict(FRESH, defrost=True), {}),
                        (dict(FRESH, ac_on=False), {"outside_c": 1.0}), (dict(FRESH, system_on=False), {})):
        r = Rig(TunnelRecircFunction, modes={"tunnel_recirc": "on"})
        base = dict(map_ok=True, map_conf=0.95, climate_live=True, real_kmh=60.0, climate=clim, **extra)
        r.run(2.0, tunnel=OUT, **base)
        r.run(30.0, tunnel=IN_MAP, **base)
        r.run(40.0, tunnel=OUT, **base)
        assert r.acts == [], clim
        assert r.entries("note")[0]["what"].startswith("skipped")


def test_tunnel_no_feedback_steps_back_and_vision_needs_3s():
    r = Rig(TunnelRecircFunction, modes={"tunnel_recirc": "on"})
    base = dict(map_ok=False, climate_live=True, real_kmh=60.0, climate=FRESH)
    r.run(2.0, tunnel={"in": True, "src": "vision", "map_usable": False}, **base)
    assert r.acts == []                                            # vision: 3 s
    r.run(2.0, tunnel={"in": True, "src": "vision", "map_usable": False}, **base)
    assert r.acts == ["recirc_on"]
    r.run(10.0, tunnel={"in": True, "src": "vision", "map_usable": False}, **base)   # status never changes
    r.run(60.0, tunnel=OUT, **base)
    assert r.acts == ["recirc_on"]
    assert any("no confirmation" in e["what"] for e in r.entries("note"))


def test_tunnel_rate_limit_between_tunnels():
    r = Rig(TunnelRecircFunction, modes={"tunnel_recirc": "on"})
    base = dict(map_ok=True, map_conf=0.95, climate_live=True, real_kmh=60.0)
    r.run(2.0, tunnel=OUT, climate=FRESH, **base)
    r.run(2.0, tunnel=IN_MAP, climate=FRESH, **base)
    r.run(3.0, tunnel=IN_MAP, climate=RECIRC, **base)
    r.run(26.0, tunnel=OUT, climate=RECIRC, **base)                # restored at ~31 s
    r.run(2.0, tunnel=IN_MAP, climate=FRESH, **base)               # next tunnel within 30 s of the restore
    assert r.acts == ["recirc_on", "recirc_off"]


# ── 6. eco ───────────────────────────────────────────────────────────────────

def test_eco_lift_hint_before_a_roundabout():
    r = Rig(EcoCoachFunction)
    v = 70.0

    def ahead(t):
        return {"junction": {"m": 500 - v / 3.6 * (t - 1000.0), "kind": "roundabout"}}
    r.run(20.0, real_kmh=v, map_ok=True, map_conf=0.95, ahead=ahead, throttle_pct=15.0)
    f = r.fires("eco_lift")
    # glide from 70 to 30 at 0.7 m/s2: (19.4^2 - 8.3^2) / 1.4 = 220 m
    assert len(f) == 1 and f[0]["evidence"]["glide_m"] == 220 and f[0]["evidence"]["m"] >= 220
    r2 = Rig(EcoCoachFunction)
    r2.run(20.0, real_kmh=v, map_ok=True, map_conf=0.95, ahead=ahead, throttle_pct=0.0)   # already lifted
    assert r2.fires() == []
    r3 = Rig(EcoCoachFunction)                                    # camera warning sign, no map
    r3.run(20.0, real_kmh=v, signs_ahead=lambda t: ({"id": "roundabout_ahead", "m": 500 - v / 3.6 * (t - 1000)},),
           throttle_pct=None)
    assert len(r3.fires("eco_lift")) == 1


def test_braking_score_per_stop():
    r = Rig(EcoCoachFunction)
    seq = {"n": 0}

    def hyb(t):
        seq["n"] += 1
        return seq["n"] // 10                                     # a new 21C3 sample per second
    r.run(5.0, real_kmh=50.0, hybrid_seq=hyb)

    def speed(t):
        return max(0.0, 50.0 - 6.0 * (t - 1005.0))
    r.run(10.0, real_kmh=speed, brake=True, regen_nm=60.0, master_cyl_nm=80.0, hybrid_seq=hyb)
    st = r.fn.status()
    assert st["drive"]["stops"] == 1 and st["drive"]["regen_share"] == pytest.approx(0.75, abs=0.01)
    assert r.entries("stat")[0]["data"]["from_kmh"] == 50.0


# ── 7. winter ────────────────────────────────────────────────────────────────

def test_ice_risk_at_start_and_on_crossing():
    r = Rig(WinterFunction)
    r.run(40.0, outside_c=1.0, climate_live=True, climate={"rear_defogger": False})
    f = r.fires("ice_risk")
    assert len(f) == 1 and f[0]["evidence"]["at"] == "start"
    assert len(r.fires("rear_defog_hint")) == 1
    r2 = Rig(WinterFunction)
    r2.run(40.0, outside_c=7.0)
    r2.run(50.0, outside_c=3.0)
    assert r2.fires() == []                                       # < 60 s
    r2.run(15.0, outside_c=3.0)
    assert len(r2.fires("ice_risk")) == 1
    r3 = Rig(WinterFunction)
    r3.run(25.0, outside_c=-2.0)                                  # settle time
    assert r3.fires() == []


# ── 8. maintenance ───────────────────────────────────────────────────────────

def test_service_due_at_drive_start(tmp_path):
    s = AlertSettings(str(tmp_path))
    r = Rig(MaintenanceFunction, settings=s)
    r.run(1.0, odometer_km=250000.0, real_kmh=0.0)
    ok, _ = r.fn.mark_service_done("oil", 240300.0)
    assert ok and not r.fn.mark_service_done("bogus")[0]
    r.run(5.0, odometer_km=250000.0, real_kmh=0.0, ready=False)
    r.run(65.0, odometer_km=250000.0, real_kmh=0.0, ready=False)
    r.run(5.0, odometer_km=250000.0, real_kmh=0.0)                 # new drive
    f = r.fires("service_due")
    assert f and f[-1]["text"] == "OIL + FILTER IN 300km"
    rows = {x["id"]: x for x in r.fn.status()["services"]}
    assert rows["oil"]["status"] == "due" and rows["air_filter"]["status"] == "unset"


def test_range_needs_a_real_fuel_level_and_history(tmp_path):
    s = AlertSettings(str(tmp_path))
    r = Rig(MaintenanceFunction, settings=s)
    r.run(2.0, fuel_l=None, meter_avg_l100=5.0)
    assert r.fn.range_km is None
    r.run(2.0, fuel_l=2.0, meter_avg_l100=5.0)
    assert r.fn.range_km == pytest.approx(40.0) and r.fires("range_low")[0]["text"] == "RANGE ~40km (PETROL)"
    s.set_memory("fuel_history", [[100.0, 4.0, 0], [100.0, 4.0, 0]])
    r.run(1.0, fuel_l=20.0, meter_avg_l100=5.0)
    assert r.fn.l100 == pytest.approx(4.0) and r.fn.range_km == pytest.approx(500.0)


def test_new_dtc_baseline_then_notice_at_standstill(tmp_path):
    s = AlertSettings(str(tmp_path))
    r = Rig(MaintenanceFunction, settings=s)
    r.run(1.0, dtc_source_seen=True, dtcs=frozenset({"ENGINE:P0420"}), real_kmh=0.0)
    assert r.fires() == [] and s.memory("known_dtcs") == ["ENGINE:P0420"]
    new = frozenset({"ENGINE:P0420", "HYBRID:P0A80"})
    r.run(5.0, dtc_source_seen=True, dtcs=new, real_kmh=60.0)      # driving: waits
    assert r.fires() == []
    r.run(35.0, dtc_source_seen=True, dtcs=new, real_kmh=0.0)
    f = r.fires("new_dtc")
    assert len(f) == 1 and f[0]["text"] == "NEW FAULT P0A80"
    assert "HYBRID:P0A80" in s.memory("known_dtcs")


# ── 9. guardian ──────────────────────────────────────────────────────────────

OFF = dict(ready=False, real_kmh=0.0, gear="PARK", bean_frames=100)


def test_guardian_unlocked_door_lamps_once_per_parking():
    r = Rig(GuardianFunction)
    r.run(310.0, step=1.0, doors_locked=False, door_open=True, door_open_which=("rear",), lamp_tail=True, **OFF)
    ids = sorted(e["id"] for e in r.fires())
    assert ids == ["door_open_parked", "lamps_left_on", "left_unlocked"]
    assert [e["text"] for e in r.fires("door_open_parked")] == ["REAR OPEN"]
    r.run(10.0, step=1.0, doors_locked=True, door_open=False, lamp_tail=False, **OFF)
    r.run(400.0, step=1.0, doors_locked=False, door_open=False, lamp_tail=False, **OFF)
    assert len(r.fires("left_unlocked")) == 1                      # once per parking
    r.run(5.0, step=1.0, ready=True, real_kmh=0.0)                 # car on -> new parking
    r.run(310.0, step=1.0, doors_locked=False, **OFF)
    assert len(r.fires("left_unlocked")) == 2


def test_guardian_intrusion_and_owner_unlock():
    r = Rig(GuardianFunction)
    r.run(20.0, step=1.0, doors_locked=True, door_open=False, **OFF)
    r.run(5.0, step=1.0, doors_locked=True, door_open=True, **OFF)
    assert len(r.fires("intrusion")) == 1
    r2 = Rig(GuardianFunction)
    r2.run(20.0, step=1.0, doors_locked=True, door_open=False, **OFF)
    t = r2.clk.t
    r2.run(5.0, step=1.0, doors_locked=True, door_open=True, lock_event="smart_unlock", lock_event_ts=t - 5, **OFF)
    assert r2.fires("intrusion") == []


def test_guardian_low_12v_resting():
    r = Rig(GuardianFunction)
    r.run(1800.0, step=10.0, v12=12.0, powerbox_fresh=True, **OFF)
    assert r.fires() == []
    r.run(610.0, step=10.0, v12=12.0, powerbox_fresh=True, **OFF)
    assert len(r.fires("low_12v")) == 1
    r2 = Rig(GuardianFunction)
    r2.run(3000.0, step=10.0, v12=12.6, powerbox_fresh=True, **OFF)
    assert r2.fires() == []


def test_guardian_silent_while_the_car_is_on():
    r = Rig(GuardianFunction)
    r.run(400.0, step=1.0, doors_locked=False, door_open=True, lamp_tail=True, bean_frames=10)
    assert r.fires() == []


# ── 10. lane departure ───────────────────────────────────────────────────────

def _lanes(y_left, y_right):
    return ({"y": [-5.0] * 11, "prob": 0.9}, {"y": [y_left] * 11, "prob": 0.9},
            {"y": [y_right] * 11, "prob": 0.9}, {"y": [5.0] * 11, "prob": 0.9})


def test_lane_departure_refuses_on_and_logs_in_shadow():
    r = Rig(LaneDepartureFunction)
    assert r.m.mode("lane_departure") == "off"
    ok, why = r.m.set_mode("lane_departure", "on")
    assert not ok and "turn-signal" in why
    assert r.m.set_mode("lane_departure", "shadow")[0]
    r.run(2.0, real_kmh=90.0, lanes=_lanes(-1.7, 1.7))
    assert r.fires() == []

    def drift(t):
        y = -1.7 + 0.5 * (t - r.clk.t0)                           # 0.5 m/s to the left line
        return _lanes(y, y + 3.4)
    r.clk.t0 = r.clk.t
    r.run(3.0, real_kmh=90.0, lanes=drift)
    f = r.fires("lane_departure")
    assert len(f) == 1 and f[0]["evidence"]["turn_signal"] == "unknown" and f[0]["text"] == "LANE LEFT"


def test_lane_departure_on_allowed_once_turn_signal_known():
    r = Rig(LaneDepartureFunction)
    r.run(0.2, turn_signal="off")
    assert r.m.set_mode("lane_departure", "on")[0]


# ── replay on recorded data ──────────────────────────────────────────────────

def test_replay_recorded_fuel_drive_finds_the_stops():
    from cyberpunk_computer.alerts.replay import Replay
    rp = Replay()
    entries = rp.run(str(FIX / "fuel_drive_2026-11-13.csv.gz"))
    stops = [e for e in entries if e["event"] == "stat" and e["id"] == "eco_stop"]
    assert len(stops) == 5 and all(s["data"]["from_kmh"] > 20 for s in stops)
    # no limit, lead, map or outside temperature in that recording: nothing may fire
    assert [e for e in entries if e["event"] == "fire"] == []
    assert rp.svc.manager.errors == 0


def test_replay_cli_with_roadsense(tmp_path, capsys):
    from cyberpunk_computer.alerts.replay import main
    out = tmp_path / "o.jsonl"
    assert main([str(FIX / "fuel_drive_2026-11-13.csv.gz"), "--roadsense",
                 str(FIX / "roadsense_ws_2026-11-14.jsonl.gz"), "--rs-offset", "100", "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "replayed" in text and "braking score" in text and out.exists()
