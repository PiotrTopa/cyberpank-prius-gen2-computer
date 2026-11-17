"""Fuel log MFD screen (headless pygame): summary, add flow, undo.

Run: SDL_VIDEODRIVER=dummy .venv-fe/bin/python -m pytest tests/test_fuel_log_screen.py
(skipped where pygame is not installed, e.g. the backend venv).
"""

import os
from dataclasses import replace

import pytest

pygame = pytest.importorskip("pygame")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["SDL_FBDEV"] = "/nonexistent/fb-for-tests"

from cyberpunk_computer.api.commands import build_command  # noqa: E402
from cyberpunk_computer.fuel_log.actions import FuelLogCommandAction  # noqa: E402
from cyberpunk_computer.fuel_log.service import FuelLogService  # noqa: E402
from cyberpunk_computer.fuel_log.store import FuelLogStore  # noqa: E402
from cyberpunk_computer.input.manager import InputEvent as IE  # noqa: E402
from cyberpunk_computer.state.store import Store  # noqa: E402

SIZE = (480, 240)


@pytest.fixture(scope="module", autouse=True)
def _pygame():
    from cyberpunk_computer.ui.fonts import FontManager
    pygame.init()
    pygame.display.set_mode(SIZE)
    FontManager._fonts.clear()
    yield


class App:
    class config:
        timeout_screen_exit = 30

    def __init__(self):
        self.pushed, self.popped = [], 0

    def push_screen(self, s):
        self.pushed.append(s)

    def pop_screen(self):
        self.popped += 1


class SpyStore(Store):
    def __init__(self):
        super().__init__()
        self.sent = []

    def dispatch(self, action):
        if isinstance(action, FuelLogCommandAction):
            self.sent.append(action)
        return super().dispatch(action)


@pytest.fixture
def store():
    st = SpyStore()
    st._state = replace(st._state, vehicle=replace(st._state.vehicle, odometer_km=300012.75))
    svc = FuelLogService(FuelLogStore(":memory:"), st)
    st.add_middleware(svc.middleware)
    st.dispatch(build_command("fuel_log_add", {"fuel": "lpg", "litres": 30, "price_per_l": 3.89,
                                               "odometer_km": 299600}))
    st.sent.clear()
    return st


def screen(store):
    from cyberpunk_computer.ui.screens.fuel_log_screen import FuelLogScreen
    return FuelLogScreen(SIZE, App(), store=store)


def render(s):
    surf = pygame.Surface(SIZE)
    s.render(surf)
    s.render_pointer_overlay(surf)
    return surf


def test_reachable_from_engine_menu(store):
    from cyberpunk_computer.ui.screens.engine_menu_screen import EngineMenuScreen
    from cyberpunk_computer.ui.screens.fuel_log_screen import FuelLogScreen
    app = App()
    m = EngineMenuScreen(SIZE, app, store=store)
    assert m._items[0].label == "FUEL LOG"
    m.handle_input(IE.PRESS_LIGHT)
    assert isinstance(app.pushed[-1], FuelLogScreen)
    render(m)
    # all seven rows end above the footer
    assert 26 + 6 + len(m._items) * (m.ITEM_HEIGHT + 3) <= 240 - 22 + 3


def test_add_by_amount_paid_defaults_to_last_price_and_full(store):
    s = screen(store)
    render(s)
    s.handle_input(IE.PRESS_LIGHT)              # ADD FILL
    assert s.mode == "add" and s.form["fuel"] == "lpg" and s.form["full"] is True
    assert s.form["by_total"] and s.form["price"] == 3.89
    # set paid amount to 92.21: edit row 2 digit by digit (100, 10, 1, 0.1, 0.01)
    s.form["total"] = 0.0
    s._selected_index = 2
    s.handle_input(IE.PRESS_LIGHT)
    s.handle_input(IE.PRESS_LIGHT)              # skip hundreds
    for _ in range(9):
        s.handle_input(IE.ROTATE_RIGHT)         # 90
    s.handle_input(IE.PRESS_LIGHT)
    for _ in range(2):
        s.handle_input(IE.ROTATE_RIGHT)         # 92
    s.handle_input(IE.PRESS_LIGHT)
    for _ in range(2):
        s.handle_input(IE.ROTATE_RIGHT)         # 92.2
    s.handle_input(IE.PRESS_LIGHT)
    s.handle_input(IE.ROTATE_RIGHT)             # 92.21
    render(s)
    s.handle_input(IE.PRESS_LIGHT)              # done -> next row (price)
    assert not s._editing and s._selected_index == 3 and s.form["total"] == 92.21
    s.handle_input(IE.PRESS_LIGHT)              # price 3.89 -> 3.99
    s.handle_input(IE.PRESS_LIGHT)
    s.handle_input(IE.ROTATE_RIGHT)
    s.handle_input(IE.PRESS_LIGHT)
    s.handle_input(IE.PRESS_LIGHT)
    assert s.form["price"] == 3.99 and s._selected_index == 4
    assert s.computed()["litres"] == 23.11
    s.handle_input(IE.ROTATE_RIGHT)             # SAVE
    s.handle_input(IE.PRESS_LIGHT)
    a = store.sent[-1]
    assert a.op == "add" and a.params == {"fuel": "lpg", "full": True, "total": 92.21, "price_per_l": 3.99}
    assert s.mode == "summary"
    fl = store.state.fuel_log
    assert fl.last_command["ok"] and fl.entries[0]["litres"] == 23.11
    assert s._status_line()[0].startswith("SAVED")
    render(s)


def test_litres_mode_partial_and_petrol(store):
    s = screen(store)
    s.open_add()
    s.handle_input(IE.PRESS_LIGHT)              # FUEL -> petrol
    assert s.form["fuel"] == "petrol"
    s._selected_index = 1
    s.handle_input(IE.PRESS_LIGHT)              # ENTER AS -> litres
    assert not s.form["by_total"]
    s._selected_index = 4
    s.handle_input(IE.PRESS_LIGHT)              # FULL -> no
    s._selected_index = 5
    render(s)
    s.handle_input(IE.PRESS_LIGHT)
    a = store.sent[-1]
    assert a.params["fuel"] == "petrol" and a.params["full"] is False and "litres" in a.params
    assert "price_per_l" not in a.params        # no petrol price known yet: optional


def test_undo_needs_confirmation_and_back_leaves(store):
    s = screen(store)
    s.handle_input(IE.ROTATE_RIGHT)
    s.handle_input(IE.PRESS_LIGHT)
    assert s._undo_armed and not store.sent
    render(s)
    s.handle_input(IE.PRESS_LIGHT)
    assert store.sent[-1].op == "undo" and store.state.fuel_log.count == 0
    s.handle_input(IE.BACK)
    assert s.app.popped == 1


def test_touch_keys_and_rows(store):
    s = screen(store)
    render(s)
    t = s.pointer_target_at(60, 240 - 22 - 15)
    s.pointer_click(t, 60, 240 - 22 - 15)       # ADD FILL key
    assert s.mode == "add"
    t = s.pointer_target_at(100, 26 + 3 + 4 * 23 + 5)   # FULL row
    s.pointer_click(t, 100, 26 + 3 + 4 * 23 + 5)
    assert s.form["full"] is False


def test_renders_without_a_backend_log():
    s = screen(Store())
    render(s)
    s.open_add()
    render(s)
