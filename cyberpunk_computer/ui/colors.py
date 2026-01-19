"""
Cyberpunk color palette.

VFD-inspired colors for the retro-futuristic aesthetic.
Supports multiple color themes (VFD, Amber, etc.)
"""

from typing import Dict, Tuple
from dataclasses import dataclass
from enum import Enum

# Type aliases
RGB = Tuple[int, int, int]
RGBA = Tuple[int, int, int, int]


class Theme(Enum):
    """Available color themes."""
    VFD = "vfd"           # Classic VFD cyan/teal
    AMBER = "amber"       # Amber/orange monochrome
    SYNTHWAVE = "synthwave"  # Pink/purple synthwave


@dataclass
class ColorPalette:
    """Color palette definition for a theme."""
    
    # Base colors from the palette
    bg_darkest: RGB       # Darkest background
    bg_dark: RGB          # Dark background
    bg_mid: RGB           # Mid-tone background
    
    # Accent colors
    accent_bright: RGB    # Brightest accent
    accent_mid: RGB       # Medium accent
    accent_dim: RGB       # Dimmed accent
    accent_dark: RGB      # Very dark accent
    
    # Secondary accent
    secondary_bright: RGB
    secondary_mid: RGB
    
    # Warm tones
    warm_bright: RGB      # Yellow/amber highlights
    warm_mid: RGB
    
    # Alert/status
    alert_bright: RGB     # Red/warning
    alert_mid: RGB
    
    # Additional
    highlight: RGB        # Pure highlights
    inactive: RGB         # Inactive/disabled


# ─────────────────────────────────────────────────────────────────────────────
# Theme Definitions
# ─────────────────────────────────────────────────────────────────────────────

# VFD Theme - Classic cyan/teal vacuum fluorescent display
VFD_PALETTE = ColorPalette(
    # Backgrounds
    bg_darkest=(0, 0, 0),
    bg_dark=(32, 17, 39),
    bg_mid=(27, 30, 52),
    
    # VFD Cyan/Teal accents
    accent_bright=(0, 255, 204),      # VFD jasny
    accent_mid=(0, 168, 150),         # VFD przydymiony
    accent_dim=(53, 93, 104),
    accent_dark=(0, 51, 51),          # VFD bardzo ciemny
    
    # Secondary - teal/green
    secondary_bright=(148, 197, 172),
    secondary_mid=(106, 175, 157),
    
    # Warm highlights
    warm_bright=(255, 235, 153),
    warm_mid=(255, 194, 122),
    
    # Alerts - red
    alert_bright=(255, 36, 36),       # red triangle of death
    alert_mid=(217, 98, 107),
    
    # Others
    highlight=(255, 255, 255),
    inactive=(53, 93, 104),
)

# Amber Theme - Classic amber monochrome
AMBER_PALETTE = ColorPalette(
    bg_darkest=(0, 0, 0),
    bg_dark=(20, 12, 5),
    bg_mid=(35, 25, 15),
    
    accent_bright=(255, 183, 51),     # amber shade
    accent_mid=(255, 159, 0),         # amber
    accent_dim=(180, 100, 30),
    accent_dark=(80, 50, 20),
    
    secondary_bright=(255, 194, 122),
    secondary_mid=(236, 154, 109),
    
    warm_bright=(255, 235, 153),
    warm_mid=(255, 194, 122),
    
    alert_bright=(255, 77, 77),       # red shade
    alert_mid=(217, 98, 107),
    
    highlight=(255, 255, 255),
    inactive=(100, 80, 60),
)

# Synthwave Theme - Pink/purple
SYNTHWAVE_PALETTE = ColorPalette(
    bg_darkest=(0, 0, 0),
    bg_dark=(32, 20, 51),
    bg_mid=(53, 30, 80),
    
    accent_bright=(255, 100, 200),
    accent_mid=(194, 75, 110),
    accent_dim=(167, 49, 105),
    accent_dark=(80, 30, 60),
    
    secondary_bright=(0, 255, 204),
    secondary_mid=(0, 168, 150),
    
    warm_bright=(255, 235, 153),
    warm_mid=(255, 194, 122),
    
    alert_bright=(255, 36, 36),
    alert_mid=(217, 98, 107),
    
    highlight=(255, 255, 255),
    inactive=(80, 60, 90),
)

# Available themes
THEMES = {
    Theme.VFD: VFD_PALETTE,
    Theme.AMBER: AMBER_PALETTE,
    Theme.SYNTHWAVE: SYNTHWAVE_PALETTE,
}

# Current active theme
_current_theme = Theme.VFD


def set_theme(theme: Theme) -> None:
    """Set the active color theme."""
    global _current_theme, COLORS
    _current_theme = theme
    COLORS = _build_color_dict(THEMES[theme])


def get_theme() -> Theme:
    """Get current theme."""
    return _current_theme


def _build_color_dict(palette: ColorPalette) -> Dict[str, RGB]:
    """Build the COLORS dict from a palette."""
    return {
        # Backgrounds
        "bg_dark": palette.bg_darkest,
        "bg_panel": palette.bg_dark,
        "bg_frame": palette.bg_dark,
        "bg_frame_focus": palette.bg_mid,
        
        # Primary accent
        "cyan": palette.accent_bright,        # Keep name for compatibility
        "cyan_bright": palette.accent_bright,
        "cyan_mid": palette.accent_mid,
        "cyan_dim": palette.accent_dim,
        "cyan_dark": palette.accent_dark,
        
        # Secondary
        "magenta": palette.secondary_bright,
        "magenta_dim": palette.secondary_mid,
        
        # Warm colors
        "orange": palette.warm_mid,
        "orange_dim": palette.warm_mid,
        "amber": palette.warm_bright,
        
        # Status
        "active": palette.warm_bright,        # Changed from green to warm
        "inactive": palette.inactive,
        "warning": palette.warm_bright,
        "error": palette.alert_bright,
        
        # Text
        "text_primary": palette.secondary_bright,
        "text_secondary": palette.accent_dim,
        "text_highlight": palette.highlight,
        "text_accent": palette.accent_bright,
        "text_value": palette.accent_bright,
        
        # Borders
        "border_normal": palette.accent_dark,
        "border_focus": palette.accent_mid,
        "border_active": palette.warm_mid,    # Changed to warm/amber
        
        # Effects
        "scanline": (0, 0, 0),
        "glow": palette.accent_mid,
    }


# Initialize default COLORS dict
COLORS: Dict[str, RGB] = _build_color_dict(VFD_PALETTE)

# Colors with alpha channel
COLORS_ALPHA: Dict[str, RGBA] = {
    "focus_glow": (0, 200, 255, 60),
    "scanline": (0, 0, 0, 20),
    "overlay_dark": (0, 0, 0, 180),
    "highlight": (255, 255, 255, 30),
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
