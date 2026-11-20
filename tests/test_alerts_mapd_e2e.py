"""Map-driven alert functions on the real roadsense stack: fake camera
daemons + chris-integrator (map box, consolidated API; was mapd + api before
lane L44), the backend's position feeder with the
synthetic drive (Opole), and the backend's own scene feed. The scenes go into
the alert manager as on the car (shadow mode), with the true speed raised so
the map look-ahead functions have something to say.

Skips like test_position_mapd_e2e.py (no roadsense checkout / pack / api
Python). Real time, about 30 s.
"""

import time

import pytest

from cyberpunk_computer.alerts.functions.curve import CurveFunction
from cyberpunk_computer.alerts.functions.eco import EcoCoachFunction
from cyberpunk_computer.alerts.functions.speed_limit import SpeedLimitFunction
from cyberpunk_computer.alerts.functions.tunnel import TunnelRecircFunction
from cyberpunk_computer.alerts.inputs import build_inputs
from cyberpunk_computer.alerts.manager import AlertManager
from cyberpunk_computer.alerts.persist import AlertSettings
from cyberpunk_computer.alerts.tuning import AlertTuning
from cyberpunk_computer.backend.position_feeder import (
    CanMotionTap, PositionFeeder, PositionFeederConfig, SyntheticGnssSource,
)

import test_position_mapd_e2e as e2e
from test_position_mapd_e2e import stack  # noqa: F401  (module fixture)

pytestmark = e2e.pytestmark


def _state(kmh):
    from dataclasses import replace
    from cyberpunk_computer.state.app_state import AppState, GearPosition
    st = AppState()
    return replace(st, vehicle=replace(st.vehicle, real_speed_kmh=kmh, gear=GearPosition.DRIVE,
                                       ready_mode=True, throttle_position=20.0))


def test_map_lookahead_functions_on_a_synthetic_drive(stack):  # noqa: F811
    if not stack.api_port:
        pytest.skip("no Python with aiohttp/numpy/PIL for the roadsense api")
    cfg = PositionFeederConfig(enabled=True, source="synthetic", rundir=stack.chris_rundir,
                               synthetic_dropouts=(), synthetic_start_m=0.0)
    src = SyntheticGnssSource(cfg)
    feeder = PositionFeeder(cfg, src, tap=CanMotionTap())
    scenes = []
    sfeed = e2e._scene_feed(stack, scenes)
    t = AlertTuning()
    t.curve.min_match_conf = 0.5
    t.curve.lead_time_min_s = 0.5        # the synthetic route's curves are close at 110 km/h
    t.curve.skip_built_up = False        # Opole: the route is in a built-up area
    fns = [SpeedLimitFunction(t), CurveFunction(t), EcoCoachFunction(t), TunnelRecircFunction(t)]
    m = AlertManager(fns, AlertSettings(None))
    feeder.start()
    feeder.set_active(True)
    try:
        t0 = time.monotonic()
        seen = 0
        usable = []
        while time.monotonic() - t0 < 25.0:
            time.sleep(0.1)
            if len(scenes) == seen:
                continue
            seen = len(scenes)
            sc = scenes[-1]
            # scenes as the backend tags them: synthetic drives are marked; the alert
            # inputs refuse those (map limit / look-ahead), so untag for this test only
            mp = sc.get("map")
            if isinstance(mp, dict):
                mp = dict(mp)
                mp.pop("syn", None)
                if isinstance(mp.get("in"), dict):
                    mp["in"] = dict(mp["in"], gnss="test")
                sc = dict(sc, map=mp)
            now = time.monotonic()
            inp = build_inputs(_state(110.0), sc, 0.1, now, time.time(), {})
            if inp.map_ok:
                usable.append(inp)
            m.tick(inp)
        assert usable, "the map layer never became usable on the synthetic drive: %s" % (
            [((s.get("map") or {}).get("ok"), (s.get("map") or {}).get("why"), (s.get("map") or {}).get("syn")) for s in scenes[-40::4]],)
        assert any(i.ahead for i in usable), "no look-ahead in any usable scene"
        assert any(i.limit is not None and i.limit.from_map_layer for i in usable)
        curves = [i.ahead["curve"] for i in usable if i.ahead.get("curve")]
        fired = [e for e in m.log.recent_entries(500) if e.get("event") == "fire"]
        def eligible(a):
            c, j = a.get("curve"), a.get("junction")
            return (c and c["r"] < 300 and 0.5 <= c["m"] / (110 / 3.6) <= 9.0
                    and not (j and abs(j["m"] - c["m"]) <= 40))
        cf = [e for e in fired if e["id"] == "curve_speed"]
        if any(eligible(i.ahead) for i in usable):
            assert cf, "a sharp map curve (no junction at it) at 110 km/h gave no curve notice"
            ev = cf[0]["evidence"]
            assert ev["v_ok_kmh"] < 110 and ev["r"] < 300 and ev["map_conf"] >= 0.5
        else:
            # every curve seen on this route starts at a junction: a turn, never a notice
            assert cf == []
        # every fire carries its evidence and the shadow mode
        assert all(e["mode"] == "shadow" and e["evidence"] for e in fired)
        assert m.errors == 0
        print("\nmap e2e: %d usable scenes, curves %s, fired %s" % (
            len(usable), curves[:3], [(e["id"], e.get("text")) for e in fired]))
    finally:
        feeder.stop()
        sfeed.stop()
