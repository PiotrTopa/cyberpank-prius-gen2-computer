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
    SCENE_EVENT, SceneFeed, SceneInputs, parse_scene, read_inputs)
from cyberpunk_computer.ui.scene.model import Camera, get_model  # noqa: E402
from cyberpunk_computer.ui.scene.powerbox import PowerHistory  # noqa: E402

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
    for name, t, vision, stale_from, _ in demo.SHOTS:
        sim = demo.Sim(vision)
        sim.stale_from = stale_from
        sim.run_to(t)
        sim.frame(surf)
        assert len(region_colours(surf, (121, 1, 238, 168))) > 3, name
        assert len(region_colours(surf, (121, 170, 238, 54))) > 2, name


def test_no_lead_vehicle_without_lead_data_and_redraw_is_cached():
    from cyberpunk_computer.ui.scene.render import SceneRenderer
    r = SceneRenderer((238, 168))
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
    assert r.pal["tail"] in region_colours(r.surface, (80, 40, 90, 70))   # a drawn vehicle
    # parked and steady: repeated frames do not redraw the scene
    p = SceneDirector()
    parked = SceneInputs()
    step(p, parked, 1.0)
    r2 = SceneRenderer((238, 168))
    for i in range(10):
        p.update(parked, 0.05)
        r2.render(p, parked, 30.0 + i * 0.05)
    assert r2.redraws == 1 and r2.cache.renders == 1


def test_open_door_changes_the_picture_and_uses_the_warning_colour():
    from cyberpunk_computer.ui.scene.render import SceneRenderer
    r = SceneRenderer((238, 168))
    d = SceneDirector()
    closed = SceneInputs(doors=(False, False, None, None, None), rear_group=False)
    step(d, closed, 0.5)
    r.render(d, closed, 1.0)
    assert r.car.pal["open"] not in region_colours(r.surface, (0, 20, 238, 130))
    opened = closed._replace(doors=(True, False, None, None, None))
    step(d, opened, 1.0)
    r.render(d, opened, 5.0)
    assert r.car.pal["open"] in region_colours(r.surface, (0, 20, 238, 130))


def test_scene_page_survives_a_bare_main_screen():
    """No store attached (other headless tests build MainScreen that way)."""
    from cyberpunk_computer.ui.screens import MainScreen
    m = MainScreen(SIZE, None)
    surf = pygame.Surface(SIZE)
    m.update(0.05)
    m.render(surf)
    m.show_page(1)
    m.render(surf)


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
    r = SceneRenderer((238, 168))
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
