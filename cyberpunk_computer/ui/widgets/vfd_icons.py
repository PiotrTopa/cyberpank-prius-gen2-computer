"""
VFD Display Icons.

Binary icon bitmaps for VFD display.
Each icon is a 2D list where 1 = pixel ON, 0 = pixel OFF.

Easy to edit - just modify the matrices below.
"""

from typing import List


# ==============================================================================
# Lightning Bolt Icon - Electric Motor / MG Power
# ==============================================================================
# Size: 11x11 pixels
# Represents electric motor / motor-generator power

ICON_LIGHTNING: List[List[int]] = [
    [0, 0, 0, 0, 1, 1, 1, 1, 0, 0, 0],
    [0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0],
    [0, 0, 0, 1, 1, 1, 0, 0, 0, 0, 0],
    [0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0],
    [0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
    [0, 0, 0, 0, 1, 1, 1, 1, 0, 0, 0],
    [0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0],
    [0, 0, 0, 1, 1, 1, 0, 0, 0, 0, 0],
    [0, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0],
    [0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0],
    [0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0],
]


# ==============================================================================
# Engine Icon - ICE / Fuel Consumption
# ==============================================================================
# Size: 11x11 pixels
# Classic dashboard engine warning icon style

ICON_ENGINE: List[List[int]] = [
    [0, 0, 1, 1, 1, 1, 1, 1, 1, 0, 0],
    [0, 1, 0, 1, 0, 0, 0, 1, 0, 1, 0],
    [1, 0, 0, 0, 1, 0, 1, 0, 0, 0, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1],
    [1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1],
    [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0],
    [0, 0, 1, 1, 1, 0, 1, 1, 1, 0, 0],
    [0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0],
]


# ==============================================================================
# Helper function to get icon size
# ==============================================================================

def get_icon_size(icon: List[List[int]]) -> tuple[int, int]:
    """
    Get the width and height of an icon.
    
    Args:
        icon: Icon bitmap (2D list)
    
    Returns:
        Tuple of (width, height)
    """
    if not icon:
        return (0, 0)
    height = len(icon)
    width = len(icon[0]) if icon else 0
    return (width, height)
