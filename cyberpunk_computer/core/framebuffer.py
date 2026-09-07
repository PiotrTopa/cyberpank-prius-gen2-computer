"""
Direct framebuffer output for Linux systems without SDL video driver support.

This module provides a way to output pygame surfaces directly to /dev/fb0
when SDL video drivers (fbcon, kmsdrm) are not available.
"""

import logging
import mmap
import os
import struct
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Channel gain LUT cache
# ---------------------------------------------------------------------------
_gain_lut_cache: dict = {}


def _get_gain_lut(gain: float):
    """Return a precomputed uint8 LUT that applies *gain* to [0..255] values.

    The LUT is built once per unique gain value and cached.  Applying it via
    numpy fancy-indexing (``lut[array]``) is O(N) in C without any Python loop.
    """
    try:
        import numpy as np
    except ImportError:
        return None
    if gain not in _gain_lut_cache:
        indices = np.arange(256, dtype=np.float32)
        _gain_lut_cache[gain] = np.clip(indices * gain, 0, 255).astype(np.uint8)
    return _gain_lut_cache[gain]


class FramebufferOutput:
    """
    Direct framebuffer output using memory-mapped /dev/fb0.
    
    This class allows pygame to render to an offscreen surface while
    we handle the display output by copying pixels directly to the
    Linux framebuffer device.
    """
    
    # ioctl constants for framebuffer
    FBIOGET_VSCREENINFO = 0x4600
    FBIOGET_FSCREENINFO = 0x4602
    
    def __init__(self, device: str = "/dev/fb0"):
        """
        Initialize framebuffer output.
        
        Args:
            device: Path to framebuffer device (default: /dev/fb0)
        """
        self.device = device
        self.fb_file: Optional[object] = None
        self.mmap: Optional[mmap.mmap] = None
        self.width = 0
        self.height = 0
        self.bpp = 0  # bits per pixel
        self.line_length = 0  # bytes per line (may include padding)
        self._initialized = False
    
    def initialize(self) -> bool:
        """
        Initialize the framebuffer.
        
        Returns:
            True if initialization succeeded, False otherwise
        """
        if self._initialized:
            return True
        
        try:
            # Check if device exists
            if not os.path.exists(self.device):
                logger.error(f"Framebuffer device {self.device} does not exist")
                return False
            
            # Get framebuffer info from sysfs
            fb_name = os.path.basename(self.device)
            sysfs_base = f"/sys/class/graphics/{fb_name}"
            
            # Read virtual size
            with open(f"{sysfs_base}/virtual_size", "r") as f:
                size_str = f.read().strip()
                self.width, self.height = map(int, size_str.split(","))
            
            # Read bits per pixel
            with open(f"{sysfs_base}/bits_per_pixel", "r") as f:
                self.bpp = int(f.read().strip())
            
            # Calculate line length (bytes per line)
            self.line_length = self.width * (self.bpp // 8)
            
            # Calculate total buffer size
            buffer_size = self.height * self.line_length
            
            logger.info(
                f"Framebuffer: {self.width}x{self.height} @ {self.bpp}bpp, "
                f"line_length={self.line_length}, buffer_size={buffer_size}"
            )
            
            # Open and memory-map the framebuffer
            self.fb_file = open(self.device, "r+b", buffering=0)
            self.mmap = mmap.mmap(
                self.fb_file.fileno(),
                buffer_size,
                mmap.MAP_SHARED,
                mmap.PROT_WRITE | mmap.PROT_READ
            )
            
            self._initialized = True
            logger.info(f"Framebuffer {self.device} initialized successfully")
            return True
            
        except PermissionError:
            logger.error(
                f"Permission denied accessing {self.device}. "
                "Try adding user to 'video' group or running as root."
            )
            return False
        except FileNotFoundError as e:
            logger.error(f"Framebuffer sysfs info not found: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to initialize framebuffer: {e}")
            return False
    
    def get_size(self) -> Tuple[int, int]:
        """
        Get framebuffer dimensions.
        
        Returns:
            Tuple of (width, height)
        """
        return (self.width, self.height)
    
    def blit_surface(self, surface) -> bool:
        """
        Copy a pygame surface to the framebuffer.
        
        Args:
            surface: pygame.Surface to copy (must match framebuffer size)
            
        Returns:
            True if successful, False otherwise
        """
        if not self._initialized or self.mmap is None:
            return False
        
        try:
            # Import pygame here to avoid circular imports
            import pygame
            
            # Get surface dimensions
            surf_width, surf_height = surface.get_size()
            
            # Scale if needed
            if surf_width != self.width or surf_height != self.height:
                surface = pygame.transform.scale(
                    surface, 
                    (self.width, self.height)
                )
            
            # Convert surface to the correct format
            # Framebuffer is typically BGRA or BGR depending on bpp
            if self.bpp == 32:
                # 32-bit framebuffer wants BGRA with R+B gain correction
                # to compensate for MFD green-biased phosphor response.
                # Strategy: get the BGRA flat buffer from pygame (pure C, ~2ms),
                # then apply a precomputed LUT to the B [0::4] and R [2::4]
                # byte slices via numpy fancy-indexing (~2ms more).
                # Total ~4ms/frame — fits in the 33ms Pi Zero 2W budget.
                try:
                    import numpy as np
                    converted = surface.convert_alpha()
                    # Get flat BGRA bytes in C (fast pygame path)
                    raw = pygame.image.tobytes(converted, "BGRA")
                    # View as writable uint8 array — no copy
                    arr = np.frombuffer(raw, dtype=np.uint8).copy()
                    # Apply gain LUT to B (offset 0) and R (offset 2); leave G alone
                    lut_rb = _get_gain_lut(1.3)
                    arr[0::4] = lut_rb[arr[0::4]]  # B
                    arr[2::4] = lut_rb[arr[2::4]]  # R
                    buffer = arr.tobytes()
                except ImportError:
                    # numpy not available: fall back to plain BGRA copy (no gain)
                    converted = surface.convert_alpha()
                    buffer = pygame.image.tobytes(converted, "BGRA")
                self.mmap.seek(0)
                self.mmap.write(buffer)
                
            elif self.bpp == 16:
                # 16-bit: RGB565 format
                converted = surface.convert(16)
                buffer = pygame.image.tobytes(converted, "RGB")
                self.mmap.seek(0)
                self.mmap.write(buffer)
            else:
                logger.error(f"Unsupported bit depth: {self.bpp}")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to blit to framebuffer: {e}")
            return False
    
    def clear(self, color: Tuple[int, int, int] = (0, 0, 0)) -> None:
        """
        Clear the framebuffer to a solid color.
        
        Args:
            color: RGB tuple (default: black)
        """
        if not self._initialized or self.mmap is None:
            return
        
        try:
            if self.bpp == 32:
                # BGRA format
                pixel = bytes([color[2], color[1], color[0], 255])
                row = pixel * self.width
                self.mmap.seek(0)
                for _ in range(self.height):
                    self.mmap.write(row)
            elif self.bpp == 16:
                # RGB565 format
                r = (color[0] >> 3) & 0x1F
                g = (color[1] >> 2) & 0x3F
                b = (color[2] >> 3) & 0x1F
                pixel = struct.pack('<H', (r << 11) | (g << 5) | b)
                row = pixel * self.width
                self.mmap.seek(0)
                for _ in range(self.height):
                    self.mmap.write(row)
        except Exception as e:
            logger.error(f"Failed to clear framebuffer: {e}")
    
    def cleanup(self) -> None:
        """Clean up framebuffer resources."""
        if self.mmap is not None:
            try:
                self.mmap.close()
            except Exception:
                pass
            self.mmap = None
        
        if self.fb_file is not None:
            try:
                self.fb_file.close()
            except Exception:
                pass
            self.fb_file = None
        
        self._initialized = False
        logger.info("Framebuffer cleaned up")
    
    def __del__(self):
        """Destructor to ensure cleanup."""
        self.cleanup()


def is_framebuffer_available(device: str = "/dev/fb0") -> bool:
    """
    Check if direct framebuffer output is available.
    
    Args:
        device: Path to framebuffer device
        
    Returns:
        True if framebuffer is available and accessible
    """
    if not os.path.exists(device):
        return False
    
    try:
        with open(device, "rb") as f:
            # Just check we can read
            f.read(1)
        return True
    except (PermissionError, IOError):
        return False
