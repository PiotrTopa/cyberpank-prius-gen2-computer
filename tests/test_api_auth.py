"""Network API authentication (api.auth, api.auth_cli; docs/AUTH.md).

Pure parts (trusted networks, next-path sanitiser, session store, pairing
codes) are tested directly; the HTTP/WebSocket behaviour end to end over real
sockets: the FastAPI app under uvicorn on an ephemeral port, urllib without
redirect following, cookies handled by hand, and a stdlib fake roadsense for
the /roadsense/ browser front. No extra HTTP client dependency.
"""

import io
import json
import os
import socket
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cyberpunk_computer.api import auth as auth_mod
from cyberpunk_computer.api import auth_cli
from cyberpunk_computer.api.auth import (
    COOKIE_NAME,
    AuthConfig,
    Authenticator,
    PairingCodes,
    SessionStore,
    parse_duration,
    parse_trusted,
    peer_trusted,
    safe_next,
)
from cyberpunk_computer.api.roadsense_proxy import rewrite_ui

REPO = Path(__file__).resolve().parent.parent
ROOTFS = REPO / "POCO" / "rootfs"
TOKEN = "t0ken-for-tests-only"


# ── pure helpers ────────────────────────────────────────────────────────────

def test_trusted_networks():
    nets = parse_trusted("127.0.0.0/8, ::1")
    assert peer_trusted("127.0.0.1", nets)
    assert peer_trusted("::1", nets)
    assert peer_trusted("::ffff:127.0.0.1", nets)          # v4-mapped
    assert not peer_trusted("10.200.0.20", nets)
    assert not peer_trusted("testclient", nets)             # not an address
    assert not peer_trusted(None, nets)
    assert not peer_trusted("127.0.0.1", parse_trusted(""))  # nothing trusted


def test_safe_next_only_local_paths():
    assert safe_next("/roadsense/") == "/roadsense/"
    assert safe_next("/#satellites") == "/#satellites"
    for bad in (None, "", "http://evil/", "//evil/", "/\\evil", "x", "/a\r\nSet-Cookie: x"):
        assert safe_next(bad) == "/"


def test_parse_duration():
    assert parse_duration("15m") == 900
    assert parse_duration("2h") == 7200
    assert parse_duration("30s") == 30
    assert parse_duration("90") == 90


# ── sessions ────────────────────────────────────────────────────────────────

def test_session_store_memory_create_revoke():
    s = SessionStore()
    a, b = s.create("login"), s.create("pair")
    assert a != b and s.valid(a) and s.valid(b)
    assert not s.valid("nope") and not s.valid(None) and not s.valid("x" * 500)
    assert s.revoke(a) and not s.valid(a) and s.valid(b)
    assert s.revoke_all() == 1 and not s.valid(b)


def test_session_store_persists_hashes_only(tmp_path):
    s = SessionStore(tmp_path)
    sid = s.create("login", "Mozilla/5.0 Android")
    path = tmp_path / "sessions.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    text = path.read_text()
    assert sid not in text                      # only the SHA-256 is stored
    assert auth_mod.hash_secret(sid) in text
    # "backend restart": a fresh store on the same directory accepts the cookie
    assert SessionStore(tmp_path).valid(sid)


def test_session_revoke_from_cli_reaches_running_store(tmp_path):
    running = SessionStore(tmp_path, reload_interval_s=0)
    sid = running.create("pair")
    assert running.valid(sid)
    assert SessionStore(tmp_path).revoke_all() == 1        # the CLI process
    assert not running.valid(sid)


def test_session_expiry_and_cap():
    now = [1000.0]
    s = SessionStore(ttl_s=60, clock=lambda: now[0])
    sid = s.create()
    now[0] += 61
    assert not s.valid(sid)
    many = [s.create() for _ in range(auth_mod.MAX_SESSIONS + 5)]
    assert len(s.list()) == auth_mod.MAX_SESSIONS
    assert s.valid(many[-1]) and not s.valid(many[0])


