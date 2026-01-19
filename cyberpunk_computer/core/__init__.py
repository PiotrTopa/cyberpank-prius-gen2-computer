"""
Core engine module.

Contains the main application loop and rendering pipeline.
"""

from .app import Application
from .renderer import Renderer

__all__ = ["Application", "Renderer"]
