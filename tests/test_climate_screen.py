"""Climate submenu on live store data (headless pygame).

Run: SDL_VIDEODRIVER=dummy .venv-fe/bin/python -m pytest tests/test_climate_screen.py
(skipped where pygame is not installed, e.g. the backend venv).
"""

import os

import pytest

pygame = pytest.importorskip("pygame")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["SDL_FBDEV"] = "/nonexistent/fb-for-tests"

from cyberpunk_computer.input.manager import InputEvent as IE  # noqa: E402
from cyberpunk_computer.state.actions import (  # noqa: E402
    ClimateButtonAction, SetAvcClimateAction, SetOutsideTempAction,
)
from cyberpunk_computer.state.store import Store  # noqa: E402

SIZE = (480, 240)


@pytest.fixture(scope="module", autouse=True)
def _pygame():
    from cyberpunk_computer.ui.fonts import FontManager
    pygame.init()
    pygame.display.set_mode(SIZE)
    FontManager._fonts.clear()   # fonts cached before another module's pygame.quit() are dead
    yield


@pytest.fixture(autouse=True)
def _no_settings_writes(monkeypatch):
    import cyberpunk_computer.persistence as persistence
    monkeypatch.setattr(persistence.SettingsManager, "save", lambda self, *a, **k: True)


class SpyStore(Store):
    """Real reducers; records UI-sourced actions (the commands)."""

    def __init__(self):
        super().__init__()
        self.sent = []

    def dispatch(self, action):
        if getattr(action.source, "name", "") == "UI":
            self.sent.append(action)
        return super().dispatch(action)


def live_store():
    st = SpyStore()
    st.dispatch(SetAvcClimateAction(
        fan_speed=5, ac_on=True, auto_mode=False, recirculation=False, fresh_air=True,
        defrost=False, rear_defogger=True, air_direction="foot", system_on=True,
        target_temp=21.5))
    st.dispatch(SetOutsideTempAction(7.0))
    return st


def texts(cs):
    return {it.label: cs._display_text(it) for it in cs.items}


class App:
    def __init__(self):
        self.pushed = []

    def push_screen(self, s):
        self.pushed.append(s)

    def pop_screen(self):
        pass


def open_from_main(store):
    from cyberpunk_computer.ui.screens import MainScreen
    app = App()
    m = MainScreen(SIZE, app)
    m.set_store(store)
    m._on_climate_action()
    (cs,) = app.pushed
    return m, cs


def test_opens_with_real_values_and_stays_live():
    st = live_store()
    m, cs = open_from_main(st)
    t = texts(cs)
    assert t["TARGET TEMP"] == "21.5°C"
    assert t["FAN SPEED"] == "5"                  # was always 3 before
    assert t["AIR DIRECTION"] == "FEET"           # was always FACE before
    assert t["MODE"] == "MANUAL"
    assert t["REAR DEFOGGER"] == "ON" and t["FRONT DEFROST"] == "OFF"
    assert t["OUTSIDE TEMP"] == "7°C" and t["INSIDE TEMP"] == "--"
    # the car changes while the page is open -> the page follows
    st.dispatch(SetAvcClimateAction(fan_speed=2, auto_mode=True, defrost=True, target_temp=23.0))
    cs.update(0.05)
    t = texts(cs)
    assert t["FAN SPEED"] == "AUTO 2" and t["MODE"] == "AUTO"
    assert t["AIR DIRECTION"] == "DEFROST" and t["FRONT DEFROST"] == "ON"
    assert t["TARGET TEMP"] == "23°C"
    # main box shows half degrees
    st.dispatch(SetAvcClimateAction(target_temp=20.5))
    assert m._temp_target == "20.5"


def test_encoder_half_degree_steps_and_mode_off():
    st = live_store()
    _, cs = open_from_main(st)
    cs.handle_input(IE.PRESS_LIGHT)               # edit TARGET TEMP
    cs.handle_input(IE.ROTATE_RIGHT)
    cs.handle_input(IE.ROTATE_RIGHT)
    assert [a.temp for a in st.sent] == [22.0, 22.5]
    assert texts(cs)["TARGET TEMP"] == "22.5°C"
    cs.handle_input(IE.PRESS_LIGHT)
    cs.handle_input(IE.ROTATE_RIGHT)              # FAN
    cs.handle_input(IE.ROTATE_RIGHT)              # MODE
    cs.handle_input(IE.PRESS_LIGHT)
    cs.handle_input(IE.ROTATE_RIGHT)              # MANUAL -> OFF
    assert isinstance(st.sent[-1], ClimateButtonAction) and st.sent[-1].button == "off"
    assert texts(cs)["MODE"] == "OFF"


