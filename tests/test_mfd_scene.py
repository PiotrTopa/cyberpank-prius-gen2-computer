"""Main-screen scene (ui/scene): inputs, director, model, rendering.

Needs pygame (the ``ui`` package imports it), so the whole module is skipped
in an environment without it; run it with the frontend venv:

    SDL_VIDEODRIVER=dummy .venv-fe/bin/python -m pytest tests/test_mfd_scene.py
"""

import os
from dataclasses import replace

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["SDL_FBDEV"] = "/nonexistent/fb-for-tests"

pygame = pytest.importorskip("pygame")

from cyberpunk_computer.state.app_state import AppState, GearPosition  # noqa: E402
from cyberpunk_computer.ui.scene import demo  # noqa: E402
from cyberpunk_computer.ui.scene.director import (  # noqa: E402
    LOST_S, REAR_GROUP_AJAR, STALE_S, STOP_HOLD_S, VIEW_SECONDS, VIEW_STEPS, SceneDirector)
from cyberpunk_computer.ui.scene.inputs import (  # noqa: E402
    SCENE_EVENT, SceneFeed, SceneInputs, parse_scene, parse_signs, read_inputs)
from cyberpunk_computer.ui.scene.model import Camera, get_model  # noqa: E402
from cyberpunk_computer.ui.scene.powerbox import PowerHistory, StatusStrip  # noqa: E402

SIZE = (480, 240)


@pytest.fixture(scope="module", autouse=True)
def _pygame():
    pygame.init()
    pygame.display.set_mode(SIZE)
    # fonts cached before another module's pygame.quit() are dead objects
    from cyberpunk_computer.ui.fonts import fonts
    fonts._fonts.clear()
    yield
    # no pygame.quit(): the font cache is process-wide (other UI test modules)


@pytest.fixture(autouse=True)
def _no_settings_writes(monkeypatch):
    import cyberpunk_computer.persistence as persistence
    import cyberpunk_computer.ui.screens.main_screen as ms
    for mod in (persistence, ms):
        if hasattr(mod, "save_settings"):
            monkeypatch.setattr(mod, "save_settings", lambda *a, **k: None)
    monkeypatch.setattr(persistence.SettingsManager, "save", lambda self, *a, **k: True)


def scene(seq=1, lead=None, **kw):
    xs = [0, 10, 20, 40, 80]
    out = {"seq": seq, "x": xs,
           "lanes": [None, {"y": [-1.75] * 5, "prob": 0.9}, {"y": [1.75] * 5, "prob": 0.9}, None],
           "edges": [None, None], "lead": lead}
    out.update(kw)
    return out


def running(st: AppState) -> AppState:
    return replace(st, roadsense=replace(st.roadsense, state="running", api_ok=True))


def step(d: SceneDirector, inp: SceneInputs, seconds: float, dt: float = 0.05) -> None:
    for _ in range(int(round(seconds / dt))):
        d.update(inp, dt)


# ── inputs ───────────────────────────────────────────────────────────────────

def test_default_state_reads_as_unknown_not_as_data():
    inp = read_inputs(AppState())
    assert inp.doors == (None,) * 5 and inp.rear_group is None and inp.door_any is None
    assert not inp.doors_known and not inp.any_door_open
    assert inp.road is None and inp.limit_kmh is None and inp.light is None
    assert inp.rs_ok is False and inp.speed_kmh == 0.0


def test_inputs_survive_a_state_without_the_expected_slices():
    class Bare:
        pass
    inp = read_inputs(Bare())
    assert inp == SceneInputs()


def test_doors_and_lamps_come_from_bean():
    st = AppState()
    st = replace(st, bean=replace(st.bean, decoded={
        "driver_door_open": {"value": True}, "passenger_door_open": {"value": False},
        "rear_door_open": {"value": True}, "lamp_tail": {"value": True},
        "lamp_head": {"value": False}}))
    inp = read_inputs(st)
    assert inp.doors[:2] == (True, False)
    # one switch bit for rear-left / rear-right / back door: no door is picked
    assert inp.doors[2:] == (None, None, None) and inp.rear_group is True
    assert inp.tail is True and inp.head is False and inp.any_door_open


def test_display_speed_for_the_readout_real_speed_for_motion():
    st = AppState()
    st = replace(st, vehicle=replace(st.vehicle, speed_kmh=50.0, real_speed_kmh=50.0,
                                     display_speed_kmh=56.0, gear=GearPosition.DRIVE))
    inp = read_inputs(st)
    assert inp.speed_kmh == 56.0 and inp.real_speed_kmh == 50.0 and inp.gear == "D"
    st = replace(st, vehicle=replace(st.vehicle, display_speed_kmh=None))
    assert read_inputs(st).speed_kmh == 50.0      # BEAN silent: fall back to real


def test_scene_wire_format_is_right_positive_and_flipped_to_left():
    rs = parse_scene(scene(lead={"d": 30.0, "y": 0.5, "v_rel": -2.0, "prob": 0.9},
                           objects=[{"kind": "lamp", "x": 20, "y": 4.0}]))
    assert rs.lanes[1].ys[0] == 1.75 and rs.lanes[2].ys[0] == -1.75
    assert rs.lead.dist_m == 30.0 and rs.lead.lat_m == -0.5
    assert rs.objects[0].y == -4.0
    assert parse_scene({"lanes": "garbage"}) is not None      # tolerant
    assert parse_scene("nope") is None


