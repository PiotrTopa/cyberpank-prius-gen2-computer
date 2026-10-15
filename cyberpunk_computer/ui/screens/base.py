"""
Base screen class.

All screens inherit from Screen.
"""

import pygame
from typing import Callable, Tuple, Optional, TYPE_CHECKING

from ..focus import FocusManager
from ..widgets.base import Widget
from ..pointer import PointerZone, SWIPE_MIN_PX
from ..colors import COLORS
from ..fonts import get_mono_font

if TYPE_CHECKING:
    from ...input.manager import InputEvent


class Screen:
    """
    Base class for all screens.
    
    A screen represents a full-screen view with its own
    widget tree and focus management.
    """
    
    def __init__(self, size: Tuple[int, int], app=None):
        """
        Initialize the screen.
        
        Args:
            size: Screen size (width, height)
            app: Reference to main application
        """
        self.width, self.height = size
        self.app = app
        
        # Focus management for this screen
        self.focus_manager = FocusManager()
        
        # All widgets on this screen
        self.widgets: list[Widget] = []

        # Pointer zones for custom-drawn areas (rows, footer buttons, swipe
        # areas). Hit-tested before widgets, last added wins.
        self.pointer_zones: list[PointerZone] = []
    
    def add_widget(self, widget: Widget) -> None:
        """
        Add a widget to the screen.
        
        Args:
            widget: Widget to add
        """
        self.widgets.append(widget)
        if widget.focusable:
            self.focus_manager.add_widget(widget)
    
    def on_enter(self) -> None:
        """Called when screen becomes active."""
        pass
    
    def on_exit(self) -> None:
        """Called when screen is removed."""
        pass
    
    def on_pause(self) -> None:
        """Called when screen is pushed behind another screen."""
        pass
    
    def on_resume(self) -> None:
        """Called when screen returns to foreground."""
        pass
    
    def update(self, dt: float) -> None:
        """
        Update all widgets.
        
        Args:
            dt: Delta time in seconds
        """
        for widget in self.widgets:
            widget.update(dt)
    
    def render(self, surface: pygame.Surface) -> None:
        """
        Render all widgets.
        
        Args:
            surface: Surface to render on
        """
        for widget in self.widgets:
            widget.render(surface)
    
    def handle_input(self, event: "InputEvent") -> bool:
        """
        Handle input event.
        
        Args:
            event: Input event
        
        Returns:
            True if event was consumed
        """
        from ...input.manager import InputEvent as IE
        
        # Try focused widget first (allows widgets to consume navigation when active)
        focused = self.focus_manager.focused_widget
        if focused:
            if focused.handle_input(event):
                return True

        # Navigation events are handled by focus manager if not consumed
        if event == IE.ROTATE_LEFT:
            self.focus_manager.prev()
            return True
        elif event == IE.ROTATE_RIGHT:
            self.focus_manager.next()
            return True
        
        return False

    # ------------------------------------------------------------------
    # Pointer model (mouse in dev mode, MFD touch in production)
    # ------------------------------------------------------------------

    _pointer_ready = False

    def setup_pointer(self) -> None:
        """Register pointer zones (BACK chip, rows, footer buttons).

        Called lazily before the first hit-test / overlay render, so screens
        only describe their geometry here. Override in subclasses."""

    def _ensure_pointer(self) -> None:
        if not self._pointer_ready:
            self._pointer_ready = True
            self.setup_pointer()

    def add_pointer_zone(self, zone: PointerZone) -> PointerZone:
        """Register a pointer zone (hit-tested before widgets)."""
        self.pointer_zones.append(zone)
        return zone

    def pointer_target_at(self, x: int, y: int):
        """Topmost pointer target at native (x, y): zones, then widgets."""
        self._ensure_pointer()
        for zone in reversed(self.pointer_zones):
            if zone.hit_test(x, y):
                return zone
        for widget in reversed(self.widgets):
            target = widget.pointer_target_at(x, y)
            if target is not None:
                return target
        return None

    def pointer_click(self, target, x: int, y: int) -> bool:
        """Tap on target: its own handler, else focus + PRESS_LIGHT."""
        from ...input.manager import InputEvent as IE
        if target.on_click(x, y):
            return True
        return self.pointer_activate(target, IE.PRESS_LIGHT)

    def pointer_long_press(self, target, x: int, y: int) -> bool:
        """Long press: its own handler, else focus + PRESS_STRONG."""
        from ...input.manager import InputEvent as IE
        if target.on_long_press(x, y):
            return True
        return self.pointer_activate(target, IE.PRESS_STRONG)

    def pointer_activate(self, target, event: "InputEvent") -> bool:
        """Focus a focusable widget and feed it an encoder press.

        Going through ``handle_input`` keeps touch and encoder semantics
        identical (screens' edit modes, timeouts, activity tracking).
        """
        if not getattr(target, "focusable", False):
            return False
        fm = self.focus_manager
        if not fm.focus_widget(target):
            return False
        fm.show_focus()
        return bool(self.handle_input(event))

    def inject_input(self, event: "InputEvent") -> None:
        """Send an encoder-equivalent event through the app (global BACK
        handling included) or straight to this screen without an app."""
        app = self.app
        if app is not None and hasattr(app, "inject_input"):
            app.inject_input(event)
        else:
            self.handle_input(event)

    def pointer_back(self) -> None:
        """BACK from a pointer affordance."""
        from ...input.manager import InputEvent as IE
        self.inject_input(IE.BACK)

    # Footer BACK chip (only on screens without another tappable way back).
    BACK_CHIP_RECT = (4, -19, 46, 16)   # y relative to the bottom edge

    _back_chip_label = None

    def add_back_chip(self) -> PointerZone:
        """Small "< BACK" chip at the left of the 22 px footer bar."""
        cx, cy, cw, ch = self.BACK_CHIP_RECT
        cy = self.height + cy
        chip = pygame.Rect(cx, cy, cw, ch)

        def draw(surface: pygame.Surface) -> None:
            label = Screen._back_chip_label
            if label is None:
                label = get_mono_font(10).render("< BACK", False, COLORS["cyan"])
                Screen._back_chip_label = label
            pygame.draw.rect(surface, COLORS["bg_panel"], chip)
            pygame.draw.rect(surface, COLORS["border_focus"], chip, 1)
            surface.blit(label, (chip.x + (chip.width - label.get_width()) // 2,
                                 chip.y + (chip.height - label.get_height()) // 2))

        # Hit area: the whole left end of the footer (bigger than the chip).
        return self.add_pointer_zone(PointerZone(
            (0, self.height - 22, cx + cw + 8, 22),
            on_click=lambda x, y: self.pointer_back(),
            feedback=lambda x, y: (chip.x, chip.y, chip.width, chip.height),
            draw=draw,
            name="back",
        ))

    def add_button_zone(self, rect, on_click: Callable[[int, int], None],
                        name: str = "", on_long_press=None, enabled=None) -> PointerZone:
        """A tappable button area (already drawn by the screen)."""
        return self.add_pointer_zone(PointerZone(
            rect, on_click=on_click, on_long_press=on_long_press,
            enabled=enabled, name=name))

    def add_swipe_zone(self, rect, on_next: Callable[[], None],
                       on_prev: Callable[[], None], name: str = "swipe") -> PointerZone:
        """Horizontal swipe area: finger moving left -> on_next, right ->
        on_prev, once per gesture. A tap here does nothing."""
        state = {"fired": False}

        def on_drag(x, y, dx, dy):
            if abs(dx) > abs(dy):
                if not state["fired"] and abs(dx) >= SWIPE_MIN_PX:
                    state["fired"] = True
                    (on_next if dx < 0 else on_prev)()
                return True
            return True  # vertical movement: still a drag, never a click

        def on_release(x, y, dx, dy):
            state["fired"] = False

        return self.add_pointer_zone(PointerZone(
            rect, on_drag=on_drag, on_release=on_release,
            feedback=lambda x, y: None, name=name))

    def render_pointer_overlay(self, surface: pygame.Surface) -> None:
        """Draw pointer chrome (BACK chip etc.) on top of the screen."""
        self._ensure_pointer()
        for zone in self.pointer_zones:
            if zone.draw is not None and zone.is_enabled():
                zone.draw(surface)
