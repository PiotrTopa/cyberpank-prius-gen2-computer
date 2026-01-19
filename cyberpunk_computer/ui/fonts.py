"""
Font management.

Handles font loading and provides consistent typography across the UI.
"""

import pygame
from typing import Dict, Optional
from pathlib import Path


class FontManager:
    """
    Manages fonts for the application.
    
    Provides cached access to fonts at different sizes with fallback
    to system fonts when custom fonts are not available.
    """
    
    # Font size presets
    SIZE_TINY = 10
    SIZE_SMALL = 12
    SIZE_NORMAL = 14
    SIZE_LARGE = 18
    SIZE_XLARGE = 24
    SIZE_TITLE = 32
    
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
        
        # Font file names (would be in assets/fonts/)
        self.font_files = {
            "mono": "RobotoMono-Regular.ttf",
            "mono_bold": "RobotoMono-Bold.ttf",
            "display": "ShareTechMono-Regular.ttf",
        }
        
        self._initialized = True
    
    def _find_asset_dir(self) -> Optional[Path]:
        """Find the assets directory."""
        # Try relative to this file
        current = Path(__file__).parent.parent.parent
        assets = current / "assets" / "fonts"
        if assets.exists():
            return assets
        return None
    
    def get_font(
        self, 
        size: int, 
        font_name: str = "mono",
        bold: bool = False
    ) -> pygame.font.Font:
        """
        Get a font at the specified size.
        
        Args:
            size: Font size in pixels
            font_name: Font identifier ('mono', 'display')
            bold: Use bold variant
        
        Returns:
            Pygame font object
        """
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
            key = f"{font_name}_bold" if bold else font_name
            if key in self.font_files:
                font_path = self.asset_dir / self.font_files[key]
                if font_path.exists():
                    try:
                        return pygame.font.Font(str(font_path), size)
                    except pygame.error:
                        pass
        
        # Fallback to system monospace font
        system_fonts = [
            "robotomono",
            "consolas", 
            "monaco",
            "couriernew",
            "monospace"
        ]
        
        for font_name in system_fonts:
            try:
                font = pygame.font.SysFont(font_name, size, bold=bold)
                if font:
                    return font
            except:
                continue
        
        # Ultimate fallback - pygame default
        return pygame.font.Font(None, size)


# Global font manager instance
fonts = FontManager()


def get_font(size: int, bold: bool = False) -> pygame.font.Font:
    """
    Convenience function to get a font.
    
    Args:
        size: Font size in pixels
        bold: Use bold variant
    
    Returns:
        Pygame font object
    """
    return fonts.get_font(size, "mono", bold)