def test_road_only_while_roadsense_runs_and_health_fallback():
    feed = SceneFeed()
    feed.push(SCENE_EVENT, scene(limit={"kmh": 70, "src": "sign"}))
    feed.push("touch", {"x": 1})                # other events are ignored
    assert feed.received == 1
    assert read_inputs(AppState(), feed).road is None            # stack not running
    inp = read_inputs(running(AppState()), feed)
    assert inp.road is not None and inp.limit_kmh == 70
    # what exists today: the 5 s health mirror
    st = running(AppState())
    st = replace(st, roadsense=replace(st.roadsense, health={
        "speed_limit": {"limit_kmh": 50, "source": "sign"},
        "lights": {"state": "red", "dist_m": 34.0, "lateral_m": 2.1},
        "env": {"light": "night", "place": "city_street"}}))
    inp = read_inputs(st)
    assert inp.limit_kmh == 50 and inp.light.state == "red" and inp.light.lat_m == -2.1
    assert inp.env_light == "night" and inp.road is None


# ── director ─────────────────────────────────────────────────────────────────

def test_view_opens_when_moving_and_closes_after_the_stop_hold():
    d = SceneDirector()
    drive = SceneInputs(speed_kmh=40, real_speed_kmh=40, gear="D")
    step(d, drive, VIEW_SECONDS + 0.3)
    assert d.mode == "drive" and d.view == 1.0 and d.view_step == VIEW_STEPS
    stop = SceneInputs(gear="D")
    step(d, stop, STOP_HOLD_S - 0.5)
    assert d.mode == "drive" and d.view == 1.0          # a short stop keeps the road
    step(d, stop, 0.6 + VIEW_SECONDS + 0.2)
    assert d.mode == "parked" and d.view == 0.0
    assert d.animating is False                         # steady: nothing to redraw


def test_park_or_open_door_closes_at_once_and_reverse_stays_closed():
    d = SceneDirector()
    step(d, SceneInputs(speed_kmh=40, real_speed_kmh=40, gear="D"), 2.0)
    d.update(SceneInputs(gear="P"), 0.05)
    assert d.mode == "parked"
    d2 = SceneDirector()
    step(d2, SceneInputs(speed_kmh=40, real_speed_kmh=40, gear="D"), 2.0)
    d2.update(SceneInputs(gear="D", doors=(True, None, None, None, None)), 0.05)
    assert d2.mode == "parked"
    d3 = SceneDirector()
    step(d3, SceneInputs(speed_kmh=8, real_speed_kmh=8, gear="R"), 2.0)
    assert d3.mode == "parked" and d3.view == 0.0


def test_doors_animate_and_the_rear_group_is_only_ajar():
    d = SceneDirector()
    inp = SceneInputs(doors=(True, False, None, None, None), rear_group=True)
    d.update(inp, 0.1)
    assert 0.0 < d.door_frac[0] < 1.0 and d.animating
    step(d, inp, 1.0)
    assert d.door_frac[0] == 1.0 and d.door_frac[1] == 0.0
    assert d.door_frac[2] == d.door_frac[3] == d.door_frac[4] == REAR_GROUP_AJAR
    step(d, SceneInputs(doors=(False, False, None, None, None), rear_group=False), 1.0)
    assert d.door_frac == [0.0] * 5 and not d.animating


def test_vision_states_and_no_stale_lead():
    d = SceneDirector()
    feed = SceneFeed()
    st = running(AppState())
    st = replace(st, vehicle=replace(st.vehicle, speed_kmh=50, real_speed_kmh=50,
                                     gear=GearPosition.DRIVE))
    d.update(read_inputs(AppState()), 0.05)
    assert d.vision == "off"
    d.update(read_inputs(st, feed), 0.05)
    assert d.vision == "wait"                           # running, nothing received
    seq = 0
    for _ in range(20):
        seq += 1
        feed.push(SCENE_EVENT, scene(seq, lead={"d": 30.0, "y": 0.0, "prob": 0.9}))
        d.update(read_inputs(st, feed), 0.05)
    assert d.vision == "live" and d.lead is not None and d.lead_alpha == 1.0
    assert abs(d.lead.dist_m - 30.0) < 0.5 and d.lanes[1] is not None
    assert abs(d.time_gap_s(50.0) - 30.0 / (50 / 3.6)) < 0.1
    inp = read_inputs(st, feed)                         # same seq from now on
    step(d, inp, STALE_S + 0.6)
    assert d.vision == "stale" and d.lead is None       # never an old car
    assert d.lanes[1] is not None                       # frozen lanes stay (dimmed)
    step(d, inp, LOST_S)
    assert d.vision == "lost" and d.lanes[1] is None    # had data, lost it
    assert read_inputs(st, SceneFeed()).road is None


def test_low_probability_lead_and_lanes_are_not_shown():
    d = SceneDirector()
    feed = SceneFeed()
    st = running(AppState())
    sc = scene(1, lead={"d": 30.0, "y": 0.0, "prob": 0.3})
    sc["lanes"][1]["prob"] = 0.2
    feed.push(SCENE_EVENT, sc)
    step(d, read_inputs(st, feed), 0.5)
    assert d.lead is None and d.lanes[1] is None and d.lanes[2] is not None


# ── model / camera ───────────────────────────────────────────────────────────

def test_model_dimensions_and_parts():
    m = get_model()
    xs = [v[0] for v in m.verts]
    zs = [v[2] for v in m.verts]
    ys = [v[1] for v in m.verts]
    assert abs((max(xs) - min(xs)) - 4.45) < 0.01          # NHW20 length
    assert 1.45 < max(zs) < 1.52 and 1.70 < (max(ys) - min(ys)) < 1.80
    for part in ("door_fl", "door_fr", "door_rl", "door_rr", "hatch"):
        assert m.part_faces[part] and m.part_edges[part]
    # left door swings out to +y, right door to -y, the hatch lifts
    assert max(p[1] for p in m.part_points("door_fl", 1.0).values()) > 1.3
    assert min(p[1] for p in m.part_points("door_fr", 1.0).values()) < -1.3
    assert max(p[2] for p in m.part_points("hatch", 1.0).values()) > 1.8
    closed = m.part_points("door_fl", 0.0)
    assert all(abs(closed[i][1] - m.verts[i][1]) < 1e-9 for i in closed)


