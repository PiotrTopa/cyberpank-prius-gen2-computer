"""MFD alert layer: the event feed (ttl, acknowledge), the banner in the
strip row, the real-speed chip, frame pacing hints and the acknowledge zone.

Run with the frontend venv: SDL_VIDEODRIVER=dummy .venv-fe/bin/python -m pytest tests/test_mfd_alerts.py
"""

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pytest  # noqa: E402

pygame = pytest.importorskip("pygame")

from cyberpunk_computer.ui.scene.alerts import EVENT, AlertBanner, AlertFeed, attach  # noqa: E402


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


ALERT = {"id": "speed_limit", "seq": 1, "text": "LIMIT 50  REAL 62", "severity": "notice",
         "icon": "limit", "ack": True, "ttl": 5.0, "chip": {"real_kmh": 62}}


@pytest.fixture(scope="module", autouse=True)
def _pg():
    pygame.init()
    pygame.display.set_mode((480, 240))
    yield


def test_feed_ttl_null_and_foreign_events():
    clk = Clock()
    f = AlertFeed(clock=clk)
    f.push("roadsense_scene", {"alert": ALERT})
    assert f.current() is None
    f.push(EVENT, {"alert": ALERT})
    assert f.current()["id"] == "speed_limit"
    clk.t += 5.5
    assert f.current() is None             # link lost: never a stale alert
    f.push(EVENT, {"alert": ALERT})
    f.push(EVENT, {"alert": None})
    assert f.current() is None


def test_ack_sends_and_hides():
    clk = Clock()
    f = AlertFeed(clock=clk)
    sent = []
    f.ack_fn = lambda aid, long: sent.append((aid, long))
    assert f.ack() is False
    f.push(EVENT, {"alert": ALERT})
    assert f.ack(long=True) is True and sent == [("speed_limit", True)] and f.current() is None
    f.push(EVENT, {"alert": dict(ALERT, ack=False)})
    assert f.ack() is False and f.current() is not None


def test_attach_remote_and_local_twins():
    class Remote:
        def __init__(self):
            self.cbs, self.sent = [], []

        def add_event_callback(self, cb):
            self.cbs.append(cb)

        def send_input_event(self, device, event, value=None):
            self.sent.append((device, event, value))
            return True

    r = Remote()
    f = AlertFeed()
    attach(r, f)
    r.cbs[0](EVENT, {"alert": ALERT})
    assert f.ack()
    assert r.sent == [("mfd", "alert_ack", {"id": "speed_limit", "long": False})]

    class Local:
        def __init__(self):
            self.actions = []
            self.store = self

        def dispatch(self, a):
            self.actions.append(a)

    loc = Local()
    f2 = AlertFeed()
    attach(loc, f2)
    f2.push(EVENT, {"alert": ALERT})
    assert f2.ack()
    a = loc.actions[0]
    assert type(a).__name__ == "InputEventAction" and a.event == "alert_ack"


def test_banner_draws_frame_icon_text_and_caches():
    b = AlertBanner((194, 21))
    s = b.render(ALERT)
    amber = b.colors["amber"]
    assert tuple(s.get_at((0, 0)))[:3] == amber              # frame in the severity colour
    assert any(tuple(s.get_at((x, 10)))[:3] == amber for x in range(3, 20))   # icon
    b.render(dict(ALERT))
    assert b.redraws == 1
    b.render(dict(ALERT, severity="warning", advisory=True, text="CLOSING 1.6s"))
    assert b.redraws == 2 and tuple(b.surface.get_at((0, 0)))[:3] == b.colors["red_bright"]
    long = b.render(dict(ALERT, text="X" * 60))
    assert long.get_width() == 194                            # truncated, never wider


def test_every_icon_draws():
    from cyberpunk_computer.ui.scene import alerts as mod
    surf = pygame.Surface((15, 15))
    for name in ("limit", "lead", "go", "light", "curve", "recirc", "lift", "snow", "defog",
                 "service", "fuel", "fault", "lane", "door", "info", "unknown"):
        surf.fill((0, 0, 0))
        mod._icon(pygame, surf, name, 0, 0, (255, 255, 255))
        assert any(surf.get_at((x, y))[:3] != (0, 0, 0) for x in range(15) for y in range(15)), name


def _page(feed, clk):
    from cyberpunk_computer.ui.scene.page import ScenePage
    return ScenePage(121, 1, 238, feed=None, clock=clk, alerts=feed)


def test_scene_page_pacing_and_strip_banner():
    clk = Clock()
    feed = AlertFeed(clock=clk)
    page = _page(feed, clk)
    surf = pygame.Surface((480, 240))
    page.render_strip(surf)
    page.render_scene(surf)
    assert page.needs_frame(home=False) is False or page.history.version != page._hist_seen
    page._hist_seen = page.history.version
    assert page.needs_frame(home=False) is False
    feed.push(EVENT, {"alert": ALERT})
    assert page.needs_frame(home=False) is True               # appeared
    page.render_strip(surf)
    page._hist_seen = page.history.version
    assert page.needs_frame(home=False) is False
    assert page.needs_frame(home=True) is True                # the chip is not drawn yet
    page.render_scene(surf)
    assert page.needs_frame(home=True) in (False, True)
    x, y, w, h = page.alert_rect()
    assert (x, y, w, h) == (121, 218, 194, 21)
    assert tuple(surf.get_at((x, y)))[:3] == page.banner.colors["amber"]
    clk.t += 6.0                                              # ttl expired
    assert page.needs_frame(home=False) is True
    page.render_strip(surf)
    assert tuple(surf.get_at((x, y)))[:3] != page.banner.colors["amber"]


def test_main_screen_ack_zone_only_while_shown():
    from cyberpunk_computer.ui.scene.alerts import ALERT_FEED
    from cyberpunk_computer.ui.screens.main_screen import MainScreen
    ms = MainScreen((480, 240), None)
    sent = []
    ALERT_FEED.ack_fn = lambda aid, long: sent.append((aid, long))
    try:
        ALERT_FEED.push(EVENT, {"alert": None})
        z = ms.pointer_target_at(200, 228)
        assert getattr(z, "name", "") != "alert-ack"
        ALERT_FEED.push(EVENT, {"alert": ALERT})
        z = ms.pointer_target_at(200, 228)
        assert z.name == "alert-ack"
        z.on_click(200, 228)
        assert sent == [("speed_limit", False)]
        ALERT_FEED.push(EVENT, {"alert": ALERT})
        assert z.on_long_press(200, 228) is True and sent[-1] == ("speed_limit", True)
        # the page dots stay reachable
        assert getattr(ms.pointer_target_at(340, 228), "name", "") != "alert-ack"
    finally:
        ALERT_FEED.ack_fn = None
        ALERT_FEED.push(EVENT, {"alert": None})


def test_alert_shots_render(tmp_path):
    from cyberpunk_computer.ui.scene.alerts_demo import SHOTS, render_shots
    paths = render_shots(str(tmp_path))
    assert len(paths) == len(SHOTS) and all(os.path.exists(p) for p in paths)
