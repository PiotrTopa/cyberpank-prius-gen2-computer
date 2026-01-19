"""
Frame widget.

A styled container with title, border, and content area.
Used for the main dashboard panels.
"""

import pygame
from typing import Optional, Callable, TYPE_CHECKING

from .base import Widget, Rect
from ..colors import COLORS, lerp_color
from ..fonts import get_font

if TYPE_CHECKING:
    from ...input.manager import InputEvent


class Frame(Widget):
    """
    A styled frame container with title and content.
    
    Used for the main dashboard panels like Audio, Climate, etc.
    Features cyberpunk-style borders and focus effects.
    """
    
    # Layout constants
    TITLE_HEIGHT = 16
    BORDER_WIDTH = 1
    PADDING = 4
    
    def __init__(
        self,
        rect: Rect,
        title: str = "",
        focusable: bool = True,
        on_select: Optional[Callable] = None,
        on_action: Optional[Callable] = None
    ):
        """
        Initialize the frame.
        
        Args:
            rect: Frame position and size
            title: Title text displayed at top
            focusable: Whether frame can receive focus
            on_select: Callback for light press (enter)
            on_action: Callback for strong press (space)
        """
        super().__init__(rect, focusable)
        
        self.title = title
        self.on_select = on_select
        self.on_action = on_action
        
        # Content area (inside padding and title)
        self._content_rect = self._calculate_content_rect()
        
        # Child widgets
        self._children: list[Widget] = []
    
    def _calculate_content_rect(self) -> Rect:
        """Calculate the content area rectangle."""
        return Rect(
            self.rect.x + self.BORDER_WIDTH + self.PADDING,
            self.rect.y + self.TITLE_HEIGHT + self.PADDING,
            self.rect.width - (self.BORDER_WIDTH + self.PADDING) * 2,
            self.rect.height - self.TITLE_HEIGHT - self.PADDING * 2 - self.BORDER_WIDTH
        )
    
    @property
    def content_rect(self) -> Rect:
        """Get the content area rectangle."""
        return self._content_rect
    
    def add_child(self, widget: Widget) -> None:
        """Add a child widget to the frame."""
        widget.parent = self
        self._children.append(widget)
    
    def update(self, dt: float) -> None:
        """Update frame and children."""
        super().update(dt)
        for child in self._children:
            child.update(dt)
    
    def render(self, surface: pygame.Surface) -> None:
        """Render the frame with cyberpunk styling."""
        if not self.visible:
            return
        
        # Calculate colors based on focus state
        focus_t = self._focus_anim
        
        bg_color = lerp_color(
            COLORS["bg_frame"],
            COLORS["bg_frame_focus"],
            focus_t
        )
        
        border_color = lerp_color(
            COLORS["border_normal"],
            COLORS["border_focus"],
            focus_t
        )
        
        title_color = lerp_color(
            COLORS["text_secondary"],
            COLORS["cyan"],
            focus_t
        )
        
        # Draw background
        pygame.draw.rect(
            surface,
            bg_color,
            self.rect.to_pygame()
        )
        
        # Draw border
        pygame.draw.rect(
            surface,
            border_color,
            self.rect.to_pygame(),
            self.BORDER_WIDTH
        )
        
        # Draw corner accents when focused
        if focus_t > 0.1:
            self._draw_corner_accents(surface, border_color, focus_t)
        
        # Draw title bar line
        title_line_y = self.rect.y + self.TITLE_HEIGHT
        pygame.draw.line(
            surface,
            border_color,
            (self.rect.x, title_line_y),
            (self.rect.right - 1, title_line_y),
            1
        )
        
        # Draw title text
        if self.title:
            font = get_font(10, bold=self.focused)
            title_surface = font.render(self.title.upper(), True, title_color)
            title_x = self.rect.x + self.PADDING + 2
            title_y = self.rect.y + (self.TITLE_HEIGHT - title_surface.get_height()) // 2
            surface.blit(title_surface, (title_x, title_y))
        
        # Draw focus indicator (small triangle or dot)
        if focus_t > 0.5:
            self._draw_focus_indicator(surface, focus_t)
        
        # Render children
        for child in self._children:
            child.render(surface)
    
    def _draw_corner_accents(
        self, 
        surface: pygame.Surface, 
        color: tuple,
        intensity: float
    ) -> None:
        """Draw small corner accents for focused state."""
        length = 6
        
        # Modify color intensity
        accent_color = lerp_color(color, COLORS["cyan"], intensity * 0.5)
        
        # Top-left
        pygame.draw.line(surface, accent_color,
            (self.rect.x, self.rect.y),
            (self.rect.x + length, self.rect.y), 1)
        pygame.draw.line(surface, accent_color,
            (self.rect.x, self.rect.y),
            (self.rect.x, self.rect.y + length), 1)
        
        # Top-right
        pygame.draw.line(surface, accent_color,
            (self.rect.right - 1, self.rect.y),
            (self.rect.right - length - 1, self.rect.y), 1)
        pygame.draw.line(surface, accent_color,
            (self.rect.right - 1, self.rect.y),
            (self.rect.right - 1, self.rect.y + length), 1)
        
        # Bottom-left
        pygame.draw.line(surface, accent_color,
            (self.rect.x, self.rect.bottom - 1),
            (self.rect.x + length, self.rect.bottom - 1), 1)
        pygame.draw.line(surface, accent_color,
            (self.rect.x, self.rect.bottom - 1),
            (self.rect.x, self.rect.bottom - length - 1), 1)
        
        # Bottom-right
        pygame.draw.line(surface, accent_color,
            (self.rect.right - 1, self.rect.bottom - 1),
            (self.rect.right - length - 1, self.rect.bottom - 1), 1)
        pygame.draw.line(surface, accent_color,
            (self.rect.right - 1, self.rect.bottom - 1),
            (self.rect.right - 1, self.rect.bottom - length - 1), 1)
    
    def _draw_focus_indicator(
        self, 
        surface: pygame.Surface,
        intensity: float
    ) -> None:
        """Draw a small focus indicator next to title."""
        # Small glowing dot
        x = self.rect.right - self.PADDING - 4
        y = self.rect.y + self.TITLE_HEIGHT // 2
        
        color = lerp_color(COLORS["cyan_dim"], COLORS["cyan"], intensity)
        pygame.draw.circle(surface, color, (x, y), 2)
    
    def handle_input(self, event: "InputEvent") -> bool:
        """Handle input events."""
        from ...input.manager import InputEvent as IE
        
        if event == IE.PRESS_LIGHT:
            if self.on_select:
                self.on_select()
                return True
        elif event == IE.PRESS_STRONG:
            if self.on_action:
                self.on_action()
                return True
        
        return False