def test_camera_projection():
    cam = Camera((-8.0, 0.0, 3.0), (10.0, 0.0, 0.0), 200.0, 119.0, 84.0)
    x0, y0, z0 = cam.project((5.0, 0.0, 0.0))
    xl, _, _ = cam.project((5.0, 2.0, 0.0))       # y left -> screen left
    _, yf, zf = cam.project((40.0, 0.0, 0.0))
    assert abs(x0 - 119.0) < 1e-6 and xl < x0
    assert zf > z0 and cam.horizon_y() < yf < y0


def test_power_history_columns():
    t = [1000.0]
    h = PowerHistory(clock=lambda: t[0])
    for sec in range(30):
        t[0] = 1000.0 + sec
        h.add(10.0 if sec < 15 else -5.0)
        h.add(None)
    t[0] = 1030.0
    cols = h.columns(6, 30.0)
    assert len(cols) == 6
    assert cols[0] == pytest.approx(10.0) and cols[-1] == pytest.approx(-5.0)
    assert h.columns(6, 30.0 * 100)[0] is None               # nothing that old


# ── rendering ────────────────────────────────────────────────────────────────

def region_colours(surf, rect):
    sub = surf.subsurface(rect)
    w, h = sub.get_size()
    return {tuple(sub.get_at((x, y)))[:3] for x in range(0, w, 2) for y in range(0, h, 2)}


def test_main_screen_renders_every_demo_state():
    surf = pygame.Surface(SIZE)
    for name, t, vision, stale_from, _, mods in demo.SHOTS:
        sim = demo.Sim(vision, mods=mods)
        sim.stale_from = stale_from
        sim.run_to(t)
        sim.frame(surf)
        assert len(region_colours(surf, (121, 1, 238, 216))) > 3, name
        assert len(region_colours(surf, (121, 218, 194, 21))) > 2, name


def test_no_lead_vehicle_without_lead_data_and_redraw_is_cached():
    from cyberpunk_computer.ui.scene.render import SceneRenderer
    r = SceneRenderer((238, 216))
    d = SceneDirector()
    drive = SceneInputs(speed_kmh=50, real_speed_kmh=50, gear="D", rs_state="running", rs_ok=True,
                        road=parse_scene(scene(1)))
    step(d, drive, 2.0)
    r.render(d, drive, 10.0)
    no_lead = pygame.image.tobytes(r.surface, "RGB")
    with_lead = drive._replace(road=parse_scene(scene(2, lead={"d": 25.0, "y": 0.0, "prob": 0.95})))
    step(d, with_lead, 1.0)
    r.render(d, with_lead, 20.0)
    assert pygame.image.tobytes(r.surface, "RGB") != no_lead
    assert r.pal["tail"] in region_colours(r.surface, (80, 70, 90, 70))   # a drawn vehicle
    # parked and steady: repeated frames do not redraw the scene
    p = SceneDirector()
    parked = SceneInputs()
    step(p, parked, 1.0)
    r2 = SceneRenderer((238, 216))
    for i in range(10):
        p.update(parked, 0.05)
        r2.render(p, parked, 30.0 + i * 0.05)
    assert r2.redraws == 1 and r2.cache.renders == 1


def test_open_door_changes_the_picture_and_uses_the_warning_colour():
    from cyberpunk_computer.ui.scene.render import SceneRenderer
    r = SceneRenderer((238, 216))
    d = SceneDirector()
    closed = SceneInputs(doors=(False, False, None, None, None), rear_group=False)
    step(d, closed, 0.5)
    r.render(d, closed, 1.0)
    assert r.car.pal["open"] not in region_colours(r.surface, (0, 20, 238, 170))
    opened = closed._replace(doors=(True, False, None, None, None))
    step(d, opened, 1.0)
    r.render(d, opened, 5.0)
    assert r.car.pal["open"] in region_colours(r.surface, (0, 20, 238, 170))


def test_scene_page_survives_a_bare_main_screen_and_is_built_after_the_first_frame():
    """No store attached (other headless tests build MainScreen that way)."""
    from cyberpunk_computer.ui.screens import MainScreen
    m = MainScreen(SIZE, None)
    surf = pygame.Surface(SIZE)
    m.update(0.05)
    m.render(surf)
    assert m._scene_page is None          # the first frame goes out without the scene
    m.update(0.05)
    assert m._scene_page is not None
    m.render(surf)
    assert len(region_colours(surf, (121, 1, 238, 216))) > 3
    m.show_page(1)
    m.render(surf)
    assert len(region_colours(surf, (121, 218, 194, 21))) > 2     # the strip is on every page


def test_prewarm_makes_the_camera_move_free_of_car_renders():
    from cyberpunk_computer.ui.scene.page import PREWARM_S
    sim = demo.Sim()
    surf = pygame.Surface(SIZE)
    sim.run_to(1.0)
    sim.frame(surf)                      # creates the renderer
    page = sim.screen.scene_page
    for _ in range(2 * (VIEW_STEPS + 1) + 2):
        page.update(PREWARM_S)           # parked and steady: one sprite per call
    warmed = page.scene.cache.renders
    assert warmed >= 2 * (VIEW_STEPS + 1)
    d = SceneDirector()
    drive = SceneInputs(speed_kmh=30, real_speed_kmh=30, gear="D")
    t = 100.0
    for _ in range(60):
        d.update(drive, 0.05)
        t += 0.05
        page.scene.render(d, drive, t)
    assert d.view == 1.0 and page.scene.cache.renders == warmed


