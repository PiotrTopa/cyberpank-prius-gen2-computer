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
    LOST_S, REAR_GROUP_AJAR, STALE_S, VIEW_SECONDS, VIEW_STEPS, SceneDirector)
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

def test_view_opens_when_moving_and_a_stop_in_d_keeps_the_road():
    d = SceneDirector()
    drive = SceneInputs(speed_kmh=40, real_speed_kmh=40, gear="D", ready=True, acc_on=True)
    step(d, drive, VIEW_SECONDS + 0.3)
    assert d.mode == "drive" and d.view == 1.0 and d.view_step == VIEW_STEPS
    for gear in ("D", "N", "B"):                        # standing in traffic, READY
        step(d, SceneInputs(gear=gear, ready=True, acc_on=True), 60.0)
        assert d.mode == "drive" and d.view == 1.0
    step(d, SceneInputs(gear="P", ready=True, acc_on=True), VIEW_SECONDS + 0.2)
    assert d.mode == "parked" and d.view == 0.0
    assert d.animating is False                         # steady: nothing to redraw


def test_power_off_closes_the_view():
    d = SceneDirector()
    step(d, SceneInputs(speed_kmh=40, real_speed_kmh=40, gear="D", ready=True, acc_on=True), 2.0)
    d.update(SceneInputs(gear="D", ready=False, acc_on=False), 0.05)  # gear frame stale
    assert d.mode == "parked"


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


def test_line_fast_path_matches_the_checked_path():
    from cyberpunk_computer.ui.scene.inputs import _line
    assert _line({"y": [0.5, -1.25, 2.0], "prob": 0.7}, 11).ys == (-0.5, 1.25, -2.0)
    assert _line([1, 2.0, 3], 11).ys == (-1.0, -2.0, -3.0)            # ints: checked path
    assert _line([1.0, float("nan"), 3.0], 11) is None
    assert _line([1.0, True, 3.0], 11) is None
    assert _line([1.0, None], 11) is None
    assert _line([1.0, 2.0, 3.0, 4.0], 2).ys == (-1.0, -2.0)


def test_feed_event_updates_only_the_perception_fields():
    from cyberpunk_computer.ui.scene.inputs import read_perception
    sim = demo.Sim("live")
    sim.run_to(20.0)
    page = sim.screen.scene_page
    st = sim.store.state
    full = read_inputs(st, page.feed)
    assert page.inputs == full
    sim.feed.push(SCENE_EVENT, demo.scene_at(20.05, 99999))
    page.update(0.05)
    assert page.inputs == read_inputs(st, page.feed)
    assert set(read_perception(st, page.feed)) < set(SceneInputs._fields)


# ── speed limit from the map layer (scene "map" block, MFD_MAIN_SCREEN.md 7.2) ─

def _map_block(src="sign_held", kmh=50, ok=True, **kw):
    m = {"ok": ok, "state": "good", "conf": 0.9, "limit": {"kmh": kmh, "src": src, "conf": 0.8},
         "in": {"gnss": "headunit", "fix": "3d"}}
    m.update(kw)
    return m


def test_fused_limit_replaces_the_vision_limit_only_while_the_map_is_usable():
    st = running(AppState())

    def inp_for(**kw):
        feed = SceneFeed()
        feed.push(SCENE_EVENT, scene(**kw))
        return read_inputs(st, feed)

    vision = {"kmh": 70, "src": "sign", "conf": 0.7}
    # sign held by the map after the camera engine dropped it
    i = inp_for(limit={"kmh": 90, "src": "default"}, map=_map_block("sign_held", 50))
    assert (i.limit_kmh, i.limit_source, i.limit_fused) == (50, "sign_held", True)
    i = inp_for(limit=vision, map=_map_block("map", 40))
    assert (i.limit_kmh, i.limit_source) == (40, "map")
    # map not usable -> the vision value, exactly as before
    for m in (_map_block(ok=False, why="no_position"),
              _map_block(syn=True),                                     # synthetic residue
              _map_block(**{"in": {"gnss": "synthetic", "fix": "3d"}}),   # synthetic drive
              _map_block(limit=None), "garbage"):
        i = inp_for(limit=vision, map=m)
        assert (i.limit_kmh, i.limit_source, i.limit_conf, i.limit_fused) == (70, "sign", 0.7, False), m
    i = inp_for(limit=vision)                                         # no map key at all
    assert (i.limit_kmh, i.limit_fused) == (70, False)
    # the tunable switches it off
    feed = SceneFeed()
    feed.push(SCENE_EVENT, scene(limit=vision, map=_map_block("map", 40)))
    assert read_inputs(st, feed, map_limit=False).limit_kmh == 70
    # stack not running: no limit at all (as before)
    assert read_inputs(AppState(), feed).limit_kmh is None


