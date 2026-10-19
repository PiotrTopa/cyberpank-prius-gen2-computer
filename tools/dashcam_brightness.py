#!/usr/bin/env python3
"""Scene brightness from the 70mai dashcam live stream.

    tools/dashcam_brightness.py [--url rtsp://192.168.0.1/liveRTSP/av4] [--fps 1] [--seconds N]

Runs ffmpeg with the signalstats filter and prints one line per sampled frame:
    <unix time>  yavg=<0..255>  ymin=<..>  ymax=<..>  median=<running median of yavg>
No OpenCV, no numpy: ffmpeg does the decoding and the statistics. Stop with Ctrl-C.
"""
import argparse
import re
import statistics
import subprocess
import sys
import time

DEFAULT_URL = "rtsp://192.168.0.1/liveRTSP/av4"
KEYS = ("YAVG", "YMIN", "YMAX")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--fps", type=float, default=1.0, help="samples per second")
    ap.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = run until Ctrl-C)")
    ap.add_argument("--window", type=int, default=30, help="running median window (samples)")
    args = ap.parse_args()

    keys = ",".join(f"lavfi.signalstats.{k}" for k in KEYS)
    vf = f"fps={args.fps},signalstats,metadata=print:key={keys.split(',')[0]}"
    # metadata=print with one key prints every key of the same filter; ask for all three explicitly
    vf = f"fps={args.fps},signalstats," + ",".join(
        f"metadata=print:key=lavfi.signalstats.{k}:file=-" for k in KEYS
    )
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
           "-rtsp_transport", "tcp", "-i", args.url]
    if args.seconds:
        cmd += ["-t", str(args.seconds)]
    cmd += ["-vf", vf, "-f", "null", "-"]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    pat = re.compile(r"lavfi\.signalstats\.(YAVG|YMIN|YMAX)=([0-9.]+)")
    current = {}
    history = []
    try:
        for line in proc.stdout:
            m = pat.search(line)
            if not m:
                if "error" in line.lower():
                    print(line.rstrip(), file=sys.stderr)
                continue
            current[m.group(1)] = float(m.group(2))
            if all(k in current for k in KEYS):
                history.append(current["YAVG"])
                del history[:-args.window]
                print(f"{time.time():.0f}  yavg={current['YAVG']:.1f}  ymin={current['YMIN']:.0f}"
                      f"  ymax={current['YMAX']:.0f}  median={statistics.median(history):.1f}", flush=True)
                current = {}
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
