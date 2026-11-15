"""End to end: the backend's position feeder against the real roadsense mapd.

Runs the roadsense checkout (``PRIUS_ROADSENSE_REPO``, default
~/projects/roadsense) as external processes; nothing of roadsense is
imported. Map pack: ``PRIUS_ROADSENSE_PACK`` or the dev pack
~/roadsense-maps/packs/dev-opolskie.rsmap, else the Poland pack (the built-in
synthetic route is in Opole, inside both). Skips cleanly without the
checkout, a pack, or a Python with numpy for mapd.

With roadsense's api interpreter (aiohttp + numpy + PIL, normally
``<repo>/.venv-models``) the whole stack runs: tools/fake_daemons.py
(``--no-mapd``) + mapd + api, and the backend's RoadsenseSceneFeed reads the
api's WebSocket, so the ``fused`` -> scene ``map`` path is checked on real
roadsense output too.

Order matters (one mapd for the module): first the no-fix case (head unit
silent: mapd without position, map layer unavailable, vision values pass
through), then the synthetic drive (fix, match, dead reckoning in a dropout).
Real time, about 40 s.
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
    for py in (os.path.join(REPO, ".venv-models", "bin", "python"), shutil.which("python3") or ""):
        if py and os.path.exists(py):
            r = subprocess.run([py, "-c", "import " + ",".join(mods)], capture_output=True, timeout=60)
            if r.returncode == 0:
                return py
    return None


PACK = _pack()
HAVE_REPO = os.path.isfile(os.path.join(REPO, "mapd", "roadsense_mapd", "__main__.py"))
pytestmark = pytest.mark.skipif(not (HAVE_REPO and PACK), reason="roadsense checkout or map pack absent")


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
    def __init__(self, rundir, api_port):
        self.rundir, self.api_port = rundir, api_port
        self.procs = []

    def latest_map(self):
        """The newest `map` message (mapd sends its last line to a new client)."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(os.path.join(self.rundir, "mapd.sock"))
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    return None
                buf += chunk
            return json.loads(buf.split(b"\n", 1)[0])

    def get(self, path):
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.api_port, path), timeout=3) as r:
            return json.loads(r.read())


@pytest.fixture(scope="module")
def stack():
    mapd_py = _python_with(["numpy"])
    if mapd_py is None:
        pytest.skip("no Python with numpy for roadsense mapd")
    api_py = _python_with(["numpy", "aiohttp", "PIL"])
    tmp = tempfile.mkdtemp(prefix="prius-feeder-e2e-")
    rundir = os.path.join(tmp, "run")
    os.makedirs(rundir)
    os.makedirs(os.path.join(tmp, "etc"))
    st = Stack(rundir, _free_port() if api_py else None)
    env = dict(os.environ, ROADSENSE_CONFIG_DIR=os.path.join(tmp, "etc"),
               PYTHONPATH=os.path.join(REPO, "mapd"))
    logf = open(os.path.join(tmp, "procs.log"), "wb")
    try:
        if api_py:
            st.procs.append(subprocess.Popen(
                [api_py, os.path.join(REPO, "tools", "fake_daemons.py"), "--rundir", rundir, "--no-mapd"],
                stdout=logf, stderr=subprocess.STDOUT))
        st.procs.append(subprocess.Popen(
            [mapd_py, "-m", "roadsense_mapd", "--rundir", rundir, "--pack", PACK, "--hz", "5",
             "--no-vision-odometry", "--stats", "0"], env=env, stdout=logf, stderr=subprocess.STDOUT))
        if api_py:
            st.procs.append(subprocess.Popen(
                [api_py, "-m", "roadsense_api", "--rundir", rundir, "--datadir", tmp,
                 "--ring", os.path.join(rundir, "roadsense-frames"), "--host", "127.0.0.1",
                 "--port", str(st.api_port)], cwd=os.path.join(REPO, "api"), stdout=logf,
                stderr=subprocess.STDOUT))
        assert _wait(lambda: st.latest_map() and st.latest_map().get("pack"), 30), \
            "mapd did not come up with the pack"
        if api_py:
            assert _wait(lambda: st.get("/v1/fused")["speed_limit"]["vision"] is not None, 30), \
                "roadsense api / fake daemons did not come up"
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
    cfg = PositionFeederConfig(enabled=True, source="headunit", rundir=stack.rundir)
    tap = CanMotionTap()
    feeder = PositionFeeder(cfg, HeadunitGnssSource(cfg), tap=tap)
    scenes = []
    sfeed = _scene_feed(stack, scenes) if stack.api_port else None
    with CanSim(tap):
        feeder.start()
        feeder.set_active(True)
        try:
            m = _wait(lambda: (lambda x: x if (x.get("input") or {}).get("gnss") and
                               (x["input"].get("motion") or {}).get("source") == "vehicle" else None)(
                stack.latest_map()), 10)
            assert m, "mapd never saw the feeder"
            assert m["input"]["gnss"]["source"] == "headunit" and m["input"]["gnss"]["fix"] == "none"
            assert m["input"]["motion"]["tag"] == "prius-can"
            assert m["state"] == "no_source" and m["available"] is False
            assert m["reason"] == "no_position" and m["position"] is None
            time.sleep(2.0)
            st = feeder.status()
            assert st["connected"] and st["none_sent"] >= 1 and st["motion_sent"] > 10
            assert st["errors"] == 0 and st["gnss"]["has_fix"] is False
            if sfeed is not None:
                fused = stack.get("/v1/fused")
                assert fused["map"]["available"] is False
                assert fused["speed_limit"]["source"] in ("sign", "sign_held", "vision_default", "unknown")
                sc = _wait(lambda: next((s for s in reversed(scenes) if "map" in s and "limit" in s), None), 10)
                assert sc, "no scene with the map key"
                assert sc["map"]["ok"] is False and sc["map"]["why"] in ("no_position", "no_mapd", "stale")
                assert "road" not in sc["map"] and "ahead" not in sc["map"]
                assert sc["map"]["in"]["gnss"] == "headunit" and sc["map"]["in"]["tag"] == "prius-can"
                if sc["limit"] and sc["map"].get("limit"):
                    assert sc["map"]["limit"]["kmh"] == sc["limit"]["kmh"]     # the vision value passes
        finally:
            feeder.stop()
            if sfeed is not None:
                sfeed.stop()


