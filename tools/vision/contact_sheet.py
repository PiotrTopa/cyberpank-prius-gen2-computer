#!/usr/bin/env python3
"""Contact sheets from ``yolo_detect.py`` JSONL output (experiment venv: needs OpenCV).

* ``frames``: a grid of whole frames with their boxes drawn;
* ``crops``:  a grid of enlarged crops around each detection (to judge whether a
  small sign is really what the detector says it is).

Examples::

    contact_sheet.py frames coco.jsonl --frames-list a.jpg,b.jpg --cols 3 -o sheet.jpg
    contact_sheet.py crops signs.jsonl --min-score 0.1 --cols 8 -o crops.jpg
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import cv2
import numpy as np

PALETTE = [(0, 200, 255), (60, 220, 60), (255, 120, 0), (220, 60, 220), (0, 80, 255),
           (255, 255, 0), (120, 120, 255), (0, 255, 160)]


def load(jsonl: str) -> list:
    with open(jsonl) as f:
        return [json.loads(line) for line in f if line.strip()]


def colour(cls: str) -> tuple:
    return PALETTE[sum(map(ord, cls)) % len(PALETTE)]


def label(img, text, org, scale=0.4, fg=(255, 255, 255)):
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    x, y = org
    cv2.rectangle(img, (x, y - th - 3), (x + tw + 2, y + 1), (0, 0, 0), -1)
    cv2.putText(img, text, (x + 1, y - 1), cv2.FONT_HERSHEY_SIMPLEX, scale, fg, 1, cv2.LINE_AA)


def draw_frame(row: dict, min_score: float, root: str):
    img = cv2.imread(os.path.join(root, row["frame"]) if root else row["frame"])
    for d in row.get("detections", []):
        if d["score"] < min_score:
            continue
        x0, y0, x1, y1 = (int(round(v)) for v in d["box"])
        c = colour(d["cls"])
        cv2.rectangle(img, (x0, y0), (x1, y1), c, 1)
        label(img, f"{d['cls']} {d['score']:.2f}", (x0, max(10, y0)), 0.35, c)
    label(img, os.path.basename(row["frame"]), (4, img.shape[0] - 4), 0.45)
    return img


def grid(tiles: list, cols: int, pad: int = 2):
    h = max(t.shape[0] for t in tiles)
    w = max(t.shape[1] for t in tiles)
    rows = (len(tiles) + cols - 1) // cols
    out = np.full((rows * (h + pad) + pad, cols * (w + pad) + pad, 3), 30, np.uint8)
    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        y, x = pad + r * (h + pad), pad + c * (w + pad)
        out[y:y + t.shape[0], x:x + t.shape[1]] = t
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("mode", choices=["frames", "crops"])
    p.add_argument("jsonl")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--min-score", type=float, default=0.25)
    p.add_argument("--cols", type=int, default=3)
    p.add_argument("--frames-list", default="", help="comma-separated basenames (frames mode)")
    p.add_argument("--scale", type=float, default=1.0, help="frame tile scale (frames mode)")
    p.add_argument("--crop", type=int, default=72, help="crop side in source px (crops mode)")
    p.add_argument("--zoom", type=int, default=3, help="crop enlargement (crops mode)")
    p.add_argument("--root", default="", help="prefix for relative frame paths")
    p.add_argument("--quality", type=int, default=85)
    a = p.parse_args(argv)

    rows = load(a.jsonl)
    tiles = []
    if a.mode == "frames":
        want = [s for s in a.frames_list.split(",") if s]
        by_name = {os.path.basename(r["frame"]): r for r in rows}
        for name in (want or list(by_name)):
            t = draw_frame(by_name[name], a.min_score, a.root)
            if a.scale != 1.0:
                t = cv2.resize(t, None, fx=a.scale, fy=a.scale, interpolation=cv2.INTER_AREA)
            tiles.append(t)
    else:
        for r in rows:
            img = None
            for d in r.get("detections", []):
                if d["score"] < a.min_score:
                    continue
                if img is None:
                    img = cv2.imread(os.path.join(a.root, r["frame"]) if a.root else r["frame"])
                x0, y0, x1, y1 = d["box"]
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                half = max(a.crop, 1.6 * max(x1 - x0, y1 - y0)) / 2
                H, W = img.shape[:2]
                X0, Y0 = int(max(0, cx - half)), int(max(0, cy - half))
                X1, Y1 = int(min(W, cx + half)), int(min(H, cy + half))
                crop = cv2.resize(img[Y0:Y1, X0:X1], None, fx=a.zoom, fy=a.zoom,
                                  interpolation=cv2.INTER_CUBIC)
                bx0, by0 = int((x0 - X0) * a.zoom), int((y0 - Y0) * a.zoom)
                bx1, by1 = int((x1 - X0) * a.zoom), int((y1 - Y0) * a.zoom)
                cv2.rectangle(crop, (bx0, by0), (bx1, by1), colour(d["cls"]), 1)
                side = int(2 * half * a.zoom)
                tile = np.full((side + 26, side, 3), 30, np.uint8)
                crop = crop[:side, :side]
                tile[:crop.shape[0], :crop.shape[1]] = crop
                stem = os.path.splitext(os.path.basename(r["frame"]))[0][-6:]
                label(tile, f"{stem} {d['cls'][:18]}", (2, side + 11), 0.36)
                label(tile, f"{d['score']:.2f}  {int(x1 - x0)}x{int(y1 - y0)}px", (2, side + 24), 0.36)
                tiles.append(tile)
    if not tiles:
        print("nothing to draw", file=sys.stderr)
        return 1
    sheet = grid(tiles, a.cols)
    cv2.imwrite(a.out, sheet, [cv2.IMWRITE_JPEG_QUALITY, a.quality])
    print(f"{a.out}: {len(tiles)} tiles, {sheet.shape[1]}x{sheet.shape[0]}, "
          f"{os.path.getsize(a.out) // 1024} KiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