# ── pairing codes ───────────────────────────────────────────────────────────

def test_pairing_code_single_use_memory():
    p = PairingCodes()
    code = p.issue(60)
    assert p.peek(code) and p.peek(code)     # peek does not consume
    assert p.redeem(code)
    assert not p.redeem(code) and not p.peek(code)


def test_pairing_code_expiry(tmp_path):
    now = [5000.0]
    p = PairingCodes(tmp_path, clock=lambda: now[0])
    code = p.issue(60)
    now[0] += 61
    assert not p.peek(code)
    assert not p.redeem(code)
    assert not list((tmp_path / "pair").iterdir())     # claimed + removed


def test_pairing_code_cross_process_and_race(tmp_path):
    code = PairingCodes(tmp_path).issue(600)              # CLI side
    files = list((tmp_path / "pair").iterdir())
    assert len(files) == 1 and code not in files[0].name   # file named by hash
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    backend = PairingCodes(tmp_path)
    results = []
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        results.append(backend.redeem(code))
    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1


def test_pairing_rejects_bad_codes_and_ttl(tmp_path):
    p = PairingCodes(tmp_path)
    for bad in (None, "", "../../etc/passwd", "a" * 65, "a b"):
        assert not p.redeem(bad) and not p.peek(bad)
    with pytest.raises(ValueError):
        p.issue(0)
    with pytest.raises(ValueError):
        p.issue(auth_mod.PAIRING_TTL_MAX_S + 1)


# ── decision ────────────────────────────────────────────────────────────────

class _Conn:
    def __init__(self, host="10.200.0.20", headers=None, cookies=None, query=None, kind="http"):
        self.scope = {"client": (host, 1234), "type": kind}
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.query_params = query or {}


def test_authorize_order():
    a = Authenticator.from_config(AuthConfig(token=TOKEN, trusted="127.0.0.0/8"))
    sid = a.sessions.create()
    assert a.authorize(_Conn("127.0.0.1")) == "trusted"
    assert a.authorize(_Conn()) is None
    assert a.authorize(_Conn(headers={"authorization": "Bearer " + TOKEN})) == "bearer"
    assert a.authorize(_Conn(headers={"authorization": "Bearer wrong"})) is None
    assert a.authorize(_Conn(headers={"authorization": TOKEN})) is None
    assert a.authorize(_Conn(cookies={COOKIE_NAME: sid})) == "session"
    assert a.authorize(_Conn(cookies={COOKIE_NAME: TOKEN})) is None   # token is not a cookie
    # ?token= only on WebSockets (kept for older stream clients)
    assert a.authorize(_Conn(query={"token": TOKEN})) is None
    assert a.authorize(_Conn(query={"token": TOKEN}, kind="websocket")) == "bearer"


def test_auth_off_without_token():
    a = Authenticator.from_config(AuthConfig(token=None, state_dir="/nonexistent/never"))
    assert not a.enabled and a.authorize(_Conn()) == "open"


# ── live app ────────────────────────────────────────────────────────────────

INDEX_HTML = (b'<!doctype html><html><body><img src="/preview">'
              b'<script>fetch("/v1/health");fetch(`/v1/road`);new WebSocket("/ws")</script>'
              b'</body></html>')
FRAME = b"\xff\xd8fakejpeg\xff\xd9"