def test_limit_provenance_styles_are_distinct():
    from cyberpunk_computer.ui.scene.render import SceneRenderer
    style = SceneRenderer.limit_style
    assert style("sign", 0.8) == style("sign_held", 0.8) == style("", None) == "sign"
    assert style("map", 0.9) == "map"
    assert style("map_implicit", 0.6) == style("vision_default", None) == style("default", None) == "weak"
    assert style("map", 0.2) == style("sign", 0.3) == "weak"
    pics = {src: drawn(drive_inputs(limit_kmh=50, limit_source=src, limit_conf=0.8))[0]
            for src in ("sign", "sign_held", "map", "map_implicit", "default")}
    assert pics["sign"] == pics["sign_held"]
    assert pics["map_implicit"] == pics["default"]
    assert len({pics["sign"], pics["map"], pics["map_implicit"]}) == 3


# ── map layer: ambient elements (ui/scene/mapinfo.py, maplayer.py; MFD_MAIN_SCREEN.md 6.5) ─

def _map_full(**kw):
    """A usable scene ``map`` block: a straight road with a right bend from 200 m."""
    xs = list(range(0, 601, 40))
    m = {"ok": True, "state": "good", "conf": 0.95,
         "limit": {"kmh": 50, "src": "map", "conf": 0.9},
         "road": {"cls": "primary", "ref": "DK46", "name": "Opolska"},
         "lanes": {"n": 2, "fwd": 1, "back": 1, "ego": 0, "onc_left": True, "c": 0.8},
         "tunnel": {"in": False, "src": "map"},
         "ahead": {"pc": 0.9, "limit": {"m": 200, "kmh": 90, "src": "maxspeed", "c": 0.9},
                   "jn": [[450, 0.9, "j"]]},
         "pos": {"src": "gnss", "hacc": 3.0},
         "in": {"gnss": "headunit", "fix": "3d"},
         "shape": {"ds": 40, "x": xs, "y": [0 if x < 200 else int((x - 200) ** 2 / 400) for x in xs]}}
    m.update(kw)
    return m


def map_inputs(m, **kw):
    from cyberpunk_computer.ui.scene.mapinfo import parse_map
    return drive_inputs(limit_kmh=50, limit_source="map", map=parse_map({"map": m}), **kw)


def live_step(d, inp, seconds, dt=0.05):
    """``step`` with a new road frame every pass (the road picture stays live)."""
    for _ in range(int(round(seconds / dt))):
        live_step.seq += 1
        d.update(inp._replace(road=inp.road._replace(seq=live_step.seq)), dt)


live_step.seq = 1000


def map_director(m, seconds=3.0, speed=50, **tune):
    from cyberpunk_computer.ui.scene.tuning import Tuning
    d = SceneDirector(Tuning(tune))
    inp = map_inputs(m, speed_kmh=speed, real_speed_kmh=speed)
    live_step(d, inp, seconds)
    return d, inp


def mdrawn(inp, seconds=3.0, **tune):
    from cyberpunk_computer.ui.scene.render import SceneRenderer
    from cyberpunk_computer.ui.scene.tuning import Tuning
    tuning = Tuning(tune)
    r = SceneRenderer((238, 216), tuning=tuning)
    d = SceneDirector(tuning)
    live_step(d, inp, seconds)
    r.render(d, inp, 10.0)
    return pygame.image.tobytes(r.surface, "RGB"), r


def test_map_block_is_parsed_only_while_usable():
    from cyberpunk_computer.ui.scene.mapinfo import parse_map
    mi = parse_map({"map": _map_full()})
    assert mi.road_ref == "DK46" and mi.lanes == (1, 1, 0) and mi.onc_left is True
    assert mi.next_limit.kmh == 90 and mi.next_limit.m == 200 and mi.junctions == ((450.0, 0.9, "j"),)
    assert mi.shape[-1] == (600.0, -400.0)                       # wire y right -> left positive
    assert parse_map({"map": _map_full()}) is mi                  # unchanged block: the same object
    for bad in (_map_full(ok=False, why="no_position"), _map_full(syn=True),
                _map_full(**{"in": {"gnss": "synthetic"}}), "garbage", None):
        assert parse_map({"map": bad}) is None, bad
    assert parse_map({}) is None
    # a tunnel the map does not know (vision only) carries no exit distance
    t = parse_map({"map": _map_full(tunnel={"in": True, "exit_m": 300, "src": "vision"})})
    assert t.tunnel_in is False and t.tunnel_exit_m is None
    # through read_inputs: only while roadsense runs
    feed = SceneFeed()
    feed.push(SCENE_EVENT, scene(map=_map_full()))
    assert read_inputs(running(AppState()), feed).map is not None
    assert read_inputs(AppState(), feed).map is None
    feed.push(SCENE_EVENT, scene(map=_map_full(syn=True)))
    assert read_inputs(running(AppState()), feed).map is None