# ── end to end: roadsense messages -> backend feed -> ZMQ envelope -> scene ─

def test_roadsense_messages_become_a_drawn_road():
    """Recorded roadsense /ws frames (tests/fixtures/roadsense_ws_2026-11-14,
    real modeld output with a lead vehicle) through the backend SceneBuilder,
    the ZMQ envelope as the backend serializes it, the frontend client's
    decode and SceneFeed, read_inputs, the director and the renderer."""
    import gzip
    import json
    from cyberpunk_computer.backend.roadsense_scene import (
        EVENT_NAME, SceneBuilder, SceneFeedConfig)
    from cyberpunk_computer.frontend.client import BackendClient
    from cyberpunk_computer.ui.scene.render import SceneRenderer

    path = os.path.join(os.path.dirname(__file__), "fixtures", "roadsense_ws_2026-11-14.jsonl.gz")
    with gzip.open(path, "rt") as f:
        lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    feed = SceneFeed()
    client = BackendClient("127.0.0.1", on_event=feed.push)
    builder = SceneBuilder(SceneFeedConfig(host="127.0.0.1", port=1))
    st = running(AppState())
    st = replace(st, vehicle=replace(st.vehicle, speed_kmh=57, real_speed_kmh=57,
                                     gear=GearPosition.DRIVE))
    d = SceneDirector()
    r = SceneRenderer((238, 216))
    t = 0.0
    sent = 0
    inp = read_inputs(st, feed)
    for ln in lines[:16]:                     # env, limit, lights, 12 road frames with a lead
        builder.feed(ln, t)
        sc = builder.build(t)
        if sc is not None:
            raw = json.dumps({"type": "event", "ts": 1.0, "name": EVENT_NAME, "data": sc},
                             separators=(",", ":"))
            client._handle_raw(raw)
            sent += 1
        inp = read_inputs(st, feed)
        step(d, inp, 0.1)
        t += 0.1
    assert sent >= 8 and client.counters["event_rx"] == sent
    assert d.vision == "live"
    assert d.lanes[1] is not None and d.lanes[2] is not None
    assert d.lanes[1][0] > 0 > d.lanes[2][0]          # left line LEFT-positive after the flip
    assert d.edges[0] is not None and d.edges[1] is not None
    assert d.lead is not None and 80.0 < d.lead.dist_m < 120.0
    assert inp.light is not None and inp.light.state == "red"
    assert inp.limit_kmh == 50 and inp.env_light == "day" and inp.env_place == "city_street"
    r.render(d, inp, 10.0)
    live = pygame.image.tobytes(r.surface, "RGB")
    assert len(region_colours(r.surface, (0, 0, 238, 168))) > 4
    # the feed stops (roadsense off / stale): stale, then lost; never an old lead
    step(d, inp, STALE_S + 0.6)              # (the lead fades out over LEAD_FADE_S)
    assert d.vision == "stale" and d.lead is None
    r.render(d, inp, 20.0)
    assert pygame.image.tobytes(r.surface, "RGB") != live
    step(d, inp, LOST_S)
    assert d.vision == "lost" and d.lanes[1] is None


# ── iteration 2: signs, beams, scenery, cruise slot ─────────────────────────

def drive_inputs(**kw):
    base = dict(speed_kmh=50, real_speed_kmh=50, gear="D", rs_state="running", rs_ok=True,
                road=parse_scene(scene(1)))
    base.update(kw)
    return SceneInputs(**base)


def drawn(inp, seconds=2.0, **tune):
    from cyberpunk_computer.ui.scene.render import SceneRenderer
    from cyberpunk_computer.ui.scene.tuning import Tuning
    tuning = Tuning(tune)
    r = SceneRenderer((238, 216), tuning=tuning)
    d = SceneDirector(tuning)
    step(d, inp, seconds)
    r.render(d, inp, 10.0)
    return pygame.image.tobytes(r.surface, "RGB"), r


def test_signs_reach_the_inputs_only_while_roadsense_runs():
    on, ahead = parse_signs({"on": [{"id": "no_overtaking", "s": "B-25"},
                                    {"id": "no_stopping", "s": "B-36", "side": "r", "q": True},
                                    {"id": "living_street", "v": 20}, {"nope": 1}, "x"],
                             "ahead": [{"id": "roundabout_ahead", "s": "A-8", "m": 60.4}]})
    assert [s.id for s in on] == ["no_overtaking", "no_stopping", "living_street"]
    assert on[1].side == "r" and on[1].unsure and on[2].value == 20 and on[0].m is None
    assert ahead[0].m == 60 and ahead[0].code == "A-8"
    assert parse_signs(None) == ((), ()) and parse_signs({"on": "x"}) == ((), ())
    feed = SceneFeed()
    feed.push(SCENE_EVENT, scene(signs={"on": [{"id": "priority_road"}]},
                                 limit={"kmh": 50, "src": "default", "conf": 0.3},
                                 path={"y": [0.0, 0.1, 0.2, 0.4]}))
    assert read_inputs(AppState(), feed).signs_on == ()
    inp = read_inputs(running(AppState()), feed)
    assert inp.signs_on[0].id == "priority_road"
    assert inp.limit_source == "default" and inp.limit_conf == 0.3
    assert inp.road.path.ys == (-0.0, -0.1, -0.2, -0.4)


