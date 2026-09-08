"""
Font management.

Handles font loading and provides consistent typography across the UI.

Font Hierarchy:
- Orbitron: Headers/titles (large decorative text)
- Terminus: Standard text at 14px and above
- 04B_03: Pixel font for all small text (below 14px)

Rendering policy (MFD hardware):
The Prius MFD is driven over an analog 15 kHz RGBS link (VGA666 resistor
DAC). Anti-aliased glyph edges produce intermediate-level pixels that the
panel samples with per-channel skew, which shows up as color fringing.
All fonts therefore render 1-bit (antialias off), regardless of what the
call site requests. Measured on this project's font files:
- terminus.ttf renders cleanly without AA only at 14px and even sizes above
- 04b03.ttf is pixel-exact at every size
- orbitron.ttf is acceptable without AA from 10px up
"""

import logging
import pygame
from typing import Dict, Optional
from pathlib import Path

# Set up logging for font operations
logger = logging.getLogger(__name__)

# Global antialias policy. Keep False for the analog MFD output; can be
# flipped for desktop development if smooth previews are ever wanted.
ANTIALIAS = False

# Font size threshold - below this, use 04B pixel font
TINY_FONT_THRESHOLD = 8

# Terminus TTF loses glyph strokes below this size when rendered 1-bit;
# smaller mono text falls back to the 04B pixel font.
TERMINUS_MIN_SIZE = 14

# Orbitron becomes illegible without AA below this size.
TITLE_MIN_SIZE = 10


class CrispFont(pygame.font.Font):
    """pygame Font that enforces the global ANTIALIAS policy.

    Call sites throughout the UI pass antialias=True; the MFD hardware
    needs 1-bit glyphs, so the flag is overridden here in one place.
    """

    def render(self, text, antialias=True, color=(255, 255, 255), background=None):
        return super().render(text, ANTIALIAS, color, background)


class PixelFont(CrispFont):
    """CrispFont for 04b03.ttf, which lacks a few glyphs the UI uses.

    Missing characters render as .notdef boxes, so they are substituted
    with the closest available equivalent (or dropped, for the degree
    sign) before rendering and measuring.
    """

    _SUBS = str.maketrans({
        "\xb0": "",     # ° — dropped: "84°C" -> "84C"
        "Δ": "d",    # Δ -> d (delta values)
        "⚠": "!",    # ⚠ -> !
        "Ω": "ohm",  # Ω -> ohm
    })

    def render(self, text, antialias=True, color=(255, 255, 255), background=None):
        if text:
            text = text.translate(self._SUBS)
        return super().render(text, antialias, color, background)

    def size(self, text):
        if text:
            text = text.translate(self._SUBS)
        return super().size(text)


