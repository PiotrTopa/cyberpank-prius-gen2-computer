"""Pointer model (mouse / MFD touch) — headless UI tests.

Run: SDL_VIDEODRIVER=dummy python -m pytest tests/test_pointer_ui.py
(the env defaults below make the plain ``python -m pytest`` work too).
"""

import json
import os
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
# Never let the renderer's direct-framebuffer fallback touch a real /dev/fb0.
os.environ["SDL_FBDEV"] = "/nonexistent/fb-for-tests"

import pygame  # noqa: E402
import pytest  # noqa: E402

from cyberpunk_computer.input.manager import InputEvent as IE  # noqa: E402
from cyberpunk_computer.ui.pointer import (  # noqa: E402
    LONG_PRESS_S,
    PointerController,
    TouchEventQueue,
)
from cyberpunk_computer.ui.hw_buttons import HW_BUTTON_TARGETS, handle_hw_button  # noqa: E402

DATA = Path(__file__).parent / "data"
SIZE = (480, 240)
FRAME_S = 1.0 / 21  # Pi frame period


@pytest.fixture(scope="module", autouse=True)
def _pygame():
    pygame.init()
    pygame.display.set_mode(SIZE)
    yield
    pygame.quit()


@pytest.fixture(autouse=True)
def _no_settings_writes(monkeypatch, tmp_path):
    """Screens persist settings on change; keep that out of the repo."""
    import cyberpunk_computer.persistence as persistence
    import cyberpunk_computer.ui.screens.main_screen as ms
    import cyberpunk_computer.ui.screens.lights_screen as ls
    import cyberpunk_computer.ui.screens.ambient_screen as ams
    for mod in (persistence, ms, ls, ams):
        if hasattr(mod, "save_settings"):
            monkeypatch.setattr(mod, "save_settings", lambda *a, **k: None)
    monkeypatch.setattr(persistence.SettingsManager, "save", lambda self, *a, **k: True)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeApp:
    """Minimal app: screen stack + global BACK, like core.app.Application."""

    def __init__(self, root=None):
        self.screen_stack = []
        self.current_screen = root
        self.config = None
        self.running = True

    def push_screen(self, screen):
        if self.current_screen:
            self.screen_stack.append(self.current_screen)
        self.current_screen = screen
        screen.on_enter()

    def pop_screen(self):
        if self.current_screen:
            self.current_screen.on_exit()
        if self.screen_stack:
            self.current_screen = self.screen_stack.pop()
            return True
        return False

    def inject_input(self, event):
        if event == IE.BACK:
            if not self.pop_screen():
                self.running = False
            return
        self.current_screen.handle_input(event)

    def go_home(self):
        while self.screen_stack:
            self.pop_screen()
        home = self.current_screen
        if hasattr(home, "reset_to_home"):
            home.reset_to_home()
        return home


def make(screen_getter, clock=None):
    clock = clock or Clock()
    return PointerController(screen_getter, clock=clock), clock


def tap(pc, clock, x, y, hold=0.1):
    pc.down(x, y)
    clock.t += hold
    pc.update()
    pc.up(x, y)
    return pc.last_gesture[0]


def long_press(pc, clock, x, y):
    pc.down(x, y)
    for _ in range(int(LONG_PRESS_S / FRAME_S) + 3):
        clock.t += FRAME_S
        pc.update()
    pc.up(x, y)
    return pc.last_gesture[0]


def center(rect):
    return rect.x + rect.width // 2, rect.y + rect.height // 2


# ── Main screen ──────────────────────────────────────────────────────────────

def main_screen(app=None):
    from cyberpunk_computer.ui.screens import MainScreen
    return MainScreen(SIZE, app)