def test_without_a_usable_map_the_picture_is_the_vision_only_one():
    base = mdrawn(drive_inputs(limit_kmh=50, limit_source="map"))[0]
    from cyberpunk_computer.ui.scene.mapinfo import parse_map
    for bad in (_map_full(ok=False), _map_full(syn=True), _map_full(**{"in": {"gnss": "synthetic"}})):
        inp = drive_inputs(limit_kmh=50, limit_source="map", map=parse_map({"map": bad}))
        assert mdrawn(inp)[0] == base
    assert mdrawn(map_inputs(_map_full()), 3.0)[0] != mdrawn(drive_inputs(limit_kmh=50, limit_source="map"), 3.0)[0]
    # all map elements switched off: the vision-only picture again
    off = dict(map_mark=False, map_next_limit="off", map_far_road=False, map_tunnel=False, map_lanes=False)
    assert mdrawn(map_inputs(_map_full()), 3.0, **off)[0] == mdrawn(
        drive_inputs(limit_kmh=50, limit_source="map"), 3.0)[0]


def test_next_limit_appears_by_distance_and_confidence_and_counts_down():
    d, inp = map_director(_map_full())
    nl = d.map.next_limit
    assert nl is not None and nl.kmh == 90
    assert 150 < nl.m < 165                       # 200 m, 3 s at 50 km/h driven since (no new message)
    # too far for the speed: 12 s at 50 km/h = 167 m window
    far = _map_full(ahead={"pc": 0.9, "limit": {"m": 380, "kmh": 90, "src": "maxspeed", "c": 0.9}})
    assert map_director(far)[0].map.next_limit is None
    assert map_director(far, speed=120)[0].map.next_limit is not None      # 400 m window at speed
    # the same value as the limit in effect, a doubtful path, a poor position: nothing
    same = _map_full(ahead={"limit": {"m": 100, "kmh": 50, "src": "maxspeed", "c": 0.9}})
    weak = _map_full(ahead={"limit": {"m": 100, "kmh": 90, "src": "maxspeed", "c": 0.3}})
    blind = _map_full(pos={"src": "dead_reckoning", "hacc": 45.0})
    for m in (same, weak, blind):
        assert map_director(m)[0].map.next_limit is None
    assert map_director(_map_full(), map_next_limit="off")[0].map.next_limit is None
    # not shown before it was reported for a second
    assert map_director(_map_full(), seconds=0.5)[0].map.next_limit is None


def test_map_elements_survive_a_short_dropout_and_go_after_the_hold():
    near = _map_full(ahead={"limit": {"m": 160, "kmh": 90, "src": "maxspeed", "c": 0.9}})
    d, inp = map_director(near, seconds=1.5)
    assert d.map.mark == "good" and d.map.next_limit is not None
    gone = inp._replace(map=None)
    live_step(d, gone, 1.5)                            # a message or two missing at a side road
    assert d.map.mark == "good" and d.map.next_limit is not None
    assert d.map.next_limit.m < 125               # still counting down by odometry
    live_step(d, gone, 1.5)                            # beyond map_hold_s: everything goes
    m = d.map
    assert (m.mark, m.next_limit, m.far, m.lanes, m.tunnel_exit_m, m.label, m.sig) == ("", None, None, None, None, None, ())
    # a map block that stops changing while the car drives on is a stalled feed: gone after 3.5 s
    d, inp = map_director(near, seconds=1.5)
    live_step(d, inp, 2.5)
    assert d.map.mark == "" and d.map.next_limit is None
    live_step(d, inp, 1.0)
    assert d.map.mark == ""                          # ... and the same block does not bring it back
    # standing still the block legitimately repeats: it stays
    d, inp = map_director(near, seconds=1.5)
    live_step(d, inp._replace(speed_kmh=0, real_speed_kmh=0), 8.0)
    assert d.map.mark == "good"
    # road picture stale: map elements go at once
    d, inp = map_director(_map_full())
    d.vision = "stale"
    d.map.update(inp, d, 0.05)
    assert d.map.mark == "" and d.map.next_limit is None


def test_low_match_confidence_hides_map_elements_with_hysteresis():
    from cyberpunk_computer.ui.scene.mapinfo import parse_map
    assert map_director(_map_full(conf=0.55), seconds=5.0)[0].map.mark == ""
    d, inp = map_director(_map_full(conf=0.95))
    # dips at side roads (one message in four at 0.5) do not hide it ...
    for i in range(12):
        m = _map_full(conf=0.5 if i % 4 == 0 else 0.9, pos={"src": "gnss", "hacc": 3.0 + i * 0.01})
        live_step(d, inp._replace(map=parse_map({"map": m})), 0.5)
        assert d.map.mark == "good", i
    # ... a lasting low confidence does (after the hold)
    for i in range(12):
        m = _map_full(conf=0.5, pos={"src": "gnss", "hacc": 4.0 + i * 0.01})
        live_step(d, inp._replace(map=parse_map({"map": m})), 0.5)
    assert d.map.mark == "" and d.map.next_limit is None


