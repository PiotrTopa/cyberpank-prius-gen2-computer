"""
Base screen class.

All screens inherit from Screen.
"""

import pygame
from typing import Tuple, Optional, TYPE_CHECKING

from ..focus import FocusManager
from ..widgets.base import Widget

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
        
        # Navigation events are handled by focus manager
        if event == IE.ROTATE_LEFT:
            self.focus_manager.prev()
            return True
        elif event == IE.ROTATE_RIGHT:
            self.focus_manager.next()
            return True
        
        # Other events go to focused widget
        focused = self.focus_manager.focused_widget
        if focused:
            return focused.handle_input(event)
        
        return False
