"""Map-layer drives for the MFD main screen: record real roadsense output, render it.

Development tool for the map elements of the scene (docs/MFD_MAIN_SCREEN.md 6.5).
Nothing here runs on the car.

    # 1. record: roadsense's fake camera daemons + the REAL chris-integrator with its map box
    #    (chris-mapper) on a map pack, driven in real time by roadsense's synthetic drive into the
    #    integrator's position.sock; writes the consolidated API's `fused` and `map` WebSocket
    #    messages with their arrival times (gzip JSON lines). PRIUS_ROADSENSE_REPO = a roadsense
    #    tree with chris-integrator/ and chris-mapper/ (>= L39). Lane L44: until then this ran the
    #    legacy mapd + roadsense api (the layout the car left); `--legacy` still does.
    .venv/bin/python tools/mfd_map_drive.py record --demo opole-rural --out /tmp/opole.jsonl.gz

    # 2. list what a recording holds (limit changes, curves, tunnels, junctions by time)
    .venv-fe/bin/python tools/mfd_map_drive.py events /tmp/opole.jsonl.gz

    # 3. render: replay a recording through the backend's SceneBuilder into the real
    #    MainScreen and write frames (native 480x240, upscaled copies with a caption)
    .venv-fe/bin/python tools/mfd_map_drive.py render /tmp/opole.jsonl.gz --at 120 --frames 8 \
        --every 2.0 --out /tmp/frames --name limit_change

A synthetic drive is flagged in the data (``map.syn``, ``map.in.gnss == "synthetic"``) and
the screen ignores the map then. ``render`` therefore re-labels the recording as a real
receiver (``--as-real``, the default here and nowhere else) -- that is the whole point of
the tool: to see what the screen does with real map output before a GNSS source exists.

The camera side of a rendered frame is not recorded (roadsense's fake camera daemons draw
a fixed straight road): the lanes are derived from the map's own road shape for the first
100 m, so camera and map agree, as on a well matched real drive. ``--camera straight``
keeps a straight camera road instead (camera and map then disagree in curves).
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HOME = os.path.expanduser("~")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROADSENSE = os.environ.get("PRIUS_ROADSENSE_REPO", os.path.join(HOME, "projects", "roadsense"))
PACKS = os.path.join(HOME, "roadsense-maps", "packs")
sys.path.insert(0, REPO)


# ── record ───────────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _default_pack() -> str:
    import glob
    poland = sorted(glob.glob(os.path.join(PACKS, "poland-*.rsmap")))
    return poland[-1] if poland else os.path.join(PACKS, "dev-opolskie.rsmap")


def record(a) -> int:
    from cyberpunk_computer.backend.roadsense_scene import MiniWebSocket
    py = os.environ.get("PRIUS_ROADSENSE_PYTHON") or next(
        (p for p in (os.path.join(ROADSENSE, ".venv-models", "bin", "python"),
                     os.path.join(HOME, "projects", "roadsense", ".venv-models", "bin", "python"))
         if os.path.exists(p)), "python3")
    pack = a.pack or _default_pack()
    tmp = tempfile.mkdtemp(prefix="mfd-map-drive-")
    rundir = os.path.join(tmp, "run")
    chris_rundir = os.path.join(tmp, "chris")
    for d in (rundir, chris_rundir, os.path.join(tmp, "etc"), os.path.join(tmp, "data")):
        os.makedirs(d)
    port = _free_port()
    chris = os.path.join(ROADSENSE, "chris-integrator")
    legacy = a.legacy or not os.path.isdir(chris)
    if legacy:
        print("legacy layout: mapd + roadsense api (%s has no chris-integrator/)" % ROADSENSE, file=sys.stderr)
    env = dict(os.environ, ROADSENSE_CONFIG_DIR=os.path.join(tmp, "etc"), CHRIS_CONFIG_DIR=os.path.join(tmp, "etc"),
               CHRIS_MAP_PACK=pack,
               PYTHONPATH=os.pathsep.join([os.path.join(ROADSENSE, "mapd"), chris,
                                           os.path.join(ROADSENSE, "chris-mapper")]))
    feed_rundir = rundir if legacy else chris_rundir     # where position.sock is (synth_drive's --rundir)
    logf = open(os.path.join(tmp, "procs.log"), "wb")
    procs = []
    n = 0
    try:
        procs.append(subprocess.Popen(
            [py, os.path.join(ROADSENSE, "tools", "fake_daemons.py"), "--rundir", rundir, "--no-mapd"],
            stdout=logf, stderr=subprocess.STDOUT))
        if legacy:
            procs.append(subprocess.Popen(
                [py, "-m", "roadsense_mapd", "--rundir", rundir, "--pack", pack, "--stats", "0"],
                env=env, stdout=logf, stderr=subprocess.STDOUT))
            procs.append(subprocess.Popen(
                [py, "-m", "roadsense_api", "--rundir", rundir, "--datadir", tmp,
                 "--ring", os.path.join(rundir, "roadsense-frames"), "--host", "127.0.0.1", "--port", str(port)],
                cwd=os.path.join(ROADSENSE, "api"), stdout=logf, stderr=subprocess.STDOUT))
        else:
            procs.append(subprocess.Popen(
                [py, "-m", "chris_integrator", "--rundir", chris_rundir, "--datadir", os.path.join(tmp, "data"),
                 "--config-dir", os.path.join(tmp, "etc"), "--roadsense", rundir, "--host", "127.0.0.1",
                 "--http-port", str(port)], env=env, cwd=tmp, stdout=logf, stderr=subprocess.STDOUT))
        ws = None
        end = time.monotonic() + 60
        while ws is None:
            try:
                ws = MiniWebSocket.connect("127.0.0.1", port, "/ws?topics=fused,map", 3.0)
            except OSError:
                if time.monotonic() > end:
                    raise
                time.sleep(0.5)
        time.sleep(3.0)
        cmd = [py, os.path.join(ROADSENSE, "tools", "synth_drive.py"), "--pack", pack, "--rundir", feed_rundir]
        cmd += ["--demo", a.demo] if a.demo else ["--from", a.start, "--to", a.goal]
        for w in a.dropout or ():
            cmd += ["--dropout", w]
        cmd += a.synth or []
        drive = subprocess.Popen(cmd, env=env, stdout=logf, stderr=subprocess.STDOUT)
        procs.append(drive)
        t0 = time.monotonic()
        tail = None
        with gzip.open(a.out, "wt") as out:
            while True:
                now = time.monotonic()
                if drive.poll() is not None and tail is None:
                    tail = now + a.tail            # keep recording: residue after the drive
                if (tail is not None and now > tail) or now - t0 > a.max_s:
                    break
                line = ws.recv(0.5)
                if line is None:
                    continue
                out.write("%.2f\t%s\n" % (time.monotonic() - t0, line))
                n += 1
        ws.close()
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        logf.close()
        if a.keep:
            print("work dir kept:", tmp, file=sys.stderr)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    print("%s: %d messages" % (a.out, n), file=sys.stderr)
    return 0


def read_recording(path: str):
    """[(t, message dict)] of a recording, in time order."""
    out = []
    try:
        with gzip.open(path, "rt") as f:
            for ln in f:
                t, _, raw = ln.partition("\t")
                try:
                    out.append((float(t), json.loads(raw)))
                except ValueError:
                    continue
    except EOFError:           # a recording still being written
        pass
    return out


# ── replay ───────────────────────────────────────────────────────────────────

def as_real(msg: dict) -> dict:
    """A recorded message of a synthetic drive, re-labelled as a real receiver, and
    with the fake camera's constant "50" sign taken out of the fused limit (the map's
    own limit stays: as if the camera had read no sign)."""
    if msg.get("type") == "map":
        inp = msg.get("input")
        if isinstance(inp, dict):
            if isinstance(inp.get("gnss"), dict):
                inp["gnss"]["source"] = "headunit"
            if isinstance(inp.get("motion"), dict):
                inp["motion"]["tag"] = "prius-can"
            inp["synthetic"] = False
    elif msg.get("type") == "view":
        rd = msg.get("road")
        if isinstance(rd, dict):
            if isinstance(rd.get("map"), dict):
                rd["map"]["synthetic"] = False
            if isinstance(rd.get("position"), dict):
                rd["position"]["synthetic"] = False
    elif msg.get("type") == "fused":
        sl = msg.get("speed_limit")
        if isinstance(sl, dict):
            mp = sl.get("map") if isinstance(sl.get("map"), dict) else None
            if mp and mp.get("limit_kmh"):
                msg["speed_limit"] = {"limit_kmh": mp["limit_kmh"],
                                      "source": "map" if mp.get("source") == "maxspeed" else "map_implicit",
                                      "confidence": mp.get("confidence"), "rule": mp.get("rule"),
                                      "agreement": "map_only"}
            else:
                msg["speed_limit"] = {"limit_kmh": None, "source": "unknown"}
    return msg


class Pose:
    """The car on the map's road shape: where it is ``adv`` metres after the map message."""

    def __init__(self, shape: dict):
        self.x = [float(v) for v in shape["x"]]
        self.y = [float(v) for v in shape["y"]]          # right positive
        self.s = [float(v) for v in shape["s"]]

    def _at(self, s: float):
        xs, ys, ss = self.x, self.y, self.s
        for i in range(1, len(ss)):
            if s <= ss[i] or i == len(ss) - 1:
                t = (s - ss[i - 1]) / ((ss[i] - ss[i - 1]) or 1.0)
                return xs[i - 1] + (xs[i] - xs[i - 1]) * t, ys[i - 1] + (ys[i] - ys[i - 1]) * t
        return xs[-1], ys[-1]

    def lateral(self, adv: float, dists) -> list:
        """Lateral (right positive) of the road centre at ``dists`` m ahead of the car
        ``adv`` m along the shape, in the car's frame there."""
        px, py = self._at(adv)
        qx, qy = self._at(adv + 4.0)
        th = math.atan2(qy - py, qx - px)
        c, sn = math.cos(th), math.sin(th)
        out = []
        for dist in dists:
            x, y = self._at(adv + dist)
            out.append(-(x - px) * sn + (y - py) * c)
        return out