class _FakeRoadsense(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(INDEX_HTML)
            return
        if self.path == "/preview":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            for _ in range(2):
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + FRAME + b"\r\n")
            return
        if self.path.startswith("/v1/"):
            body = json.dumps({"type": self.path[4:]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Bridge:
    def attach_loop(self, loop):
        pass

    def latest_snapshot(self):
        return {"ts": 1.0, "state": {"vehicle": {"speed_kmh": 0}}}

    def register_client(self):
        import asyncio

        q = asyncio.Queue()
        q.put_nowait({"type": "state", "ts": 1.0, "state": {}})
        return q

    def unregister_client(self, client):
        pass


def _serve(app):
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", loop="asyncio"))
    server.install_signal_handlers = lambda: None
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return server, t, port


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http(port, path, method="GET", headers=None, form=None, cookie=None):
    """(status, headers, body) without following redirects."""
    hdrs = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        hdrs["Cookie"] = "%s=%s" % (COOKIE_NAME, cookie)
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data,
                                 method=method, headers=hdrs)
    try:
        with _OPENER.open(req, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _cookie_from(headers):
    raw = headers.get("set-cookie", "")
    first = raw.split(";", 1)[0]
    name, _, value = first.partition("=")
    assert name == COOKIE_NAME
    return value, raw.lower()


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    from cyberpunk_computer.api import app as app_mod
    from cyberpunk_computer.api import roadsense_proxy as rp

    state = tmp_path_factory.mktemp("auth")
    rs_port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", rs_port), _FakeRoadsense)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    app = app_mod.create_app(_Bridge(), db=object(), auth_token=TOKEN,
                             auth_state_dir=str(state),
                             roadsense_url="http://127.0.0.1:%d" % rs_port)
    server, t, port = _serve(app)
    yield {"port": port, "state": state, "app": app}
    server.should_exit = True
    t.join(5)
    httpd.shutdown()


def _login(port):
    st, hdrs, _ = _http(port, "/login", "POST", form={"token": TOKEN, "next": "/roadsense/"})
    assert st == 303 and hdrs["location"] == "/roadsense/"
    return _cookie_from(hdrs)


def test_health_public_api_401(live):
    p = live["port"]
    assert _http(p, "/health")[0] == 200
    st, hdrs, body = _http(p, "/api/v1/state")
    assert st == 401 and hdrs["www-authenticate"] == "Bearer"
    assert _http(p, "/api/v1/state", headers={"Authorization": "Bearer nope"})[0] == 401
    assert _http(p, "/api/v1/state", headers={"Authorization": "Bearer " + TOKEN})[0] == 200
    # commands and the GNSS intake are refused before routing (not wired here)
    assert _http(p, "/api/v1/commands/mfd_hold", "POST")[0] == 401
    assert _http(p, "/api/v1/position/gnss", "POST")[0] == 401


def test_page_redirects_to_login(live):
    p = live["port"]
    st, hdrs, _ = _http(p, "/roadsense/?x=1", headers={"Accept": "text/html,*/*"})
    assert st == 303 and hdrs["location"] == "/login?next=/roadsense/%3Fx%3D1"
    # non-browser clients get 401, not a redirect
    assert _http(p, "/roadsense/")[0] == 401
    st, _, body = _http(p, "/login?next=/roadsense/")
    assert st == 200 and b'type="password"' in body and b'value="/roadsense/"' in body


def test_login_sets_revocable_cookie(live):
    p = live["port"]
    st, _, body = _http(p, "/login", "POST", form={"token": "wrong", "next": "/"})
    assert st == 401 and b"Wrong token" in body
    sid, raw = _login(p)
    assert sid and sid != TOKEN and TOKEN not in raw
    assert "httponly" in raw and "samesite=lax" in raw and "secure" not in raw
    assert "max-age=%d" % auth_mod.SESSION_TTL_S in raw and "path=/" in raw
    assert _http(p, "/api/v1/state", cookie=sid)[0] == 200
    assert json.loads(_http(p, "/auth/status", cookie=sid)[2]) == {"auth": True, "authenticated": True}
    assert json.loads(_http(p, "/auth/status")[2]) == {"auth": True, "authenticated": False}
    # already logged in: /login bounces to the target
    assert _http(p, "/login?next=/x", cookie=sid)[1]["location"] == "/x"
    # logout revokes server-side (the old cookie value is dead, not just cleared)
    st, hdrs, _ = _http(p, "/logout", "POST", cookie=sid)
    assert st == 303 and hdrs["location"] == "/login"
    assert _http(p, "/api/v1/state", cookie=sid)[0] == 401


def test_login_next_cannot_leave_origin(live):
    st, hdrs, _ = _http(live["port"], "/login", "POST",
                        form={"token": TOKEN, "next": "//evil.example/"})
    assert st == 303 and hdrs["location"] == "/"


def test_session_survives_restart(live):
    sid, _ = _login(live["port"])
    again = Authenticator.from_config(AuthConfig(token=TOKEN, state_dir=str(live["state"])))
    assert again.sessions.valid(sid)


def test_websocket_auth(live):
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect

    url = "ws://127.0.0.1:%d/api/v1/stream" % live["port"]
    with pytest.raises(InvalidStatus) as e:
        with connect(url, open_timeout=5):
            pass
    assert e.value.response.status_code == 403
    sid, _ = _login(live["port"])
    with connect(url, open_timeout=5,
                 additional_headers={"Cookie": "%s=%s" % (COOKIE_NAME, sid)}) as ws:
        assert json.loads(ws.recv(timeout=5))["type"] == "state"
    with connect(url, open_timeout=5,
                 additional_headers={"Authorization": "Bearer " + TOKEN}) as ws:
        assert json.loads(ws.recv(timeout=5))["type"] == "state"
    with connect(url + "?token=" + TOKEN, open_timeout=5) as ws:
        assert json.loads(ws.recv(timeout=5))["type"] == "state"


def test_revoke_all_api_and_cli(live, monkeypatch):
    p = live["port"]
    a, _ = _login(p)
    b, _ = _login(p)
    assert _http(p, "/auth/revoke-all", "POST")[0] == 401
    st, _, body = _http(p, "/auth/revoke-all", "POST", headers={"Authorization": "Bearer " + TOKEN})
    assert st == 200 and json.loads(body)["revoked"] >= 2
    assert _http(p, "/api/v1/state", cookie=a)[0] == 401
    assert _http(p, "/api/v1/state", cookie=b)[0] == 401
    # the root CLI revokes on disk; the running backend notices within ~1 s
    c, _ = _login(p)
    monkeypatch.setenv("BACKEND_AUTH_STATE_DIR", str(live["state"]))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with redirect_stdout(io.StringIO()) as out:
        assert auth_cli.main(["revoke-all"]) == 0
    assert "revoked 1" in out.getvalue()
    time.sleep(1.1)
    assert _http(p, "/api/v1/state", cookie=c)[0] == 401


def test_pairing_link_flow(live, monkeypatch):
    p = live["port"]
    monkeypatch.setenv("BACKEND_AUTH_STATE_DIR", str(live["state"]))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with redirect_stdout(io.StringIO()) as out:
        assert auth_cli.main(["link", "--host", "127.0.0.1", "--port", str(p), "--ttl", "5m"]) == 0
    url = out.getvalue().strip()
    assert url.startswith("http://127.0.0.1:%d/auth/pair?code=" % p)
    code = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["code"][0]
    assert TOKEN not in url
    # GET shows a confirm page and does NOT consume the code (link previews)
    for _ in range(2):
        st, _, body = _http(p, "/auth/pair?code=" + code)
        assert st == 200 and b"Pair this browser" in body
    st, hdrs, _ = _http(p, "/auth/pair", "POST", form={"code": code})
    assert st == 303 and hdrs["location"] == "/"
    sid, raw = _cookie_from(hdrs)
    assert "httponly" in raw and code not in sid
    assert _http(p, "/api/v1/state", cookie=sid)[0] == 200
    # single use
    st, hdrs, body = _http(p, "/auth/pair", "POST", form={"code": code})
    assert st == 403 and "set-cookie" not in hdrs and b"expired or was already used" in body
    assert _http(p, "/auth/pair?code=" + code)[0] == 403


def test_pairing_code_expired_over_http(live):
    past = PairingCodes(live["state"], clock=lambda: time.time() - 3600).issue(60)
    assert _http(live["port"], "/auth/pair?code=" + past)[0] == 403
    assert _http(live["port"], "/auth/pair", "POST", form={"code": past})[0] == 403


def test_login_throttle(live):
    auth = live["app"].state.auth
    auth._fails[:] = [time.monotonic()] * 10
    try:
        st, _, body = _http(live["port"], "/login", "POST", form={"token": TOKEN})
        assert st == 429                       # even the right token waits
    finally:
        auth._fails.clear()


def test_install_assets_public(live):
    # Chrome fetches the manifest and icons without cookies.
    for path in ("/manifest.webmanifest", "/icons/icon-192.png"):
        assert _http(live["port"], path)[0] in (200, 404)    # 404 if dashboard/dist absent


# ── roadsense browser front behind the same login ───────────────────────────

def test_rewrite_ui():
    out = rewrite_ui(INDEX_HTML.decode(), "/roadsense")
    assert 'src="/roadsense/preview"' in out
    assert 'fetch("/roadsense/v1/health")' in out and "fetch(`/roadsense/v1/road`)" in out
    assert 'WebSocket("/roadsense/ws")' in out
    assert rewrite_ui('href="/preview2"', "/r") == 'href="/preview2"'


def test_roadsense_ui_proxy(live):
    p = live["port"]
    assert _http(p, "/roadsense/v1/health")[0] == 401
    assert _http(p, "/roadsense/preview")[0] == 401
    sid, _ = _login(p)
    st, hdrs, _ = _http(p, "/roadsense", cookie=sid)
    assert st == 307 and hdrs["location"] == "/roadsense/"
    st, hdrs, body = _http(p, "/roadsense/", cookie=sid)
    assert st == 200 and hdrs["content-type"].startswith("text/html")
    assert b'src="/roadsense/preview"' in body and b'"/roadsense/v1/health"' in body
    st, _, body = _http(p, "/roadsense/v1/health", cookie=sid)
    assert st == 200 and json.loads(body) == {"type": "health"}
    assert json.loads(_http(p, "/roadsense/v1/map", cookie=sid)[2]) == {"type": "map"}
    assert _http(p, "/roadsense/v1/../../etc/passwd", cookie=sid)[0] == 404
    assert _http(p, "/roadsense/v1/position", cookie=sid)[0] == 404    # no POST passthrough
    st, hdrs, body = _http(p, "/roadsense/preview", cookie=sid)
    assert st == 200 and hdrs["content-type"].startswith("multipart/x-mixed-replace")
    assert body.count(FRAME) == 2


def test_roadsense_ws_needs_session(live):
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect

    url = "ws://127.0.0.1:%d/roadsense/ws" % live["port"]
    with pytest.raises(InvalidStatus):
        with connect(url, open_timeout=5):
            pass
    sid, _ = _login(live["port"])
    # authenticated: the handshake succeeds; the fake upstream has no /ws, so
    # the relay closes with 1013 "roadsense unavailable".
    with connect(url, open_timeout=5,
                 additional_headers={"Cookie": "%s=%s" % (COOKIE_NAME, sid)}) as ws:
        with pytest.raises(Exception):
            ws.recv(timeout=5)
        assert ws.close_code == 1013


def test_roadsense_ui_when_stack_down(tmp_path):
    from cyberpunk_computer.api import app as app_mod

    app = app_mod.create_app(_Bridge(), db=object(), auth_token=TOKEN,
                             roadsense_url="http://127.0.0.1:%d" % _free_port())
    server, t, port = _serve(app)
    try:
        st, _, body = _http(port, "/roadsense/", headers={"Authorization": "Bearer " + TOKEN})
        assert st == 503 and b"not running" in body
    finally:
        server.should_exit = True
        t.join(5)


# ── trusted peers, auth off, server config ──────────────────────────────────

def test_trusted_loopback_and_auth_off():
    from cyberpunk_computer.api import app as app_mod

    app = app_mod.create_app(_Bridge(), db=object(), auth_token=TOKEN, auth_trusted="127.0.0.0/8")
    server, t, port = _serve(app)
    try:
        assert _http(port, "/api/v1/state")[0] == 200
    finally:
        server.should_exit = True
        t.join(5)
    app = app_mod.create_app(_Bridge(), db=object())
    server, t, port = _serve(app)
    try:
        assert _http(port, "/api/v1/state")[0] == 200
        assert _http(port, "/login")[0] == 303     # nothing to log in to
        assert _http(port, "/login", "POST", form={"token": "x"})[0] == 303
    finally:
        server.should_exit = True
        t.join(5)


def test_api_server_ignores_forwarded_headers():
    from cyberpunk_computer.api.server import ApiServer

    srv = ApiServer(_Bridge(), db=object(), host="127.0.0.1", port=0, auth_token=TOKEN,
                    auth_trusted="127.0.0.0/8")
    assert srv._server.config.proxy_headers is False


def test_backend_env_token_popped(monkeypatch):
    from cyberpunk_computer.backend import __main__ as m

    monkeypatch.setenv("BACKEND_AUTH_TOKEN", " abc ")
    assert m._pop_env_token() == "abc"
    assert "BACKEND_AUTH_TOKEN" not in os.environ


def test_cli_hosts_mirror_firewall():
    ip_out = (
        "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
        "4: wlan0    inet 10.42.0.1/24 brd 10.42.0.255 scope global wlan0\\ x\n"
        "6: wg-homelab    inet 10.200.0.14/32 scope global wg-homelab\\ x\n"
        "7: qmapmux0.0    inet 10.163.9.66/30 scope global qmapmux0.0\\ x\n"
        "8: usb0    inet 192.168.100.1/24 scope global usb0\\ x\n")
    assert auth_cli.local_hosts(ip_out) == [
        ("wg-homelab", "10.200.0.14"), ("wlan0", "10.42.0.1"), ("usb0", "192.168.100.1")]
    # Wi-Fi client mode (home network address on wlan0) is not offered
    assert auth_cli.local_hosts("4: wlan0    inet 192.168.0.86/24 scope global wlan0\n") == []


def test_cli_token_new(tmp_path, monkeypatch):
    env = tmp_path / "backend-auth.env"
    monkeypatch.setenv("BACKEND_AUTH_ENV", str(env))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with redirect_stdout(io.StringIO()) as out:
        assert auth_cli.main(["token", "new"]) == 0
    assert out.getvalue() == ""                      # printed only with --stdout
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    tok = env.read_text().split("BACKEND_AUTH_TOKEN=")[1].strip()
    assert len(tok) >= 43 and auth_cli.token_configured()
    with redirect_stdout(io.StringIO()) as out:
        auth_cli.main(["token", "new", "--stdout"])
    assert out.getvalue().strip() == env.read_text().split("BACKEND_AUTH_TOKEN=")[1].strip() != tok


def test_cli_needs_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit):
        auth_cli.main(["link", "--host", "x"])


# ── provisioning ────────────────────────────────────────────────────────────

def test_firewall_guard_limits_ports():
    text = (ROOTFS / "etc" / "nftables.d" / "50_prius_backend.nft").read_text()
    rules = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
    assert "table inet prius_guard" in rules and "hook input priority -10" in rules
    assert "tcp dport { 8080, 8081, 8082, 8090 } jump prius_ports" in rules
    for allowed in ('iifname "lo" return', 'iifname "wg*" return', 'iifname "usb*" return',
                    'iifname "wlan*" ip saddr 10.42.0.0/24 ip daddr 10.42.0.1 tcp dport { 8080, 8090 } return'):
        assert allowed in rules
    assert 'counter drop comment "prius ports: interface not allowed"' in rules
    assert "ip saddr 192.168.100.0/24 drop" in rules     # gadget subnet only on usb*


def test_launcher_keeps_token_off_argv():
    text = (ROOTFS / "usr" / "local" / "sbin" / "prius-backend").read_text()
    assert "--auth-token" not in text
    assert "/etc/prius/backend-auth.env" in text
    cli = (ROOTFS / "usr" / "local" / "sbin" / "prius-auth").read_text()
    assert "cyberpunk_computer.api.auth_cli" in cli