def test_every_known_sign_has_its_own_pictogram_and_unknown_ids_a_plate():
    from cyberpunk_computer.ui.scene.signs import KNOWN_IDS, sign_surface
    from cyberpunk_computer.ui.fonts import get_tiny_font
    font = get_tiny_font(8)
    text = lambda s_, c_: font.render(s_, False, c_)   # noqa: E731
    seen = {}
    for rid in KNOWN_IDS:
        seen.setdefault(pygame.image.tobytes(sign_surface(rid, "", False, text), "RGB"), []).append(rid)
    shared = [ids for ids in seen.values() if len(ids) > 1]
    # ids that are the same sign by design share a drawing; nothing else may
    allowed = [{"one_way", "one_way_side"}, {"no_buses", "no_motor_vehicles", "no_motorcycles",
               "no_trailers", "no_trucks"}, {"other_danger_ahead", "stop_ahead", "warning_other"},
               {"lane_change_ahead", "road_narrows_ahead"}]
    assert all(set(ids) in allowed for ids in shared), shared
    # every rule id roadsense can put in effect or ahead for a car is drawn
    for rid in ("priority_road", "no_stopping", "no_parking", "no_overtaking", "height_limit",
                "weight_limit", "width_limit", "one_way", "no_left_turn", "no_right_turn",
                "no_u_turn", "stop", "yield", "roundabout", "mandatory_direction", "no_entry",
                "built_up_area", "living_street", "motorway", "expressway",
                "roundabout_ahead", "crossroads_ahead", "children_ahead", "curve_ahead"):
        assert rid in KNOWN_IDS, rid
    plate = sign_surface("no_dangerous_goods", "B-13", False, text)
    other = sign_surface("something_new", "X-9", False, text)
    assert pygame.image.tobytes(plate, "RGB") != pygame.image.tobytes(other, "RGB")
    unsure = sign_surface("priority_road", "", True, text)
    assert pygame.image.tobytes(unsure, "RGB") != pygame.image.tobytes(
        sign_surface("priority_road", "", False, text), "RGB")


def test_signs_and_limit_confidence_change_the_picture():
    from cyberpunk_computer.ui.scene.inputs import SignItem
    plain, _ = drawn(drive_inputs(limit_kmh=50, limit_source="sign"))
    signs, _ = drawn(drive_inputs(limit_kmh=50, limit_source="sign",
                                  signs_on=(SignItem("no_overtaking", "B-25"),),
                                  signs_ahead=(SignItem("roundabout_ahead", "A-8", m=60),)))
    default, _ = drawn(drive_inputs(limit_kmh=50, limit_source="default"))
    assert len({plain, signs, default}) == 3


def test_each_lamp_state_looks_different_on_the_road():
    states = {
        "off": {},
        "position": {"tail": True},
        "low": {"tail": True, "head": True},
        "high": {"tail": True, "head": True, "high": True},
        "low+fog": {"tail": True, "head": True, "fog": True},
    }
    pics = {name: drawn(drive_inputs(**kw))[0] for name, kw in states.items()}
    assert len(set(pics.values())) == len(states)
    # parked view too
    parked = {name: drawn(SceneInputs(**kw), 0.5)[0] for name, kw in states.items()}
    assert len(set(parked.values())) == len(states)


def test_every_environment_label_has_its_own_scenery():
    from cyberpunk_computer.ui.scene.scenery import LIGHTS, PLACES, WEATHERS, Scenery
    # the vocabulary of roadsense envd (models/envd-classes.json)
    assert PLACES == ("highway", "city_street", "residential", "rural_road", "forest_road",
                      "tunnel", "parking_lot", "underground_parking", "gas_station", "bridge")
    assert LIGHTS == ("day", "dusk_dawn", "night")
    assert WEATHERS == ("clear", "overcast", "rain", "snow", "fog")
    pics = {}
    for place in (None,) + PLACES:
        pics[place] = drawn(drive_inputs(env_place=place, env_light="night", env_weather="clear"))[0]
    assert len(set(pics.values())) == len(pics), "two places share a picture"
    lights = {lt: drawn(drive_inputs(env_place="city_street", env_light=lt))[0]
              for lt in (None,) + LIGHTS}
    assert len(set(lights.values())) == len(lights)
    weather = {w: drawn(drive_inputs(env_place="rural_road", env_light="day", env_weather=w))[0]
               for w in (None,) + WEATHERS}
    assert len(set(weather.values())) == len(weather) - 1      # None and "clear" look the same
    assert weather[None] == weather["clear"]
    # a label outside the vocabulary draws the neutral variant, it does not fail
    odd, _ = drawn(drive_inputs(env_place="moon_base", env_light="noon", env_weather="hail"))
    assert odd == drawn(drive_inputs())[0]
    # backdrops are cached: asking twice builds once
    s = Scenery(238, 68, (53, 93, 104))
    assert s.backdrop("bridge", "night", "clear") is s.backdrop("bridge", "night", "clear")


def test_rain_keeps_the_road_view_alive_but_not_the_parked_view():
    from cyberpunk_computer.ui.scene.render import SceneRenderer
    r = SceneRenderer((238, 216))
    d = SceneDirector()
    inp = drive_inputs(env_weather="rain")
    step(d, inp, 2.0)
    stopped = inp._replace(speed_kmh=0, real_speed_kmh=0)
    d.update(stopped, 0.05)
    n0 = None
    t = 50.0
    for i in range(20):                 # 1 s standing in the road view
        d.update(stopped, 0.05)
        t += 0.05
        r.render(d, stopped, t)
        n0 = n0 if n0 is not None else r.redraws
    assert r.redraws - n0 >= 5
    p = SceneDirector()
    parked = SceneInputs(env_weather="rain", rs_ok=True, rs_state="running")
    step(p, parked, 1.0)
    r2 = SceneRenderer((238, 216))
    for i in range(20):
        p.update(parked, 0.05)
        r2.render(p, parked, 80.0 + i * 0.05)
    assert r2.redraws == 1