def test_tap_frame_focuses_and_activates():
    m = main_screen()
    pc, clock = make(lambda: m)
    assert not m.focus_manager.focus_visible

    assert tap(pc, clock, *center(m._climate_frame.rect)) == "click"
    assert m.focus_manager.focused_widget is m._climate_frame
    assert m.focus_manager.focus_visible
    assert m._editing_target_temp            # PRESS_LIGHT = quick temp edit

    # Right half of the body = +1 (ROTATE_RIGHT), left half = -1
    t0 = int(m._temp_target)
    r = m._climate_frame.rect
    tap(pc, clock, r.right - 10, r.y + 50)
    assert int(m._temp_target) == t0 + 1
    tap(pc, clock, r.x + 10, r.y + 50)
    tap(pc, clock, r.x + 10, r.y + 50)
    assert int(m._temp_target) == t0 - 1
    # Title bar = done
    tap(pc, clock, r.x + 30, r.y + 5)
    assert not m._editing_target_temp


def test_tap_other_frame_ends_edit_and_activates_it():
    m = main_screen()
    pc, clock = make(lambda: m)
    tap(pc, clock, *center(m._audio_frame.rect))
    assert m._editing_volume
    tap(pc, clock, m._lights_frame.rect.x + 20, m._lights_frame.rect.y + 40)
    assert not m._editing_volume
    assert m._editing_lights
    assert m.focus_manager.focused_widget is m._lights_frame


def test_drag_volume_bar_sets_volume_and_throttles_dispatch():
    m = main_screen()

    class Store:
        actions = []

        def dispatch(self, a):
            self.actions.append(a)

        def subscribe(self, *a):
            return lambda: None

    m._store = Store()
    pc, clock = make(lambda: m)
    bar = m._volume_bar.rect
    y = bar.y + bar.height // 2
    pc.down(bar.x + 2, y)
    assert m._volume == 0
    for x in range(bar.x + 2, bar.right + 20, 5):
        pc.move(x, y)
    pc.up(bar.right + 20, y)
    assert pc.last_gesture[0] == "drag"        # never a click
    assert m._volume == 100
    assert m._volume_bar.value == 100
    assert m._volume_label.value == "100"
    assert not m._editing_volume               # drag does not enter edit mode
    # Throttled: far fewer commands than moves, final value flushed by update()
    m._volume_sent_t = 0.0
    m.update(0.05)
    sent = [a.volume for a in Store.actions]
    assert sent[-1] == 100
    assert len(sent) < 10


def test_long_press_frame_is_press_strong():
    opened = []
    app = FakeApp()
    m = main_screen(app)
    app.current_screen = m
    app.push_screen = lambda s: opened.append(type(s).__name__)
    pc, clock = make(lambda: m)
    assert long_press(pc, clock, *center(m._battery_frame.rect)) == "long"
    assert opened == ["BatteryScreen"]
    assert long_press(pc, clock, *center(m._vehicle_frame.rect)) == "long"
    assert opened[-1] == "EngineDetailScreen"
    # Short tap on ENGINE = PRESS_LIGHT = engine menu
    assert tap(pc, clock, *center(m._vehicle_frame.rect)) == "click"
    assert opened[-1] == "EngineMenuScreen"


def test_pagination_tap_and_center_swipe():
    m = main_screen()
    pc, clock = make(lambda: m)
    pag = m._pagination_control
    # dot 1 is the right one
    tap(pc, clock, pag.rect.right - 18, pag.rect.centery)
    assert m.current_page == 1
    tap(pc, clock, pag.rect.x + 18, pag.rect.centery)
    assert m.current_page == 0
    # swipe left in the centre = next page
    pc.down(300, 120)
    for x in (290, 270, 240, 210):
        pc.move(x, 121)
    pc.up(210, 121)
    assert m.current_page == 1
    # a plain tap in the centre does nothing
    assert tap(pc, clock, 240, 120) == "click"
    assert m.current_page == 1


def test_tap_outside_does_nothing_and_slide_off_cancels():
    m = main_screen()
    pc, clock = make(lambda: m)
    # Bottom bar left of the pagination dots: no target
    tap(pc, clock, 200, 235)
    assert pc.last_gesture == ("none", None)
    assert not m.focus_manager.focus_visible
    # Press on AUDIO, slide away, release elsewhere -> no activation
    pc.down(*center(m._audio_frame.rect))
    assert pc.feedback_visible
    pc.move(60, 120)
    pc.move(60, 150)
    pc.up(60, 150)
    assert pc.last_gesture[0] == "none"
    assert not m._editing_volume


