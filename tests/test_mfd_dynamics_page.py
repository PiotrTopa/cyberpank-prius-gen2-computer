"""MFD dynamics page (page 2) draws the calibrated CAN 0x022 / 0x023 values
and keeps working with a state that lacks them (older backend)."""

import os

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
pygame = pytest.importorskip("pygame")

from cyberpunk_computer.state.actions import SetPassiveSignalsAction  # noqa: E402
from cyberpunk_computer.state.store import Store  # noqa: E402


def _page(store):
    from cyberpunk_computer.ui.screens.main_screen import MainScreen
    pygame.init()
    ms = MainScreen((480, 240), None)
    ms._store = store
    surf = pygame.Surface((480, 240))
    ms._render_dynamics_page(surf, 120, 240)
    return surf


def test_dynamics_page_renders_calibrated_and_unknown_values():
    store = Store()
    _page(store)                                  # all None: dashes
    store.dispatch(SetPassiveSignalsAction(dynamics={
        "yaw_rate_dps": -12.3, "lateral_accel_ms2": 3.1, "longitudinal_accel_ms2": -0.4}))
    surf = _page(store)
    assert surf.get_bounding_rect().width > 0