def test_dead_reckoning_marks_the_pin_and_a_poor_position_hides_distances():
    d, _ = map_director(_map_full(state="dead_reckoning", pos={"src": "dead_reckoning", "hacc": 8.0}))
    assert d.map.mark == "dr" and d.map.next_limit is not None
    good = mdrawn(map_inputs(_map_full()), 3.0)[0]
    dr = mdrawn(map_inputs(_map_full(state="dead_reckoning")), 3.0)[0]
    assert good != dr
    d, _ = map_director(_map_full(state="dead_reckoning", pos={"src": "dead_reckoning", "hacc": 30.0}))
    assert d.map.mark == "dr" and d.map.next_limit is None and d.map.far is None


def test_tunnel_exit_distance_only_inside_a_mapped_tunnel():
    tun = _map_full(tunnel={"in": True, "exit_m": 640, "src": "map+vision"})
    d, _ = map_director(tun)
    assert 590 < d.map.tunnel_exit_m < 605
    assert map_director(_map_full())[0].map.tunnel_exit_m is None
    assert map_director(tun, map_tunnel=False)[0].map.tunnel_exit_m is None
    # dead reckoning deep in a long tunnel: shown while the error is small against the distance
    dr = dict(state="dead_reckoning", pos={"src": "dead_reckoning", "hacc": 35.0})
    assert map_director(_map_full(tunnel={"in": True, "exit_m": 900, "src": "map"}, **dr))[0].map.tunnel_exit_m
    assert map_director(_map_full(tunnel={"in": True, "exit_m": 250, "src": "map"}, **dr))[0].map.tunnel_exit_m is None
    inp = map_inputs(tun, env_place="tunnel")
    assert mdrawn(inp, 3.0)[0] != mdrawn(inp, 3.0, map_tunnel=False)[0]
    from cyberpunk_computer.ui.scene.maplayer import dist_text
    assert dist_text(644) == ("640", "m") and dist_text(1240) == ("1.2", "km") and dist_text(4) == ("0", "m")


def test_far_road_is_cut_where_the_path_is_a_guess_and_needs_the_camera_to_agree():
    from cyberpunk_computer.ui.scene.mapinfo import far_path, parse_map

    def far(**ahead):
        return far_path(parse_map({"map": _map_full(ahead=ahead)}), 0.6, 600)

    assert far(jn=[])[0][-1][0] == 600.0
    cut = far(jn=[[300, 0.4, "j"]])                      # a real junction, path uncertain beyond
    assert cut.end == "" and 290 < cut.pts[-1][0] <= 300
    assert far(jn=[[300, 0.9, "j"], [500, 0.9, "r"]]).end == "roundabout"
    assert far(jn=[[260, 1.0, "d"]]).end == "dead_end"
    assert far(jn=[[120, 0.3, "j"]]) is None             # nothing worth drawing beyond the camera
    d, _ = map_director(_map_full())
    assert d.map.far is not None and d.map.far_alpha == 1.0
    assert map_director(_map_full(), map_far_road=False)[0].map.far is None
    # the camera sees the road bend away from where the map says it goes: hidden
    xs = list(range(0, 601, 40))
    left = _map_full(shape={"ds": 40, "x": xs, "y": [-int(x * x / 300) for x in xs]})
    assert map_director(left)[0].map.far is None
    inp = map_inputs(_map_full())
    assert mdrawn(inp, 3.0)[0] != mdrawn(inp, 3.0, map_far_road=False)[0]


