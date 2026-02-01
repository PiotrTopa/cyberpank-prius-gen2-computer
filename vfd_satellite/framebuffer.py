"""
VFD Framebuffer - Portable binary framebuffer.

Uses only basic drawing primitives compatible with RP2040 MicroPython.
All pixels are binary (on/off) - no shading.
"""

from typing import List


# VFD Display dimensions (actual hardware)
VFD_WIDTH = 256
VFD_HEIGHT = 48


class VFDFramebuffer:
    """
    Binary framebuffer for VFD display.
    
    Uses only basic drawing primitives compatible with RP2040 MicroPython.
    All pixels are binary (on/off) - no shading.
    """
    
    def __init__(self, width: int = VFD_WIDTH, height: int = VFD_HEIGHT):
        """Initialize framebuffer with given dimensions."""
        self.width = width
        self.height = height
        # Binary pixel buffer: 0 = off, 1 = on
        self._buffer: List[List[int]] = [[0] * width for _ in range(height)]
    
    def clear(self) -> None:
        """Clear the framebuffer (all pixels off)."""
        for y in range(self.height):
            for x in range(self.width):
                self._buffer[y][x] = 0
    
    def set_pixel(self, x: int, y: int, on: bool = True) -> None:
        """Set a single pixel on or off."""
        if 0 <= x < self.width and 0 <= y < self.height:
            self._buffer[y][x] = 1 if on else 0
    
    def get_pixel(self, x: int, y: int) -> bool:
        """Get pixel state at position."""
        if 0 <= x < self.width and 0 <= y < self.height:
            return self._buffer[y][x] == 1
        return False
    
    def draw_hline(self, x: int, y: int, length: int, on: bool = True) -> None:
        """Draw a horizontal line."""
        for i in range(length):
            self.set_pixel(x + i, y, on)
    
    def draw_vline(self, x: int, y: int, length: int, on: bool = True) -> None:
        """Draw a vertical line."""
        for i in range(length):
            self.set_pixel(x, y + i, on)
    
    def draw_line(self, x0: int, y0: int, x1: int, y1: int, on: bool = True) -> None:
        """Draw a line using Bresenham's algorithm."""
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        
        while True:
            self.set_pixel(x0, y0, on)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy
    
    def draw_rect(self, x: int, y: int, w: int, h: int, on: bool = True) -> None:
        """Draw a rectangle outline."""
        self.draw_hline(x, y, w, on)              # Top
        self.draw_hline(x, y + h - 1, w, on)      # Bottom
        self.draw_vline(x, y, h, on)              # Left
        self.draw_vline(x + w - 1, y, h, on)      # Right
    
    def fill_rect(self, x: int, y: int, w: int, h: int, on: bool = True) -> None:
        """Fill a rectangle."""
        for dy in range(h):
            self.draw_hline(x, y + dy, w, on)
    
    def draw_char_3x5(self, x: int, y: int, char: str, on: bool = True) -> None:
        """Draw a single character using 3x5 pixel font."""
        font_3x5 = {
            '0': [0b111, 0b101, 0b101, 0b101, 0b111],
            '1': [0b010, 0b110, 0b010, 0b010, 0b111],
            '2': [0b111, 0b001, 0b111, 0b100, 0b111],
            '3': [0b111, 0b001, 0b111, 0b001, 0b111],
            '4': [0b101, 0b101, 0b111, 0b001, 0b001],
            '5': [0b111, 0b100, 0b111, 0b001, 0b111],
            '6': [0b111, 0b100, 0b111, 0b101, 0b111],
            '7': [0b111, 0b001, 0b010, 0b010, 0b010],
            '8': [0b111, 0b101, 0b111, 0b101, 0b111],
            '9': [0b111, 0b101, 0b111, 0b001, 0b111],
            '-': [0b000, 0b000, 0b111, 0b000, 0b000],
            '+': [0b000, 0b010, 0b111, 0b010, 0b000],
            '.': [0b000, 0b000, 0b000, 0b000, 0b010],
            ' ': [0b000, 0b000, 0b000, 0b000, 0b000],
            'O': [0b111, 0b101, 0b101, 0b101, 0b111],
            'F': [0b111, 0b100, 0b110, 0b100, 0b100],
            'P': [0b110, 0b101, 0b110, 0b100, 0b100],
            'T': [0b111, 0b010, 0b010, 0b010, 0b010],
            'R': [0b110, 0b101, 0b110, 0b101, 0b101],
            'L': [0b100, 0b100, 0b100, 0b100, 0b111],
            'G': [0b111, 0b100, 0b101, 0b101, 0b111],
            'B': [0b110, 0b101, 0b110, 0b101, 0b110],
        }
        
        if char in font_3x5:
            rows = font_3x5[char]
            for row_idx, row in enumerate(rows):
                for col in range(3):
                    if row & (1 << (2 - col)):
                        self.set_pixel(x + col, y + row_idx, on)
    
    def draw_text_3x5(self, x: int, y: int, text: str, on: bool = True) -> int:
        """
        Draw text using 3x5 font.
        
        Returns: x position after the text (for continuation).
        """
        cursor_x = x
        for char in text.upper():
            self.draw_char_3x5(cursor_x, y, char, on)
            cursor_x += 4  # 3 pixels + 1 spacing
        return cursor_x
    
    def draw_text_3x5_xor(self, x: int, y: int, text: str) -> int:
        """Draw text using 3x5 font with XOR mode (inverts pixels)."""
        font_3x5 = {
            '0': [0b111, 0b101, 0b101, 0b101, 0b111],
            '1': [0b010, 0b110, 0b010, 0b010, 0b111],
            '2': [0b111, 0b001, 0b111, 0b100, 0b111],
            '3': [0b111, 0b001, 0b111, 0b001, 0b111],
            '4': [0b101, 0b101, 0b111, 0b001, 0b001],
            '5': [0b111, 0b100, 0b111, 0b001, 0b111],
            '6': [0b111, 0b100, 0b111, 0b101, 0b111],
            '7': [0b111, 0b001, 0b010, 0b010, 0b010],
            '8': [0b111, 0b101, 0b111, 0b101, 0b111],
            '9': [0b111, 0b101, 0b111, 0b001, 0b111],
            '-': [0b000, 0b000, 0b111, 0b000, 0b000],
            '+': [0b000, 0b010, 0b111, 0b010, 0b000],
            '.': [0b000, 0b000, 0b000, 0b000, 0b010],
            ' ': [0b000, 0b000, 0b000, 0b000, 0b000],
        }
        
        cursor_x = x
        for char in text.upper():
            if char in font_3x5:
                rows = font_3x5[char]
                for row_idx, row in enumerate(rows):
                    for col in range(3):
                        if row & (1 << (2 - col)):
                            px, py = cursor_x + col, y + row_idx
                            if 0 <= px < self.width and 0 <= py < self.height:
                                self._buffer[py][px] = 1 - self._buffer[py][px]
            cursor_x += 4
        return cursor_x
    
    def draw_icon(self, x: int, y: int, icon: List[List[int]], on: bool = True) -> None:
        """Draw an icon at the specified position."""
        for row_idx, row in enumerate(icon):
            for col_idx, pixel in enumerate(row):
                if pixel:
                    self.set_pixel(x + col_idx, y + row_idx, on)
    
    def draw_icon_centered(self, cx: int, cy: int, icon: List[List[int]], on: bool = True) -> None:
        """Draw an icon centered at the specified position."""
        icon_h = len(icon)
        icon_w = len(icon[0]) if icon else 0
        x = cx - icon_w // 2
        y = cy - icon_h // 2
        self.draw_icon(x, y, icon, on)
