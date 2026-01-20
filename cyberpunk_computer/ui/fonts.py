"""
Font management.

Handles font loading and provides consistent typography across the UI.

Font Hierarchy:
- Orbitron: Headers/titles (large decorative text)
- Terminus: Standard text (labels, values, general UI)
- 04B_03: Tiny text below 8px height (pixel-perfect at small sizes)
"""

import logging
import pygame
from typing import Dict, Optional
from pathlib import Path

# Set up logging for font operations
logger = logging.getLogger(__name__)

# Font size threshold - below this, use 04B pixel font
TINY_FONT_THRESHOLD = 8


class FontManager:
    """
    Manages fonts for the application.
    
    Provides cached access to fonts at different sizes with fallback
    to system fonts when custom fonts are not available.
    
    Font selection:
    - "title" / "header": Orbitron (decorative headers)
    - "mono" / "standard": Terminus (general purpose)
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
                - "mono"/"standard": Terminus monospace
                - "title"/"header": Interceptor Bold
                - "tiny"/"pixel": 04B pixel font
            bold: Use bold variant (only affects some fonts)
        
        Returns:
            Pygame font object
        """
        # Auto-select font based on size
        if font_name == "auto":
            font_name = "tiny" if size < TINY_FONT_THRESHOLD else "mono"
        
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
                        font = pygame.font.Font(str(font_path), size)
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
                font = pygame.font.SysFont(sys_font, size, bold=bold)
                if font:
                    return font
            except:
                continue
        
        # Ultimate fallback - pygame default
        return pygame.font.Font(None, size)


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
    """Get Interceptor Bold font for titles/headers."""
    return fonts.get_font(size, "title")


def get_mono_font(size: int) -> pygame.font.Font:
    """Get Terminus font for standard UI text."""
    return fonts.get_font(size, "mono")


def get_tiny_font(size: int = 8) -> pygame.font.Font:
    """Get 04B pixel font for tiny text."""
    return fonts.get_font(size, "tiny")


def get_icon_font(size: int = 14) -> pygame.font.Font:
    """Get Font Awesome icon font."""
    return fonts.get_font(size, "icons")