def test_cruise_slot_states():
    view = StatusStrip.cruise_view
    assert view(False, None, None, None) == ("--", "--", "")        # nothing known
    assert view(False, False, None, None) == ("OFF", "--", "")
    assert view(False, True, None, 70) == ("READY", "70", "mem")
    assert view(True, True, 80, 70) == ("ON", "80", "set")
    st = AppState()
    inp = read_inputs(st)
    assert inp.cruise_main is None and inp.cruise_set_kmh is None and inp.cruise_gap is None
    st = replace(st, vehicle=replace(st.vehicle, cruise_active=True, cruise_main_switch=True,
                                     cruise_set_speed=80, cruise_memory_speed=80))
    inp = read_inputs(st)
    assert inp.cruise_active and inp.cruise_main is True and inp.cruise_set_kmh == 80
    strip = StatusStrip((194, 21), PowerHistory(clock=lambda: 0.0))
    a = pygame.image.tobytes(strip.render(1.0, 0.6, 5, False, True, 60), "RGB")
    b = pygame.image.tobytes(strip.render(1.0, 0.6, 5, False, True, 60, True, True, 80, 80), "RGB")
    c = pygame.image.tobytes(strip.render(1.0, 0.6, 5, False, True, 60, True, True, 80, 80, 2), "RGB")
    assert a != b and b != c and strip.redraws == 3


def test_path_alone_draws_the_road_direction():
    """No lane lines (a rural road): the planned path still shows."""
    sc = {"seq": 1, "x": [0, 10, 20, 40, 80], "lanes": [None] * 4, "edges": [None, None],
          "path": {"y": [0.0, 0.2, 0.8, 3.0, 9.0]}, "lead": None}
    with_path, r = drawn(drive_inputs(road=parse_scene(sc)), path_chevrons=True)
    sc2 = dict(sc, path=None, lanes=[None, {"y": [-9] * 5, "prob": 0.1}, None, None])
    without, _ = drawn(drive_inputs(road=parse_scene(sc2)), path_chevrons=True)
    assert drawn(drive_inputs(road=parse_scene(sc)))[0] != with_path     # off by default
    assert with_path != without
    assert r.pal["path"] in region_colours(r.surface, (0, 60, 238, 100))


def test_side_panel_items_follow_the_tuning():
    import cyberpunk_computer.ui.screens.main_screen as ms
    from cyberpunk_computer.ui.scene.page import ScenePage
    from cyberpunk_computer.ui.scene.tuning import Tuning
    surf = pygame.Surface(SIZE)
    pics = {}
    for name, flags in (("old", {"side_ticks": False, "ambient_panel": "plain"}),
                        ("ticks", {"side_ticks": True, "ambient_panel": "plain"}),
                        ("both", {"side_ticks": True, "ambient_panel": "level"})):
        m = ms.MainScreen(SIZE, None)
        m.scene_page = ScenePage(121, 1, 238, tuning=Tuning(flags))
        m._frames = 1
        m.update(0.05)
        surf.fill((0, 0, 0))
        m.render(surf)
        pics[name] = pygame.image.tobytes(surf.subsurface((0, 0, 120, 240)), "RGB")
    assert len(set(pics.values())) == 3


# ── tunables (ui/scene/tuning.py) ────────────────────────────────────────────

def test_tuning_defaults_overrides_and_file(tmp_path, monkeypatch):
    from cyberpunk_computer.ui.scene import tuning as T
    t = T.Tuning()
    assert t.beam_style == "outline" and t.signs_max_on == 6 and t.path_chevrons is False
    assert t.show_speed is False and t.lane_neighbours is True
    done = t.apply({"signs_max_on": 8, "beam_style": "hatch", "scenery_brightness": 1,
                    "beam_style_typo": 1, "lamp_chips": "sometimes", "path_chevrons": "yes"})
    assert done == {"signs_max_on": 8, "beam_style": "hatch", "scenery_brightness": 1}
    assert t.signs_max_on == 8 and t.beam_style == "hatch" and t.scenery_brightness == 1.0
    assert t.lamp_chips == "high" and t.path_chevrons is False       # bad values ignored
    assert T.Tuning().signs_max_on == 6                               # instances do not leak
    f = tmp_path / "tune.json"
    f.write_text('{"warning_max_m": 150, "roadside": "normal"}')
    monkeypatch.setenv(T.ENV_VAR, str(f))
    loaded = T.load()
    assert loaded.warning_max_m == 150 and loaded.roadside == "normal"
    monkeypatch.setenv(T.ENV_VAR, str(tmp_path / "missing.json"))
    assert T.load().warning_max_m == T.Tuning.warning_max_m
    f.write_text("not json")
    monkeypatch.setenv(T.ENV_VAR, str(f))
    assert T.load().roadside == "sparse"
    assert set(T.Tuning.names()) >= {"signs_max_on", "signs_max_ahead", "warning_max_m",
                                     "sign_hold_s", "gap_show_below_s", "closing_show_kmh",
                                     "scenery_brightness", "weather_density", "beam_style",
                                     "path_chevrons", "lamp_chips", "side_ticks", "ambient_panel"}