# ── Sub screens ──────────────────────────────────────────────────────────────

def test_audio_screen_rows_edit_and_back_chip():
    from cyberpunk_computer.ui.screens import AudioScreen
    root = main_screen()
    app = FakeApp(root)
    a = AudioScreen(SIZE, app, initial_volume=20)
    changes = []
    a.set_on_value_changed(lambda label, v: changes.append((label, v)))
    app.push_screen(a)
    pc, clock = make(lambda: app.current_screen)

    row_y = lambda i: a.HEADER_HEIGHT + a.ITEM_PADDING + i * (a.ITEM_HEIGHT + a.ITEM_PADDING) + 10
    # Tap BASS row: selects and enters edit
    tap(pc, clock, 100, row_y(1))
    assert a._selected_index == 1 and a._editing
    # Tap the '>' end of the edited row: +1; value area left of it: -1
    right = a.SIDE_MARGIN + a.width - a.SIDE_MARGIN * 2
    tap(pc, clock, right - 10, row_y(1))
    tap(pc, clock, right - 10, row_y(1))
    tap(pc, clock, right - 80, row_y(1))
    assert a.items[1].value == 1
    assert changes == [("BASS", 1), ("BASS", 2), ("BASS", 1)]
    # Horizontal drag on the edited row = rotate steps
    pc.down(right - 100, row_y(1))
    for dx in range(0, 70, 7):
        pc.move(right - 100 + dx, row_y(1))
    pc.up(right - 30, row_y(1))
    assert a.items[1].value == 4    # 63 px of travel = 3 steps of 16 px
    # Tap another row while editing: done + select + edit that row
    tap(pc, clock, 100, row_y(3))
    assert a._selected_index == 3 and a._editing
    # Label area of the edited row = done
    tap(pc, clock, 60, row_y(3))
    assert not a._editing
    # BACK chip pops to the main screen
    tap(pc, clock, 20, 232)
    assert app.current_screen is root


def test_engine_menu_tap_opens_entry():
    from cyberpunk_computer.ui.screens import EngineMenuScreen, DataSourcesScreen
    root = main_screen()
    app = FakeApp(root)
    em = EngineMenuScreen(SIZE, app)
    app.push_screen(em)
    pc, clock = make(lambda: app.current_screen)
    # Row 1 = CHART SETTINGS
    y = em.HEADER_HEIGHT + 8 + 1 * (em.ITEM_HEIGHT + 3) + 12
    assert tap(pc, clock, 200, y) == "click"
    assert type(app.current_screen).__name__ == "EngineScreen"
    tap(pc, clock, 20, 232)           # BACK chip
    assert app.current_screen is em
    assert em._selected_index == 1


def test_engine_menu_avc_status_screen():
    """AVC-LAN STATUS entry opens the read-only status page (AppState.avc);
    it renders from a cache and the BACK chip returns to the menu."""
    from dataclasses import replace
    from cyberpunk_computer.state.store import Store
    from cyberpunk_computer.ui.screens import EngineMenuScreen, AVCStatusScreen
    store = Store()
    st = store.state
    store._state = replace(st, avc=replace(st.avc, screen="5F", screen_name="energy"),
                           climate=replace(st.climate, outside_temp=None))
    app = FakeApp(main_screen())
    em = EngineMenuScreen(SIZE, app, store=store)
    app.push_screen(em)
    idx = next(i for i, it in enumerate(em._items) if it.action_key == "avc")
    pc, clock = make(lambda: app.current_screen)
    y = em.HEADER_HEIGHT + 8 + idx * (em.ITEM_HEIGHT + 3) + 12
    assert tap(pc, clock, 200, y) == "click"
    scr = app.current_screen
    assert isinstance(scr, AVCStatusScreen)
    surf = pygame.Surface(SIZE)
    scr.render(surf)
    first = scr._ref_avc
    scr.render(surf)                   # unchanged state: cached canvas reused
    assert scr._ref_avc is first is store.state.avc
    tap(pc, clock, 20, 232)            # BACK chip
    assert app.current_screen is em


