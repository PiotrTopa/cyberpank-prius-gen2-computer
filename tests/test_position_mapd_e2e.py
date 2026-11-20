"""End to end: the backend's position feeder against the real chris-integrator (lane L44).

Until L44 this ran roadsense's legacy ``mapd`` (+ api); in the chris layout the
vehicle input API, the position estimate and the map box (chris-mapper) live in
chris-integrator, so it runs that instead, as the car does:

    tools/fake_daemons.py --no-mapd  (camerad, modeld, signd, envd, lightd sockets)
      -> python -m chris_integrator --roadsense <run> --rundir <chris>   (consolidated API on a free port)
    PositionFeeder -> <chris>/position.sock      RoadsenseSceneFeed <- ws://127.0.0.1:<port>/ws

Nothing of roadsense / chris is imported. The tree: ``PRIUS_ROADSENSE_REPO`` (a roadsense checkout that has
``chris-integrator/`` and ``chris-mapper/``, i.e. roadsense >= L39; default ~/projects/roadsense). Map pack:
``PRIUS_ROADSENSE_PACK`` or the dev pack ~/roadsense-maps/packs/dev-opolskie.rsmap, else the Poland pack (the
built-in synthetic route is in Opole, inside both). Skips cleanly without the tree, a pack, or a Python with
numpy + aiohttp + PIL (normally ``<repo>/.venv-models``).

Order matters (one integrator for the module): first the no-fix case (head unit silent: no position, map layer
unavailable, vision values pass through), then the synthetic drive (fix, match, dead reckoning in a drop-out),
then the run-time synthetic session and its ``reset``. Real time, about 60 s.

What changed against the mapd version, by design of the integrator (L39 section 2): ``map.input.motion.source`` is
``integrator`` (the pose is the estimator's ``ego_state``) and has no ``tag``; ``map.input.gnss.source`` is the
receiver's tag the feeder sent (``headunit`` / ``synthetic``). What the integrator does not do yet (L39 section 4,
L43 to port): it ignores ``reset``; the reset test reports that as xfail instead of passing.
"""

import glob
import json
import math
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request

import pytest

from cyberpunk_computer.backend.position_feeder import (
    CanMotionTap,
    HeadunitGnssSource,
    PositionFeeder,
    PositionFeederConfig,
    SyntheticGnssSource,
)
from cyberpunk_computer.backend.roadsense_scene import RoadsenseSceneFeed, SceneFeedConfig

HOME = os.path.expanduser("~")
REPO = os.environ.get("PRIUS_ROADSENSE_REPO", os.path.join(HOME, "projects", "roadsense"))
CHRIS = os.path.join(REPO, "chris-integrator")


def _pack():
    env = os.environ.get("PRIUS_ROADSENSE_PACK")
    if env:
        return env if os.path.isfile(env) else None
    packs = os.path.join(HOME, "roadsense-maps", "packs")
    dev = os.path.join(packs, "dev-opolskie.rsmap")
    if os.path.isfile(dev):
        return dev
    poland = sorted(glob.glob(os.path.join(packs, "poland-*.rsmap")))
    return poland[-1] if poland else None


def _python_with(mods):
    for py in (os.path.join(REPO, ".venv-models", "bin", "python"),
               os.path.join(HOME, "projects", "roadsense", ".venv-models", "bin", "python"),
               shutil.which("python3") or ""):
        if py and os.path.exists(py):
            r = subprocess.run([py, "-c", "import " + ",".join(mods)], capture_output=True, timeout=60)
            if r.returncode == 0:
                return py
    return None


PACK = _pack()
HAVE_REPO = (os.path.isfile(os.path.join(CHRIS, "chris_integrator", "__main__.py"))
             and os.path.isfile(os.path.join(REPO, "tools", "fake_daemons.py")))
pytestmark = pytest.mark.skipif(not (HAVE_REPO and PACK),
                                reason="roadsense tree with chris-integrator (>= L39) or map pack absent")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(pred, timeout, step=0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            v = pred()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return None


class Stack:
    def __init__(self, rundir, chris_rundir, api_port):
        self.rundir, self.chris_rundir, self.api_port = rundir, chris_rundir, api_port
        self.procs = []

    def get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.api_port, path), timeout=3) as r:
            return json.loads(r.read())

    def latest_map(self):
        """The newest `map` message of the map box (GET /v2/map; 503 before the first one)."""
        try:
            return self.get("/v2/map")
        except Exception:
            return None


