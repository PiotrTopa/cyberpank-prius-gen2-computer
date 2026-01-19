"""
Focus management system.

Handles navigation focus between widgets using encoder rotation.
"""

from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .widgets.base import Widget


class FocusManager:
    """
    Manages focus navigation between focusable widgets.
    
    Handles cycling through widgets using encoder rotation
    and tracks the currently focused widget.
    """
    
    def __init__(self, widgets: Optional[List["Widget"]] = None):
        """
        Initialize the focus manager.
        
        Args:
            widgets: Initial list of focusable widgets
        """
        self._widgets: List["Widget"] = []
        self._focus_index: int = 0
        
        if widgets:
            for widget in widgets:
                self.add_widget(widget)
    
    def add_widget(self, widget: "Widget") -> None:
        """
        Add a widget to the focus chain.
        
        Args:
            widget: Widget to add
        """
        if widget.focusable:
            self._widgets.append(widget)
            # Set first widget as focused by default
            if len(self._widgets) == 1:
                widget.focused = True
    
    def remove_widget(self, widget: "Widget") -> None:
        """
        Remove a widget from the focus chain.
        
        Args:
            widget: Widget to remove
        """
        if widget in self._widgets:
            was_focused = widget.focused
            widget.focused = False
            
            index = self._widgets.index(widget)
            self._widgets.remove(widget)
            
            if was_focused and self._widgets:
                # Adjust focus index and focus next widget
                self._focus_index = min(index, len(self._widgets) - 1)
                self._widgets[self._focus_index].focused = True
    
    def clear(self) -> None:
        """Clear all widgets from focus management."""
        for widget in self._widgets:
            widget.focused = False
        self._widgets.clear()
        self._focus_index = 0
    
    @property
    def focused_widget(self) -> Optional["Widget"]:
        """Get the currently focused widget."""
        if self._widgets and 0 <= self._focus_index < len(self._widgets):
            return self._widgets[self._focus_index]
        return None
    
    @property
    def focus_index(self) -> int:
        """Get current focus index."""
        return self._focus_index
    
    @property
    def widget_count(self) -> int:
        """Get number of focusable widgets."""
        return len(self._widgets)
    
    def next(self) -> Optional["Widget"]:
        """
        Move focus to the next widget.
        
        Returns:
            The newly focused widget, or None if no widgets
        """
        if not self._widgets:
            return None
        
        # Unfocus current
        if self._widgets[self._focus_index]:
            self._widgets[self._focus_index].focused = False
        
        # Move to next (with wrap)
        self._focus_index = (self._focus_index + 1) % len(self._widgets)
        
        # Focus new
        self._widgets[self._focus_index].focused = True
        return self._widgets[self._focus_index]
    
    def prev(self) -> Optional["Widget"]:
        """
        Move focus to the previous widget.
        
        Returns:
            The newly focused widget, or None if no widgets
        """
        if not self._widgets:
            return None
        
        # Unfocus current
        if self._widgets[self._focus_index]:
            self._widgets[self._focus_index].focused = False
        
        # Move to previous (with wrap)
        self._focus_index = (self._focus_index - 1) % len(self._widgets)
        
        # Focus new
        self._widgets[self._focus_index].focused = True
        return self._widgets[self._focus_index]
    
    def focus_widget(self, widget: "Widget") -> bool:
        """
        Set focus directly to a specific widget.
        
        Args:
            widget: Widget to focus
        
        Returns:
            True if widget was found and focused
        """
        if widget not in self._widgets:
            return False
        
        # Unfocus current
        if self._widgets and self._focus_index < len(self._widgets):
            self._widgets[self._focus_index].focused = False
        
        # Focus new
        self._focus_index = self._widgets.index(widget)
        widget.focused = True
        return True
    
    def focus_by_index(self, index: int) -> bool:
        """
        Set focus to widget at specific index.
        
        Args:
            index: Index of widget to focus
        
        Returns:
            True if index was valid and widget focused
        """
        if not self._widgets or index < 0 or index >= len(self._widgets):
            return False
        
        # Unfocus current
        if self._focus_index < len(self._widgets):
            self._widgets[self._focus_index].focused = False
        
        # Focus new
        self._focus_index = index
        self._widgets[self._focus_index].focused = True
        return True