def test_data_sources_tap_toggles():
    from cyberpunk_computer.ui.screens import DataSourcesScreen
    app = FakeApp(main_screen())
    ds = DataSourcesScreen(SIZE, app)
    app.push_screen(ds)
    pc, clock = make(lambda: app.current_screen)
    before = ds._items[0].enabled
    tap(pc, clock, 200, ds._content_top() + 10)
    assert ds._items[0].enabled is (not before)


def test_dtc_buttons_and_clear_confirm():
    from cyberpunk_computer.ui.screens import DTCScreen
    root = main_screen()
    app = FakeApp(root)
    d = DTCScreen(SIZE, app)
    app.push_screen(d)
    d._stored_dtcs = (("P0420", "ENG"),)
    pc, clock = make(lambda: app.current_screen)
    # CLEAR button (third) -> confirmation
    tap(pc, clock, 336, 230)
    assert d._focused_btn == d._BTN_CLEAR and d._clear_confirm
    # A tap on the confirmation bar cancels (PRESS_LIGHT)
    tap(pc, clock, 240, 230)
    assert not d._clear_confirm
    # BACK button pops
    tap(pc, clock, 144, 230)
    assert app.current_screen is root


def test_battery_paged_footer_and_swipe():
    from cyberpunk_computer.ui.screens import BatteryScreen
    root = main_screen()
    app = FakeApp(root)
    b = BatteryScreen(SIZE, app)
    app.push_screen(b)
    pc, clock = make(lambda: app.current_screen)
    tap(pc, clock, 470, 230)               # "▶" half of the page hint
    assert b._page == 1
    tap(pc, clock, 380, 230)               # "◀" half
    assert b._page == 0
    pc.down(400, 120)
    for x in (380, 340, 300):
        pc.move(x, 120)
    pc.up(300, 120)
    assert b._page == 1
    assert tap(pc, clock, 240, 120) == "click"   # body tap: nothing
    assert app.current_screen is b
    tap(pc, clock, 40, 230)                # BACK
    assert app.current_screen is root


def test_ev_screen_tap_back_hold_battery():
    from cyberpunk_computer.ui.screens import EVScreen
    root = main_screen()
    app = FakeApp(root)
    ev = EVScreen(SIZE, app)
    app.push_screen(ev)
    pc, clock = make(lambda: app.current_screen)
    assert long_press(pc, clock, 240, 120) == "long"
    assert type(app.current_screen).__name__ == "BatteryScreen"
    app.pop_screen()
    tap(pc, clock, 240, 120)
    assert app.current_screen is root


# ── Touch event stream (backend "touch" events) ──────────────────────────────

def _capture_touch_events(path, until_ht=None):
    """Convert 110->178 '00 21 24 78 X Y X Y' frames to the backend touch
    contract with the measured calibration px=(X-4)*480/245, py=(255-Y)*240/246.
    X=Y=0 is the release frame (sent with the last known position)."""
    events = []
    last = None
    for line in path.read_text().splitlines():
        o = json.loads(line)
        d = o["obj"]["d"]
        if d["m"] != "110" or d["s"] != "178" or d["d"][:4] != ["00", "21", "24", "78"]:
            continue
        ht = o["ht"]
        if until_ht is not None and ht > until_ht:
            break
        X, Y = int(d["d"][4], 16), int(d["d"][5], 16)
        if X == 0 and Y == 0:
            if last is None:
                continue
            events.append((ht, {"x": last[0], "y": last[1], "pressed": False,
                                "raw_x": 0, "raw_y": 0, "t": ht}))
            last = None
            continue
        px = min(479, max(0, round((X - 4) * 480 / 245)))
        py = min(239, max(0, round((255 - Y) * 240 / 246)))
        last = (px, py)
        events.append((ht, {"x": px, "y": py, "pressed": True,
                            "raw_x": X, "raw_y": Y, "t": ht}))
    return events


