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
# Channel equalizer (MFD panel color balance)
#
# The Prius MFD's green phosphor dominates through the VGA666 resistor DAC,
# so the default output gains push red and blue up 25%. Override per channel
# with MFD_GAIN_R / MFD_GAIN_G / MFD_GAIN_B (floats; 1.0 = passthrough).
# Applied via 256-byte translate LUTs — pure C, no numpy needed.
# ---------------------------------------------------------------------------
# 2026-09-08: the "green cast" turned out to be dpi_output_format=0x06
# (RGB666 config 2) scrambling bit lanes into the config-1-wired VGA666 —
# fixed in /boot/firmware/config.txt (now 0x05). With that fixed the panel
# measures neutral, so the equalizer defaults to passthrough. Tune via the
# mfd.service drop-in (MFD_GAIN_R/G/B) if a residual tint shows up.
DEFAULT_GAINS = {"MFD_GAIN_R": 1.0, "MFD_GAIN_G": 1.0, "MFD_GAIN_B": 1.0}


def _gain_from_env(name: str) -> float:
    try:
        return float(os.environ.get(name, DEFAULT_GAINS[name]))
    except ValueError:
        logger.warning(f"Invalid {name}, using default {DEFAULT_GAINS[name]}")
        return DEFAULT_GAINS[name]


def _build_gain_lut(gain: float):
    """256-byte translation table applying *gain*, or None for passthrough."""
    if abs(gain - 1.0) < 1e-3:
        return None
    return bytes(min(255, int(i * gain + 0.5)) for i in range(256))


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

        # Channel equalizer LUTs (None = passthrough for that channel)
        self._lut_r = _build_gain_lut(_gain_from_env("MFD_GAIN_R"))
        self._lut_g = _build_gain_lut(_gain_from_env("MFD_GAIN_G"))
        self._lut_b = _build_gain_lut(_gain_from_env("MFD_GAIN_B"))
        if self._lut_r or self._lut_g or self._lut_b:
            logger.info(
                "Channel equalizer active: R=%s G=%s B=%s",
                _gain_from_env("MFD_GAIN_R"),
                _gain_from_env("MFD_GAIN_G"),
                _gain_from_env("MFD_GAIN_B"),
            )
    
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

            # Read the real line length (stride) — the GPU may pad rows
            # beyond width*bytes_per_pixel; assuming packed rows would
            # progressively shear the image.
            try:
                with open(f"{sysfs_base}/stride", "r") as f:
                    self.line_length = int(f.read().strip())
            except (FileNotFoundError, ValueError):
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
            # Framebuffer expects BGRA (32bpp, verified on this hardware).
            if self.bpp == 32:
                converted = surface.convert_alpha()
                buffer = pygame.image.tobytes(converted, "BGRA")
                buffer = self._equalize_bgra(buffer)
                self._write_rows(buffer, self.width * 4)

            elif self.bpp == 16:
                # 16-bit: RGB565 format
                converted = surface.convert(16)
                buffer = pygame.image.tobytes(converted, "RGB")
                self._write_rows(buffer, self.width * 2)
            else:
                logger.error(f"Unsupported bit depth: {self.bpp}")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to blit to framebuffer: {e}")
            return False
    
    def _equalize_bgra(self, buffer: bytes):
        """Apply per-channel gain LUTs to a BGRA buffer (panel equalizer).

        Strided slice + bytes.translate run in C: ~3-4 ms per 480x240 frame
        on a Pi Zero 2W, well within the 30 fps budget.
        """
        if not (self._lut_r or self._lut_g or self._lut_b):
            return buffer
        arr = bytearray(buffer)
        if self._lut_b:
            arr[0::4] = arr[0::4].translate(self._lut_b)
        if self._lut_g:
            arr[1::4] = arr[1::4].translate(self._lut_g)
        if self._lut_r:
            arr[2::4] = arr[2::4].translate(self._lut_r)
        return arr

    def _write_rows(self, buffer: bytes, row_bytes: int) -> None:
        """Write packed pixel rows into the framebuffer, honoring stride."""
        if self.line_length == row_bytes:
            # No row padding: single contiguous write
            self.mmap.seek(0)
            self.mmap.write(buffer)
            return
        for y in range(self.height):
            dst = y * self.line_length
            src = y * row_bytes
            self.mmap[dst:dst + row_bytes] = buffer[src:src + row_bytes]

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
                # BGRA format (equalized like the blit path)
                pixel = bytes(self._equalize_bgra(
                    bytes([color[2], color[1], color[0], 255])))
                self._write_rows(pixel * self.width * self.height, self.width * 4)
            elif self.bpp == 16:
                # RGB565 format
                r = (color[0] >> 3) & 0x1F
                g = (color[1] >> 2) & 0x3F
                b = (color[2] >> 3) & 0x1F
                pixel = struct.pack('<H', (r << 11) | (g << 5) | b)
                self._write_rows(pixel * self.width * self.height, self.width * 2)
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