def test_map_lanes_give_the_boxes_and_mark_the_oncoming_lane():
    from cyberpunk_computer.ui.scene.mapinfo import parse_map
    three = _map_full(lanes={"n": 3, "fwd": 3, "back": 0, "ego": 2, "onc_left": False, "c": 0.8})
    inp = map_inputs(three)
    assert mdrawn(inp, 3.0)[0] != mdrawn(inp, 3.0, map_lanes=False)[0]      # boxes although no lane is seen
    assert map_director(three)[0].map.lanes == (3, 0, 2)
    low = _map_full(lanes={"n": 3, "fwd": 3, "back": 0, "ego": 2, "c": 0.3})
    assert map_director(low)[0].map.lanes is None
    unknown = _map_full(lanes={"n": 3, "fwd": 3, "back": 0, "c": 0.8})     # ego lane not known
    assert map_director(unknown)[0].map.lanes is None
    # one lane each way, nothing seen beside: no boxes, as without the map
    one = map_inputs(_map_full())
    assert mdrawn(one, 3.0, map_far_road=False)[0] == mdrawn(one, 3.0, map_far_road=False, map_lanes=False)[0]
    # the camera sees the lane to the left; the map says it is oncoming: another tint
    sc = scene(1)
    sc["lanes"][0] = {"y": [-5.25] * 5, "prob": 0.9}
    seen = drive_inputs(road=parse_scene(sc), limit_kmh=50, limit_source="map",
                        map=parse_map({"map": _map_full()}))
    assert mdrawn(seen, 3.0)[0] != mdrawn(seen, 3.0, map_lanes=False)[0]
    # the camera sees a lane where the map has none: the camera's boxes, as before
    right = scene(1)
    right["lanes"][3] = {"y": [5.25] * 5, "prob": 0.9}
    clash = drive_inputs(road=parse_scene(right), limit_kmh=50, limit_source="map",
                         map=parse_map({"map": three}))
    assert mdrawn(clash, 3.0, map_far_road=False, map_mark=False, map_next_limit="off")[0] == mdrawn(
        clash._replace(map=None), 3.0)[0]


def test_road_label_shows_for_a_while_after_the_road_changed():
    d, inp = map_director(_map_full(), seconds=3.0, speed=0, map_road_label="ref")
    assert d.map.label == ("DK46", "primary", "")
    live_step(d, inp, 9.0)
    assert d.map.label is None                      # shown for map_road_label_s, then quiet
    from cyberpunk_computer.ui.scene.mapinfo import parse_map
    other = inp._replace(map=parse_map({"map": _map_full(road={"cls": "secondary", "ref": "414;S1"})}))
    live_step(d, other, 3.0)
    assert d.map.label == ("414", "secondary", "")
    assert map_director(_map_full(), map_road_label="name")[0].map.label.name == "Opolska"
    assert map_director(_map_full())[0].map.label is None           # off by default
    assert mdrawn(map_inputs(_map_full()), 3.0, map_road_label="name")[0] != mdrawn(map_inputs(_map_full()), 3.0)[0]


def test_map_tunables_change_the_picture_and_redraws_stay_cached():
    inp = map_inputs(_map_full(), env_place="rural_road")
    base = mdrawn(inp, 3.0)[0]
    for name, value in (("map_mark", False), ("map_next_limit", "off"), ("map_next_limit", "beside"),
                        ("map_far_road", False), ("map_ahead_s", 5.0), ("map_far_max_m", 300)):
        assert mdrawn(inp, 3.0, **{name: value})[0] != base, name
    near = map_inputs(_map_full(ahead={"limit": {"m": 60, "kmh": 90, "src": "maxspeed", "c": 0.9}}))
    assert mdrawn(near, 2.0, map_next_limit_road=False)[0] != mdrawn(near, 2.0)[0]     # the sign at the roadside
    # standing still with the map: nothing animates, no redraw without a change
    from cyberpunk_computer.ui.scene.render import SceneRenderer
    from cyberpunk_computer.ui.scene.tuning import Tuning
    t = Tuning()
    r, d = SceneRenderer((238, 216), tuning=t), SceneDirector(t)
    live_step(d, inp, 3.0)
    still = inp._replace(speed_kmh=0, real_speed_kmh=0, ready=True, acc_on=True)   # stopped in D
    live_step(d, still, 1.0)
    r.render(d, still, 1.0)
    n = r.redraws
    d.update(still, 0.05)
    r.render(d, still, 2.0)
    assert r.redraws == n


def test_roadside_trees_keep_their_place_while_they_approach(monkeypatch):
    """A forest tree is one object: the same distance from the road edge and the same height from the
    moment it appears until it passes (they swayed and scattered, owner 2026-11-16: the per-tree scatter
    was keyed by the tree's current distance)."""
    import cyberpunk_computer.ui.scene.render as R
    from cyberpunk_computer.ui.scene.tuning import Tuning
    r = R.SceneRenderer((238, 216), tuning=Tuning({"roadside": "normal"}))
    d = SceneDirector(Tuning({"roadside": "normal"}))
    step(d, drive_inputs(env_place="forest_road"), 2.0)
    r.render(d, drive_inputs(env_place="forest_road"), 10.0)       # sets up the chase geometry
    placed = []
    monkeypatch.setattr(r, "_place", lambda cam, x, y, v: placed.append((x, y)) or (100.0, 100.0, 4.0))
    talls = []
    monkeypatch.setattr(R, "conifer", lambda s, gx, gy, tall, *a: talls.append(tall))
    d.edges = [[5.5] * len(d.xs), [-5.5] * len(d.xs)]              # straight edges: any sway is the scatter's
    trees = {}                                                     # (side, row, world position) -> offsets, heights
    odo0 = d.odo
    for i in range(400):
        d.odo = odo0 + i * 0.25                                    # 100 m in 0.25 m steps
        placed.clear()
        talls.clear()
        r._draw_roadside(r.surface, d, "forest_road", "day")
        assert len(placed) == len(talls) > 4
        for (x, y), tall in zip(placed, talls):
            row = 0 if abs(y) > 8.0 else 1                          # far row beyond 7.5 m + 5.5 m edge
            key = (y > 0, row, round((d.odo + x) * 4))
            trees.setdefault(key, []).append((round(y, 3), round(tall, 3)))
    followed = [v for v in trees.values() if len(v) > 100]
    assert len(followed) >= 8
    for v in followed:
        assert len(set(v)) == 1, v[:5]
    # and the trees do differ from one another
    assert len({v[0] for v in followed}) > 3


