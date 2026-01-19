"""
Cyberpunk color palette.

VFD-inspired colors for the retro-futuristic aesthetic.
All colors are RGB tuples. Use COLORS_ALPHA for colors with transparency.
"""

from typing import Dict, Tuple

# Type aliases
RGB = Tuple[int, int, int]
RGBA = Tuple[int, int, int, int]

# ─────────────────────────────────────────────────────────────────────────────
# Main Color Palette
# ─────────────────────────────────────────────────────────────────────────────

COLORS: Dict[str, RGB] = {
    # Backgrounds (dark, blue-tinted)
    "bg_dark": (8, 10, 15),           # Main background - near black
    "bg_panel": (12, 16, 24),         # Panel background
    "bg_frame": (18, 22, 32),         # Frame/widget background
    "bg_frame_focus": (25, 32, 45),   # Focused frame background
    
    # Primary Accent - Cyan (VFD-style)
    "cyan": (0, 255, 255),            # Bright cyan
    "cyan_bright": (128, 255, 255),   # Extra bright
    "cyan_mid": (0, 180, 200),        # Medium cyan
    "cyan_dim": (0, 100, 120),        # Dimmed cyan
    "cyan_dark": (0, 50, 60),         # Dark cyan
    
    # Secondary Accent - Magenta/Pink
    "magenta": (255, 0, 128),         # Hot pink/magenta
    "magenta_dim": (150, 0, 80),      # Dimmed magenta
    
    # Tertiary Accent - Orange (warnings, highlights)
    "orange": (255, 140, 0),          # Warm orange
    "orange_dim": (180, 100, 0),      # Dimmed orange
    "amber": (255, 180, 0),           # Amber/gold
    
    # Status Colors
    "active": (0, 255, 128),          # Active/enabled - green
    "inactive": (60, 70, 80),         # Inactive/disabled - gray
    "warning": (255, 200, 0),         # Warning - yellow
    "error": (255, 60, 60),           # Error - red
    
    # Text Colors
    "text_primary": (200, 220, 240),  # Main text - slightly blue
    "text_secondary": (100, 120, 140),# Secondary text - dimmed
    "text_highlight": (255, 255, 255),# Highlighted text - pure white
    "text_accent": (0, 255, 255),     # Accent text - cyan
    "text_value": (128, 255, 200),    # Value display - bright teal
    
    # Frame/Border Colors
    "border_normal": (40, 50, 65),    # Normal border
    "border_focus": (0, 200, 255),    # Focused border
    "border_active": (0, 255, 200),   # Active/selected border
    
    # Special
    "scanline": (0, 0, 0),            # Scanline overlay
    "glow": (0, 200, 255),            # Glow effect base
}

# Colors with alpha channel
COLORS_ALPHA: Dict[str, RGBA] = {
    "focus_glow": (0, 200, 255, 60),      # Focus glow overlay
    "scanline": (0, 0, 0, 20),            # Scanline alpha
    "overlay_dark": (0, 0, 0, 180),       # Dark overlay for modals
    "highlight": (255, 255, 255, 30),     # Subtle highlight
}


# ─────────────────────────────────────────────────────────────────────────────
# Color Utilities
# ─────────────────────────────────────────────────────────────────────────────

def dim_color(color: RGB, factor: float = 0.5) -> RGB:
    """
    Dim a color by a factor.
    
    Args:
        color: RGB color tuple
        factor: Dimming factor (0.0 = black, 1.0 = original)
    
    Returns:
        Dimmed RGB color
    """
    return tuple(int(c * factor) for c in color)


def brighten_color(color: RGB, factor: float = 1.5) -> RGB:
    """
    Brighten a color by a factor.
    
    Args:
        color: RGB color tuple
        factor: Brightening factor (1.0 = original, 2.0 = twice as bright)
    
    Returns:
        Brightened RGB color (clamped to 255)
    """
    return tuple(min(255, int(c * factor)) for c in color)


def lerp_color(color_a: RGB, color_b: RGB, t: float) -> RGB:
    """
    Linear interpolation between two colors.
    
    Args:
        color_a: Start color
        color_b: End color
        t: Interpolation factor (0.0 = color_a, 1.0 = color_b)
    
    Returns:
        Interpolated RGB color
    """
    t = max(0.0, min(1.0, t))
    return tuple(
        int(a + (b - a) * t) 
        for a, b in zip(color_a, color_b)
    )


def with_alpha(color: RGB, alpha: int) -> RGBA:
    """
    Add alpha channel to RGB color.
    
    Args:
        color: RGB color tuple
        alpha: Alpha value (0-255)
    
    Returns:
        RGBA color tuple
    """
    return (*color, alpha)
