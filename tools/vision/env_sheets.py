#!/usr/bin/env python3
"""Contact sheets of environment-model hits and failures (experiment venv: pandas, Pillow).

    env_sheets.py --out docs/img

Reads the feature CSVs (``env_fit.py`` paths), runs the committed runtime model and writes
small JPEG grids: ``env_tod_errors.jpg``, ``env_enclosed.jpg``, ``env_drive_dawn.jpg``.
Each tile is captioned "truth -> model". Picks are seeded (random_state=0).
"""

from __future__ import annotations

import argparse
import os
import sys

import pandas as pd
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import env_fit as E  # noqa: E402

BDD_IMG = "/home/piotr/datasets/dashcam/bdd100k_val_fo/data/"
DRIVE_IMG = "/home/piotr/projects/cyberpunk-prius-gateway/captures/2026-10-19/70mai/drive_home/"
DAWN_IMG = "/home/piotr/datasets/dashcam/dawn/"
W, H, COLS = 256, 144, 4


def find_dawn(name):
    for dp, _, fs in os.walk(DAWN_IMG):
        if name in fs:
            return os.path.join(dp, name)
    raise FileNotFoundError(name)


def sheet(tiles, out):
    rows = (len(tiles) + COLS - 1) // COLS
    img = Image.new("RGB", (COLS * W, rows * H), (0, 0, 0))
    for i, (path, caption) in enumerate(tiles):
        im = Image.open(path).convert("RGB").resize((W, H))
        d = ImageDraw.Draw(im)
        d.rectangle((0, 0, W, 13), fill=(0, 0, 0))
        d.text((3, 1), caption, fill=(255, 230, 0))
        img.paste(im, ((i % COLS) * W, (i // COLS) * H))
    img.save(out, quality=72, optimize=True)
    print(out, os.path.getsize(out) // 1024, "KiB")


def pick(df, n, seed=0):
    return df.sample(min(n, len(df)), random_state=seed)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    b, d, w = E.load()
    b = E.run_runtime(b)
    b["truth"] = b.timeofday.map(E.TOD)
    clean = b[(b.enc == 0) & b.truth.notna()]
    tiles = []
    for t, p, n in (("day", "dusk_dawn", 4), ("dusk_dawn", "day", 4), ("night", "dusk_dawn", 2), ("night", "day", 2)):
        for r in pick(clean[(clean.truth == t) & (clean.p_env == p)], n).itertuples():
            tiles.append((BDD_IMG + r.file, f"BDD {t} -> {p}"))
    sheet(tiles, os.path.join(a.out, "env_tod_errors.jpg"))

    tiles = []
    enc = b[b.enc == 1]
    for r in pick(enc[enc.p_enc], 4).itertuples():
        tiles.append((BDD_IMG + r.file, f"BDD enclosed -> enclosed"))
    for r in pick(enc[~enc.p_enc], 4).itertuples():
        tiles.append((BDD_IMG + r.file, f"BDD enclosed -> {r.p_env}"))
    fp = b[(b.enc == 0) & (b.timeofday == "daytime") & b.p_enc]
    for r in pick(fp, 4).itertuples():
        tiles.append((BDD_IMG + r.file, "BDD day -> enclosed"))
    sheet(tiles, os.path.join(a.out, "env_enclosed.jpg"))

    dd = E.run_runtime(d)
    tiles = []
    for i in (0, 2, 4, 8, 10, 12):
        r = dd.iloc[i]
        tiles.append((DRIVE_IMG + r.file, f"70mai garage -> {r.p_env}"))
    for i in (20, 60, 100, 165):
        r = dd.iloc[i]
        tiles.append((DRIVE_IMG + r.file, f"70mai {r.segment[:12]} -> {r.p_env} {r.p_wx}"))
    if w is not None:
        ww = E.run_runtime(w)
        for r in pick(ww[(ww.weather == "foggy") & ww.p_enc], 2).itertuples():
            tiles.append((find_dawn(r.file), "DAWN fog -> enclosed"))
    sheet(tiles, os.path.join(a.out, "env_drive_dawn.jpg"))


if __name__ == "__main__":
    main()
