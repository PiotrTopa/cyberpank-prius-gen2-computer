"""MFD UI production-loop bench (run on the Pi with mfd.service stopped).

The real ``Application`` (Renderer -> /dev/fb0 when available) fed by a fake
twin: AppState snapshots at 1 Hz (the backend's ZMQ rate) and
``roadsense_scene`` events at --hz from the demo timeline (ui/scene/demo.py;
drive mode loops its 18..29 s, parked holds 1 s). Prints CPU %, loop and
frame rates, per-part ms and frame-interval percentiles.

    sudo systemctl stop mfd.service
    PYTHONPATH=~/cyberpunk_computer MFD_SCENE_TUNING=/tmp/t.json \
        ~/cyberpunk_computer/.venv/bin/python tools/mfd_bench.py drive 30 --hz 10 [--profile]
    sudo systemctl start mfd.service

(``/tmp/t.json``: scene tuning overrides, e.g. ``{"fps_scene": 20}``.)
Numbers: docs/MFD_MAIN_SCREEN.md section 10.1.
"""
import os, sys, time, argparse, threading, cProfile, pstats
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
ap = argparse.ArgumentParser()
ap.add_argument("mode"); ap.add_argument("seconds", type=float)
ap.add_argument("--hz", type=float, default=10.0); ap.add_argument("--profile", action="store_true")
a = ap.parse_args()
import pygame
from cyberpunk_computer.config import Config
from cyberpunk_computer.core.app import Application
from cyberpunk_computer.ui.scene import demo
from cyberpunk_computer.ui.scene.inputs import SCENE_EVENT

class Twin:
    def __init__(self):
        self.store = demo.DemoStore()
        self.t = 1.0 if a.mode == "parked" else 18.0
        self.state = demo.state_at(self.t, "live")
        self.store.state = self.state
        self.cbs = []; self.seq = 0
        self.last_state = 0.0; self.last_scene = 0.0; self.w0 = None
        class Ing:
            def add_message_log_callback(s, cb): pass
        self.ingress = Ing()
    def add_event_callback(self, cb): self.cbs.append(cb)
    def start(self): return True
    def update(self):
        now = time.monotonic()
        if self.w0 is None: self.w0 = now
        if a.mode == "drive":
            self.t = 18.0 + (now - self.w0) % 11.0
        if now - self.last_state >= 1.0:
            self.last_state = now
            self.store.push(demo.state_at(self.t, "live"))
        if a.mode == "drive" and now - self.last_scene >= 1.0 / a.hz:
            self.last_scene = now
            self.seq += 1
            sc = demo.scene_at(self.t, self.seq)
            if sc is not None:
                for cb in self.cbs: cb(SCENE_EVENT, sc)
        return 0

cfg = Config(dev_mode=False, scale_factor=1, fullscreen=True, gateway_enabled=False)
app = Application(cfg)
print("direct fb:", app.renderer.use_direct_fb)
app.set_virtual_twin(Twin())
# timing taps
acc = {"loop_total": 0.0, "update": 0.0, "render": 0.0, "present": 0.0}
cnt = {"render": 0}
r_render, r_present = app._render, app.renderer.present
def t_present(*k):
    t = time.perf_counter(); r_present(*k); acc["present"] += time.perf_counter() - t
app.renderer.present = t_present
stamps = []
def t_render():
    t = time.perf_counter(); r_render(); acc["render"] += time.perf_counter() - t; cnt["render"] += 1
    if "w0" in marks and "w1" not in marks: stamps.append(t)
app._render = t_render
import cyberpunk_computer.ui.screens.main_screen as MS
r_upd = MS.MainScreen.update
def t_upd(self, dt):
    t = time.perf_counter(); r_upd(self, dt); acc["update"] += time.perf_counter() - t
MS.MainScreen.update = t_upd
import cyberpunk_computer.ui.scene.render as SR
import cyberpunk_computer.ui.screens.base as SB
import cyberpunk_computer.ui.scene.page as SP
taps = {}
def tap(cls, name, key):
    orig = getattr(cls, name)
    def w(self, *k, **kw):
        t = time.perf_counter(); r = orig(self, *k, **kw)
        e = taps.setdefault(key, [0, 0.0]); e[0] += 1; e[1] += time.perf_counter() - t
        return r
    setattr(cls, name, w)
tap(SR.SceneRenderer, "_draw", "scene _draw")
tap(SR.SceneRenderer, "render", "scene render (incl blit-free return)")
tap(SB.Screen, "render", "chrome widgets (Screen.render)")
tap(SP.ScenePage, "render_scene", "page.render_scene (draw+blit)")
tap(SP.ScenePage, "render_strip", "page.render_strip")
tap(SP.ScenePage, "update", "page.update")
tap(MS.MainScreen, "render", "MainScreen.render")
WARM = 6.0
marks = {}
def stopper():
    time.sleep(WARM)
    marks["c0"] = os.times(); marks["w0"] = time.perf_counter(); marks["f0"] = app.frame_count
    marks["r0"] = cnt["render"]; marks["a0"] = dict(acc); taps.clear()
    if prof: prof.enable()
    time.sleep(a.seconds)
    if prof: prof.disable()
    marks["c1"] = os.times(); marks["w1"] = time.perf_counter(); marks["f1"] = app.frame_count
    marks["r1"] = cnt["render"]; marks["a1"] = dict(acc); marks["taps"] = {k: list(v) for k, v in taps.items()}
    app.running = False
prof = cProfile.Profile() if a.profile else None
threading.Thread(target=stopper, daemon=True).start()
app.run()
el = marks["w1"] - marks["w0"]; c0, c1 = marks["c0"], marks["c1"]
cpu = c1.user - c0.user + c1.system - c0.system
loops = marks["f1"] - marks["f0"]; ren = marks["r1"] - marks["r0"]
d = {k: marks["a1"][k] - marks["a0"][k] for k in acc}
page = app.current_screen.scene_page
print("mode=%s hz=%.0f  CPU=%.1f%%  loops/s=%.1f  rendered/s=%.1f  scene redraws=%d" % (
    a.mode, a.hz, 100 * cpu / el, loops / el, ren / el, page.scene.redraws if page.scene else -1))
print("ms per rendered frame: render(compose+present)=%.2f  of which present=%.2f;  update per loop=%.2f ms" % (
    1000 * d["render"] / max(1, ren), 1000 * d["present"] / max(1, ren), 1000 * d["update"] / max(1, loops)))
for k, (n, tt) in sorted(marks["taps"].items()):
    print("  %-40s n/s=%5.1f  avg=%6.2f ms  total=%5.1f %%" % (k, n / el, 1000 * tt / max(1, n), 100 * tt / el))
gaps = sorted(1000 * (b - a) for a, b in zip(stamps, stamps[1:]))
if gaps:
    q = lambda f: gaps[min(len(gaps) - 1, int(f * len(gaps)))]
    print("frame interval ms: p10=%.0f p50=%.0f p90=%.0f p99=%.0f max=%.0f  >75ms: %d of %d" % (
        q(.1), q(.5), q(.9), q(.99), gaps[-1], sum(g > 75 for g in gaps), len(gaps)))
if prof:
    st = pstats.Stats(prof); st.sort_stats("tottime").print_stats(20)