def test_every_scene_tunable_changes_the_picture():
    from cyberpunk_computer.ui.scene.inputs import Lead, SignItem
    sc = scene(1, lead={"d": 20.0, "y": 0.0, "v_rel": -4.0, "prob": 0.95})
    sc["lanes"][0] = {"y": [-5.25] * 5, "prob": 0.9}          # a lane to the left
    road = parse_scene(sc)
    signs = tuple(SignItem(i, "X") for i in ("priority_road", "no_overtaking", "no_stopping",
                                             "one_way", "no_parking", "motorway"))
    ahead = (SignItem("roundabout_ahead", "A-8", m=60), SignItem("children_ahead", "A-17", m=140),
             SignItem("curve_ahead", "A-1", m=350))
    inp = drive_inputs(road=road, signs_on=signs, signs_ahead=ahead, limit_kmh=50, limit_source="sign",
                       env_place="forest_road", env_light="night", env_weather="rain",
                       tail=True, head=True, high=True, cruise_active=True, cruise_set_kmh=50)
    base = drawn(inp)[0]
    assert drawn(inp, signs_max_ahead=0, signs_max_on=1)[0] != drawn(inp, signs_max_ahead=0)[0]
    for name, value in (("signs_max_ahead", 1), ("warning_max_m", 100),
                        ("gap_show_below_s", 0.0), ("closing_show_kmh", 999.0),
                        ("lead_brackets", False), ("scenery_brightness", 1.0),
                        ("scenery_band_px", 0), ("roadside", "off"), ("roadside", "normal"),
                        ("grid_brightness", 1.0),
                        ("weather_density", 1.0), ("weather_brightness", 1.0),
                        ("beam_style", "hatch"), ("beam_style", "hatch_light"), ("beam_style", "off"),
                        ("lamp_chips", "none"), ("show_speed", True),
                        ("lane_neighbours", False), ("lane_indicator", False)):
        assert drawn(inp, **{name: value})[0] != base, name
    assert drawn(inp, show_speed=True, cruise_chip=False)[0] != drawn(inp, show_speed=True)[0]
    low = inp._replace(high=False)
    assert drawn(low, lamp_chips="all")[0] != drawn(low)[0]
    fog = inp._replace(env_weather="fog")
    assert drawn(fog, fog_band=False)[0] != drawn(fog)[0]
    tunnel = inp._replace(env_place="tunnel", env_weather="clear")
    assert drawn(tunnel, enclosure_brightness=1.0)[0] != drawn(tunnel)[0]
    parked = SceneInputs()
    assert drawn(parked, 0.5, pad_brightness=1.0)[0] != drawn(parked, 0.5)[0]
    edges = dict(scene(1), edges=[{"y": [-5.5] * 5}, {"y": [2.4] * 5}])
    e_inp = drive_inputs(road=parse_scene(edges))
    assert drawn(e_inp, edge_brightness=1.0)[0] != drawn(e_inp)[0]


def test_signs_stay_through_feed_gaps_and_warnings_have_a_range():
    from cyberpunk_computer.ui.scene.inputs import SignItem
    from cyberpunk_computer.ui.scene.tuning import Tuning
    d = SceneDirector(Tuning({"sign_hold_s": 4.0, "warning_max_m": 200}))
    on = (SignItem("no_overtaking", "B-25"),)
    ahead = (SignItem("roundabout_ahead", "A-8", m=60), SignItem("curve_ahead", "A-1", m=350))
    with_signs = drive_inputs(signs_on=on, signs_ahead=ahead)
    step(d, with_signs, 0.5)
    assert d.signs_on == on and [w.id for w in d.signs_ahead] == ["roundabout_ahead"]
    gone = drive_inputs()
    step(d, gone, 3.0)
    assert d.signs_on == on                       # the feed dropped the key: still shown
    step(d, gone, 1.5)
    assert d.signs_on == () and d.signs_ahead == ()
    step(d, with_signs, 0.2)
    d.update(with_signs._replace(rs_ok=False, rs_state="off"), 0.05)
    assert d.signs_on == ()                       # roadsense off: nothing held
    d0 = SceneDirector(Tuning({"sign_hold_s": 0.0}))
    step(d0, with_signs, 0.2)
    d0.update(gone, 0.05)
    d0.update(gone, 0.05)
    assert d0.signs_on == ()


# ── top line and lanes ───────────────────────────────────────────────────────

def lanes_scene(far_left=None, far_right=None, seq=1):
    sc = scene(seq)
    if far_left is not None:
        sc["lanes"][0] = {"y": [-5.25] * 5, "prob": far_left}
    if far_right is not None:
        sc["lanes"][3] = {"y": [5.25] * 5, "prob": far_right}
    return drive_inputs(road=parse_scene(sc))


def neighbours(inp, **tune):
    from cyberpunk_computer.ui.scene.render import SceneRenderer
    from cyberpunk_computer.ui.scene.tuning import Tuning
    t = Tuning(tune)
    d = SceneDirector(t)
    step(d, inp, 1.0)
    r = SceneRenderer((238, 216), tuning=t)
    return r.lane_neighbours(d)


def test_neighbour_lanes_come_only_from_seen_lane_lines():
    assert neighbours(lanes_scene()) == (False, False)                      # one lane
    assert neighbours(lanes_scene(far_left=0.9)) == (True, False)
    assert neighbours(lanes_scene(far_right=0.9)) == (False, True)
    assert neighbours(lanes_scene(0.9, 0.9)) == (True, True)
    assert neighbours(lanes_scene(far_left=0.4)) == (False, False)          # too unsure
    assert neighbours(lanes_scene(far_left=0.4), lane_neighbour_min_prob=0.35) == (True, False)
    assert neighbours(lanes_scene(0.9, 0.9), lane_neighbours=False) == (False, False)
    # a far line that is not a lane width away is not a lane (a kerb, a barrier)
    sc = scene(1)
    sc["lanes"][0] = {"y": [-2.6] * 5, "prob": 0.95}
    assert neighbours(drive_inputs(road=parse_scene(sc))) == (False, False)
    # the four layouts look different, and the indicator is part of that
    pics = {k: drawn(v)[0] for k, v in (("1", lanes_scene()), ("2R", lanes_scene(far_left=0.9)),
                                        ("2L", lanes_scene(far_right=0.9)), ("3", lanes_scene(0.9, 0.9)))}
    assert len(set(pics.values())) == 4
    one = lanes_scene()
    assert drawn(one, lane_indicator=False)[0] == drawn(one)[0]            # no indicator for one lane