def test_synthetic_drive_matches_and_dead_reckons(stack):
    cfg = PositionFeederConfig(enabled=True, source="synthetic", rundir=stack.rundir,
                               synthetic_dropouts=((15.0, 20.0),), synthetic_start_m=0.0)
    src = SyntheticGnssSource(cfg)
    feeder = PositionFeeder(cfg, src, tap=CanMotionTap())
    scenes = []
    sfeed = _scene_feed(stack, scenes) if stack.api_port else None
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
        assert last["input"]["gnss"]["source"] == "synthetic"
        assert last["input"]["motion"]["source"] == "vehicle" and last["input"]["motion"]["tag"] == "synthetic"
        # the filtered position follows the synthetic truth (GNSS noise 2.5 m correlated + 0.5 m white)
        errs = [_dist_m((m["position"]["lat"], m["position"]["lon"]), tr) for t, m, tr in good if t > 8.0]
        assert errs and max(errs) < 25.0, errs
        assert all(m["match"]["matched"] for t, m, tr in good if t > 8.0)
        # fix lost 15..20 s since the source started: dead reckoning on the vehicle motion, still matched
        dr = [(t, m) for t, m, _ in samples if m["state"] == "dead_reckoning"]
        assert dr and all(14.0 < t < 23.0 for t, _ in dr), [(round(t, 1), m["state"]) for t, m, _ in samples]
        assert any(m["available"] for _, m in dr)
        assert samples[-1][1]["state"] == "good"
        st = feeder.status()
        assert st["synthetic"] and st["source"] == "synthetic" and st["errors"] == 0
        assert st["gnss_sent"] >= 20 and st["none_sent"] >= 1 and st["motion_sent"] > 200
        if sfeed is not None:
            sc = _wait(lambda: next((s for s in reversed(scenes) if (s.get("map") or {}).get("ok")), None), 5)
            assert sc, "no scene with the map layer available"
            assert sc["map"]["in"]["gnss"] == "synthetic" and sc["map"]["in"]["tag"] == "synthetic"
            assert sc["map"]["road"].get("cls") and sc["map"]["pos"]["src"] in ("gnss", "dead_reckoning")
            assert sc["map"]["limit"] is None or sc["map"]["limit"]["src"] in (
                "sign", "sign_held", "map", "map_implicit", "vision_default", "unknown")
    finally:
        feeder.stop()
        if sfeed is not None:
            sfeed.stop()
