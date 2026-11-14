"""
Road-sign pictograms for the scene's sign columns.

Small original drawings of European (Vienna convention, Polish codes) signs,
keyed by the roadsense rule id (roadsense ``api/roadsense_api/rules/pl.json``
``effects[].id``). Neon style like the limit sign: dark faces, coloured
outlines, 1-bit edges. Each pictogram is drawn once and cached.

An id without a drawing falls back to a plate with the sign code roadsense
prints ("B-26"), so a rule is never dropped silently.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional, Tuple

import pygame

RGB = Tuple[int, int, int]

KEY = (255, 0, 255)
RED = (255, 36, 36)
BLUE = (30, 96, 214)
BLUE_HI = (120, 180, 255)
YELLOW = (255, 206, 64)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
DARK = (22, 18, 6)
GREEN = (0, 230, 118)

S = 19           # pictogram size (odd: a centre pixel)
C = S // 2


def _surface() -> pygame.Surface:
    s = pygame.Surface((S, S))
    s.fill(KEY)
    s.set_colorkey(KEY, pygame.RLEACCEL)
    return s


def _ring(s: pygame.Surface) -> None:
    """Prohibition base: red ring on a dark face."""
    pygame.draw.circle(s, BLACK, (C, C), C)
    pygame.draw.circle(s, RED, (C, C), C, 2)


def _disc(s: pygame.Surface, col: RGB = BLUE) -> None:
    """Mandatory base: filled disc."""
    pygame.draw.circle(s, col, (C, C), C)


def _plate(s: pygame.Surface, col: RGB = BLUE) -> None:
    """Information base: filled square plate."""
    pygame.draw.rect(s, col, (0, 1, S, S - 2))
    pygame.draw.rect(s, BLUE_HI if col == BLUE else WHITE, (0, 1, S, S - 2), 1)


def _warn(s: pygame.Surface) -> None:
    """Warning base: yellow triangle, red edge."""
    pts = [(C, 0), (S - 1, S - 2), (0, S - 2)]
    pygame.draw.polygon(s, YELLOW, pts)
    pygame.draw.polygon(s, RED, pts, 2)


def _slash(s: pygame.Surface) -> None:
    pygame.draw.line(s, RED, (4, 4), (S - 5, S - 5), 2)


def _arrow_up(s: pygame.Surface, x: int, y0: int, y1: int, col: RGB) -> None:
    pygame.draw.line(s, col, (x, y0), (x, y1))
    pygame.draw.polygon(s, col, [(x - 2, y0 + 2), (x + 2, y0 + 2), (x, y0 - 1)])


def _arrow_down(s: pygame.Surface, x: int, y0: int, y1: int, col: RGB) -> None:
    pygame.draw.line(s, col, (x, y0), (x, y1))
    pygame.draw.polygon(s, col, [(x - 2, y1 - 2), (x + 2, y1 - 2), (x, y1 + 1)])


def _figure(s: pygame.Surface, x: int, y: int, col: RGB) -> None:
    """A walking person, 5 px tall."""
    s.set_at((x, y), col)
    pygame.draw.line(s, col, (x, y + 1), (x, y + 3))
    pygame.draw.line(s, col, (x - 1, y + 2), (x + 1, y + 2))
    s.set_at((x - 1, y + 4), col)
    s.set_at((x + 1, y + 4), col)


# ── prohibitions ─────────────────────────────────────────────────────────────

def _no_overtaking(s):
    _ring(s)
    pygame.draw.rect(s, RED, (4, 7, 5, 5))
    pygame.draw.rect(s, WHITE, (10, 7, 5, 5))


def _no_overtaking_hgv(s):
    _ring(s)
    pygame.draw.rect(s, RED, (4, 6, 5, 7))
    pygame.draw.rect(s, WHITE, (10, 7, 5, 5))


def _no_stopping(s):
    _disc(s, BLUE)
    pygame.draw.circle(s, RED, (C, C), C, 2)
    pygame.draw.line(s, RED, (4, 4), (S - 5, S - 5), 2)
    pygame.draw.line(s, RED, (S - 5, 4), (4, S - 5), 2)


def _no_parking(s):
    _disc(s, BLUE)
    pygame.draw.circle(s, RED, (C, C), C, 2)
    _slash(s)


def _no_entry(s):
    _disc(s, RED)
    pygame.draw.rect(s, WHITE, (4, C - 1, S - 8, 3))


def _road_closed(s):
    _ring(s)


def _no_left_turn(s):
    _ring(s)
    pygame.draw.lines(s, WHITE, False, [(11, 14), (11, 8), (6, 8)])
    pygame.draw.polygon(s, WHITE, [(7, 6), (7, 10), (4, 8)])
    _slash(s)


def _no_right_turn(s):
    _ring(s)
    pygame.draw.lines(s, WHITE, False, [(7, 14), (7, 8), (12, 8)])
    pygame.draw.polygon(s, WHITE, [(11, 6), (11, 10), (14, 8)])
    pygame.draw.line(s, RED, (S - 5, 4), (4, S - 5), 2)


def _no_u_turn(s):
    _ring(s)
    pygame.draw.lines(s, WHITE, False, [(12, 14), (12, 7), (10, 5), (8, 5), (6, 7), (6, 11)])
    pygame.draw.polygon(s, WHITE, [(4, 10), (8, 10), (6, 13)])
    _slash(s)


def _no_turns(s):
    _ring(s)
    _arrow_up(s, C, 5, 14, WHITE)


def _no_straight(s):
    _ring(s)
    _arrow_up(s, C, 5, 14, WHITE)
    _slash(s)


def _height_limit(s):
    _ring(s)
    pygame.draw.polygon(s, WHITE, [(C - 3, 4), (C + 3, 4), (C, 7)])
    pygame.draw.polygon(s, WHITE, [(C - 3, 14), (C + 3, 14), (C, 11)])


def _width_limit(s):
    _ring(s)
    pygame.draw.polygon(s, WHITE, [(4, C - 3), (4, C + 3), (7, C)])
    pygame.draw.polygon(s, WHITE, [(14, C - 3), (14, C + 3), (11, C)])


def _weight_limit(s):
    _ring(s)
    pygame.draw.line(s, WHITE, (C, 5), (C, 13))
    pygame.draw.line(s, WHITE, (C - 2, 8), (C + 2, 8))
    pygame.draw.line(s, WHITE, (C, 13), (C + 2, 13))


def _give_way_oncoming(s):
    _ring(s)
    _arrow_up(s, 12, 6, 13, WHITE)
    _arrow_down(s, 6, 5, 12, RED)


def _vehicle_ban(s):
    """No motor vehicles / trucks / buses ...: ring with a vehicle box."""
    _ring(s)
    pygame.draw.rect(s, WHITE, (5, 7, 9, 5), 1)


# ── priority ─────────────────────────────────────────────────────────────────

def _stop(s):
    o = 5
    pts = [(o, 0), (S - 1 - o, 0), (S - 1, o), (S - 1, S - 1 - o), (S - 1 - o, S - 1),
           (o, S - 1), (0, S - 1 - o), (0, o)]
    pygame.draw.polygon(s, RED, pts)
    pygame.draw.polygon(s, WHITE, pts, 1)
    # "STOP" does not fit 19 px in any 1-bit font: a bold white bar of letters
    letters = (
        (2, ((0, 1, 2), (0,), (0, 1, 2), (2,), (0, 1, 2))),       # S
        (6, ((0, 1, 2), (1,), (1,), (1,), (1,))),                 # T
        (10, ((0, 1, 2), (0, 2), (0, 2), (0, 2), (0, 1, 2))),     # O
        (14, ((0, 1, 2), (0, 2), (0, 1, 2), (0,), (0,))),         # P
    )
    for x, glyph in letters:
        for row, cols in enumerate(glyph):
            for c in cols:
                s.set_at((x + c, 7 + row), WHITE)


def _yield(s):
    pts = [(0, 1), (S - 1, 1), (C, S - 1)]
    pygame.draw.polygon(s, YELLOW, pts)
    pygame.draw.polygon(s, RED, pts, 3)


def _priority_road(s):
    pygame.draw.polygon(s, WHITE, [(C, 0), (S - 1, C), (C, S - 1), (0, C)])
    pygame.draw.polygon(s, YELLOW, [(C, 4), (S - 5, C), (C, S - 5), (4, C)])


def _end_priority(s):
    _priority_road(s)
    pygame.draw.line(s, BLACK, (S - 4, 3), (3, S - 4), 2)


def _priority_over_oncoming(s):
    _plate(s)
    _arrow_up(s, 12, 5, 13, WHITE)
    _arrow_down(s, 6, 5, 13, RED)


# ── information / mandatory ──────────────────────────────────────────────────

def _built_up(s):
    pygame.draw.rect(s, BLACK, (0, 3, S, 13))
    pygame.draw.rect(s, WHITE, (0, 3, S, 13), 1)
    pygame.draw.lines(s, WHITE, False, [(2, 13), (2, 9), (5, 9), (5, 6), (8, 6), (8, 10),
                                        (11, 10), (11, 7), (13, 5), (15, 7), (15, 13)])


def _living_street(s):
    _plate(s)
    pygame.draw.lines(s, WHITE, False, [(9, 9), (12, 6), (15, 9), (15, 14), (9, 14), (9, 9)])
    _figure(s, 5, 8, WHITE)


def _motorway(s):
    _plate(s)
    pygame.draw.line(s, WHITE, (6, 15), (8, 6))
    pygame.draw.line(s, WHITE, (12, 15), (10, 6))
    pygame.draw.line(s, WHITE, (3, 5), (15, 5))
    pygame.draw.line(s, WHITE, (3, 4), (15, 4))


def _expressway(s):
    _plate(s)
    pygame.draw.rect(s, WHITE, (5, 8, 9, 5), 1)
    pygame.draw.line(s, WHITE, (7, 6), (11, 6))
    s.set_at((6, 14), WHITE)
    s.set_at((12, 14), WHITE)


def _one_way(s):
    _plate(s)
    _arrow_up(s, C, 5, 14, WHITE)
    pygame.draw.line(s, WHITE, (C - 1, 7), (C - 1, 14))
    pygame.draw.line(s, WHITE, (C + 1, 7), (C + 1, 14))


def _dead_end(s):
    _plate(s)
    pygame.draw.line(s, WHITE, (C, 8), (C, 15), 3)
    pygame.draw.rect(s, RED, (4, 4, 11, 3))


def _crossing(s):
    _plate(s)
    pygame.draw.polygon(s, WHITE, [(C, 3), (15, 15), (3, 15)])
    _figure(s, C, 8, BLACK)


def _roundabout(s):
    _disc(s)
    pygame.draw.circle(s, WHITE, (C, C), 5, 1)
    for x, y in ((C, 4), (5, 12), (13, 12)):
        pygame.draw.rect(s, BLUE, (x - 1, y - 1, 3, 3))
    for x, y in ((C + 2, 3), (4, 10), (14, 13)):
        s.set_at((x, y), WHITE)


def _mandatory(s):
    _disc(s)
    _arrow_up(s, C, 5, 14, WHITE)


# ── warnings (triangle + glyph) ──────────────────────────────────────────────

def _w(glyph: Callable[[pygame.Surface], None]) -> Callable[[pygame.Surface], None]:
    def draw(s: pygame.Surface) -> None:
        _warn(s)
        glyph(s)
    return draw


def _g_bang(s):
    pygame.draw.line(s, DARK, (C, 7), (C, 11))
    s.set_at((C, 13), DARK)
    s.set_at((C, 14), DARK)


def _g_ring(s):
    pygame.draw.circle(s, DARK, (C, 11), 3, 1)


def _g_cross(s):
    pygame.draw.line(s, DARK, (C, 7), (C, 15))
    pygame.draw.line(s, DARK, (C - 3, 11), (C + 3, 11))


def _g_side(s):
    pygame.draw.line(s, DARK, (C, 7), (C, 15), 2)
    pygame.draw.line(s, DARK, (C, 11), (C + 4, 11))


def _g_signals(s):
    s.set_at((C, 8), RED)
    s.set_at((C, 7), RED)
    s.set_at((C, 10), DARK)
    s.set_at((C, 11), DARK)
    s.set_at((C, 13), GREEN)
    s.set_at((C, 14), GREEN)


def _g_xing(s):
    pygame.draw.line(s, DARK, (C - 3, 8), (C + 3, 14))
    pygame.draw.line(s, DARK, (C + 3, 8), (C - 3, 14))


def _g_person(s):
    _figure(s, C, 9, DARK)


def _g_children(s):
    _figure(s, C - 2, 10, DARK)
    _figure(s, C + 2, 9, DARK)


def _g_bike(s):
    pygame.draw.circle(s, DARK, (C - 3, 13), 2, 1)
    pygame.draw.circle(s, DARK, (C + 3, 13), 2, 1)
    pygame.draw.line(s, DARK, (C - 3, 13), (C, 9))
    pygame.draw.line(s, DARK, (C, 9), (C + 3, 13))


def _g_works(s):
    pygame.draw.polygon(s, DARK, [(C - 4, 15), (C + 3, 15), (C, 11)])
    _figure(s, C - 1, 6, DARK)


def _g_slippery(s):
    pygame.draw.rect(s, DARK, (C - 2, 7, 5, 3))
    pygame.draw.lines(s, DARK, False, [(C - 3, 12), (C - 1, 14), (C - 3, 15)])
    pygame.draw.lines(s, DARK, False, [(C + 2, 12), (C + 4, 14), (C + 2, 15)])


def _g_bump(s):
    pygame.draw.lines(s, DARK, False, [(C - 5, 14), (C - 2, 14), (C, 10), (C + 2, 14), (C + 5, 14)])


def _g_narrows(s):
    pygame.draw.lines(s, DARK, False, [(C - 3, 15), (C - 3, 12), (C - 1, 10), (C - 1, 7)])
    pygame.draw.lines(s, DARK, False, [(C + 3, 15), (C + 3, 12), (C + 1, 10), (C + 1, 7)])


def _g_two_way(s):
    _arrow_up(s, C - 2, 8, 14, DARK)
    _arrow_down(s, C + 2, 8, 14, DARK)


def _g_curve(s):
    pygame.draw.lines(s, DARK, False, [(C - 2, 15), (C - 2, 11), (C + 2, 8), (C + 2, 6)], 2)


def _g_steep(s):
    pygame.draw.polygon(s, DARK, [(C - 5, 15), (C + 5, 15), (C + 5, 9)])


def _g_rocks(s):
    pygame.draw.line(s, DARK, (C - 4, 8), (C - 4, 15))
    for x, y in ((C - 1, 10), (C + 1, 13), (C + 3, 15), (C, 15)):
        pygame.draw.rect(s, DARK, (x, y - 1, 2, 2))


def _g_tram(s):
    pygame.draw.rect(s, DARK, (C - 3, 8, 7, 6), 1)
    pygame.draw.line(s, DARK, (C, 6), (C, 8))


def _g_animal(s):
    pygame.draw.lines(s, DARK, False, [(C - 4, 15), (C - 3, 11), (C + 2, 11), (C + 4, 8)])
    pygame.draw.line(s, DARK, (C + 2, 11), (C + 3, 15))


def _g_height(s):
    pygame.draw.line(s, DARK, (C - 4, 8), (C + 4, 8))
    pygame.draw.polygon(s, DARK, [(C - 2, 13), (C + 2, 13), (C, 10)])


_DRAW: Dict[str, Callable[[pygame.Surface], None]] = {
    "no_overtaking": _no_overtaking, "no_overtaking_hgv": _no_overtaking_hgv,
    "no_stopping": _no_stopping, "no_parking": _no_parking,
    "no_entry": _no_entry, "road_closed": _road_closed,
    "no_left_turn": _no_left_turn, "no_right_turn": _no_right_turn,
    "no_u_turn": _no_u_turn, "no_turns": _no_turns, "no_straight": _no_straight,
    "height_limit": _height_limit, "width_limit": _width_limit, "weight_limit": _weight_limit,
    "give_way_oncoming": _give_way_oncoming,
    "no_motor_vehicles": _vehicle_ban, "no_trucks": _vehicle_ban, "no_buses": _vehicle_ban,
    "no_motorcycles": _vehicle_ban, "no_trailers": _vehicle_ban,
    "stop": _stop, "yield": _yield, "priority_road": _priority_road,
    "end_of_priority_road": _end_priority, "priority_over_oncoming": _priority_over_oncoming,
    "built_up_area": _built_up, "living_street": _living_street,
    "motorway": _motorway, "expressway": _expressway,
    "one_way": _one_way, "one_way_side": _one_way, "dead_end": _dead_end,
    "pedestrian_crossing": _crossing, "roundabout": _roundabout,
    "mandatory_direction": _mandatory,
    # warnings
    "roundabout_ahead": _w(_g_ring), "crossroads_ahead": _w(_g_cross),
    "side_road_ahead": _w(_g_side), "traffic_signals_ahead": _w(_g_signals),
    "stop_ahead": _w(_g_bang), "level_crossing_ahead": _w(_g_xing),
    "pedestrian_crossing_ahead": _w(_g_person), "children_ahead": _w(_g_children),
    "cyclists_ahead": _w(_g_bike), "tram_crossing_ahead": _w(_g_tram),
    "animals_ahead": _w(_g_animal), "roadworks_ahead": _w(_g_works),
    "slippery_ahead": _w(_g_slippery), "uneven_road_ahead": _w(_g_bump),
    "road_narrows_ahead": _w(_g_narrows), "two_way_ahead": _w(_g_two_way),
    "curve_ahead": _w(_g_curve), "lane_change_ahead": _w(_g_narrows),
    "steep_ahead": _w(_g_steep), "falling_rocks_ahead": _w(_g_rocks),
    "other_danger_ahead": _w(_g_bang), "height_restriction_ahead": _w(_g_height),
    "warning_other": _w(_g_bang),
}

KNOWN_IDS = tuple(sorted(_DRAW))

_CACHE: Dict[Tuple[str, str, bool], pygame.Surface] = {}


def sign_surface(rule_id: str, code: str = "", unsure: bool = False,
                 text: Optional[Callable[[str, RGB], pygame.Surface]] = None) -> pygame.Surface:
    """The 19 x 19 pictogram of a roadsense rule id (colour-keyed, cached).

    ``text(string, colour)`` renders tiny text for the fallback plate and the
    "?" of an uncertain entry.
    """
    key = (rule_id, code if rule_id not in _DRAW else "", unsure)
    surf = _CACHE.get(key)
    if surf is not None:
        return surf
    if len(_CACHE) > 96:
        _CACHE.clear()
    s = _surface()
    draw = _DRAW.get(rule_id)
    if draw is not None:
        draw(s)
    else:
        # unknown rule: a plate with roadsense's sign code, never nothing
        pygame.draw.rect(s, BLACK, (0, 4, S, 11))
        pygame.draw.rect(s, (110, 150, 160), (0, 4, S, 11), 1)
        if text is not None:
            label = (code or "?").replace("-", "")[:4]
            t = text(label, WHITE)
            s.blit(t, (max(1, (S - t.get_width()) // 2 + 1), 6))
    if unsure and text is not None:
        q = text("?", (255, 235, 153))
        pygame.draw.rect(s, BLACK, (S - 6, S - 8, 6, 8))
        s.blit(q, (S - 5, S - 8))
    _CACHE[key] = s
    return s