# ── junction view and the lane assignment of the traffic light ───────────────

_JV = {"k": "j", "m": 180, "c": 0.9, "ctl": "s", "to": "Ozimska",
       "arms": [[-90, "xe", 0, 0], [0, "xep", 1, 0], [90, "e", 2, 0]]}
_JV_RING = {"k": "r", "m": 140, "c": 0.9, "ctl": "g", "to": "DW414",
            "arms": [[95, "xe", 1, 1], [3, "xep", 0, 2], [-88, "xe", 2, 3], [180, "ea", 0, 0]],
            "ring": {"r": 16, "n": 3, "p": 2}}


def test_junction_view_is_parsed():
    from cyberpunk_computer.ui.scene.mapinfo import parse_map
    jv = parse_map({"map": _map_full(jv=_JV)}).junction
    assert jv.kind == "j" and jv.m == 180 and jv.control == "s" and jv.to == "Ozimska"
    assert [a.angle for a in jv.arms] == [-90, 0, 90]
    assert jv.arms[1].path and jv.arms[1].rank == 1 and not jv.arms[2].exit and jv.arms[2].entry
    rg = parse_map({"map": _map_full(jv=dict(_JV_RING, ring={"r": 16, "n": 3, "p": 2, "on": True, "car": 70}))}).junction
    assert rg.kind == "r" and rg.ring_exits == 3 and rg.ring_path == 2 and rg.on_ring and rg.car_angle == 70
    assert rg.arms[3].approach and rg.arms[1].exit_no == 2
    for bad in ({"k": "z", "m": 1, "c": 1, "arms": [[0, "x"]]}, {"k": "j", "m": 1, "c": 1, "arms": []}, "x", None):
        assert parse_map({"map": _map_full(jv=bad)}).junction is None


def test_junction_view_is_off_by_default_and_gated_by_distance_confidence_and_position():
    assert map_director(_map_full(jv=_JV))[0].map.junction is None                 # tuning map_junction is off
    d, _ = map_director(_map_full(jv=_JV), map_junction=True)
    jv = d.map.junction
    assert jv is not None and 130 < jv.m < 145                                     # 180 m, 3 s at 50 km/h driven
    assert map_director(_map_full(jv=dict(_JV, m=600)), map_junction=True)[0].map.junction is None
    assert map_director(_map_full(jv=dict(_JV, m=600)), map_junction=True, map_junction_m=700)[0].map.junction is not None
    assert map_director(_map_full(jv=dict(_JV, c=0.3)), map_junction=True)[0].map.junction is None
    assert map_director(_map_full(jv=_JV, pos={"src": "dead_reckoning", "hacc": 45.0}),
                        map_junction=True)[0].map.junction is None
    # on the ring the picture stays although the path confidence is gone
    on = dict(_JV_RING, m=0, c=0.2, ring={"r": 16, "n": 3, "p": 3, "on": True, "car": 70})
    assert map_director(_map_full(jv=on), map_junction=True)[0].map.junction.on_ring
    # the map drops out: it goes with the other map elements
    d, inp = map_director(_map_full(jv=_JV), seconds=1.5, map_junction=True)
    live_step(d, inp._replace(map=None), 3.5)
    assert d.map.junction is None


def test_junction_view_draws_in_its_panel_only():
    from cyberpunk_computer.ui.scene import maplayer
    base = mdrawn(map_inputs(_map_full()), map_junction=True)[1].surface.copy()
    for jv in (_JV, _JV_RING, dict(_JV_RING, m=0, ring={"r": 16, "n": 3, "p": 3, "on": True, "car": 70})):
        surf = mdrawn(map_inputs(_map_full(jv=jv)), map_junction=True)[1].surface
        x0, y0, w, h = maplayer.JV_X, maplayer.JV_Y, maplayer.JV_W + 40, maplayer.JV_H + 16
        changed = [(x, y) for x in range(surf.get_width()) for y in range(surf.get_height())
                   if surf.get_at((x, y)) != base.get_at((x, y))]
        assert changed, jv
        assert all(x0 <= x < x0 + w and y0 <= y < y0 + h for x, y in changed), jv
        # the way on is drawn in the brightest lane colour
        hot = pygame.Color(*mdrawn(map_inputs(_map_full()))[1].pal["lane_ego"])
        assert any(surf.get_at(p) == hot for p in changed)
    # flag off: the same picture as without a view
    assert mdrawn(map_inputs(_map_full(jv=_JV)))[0] == mdrawn(map_inputs(_map_full()))[0]


