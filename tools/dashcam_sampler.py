#!/usr/bin/env python3
"""Sample still frames from the 70mai dashcam live stream for lighting/weather analysis.

    tools/dashcam_sampler.py --out DIR [--interval 10] [--url rtsp://192.168.0.1/liveRTSP/av4]

Every --interval seconds one JPEG frame is grabbed (ffmpeg, RTSP over TCP, one session per
sample so the camera's single-client server is never held) and saved as
    DIR/YYYYmmdd_HHMMSS.jpg
and one CSV line is appended to DIR/samples.csv with simple luminance/chroma statistics
(ffprobe signalstats, 8-bit values) for the whole frame, the top third ("sky") and the bottom
half ("road"):
    iso_time, epoch, file, y_avg, y_min, y_max, u_avg, v_avg, sky_y, sky_u, sky_v, road_y
Failures (camera off, hotspot gone) are logged to DIR/sampler.log and retried forever.
The CSV is meant to be annotated by hand later (weather, time of day, lights on/off).
"""
import argparse
import csv
import os
import subprocess
import sys
import time

DEFAULT_URL = "rtsp://192.168.0.1/liveRTSP/av4"
FIELDS = ["iso_time", "epoch", "file", "y_avg", "y_min", "y_max", "u_avg", "v_avg",
          "sky_y", "sky_u", "sky_v", "road_y"]


def grab(url: str, path: str, timeout: float = 25) -> bool:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-rtsp_transport", "tcp", "-i", url, "-frames:v", "1", "-q:v", "3", "-y", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 0


def stats(path: str, crop: str = "") -> dict:
    """signalstats averages for the file, optionally after a crop filter (ffmpeg syntax)."""
    chain = f"movie={path}" + (f",{crop}" if crop else "") + ",signalstats"
    cmd = ["ffprobe", "-v", "error", "-f", "lavfi", "-i", chain,
           "-show_entries", "frame_tags=lavfi.signalstats.YAVG,lavfi.signalstats.YMIN,"
           "lavfi.signalstats.YMAX,lavfi.signalstats.UAVG,lavfi.signalstats.VAVG",
           "-of", "csv=p=0"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    vals = r.stdout.strip().split(",")
    if len(vals) < 5:
        return {}
    y_avg, y_min, y_max, u_avg, v_avg = (float(v) for v in vals[:5])
    return {"y_avg": y_avg, "y_min": y_min, "y_max": y_max, "u_avg": u_avg, "v_avg": v_avg}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--url", default=DEFAULT_URL)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    csv_path = os.path.join(args.out, "samples.csv")
    log_path = os.path.join(args.out, "sampler.log")
    new_csv = not os.path.exists(csv_path)
    log = open(log_path, "a", buffering=1)

    def say(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        log.write(line + "\n")
        print(line, flush=True)

    say(f"sampler start url={args.url} interval={args.interval}s out={args.out}")
    failures = 0
    while True:
        t0 = time.time()
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(t0))
        fname = f"{stamp}.jpg"
        fpath = os.path.join(args.out, fname)
        if grab(args.url, fpath):
            try:
                full = stats(fpath)
                sky = stats(fpath, "crop=iw:ih/3:0:0")
                road = stats(fpath, "crop=iw:ih/2:0:ih/2")
                row = {"iso_time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0)),
                       "epoch": int(t0), "file": fname,
                       **{k: round(full.get(k, -1), 1) for k in ("y_avg", "y_min", "y_max", "u_avg", "v_avg")},
                       "sky_y": round(sky.get("y_avg", -1), 1), "sky_u": round(sky.get("u_avg", -1), 1),
                       "sky_v": round(sky.get("v_avg", -1), 1), "road_y": round(road.get("y_avg", -1), 1)}
                with open(csv_path, "a", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=FIELDS)
                    if new_csv:
                        w.writeheader()
                        new_csv = False
                    w.writerow(row)
                say(f"ok {fname} y={row['y_avg']} sky={row['sky_y']} road={row['road_y']}")
            except Exception as e:  # keep sampling whatever happens to one frame
                say(f"stats failed for {fname}: {e}")
            failures = 0
        else:
            failures += 1
            if os.path.exists(fpath):
                os.remove(fpath)
            if failures in (1, 5, 30) or failures % 60 == 0:
                say(f"grab failed ({failures} in a row) — camera off or hotspot down? retrying")
        wait = args.interval - (time.time() - t0)
        if wait > 0:
            time.sleep(wait)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
