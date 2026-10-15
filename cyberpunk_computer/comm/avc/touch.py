"""
MFD touch panel calibration (measured 2026-10-14, emu8_release_ack.ndjson).

Frame ``110->178 00 21 24 78 X Y X Y``; X grows left → right (4..249),
Y grows bottom → top (9..255). For the 480×240 native picture
(AVC_MAP.md "Touch panel calibration"):

    px = (X − 4) · 480 / 245
    py = (255 − Y) · 240 / 246
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

SCREEN_W = 480
SCREEN_H = 240


@dataclass(frozen=True)
class TouchCalibration:
    x_min: int = 4      # raw X at the left edge
    x_span: int = 245   # raw X counts across the width
    y_top: int = 255    # raw Y at the top edge
    y_span: int = 246   # raw Y counts across the height
    width: int = SCREEN_W
    height: int = SCREEN_H

    def to_screen(self, raw_x: int, raw_y: int) -> Tuple[int, int]:
        px = (raw_x - self.x_min) * self.width / self.x_span
        py = (self.y_top - raw_y) * self.height / self.y_span
        x = int(round(px))
        y = int(round(py))
        return (min(max(x, 0), self.width - 1), min(max(y, 0), self.height - 1))


DEFAULT_CALIBRATION = TouchCalibration()