PLACES = {"motorway": "highway", "trunk": "highway", "motorway_link": "highway", "trunk_link": "highway"}


class Replay:
    """A recording as the frontend would get it: scene events from the backend's own
    ``SceneBuilder``, the camera's lanes derived from the map's road shape."""

    def __init__(self, path: str, real: bool = True, camera: str = "map", drops=(), light: str = "night",
                 hacc_add=None):
        from cyberpunk_computer.backend.roadsense_scene import SceneBuilder, SceneFeedConfig
        self.msgs = read_recording(path)
        self.real, self.camera, self.drops, self.light = real, camera, tuple(drops), light
        self.builder = SceneBuilder(SceneFeedConfig(max_hz=50.0))
        self.i = 0
        self.seq = 0
        self.map_msg = None
        self.map_t = 0.0
        self.fused = None
        self.speed = 0.0
        self.hacc_add = hacc_add
        # a recording of roadsense's tools/roadgeom_drive.py carries the camera side too
        # (road, lanes, view with view.road): it is replayed as it is, nothing is derived here
        self.full = any(m.get("type") == "view" for _, m in self.msgs)
        self._last_sc: dict = {}

    def seek(self, t: float) -> None:
        """Feed every message up to ``t`` (messages inside a drop window are withheld)."""
        while self.i < len(self.msgs) and self.msgs[self.i][0] <= t:
            mt, msg = self.msgs[self.i]
            self.i += 1
            if any(a <= mt < b for a, b in self.drops):
                continue
            if self.real:
                msg = as_real(msg)
            if self.hacc_add and msg.get("type") == "fused" and isinstance(msg.get("position"), dict):
                a, b, extra = self.hacc_add
                if a <= mt < b:
                    msg["position"]["hacc_m"] = (msg["position"].get("hacc_m") or 0.0) + extra * (mt - a) / (b - a)
            if msg.get("type") == "map":
                self.map_msg, self.map_t = msg, mt
            elif msg.get("type") == "fused":
                self.fused = msg
                pos = msg.get("position")
                if isinstance(pos, dict) and pos.get("speed_mps") is not None:
                    self.speed = float(pos["speed_mps"])
            self.builder.feed(json.dumps(msg), mt)

    def scene(self, t: float) -> dict:
        from cyberpunk_computer.ui.scene.inputs import DEFAULT_XS
        self.seek(t)
        if self.full:
            sc = self.builder.build(t)
            if sc is not None:
                road = (self.fused or {}).get("road") or {}
                bu = ((self.fused or {}).get("built_up") or {}).get("map") or {}
                sc["env"] = {"light": self.light, "weather": "clear",
                             "place": PLACES.get(road.get("class"), "city_street" if bu.get("value") else "rural_road")}
                sc.pop("limit", None)
                self._last_sc = sc
            return self._last_sc
        self.seq += 1
        self.builder.feed(json.dumps({"type": "road", "seq": self.seq, "lane_lines": [], "road_edges": [],
                                      "leads": []}), t)
        sc = self.builder.build(t) or {"seq": self.seq}
        xs = DEFAULT_XS
        cen = [0.0] * len(xs)
        sh = ((self.map_msg or {}).get("ahead") or {}).get("shape") if self.camera == "map" else None
        if sh and t - self.map_t < 3.0:
            cen = Pose(sh).lateral(self.speed * (t - self.map_t), xs)
        fl = (self.fused or {}).get("lanes") or {}
        fwd, back, ego = fl.get("forward") or 1, fl.get("backward") or 0, fl.get("ego_index")
        ego = 0 if ego is None else ego

        def line(off_right: float, prob: float) -> dict:
            return {"y": [round(c + off_right, 2) for c in cen], "prob": prob}

        far_l = line(-5.25, 0.85) if (ego > 0 or back > 0) else None
        far_r = line(5.25, 0.85) if ego < fwd - 1 else None
        sc["x"] = list(xs)
        sc["lanes"] = [far_l, line(-1.75, 0.95), line(1.75, 0.93), far_r]
        sc["edges"] = [line(-5.7 if far_l else -2.4, 1.0), line(5.8 if far_r else 2.4, 1.0)]
        sc["path"] = {"y": [round(c, 2) for c in cen]}
        sc["lead"] = None
        road = (self.fused or {}).get("road") or {}
        tun = (self.fused or {}).get("tunnel") or {}
        bu = ((self.fused or {}).get("built_up") or {}).get("map") or {}
        place = "tunnel" if tun.get("in_tunnel") else PLACES.get(
            road.get("class"), "city_street" if bu.get("value") else "rural_road")
        sc["env"] = {"light": self.light, "place": place, "weather": "clear"}
        sc.pop("limit", None)
        return sc