@pytest.fixture(scope="module")
def stack():
    py = _python_with(["numpy", "aiohttp", "PIL"])
    if py is None:
        pytest.skip("no Python with numpy + aiohttp + PIL for chris-integrator and the fake daemons")
    tmp = tempfile.mkdtemp(prefix="prius-feeder-e2e-")
    rundir, chris_rundir = os.path.join(tmp, "run"), os.path.join(tmp, "chris")
    for d in (rundir, chris_rundir, os.path.join(tmp, "etc"), os.path.join(tmp, "data")):
        os.makedirs(d)
    st = Stack(rundir, chris_rundir, _free_port())
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([CHRIS, os.path.join(REPO, "chris-mapper")]),
               CHRIS_MAP_PACK=PACK, CHRIS_CONFIG_DIR=os.path.join(tmp, "etc"))
    logf = open(os.path.join(tmp, "procs.log"), "wb")
    try:
        st.procs.append(subprocess.Popen(
            [py, os.path.join(REPO, "tools", "fake_daemons.py"), "--rundir", rundir, "--no-mapd"],
            stdout=logf, stderr=subprocess.STDOUT))
        st.procs.append(subprocess.Popen(
            [py, "-m", "chris_integrator", "--rundir", chris_rundir, "--datadir", os.path.join(tmp, "data"),
             "--config-dir", os.path.join(tmp, "etc"), "--roadsense", rundir, "--host", "127.0.0.1",
             "--http-port", str(st.api_port)], env=env, cwd=tmp, stdout=logf, stderr=subprocess.STDOUT))
        assert _wait(lambda: os.path.exists(os.path.join(chris_rundir, "position.sock")), 30), \
            "chris-integrator did not open position.sock"
        assert _wait(lambda: (st.latest_map() or {}).get("pack"), 30), "the map box did not come up with the pack"
        assert _wait(lambda: st.get("/v1/fused")["speed_limit"]["vision"] is not None, 30), \
            "chris-integrator / fake daemons did not come up"
        yield st
    finally:
        for p in st.procs:
            p.terminate()
        for p in st.procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        logf.close()
        if os.environ.get("PRIUS_E2E_KEEP"):
            print("kept", tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


class CanSim:
    """Steady broadcast CAN as the panda would deliver it (0x0B4 / wheels / gear at ~70 Hz)."""

    def __init__(self, tap, kmh=36.0):
        self.tap, self.kmh = tap, kmh
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            t = time.monotonic()
            sp = int(self.kmh * 100)
            self.tap.on_frame(0x0B4, bytes([0, 0, 0, 0, 0, sp >> 8, sp & 0xFF, 0]), 0, 0.0, t)
            self.tap.on_frame(0x0B1, bytes([sp >> 8, sp & 0xFF, sp >> 8, sp & 0xFF, 0, 0]), 0, 0.0, t)
            self.tap.on_frame(0x0B3, bytes([sp >> 8, sp & 0xFF, sp >> 8, sp & 0xFF, 0, 0]), 0, 0.0, t)
            self.tap.on_frame(0x120, bytes([0, 0, 0, 0, 16, 0x23, 0, 0]), 0, 0.0, t)
            self._stop.wait(0.014)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join(2)


def _scene_feed(stack, got):
    cfg = SceneFeedConfig(host="127.0.0.1", port=stack.api_port, reconnect_min_s=0.2, road_max_age_s=2.0)
    feed = RoadsenseSceneFeed(cfg, got.append)
    feed.start()
    feed.set_active(True)
    return feed


def _dist_m(a, b):
    dy = (a[0] - b[0]) * 111132.954
    dx = (a[1] - b[1]) * 111319.49 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def test_no_fix_map_unavailable_vision_passes(stack):
    cfg = PositionFeederConfig(enabled=True, source="headunit", rundir=stack.chris_rundir)
    tap = CanMotionTap()
    feeder = PositionFeeder(cfg, HeadunitGnssSource(cfg), tap=tap)
    scenes = []
    sfeed = _scene_feed(stack, scenes)
    with CanSim(tap):
        feeder.start()
        feeder.set_active(True)
        try:
            assert _wait(lambda: feeder.status()["server"] == "chris-integrator", 10), feeder.status()
            st = feeder.status()
            assert st["vehicle_api"] is True and "controls" in st["mapd_accepts"]
            m = _wait(lambda: (lambda x: x if (x.get("input") or {}).get("gnss") else None)(stack.latest_map()), 10)
            assert m, "the map box never saw the feeder's receiver"
            time.sleep(3.0)
            m = stack.latest_map()
            assert m["input"]["gnss"]["source"] == "headunit" and m["input"]["gnss"]["fix"] in ("none", None), (
                m["input"], stack.get("/v2/ego_state")["sources"].get("gnss"))
            assert m["input"]["motion"]["source"] == "integrator" and m["input"]["pose"] == "ego_state"
            assert m["state"] in ("no_source", "unknown") and m["available"] is False
            assert m["reason"] in ("no_position", "position_unknown") and m.get("position") is None
            ego = stack.get("/v2/ego_state")
            assert ego["speed"]["valid"] and abs(ego["speed"]["value"] - 10.0) < 1.0, ego["speed"]   # 36 km/h CAN
            time.sleep(2.0)
            st = feeder.status()
            assert st["connected"] and st["none_sent"] >= 1 and st["motion_sent"] > 10
            assert st["errors"] == 0 and st["gnss"]["has_fix"] is False
            fused = stack.get("/v1/fused")
            assert fused["map"]["available"] is False
            assert fused["speed_limit"]["source"] in ("sign", "sign_held", "vision_default", "unknown")
            sc = _wait(lambda: next((s for s in reversed(scenes) if "map" in s and "limit" in s), None), 10)
            assert sc, "no scene with the map key"
            assert sc["map"]["ok"] is False and sc["map"]["why"] in ("no_position", "position_unknown", "no_mapd",
                                                                     "stale")
            assert "road" not in sc["map"] and "ahead" not in sc["map"]
            assert sc["map"]["in"]["gnss"] == "headunit" and sc["map"]["in"]["motion"] == "integrator"
            if sc["limit"] and sc["map"].get("limit"):
                assert sc["map"]["limit"]["kmh"] == sc["limit"]["kmh"]     # the vision value passes
            # L44: the integrator states the camera's believability; the scene carries it
            assert any("vis" in s for s in scenes), "no scene carried vis"
        finally:
            feeder.stop()
            sfeed.stop()


def test_synthetic_drive_matches_and_dead_reckons(stack):
    cfg = PositionFeederConfig(enabled=True, source="synthetic", rundir=stack.chris_rundir,
                               synthetic_dropouts=((15.0, 20.0),), synthetic_start_m=0.0)
    src = SyntheticGnssSource(cfg)
    feeder = PositionFeeder(cfg, src, tap=CanMotionTap())
    scenes = []
    sfeed = _scene_feed(stack, scenes)
    samples = []
    feeder.start()
    feeder.set_active(True)
    try:
        t0 = time.monotonic()
        while time.monotonic() - t0 < 27.0:
            time.sleep(0.4)
            m = stack.latest_map()
            if m is None:
                continue
            truth = src.drive.latlon()
            samples.append((time.monotonic() - t0, m, truth))
        good = [(t, m, tr) for t, m, tr in samples if m["state"] == "good" and m["available"]]
        assert good, "never good + available: %s" % [(round(t), m["state"], m["reason"]) for t, m, _ in samples]
        last = samples[-1][1]
        assert last["input"]["gnss"]["source"] == "synthetic" and last["input"]["synthetic"] is True
        assert last["input"]["motion"]["source"] == "integrator"
        # the estimated position follows the synthetic truth (GNSS noise 2.5 m correlated + 0.5 m white)
        errs = [_dist_m((m["position"]["lat"], m["position"]["lon"]), tr) for t, m, tr in good if t > 8.0]
        assert errs and max(errs) < 25.0, errs
        assert all(m["match"]["matched"] for t, m, tr in good if t > 8.0)
        # fix lost 15..20 s since the source started: never "good" inside it (the integrator says dead_reckoning
        # or, with this receiver model, mostly "unknown": its position is declared unknown rather than coasted)
        tl = " ".join("%.0f:%s" % (t, m["state"][:4]) for t, m, _ in samples)
        assert not any(m["state"] == "good" for t, m, _ in samples if 16.5 < t < 19.5), tl
        assert any(m["state"] in ("dead_reckoning", "unknown") for t, m, _ in samples if 16.5 < t < 19.5), tl
        late = [m["state"] for t, m, _ in samples if t > 21.0]
        assert late.count("good") >= len(late) // 2, tl
        # L44 finding, measured not asserted: the state flickers good / unknown / dead_reckoning while fixes come
        flips = sum(1 for (_, a, _), (_, b, _) in zip(samples, samples[1:]) if (a["state"] == "good") != (b["state"] == "good"))
        print("\nmap box states (s:state): %s\ngood <-> not good switches: %d" % (tl, flips))
        st = feeder.status()
        assert st["synthetic"] and st["source"] == "synthetic" and st["errors"] == 0
        assert st["gnss_sent"] >= 20 and st["none_sent"] >= 1 and st["motion_sent"] > 200
        sc = _wait(lambda: next((s for s in reversed(scenes) if (s.get("map") or {}).get("ok")), None), 5)
        assert sc, "no scene with the map layer available"
        assert sc["map"]["in"]["gnss"] == "synthetic"
        assert sc["map"]["road"].get("cls") and sc["map"]["pos"]["src"] in ("gnss", "dead_reckoning", "fused",
                                                                            "coast", None)
        assert sc["map"]["limit"] is None or sc["map"]["limit"]["src"] in (
            "sign", "sign_held", "map", "map_implicit", "vision_default", "unknown")
        assert sc["map"].get("syn") is True          # SyntheticTaint: the receiver's tag passed through the map box
    finally:
        feeder.stop()
        sfeed.stop()


def test_session_end_sends_reset_and_the_position_is_forgotten(stack):
    """Run-time synthetic session on a head-unit feeder: start (reset), a fix and a match, stop -> the
    feeder's reset goes out before the head unit's "no fix". The legacy mapd forgot the place at once; the
    integrator of L39..L43 accepts `reset` in its hello but ignores it (L39 section 4): reported as xfail until
    L43 ports it, never passed by force."""
    cfg = PositionFeederConfig(enabled=True, source="headunit", rundir=stack.chris_rundir)
    feeder = PositionFeeder(cfg, HeadunitGnssSource(cfg), tap=CanMotionTap())   # CAN dark: parked
    feeder.start()
    feeder.set_active(True)
    try:
        assert _wait(lambda: feeder.status()["mapd_accepts"], 10), "no hello reply from the integrator"
        assert "reset" in feeder.status()["mapd_accepts"]
        assert feeder.start_synthetic(by="e2e") == (True, None)
        m = _wait(lambda: (lambda x: x if x["state"] == "good" and x["available"] else None)(stack.latest_map()), 20)
        assert m, "the synthetic session never gave a position"
        assert m["input"]["synthetic"] is True
        assert feeder.stop_synthetic("e2e_done")
        st = _wait(lambda: (lambda x: x if x["reset_sent"] == 2 and not x["reset_pending"] else None)(
            feeder.status()), 5)
        assert st and st["last_reset"]["sent"], feeder.status()
        # forgotten = "no_source" (no position ever since the reset), held: "unknown" only says the position is
        # not trusted now (the box had one), and the integrator's state flickers through it while fixes come
        m = _wait(lambda: (lambda x: x if x["state"] == "no_source" else None)(stack.latest_map()), 5)
        held = m is not None and all((time.sleep(0.5) or (stack.latest_map() or {}).get("state")) == "no_source"
                                     for _ in range(4))
        if not held:
            pytest.xfail("chris-integrator ignores the vehicle input API `reset` (L39 section 4; L43 to port): "
                         "the synthetic place is not forgotten (state %s, input.synthetic %s)" % (
                             (stack.latest_map() or {}).get("state"),
                             ((stack.latest_map() or {}).get("input") or {}).get("synthetic")))
        assert m["available"] is False and m.get("position") is None
    finally:
        feeder.stop()
