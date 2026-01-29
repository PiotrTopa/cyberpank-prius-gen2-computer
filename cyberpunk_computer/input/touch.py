"""
Touch Input Handler.

Handles touch/mouse events for UI interaction.
Provides infrastructure for touch screen support on the Prius display.
"""

import pygame
from typing import Optional, Callable, List, Tuple
from dataclasses import dataclass
from enum import Enum

from .manager import InputEvent


class TouchEventType(Enum):
    """Types of touch events."""
    DOWN = "down"       # Finger/mouse pressed
    UP = "up"           # Finger/mouse released
    MOVE = "move"       # Finger/mouse moved while pressed
    TAP = "tap"         # Quick press and release
    LONG_PRESS = "long_press"  # Held for extended time
    SWIPE = "swipe"     # Rapid movement


class SwipeDirection(Enum):
    """Direction of swipe gesture."""
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"


@dataclass
class TouchEvent:
    """Touch/mouse event data."""
    event_type: TouchEventType
    x: int
    y: int
    # For swipe events
    swipe_direction: Optional[SwipeDirection] = None
    swipe_distance: float = 0.0
    # For identifying multi-touch (future)
    touch_id: int = 0


@dataclass
class TouchZone:
    """
    Defines a touchable area on screen.
    
    Used for mapping touch regions to actions.
    """
    x: int
    y: int
    width: int
    height: int
    name: str
    callback: Optional[Callable[[TouchEvent], None]] = None
    
    def contains(self, x: int, y: int) -> bool:
        """Check if point is inside zone."""
        return (self.x <= x < self.x + self.width and
                self.y <= y < self.y + self.height)