class Player:
    """The real MainScreen fed by a Replay with a simulated clock."""

    def __init__(self, rep: Replay, t0: float, tuning=None, warm: float = 6.0):
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
        import pygame
        from cyberpunk_computer.ui.scene import demo
        pygame.init()
        pygame.display.set_mode(demo.SIZE)
        self.pygame, self.demo, self.rep = pygame, demo, rep
        self.sim = demo.Sim("live", mods={"tuning": tuning} if tuning else None)
        self.surf = pygame.Surface(demo.SIZE)
        self.t = t0 - warm
        self.sim.t = self.t
        self._last_scene = -1.0
        self.run_to(t0)

    def step(self, dt: float = 0.05) -> None:
        from dataclasses import replace
        from cyberpunk_computer.ui.scene.inputs import SCENE_EVENT
        self.t += dt
        self.sim.t = self.t
        if self.t - self._last_scene >= 0.1 - 1e-6:
            self._last_scene = self.t
            self.sim.feed.push(SCENE_EVENT, self.rep.scene(self.t))
        st = self.demo.state_at(19.5, "live")
        kmh = max(self.rep.speed * 3.6, 12.0)
        st = replace(st, vehicle=replace(st.vehicle, speed_kmh=kmh, real_speed_kmh=kmh,
                                         display_speed_kmh=float(int(kmh + 0.5))))
        self.sim.store.push(st)
        self.sim.screen.update(dt)

    def run_to(self, t: float) -> None:
        while self.t < t - 1e-9:
            self.step()

    def frame(self):
        scene = self.sim.screen.scene_page.scene
        if scene is not None:
            scene.invalidate()
        self.sim.frame(self.surf)
        return self.surf.copy()


