"""
Pointer helpers for the custom-drawn menu screens.

The settings screens (audio, climate, lights, ambient, chart settings,
engine menu, data sources) draw a vertical list of rows and drive it with
``_selected_index`` / ``_editing`` + encoder events. :class:`MenuListZone`
maps touch onto exactly those encoder events, so touch and encoder behave
identically:

* tap a row (not editing)        -> select it, PRESS_LIGHT (edit / open / toggle)
* tap another row while editing  -> PRESS_LIGHT (done), select it, PRESS_LIGHT
* tap the row being edited       -> right end ('>' side, last 40 px) = ROTATE_RIGHT,
                                    the value area left of it = ROTATE_LEFT,
                                    the label area = PRESS_LIGHT (done)
* horizontal drag on that row    -> one ROTATE per ``DRAG_STEP_PX``
* vertical drag (scrollable)     -> one ROTATE per row stride (scrolls selection)
"""

from __future__ import annotations

from typing import Callable, Optional

from ..pointer import PointerZone, TAP_SLOP
from ...input.manager import InputEvent as IE

DRAG_STEP_PX = 16
VALUE_INC_PX = 40     # rightmost part of an editing row = increment
VALUE_AREA_PX = 160   # value area (decrement) left of it


class MenuListZone(PointerZone):
    """Pointer zone covering a screen's list rows."""

    def __init__(
        self,
        screen,
        x: int,
        width: int,
        first_y: int,
        stride: int,
        row_h: int,
        bottom: int,
        count: Callable[[], int],
        scroll_px: Optional[Callable[[], int]] = None,
        width_fn: Optional[Callable[[], int]] = None,
        editing_attr: str = "_editing",
        scrollable: bool = False,
    ):
        super().__init__((x, first_y, width, bottom - first_y), name="menu-list")
        self._screen = screen
        self._x = x
        self._w = width
        self._first_y = first_y
        self._stride = stride
        self._row_h = row_h
        self._bottom = bottom
        self._count = count
        self._scroll_px = scroll_px
        self._width_fn = width_fn
        self._editing_attr = editing_attr
        self._scrollable = scrollable
        self._down_row: Optional[int] = None
        self._steps_done = 0
        self._mode = None  # None | "adjust" | "scroll"

    # -- geometry ------------------------------------------------------------

    def _row_width(self) -> int:
        return self._width_fn() if self._width_fn else self._w

    def row_at(self, y: int) -> Optional[int]:
        if y < self._first_y or y >= self._bottom:
            return None
        off = y - self._first_y + (self._scroll_px() if self._scroll_px else 0)
        if off < 0:
            return None
        i = off // self._stride
        if i >= self._count():
            return None
        return i

    def row_rect(self, i: int):
        scroll = self._scroll_px() if self._scroll_px else 0
        y = self._first_y + i * self._stride - scroll
        return (self._x, y, self._row_width(), self._row_h)

    # -- semantics -------------------------------------------------------------

    def _editing(self) -> bool:
        return bool(getattr(self._screen, self._editing_attr, False))

    def _emit(self, event) -> None:
        self._screen.handle_input(event)

    def _select(self, i: int) -> None:
        self._screen._selected_index = i
        ensure = getattr(self._screen, "_ensure_selected_visible", None)
        if ensure is not None:
            ensure()

    def pointer_feedback_rect(self, x: int, y: int):
        i = self.row_at(y)
        return None if i is None else self.row_rect(i)

    def hit_test(self, x: int, y: int) -> bool:
        return (self._x <= x < self._x + self._row_width()
                and self.row_at(y) is not None)

    def on_pointer_down(self, x: int, y: int) -> None:
        super().on_pointer_down(x, y)
        self._down_row = self.row_at(y)
        self._steps_done = 0
        self._mode = None

    def on_pointer_move(self, x: int, y: int, pressed: bool) -> bool:
        dx = x - self._down_x
        dy = y - self._down_y
        if self._mode is None:
            if abs(dx) <= TAP_SLOP and abs(dy) <= TAP_SLOP:
                return False
            if (abs(dx) >= abs(dy) and self._editing()
                    and self._down_row == self._screen._selected_index):
                self._mode = "adjust"
            elif abs(dy) > abs(dx) and self._scrollable and not self._editing():
                self._mode = "scroll"
            else:
                return False
        if self._mode == "adjust":
            steps = int(dx / DRAG_STEP_PX)
            right, left = IE.ROTATE_RIGHT, IE.ROTATE_LEFT
        else:
            # Finger up = content up = move selection down the list.
            steps = int(-dy / self._stride)
            right, left = IE.ROTATE_RIGHT, IE.ROTATE_LEFT
        while self._steps_done < steps:
            self._steps_done += 1
            self._emit(right)
        while self._steps_done > steps:
            self._steps_done -= 1
            self._emit(left)
        return True

    def on_click(self, x: int, y: int) -> bool:
        i = self.row_at(y)
        if i is None:
            return True
        if self._editing():
            if i == self._screen._selected_index:
                right = self._x + self._row_width()
                if x >= right - VALUE_INC_PX:
                    self._emit(IE.ROTATE_RIGHT)
                elif x >= right - VALUE_AREA_PX:
                    self._emit(IE.ROTATE_LEFT)
                else:
                    self._emit(IE.PRESS_LIGHT)
                return True
            self._emit(IE.PRESS_LIGHT)  # finish editing the other row
        if i >= self._count():
            return True
        self._select(i)
        self._emit(IE.PRESS_LIGHT)
        return True


def add_menu_list(screen, **kwargs) -> MenuListZone:
    """Create and register a :class:`MenuListZone` on ``screen``."""
    zone = MenuListZone(screen, **kwargs)
    screen.add_pointer_zone(zone)
    return zone


def add_paged_screen_pointer(screen, header_h: int, footer_h: int = 22) -> None:
    """Multi-page diagnostic screens (battery, engine detail).

    * footer BACK button (drawn at x=8, 80 px wide) -> BACK
    * footer "< ROTATE >" hint (right end): left half = ROTATE_LEFT
      (previous page), right half = ROTATE_RIGHT (next page)
    * horizontal swipe on the body -> next / previous page
    """
    w, h = screen.width, screen.height
    bar_y = h - footer_h
    screen.add_pointer_zone(PointerZone(
        (0, bar_y, 104, footer_h),
        on_click=lambda x, y: screen.pointer_back(),
        feedback=lambda x, y: (8, bar_y + 3, 80, footer_h - 6),
        name="back"))
    hint_x = w - 110
    screen.add_pointer_zone(PointerZone(
        (hint_x, bar_y, w - hint_x, footer_h),
        on_click=lambda x, y: screen.handle_input(
            IE.ROTATE_LEFT if x < hint_x + (w - hint_x) // 2 else IE.ROTATE_RIGHT),
        name="page-hint"))
    screen.add_swipe_zone(
        (0, header_h, w, bar_y - header_h),
        on_next=lambda: screen.handle_input(IE.ROTATE_RIGHT),
        on_prev=lambda: screen.handle_input(IE.ROTATE_LEFT),
        name="page-swipe")
