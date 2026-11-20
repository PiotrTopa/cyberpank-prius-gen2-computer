"""Walk the whole map chain of a recorded drive and say where junctions and roundabouts get lost.

Development tool (nothing here runs on the car). A roadsense drive-recorder session goes through:

    mapd `map` (as recorded, or a replay of it: roadsense tools/map_replay.py --out)
      -> roadsense api: `fused`, `lanes`, the ego lane, and the display channel `view` with the road picture
         `view.road` (roadsense tools/replay_lib.py: the api's derived topics in the api's order)   [roadsense code]
      -> backend SceneBuilder (scene event: map, map.jv, map.lanes, geo, all from view.road)        [this repo]
      -> frontend MapDirector (the gates of docs/MFD_MAIN_SCREEN.md 6.5-6.7)                        [this repo]

and at every labelled junction (a CSV `source,stamp,kind,note` with stamp = camera frame seq, kind
junction_major / roundabout) reports, for the 12 s of driving before the car reaches it, which stage
still knew about it:

    pos     mapd had a position (good / dead_reckoning)
    map     the map layer was usable (mapd available, fused map.available)
    jv      mapd's junction view or ahead.junctions announced a junction within 300 m
    scene   the scene event carried map.jv (backend) or a geo block with side roads / a ring
    mfd     the MapDirector showed the schematic (map_junction) or the road picture with side roads / ring

    .venv-fe/bin/python tools/mfd_chain_replay.py SESSION_DIR --labels LABELS.csv [--map MAP.jsonl]
        [--tuning '{"map_junction": true}'] [--roadsense ~/projects/roadsense] [--car OTHER_CHECKOUT]

Lane L44 (chris-integrator): `--roadsense` may point at a roadsense tree whose `tools/replay_lib.py` is the shim to
`chris_integrator.replay` (roadsense >= L39); the pipeline is then chris-integrator's mixer. `--glass` adds what the
road view shows, scene by scene, through this repository's SceneBuilder, parse_scene and SceneDirector: the share of
time the camera is held not believable ("CAMERA UNSURE", director state `doubt`), the lead unknown ("LEAD ?"), a
vehicle or lane lines drawn, the raw-fallback count (a scene lead without `lead_src`: must be 0 against the
integrator), the on/off switches of the camera mark per minute and the runs of each state. `--shots DIR` (needs
pygame: .venv-fe) renders the glass (480x240, the whole main screen of the demo with the replayed scene) at the first
scene of each camera state run, at most `--max-shots`.

Always printed as well (lane L36): how often the lane boxes (two or more own lanes: one lane alone draws no
box) and their turn arrows switch on or off on the glass (the MapDirector's output per scene), and how often
the number of boxes changes, per minute of session time. `--car` takes this repository's code from another checkout (the code before / after on the same data).
"""

from __future__ import annotations

import argparse
import bisect
import collections
import csv
import json
import os
import sys
from types import SimpleNamespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
CAR = [REPO]                 # the checkout whose car code is replayed (--car)


