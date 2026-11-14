"""
LED-key indicators: a pictogram with an indicator dash under it, like the
car's own climate keys (amber dash = on).

Used by the main screen's CLIMATE box for A/C, AUTO and the air intake. The
pictograms are original 1-bit drawings (hand-placed bitmaps, no icon font):
1 px strokes, no anti-aliasing, the brightness tier says on / off, the amber
dash says it again without relying on telling two cyans apart.

    ac       snowflake
    auto     the word AUTO (as on the car's key), Terminus 14
    intake   on  = recirculation: a car with the air looping inside
             off = fresh air: a car with an arrow entering at the nose
             (this key always shows which air is used)
"""

from __future__ import annotations

from typing import Dict, Tuple

import pygame

from .base import Rect, Widget
from ..colors import COLORS
from ..fonts import get_mono_font

_GLYPHS = {
    "snow": """
....#.#.#....
.....###.....
...#..#..#...
#...#.#.#...#
.#...###...#.
..#...#...#..
#############
..#...#...#..
.#...###...#.
#...#.#.#...#
...#..#..#...
.....###.....
....#.#.#....
""",
    "recirc": """
.......########......
......#........##....
.....#...........#...
....#...########..#..
#####..#...........#.
#......#......#.....#
#.......########....#
#.............#.....#
#...................#
#####################
...###.........###...
....#...........#....
""",
    "fresh": """
...........########......
..........#........##....
.........#...........#...
........#.............#..
....#####..............#.
....#....#..............#
###########.............#
....#....#..............#
....#...................#
....#####################
.......###.........###...
........#...........#....
""",
}

_CACHE: Dict[Tuple[str, Tuple[int, int, int]], pygame.Surface] = {}
_KEY = (255, 0, 255)


def glyph(name: str, color: Tuple[int, int, int]) -> pygame.Surface:
    """The pictogram ``name`` in ``color`` (colour-keyed surface, cached)."""
    key = (name, tuple(color))
    surf = _CACHE.get(key)
    if surf is None:
        rows = _GLYPHS[name].strip("\n").split("\n")
        w = max(len(r) for r in rows)
        surf = pygame.Surface((w, len(rows)))
        surf.fill(_KEY)
        surf.set_colorkey(_KEY)
        for y, row in enumerate(rows):
            for x, ch in enumerate(row):
                if ch == "#":
                    surf.set_at((x, y), color)
        _CACHE[key] = surf
    return surf


class LedKeyIcon(Widget):
    """One LED key. ``icon``: ``ac`` | ``auto`` | ``recirc`` (the intake key)."""

    LED_W = 13

    def __init__(self, rect: Rect, icon: str = "ac", active: bool = False):
        super().__init__(rect, focusable=False)
        self.icon = icon
        self._active = active

    def set_active(self, active: bool) -> None:
        if self._active != active:
            self._active = active
            self._dirty = True

    def render(self, surface: pygame.Surface) -> None:
        if not self.visible:
            return
        on = bool(self._active)
        cx = self.rect.x + self.rect.width // 2
        cy = self.rect.y + 8
        col = COLORS["cyan_bright"] if on else COLORS["inactive"]
        led = True
        if self.icon == "auto":
            pic = get_mono_font(14).render("AUTO", False, col)
        elif self.icon == "recirc":
            # the intake key: recirculation (lit) or fresh air (no LED)
            pic = glyph("recirc", col) if on else glyph("fresh", COLORS["cyan_mid"])
            led = on
            if not on:
                cx -= 1
        else:
            pic = glyph("snow", col)
        surface.blit(pic, (cx - pic.get_width() // 2, cy - pic.get_height() // 2))
        if led:
            pygame.draw.rect(surface, COLORS["amber"] if on else COLORS["cyan_dark"],
                             (cx - self.LED_W // 2, self.rect.y + 18, self.LED_W, 2))
