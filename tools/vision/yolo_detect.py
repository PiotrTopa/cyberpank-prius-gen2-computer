#!/usr/bin/env python3
"""Run an Ultralytics YOLO model over dashcam frames and print JSON lines.

This script runs in the *experiment* venv (``.venv-vision``: torch +
ultralytics), never in the backend's interpreter. The backend talks to it
through :class:`cyberpunk_computer.vision.detectors.SubprocessYoloDetector`
and only parses its output, so torch/numpy never enter the backend process.

Two modes:

* batch:  ``yolo_detect.py --model yolo11n.pt a.jpg b.jpg ...``
* server: ``yolo_detect.py --model yolo11n.pt --stdin`` — prints one
  ``{"ready": true, ...}`` line, then reads one frame path per line on stdin
  and answers each with exactly one JSON line (the model is loaded once, so a
  frame costs tens of ms instead of the ~1 s model start-up).

One output line per frame::

    {"frame": "a.jpg", "w": 640, "h": 360, "model": "yolo11n.pt", "imgsz": 640,
     "ms": 41.2, "infer_ms": 30.1,
     "detections": [{"cls": "car", "score": 0.873, "box": [x0, y0, x1, y1]}]}

Boxes are in pixels of the original frame. A frame that cannot be processed
gives ``{"frame": ..., "error": "..."}`` instead of ``detections``.
"""

from __future__ import annotations

import argparse
import json
import os
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


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--model", required=True, help="weights (.pt, .onnx, .tflite, *_ncnn_model)")
    p.add_argument("--imgsz", type=int, default=640, help="inference size (long side)")
    p.add_argument("--conf", type=float, default=0.25, help="score threshold")
    p.add_argument("--iou", type=float, default=0.45, help="NMS IoU")
    p.add_argument("--classes", default="", help="comma-separated class names to keep (default: all)")
    p.add_argument("--device", default="cpu", help="cpu | 0 (CUDA) | vulkan:0 (ncnn)")
    p.add_argument("--threads", type=int, default=0, help="torch CPU threads (0 = torch default)")
    p.add_argument("--annotate-dir", default="", help="write annotated JPEGs here")
    p.add_argument("--stdin", action="store_true", help="server mode: frame paths on stdin")
    p.add_argument("frames", nargs="*")
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    a.frames = expand(a.frames)
    # Ultralytics and its backends print progress to stdout ("Loading ... for
    # ONNX Runtime inference"). stdout is the JSON channel, so keep a private
    # copy of it and point fd 1 (Python and C level) at stderr.
    out = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    os.environ.setdefault("YOLO_AUTOINSTALL", "false")  # never pip-install at run time
    os.environ.setdefault("YOLO_OFFLINE", "true")

    def emit(obj: dict) -> None:
        out.write(json.dumps(obj) + "\n")
        out.flush()

    if a.threads > 0:  # torch.set_num_threads() alone is not honoured; pin the process
        os.environ["OMP_NUM_THREADS"] = str(a.threads)
        os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:a.threads])
        import torch
        torch.set_num_threads(a.threads)
    from ultralytics import YOLO

    model = YOLO(a.model, task="detect")
    names = model.names
    keep = {c.strip() for c in a.classes.split(",") if c.strip()}
    keep_ids = [i for i, n in names.items() if n in keep] if keep else None
    model_tag = os.path.basename(a.model.rstrip("/"))
    if a.annotate_dir:
        os.makedirs(a.annotate_dir, exist_ok=True)

    def one(path: str) -> dict:
        if not os.path.isfile(path):
            return {"frame": path, "error": "no such file"}
        t0 = time.perf_counter()
        try:
            r = model.predict(path, imgsz=a.imgsz, conf=a.conf, iou=a.iou, device=a.device,
                              classes=keep_ids, verbose=False)[0]
        except Exception as e:  # noqa: BLE001 - reported to the caller as data
            return {"frame": path, "error": f"{type(e).__name__}: {e}"}
        wall_ms = (time.perf_counter() - t0) * 1000.0
        h, w = r.orig_shape
        dets = []
        for (x0, y0, x1, y1), s, c in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist(),
                                          r.boxes.cls.tolist()):
            dets.append({"cls": names[int(c)], "score": round(float(s), 4),
                         "box": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]})
        if a.annotate_dir:
            r.save(filename=os.path.join(a.annotate_dir, os.path.basename(path)))
        return {"frame": path, "w": int(w), "h": int(h), "model": model_tag, "imgsz": a.imgsz,
                "ms": round(wall_ms, 2), "infer_ms": round(float(r.speed.get("inference", 0.0)), 2),
                "detections": dets}

    if a.stdin:
        emit({"ready": True, "model": model_tag, "imgsz": a.imgsz})
        for line in sys.stdin:
            path = line.strip()
            if path:
                emit(one(path))
    else:
        for path in a.frames:
            emit(one(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