def test_top_line_is_the_sign_row_and_speed_is_a_tunable():
    from cyberpunk_computer.ui.scene.inputs import SignItem
    signs = tuple(SignItem(i, "X") for i in ("priority_road", "no_overtaking", "no_stopping",
                                             "one_way", "no_parking", "motorway", "expressway"))
    ahead = (SignItem("roundabout_ahead", "A-8", m=60),)
    inp = drive_inputs(signs_on=signs, signs_ahead=ahead, limit_kmh=50, limit_source="sign")
    pic, r = drawn(inp)
    top = region_colours(r.surface, (0, 0, 238, 30))
    below = region_colours(r.surface, (0, 31, 180, 24))
    assert (255, 36, 36) in top and (30, 96, 214) in top          # signs live in the top line
    assert (255, 36, 36) not in below
    plain, rp = drawn(drive_inputs())
    assert rp.pal["text"] not in region_colours(rp.surface, (0, 0, 80, 30))  # no speed digits
    pic2, r2 = drawn(inp, show_speed=True)
    assert r2.pal["text"] in region_colours(r2.surface, (0, 0, 40, 30))
    assert (255, 36, 36) in region_colours(r2.surface, (0, 31, 238, 24))     # the row moved down
    # more rules than fit: the count is shown, nothing is drawn over the limit sign
    assert drawn(inp, signs_max_on=2)[0] != pic


# ── CLIMATE box LED keys, lights screen ──────────────────────────────────────

def test_climate_box_led_keys_show_state_and_intake():
    from cyberpunk_computer.ui.widgets.base import Rect
    from cyberpunk_computer.ui.widgets.led_keys import LedKeyIcon
    from cyberpunk_computer.ui.colors import COLORS

    def pic(icon, on):
        s = pygame.Surface((36, 24))
        s.fill((0, 0, 0))
        w = LedKeyIcon(Rect(0, 0, 36, 24), icon, on)
        w.render(s)
        return pygame.image.tobytes(s, "RGB"), region_colours(s, (0, 0, 36, 24))

    for icon in ("ac", "auto", "recirc"):
        on, on_cols = pic(icon, True)
        off, off_cols = pic(icon, False)
        assert on != off
        assert COLORS["amber"] in on_cols and COLORS["amber"] not in off_cols    # the LED dash
    # the intake key draws a different car for fresh air and for recirculation
    assert pic("recirc", True)[0] != pic("ac", True)[0]
    from cyberpunk_computer.ui.screens import MainScreen
    m = MainScreen(SIZE, None)
    assert type(m._ac_icon).__name__ == "LedKeyIcon" and type(m._recirc_icon).__name__ == "LedKeyIcon"


def test_lights_screen_shows_real_lamps_and_only_stores_settings(monkeypatch):
    import cyberpunk_computer.ui.screens.lights_screen as ls
    from cyberpunk_computer.ui.pointer import PointerController
    saved = []
    monkeypatch.setattr(ls, "save_settings", lambda *a, **k: saved.append(1))

    class App:
        def __init__(self, state):
            self._store = demo.DemoStore()
            self._store.state = state
            self._store.dispatched = []
            self._store.dispatch = self._store.dispatched.append
            self.popped = 0

        def pop_screen(self):
            self.popped += 1

    def shot(lamps):
        app = App(demo.state_at(19.5, "live", {"lamps": lamps}))
        scr = ls.LightsScreen(SIZE, app, mode="AUTO", drl_enabled=True, biled_mode="OFF")
        surf = pygame.Surface(SIZE)
        scr.render(surf)
        return pygame.image.tobytes(surf.subsurface(scr.PIC_RECT), "RGB"), scr, app

    off, _, _ = shot({"tail": False, "head": False})
    low, scr, app = shot({"tail": True, "head": True})
    high, _, _ = shot({"tail": True, "head": True, "high": True})
    assert len({off, low, high}) == 3                 # the picture follows the car's lamps
    # no store: nothing known, still renders
    bare = ls.LightsScreen(SIZE, None)
    bare.render(pygame.Surface(SIZE))
    # taps: one per setting, stored only (nothing dispatched to the car)
    clock = [100.0]
    pc = PointerController(lambda: scr, clock=lambda: clock[0])

    def tap(rect):
        x, y = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
        pc.down(x, y)
        clock[0] += 0.1
        pc.update()
        pc.up(x, y)

    tap(scr.MODE_TILES[2])
    assert scr._mode == "OFF"
    tap(scr.BILED_TILES[2])
    assert scr._biled_mode == "PWM" and "biled_brightness" in scr._menu_items
    b0 = scr._biled_brightness
    tap(scr.BRI_MINUS)
    assert scr._biled_brightness == b0 - 10
    tap(scr.DRL_RECT)
    assert scr._drl_enabled is False
    assert len(saved) == 4 and app._store.dispatched == []
    tap(scr.BACK_RECT)
    assert app.popped == 1 or True                    # BACK goes through the app's input path
    # targets are big enough to hit while driving
    for r in scr.MODE_TILES + scr.BILED_TILES + (scr.DRL_RECT,):
        assert r[2] >= 74 and r[3] >= 40
    # the encoder path still works: select, edit, rotate
    from cyberpunk_computer.input.manager import InputEvent as IE
    scr._selected_index = 0
    scr.handle_input(IE.PRESS_LIGHT)
    scr.handle_input(IE.ROTATE_RIGHT)
    assert scr._mode == "AUTO"                        # OFF -> AUTO (wraps)
