"""
Geometry of the radar-view button (ui/scene/page.py, radar.py). No pygame and no other scene module: the main
screen registers the touch zone at start-up, before ``ui.scene``'s heavy modules are imported (section 1 of
docs/MFD_MAIN_SCREEN.md: the scene is built after the first frame).
"""

from __future__ import annotations

from typing import Tuple

SCENE_H = 216            # page.SCENE_H
BTN_W, BTN_H = 34, 22    # drawn button (Terminus 14 label)
BTN_MARGIN = 2           # from the scene's right and bottom edges
BTN_HIT_PAD = (10, 9)    # the touch zone reaches this much further left / up (inside the scene): 46 x 33 px


def radar_button_rect(width: int) -> Tuple[int, int, int, int]:
    """(x, y, w, h) of the drawn button in scene coordinates."""
    return (width - BTN_W - BTN_MARGIN, SCENE_H - BTN_H - BTN_MARGIN, BTN_W, BTN_H)


def radar_button_hit_rect(x: int, y: int, width: int) -> Tuple[int, int, int, int]:
    """(x, y, w, h) of the button's touch zone in screen coordinates, for a page at (x, y): the button plus
    BTN_HIT_PAD to the left and up and its margin to the right and bottom (the scene's edge)."""
    bx, by, bw, bh = radar_button_rect(width)
    px, py = BTN_HIT_PAD
    return (x + bx - px, y + by - py, bw + px + BTN_MARGIN, bh + py + BTN_MARGIN)