def _replay(events, screen):
    """Feed events through TouchEventQueue at their capture times, ticking
    the long-press timer at the Pi frame rate in between."""
    clock = Clock(events[0][0])
    pc = PointerController(lambda: screen, clock=clock)
    q = TouchEventQueue(clock=clock)
    log = []
    orig_click, orig_long = screen.pointer_click, screen.pointer_long_press

    def spy_click(target, x, y):
        log.append(("click", target, x, y))
        return orig_click(target, x, y)

    def spy_long(target, x, y):
        log.append(("long", target, x, y))
        return orig_long(target, x, y)

    screen.pointer_click, screen.pointer_long_press = spy_click, spy_long
    starts = []
    for ht, data in events:
        while clock.t + FRAME_S < ht:
            clock.t += FRAME_S
            q.drain(pc)
            pc.update()
        clock.t = ht
        was_pressed = pc.pressed
        q.push("touch", data)
        q.drain(pc)
        if pc.pressed and not was_pressed:
            starts.append((data["x"], data["y"]))
        pc.update()
        if not data["pressed"]:
            log.append(("gesture", pc.last_gesture[0], data["x"], data["y"]))
    return log, starts


def _title(target):
    return getattr(target, "title", None) or getattr(target, "name", type(target).__name__)


def test_capture_corner_taps_and_drags():
    events = _capture_touch_events(DATA / "emu8_release_ack_touch.ndjson",
                                   until_ht=1791980720.0)
    m = main_screen()
    log, starts = _replay(events, m)
    acts = [(kind, _title(t), x, y) for kind, t, x, y in
            (e for e in log if e[0] in ("click", "long"))]

    corner = {(0, 0): "AUDIO", (1, 0): "CLIMATE", (0, 1): "ENGINE", (1, 1): "BATTERY"}
    corner_acts = [a for a in acts if (a[2] < 30 or a[2] > 449) and (a[3] < 30 or a[3] > 209)]
    assert len(corner_acts) >= 8
    for kind, title, x, y in corner_acts:
        assert title == corner[(int(x > 240), int(y > 120))], (kind, title, x, y)

    # The four quick corner taps (687-692 s): TL, TR, BR, BL -> clicks
    quick = [a for a in acts if a[0] == "click"][-4:]
    assert [a[1] for a in quick] == ["AUDIO", "CLIMATE", "BATTERY", "ENGINE"]

    # The two drags: left->right at mid height, top->bottom at mid width.
    # Neither activates anything (no click/long), no page change.
    gestures = [e for e in log if e[0] == "gesture"]
    h_drag, v_drag = gestures[-2], gestures[-1]
    assert h_drag[1] == "none"     # started on AMBIENT (not draggable), moved off
    assert v_drag[1] == "drag"     # centre swipe zone claimed it, vertical = no-op
    assert starts[-2][0] < 10 and 100 < starts[-2][1] < 140
    assert 230 < starts[-1][0] < 260 and starts[-1][1] < 10
    assert m.current_page == 0
    assert not m._is_editing()


def test_touch_queue_stale_press_is_cancelled_not_clicked():
    m = main_screen()
    clock = Clock()
    pc = PointerController(lambda: m, clock=clock)
    q = TouchEventQueue(clock=clock)
    x, y = center(m._audio_frame.rect)
    q.push("touch", {"x": x, "y": y, "pressed": True, "t": 0})
    q.drain(pc)
    assert pc.pressed
    clock.t += 0.3
    q.drain(pc)
    assert pc.pressed
    clock.t += 1.0          # link dropped: no frames
    q.drain(pc)
    assert not pc.pressed
    assert not m._editing_volume
    # A release with no press (idle release frames) is ignored
    q.push("touch", {"x": x, "y": y, "pressed": False, "t": 0})
    q.drain(pc)
    assert not m._editing_volume


def test_touch_queue_ignores_other_events_and_routes_buttons():
    q = TouchEventQueue()
    q.push("input", {"x": 1})
    q.push("touch", "garbage")
    assert len(q) == 0
    q.push("button", {"key": "menu", "pressed": True, "t": 0})
    got = []
    q.drain(PointerController(lambda: None), lambda k, p: got.append((k, p)))
    assert got == [("menu", True)]