def caption(pygame, img, text: str, scale: int):
    """``img`` upscaled with a caption line under it."""
    from cyberpunk_computer.ui.fonts import get_mono_font
    big = pygame.transform.scale(img, (img.get_width() * scale, img.get_height() * scale))
    font = get_mono_font(14 if scale < 3 else 28)
    h = font.get_height() + 8
    out = pygame.Surface((big.get_width(), big.get_height() + h))
    out.fill((10, 12, 22))
    out.blit(big, (0, 0))
    out.blit(font.render(text, False, (200, 210, 220)), (8, big.get_height() + 4))
    return out


def sheet(pygame, tiles, cols: int):
    """Captioned tiles on one sheet."""
    cols = max(1, min(cols, len(tiles)))
    rows = (len(tiles) + cols - 1) // cols
    tw, th = tiles[0].get_size()
    out = pygame.Surface((cols * tw + (cols - 1) * 6, rows * th + (rows - 1) * 6))
    out.fill((40, 44, 60))
    for i, t in enumerate(tiles):
        out.blit(t, ((i % cols) * (tw + 6), (i // cols) * (th + 6)))
    return out


def describe(rep: Replay, d) -> str:
    """What the map elements show, for a frame's caption."""
    m = d.map
    parts = []
    if m.next_limit:
        parts.append("next %d in %d m" % (m.next_limit.kmh, m.next_limit.m))
    if m.tunnel_exit_m is not None:
        parts.append("exit %d m" % m.tunnel_exit_m)
    if m.far is not None and m.far_alpha > 0:
        parts.append("far road%s" % (" > " + m.far.end if m.far.end else ""))
    if m.lanes:
        parts.append("lanes %d+%d ego %d" % (m.lanes.fwd, m.lanes.back, m.lanes.ego))
    if m.label:
        parts.append("label %s %s" % (m.label.ref, m.label.name))
    if m.mark:
        parts.append("map " + m.mark)
    return ", ".join(parts) or "no map element"


def render(a) -> int:
    drops = [tuple(float(x) for x in w.split(":")) for w in a.drop or ()]
    hacc = tuple(float(x) for x in a.hacc_add.split(":")) if a.hacc_add else None
    tuning = json.loads(a.tuning) if a.tuning else None
    rep = Replay(a.rec, real=not a.synthetic, camera=a.camera, drops=drops, light=a.light, hacc_add=hacc)
    pl = Player(rep, a.at, tuning)
    pg = pl.pygame
    os.makedirs(a.out, exist_ok=True)
    tiles = []
    for i in range(a.frames):
        t = a.at + i * a.every
        pl.run_to(t)
        img = pl.frame()
        d = pl.sim.screen.scene_page.director
        text = "t=%.1fs  %.0f km/h  %s" % (t, rep.speed * 3.6, describe(rep, d))
        if a.frames == 1 and a.caption:
            text = a.caption
        print(text)
        if a.native:
            pg.image.save(img, os.path.join(a.out, "%s_%02d.png" % (a.name, i + 1)))
        tiles.append(caption(pg, img, text, a.scale))
    if a.frames == 1:
        pg.image.save(tiles[0], os.path.join(a.out, a.name + ".png"))
    else:
        pg.image.save(sheet(pg, tiles, a.cols), os.path.join(a.out, a.name + ".png"))
    return 0


def events(a) -> int:
    """Timeline of a recording: what changes when."""
    prev = {}
    for t, msg in read_recording(a.rec):
        if msg.get("type") != "fused":
            continue
        mp, road, ah = msg.get("map") or {}, msg.get("road") or {}, msg.get("ahead") or {}
        sl, tun, ln, pos = (msg.get("speed_limit") or {}), msg.get("tunnel") or {}, msg.get("lanes") or {}, \
            msg.get("position") or {}
        nxt = (ah.get("speed_limit_changes") or [None])[0]
        jn = [j for j in ah.get("junctions") or () if not j.get("minor")]
        cur = {"map": (mp.get("available"), mp.get("state"), mp.get("reason")),
               "road": (road.get("class"), road.get("ref"), road.get("name")),
               "limit": ((sl.get("map") or {}).get("limit_kmh"), (sl.get("map") or {}).get("source")),
               "next": (nxt.get("limit_kmh"), nxt.get("source"), nxt.get("confidence")) if nxt else None,
               "tunnel": tun.get("in_tunnel"),
               "lanes": (ln.get("forward"), ln.get("backward"), ln.get("ego_index"), ln.get("confidence")),
               "jkind": jn[0].get("kind") if jn else None}
        if cur != prev:
            ch = [k for k in cur if cur[k] != prev.get(k)]
            print("%7.1f  %5.1f km/h  hacc %4.1f  conf %s  pc %s  %s" % (
                t, (pos.get("speed_mps") or 0) * 3.6, pos.get("hacc_m") or -1, mp.get("match_confidence"),
                ah.get("path_confidence"),
                "  ".join("%s=%s" % (k, cur[k]) for k in ch)
                + ("  next@%s" % nxt.get("dist_m") if nxt and "next" in ch else "")
                + ("  j@%s c=%s" % (jn[0].get("dist_m"), jn[0].get("confidence")) if jn and "jkind" in ch else "")
                + ("  exit@%s" % tun.get("exit_in_m") if tun.get("in_tunnel") and "tunnel" in ch else "")))
            prev = cur
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--demo", help="a roadsense synth_drive demo: warsaw-s2-tunnel, warsaw-city, krakow-city, opole-rural")
    r.add_argument("--from", dest="start", metavar="LAT,LON")
    r.add_argument("--to", dest="goal", metavar="LAT,LON")
    r.add_argument("--pack")
    r.add_argument("--out", required=True)
    r.add_argument("--dropout", action="append", metavar="T0:T1", help="seconds without a fix")
    r.add_argument("--max-s", type=float, default=3600.0)
    r.add_argument("--tail", type=float, default=8.0, help="seconds recorded after the drive ended")
    r.add_argument("--keep", action="store_true")
    r.add_argument("--legacy", action="store_true", help="the old layout: roadsense mapd + api instead of chris-integrator")
    r.add_argument("synth", nargs="*", help="further synth_drive options (after --)")
    r.set_defaults(fn=record)
    e = sub.add_parser("events")
    e.add_argument("rec")
    e.set_defaults(fn=events)
    d = sub.add_parser("render")
    d.add_argument("rec")
    d.add_argument("--at", type=float, required=True, help="recording time of the first frame (s)")
    d.add_argument("--frames", type=int, default=1)
    d.add_argument("--every", type=float, default=2.0, help="seconds between frames")
    d.add_argument("--out", required=True)
    d.add_argument("--name", required=True, help="file name stem")
    d.add_argument("--caption")
    d.add_argument("--cols", type=int, default=3)
    d.add_argument("--scale", type=int, default=2)
    d.add_argument("--native", action="store_true", help="also write each frame at 480x240")
    d.add_argument("--tuning", help="scene tuning overrides (JSON object)")
    d.add_argument("--camera", choices=("map", "straight"), default="map")
    d.add_argument("--light", default="night")
    d.add_argument("--drop", action="append", metavar="T0:T1", help="withhold the map messages of this window")
    d.add_argument("--hacc-add", metavar="T0:T1:M", help="position accuracy growing by M metres over the window")
    d.add_argument("--synthetic", action="store_true", help="leave the recording flagged synthetic")
    d.set_defaults(fn=render)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
