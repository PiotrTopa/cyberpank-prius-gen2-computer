"""
Pointer model (mouse in dev mode, the MFD touch panel in production).

One gesture recogniser, :class:`PointerController`, turns a raw
down / move / up stream in native 480x240 coordinates into:

* **click**      - down + up on the same target (within ``CLICK_TOLERANCE``
  px of its rect) without moving more than ``TAP_SLOP`` px in between;
* **long press** - held still for ``LONG_PRESS_S`` (600 ms); fires once while
  the finger is still down and suppresses the click on release;
* **drag**       - a target that is ``draggable`` (slider, bar) receives every
  move; any other target may claim a move as a drag (swipe, list scroll) by
  returning True from ``on_pointer_move``. A claimed drag never clicks.

Targets are widgets (:class:`~.widgets.base.Widget` carries default pointer
hooks) or :class:`PointerZone` objects that custom-drawn screens register
for rows, buttons and swipe areas. Hit-testing and default semantics live in
:class:`~.screens.base.Screen`; this module has no knowledge of screens beyond
the small protocol used below.

Both input sources call the same four methods on the main (pygame) thread;
the touch stream from the backend arrives on the ZMQ thread and is queued by
:class:`TouchEventQueue` first. Nothing here allocates per frame: ``update``
is a handful of comparisons while idle, and the pressed-feedback rectangle
is a single pre-allocated ``pygame.Rect`` updated in place.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, Optional, Tuple

import pygame

logger = logging.getLogger(__name__)

# Gesture thresholds (native pixels / seconds).
TAP_SLOP = 10           # movement allowed before a press stops being a tap
CLICK_TOLERANCE = 8     # release may land this far outside the target rect
LONG_PRESS_S = 0.6      # hold time for PRESS_STRONG semantics
SWIPE_MIN_PX = 40       # horizontal travel for a swipe gesture
TOUCH_STALE_S = 1.0     # touch press with no frame for this long is cancelled

NATIVE_W = 480
NATIVE_H = 240


def _clamp(x: int, y: int) -> Tuple[int, int]:
    if x < 0:
        x = 0
    elif x >= NATIVE_W:
        x = NATIVE_W - 1
    if y < 0:
        y = 0
    elif y >= NATIVE_H:
        y = NATIVE_H - 1
    return x, y


class PointerZone:
    """A rectangular pointer target for custom-drawn screen areas.

    ``rect`` is ``(x, y, w, h)`` in native coordinates (a pygame.Rect is
    built once). All callbacks are optional:

    * ``on_click(x, y)``               - tap released inside the zone;
    * ``on_long_press(x, y) -> bool``  - held 600 ms; return True if handled
      (then the release does not click), False to fall back to click;
    * ``on_drag(x, y, dx, dy) -> bool`` - movement beyond the tap slop while
      pressed (dx/dy relative to the press point); return True to claim the
      gesture as a drag (no click on release);
    * ``on_release(x, y, dx, dy)``     - every release (after drag handling);
    * ``feedback(x, y) -> (x, y, w, h) | None`` - rect to highlight while
      pressed (default: the zone itself; None = no highlight);
    * ``enabled() -> bool``            - zone is live (default: always).
    * ``draw(surface)``                - optional chrome drawn by the screen
      after its own render (e.g. the BACK chip).
    """

    draggable = False
    focusable = False
    visible = True

    def __init__(
        self,
        rect: Tuple[int, int, int, int],
        on_click: Optional[Callable[[int, int], None]] = None,
        on_long_press: Optional[Callable[[int, int], bool]] = None,
        on_drag: Optional[Callable[[int, int, int, int], bool]] = None,
        on_release: Optional[Callable[[int, int, int, int], None]] = None,
        feedback: Optional[Callable[[int, int], Optional[tuple]]] = None,
        enabled: Optional[Callable[[], bool]] = None,
        draw: Optional[Callable[[pygame.Surface], None]] = None,
        name: str = "",
    ):
        self.prect = pygame.Rect(rect)
        self._on_click = on_click
        self._on_long_press = on_long_press
        self._on_drag = on_drag
        self._on_release = on_release
        self._feedback = feedback
        self._enabled = enabled
        self.draw = draw
        self.name = name
        self._down_x = 0
        self._down_y = 0

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"PointerZone({self.name or tuple(self.prect)})"

    # -- protocol used by Screen / PointerController -----------------------

    def is_enabled(self) -> bool:
        return self._enabled() if self._enabled else True

    def hit_test(self, x: int, y: int) -> bool:
        return self.prect.collidepoint(x, y) and self.is_enabled()

    def pointer_target_at(self, x: int, y: int):
        return self if self.hit_test(x, y) else None

    def pointer_bounds(self) -> pygame.Rect:
        return self.prect

    def pointer_feedback_rect(self, x: int, y: int):
        if self._feedback is not None:
            return self._feedback(x, y)
        return self.prect

    def on_pointer_down(self, x: int, y: int) -> None:
        self._down_x = x
        self._down_y = y

    def on_pointer_move(self, x: int, y: int, pressed: bool) -> bool:
        if self._on_drag is None:
            return False
        dx = x - self._down_x
        dy = y - self._down_y
        if abs(dx) <= TAP_SLOP and abs(dy) <= TAP_SLOP:
            return False
        return bool(self._on_drag(x, y, dx, dy))

    def on_pointer_up(self, x: int, y: int) -> None:
        if self._on_release is not None:
            self._on_release(x, y, x - self._down_x, y - self._down_y)

    def on_click(self, x: int, y: int) -> bool:
        if self._on_click is None:
            return False
        self._on_click(x, y)
        return True

    def on_long_press(self, x: int, y: int) -> bool:
        if self._on_long_press is None:
            return False
        return bool(self._on_long_press(x, y))


class PointerController:
    """Gesture recogniser shared by mouse and touch input.

    ``screen_getter`` returns the current screen; the screen supplies
    ``pointer_target_at(x, y)``, ``pointer_click(target, x, y)`` and
    ``pointer_long_press(target, x, y)`` (see Screen). ``clock`` returns
    seconds (monotonic) and is injectable for tests.
    """

    def __init__(
        self,
        screen_getter: Callable[[], object],
        clock: Callable[[], float] = time.monotonic,
    ):
        self._screen_getter = screen_getter
        self._clock = clock

        self.pressed = False
        self._screen = None
        self._target = None
        self._down_t = 0.0
        self._down_x = 0
        self._down_y = 0
        self._x = 0
        self._y = 0
        self._moved = False       # left the tap slop
        self._dragging = False    # target claimed the movement
        self._long_fired = False

        # Pressed feedback: one Rect, updated in place.
        self.feedback_rect = pygame.Rect(0, 0, 0, 0)
        self.feedback_visible = False

        # Last gesture, for logging/tests: ("click"|"long"|"drag"|"none", target)
        self.last_gesture: Tuple[str, object] = ("none", None)

    # -- raw stream ---------------------------------------------------------

    def down(self, x: int, y: int) -> None:
        if self.pressed:
            # Missing release (lost frame / second button): finish first.
            self.cancel()
        x, y = _clamp(int(x), int(y))
        screen = self._screen_getter()
        self.pressed = True
        self._screen = screen
        self._down_t = self._clock()
        self._down_x = self._x = x
        self._down_y = self._y = y
        self._moved = False
        self._dragging = False
        self._long_fired = False
        self._target = None
        if screen is None or not hasattr(screen, "pointer_target_at"):
            return
        target = screen.pointer_target_at(x, y)
        self._target = target
        if target is None:
            return
        target.on_pointer_down(x, y)
        fb = target.pointer_feedback_rect(x, y)
        if fb is not None:
            self.feedback_rect.update(fb)
            self.feedback_visible = True
        if getattr(target, "draggable", False):
            self._dragging = True

    def move(self, x: int, y: int) -> None:
        if not self.pressed:
            return  # hover: touch has none, mouse hover is ignored
        x, y = _clamp(int(x), int(y))
        self._x, self._y = x, y
        if self._stale_screen():
            return
        target = self._target
        if not self._moved:
            if abs(x - self._down_x) > TAP_SLOP or abs(y - self._down_y) > TAP_SLOP:
                self._moved = True
        if target is None:
            return
        claimed = target.on_pointer_move(x, y, True)
        if claimed and not self._dragging:
            self._dragging = True
        if self._moved and not self._dragging and self.feedback_visible:
            # Finger slid off a plain button: drop the highlight (no click).
            if not self._inside(target, x, y):
                self.feedback_visible = False

    def up(self, x: int, y: int) -> None:
        if not self.pressed:
            return
        x, y = _clamp(int(x), int(y))
        self._x, self._y = x, y
        target = self._target
        screen = self._screen
        long_fired, dragging, moved = self._long_fired, self._dragging, self._moved
        stale = self._stale_screen()
        self._reset()
        if long_fired:
            # The long press may have opened another screen: still a long press
            if not stale and target is not None:
                target.on_pointer_up(x, y)
            self.last_gesture = ("long", target)
            return
        if target is None or stale:
            self.last_gesture = ("none", target)
            return
        target.on_pointer_up(x, y)
        if dragging:
            self.last_gesture = ("drag", target)
        elif moved or not self._inside(target, x, y):
            self.last_gesture = ("none", target)
        else:
            self.last_gesture = ("click", target)
            screen.pointer_click(target, x, y)

    def cancel(self) -> None:
        """Abort the current gesture without clicking."""
        if self.pressed and self._target is not None and not self._stale_screen():
            self._target.on_pointer_up(self._x, self._y)
        self._reset()
        self.last_gesture = ("none", None)

    def update(self) -> None:
        """Per-frame tick: fires the long press. Cheap when idle."""
        if not self.pressed or self._long_fired or self._dragging or self._moved:
            return
        target = self._target
        if target is None:
            return
        if self._clock() - self._down_t < LONG_PRESS_S:
            return
        if self._stale_screen():
            self.cancel()
            return
        self._long_fired = True
        handled = self._screen.pointer_long_press(target, self._x, self._y)
        if handled:
            self.feedback_visible = False
        else:
            self._long_fired = False
            self._down_t = float("inf")  # don't retry every frame

    # -- helpers --------------------------------------------------------------

    def _stale_screen(self) -> bool:
        """The screen changed under the finger (pushed/popped): ignore."""
        if self._screen is not self._screen_getter():
            self._target = None
            self.feedback_visible = False
            return True
        return False

    @staticmethod
    def _inside(target, x: int, y: int) -> bool:
        r = target.pointer_bounds()
        t = CLICK_TOLERANCE
        return (r.x - t <= x < r.x + r.width + t) and (r.y - t <= y < r.y + r.height + t)

    def _reset(self) -> None:
        self.pressed = False
        self._target = None
        self._screen = None
        self.feedback_visible = False
        self._moved = False
        self._dragging = False
        self._long_fired = False


class TouchEventQueue:
    """Thread-safe hand-off of backend ``touch``/``button`` events.

    ``push(name, data)`` is called on the ZMQ client thread; the main loop
    calls ``drain(controller, on_button)`` once per frame. The touch frame
    stream (pressed=True at 10-15 Hz while down, pressed=False on release) is
    translated to down/move/up here; a press whose frames stop arriving for
    ``TOUCH_STALE_S`` is cancelled (never clicked) so a lost link cannot
    leave a phantom finger on the screen.
    """

    def __init__(self, maxlen: int = 256, clock: Callable[[], float] = time.monotonic):
        self._q: deque = deque(maxlen=maxlen)
        self._clock = clock
        self._touch_down = False
        self._last_frame_t = 0.0
        self._lock = threading.Lock()  # only guards counters
        self.dropped = 0

    def push(self, name: str, data: dict) -> None:
        if name != "touch" and name != "button":
            return
        if not isinstance(data, dict):
            return
        if len(self._q) == self._q.maxlen:
            with self._lock:
                self.dropped += 1
        self._q.append((name, data))

    def __len__(self) -> int:
        return len(self._q)

    @property
    def touch_active(self) -> bool:
        """A touch press is in progress (drain needed for stale detection)."""
        return self._touch_down

    def drain(self, controller: PointerController,
              on_button: Optional[Callable[[str, bool], None]] = None) -> int:
        n = 0
        q = self._q
        while q:
            try:
                name, data = q.popleft()
            except IndexError:
                break
            n += 1
            if name == "touch":
                self._feed_touch(controller, data)
            elif on_button is not None:
                key = data.get("key")
                if isinstance(key, str):
                    on_button(key, bool(data.get("pressed", True)))
        if (self._touch_down and n == 0
                and self._clock() - self._last_frame_t > TOUCH_STALE_S):
            logger.info("touch: no frame for %.1fs while pressed - cancelling",
                        TOUCH_STALE_S)
            self._touch_down = False
            controller.cancel()
        return n

    def _feed_touch(self, controller: PointerController, data: dict) -> None:
        try:
            x = int(data.get("x", 0))
            y = int(data.get("y", 0))
        except (TypeError, ValueError):
            return
        pressed = bool(data.get("pressed", False))
        self._last_frame_t = self._clock()
        if pressed:
            if not self._touch_down or not controller.pressed:
                self._touch_down = True
                controller.down(x, y)
            else:
                controller.move(x, y)
        elif self._touch_down:
            self._touch_down = False
            controller.up(x, y)
        # A release without a press (idle/repeated release frames) is ignored.
