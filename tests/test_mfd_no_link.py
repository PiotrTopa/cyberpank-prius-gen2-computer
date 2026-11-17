"""MFD main screen shows ``NO LINK`` when the backend link is silent (incident
2026-11-17: ten minutes of a frozen, live-looking picture with link=DOWN)."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
pygame = pytest.importorskip("pygame")


class _Twin:
    def __init__(self):
        self.age = 0.5

    def link_age_s(self):
        return self.age


def _screen(twin):
    from cyberpunk_computer.ui.screens.main_screen import MainScreen
    pygame.init()
    app = SimpleNamespace(_virtual_twin=twin, config=SimpleNamespace(
        dev_mode=False, timeout_focus_hide=15.0, timeout_editing_exit=60.0))
    ms = MainScreen((480, 240), app)
    ms._gateway_connected = True
    ms._connection_indicator.set_connected(True)
    return ms


def _tag_pixels(ms):
    """Amber pixels in the strip left of the link LED."""
    from cyberpunk_computer.ui.colors import COLORS
    surf = pygame.Surface((480, 240))
    surf.fill((0, 0, 0))
    ms._render_no_link(surf, 120, 240) if ms._link_lost else None
    led = ms._connection_indicator.rect
    amber = tuple(COLORS["amber"][:3])
    n = 0
    for x in range(led.x - 80, led.x):
        for y in range(0, 24):
            if tuple(surf.get_at((x, y)))[:3] == amber:
                n += 1
    return n


def test_no_link_appears_after_silence_and_clears_on_rx():
    twin = _Twin()
    ms = _screen(twin)
    ms._check_backend_link()
    assert not ms._link_lost and ms._connection_indicator.connected
    assert _tag_pixels(ms) == 0

    twin.age = ms.LINK_LOST_S + 0.1
    ms._chrome_valid = True
    ms._check_backend_link()
    assert ms._link_lost
    assert not ms._connection_indicator.connected      # LED goes red too
    assert not ms._chrome_valid                        # forces a frame on the edge
    assert ms.frame_state()[0] is True
    assert _tag_pixels(ms) > 20

    twin.age = 0.2
    ms._check_backend_link()
    assert not ms._link_lost and ms._connection_indicator.connected


def test_no_indication_without_a_remote_backend():
    ms = _screen(SimpleNamespace())                    # local twin: no link_age_s
    ms._check_backend_link()
    assert not ms._link_lost
    ms = _screen(None)
    ms._check_backend_link()
    assert not ms._link_lost


def test_full_render_draws_the_tag():
    twin = _Twin()
    ms = _screen(twin)
    twin.age = 30.0
    ms._check_backend_link()
    surf = pygame.Surface((480, 240))
    ms.render(surf)
    from cyberpunk_computer.ui.colors import COLORS
    led = ms._connection_indicator.rect
    amber = tuple(COLORS["amber"][:3])
    hits = sum(1 for x in range(led.x - 80, led.x) for y in range(24)
               if tuple(surf.get_at((x, y)))[:3] == amber)
    assert hits > 20