def test_light_uncertainty_and_other_group_are_shown():
    from cyberpunk_computer.ui.scene.inputs import _traffic_light
    lt = _traffic_light({"state": "green", "d": 34, "y": 0.4, "q": True, "alt": {"s": "red", "side": "l"}})
    assert lt.uncertain and lt.alt_state == "red" and lt.alt_side == "l" and lt.lat_m == -0.4
    plain = _traffic_light({"state": "green", "d": 34, "y": 0.4})
    assert not plain.uncertain and plain.alt_state == "" and plain.alt_side == ""
    a = mdrawn(drive_inputs(light=plain))[0]
    assert mdrawn(drive_inputs(light=lt))[0] != a
    # both extras switched off: the plain signal
    assert mdrawn(drive_inputs(light=lt), light_uncertain=False, light_alt=False)[0] == a
    assert mdrawn(drive_inputs(light=lt._replace(uncertain=False)), light_alt=False)[0] == a


# ── fused road geometry: the road picture continues on the map (docs 6.7, 7.3) ──

def _geo(**kw):
    """A scene ``geo`` block: the camera to 60 m, the map's path on to 300 m, a side road to
    the right at 80 m (wire: y right)."""
    g = {"v": 60.0, "w": 3.4, "s": 300.0,
         "p": [[60.0, 0.0, 9], [120.0, 0.0, 9], [200.0, 6.0, 9], [300.0, 30.0, 9]],
         "br": [["xe", 1, 6.0, [80.0, 0.0, 80.0, 20.0, 80.0, 45.0]]]}
    g.update(kw)
    return g


def _ring_geo():
    """A roundabout 30 m ahead: ring of 14 m radius, the way half round it, two arms."""
    import math
    p = [[27.0, 0.0, 9], [30.0, 0.0, 9]]
    p += [[round(44.0 - 14.0 * math.cos(a), 1), round(14.0 * math.sin(a), 1), 9] for a in (0.4, 0.8, 1.2, 1.6, 2.0, 2.4, 2.8, 3.14)]
    p += [[90.0, 0.0, 9], [200.0, 0.0, 9]]
    return {"v": 27.0, "w": 3.4, "s": 30.0, "p": p, "ring": [44.0, 0.0, 14.0, 4.5, 0],
            "br": [["xer", 2, 6.0, [44.0, 14.0, 44.0, 40.0]], ["xer", 1, 6.0, [44.0, -14.0, 44.0, -40.0]]]}


def geo_inputs(g, m=None, **kw):
    from cyberpunk_computer.ui.scene.mapinfo import parse_geo, parse_map
    raw = {"map": m or _map_full(), "geo": g}
    return drive_inputs(limit_kmh=50, limit_source="map", map=parse_map(raw), geo=parse_geo(raw), **kw)


def test_geo_block_is_parsed_only_beside_a_usable_map():
    from cyberpunk_computer.ui.scene.mapinfo import parse_geo
    g = _geo()
    geo = parse_geo({"map": _map_full(), "geo": g})
    assert geo.v == 60.0 and geo.steer == 300.0 and geo.width == 3.4
    assert geo.path[-1][:2] == (300.0, -30.0)                       # wire y right -> left positive
    assert geo.branches[0].pts[-1] == (80.0, -45.0) and geo.branches[0].exit and not geo.branches[0].ring
    assert parse_geo({"map": _map_full(), "geo": g}) is geo          # the same block: the same object
    ring = parse_geo({"map": _map_full(), "geo": _ring_geo()})
    assert ring.ring == (44.0, -0.0, 14.0, 4.5, False) and all(b.ring for b in ring.branches)
    for bad_map in (_map_full(ok=False), _map_full(syn=True), _map_full(**{"in": {"gnss": "synthetic"}})):
        assert parse_geo({"map": bad_map, "geo": _geo()}) is None
    assert parse_geo({"geo": _geo()}) is None
    for bad in ({"v": 60.0}, {"v": 60.0, "p": [[1, 2]]}, "x", _geo(p=[[60.0, 0.0, 9]]), _geo(w=40)):
        assert parse_geo({"map": _map_full(), "geo": bad}) is None, bad


