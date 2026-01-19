"""
Base widget class.

All UI components inherit from Widget.
"""

import pygame
from dataclasses import dataclass
from typing import Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from ...input.manager import InputEvent


@dataclass
class Rect:
    """Simple rectangle for widget positioning."""
    x: int
    y: int
    width: int
    height: int
    
    @property
    def right(self) -> int:
        """Get right edge x coordinate."""
        return self.x + self.width
    
    @property
    def bottom(self) -> int:
        """Get bottom edge y coordinate."""
        return self.y + self.height
    
    @property
    def center(self) -> Tuple[int, int]:
        """Get center point."""
        return (self.x + self.width // 2, self.y + self.height // 2)
    
    @property
    def size(self) -> Tuple[int, int]:
        """Get size as tuple."""
        return (self.width, self.height)
    
    @property
    def topleft(self) -> Tuple[int, int]:
        """Get top-left corner."""
        return (self.x, self.y)
    
    def to_pygame(self) -> pygame.Rect:
        """Convert to pygame Rect."""
        return pygame.Rect(self.x, self.y, self.width, self.height)
    
    def contains(self, x: int, y: int) -> bool:
        """Check if point is inside rectangle."""
        return (self.x <= x < self.right and 
                self.y <= y < self.bottom)
    
    def inset(self, amount: int) -> "Rect":
        """Return a new rect inset by amount on all sides."""
        return Rect(
            self.x + amount,
            self.y + amount,
            self.width - amount * 2,
            self.height - amount * 2
        )


class Widget:
    """
    Base class for all UI components.
    
    Provides common functionality for positioning, focus management,
    rendering, and input handling.
    """
    
    def __init__(
        self, 
        rect: Rect,
        focusable: bool = True,
        visible: bool = True
    ):
        """
        Initialize the widget.
        
        Args:
            rect: Widget position and size
            focusable: Whether widget can receive focus
            visible: Whether widget is rendered
        """
        self.rect = rect
        self.focusable = focusable
        self.visible = visible
        
        # State
        self._focused = False
        self._active = False  # Widget is in "editing" mode
        self._dirty = True    # Needs redraw
        
        # Animation state
        self._focus_anim = 0.0  # 0 = unfocused, 1 = focused
        
        # Parent reference (set by container)
        self.parent: Optional["Widget"] = None
    
    @property
    def focused(self) -> bool:
        """Check if widget has focus."""
        return self._focused
    
    @focused.setter
    def focused(self, value: bool) -> None:
        """Set focus state."""
        if self._focused != value:
            self._focused = value
            self._dirty = True
            self.on_focus_changed(value)
    
    @property
    def active(self) -> bool:
        """Check if widget is in active/editing mode."""
        return self._active
    
    @active.setter
    def active(self, value: bool) -> None:
        """Set active state."""
        if self._active != value:
            self._active = value
            self._dirty = True
            self.on_active_changed(value)
    
    def on_focus_changed(self, focused: bool) -> None:
        """
        Called when focus state changes.
        
        Override in subclass for custom behavior.
        
        Args:
            focused: New focus state
        """
        pass
    
    def on_active_changed(self, active: bool) -> None:
        """
        Called when active state changes.
        
        Override in subclass for custom behavior.
        
        Args:
            active: New active state
        """
        pass
    
    def update(self, dt: float) -> None:
        """
        Update widget state.
        
        Called every frame. Override for animations, etc.
        
        Args:
            dt: Delta time in seconds
        """
        # Animate focus transition
        target = 1.0 if self._focused else 0.0
        if self._focus_anim != target:
            speed = 8.0  # Animation speed
            if self._focus_anim < target:
                self._focus_anim = min(target, self._focus_anim + dt * speed)
            else:
                self._focus_anim = max(target, self._focus_anim - dt * speed)
            self._dirty = True
    
    def render(self, surface: pygame.Surface) -> None:
        """
        Render the widget.
        
        Override in subclass for custom rendering.
        
        Args:
            surface: Surface to render on
        """
        pass
    
    def handle_input(self, event: "InputEvent") -> bool:
        """
        Handle an input event.
        
        Override in subclass for custom input handling.
        
        Args:
            event: Input event to handle
        
        Returns:
            True if event was consumed
        """
        return False
    
    def get_encoder_config(self) -> Optional[dict]:
        """
        Get encoder configuration for this widget.
        
        Override in subclass to provide encoder settings
        when this widget is focused.
        
        Returns:
            Encoder configuration dict, or None for default
        """
        return None
    
    def mark_dirty(self) -> None:
        """Mark widget as needing redraw."""
        self._dirty = True
