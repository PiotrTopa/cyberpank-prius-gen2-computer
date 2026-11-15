"""roadsense front (api.roadsense_proxy): the backend re-publishes the
loopback-only roadsense API under /api/v1/roadsense, plus the single-computer
resource layout (prius.slice on a core that survives the low power profile).

The proxy is exercised end to end over real sockets: a stdlib fake roadsense
(HTTP + a websockets server) and the real FastAPI app under uvicorn on an
ephemeral port. No extra HTTP client dependency is needed (urllib only).
"""

import asyncio
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cyberpunk_computer.api.roadsense_proxy import (
    JSON_ENDPOINTS,
    ProxyConfig,
    build_query,
    ws_topics,
)

REPO = Path(__file__).resolve().parent.parent
ROOTFS = REPO / "POCO" / "rootfs"


# ── pure helpers ────────────────────────────────────────────────────────────

def test_build_query_whitelists_params():
    assert build_query("signs/history", {"limit": "5", "evil": "x"}) == "?limit=5"
    assert build_query("health", {"limit": "5"}) == ""
    assert build_query("signs/history", {}) == ""


def test_ws_topics_sanitised():
    assert ws_topics("signs,rules,bogus,signs") == "signs,rules"
    assert ws_topics("bogus") is None
    assert ws_topics(None) is None


def test_proxy_config_urls():
    c = ProxyConfig(base_url="http://127.0.0.1:8090")
    assert c.host_port == ("127.0.0.1", 8090)
    assert c.ws_url == "ws://127.0.0.1:8090/ws"


# ── fake roadsense ──────────────────────────────────────────────────────────

FRAME = b"\xff\xd8fakejpeg\xff\xd9"


class _Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/signs/history"):
            return self._json({"path": self.path})
        if self.path == "/v1/health":
            return self._json({"fps": {"camera": 19.5}, "daemons": {"camerad": True}})
        if self.path.startswith("/v1/"):
            return self._json({"type": self.path[4:]})
        if self.path == "/v1/frame.jpg" or self.path == "/frame.jpg":
            pass
        if self.path == "/preview":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            for _ in range(3):
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + FRAME + b"\r\n")
                self.wfile.flush()
                time.sleep(0.05)
            return
        self.send_response(404)
        self.end_headers()


class _FakeFrame(_Fake):
    def do_GET(self):
        if self.path == "/v1/frame.jpg":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(FRAME)
            return
        return super().do_GET()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _WsServer:
    """websockets server on its own loop/thread: sends two messages per client."""

    def __init__(self, port):
        self.port = port
        self.paths = []
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        assert self.ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        from websockets.asyncio.server import serve

        async def handler(ws):
            self.paths.append(ws.request.path)
            await ws.send(json.dumps({"type": "signs", "n": 1}))
            await ws.send(json.dumps({"type": "rules", "n": 2}))
            try:
                await ws.wait_closed()
            except Exception:
                pass

        async def main():
            self.server = await serve(handler, "127.0.0.1", self.port)
            self.ready.set()
            await self.server.wait_closed()

        try:
            self.loop.run_until_complete(main())
        except Exception:
            pass

    def stop(self):
        self.loop.call_soon_threadsafe(self.server.close)


@pytest.fixture(scope="module")
def stack():
    """Fake roadsense (HTTP on one port; WS server on another port wired via
    a ProxyConfig override) + the backend app under uvicorn."""
    import uvicorn
    from cyberpunk_computer.api import app as app_mod
    from cyberpunk_computer.api import roadsense_proxy as rp

    rs_port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", rs_port), _FakeFrame)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    ws_port = _free_port()
    wss = _WsServer(ws_port)

    # Point the WS relay at the websockets server (same host, other port).
    orig_ws_url = rp.ProxyConfig.ws_url
    rp.ProxyConfig.ws_url = property(lambda self: "ws://127.0.0.1:%d/ws" % ws_port)

    class Bridge:
        def attach_loop(self, loop):
            pass

        def latest_snapshot(self):
            return {"state": {}}

    app = app_mod.create_app(Bridge(), db=object(), roadsense_url="http://127.0.0.1:%d" % rs_port)
    api_port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port,
                                           log_level="warning", loop="asyncio"))
    server.install_signal_handlers = lambda: None
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield {"api": "http://127.0.0.1:%d" % api_port, "api_port": api_port, "wss": wss,
           "rs_port": rs_port}
    server.should_exit = True
    t.join(5)
    httpd.shutdown()
    wss.stop()
    rp.ProxyConfig.ws_url = orig_ws_url


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers.get("content-type"), r.read()