def test_defrost_and_defogger_rows_send_buttons():
    st = live_store()
    _, cs = open_from_main(st)
    labels = [it.label for it in cs.items]
    for label, button in (("FRONT DEFROST", "front_defrost"), ("REAR DEFOGGER", "rear_defogger")):
        cs._selected_index = labels.index(label)
        cs._editing = True
        cs.handle_input(IE.ROTATE_RIGHT)
        assert st.sent[-1].button == button
        cs._editing = False


def test_encoder_reaches_every_control_and_skips_the_readouts():
    _, cs = open_from_main(live_store())
    seen = []
    for _ in range(len(cs.items)):
        seen.append(cs.items[cs._selected_index].label)
        cs.handle_input(IE.ROTATE_RIGHT)
    assert seen[:8] == ["TARGET TEMP", "FAN SPEED", "MODE", "A/C", "AIR INTAKE", "AIR DIRECTION",
                        "FRONT DEFROST", "REAR DEFOGGER"]
    assert "OUTSIDE TEMP" not in seen and "INSIDE TEMP" not in seen     # read-outs, not stops
    assert seen[8] == "TARGET TEMP"                                      # wraps
    assert set(cs.FOCUS) == set(seen)                                    # each has a focus box
    cs.render(pygame.Surface(SIZE))


def _tap(cs, rect):
    from cyberpunk_computer.ui.pointer import PointerController
    clock = [50.0]
    pc = PointerController(lambda: cs, clock=lambda: clock[0])
    x, y = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
    pc.down(x, y)
    clock[0] += 0.1
    pc.update()
    pc.up(x, y)


def test_cabin_view_one_tap_per_function():
    st = live_store()
    _, cs = open_from_main(st)
    cs.controller._stale = lambda state: False          # a fresh link (toggles allowed)
    _tap(cs, cs.TEMP_UP)
    assert st.sent[-1].temp == 22.0
    _tap(cs, cs.TEMP_DOWN)
    _tap(cs, cs.TEMP_DOWN)
    assert st.sent[-1].temp == 21.0
    n = len(st.sent)
    _tap(cs, cs.FAN_UP)
    assert len(st.sent) == n + 1
    _tap(cs, cs.VENT_TILES[0])                           # FACE
    assert texts(cs)["AIR DIRECTION"] == "FACE"
    _tap(cs, cs.TILES["FRONT"])
    assert st.sent[-1].button == "front_defrost"
    _tap(cs, cs.TILES["REAR"])
    assert st.sent[-1].button == "rear_defogger"
    _tap(cs, cs.TILES["OFF"])
    assert st.sent[-1].button == "off" and texts(cs)["MODE"] == "OFF"
    _tap(cs, cs.TILES["AUTO"])
    assert texts(cs)["MODE"] == "AUTO"
    # every target is at least 44 px high and 55 px wide
    rects = [cs.TEMP_UP, cs.TEMP_DOWN, cs.FAN_UP, cs.FAN_DOWN] + list(cs.VENT_TILES) + list(cs.TILES.values())
    assert all(r[3] >= 44 and r[2] >= 55 for r in rects)


def test_cabin_picture_follows_the_reported_state():
    surf = pygame.Surface(SIZE)

    def pic(**kw):
        st = SpyStore()
        base = dict(fan_speed=4, ac_on=True, auto_mode=False, recirculation=False, fresh_air=True,
                    defrost=False, rear_defogger=False, air_direction="face", system_on=True,
                    target_temp=21.5)
        base.update(kw)
        st.dispatch(SetAvcClimateAction(**base))
        cs = __import__("cyberpunk_computer.ui.screens.climate_screen", fromlist=["x"]).ClimateScreen(
            SIZE, None, store=st)
        cs.controller._stale = lambda state: False
        cs.update(0.05)
        cs.render(surf)
        return pygame.image.tobytes(surf, "RGB")

    variants = [pic(), pic(air_direction="face_foot"), pic(air_direction="foot"),
                pic(air_direction="windscreen_foot"), pic(defrost=True), pic(rear_defogger=True),
                pic(recirculation=True, fresh_air=False), pic(ac_on=False),
                pic(system_on=False, fan_speed=0)]
    assert len(set(variants)) == len(variants)
    # no AVC status: no streams, no tile state, a NO AVC flag; still renders
    from cyberpunk_computer.ui.screens.climate_screen import ClimateScreen
    cs = ClimateScreen(SIZE, None, store=SpyStore())
    cs.update(0.05)
    cs.render(surf)
    snap = cs._snapshot()
    assert snap["known"] is False and snap["toggles_ok"] is False
    # no store at all (the legacy path) renders and takes taps
    legacy = ClimateScreen(SIZE, None, temp_target=21.5)
    legacy.render(surf)
    _tap(legacy, legacy.TEMP_UP)
    assert legacy.target_temp == 22.0
