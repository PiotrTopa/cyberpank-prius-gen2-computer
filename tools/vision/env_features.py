#!/usr/bin/env python3
"""Extract environment-model features for a labelled image set (stdlib + ffprobe only).

Runs the backend's own extractor (:func:`cyberpunk_computer.vision.scene_features.extract`)
in parallel processes, so the cached features are exactly what the car computes.

    env_features.py bdd   --root /home/piotr/datasets/dashcam/bdd100k_val_fo -o bdd_val.csv
    env_features.py drive --root .../70mai/drive_home -o drive_home.csv

``bdd``   : ``ROOT/labels.csv`` (file, timeofday, weather, scene) from the FiftyOne export
            of the BDD100K val split (dgural/bdd100k on Hugging Face);
``dawn``  : DAWN (Fog/Rain/Snow folders of the Mendeley set, mirrored as zips on HF);
``drive`` : a ``tools/dashcam_sampler.py`` folder; labels from ``annotations.txt`` by
            laptop time (garage before the first note, then the note's text).
Output: one row per image, the label columns then every ``SceneFeatures`` field.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from cyberpunk_computer.vision import scene_features as sf  # noqa: E402


def _one(args):
    path, t = args
    t0 = time.perf_counter()
    f = sf.extract(path, t=t)
    return path, f, time.perf_counter() - t0


def bdd_rows(root):
    with open(os.path.join(root, "labels.csv"), newline="") as fh:
        for r in csv.DictReader(fh):
            p = os.path.join(root, r["file"])
            if os.path.exists(p):
                yield p, 0.0, {"file": os.path.basename(p), "timeofday": r["timeofday"],
                               "weather": r["weather"], "scene": r["scene"]}


def dawn_rows(root):
    """DAWN (fog/rain/snow folders, web images of mixed size; no time-of-day label)."""
    for weather in ("Fog", "Rain", "Snow", "Sand"):
        top = os.path.join(root, weather)
        if not os.path.isdir(top):
            continue
        for dirpath, _, files in os.walk(top):
            for f in sorted(files):
                if f.lower().endswith((".jpg", ".jpeg", ".png")):
                    yield os.path.join(dirpath, f), 0.0, {
                        "file": f, "timeofday": "undefined",
                        "weather": {"Fog": "foggy", "Rain": "rainy", "Snow": "snowy",
                                    "Sand": "sand"}[weather], "scene": "undefined"}


def drive_rows(root):
    notes = []
    ann = os.path.join(root, "annotations.txt")
    if os.path.exists(ann):
        for line in open(ann):
            line = line.strip()
            if not line:
                continue
            hhmmss, text = line.split(" ", 1)
            notes.append((hhmmss, text.replace("user:", "").strip()))
    with open(os.path.join(root, "samples.csv"), newline="") as fh:
        for r in csv.DictReader(fh):
            hms = r["iso_time"][11:19]
            seg = "garage, lights on"
            for t, text in notes:
                if hms >= t:
                    seg = text
            yield os.path.join(root, r["file"]), float(r["epoch"]), {
                "file": r["file"], "iso_time": r["iso_time"], "segment": seg,
                "timeofday": "daytime", "weather": "clear",
                "scene": "garage" if seg.startswith("garage") else "outdoor"}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["bdd", "drive", "dawn"])
    ap.add_argument("--root", required=True)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("-j", "--jobs", type=int, default=10)
    a = ap.parse_args(argv)
    items = list({"bdd": bdd_rows, "drive": drive_rows, "dawn": dawn_rows}[a.kind](a.root))
    labels = {p: lab for p, _, lab in items}
    lab_cols = list(items[0][2])
    t0 = time.time()
    n_ok, times = 0, []
    with open(a.out, "w", newline="") as fh, ProcessPoolExecutor(a.jobs) as ex:
        w = csv.writer(fh)
        w.writerow(lab_cols + list(sf.FIELDS))
        for path, f, dt in ex.map(_one, [(p, t) for p, t, _ in items], chunksize=16):
            if f is None:
                print("failed:", path, file=sys.stderr)
                continue
            d = f.as_dict()
            w.writerow([labels[path][c] for c in lab_cols] +
                       [round(d[k], 4) for k in sf.FIELDS])
            n_ok += 1
            times.append(dt)
    times.sort()
    print(f"{n_ok}/{len(items)} rows in {time.time() - t0:.0f} s; per frame "
          f"median {1000 * times[len(times) // 2]:.0f} ms, p95 {1000 * times[int(.95 * len(times))]:.0f} ms "
          f"(with {a.jobs} parallel processes)")


if __name__ == "__main__":
    main()
