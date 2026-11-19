#!/usr/bin/env python3
"""Fit per-channel 256-entry LUTs (tone curve + white balance) for the MFD.

Input: grey-ramp response sampled from a photo, normalised to a reference
white (photo of 2026-11-19 12:48, MacBook white as reference).
Target: output encoded value proportional to input above the panel black
floor, common white = strength-weighted mix of the weakest channel's peak.
Writes mfd_lut.bin (768 bytes: R, G, B tables) and chart_corrected.bgra.
"""
import sys
from pathlib import Path

V = [0, 23, 46, 70, 93, 116, 139, 162, 185, 209, 232, 255]
RESP = {  # measured (camera-encoded, white-normalised)
    "R": [34, 41, 68, 102, 133, 166, 182, 205, 216, 228, 243, 243],
    "G": [38, 47, 76, 111, 141, 177, 194, 218, 230, 242, 254, 248],
    "B": [39, 45, 64, 94, 113, 139, 153, 167, 178, 183, 190, 173],
}

def monotone(ys):
    out, m = [], -1
    for y in ys:
        m = max(m, y); out.append(m)
    return out

def inverse(resp, target):
    """smallest input v whose interpolated response reaches target."""
    r = monotone(resp)
    if target <= r[0]:
        return 0
    for i in range(1, len(V)):
        if r[i] >= target:
            if r[i] == r[i - 1]:
                return V[i - 1]
            t = (target - r[i - 1]) / (r[i] - r[i - 1])
            return round(V[i - 1] + t * (V[i] - V[i - 1]))
    # target above the channel's peak: first input reaching the peak
    return V[r.index(r[-1])]

def build(strength: float):
    peaks = {c: max(monotone(r)) for c, r in RESP.items()}
    wmin = min(peaks.values())
    luts = {}
    for c, r in RESP.items():
        black = monotone(r)[0]
        white = wmin * strength + peaks[c] * (1 - strength)
        luts[c] = bytes(inverse(r, black + (white - black) * v / 255) for v in range(256))
    return luts

def main():
    strength = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    here = Path(__file__).resolve().parent
    luts = build(strength)
    (here / "mfd_lut.bin").write_bytes(luts["R"] + luts["G"] + luts["B"])
    arr = bytearray((here / "chart.bgra").read_bytes())
    arr[0::4] = arr[0::4].translate(luts["B"])
    arr[1::4] = arr[1::4].translate(luts["G"])
    arr[2::4] = arr[2::4].translate(luts["R"])
    (here / "chart_corrected.bgra").write_bytes(arr)
    for c in "RGB":
        print(c, [luts[c][v] for v in (0, 46, 93, 139, 185, 232, 255)])

if __name__ == "__main__":
    main()