class TouchHandler:
    """
    Handles touch/mouse input and gesture recognition.
    
    Converts raw pygame events to TouchEvents and dispatches
    to registered zones.
    """
    
    # Gesture thresholds
    TAP_MAX_TIME_MS = 200       # Max time for tap gesture
    LONG_PRESS_TIME_MS = 500   # Time to trigger long press
    SWIPE_MIN_DISTANCE = 30    # Minimum pixels for swipe
    SWIPE_MAX_TIME_MS = 300    # Max time for swipe gesture
    
    def __init__(self):
        """Initialize touch handler."""
        self._zones: List[TouchZone] = []
        
        # Touch state tracking
        self._touch_start_time: int = 0
        self._touch_start_pos: Optional[Tuple[int, int]] = None
        self._is_touching = False
        self._current_pos: Optional[Tuple[int, int]] = None
        
        # Global callbacks
        self._on_touch: Optional[Callable[[TouchEvent], None]] = None
        self._on_gesture: Optional[Callable[[TouchEvent], None]] = None
        
    def register_zone(self, zone: TouchZone) -> None:
        """Register a touchable zone."""
        self._zones.append(zone)
        
    def unregister_zone(self, name: str) -> None:
        """Unregister a zone by name."""
        self._zones = [z for z in self._zones if z.name != name]
        
    def clear_zones(self) -> None:
        """Clear all zones."""
        self._zones.clear()
        
    def set_on_touch(self, callback: Callable[[TouchEvent], None]) -> None:
        """Set global touch callback (called for all touch events)."""
        self._on_touch = callback
        
    def set_on_gesture(self, callback: Callable[[TouchEvent], None]) -> None:
        """Set gesture callback (called for taps, swipes, long presses)."""
        self._on_gesture = callback
        
    def handle_pygame_event(self, event: pygame.event.Event) -> Optional[TouchEvent]:
        """
        Process pygame event and return TouchEvent if applicable.
        
        Args:
            event: Pygame event
            
        Returns:
            TouchEvent if event was a touch/mouse event, None otherwise
        """
        touch_event = None
        
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            touch_event = self._handle_touch_down(event.pos)
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            touch_event = self._handle_touch_up(event.pos)
        elif event.type == pygame.MOUSEMOTION and self._is_touching:
            touch_event = self._handle_touch_move(event.pos)
        elif event.type == pygame.FINGERDOWN:
            # Real touch event (for touchscreen)
            x = int(event.x * pygame.display.get_surface().get_width())
            y = int(event.y * pygame.display.get_surface().get_height())
            touch_event = self._handle_touch_down((x, y))
        elif event.type == pygame.FINGERUP:
            x = int(event.x * pygame.display.get_surface().get_width())
            y = int(event.y * pygame.display.get_surface().get_height())
            touch_event = self._handle_touch_up((x, y))
        elif event.type == pygame.FINGERMOTION and self._is_touching:
            x = int(event.x * pygame.display.get_surface().get_width())
            y = int(event.y * pygame.display.get_surface().get_height())
            touch_event = self._handle_touch_move((x, y))
            
        if touch_event:
            self._dispatch_to_zones(touch_event)
            if self._on_touch:
                self._on_touch(touch_event)
                
        return touch_event
        
    def _handle_touch_down(self, pos: Tuple[int, int]) -> TouchEvent:
        """Handle touch/mouse press."""
        self._is_touching = True
        self._touch_start_time = pygame.time.get_ticks()
        self._touch_start_pos = pos
        self._current_pos = pos
        
        return TouchEvent(
            event_type=TouchEventType.DOWN,
            x=pos[0],
            y=pos[1]
        )
        
    def _handle_touch_up(self, pos: Tuple[int, int]) -> TouchEvent:
        """Handle touch/mouse release."""
        self._is_touching = False
        
        # Calculate gesture
        elapsed = pygame.time.get_ticks() - self._touch_start_time
        gesture_event = self._detect_gesture(pos, elapsed)
        
        # Basic up event
        up_event = TouchEvent(
            event_type=TouchEventType.UP,
            x=pos[0],
            y=pos[1]
        )
        
        # Return gesture if detected, otherwise up
        if gesture_event:
            if self._on_gesture:
                self._on_gesture(gesture_event)
            self._dispatch_to_zones(gesture_event)
            return gesture_event
            
        return up_event
        
    def _handle_touch_move(self, pos: Tuple[int, int]) -> TouchEvent:
        """Handle touch/mouse movement."""
        self._current_pos = pos
        
        return TouchEvent(
            event_type=TouchEventType.MOVE,
            x=pos[0],
            y=pos[1]
        )
        
    def _detect_gesture(self, end_pos: Tuple[int, int], elapsed_ms: int) -> Optional[TouchEvent]:
        """
        Detect gesture from touch sequence.
        
        Args:
            end_pos: Final touch position
            elapsed_ms: Time since touch start
            
        Returns:
            TouchEvent with gesture info, or None
        """
        if not self._touch_start_pos:
            return None
            
        dx = end_pos[0] - self._touch_start_pos[0]
        dy = end_pos[1] - self._touch_start_pos[1]
        distance = (dx * dx + dy * dy) ** 0.5
        
        # Check for tap (short press, minimal movement)
        if elapsed_ms < self.TAP_MAX_TIME_MS and distance < 15:
            return TouchEvent(
                event_type=TouchEventType.TAP,
                x=end_pos[0],
                y=end_pos[1]
            )
            
        # Check for long press (held in place)
        if elapsed_ms > self.LONG_PRESS_TIME_MS and distance < 15:
            return TouchEvent(
                event_type=TouchEventType.LONG_PRESS,
                x=end_pos[0],
                y=end_pos[1]
            )
            
        # Check for swipe (quick movement)
        if elapsed_ms < self.SWIPE_MAX_TIME_MS and distance >= self.SWIPE_MIN_DISTANCE:
            # Determine direction
            if abs(dx) > abs(dy):
                direction = SwipeDirection.RIGHT if dx > 0 else SwipeDirection.LEFT
            else:
                direction = SwipeDirection.DOWN if dy > 0 else SwipeDirection.UP
                
            return TouchEvent(
                event_type=TouchEventType.SWIPE,
                x=end_pos[0],
                y=end_pos[1],
                swipe_direction=direction,
                swipe_distance=distance
            )
            
        return None
        
    def _dispatch_to_zones(self, event: TouchEvent) -> None:
        """Dispatch event to matching zones."""
        for zone in self._zones:
            if zone.contains(event.x, event.y) and zone.callback:
                zone.callback(event)
                
    def update(self) -> Optional[TouchEvent]:
        """
        Check for time-based gestures (like long press).
        
        Call this every frame.
        
        Returns:
            TouchEvent if long press detected
        """
        if not self._is_touching or not self._touch_start_pos:
            return None
            
        elapsed = pygame.time.get_ticks() - self._touch_start_time
        
        if elapsed >= self.LONG_PRESS_TIME_MS:
            # Check minimal movement
            if self._current_pos:
                dx = self._current_pos[0] - self._touch_start_pos[0]
                dy = self._current_pos[1] - self._touch_start_pos[1]
                distance = (dx * dx + dy * dy) ** 0.5
                
                if distance < 15:
                    event = TouchEvent(
                        event_type=TouchEventType.LONG_PRESS,
                        x=self._current_pos[0],
                        y=self._current_pos[1]
                    )
                    
                    # Reset to prevent repeated events
                    self._touch_start_time = pygame.time.get_ticks()
                    
                    if self._on_gesture:
                        self._on_gesture(event)
                    self._dispatch_to_zones(event)
                    return event
                    
        return None


def touch_to_input_event(touch: TouchEvent) -> Optional[InputEvent]:
    """
    Convert touch gesture to InputEvent for compatibility.
    
    Args:
        touch: Touch event
        
    Returns:
        InputEvent if mapping exists
    """
    if touch.event_type == TouchEventType.TAP:
        return InputEvent.PRESS_LIGHT
    elif touch.event_type == TouchEventType.LONG_PRESS:
        return InputEvent.PRESS_STRONG
    elif touch.event_type == TouchEventType.SWIPE:
        if touch.swipe_direction == SwipeDirection.LEFT:
            return InputEvent.ROTATE_LEFT
        elif touch.swipe_direction == SwipeDirection.RIGHT:
            return InputEvent.ROTATE_RIGHT
        elif touch.swipe_direction == SwipeDirection.UP:
            return InputEvent.ROTATE_LEFT  # Map up to left
        elif touch.swipe_direction == SwipeDirection.DOWN:
            return InputEvent.ROTATE_RIGHT  # Map down to right
            
    return None
