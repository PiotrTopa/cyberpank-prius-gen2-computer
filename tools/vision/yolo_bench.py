#!/usr/bin/env python3
"""Time an Ultralytics YOLO model on a folder of frames (experiment venv only).

Prints one JSON line with medians / p90 of Ultralytics' own per-stage timings
(preprocess, inference, postprocess, ms) and of the wall time per frame, after
``--warmup`` untimed frames. Run one process per configuration so thread
settings do not leak between runs::

    yolo_bench.py --model yolo11n.pt --imgsz 416 --threads 1 frames/*.jpg
    yolo_bench.py --model yolo11n_ncnn_model --device vulkan:0 frames/*.jpg
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time


def expand(paths):
    """Directories expand to their sorted *.jpg files."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            out += sorted(os.path.join(p, n) for n in os.listdir(p) if n.lower().endswith(".jpg"))
        else:
            out.append(p)
    return out


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--model", required=True)
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--device", default="cpu")
    p.add_argument("--threads", type=int, default=0, help="torch threads (0 = default)")
    p.add_argument("--warmup", type=int, default=5)
    p.add_argument("--half", action="store_true")
    p.add_argument("frames", nargs="+")
    a = p.parse_args(argv)
    a.frames = expand(a.frames)

    if a.threads > 0:
        # torch.set_num_threads() alone is not honoured by every backend
        # (measured: "1 thread" ran as fast as 8), so pin the process as well.
        os.environ["OMP_NUM_THREADS"] = str(a.threads)
        os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:a.threads])
    import torch
    if a.threads > 0:
        torch.set_num_threads(a.threads)
    from ultralytics import YOLO

    model = YOLO(a.model, task="detect")
    kw = dict(imgsz=a.imgsz, device=a.device, conf=0.25, verbose=False)
    if a.half:
        kw["half"] = True
    for f in a.frames[:a.warmup]:
        model.predict(f, **kw)
    stages = {"preprocess": [], "inference": [], "postprocess": []}
    wall = []
    for f in a.frames:
        t0 = time.perf_counter()
        r = model.predict(f, **kw)[0]
        wall.append((time.perf_counter() - t0) * 1000.0)
        for k in stages:
            stages[k].append(r.speed[k])
    out = {"model": os.path.basename(a.model.rstrip("/")), "imgsz": a.imgsz, "device": a.device,
           "threads": a.threads or torch.get_num_threads(), "n": len(a.frames)}
    for k, xs in stages.items():
        out[f"{k}_med"] = round(statistics.median(xs), 2)
    out["inference_p90"] = round(pct(stages["inference"], 0.9), 2)
    out["wall_med"] = round(statistics.median(wall), 2)
    out["wall_p90"] = round(pct(wall, 0.9), 2)
    out["fps_wall"] = round(1000.0 / out["wall_med"], 1)
    print(json.dumps(out), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
