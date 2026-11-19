#!/usr/bin/env python3
"""Replay roadsense's outgoing stream through the car's scene path and measure what the MFD draws.

    tools/scene_feed_replay.py WS.jsonl.gz [--no-view] [--repo DIR] [--seed N] [--link-ms 5] [--minutes M]

WS is the api's stream as roadsense `tools/view_replay.py SESSION --ws WS.jsonl.gz` writes it from a drive
recorder session (every line carries `_rx_s`, the time it left the api). The path simulated:

    api /ws ──> backend SceneBuilder (as RoadsenseSceneFeed._run calls it: feed per line, build when due)
            ──> ZmqServer latest-only slot, flushed on its 50 ms poll (random phase)
            ──> link to the Pi Zero (base delay + exponential jitter, --link-ms)
            ──> SceneFeed (latest wins) ──> UI loop at fps_scene (20 Hz): read_perception, SceneDirector,
                SceneRenderer.due/render (headless pygame)

--no-view   drop roadsense's `view` messages (what an older roadsense, or this car code before L19, does)
--repo DIR  import cyberpunk_computer from another checkout (the code before / after on the same data)

Printed, per UI frame (what the glass shows):
* ego traffic light: drawn distance step after the car's own motion (dx + v dt), lateral step; frames on which
  a light is drawn while roadsense has no ego light (the 5 s health mirror's old light);
* lead: drawn distance step after its relative speed; read-out changes per minute while both stand;
* lane lines at 10 / 30 m: drawn lateral step;
* backend: scenes whose lead / lanes come from another frame than their road frame; event rate;
* standing with a lead, road view open: scene redraws per second and the longest time without one (a frozen
  picture);
* age of the drawn sample (frame time -> drawn), median / p95.
"""
import argparse
import gzip
import json
import math
import os
import random
import sys
import types

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("ws")
ap.add_argument("--no-view", action="store_true")
ap.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ap.add_argument("--seed", type=int, default=1)
ap.add_argument("--link-ms", type=float, default=5.0)
ap.add_argument("--minutes", type=float, default=0.0, help="stop after this much drive time (0 = all)")
ap.add_argument("--no-render", action="store_true", help="skip the headless renderer (no redraw figures)")
ap.add_argument("--strip", metavar="T:N:PNG", help="save N consecutive drawn frames from T s after the start as one PNG")
ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="director constant override")
A = ap.parse_args()
sys.path.insert(0, A.repo)
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

from cyberpunk_computer.backend.roadsense_scene import SceneBuilder, SceneFeedConfig   # noqa: E402
from cyberpunk_computer.ui.scene.director import SceneDirector                        # noqa: E402
from cyberpunk_computer.ui.scene.inputs import SceneFeed, read_inputs                 # noqa: E402
from cyberpunk_computer.ui.scene.tuning import Tuning                                 # noqa: E402
import cyberpunk_computer.ui.scene.director as _director                              # noqa: E402

for _kv in A.set:
    _k, _v = _kv.split("=", 1)
    setattr(_director, _k, float(_v))

NS = types.SimpleNamespace


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(q * len(v)))] if v else float("nan")


def rms(v):
    return math.sqrt(sum(x * x for x in v) / len(v)) if v else float("nan")


def stats(v, big):
    return "rms %.3f p95 %.3f max %.2f >%.1f: %d (n %d)" % (
        rms(v), pct([abs(x) for x in v], 0.95), max((abs(x) for x in v), default=float("nan")), big,
        sum(1 for x in v if abs(x) > big), len(v))