# ── MFD buttons ──────────────────────────────────────────────────────────────

def test_hw_button_mapping():
    from cyberpunk_computer.ui.screens import AudioScreen
    root = main_screen()
    app = FakeApp(root)
    opened = []
    root.app = app
    real_push = app.push_screen

    def push(s):
        opened.append(type(s).__name__)
        real_push(s)

    app.push_screen = push
    assert set(HW_BUTTON_TARGETS) == {"menu", "dest", "map", "maintenance", "calendar", "language"}

    app.push_screen(AudioScreen(SIZE, app))
    assert handle_hw_button(app, "menu", True) == "home"
    assert app.current_screen is root and root.current_page == 0

    assert handle_hw_button(app, "map", True) == "home_dynamics"
    assert root.current_page == 1
    assert handle_hw_button(app, "map", True) == "home_dynamics"   # toggles back
    assert root.current_page == 0

    assert handle_hw_button(app, "dest", True) == "engine_menu"
    assert opened[-1] == "EngineMenuScreen"
    assert handle_hw_button(app, "maintenance", True) == "dtc"
    assert opened[-1] == "DTCScreen"
    assert len(app.screen_stack) == 1           # unwound to home first

    assert handle_hw_button(app, "calendar", True) is None
    assert handle_hw_button(app, "language", True) is None
    assert handle_hw_button(app, "menu", False) is None   # release ignored
    assert handle_hw_button(app, "bogus", True) is None


# ── Application: mouse in dev mode (scale-aware) ─────────────────────────────

def test_application_mouse_scaled_and_keyboard_unchanged(monkeypatch):
    monkeypatch.setenv("SDL_VIDEODRIVER", "offscreen")
    pygame.display.quit()
    pygame.display.init()
    from cyberpunk_computer.config import Config
    from cyberpunk_computer.core.app import Application
    app = Application(Config(dev_mode=True, scale_factor=2, gateway_enabled=False))
    assert app.renderer.window.get_size() == (960, 480)
    m = main_screen(app)
    app.push_screen(m)

    cx, cy = center(m._climate_frame.rect)
    pygame.event.clear()
    pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(cx * 2, cy * 2)))
    pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=(cx * 2, cy * 2)))
    app._process_events()
    assert m._editing_target_temp
    assert m.focus_manager.focused_widget is m._climate_frame

    # Keyboard path untouched: Enter ends the edit, Right moves focus
    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN, mod=0, unicode="\r"))
    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RIGHT, mod=0, unicode=""))
    app._process_events()
    assert not m._editing_target_temp
    assert m.focus_manager.focused_widget is m._ambient_frame

    # Touch events queued from another thread feed the same pointer model
    import threading
    ax, ay = center(m._audio_frame.rect)
    t = threading.Thread(target=lambda: (
        app.touch_queue.push("touch", {"x": ax, "y": ay, "pressed": True, "t": 0}),
        app.touch_queue.push("touch", {"x": ax, "y": ay, "pressed": False, "t": 0})))
    t.start()
    t.join()
    app.touch_queue.drain(app.pointer, app._on_hw_button)
    assert m._editing_volume

    # Render path draws overlay + feedback without errors
    app.pointer.down(ax, ay)
    app._render()
    app.pointer.cancel()
    pygame.display.quit()
    pygame.display.init()
    pygame.display.set_mode(SIZE)


def test_climate_screen_outside_temp_na_without_avc():
    """Outside temperature is AVC-only: no value -> "N/A", never a default."""
    from cyberpunk_computer.ui.screens import ClimateScreen
    cs = ClimateScreen(SIZE, None)
    item = next(i for i in cs.items if i.label == "OUTSIDE TEMP")
    assert item.display_value == "N/A"
    cs.update_temperatures(outside=22.0)
    assert item.display_value == "22°C"
    cs.update_temperatures(outside=None)
    assert item.display_value == "N/A"
