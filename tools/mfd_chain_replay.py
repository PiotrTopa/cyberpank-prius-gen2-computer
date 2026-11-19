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
        [--tuning '{"map_junction": true}'] [--roadsense ~/projects/roadsense]
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


def _load(name, rel):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, rel))
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
    a = ap.parse_args(argv)
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
    builder = SceneBuilder(SceneFeedConfig(max_hz=10.0))
    md = mapinfo.MapDirector(tun)
    cam = []
    speed = 0.0
    have_ego = False
    timeline = []            # (t, speed, flags)
    last_t = None
    cur = {"pos": False, "map": False, "jv": False}
    arrows = lanes_lit = lanes_likely = 0
    for m in read_session(a.session, a.map):
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
        mi = mapinfo.parse_map(sc)
        geo = mapinfo.parse_geo(sc)
        inp = SimpleNamespace(map=mi, geo=geo, real_speed_kmh=speed * 3.6, limit_kmh=None)
        d = SimpleNamespace(vision="live", moving=speed > 0.5, xs=(), lanes=(None, None, None, None), path=None)
        md.update(inp, d, dt)
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


if __name__ == "__main__":
    sys.exit(main())
