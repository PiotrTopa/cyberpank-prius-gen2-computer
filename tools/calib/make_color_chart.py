#!/usr/bin/env python3
"""MFD colour calibration chart (480x240).

Writes:
  chart.png        native 480x240 (sRGB, reference values)
  chart_x4.png     nearest-neighbour x4 for the MacBook screen
  chart.bgra       raw BGRA for `cat > /dev/fb0` on the Pi (no gains applied)

Layout (rows top to bottom, 40 px each, 2 px black gutters):
  0  grey ramp, 12 steps 0..255
  1  R G B C M Y W at 100 %, then R G B at 25 %
  2  R G B C M Y W at 50 %, then  C M Y at 25 %
  3  near-black steps 0,4,8,12,16,24,32,48,64,80 (shadow detail)
  4  UI theme: accent_bright/mid/dim/dark, secondary_b/m, warm_b/m, alert_b/m
  5  skin tones + neutral 18 % grey + sky/foliage (ColorChecker-ish sRGB)
"""
import sys
from pathlib import Path
from PIL import Image, ImageDraw

W, H = 480, 240
ROWS = [
    [(v, v, v) for v in (0, 23, 46, 70, 93, 116, 139, 162, 185, 209, 232, 255)],
    [(255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 255, 255), (255, 0, 255), (255, 255, 0),
     (255, 255, 255), (64, 0, 0), (0, 64, 0), (0, 0, 64)],
    [(128, 0, 0), (0, 128, 0), (0, 0, 128), (0, 128, 128), (128, 0, 128), (128, 128, 0),
     (128, 128, 128), (0, 64, 64), (64, 0, 64), (64, 64, 0)],
    [(v, v, v) for v in (0, 4, 8, 12, 16, 24, 32, 48, 64, 80)],
    [(0, 255, 204), (0, 168, 150), (53, 93, 104), (0, 51, 51), (148, 197, 172),
     (106, 175, 157), (255, 235, 153), (255, 194, 122), (255, 36, 36), (217, 98, 107)],
    [(115, 82, 68), (194, 150, 130), (98, 122, 157), (87, 108, 67), (133, 128, 177),
     (103, 189, 170), (214, 126, 44), (80, 91, 166), (119, 119, 119), (52, 52, 52)],
]

def build() -> Image.Image:
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    rh = H // len(ROWS)
    for r, row in enumerate(ROWS):
        cw = W / len(row)
        for c, rgb in enumerate(row):
            x0, x1 = round(c * cw) + 1, round((c + 1) * cw) - 2
            y0, y1 = r * rh + 1, (r + 1) * rh - 2
            d.rectangle([x0, y0, x1, y1], fill=rgb)
    return img

def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    img = build()
    img.save(out / "chart.png")
    img.resize((W * 4, H * 4), Image.NEAREST).save(out / "chart_x4.png")
    r, g, b = img.split()
    Image.merge("RGBA", (b, g, r, Image.new("L", (W, H), 255))).tobytes()
    (out / "chart.bgra").write_bytes(
        Image.merge("RGBA", (b, g, r, Image.new("L", (W, H), 255))).tobytes())

if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