def test_index_lists_endpoints(stack):
    st, ct, body = _get(stack["api"] + "/api/v1/roadsense")
    d = json.loads(body)
    assert st == 200
    assert "/api/v1/roadsense/health" in d["endpoints"]
    assert "/api/v1/roadsense/preview" in d["endpoints"]


@pytest.mark.parametrize("name", sorted(JSON_ENDPOINTS))
def test_json_endpoints_forwarded(stack, name):
    st, ct, body = _get(stack["api"] + "/api/v1/roadsense/" + name)
    assert st == 200 and ct.startswith("application/json")
    json.loads(body)


def test_health_body_passthrough(stack):
    _, _, body = _get(stack["api"] + "/api/v1/roadsense/health")
    assert json.loads(body)["fps"]["camera"] == 19.5


def test_query_whitelist_applied(stack):
    _, _, body = _get(stack["api"] + "/api/v1/roadsense/signs/history?limit=7&x=1")
    assert json.loads(body)["path"] == "/v1/signs/history?limit=7"


def test_unknown_endpoint_404(stack):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(stack["api"] + "/api/v1/roadsense/../../etc/passwd")
    assert e.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(stack["api"] + "/api/v1/roadsense/preview2")
    assert e.value.code == 404


def test_frame_jpeg(stack):
    st, ct, body = _get(stack["api"] + "/api/v1/roadsense/frame.jpg")
    assert st == 200 and ct == "image/jpeg" and body == FRAME


def test_preview_mjpeg_stream(stack):
    st, ct, body = _get(stack["api"] + "/api/v1/roadsense/preview")
    assert st == 200 and ct.startswith("multipart/x-mixed-replace")
    assert body.count(FRAME) == 3


def test_ws_relay_with_topics(stack):
    from websockets.sync.client import connect

    url = "ws://127.0.0.1:%d/api/v1/roadsense/ws?topics=signs,bogus" % stack["api_port"]
    with connect(url, open_timeout=5) as ws:
        a = json.loads(ws.recv(timeout=5))
        b = json.loads(ws.recv(timeout=5))
    assert (a["type"], b["type"]) == ("signs", "rules")
    assert stack["wss"].paths[-1] == "/ws?topics=signs"


def test_upstream_down_is_503():
    """roadsense stopped (key-off): the front answers 503, not 500."""
    import uvicorn
    from cyberpunk_computer.api import app as app_mod

    class Bridge:
        def attach_loop(self, loop):
            pass

        def latest_snapshot(self):
            return None

    dead = _free_port()
    app = app_mod.create_app(Bridge(), db=object(), roadsense_url="http://127.0.0.1:%d" % dead)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    server.install_signal_handlers = lambda: None
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        for path in ("health", "frame.jpg", "preview"):
            with pytest.raises(urllib.error.HTTPError) as e:
                _get("http://127.0.0.1:%d/api/v1/roadsense/%s" % (port, path))
            assert e.value.code == 503
    finally:
        server.should_exit = True
        t.join(5)


def test_front_absent_without_roadsense():
    from cyberpunk_computer.api import app as app_mod

    class Bridge:
        def attach_loop(self, loop):
            pass

        def latest_snapshot(self):
            return None

    app = app_mod.create_app(Bridge(), db=object())
    assert not any(getattr(r, "path", "").startswith("/api/v1/roadsense") for r in app.routes)