def _load(name, rel):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, os.path.join(CAR[0], rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("session")
    ap.add_argument("--labels")
    ap.add_argument("--map", help="map messages of a mapd replay (JSON lines) instead of the recorded ones")
    ap.add_argument("--tuning", help="JSON overrides of ui/scene/tuning.py for the MapDirector")
    ap.add_argument("--roadsense", default=os.environ.get("PRIUS_ROADSENSE_REPO",
                                                         os.path.expanduser("~/projects/roadsense")))
    ap.add_argument("--before-s", type=float, default=12.0)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--car", help="another checkout of this repository whose code is replayed")
    ap.add_argument("--glass", action="store_true", help="what the road view shows (L44)")
    ap.add_argument("--shots", metavar="DIR", help="render the glass at each camera state change (pygame)")
    ap.add_argument("--max-shots", type=int, default=8)
    ap.add_argument("--hold", type=float, default=None,
                    help="SceneFeedConfig.invalid_hold_s (how long a lead / lanes valid:false keeps 'unknown')")
    ap.add_argument("--road-model", default="vision",
                    help="road.road_model for sessions recorded without it (chris_integrator.replay.read_session)")
    a = ap.parse_args(argv)
    if a.shots:
        a.glass = True
    if a.car:
        CAR[0] = os.path.abspath(a.car)
    sys.path.insert(0, os.path.join(a.roadsense, "tools"))
    from replay_lib import Pipeline, read_session       # roadsense's own offline pipeline (api 2)
    # loaded by file: the packages' __init__ pull in the whole backend / pygame, which this needs neither of
    rs = _load("roadsense_scene", "cyberpunk_computer/backend/roadsense_scene.py")
    SceneBuilder, SceneFeedConfig = rs.SceneBuilder, rs.SceneFeedConfig
    mapinfo = _load("mapinfo", "cyberpunk_computer/ui/scene/mapinfo.py")
    tuning_mod = _load("scene_tuning", "cyberpunk_computer/ui/scene/tuning.py")

    base = tuning_mod.Tuning()
    tun = SimpleNamespace(**{k: getattr(base, k) for k in dir(base) if not k.startswith("_")})
    for k, v in (json.loads(a.tuning) if a.tuning else {}).items():
        setattr(tun, k, v)

    pipe = Pipeline()
    builder = SceneBuilder(SceneFeedConfig(max_hz=10.0) if a.hold is None
                           else SceneFeedConfig(max_hz=10.0, invalid_hold_s=a.hold))
    glass = _Glass(CAR[0], a.shots, a.max_shots) if a.glass else None
    md = mapinfo.MapDirector(tun)
    cam = []
    speed = 0.0
    have_ego = False
    timeline = []            # (t, speed, flags)
    last_t = None
    cur = {"pos": False, "map": False, "jv": False}
    arrows = lanes_lit = lanes_likely = 0
    flick = {"boxes": 0, "arrows": 0, "count": 0}
    prev_lb = None
    t_first = None
    try:
        msgs = read_session(a.session, a.map, road_model=a.road_model)
    except TypeError:                                   # roadsense before L37: no road_model argument
        msgs = read_session(a.session, a.map)
    for m in msgs:
        t_ns = m.get("ts_mono_ns")
        typ = m.get("type")
        if not isinstance(t_ns, int) or typ not in ("road", "ego", "camera", "speed_limit", "env", "vehicle", "map"):
            continue
        t = t_ns / 1e9
        if typ == "camera":
            cam.append((t_ns, m["seq"]))
            continue
        if typ == "ego":
            speed = abs(m.get("speed_mps") or 0.0)
            have_ego = True
        elif typ == "map":
            cur["pos"] = m.get("state") in ("good", "dead_reckoning")
            ah = m.get("ahead") or {}
            jvw = ah.get("junction_view")
            near = [j for j in ah.get("junctions") or () if j.get("dist_m", 1e9) <= 300 and not j.get("minor")]
            cur["jv"] = bool(m.get("available") and (jvw or near))
            if not have_ego and isinstance(m.get("position"), dict):
                speed = abs(m["position"].get("speed_mps") or 0.0)
        outs = pipe.feed(m)
        for o in outs:
            ot = o.get("type")
            if ot == "fused":
                cur["map"] = bool(o["map"]["available"])
            if ot in ("map", "fused", "lanes", "lead", "view", "road"):
                builder.feed(json.dumps(o), t)
        if typ != "road":
            continue
        sc = builder.build(t)
        if sc is None:
            continue
        dt = 0.1 if last_t is None else max(0.0, min(0.5, t - last_t))
        last_t = t
        if glass is not None:
            glass.step(t, sc, speed, dt)
        mi = mapinfo.parse_map(sc)
        geo = mapinfo.parse_geo(sc)
        inp = SimpleNamespace(map=mi, geo=geo, real_speed_kmh=speed * 3.6, limit_kmh=None)
        d = SimpleNamespace(vision="live", moving=speed > 0.5, xs=(), lanes=(None, None, None, None), path=None)
        md.update(inp, d, dt)
        t_first = t if t_first is None else t_first
        lb = md.lanes
        if dt > 0.0:
            # boxes as drawn: two or more lanes of the own direction (one lane alone draws no box)
            cur_lb = (lb is not None and lb.fwd >= 2, bool(lb is not None and lb.turn), lb.fwd if lb is not None else None)
            if prev_lb is not None:
                flick["boxes"] += cur_lb[0] != prev_lb[0]
                flick["arrows"] += cur_lb[1] != prev_lb[1]
                flick["count"] += cur_lb[0] and prev_lb[0] and cur_lb[2] != prev_lb[2]
            prev_lb = cur_lb
        mp = sc.get("map") or {}
        g = sc.get("geo") or {}
        scene_has = bool(mp.get("jv")) or bool(g.get("br") or g.get("ring"))
        geo_shown = md.geo is not None and bool(md.geo.branches or md.geo.ring)
        mfd = md.junction is not None or geo_shown
        if speed >= 3.0 and md.lanes is not None:
            arrows += bool(md.lanes.turn)
            lanes_lit += md.lanes.ego >= 0
            lanes_likely += md.lanes.ego < 0 and md.lanes.likely >= 0
        timeline.append((t, speed, dict(cur, scene=scene_has, mfd=mfd, sch=md.junction is not None, geo=geo_shown,
                                        approx=md.junction is not None and md.junction_approx,
                                        boxes=md.lanes is not None,
                                        jm=None if md.junction is None else md.junction.m)))

    # time share while driving
    drv = [x for x in timeline if x[1] >= 3.0]
    keys = ("pos", "map", "jv", "scene", "mfd", "sch", "approx", "geo", "boxes", "right")
    for x in timeline:
        x[2].setdefault("right", False)
    share = {k: round(100.0 * sum(x[2][k] for x in drv) / max(1, len(drv)), 1) for k in keys}
    print("driving scenes %d: share of the time  %s" % (len(drv), json.dumps(share)))
    print("lane boxes while driving: own lane lit on %d scenes, likely lane only on %d, with turn arrows on %d" % (
        lanes_lit, lanes_likely, arrows))
    mins = max(1e-9, ((timeline[-1][0] - t_first) if timeline else 0.0) / 60.0)
    print("on the glass over %.1f min: lane boxes on/off %d (%.2f/min), turn arrows on/off %d (%.2f/min), "
          "box count changes %d (%.2f/min)" % (mins, flick["boxes"], flick["boxes"] / mins, flick["arrows"],
                                                flick["arrows"] / mins, flick["count"], flick["count"] / mins))
    if glass is not None:
        glass.report()
    if not a.labels:
        return 0
    tl_t = [x[0] for x in timeline]
    ct = [c[1] for c in cam]
    rows = []
    with open(a.labels) as f:
        for r in csv.DictReader(f):
            if r["kind"] not in ("junction_major", "roundabout"):
                continue
            seq = int(float(r["stamp"]))
            k = bisect.bisect_left(ct, seq)
            if not cam or k >= len(cam):
                continue
            tj = cam[k][0] / 1e9
            i0, i1 = bisect.bisect_left(tl_t, tj - a.before_s), bisect.bisect_right(tl_t, tj)
            win = timeline[i0:i1]
            # "right": the schematic's distance agrees with the distance actually driven to the junction
            rem = 0.0
            for k2 in range(len(win) - 1, -1, -1):
                x = win[k2]
                nxt = win[k2 + 1][0] if k2 + 1 < len(win) else tj
                rem += x[1] * max(0.0, nxt - x[0])
                if x[2]["jm"] is not None and abs(x[2]["jm"] - rem) <= max(30.0, 0.35 * rem):
                    x[2]["right"] = True
            got = {kk: any(x[2][kk] for x in win) for kk in keys}
            rows.append((r["kind"], seq, got))
    tot = collections.Counter()
    for kind, seq, got in rows:
        for kk in keys:
            tot[kk] += got[kk]
        if not a.quiet:
            print("  %-15s seq %6d  %s" % (kind, seq, " ".join(kk if got[kk] else "-" * len(kk) for kk in keys)))
    print("labelled junctions %d; known at stage: %s" % (len(rows), json.dumps({k: tot[k] for k in keys})))
    return 0


class _Glass:
    """What the road view shows (L44): the car's own parse_scene + SceneDirector on every scene."""

    def __init__(self, car, shots_dir=None, max_shots=8):
        import collections as _c
        import types
        pkg = types.ModuleType("_carscene")          # the scene package by path: relative imports, no pygame
        pkg.__path__ = [os.path.join(car, "cyberpunk_computer", "ui", "scene")]
        sys.modules["_carscene"] = pkg
        import importlib
        self.inputs = importlib.import_module("_carscene.inputs")
        self.director = importlib.import_module("_carscene.director").SceneDirector()
        self.hist = _c.Counter()
        self.hist_drv = _c.Counter()
        self.n = self.n_drv = 0
        self.raw_fallback = 0
        self.why = _c.Counter()
        self.runs = []                     # [state, t0, t1, n]
        self.switches = 0
        self.t0 = self.t1 = None
        self.shots_dir, self.max_shots = shots_dir, max_shots
        self.recent = _c.deque(maxlen=12)  # the scenes before a shot (the director's fades)
        self.shots = []                    # (t, state, [scenes])

    def _state(self):
        d = self.director
        if d.vision != "live":
            return d.vision                # doubt | stale | lost | wait
        if d.lead_unknown:
            return "lead_unknown"
        return "live_lead" if d.lead is not None and d.lead_alpha > 0.0 else "live_no_lead"

    def step(self, t, sc, speed, dt):
        I = self.inputs
        road = I.parse_scene(sc)
        kmh = speed * 3.6
        inp = I.SceneInputs(speed_kmh=kmh, real_speed_kmh=kmh, gear="D", ready=True, rs_state="running",
                            rs_ok=True, road=road)
        self.director.update(inp, dt)
        st = self._state()
        d = self.director
        lines = any(ln is not None for ln in d.lanes)
        key = (st, "lines" if lines else "no_lines")
        self.hist[key] += 1
        self.n += 1
        if speed >= 3.0:
            self.hist_drv[key] += 1
            self.n_drv += 1
        if isinstance(sc.get("lead"), dict) and sc.get("lead_src") != "tracked":
            self.raw_fallback += 1
        if d.vision == "doubt":
            self.why[d.vis_why] += 1
        self.t0 = t if self.t0 is None else self.t0
        self.t1 = t
        self.recent.append(sc)
        if not self.runs or self.runs[-1][0] != st:
            if self.runs and (self.runs[-1][0] == "doubt") != (st == "doubt"):
                self.switches += 1
            self.runs.append([st, t, t, 1])
            if self.shots_dir and len(self.shots) < self.max_shots and st in ("doubt", "live_lead", "lead_unknown",
                                                                             "live_no_lead"):
                if not any(x[1] == st for x in self.shots) or st == "doubt":
                    self.shots.append((t, st, list(self.recent)))
        else:
            self.runs[-1][2] = t
            self.runs[-1][3] += 1

    def report(self):
        def share(h, n):
            return {"%s/%s" % k: round(100.0 * v / max(1, n), 1) for k, v in sorted(h.items(), key=lambda kv: -kv[1])}
        mins = max(1e-9, ((self.t1 or 0) - (self.t0 or 0)) / 60.0)
        print("glass: %d scenes over %.1f min, %% of scenes: %s" % (self.n, mins, json.dumps(share(self.hist, self.n))))
        print("glass while driving (>= 3 m/s): %d scenes, %%: %s" % (self.n_drv, json.dumps(share(self.hist_drv,
                                                                                               self.n_drv))))
        print("glass: CAMERA UNSURE on/off %d (%.2f/min); raw-fallback scenes %d; reasons %s" % (
            self.switches, self.switches / mins, self.raw_fallback, json.dumps(dict(self.why.most_common(5)))))
        long_runs = [r for r in self.runs if r[2] - r[1] >= 5.0]
        print("glass runs >= 5 s (state, from, to, s since session start):")
        for st, a0, a1, n in long_runs[:40]:
            print("  %-13s %8.1f .. %8.1f  (%5.1f s, %d scenes)" % (st, a0 - self.t0, a1 - self.t0, a1 - a0, n))
        if self.shots_dir:
            for p in self._render():
                print("glass shot: %s" % p)

    def _render(self):
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
        import pygame
        from cyberpunk_computer.ui.scene import demo
        from cyberpunk_computer.ui.scene.inputs import SCENE_EVENT
        os.makedirs(self.shots_dir, exist_ok=True)
        pygame.init()
        pygame.display.set_mode(demo.SIZE)
        surf = pygame.Surface(demo.SIZE)
        out = []
        for i, (t, st, scenes) in enumerate(self.shots):
            sim = demo.Sim("replay")                   # running stack, no synthetic scenes: the replayed ones
            sim.run_to(19.0)                           # the demo drive: chase view open, 50 km/h
            for k, sc in enumerate(scenes):
                sim.feed.push(SCENE_EVENT, dict(sc, seq=k + 1))
                sim.step(0.1)
            page = sim.screen.scene_page.scene
            if page is not None:
                page.invalidate()
            sim.frame(surf)
            p = os.path.join(self.shots_dir, "%02d_%s_t%.0f.png" % (i + 1, st, t - self.t0))
            pygame.image.save(surf, p)
            pygame.image.save(pygame.transform.scale(surf, (demo.SIZE[0] * 4, demo.SIZE[1] * 4)),
                              p.replace(".png", "@4x.png"))
            out.append(p)
        return out


if __name__ == "__main__":
    sys.exit(main())
