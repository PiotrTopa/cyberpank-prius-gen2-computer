"""
Pagination Widget.

A control that displays multiple dots representing pages and allows
navigation between them when focused and activated.
"""

import pygame
from typing import Optional, Callable
from .base import Widget, Rect
from ..colors import COLORS, dim_color

class PaginationControl(Widget):
    """
    Pagination control with dots.
    
    Behavior:
    - Default: Shows current page as active dot.
    - Focused: Highlights the control.
    - Active (Enter pressed): Allows Changing page with Left/Right arrows.
    """
    
    def __init__(
        self, 
        rect: Rect, 
        num_pages: int = 2, 
        current_page: int = 0,
        on_change: Optional[Callable[[int], None]] = None
    ):
        """
        Initialize pagination control.
        
        Args:
            rect: Widget bounds
            num_pages: Total number of pages
            current_page: Initial active page (0-based)
            on_change: Callback when page changes
        """
        super().__init__(rect, focusable=True)
        self.num_pages = num_pages
        self.current_page = max(0, min(current_page, num_pages - 1))
        self._on_change = on_change
        self.active_edit = False  # True when "editing" (switching pages)
        
    def handle_input(self, event) -> bool:
        """Handle input events."""
        if not self.visible:
            return False
        
        # We need to import InputEvent locally to avoid circular imports or just use the values passed
        # Assuming event is an InputEvent enum member
        
        # Check by name/value since we might not have the Enum imported easily here
        # But actually we can just import it inside the method or rely on the fact that
        # the event passed IS the enum member.
        
        # Simple string check for robustness, or import if preferred.
        event_name = getattr(event, "name", str(event))
        
        if event_name == "PRESS_LIGHT": # Enter
            if self.focused:
                self.active_edit = not self.active_edit
                self._dirty = True
                return True
        
        if self.active_edit:
            if event_name == "ROTATE_LEFT": # Left / Up
                if self.current_page > 0:
                    self.current_page -= 1
                    if self._on_change:
                        self._on_change(self.current_page)
                    self._dirty = True
                return True
            elif event_name == "ROTATE_RIGHT": # Right / Down
                if self.current_page < self.num_pages - 1:
                    self.current_page += 1
                    if self._on_change:
                        self._on_change(self.current_page)
                    self._dirty = True
                return True
            # Consume BACK if active to exit edit mode?
            elif event_name == "BACK":
                 self.active_edit = False
                 self._dirty = True
                 return True
                 
        return False
        
    def render(self, surface: pygame.Surface) -> None:
        """Render the pagination dots."""
        if not self.visible:
            return

        # Determine colors
        if self.active_edit:
            base_color = COLORS["active"]
            active_color = COLORS["text_highlight"]
            focus_color = COLORS["active"]
        elif self.focused:
            base_color = COLORS["cyan_dim"]
            active_color = COLORS["active"]
            focus_color = COLORS["cyan_bright"]
        else:
            base_color = dim_color(COLORS["cyan_dim"], 0.5)
            active_color = COLORS["cyan_bright"]
            focus_color = None

        # Calculate layout
        # Dots are small circles
        dot_radius = 4
        spacing = 16
        total_width = (self.num_pages * dot_radius * 2) + ((self.num_pages - 1) * spacing)
        start_x = self.rect.x + (self.rect.width - total_width) // 2
        cy = self.rect.centery
        
        # Draw background/focus indication
        if self.focused:
             pygame.draw.rect(
                surface, 
                dim_color(focus_color, 0.2), 
                self.rect.to_pygame(),
                border_radius=4
            )
             pygame.draw.rect(
                surface, 
                focus_color, 
                self.rect.to_pygame(), 
                1,
                border_radius=4
            )
        
        for i in range(self.num_pages):
            cx = start_x + (i * (dot_radius * 2 + spacing)) + dot_radius
            
            if i == self.current_page:
                # Active page dot (filled)
                pygame.draw.circle(surface, active_color, (cx, cy), dot_radius)
                # If editing, maybe add an extra ring or glow
                if self.active_edit:
                     pygame.draw.circle(surface, COLORS["text_highlight"], (cx, cy), dot_radius + 2, 1)
            else:
                # Inactive page dot (outline or dim)
                pygame.draw.circle(surface, base_color, (cx, cy), dot_radius, 1)