def test_auth_token_required(stack):
    from cyberpunk_computer.api import app as app_mod

    class Bridge:
        def attach_loop(self, loop):
            pass

        def latest_snapshot(self):
            return None

    app = app_mod.create_app(Bridge(), db=object(), auth_token="s3cret",
                             roadsense_url="http://127.0.0.1:%d" % stack["rs_port"])
    import uvicorn
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    server.install_signal_handlers = lambda: None
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _get("http://127.0.0.1:%d/api/v1/roadsense/health" % port)
        assert e.value.code == 401
        req = urllib.request.Request("http://127.0.0.1:%d/api/v1/roadsense/health" % port,
                                     headers={"Authorization": "Bearer s3cret"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
    finally:
        server.should_exit = True
        t.join(5)


# ── env wiring ──────────────────────────────────────────────────────────────

def test_port_power_and_probe_env(monkeypatch):
    from cyberpunk_computer.backend import __main__ as m

    for k in ("BACKEND_HUB", "BACKEND_PP_POWERBOX_PORT", "BACKEND_PP_MFD_PORT",
              "BACKEND_PP_GATEWAY_PORT", "BACKEND_PP_CAMERA_PORT"):
        monkeypatch.delenv(k, raising=False)
    assert m._build_port_power() is None
    monkeypatch.setenv("BACKEND_HUB", "1-1")
    monkeypatch.setenv("BACKEND_PP_POWERBOX_PORT", "1")
    monkeypatch.setenv("BACKEND_PP_MFD_PORT", "2")
    monkeypatch.setenv("BACKEND_PP_GATEWAY_PORT", "3")
    monkeypatch.setenv("BACKEND_PP_CAMERA_PORT", "4")
    pp = m._build_port_power()
    assert (pp.hub, pp.powerbox_port, pp.mfd_data_port, pp.gateway_data_port,
            pp.camera_data_port) == ("1-1", 1, 2, 3, 4)
    monkeypatch.setenv("BACKEND_USB_PROBE_FALLBACK", "1")
    assert m._env_flag("BACKEND_USB_PROBE_FALLBACK")
    monkeypatch.setenv("BACKEND_USB_PROBE_FALLBACK", "0")
    assert not m._env_flag("BACKEND_USB_PROBE_FALLBACK")


# ── resource layout (POCO overlay) ──────────────────────────────────────────

def _unit(path):
    out = {}
    for line in (ROOTFS / path).read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#") and not line.startswith("["):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _cpus(spec):
    cpus = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            cpus.update(range(int(a), int(b) + 1))
        else:
            cpus.add(int(part))
    return cpus


def _mib(v):
    m = re.fullmatch(r"(\d+)([KMG])", v)
    n, u = int(m.group(1)), m.group(2)
    return n / 1024 if u == "K" else n * 1024 if u == "G" else n


def test_backend_memory_cap_and_slice():
    u = _unit("etc/systemd/system/prius-backend.service")
    assert u["Slice"] == "prius.slice"
    assert _mib(u["MemoryMax"]) == 512
    assert _mib(u["MemoryLow"]) < _mib(u["MemoryHigh"]) < _mib(u["MemoryMax"])


def test_backend_core_survives_low_profile_and_is_exclusive():
    backend = _cpus(_unit("etc/systemd/system/prius.slice")["AllowedCPUs"])
    script = (ROOTFS / "usr/local/sbin/prius-power").read_text()
    big = {int(c) for c in re.search(r'BIG_CORES="([\d ]+)"', script).group(1).split()}
    assert backend and not (backend & big), "backend core must stay online in `low`"
    for other in ("roadsense", "system", "user"):
        allowed = _cpus(_unit("etc/systemd/system/%s.slice.d/50-prius-cpus.conf" % other)["AllowedCPUs"])
        assert not (allowed & backend), other + ".slice must stay off the backend core"
        assert allowed - big, other + ".slice needs a core that stays online in `low`"


def test_helpers_in_prius_slice():
    for svc in ("prius-power", "prius-netwatch", "prius-clock-fallback", "prius-wifi", "prius-lifeline",
                "prius-sim-unlock"):
        assert _unit("etc/systemd/system/%s.service" % svc).get("Slice") == "prius.slice", svc


def test_firewall_has_no_roadsense_opening():
    # The Prius file never ACCEPTs 8090 (roadsense is reached through the
    # backend front); it only confines it in the prius_guard table.
    nft = (ROOTFS / "etc/nftables.d/50_prius_backend.nft").read_text()
    rules = [l for l in nft.splitlines() if "dport" in l and " accept" in l
             and not l.strip().startswith("#")]
    assert rules and all("8090" not in r for r in rules)
    assert all(p in rules[0] for p in ("8080", "8081", "8082"))