class FontManager:
    """
    Manages fonts for the application.

    Provides cached access to fonts at different sizes with fallback
    to system fonts when custom fonts are not available.

    Font selection:
    - "title" / "header": Orbitron (decorative headers)
    - "mono" / "standard": Terminus >= 14px, 04B_03 below
    - "tiny": 04B_03 (pixel font for small sizes)
    - Auto-select: Uses size threshold to pick appropriate font
    """

    # Font size presets
    SIZE_TINY = 8
    SIZE_SMALL = 10
    SIZE_NORMAL = 12
    SIZE_LARGE = 14
    SIZE_XLARGE = 18
    SIZE_TITLE = 24

    _instance: Optional["FontManager"] = None
    _fonts: Dict[tuple, pygame.font.Font] = {}

    def __new__(cls):
        """Singleton pattern - only one font manager instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Initialize the font manager."""
        if self._initialized:
            return

        pygame.font.init()

        # Try to find asset directory
        self.asset_dir = self._find_asset_dir()
        logger.info(f"Font asset directory: {self.asset_dir}")

        # Font file names (in assets/fonts/)
        # Naming convention: lowercase, no spaces
        self.font_files = {
            # Standard UI text - Terminus monospace
            "mono": "terminus.ttf",
            "standard": "terminus.ttf",
            # Headers/titles - Orbitron decorative
            "title": "orbitron.ttf",
            "header": "orbitron.ttf",
            "display": "orbitron.ttf",
            # Tiny pixel font - 04B for small sizes
            "tiny": "04b03.ttf",
            "pixel": "04b03.ttf",
            # Icon font - Font Awesome 7 Free Solid
            "icons": "fontawesome.otf",
            "icon": "fontawesome.otf",
        }

        # Log available fonts
        if self.asset_dir:
            available = list(self.asset_dir.glob("*.*"))
            logger.info(f"Available font files: {[f.name for f in available]}")

        self._initialized = True
        self._load_errors: list[str] = []

    def _find_asset_dir(self) -> Optional[Path]:
        """Find the assets directory."""
        # Try relative to this file
        current = Path(__file__).parent.parent.parent
        assets = current / "assets" / "fonts"
        logger.debug(f"Looking for fonts in: {assets}")
        if assets.exists():
            logger.info(f"Found font directory: {assets}")
            return assets
        logger.warning(f"Font directory not found: {assets}")
        return None

    @staticmethod
    def _resolve(font_name: str, size: int) -> tuple:
        """Map a requested (font, size) to one that renders crisply 1-bit."""
        if font_name == "auto":
            font_name = "tiny" if size < TINY_FONT_THRESHOLD else "mono"

        if font_name in ("mono", "standard"):
            if size < TERMINUS_MIN_SIZE:
                # Terminus drops strokes below 14px without AA
                font_name = "tiny"
            elif size % 2:
                # Terminus only hints cleanly on even pixel sizes
                size -= 1
        elif font_name in ("title", "header", "display"):
            size = max(size, TITLE_MIN_SIZE)

        return font_name, size

    def get_font(
        self,
        size: int,
        font_name: str = "auto",
        bold: bool = False
    ) -> pygame.font.Font:
        """
        Get a font at the specified size.

        Args:
            size: Font size in pixels
            font_name: Font identifier:
                - "auto": Auto-select based on size (tiny for <8px, mono otherwise)
                - "mono"/"standard": Terminus monospace (04B below 14px)
                - "title"/"header": Orbitron
                - "tiny"/"pixel": 04B pixel font
            bold: Use bold variant (only affects some fonts)

        Returns:
            Pygame font object
        """
        font_name, size = self._resolve(font_name, size)

        cache_key = (font_name, size, bold)

        if cache_key in self._fonts:
            return self._fonts[cache_key]

        font = self._load_font(font_name, size, bold)
        self._fonts[cache_key] = font
        return font

    def _load_font(
        self,
        font_name: str,
        size: int,
        bold: bool
    ) -> pygame.font.Font:
        """Load a font from file or system."""
        # Try custom font file first
        if self.asset_dir:
            # Direct lookup by font_name
            if font_name in self.font_files:
                font_path = self.asset_dir / self.font_files[font_name]
                logger.debug(f"Trying to load font: {font_path}")
                if font_path.exists():
                    try:
                        cls = PixelFont if font_name in ("tiny", "pixel") else CrispFont
                        font = cls(str(font_path), size)
                        logger.info(f"Loaded font: {font_path.name} size={size}")
                        return font
                    except pygame.error as e:
                        error_msg = f"Failed to load {font_path}: {e}"
                        logger.error(error_msg)
                        if error_msg not in self._load_errors:
                            self._load_errors.append(error_msg)
                else:
                    logger.warning(f"Font file not found: {font_path}")

        # Fallback to system monospace font
        logger.info(f"Falling back to system font for {font_name} size={size}")
        system_fonts = [
            "terminus",
            "robotomono",
            "consolas",
            "monaco",
            "couriernew",
            "monospace"
        ]

        for sys_font in system_fonts:
            try:
                path = pygame.font.match_font(sys_font, bold=bold)
                if path:
                    return CrispFont(path, size)
            except Exception:
                continue

        # Ultimate fallback - pygame default
        return CrispFont(None, size)


# Global font manager instance
fonts = FontManager()


def get_font(size: int, font_name: str = "auto", bold: bool = False) -> pygame.font.Font:
    """
    Convenience function to get a font.

    Args:
        size: Font size in pixels
        font_name: Font type ("auto", "mono", "title", "tiny")
        bold: Use bold variant

    Returns:
        Pygame font object
    """
    return fonts.get_font(size, font_name, bold)


def get_title_font(size: int) -> pygame.font.Font:
    """Get Orbitron font for titles/headers."""
    return fonts.get_font(size, "title")


def get_mono_font(size: int) -> pygame.font.Font:
    """Get monospace font for standard UI text (Terminus / 04B below 14px)."""
    return fonts.get_font(size, "mono")


def get_tiny_font(size: int = 8) -> pygame.font.Font:
    """Get 04B pixel font for tiny text."""
    return fonts.get_font(size, "tiny")


def get_icon_font(size: int = 14) -> pygame.font.Font:
    """Get Font Awesome icon font."""
    return fonts.get_font(size, "icons")