def main():
    rnd = random.Random(A.seed)
    msgs = []
    with gzip.open(A.ws, "rt") as f:
        for ln in f:
            m = json.loads(ln)
            if A.no_view and m.get("type") == "view":
                continue
            rx = m.pop("_rx_s")
            msgs.append((rx, m))
    msgs.sort(key=lambda p: p[0])
    t0 = msgs[0][0]
    if A.minutes:
        msgs = [p for p in msgs if p[0] - t0 <= 60.0 * A.minutes]
    frame_ts = {m["seq"]: m["ts_mono_ns"] / 1e9 for _, m in msgs if m.get("type") == "road"}
    speed = []          # (t, m/s) from the lanes topic's ego
    ego_light = []      # (t, ego dict or None) as roadsense said it
    lead_truth = []     # (frame time, dist) of the lead tracker: what the drawn value should be at that time
    for _, m in msgs:
        if m.get("type") == "lead" and m.get("present"):
            lead_truth.append((m["ts_mono_ns"] / 1e9, m["dist_m"], m.get("track_id")))
    for rx, m in msgs:
        if m.get("type") == "lanes":
            speed.append((rx, (m.get("ego") or {}).get("speed_mps") or 0.0))
        if m.get("type") == "lights":
            ego_light.append((rx, m.get("ego")))

    # ---- backend
    b = SceneBuilder(SceneFeedConfig())
    published, mixed = [], 0
    for i, (rx, m) in enumerate(msgs):
        nxt = msgs[i + 1][0] if i + 1 < len(msgs) else rx + 1.0
        b.feed(json.dumps(m, separators=(",", ":")), rx)
        now = rx
        while True:
            sc = b.build(now)
            if sc is not None:
                lanes_seq = b._lanes[1].get("seq") if b._lanes and b._lanes[1] else None
                if lanes_seq is not None and lanes_seq != sc["seq"]:
                    mixed += 1
                published.append((now, sc))
            due = b.time_to_due(now)
            if due is None or now + due >= nxt:
                break
            now += max(due, 1e-4)
    # ---- ZMQ poll (50 ms, random phase, latest only) + link
    phase = rnd.random() * 0.05
    arrivals, j, pending = [], 0, None
    t = msgs[0][0] + phase
    end = msgs[-1][0] + 1.0
    while t < end:
        while j < len(published) and published[j][0] <= t:
            pending = published[j][1]
            j += 1
        if pending is not None:
            arrivals.append((t + A.link_ms / 1e3 * (0.5 + rnd.expovariate(2.0)), pending))
            pending = None
        t += 0.05
    arrivals.sort(key=lambda p: p[0])

    # ---- frontend
    tune = Tuning()
    clock = [0.0]
    feed = SceneFeed(clock=lambda: clock[0])
    director = SceneDirector(tune)
    renderer = None
    if not A.no_render:
        import pygame
        pygame.init()
        from cyberpunk_computer.ui.scene.render import SceneRenderer
        renderer = SceneRenderer((238, 216), tuning=tune)
    dt = 1.0 / tune.fps_scene
    health = {"lights": None}
    next_health = msgs[0][0]
    rows = []
    strip = None
    if A.strip and renderer is not None:
        st_t, st_n, st_png = A.strip.split(":", 2)
        strip = {"t": msgs[0][0] + float(st_t), "n": int(st_n), "png": st_png, "frames": []}
    ai = si = li = 0
    cur_speed = 0.0
    cur_ego = None
    t = msgs[0][0] + 0.5
    while t < end - 0.5:
        clock[0] = t
        while ai < len(arrivals) and arrivals[ai][0] <= t:
            feed.push("roadsense_scene", arrivals[ai][1])
            ai += 1
        while si < len(speed) and speed[si][0] <= t:
            cur_speed = speed[si][1]
            si += 1
        while li < len(ego_light) and ego_light[li][0] <= t:
            cur_ego = ego_light[li][1]
            li += 1
        if t >= next_health:                      # the backend's 5 s health mirror
            next_health += 5.0
            health = {"lights": cur_ego}
        state = NS(vehicle=NS(real_speed_kmh=cur_speed * 3.6, display_speed_kmh=cur_speed * 3.6, gear="DRIVE",
                              ready_mode=True, acc_on=True),
                   roadsense=NS(state="running", api_ok=True, health=health, scene=None))
        inp = read_inputs(state, feed, tune.map_limit)
        director.update(inp, dt)
        redraw = False
        if renderer is not None:
            n0 = renderer.redraws
            renderer.render(director, inp, t)
            redraw = renderer.redraws != n0
            if strip is not None and t >= strip["t"] and len(strip["frames"]) < strip["n"]:
                strip["frames"].append(renderer.surface.copy())
        lp = getattr(director, "light_pos", None)
        if lp is None and inp.light is not None and inp.light.dist_m is not None:
            lp = (inp.light.dist_m, inp.light.lat_m if inp.light.lat_m is not None else -4.5)
        latest = feed.latest()
        rows.append({"t": t, "v": cur_speed, "seq": latest.get("seq") if latest else None,
                     "lead": None if director.lead is None or director.lead_alpha < 0.5 else
                     (director.lead.dist_m, director.lead.lat_m, director.lead.v_rel or 0.0),
                     "readout": getattr(director, "lead_shown_m", None) if director.lead is not None else None,
                     "light": lp, "light_id": (inp.light.oid if inp.light is not None and hasattr(inp.light, "oid") else None),
                     "rs_ego": cur_ego is not None, "redraw": redraw, "road_view": director.view_step == 14,
                     # a line may end before 30 m (a display lane source reports its own reach): None there
                     "lanes": [None if director.lanes[i] is None or len(director.lanes[i]) < 3 else
                               (director.lanes[i][2], director.lanes[i][5] if len(director.lanes[i]) > 5 else None)
                               for i in (1, 2)]})
        t += dt

    if strip is not None and strip["frames"]:
        import pygame
        w, h = strip["frames"][0].get_size()
        out = pygame.Surface((w * len(strip["frames"]) + 2 * (len(strip["frames"]) - 1), h))
        out.fill((60, 60, 60))
        for i, fr in enumerate(strip["frames"]):
            out.blit(fr, (i * (w + 2), 0))
        pygame.image.save(pygame.transform.scale_by(out, 2), strip["png"])
    dur = rows[-1]["t"] - rows[0]["t"]
    print("%s%s: %.0f s, %d scenes published (%.1f/s), %d reached the Pi (%.1f/s); scenes with lead/lanes of "
          "another frame: %d (%.1f %%)" % (os.path.basename(A.ws), " [no view]" if A.no_view else "", dur,
                                            len(published), len(published) / dur, len(arrivals), len(arrivals) / dur,
                                            mixed, 100.0 * mixed / max(1, len(published))))
    ages = [r["t"] - frame_ts[r["seq"]] for r in rows if r["seq"] in frame_ts]
    print("  age of the newest sample at the drawn frame: median %.0f ms, p95 %.0f ms" % (
        1e3 * pct(ages, 0.5), 1e3 * pct(ages, 0.95)))
    ldx, ldy, ghost, shown = [], [], 0, 0
    lead_d, readout_changes, stand_t = [], 0, 0.0
    import bisect
    lt_t = [x[0] for x in lead_truth]
    lead_err = []
    for r in rows:
        if not r["lead"]:
            continue
        k = bisect.bisect_left(lt_t, r["t"])
        if 0 < k < len(lt_t) and lt_t[k] - lt_t[k - 1] < 0.2 and lead_truth[k][2] == lead_truth[k - 1][2]:
            a, c = lead_truth[k - 1], lead_truth[k]
            truth = a[1] + (c[1] - a[1]) * (r["t"] - a[0]) / (c[0] - a[0])
            lead_err.append(r["lead"][0] - truth)
    lane_steps = {(i, x): [] for i in (0, 1) for x in (0, 1)}
    for r0, r1 in zip(rows, rows[1:]):
        if r1["light"] is not None:
            shown += 1
            if not r1["rs_ego"]:
                ghost += 1
        if r0["light"] is not None and r1["light"] is not None and r0["light_id"] == r1["light_id"]:
            ddx = r1["light"][0] - r0["light"][0] + r1["v"] * dt
            if abs(ddx) < 30:
                ldx.append(ddx)
                ldy.append(r1["light"][1] - r0["light"][1])
        if r0["lead"] and r1["lead"]:
            step = r1["lead"][0] - r0["lead"][0] - r1["lead"][2] * dt
            if abs(step) < 5.0:
                lead_d.append(step)
            if r1["v"] < 0.1 and abs(r1["lead"][2]) < 0.15 and r1["road_view"]:   # both stand, road view open
                stand_t += dt
                if r0["readout"] != r1["readout"]:
                    readout_changes += 1
        for k in (0, 1):
            a, c = r0["lanes"][k], r1["lanes"][k]
            if a is not None and c is not None:
                for x in (0, 1):
                    if a[x] is not None and c[x] is not None:
                        lane_steps[(k, x)].append(c[x] - a[x])
    print("  ego light drawn: step after the car's motion %s; lateral %s" % (stats(ldx, 2.0), stats(ldy, 0.5)))
    print("  ego light drawn on %d frames, of them %d while roadsense had no ego light (%.1f %%)" % (
        shown, ghost, 100.0 * ghost / max(1, shown)))
    print("  lead drawn: step after its relative speed %s; read-out changes while standing: %d in %.0f s (%.1f/min)"
          % (stats(lead_d, 0.5), readout_changes, stand_t, 60.0 * readout_changes / max(1.0, stand_t)))
    print("  lead drawn against the tracker's value at the drawn moment: %s, bias %+.3f m" % (
        stats(lead_err, 1.0), sum(lead_err) / max(1, len(lead_err))))
    for k, name in ((0, "left"), (1, "right")):
        for x, xm in ((0, 10), (1, 30)):
            print("  %-5s lane line at %d m: %s" % (name, xm, stats(lane_steps[(k, x)], 0.3)))
    if renderer is not None:
        st_rows = [r for r in rows if r["v"] < 0.1 and r["lead"] is not None and r["road_view"]]
        n = sum(1 for r in st_rows if r["redraw"])
        longest, run, prev_t = 0.0, 0.0, None
        for r in st_rows:                         # longest stretch of standing frames without a redraw
            if r["redraw"] or prev_t is None or r["t"] - prev_t > 1.5 * dt:
                run = 0.0
            else:
                run += dt
            longest = max(longest, run)
            prev_t = r["t"]
        gaps = [longest]
        tot = len(st_rows) * dt
        lead_moved = sum(1 for a, c in zip(st_rows, st_rows[1:]) if abs(c["lead"][0] - a["lead"][0]) > 0.0)
        print("  standing with a lead (%.0f s): %.1f scene redraws/s, longest without a redraw %.1f s "
              "(the drawn lead value changed on %d of %d frames)" % (
                  tot, n / max(tot, 1e-9), max(gaps, default=float("nan")), lead_moved, len(st_rows)))


if __name__ == "__main__":
    main()