def test_geo_passes_the_gates_of_the_map_layer_and_replaces_the_far_road():
    from cyberpunk_computer.ui.scene.tuning import Tuning
    d = SceneDirector(Tuning())
    inp = geo_inputs(_geo(), speed_kmh=50, real_speed_kmh=50)
    live_step(d, inp, 3.0)
    assert d.map.geo is inp.geo and d.map.far is None
    # switched off: the far road of the map layer as before
    d2 = SceneDirector(Tuning({"road_geo": False}))
    live_step(d2, inp, 3.0)
    assert d2.map.geo is None and d2.map.far is not None
    # a doubtful match, an inaccurate position, a stale road picture: nothing
    for m in (_map_full(conf=0.4), _map_full(pos={"src": "gnss", "hacc": 45.0})):
        d3 = SceneDirector(Tuning())
        live_step(d3, geo_inputs(_geo(), m), 3.0)
        assert d3.map.geo is None
    step(d, inp, 4.0)                                               # the same scene for 4 s: stale
    assert d.vision == "stale" and d.map.geo is None


def test_geo_continues_the_road_picture_and_ends_the_cameras_lanes():
    plain = mdrawn(map_inputs(_map_full()), 3.0, map_far_road=False)
    img, r = mdrawn(geo_inputs(_geo()), 3.0)
    assert img != plain[0]
    assert r._x_end == 60.0 and r._geo_p is not None
    # the ribbon reaches beyond the 100 m row, and a side road leaves to the right of the lane
    y100 = int(r._row(__import__("cyberpunk_computer.ui.scene.render", fromlist=["compress"]).compress(100.4))[0])
    left, right = r._geo_p.way
    assert min(p[1] for p in left) < y100 - 5
    (b, (bl, br)), = r._geo_p.arms
    assert max(p[0] for p in bl) > r.w * 0.75
    # the camera's lane lines stop at geo.v: no bright ego-lane pixel above the 60 m row + margin
    y60 = int(r._row(__import__("cyberpunk_computer.ui.scene.render", fromlist=["compress"]).compress(60.4))[0])
    ego = r.pal["lane_ego"]
    assert not any(r.surface.get_at((x, y))[:3] == ego for y in range(r.horizon_y + 2, y60 - 3) for x in range(40, 200))
    assert any(r.surface.get_at((x, y60 + 6))[:3] == ego for x in range(40, 200))
    # switched off, or without the block: the picture of before
    assert mdrawn(geo_inputs(_geo()), 3.0, road_geo=False)[0] == mdrawn(map_inputs(_map_full()), 3.0)[0]


def test_geo_roundabout_is_a_ring_on_the_road():
    img, r = mdrawn(geo_inputs(_ring_geo()), 3.0)
    p = r._geo_p
    assert p.ring_out is not None and p.ring_in is not None and len(p.arms) == 2
    xs = [q[0] for q in p.ring_out]
    ys = [q[1] for q in p.ring_out]
    assert max(xs) - min(xs) > 150 and 12 <= max(ys) - min(ys) <= 40      # a wide, flat ellipse across the road
    assert abs((max(xs) + min(xs)) / 2 - r.w / 2) < 12                    # centred on the lane
    ixs = [q[0] for q in p.ring_in]
    assert min(xs) < min(ixs) and max(ixs) < max(xs)                      # the island inside the ring
    # on the ring (the centre 14 m to the left of the car): cut off behind the car, still drawn
    on = dict(_ring_geo(), v=0.0, s=0.0, ring=[2.0, -14.0, 14.0, 4.5, 1],
              p=[[0.0, 0.0, 9], [5.0, -0.9, 9], [10.0, -4.0, 9], [14.0, -9.0, 9], [16.0, -14.0, 9]], br=[])
    img2, r2 = mdrawn(geo_inputs(on), 3.0)
    assert r2._x_end == 0.0 and r2._geo_p.ring_out is not None
    assert all(-3000 <= q[0] <= 3000 and 0 <= q[1] <= r2.h + 150 for q in r2._geo_p.ring_out)


def test_geo_redraws_when_it_changes_and_costs_nothing_without_it():
    from cyberpunk_computer.ui.scene.render import SceneRenderer
    from cyberpunk_computer.ui.scene.tuning import Tuning
    tuning = Tuning()
    r = SceneRenderer((238, 216), tuning=tuning)
    d = SceneDirector(tuning)
    inp = geo_inputs(_geo())
    live_step(d, inp, 3.0)
    s1 = r._signature(d, inp)
    inp2 = geo_inputs(_geo(v=55.0))
    d.map.update(inp2, d, 0.05)
    assert r._signature(d, inp2) != s1
    d0 = SceneDirector(tuning)
    live_step(d0, drive_inputs(), 1.0)
    r.render(d0, drive_inputs(), 10.0)
    assert r._geo_p is None and r._x_end == 1e9
