"""
Network API authentication: bearer token, trusted networks, browser sessions
and one-time pairing codes (docs/AUTH.md).

Who gets in (first match wins; nothing is checked when no token is set):

    trusted network   the TCP peer address is in ``trusted`` (the backend
                      default is loopback only: processes on the phone itself)
    bearer token      ``Authorization: Bearer <BACKEND_AUTH_TOKEN>`` (tools,
                      head-unit GNSS feeder, laptop scripts); WebSockets also
                      accept ``?token=`` (older clients)
    session cookie    ``prius_session=<id>``: a random session id issued by
                      ``POST /login`` (enter the token once) or by redeeming a
                      pairing code (``prius-auth link`` on the phone). Only the
                      SHA-256 of the id is stored (``sessions.json``, root 0600),
                      so the server can revoke it and a leaked state file does
                      not give a usable cookie. Sessions survive restarts.

Everything else gets 401 (API, non-GET, non-HTML clients, WebSocket handshake
refused) or, for a browser page, a redirect to the login page.

Pairing codes are single use and short lived: the CLI writes
``pair/<sha256(code)>`` (expiry inside) into the state directory and the backend
claims it with an atomic rename, so a code works exactly once even with two
racing requests. The link carries the code, never the token.

Cookies are ``HttpOnly; SameSite=Lax`` and deliberately NOT ``Secure``: the
API is plain HTTP, reachable only over WireGuard, the car's own Wi-Fi AP, the
USB link and loopback (the firewall in POCO/rootfs/etc/nftables.d).
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union
from urllib.parse import parse_qs, quote

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.requests import HTTPConnection

logger = logging.getLogger(__name__)

COOKIE_NAME = "prius_session"
SESSION_TTL_S = 365 * 86400          # browsers cap Max-Age at ~400 days
MAX_SESSIONS = 64
PAIRING_TTL_DEFAULT_S = 15 * 60
PAIRING_TTL_MAX_S = 24 * 3600
DEFAULT_STATE_DIR = "/var/lib/prius/auth"
DEFAULT_TRUSTED = "127.0.0.0/8,::1"

# Reachable without credentials: liveness, the login/pairing flow itself and
# what Chrome fetches without cookies to install the dashboard (manifest/icons).
PUBLIC_PATHS = frozenset({
    "/health", "/login", "/logout", "/auth/pair", "/auth/status",
    "/manifest.webmanifest", "/favicon.svg",
})
PUBLIC_PREFIXES = ("/icons/",)

Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_trusted(spec: Optional[str]) -> List[Network]:
    """``"127.0.0.0/8, ::1"`` -> networks. Empty/None -> nothing trusted."""
    nets: List[Network] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def peer_trusted(host: Optional[str], trusted: Sequence[Network]) -> bool:
    if not host or not trusted:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip.version == n.version and ip in n for n in trusted)


def safe_next(target: Optional[str]) -> str:
    """Only same-origin absolute paths are allowed as a post-login target."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    if any(c in target for c in "\r\n"):
        return "/"
    return target


def parse_duration(text: str) -> float:
    """``"15m"`` / ``"900"`` / ``"2h"`` / ``"30s"`` -> seconds."""
    text = text.strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text and text[-1] in mult:
        return float(text[:-1]) * mult[text[-1]]
    return float(text)


# ── persistence helpers ─────────────────────────────────────────────────────

def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except PermissionError:
        pass


def _write_private(path: Path, data: bytes) -> None:
    """Atomic write of a 0600 file (tmp + rename in the same directory)."""
    tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


class _FileLock:
    """flock on ``<dir>/.lock``: the backend and the prius-auth CLI share it."""

    def __init__(self, directory: Path) -> None:
        self._path = directory / ".lock"
        self._fd: Optional[int] = None

    def __enter__(self):
        self._fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


# ── sessions ────────────────────────────────────────────────────────────────

class SessionStore:
    """Revocable browser sessions, persisted as hashes in ``sessions.json``.

    ``directory=None`` keeps them in memory only (tests, dev runs). With a
    directory the file is re-read when it changes on disk, so a revoke done by
    the CLI applies to the running backend within a second.
    """

    FILE = "sessions.json"

    def __init__(self, directory: Optional[Union[str, Path]] = None,
                 ttl_s: float = SESSION_TTL_S, clock=time.time,
                 reload_interval_s: float = 1.0) -> None:
        self._dir = Path(directory) if directory else None
        self._reload_interval = reload_interval_s
        self._ttl = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: Dict[str, Dict[str, object]] = {}
        self._stamp = None
        self._checked = 0.0
        if self._dir is not None:
            _ensure_dir(self._dir)
            self._reload(force=True)

    @property
    def path(self) -> Optional[Path]:
        return self._dir / self.FILE if self._dir else None

    # file I/O (callers hold self._lock) ──────────────────────────────────────

    def _file_stamp(self):
        try:
            st = os.stat(str(self.path))
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            return None

    def _read_file(self) -> Dict[str, Dict[str, object]]:
        try:
            data = json.loads(Path(self.path).read_text())
            sessions = data.get("sessions", {})
            return {k: v for k, v in sessions.items() if isinstance(v, dict)}
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as exc:
            logger.error("auth: unreadable %s (%s); no sessions loaded", self.path, exc)
            return {}

    def _reload(self, force: bool = False) -> None:
        if self._dir is None:
            return
        now = time.monotonic()
        if not force and now - self._checked < self._reload_interval:
            return
        self._checked = now
        stamp = self._file_stamp()
        if force or stamp != self._stamp:
            self._sessions = self._read_file()
            self._stamp = stamp

    def _mutate(self, fn):
        """Read-modify-write under the shared file lock; returns fn's result."""
        with self._lock:
            if self._dir is None:
                result = fn(self._sessions)
                self._prune(self._sessions)
                return result
            with _FileLock(self._dir):
                sessions = self._read_file()
                result = fn(sessions)
                self._prune(sessions)
                _write_private(self.path, json.dumps(
                    {"version": 1, "sessions": sessions}, indent=1, sort_keys=True).encode())
                self._sessions = sessions
                self._stamp = self._file_stamp()
                self._checked = time.monotonic()
            return result

    def _prune(self, sessions: Dict[str, Dict[str, object]]) -> None:
        now = self._clock()
        for k in [k for k, v in sessions.items() if float(v.get("expires", 0)) <= now]:
            del sessions[k]
        if len(sessions) > MAX_SESSIONS:
            oldest = sorted(sessions, key=lambda k: float(sessions[k].get("created", 0)))
            for k in oldest[:len(sessions) - MAX_SESSIONS]:
                del sessions[k]

    # API ─────────────────────────────────────────────────────────────────────

    def create(self, label: str = "", agent: str = "") -> str:
        sid = secrets.token_urlsafe(32)
        now = self._clock()
        entry = {"created": now, "expires": now + self._ttl,
                 "label": label[:32], "agent": agent[:96]}

        def add(sessions):
            sessions[hash_secret(sid)] = entry
        self._mutate(add)
        return sid

    def valid(self, sid: Optional[str]) -> bool:
        if not sid or len(sid) > 128:
            return False
        key = hash_secret(sid)
        with self._lock:
            self._reload()
            entry = self._sessions.get(key)
        return entry is not None and float(entry.get("expires", 0)) > self._clock()

    def revoke(self, sid: str) -> bool:
        key = hash_secret(sid)
        return bool(self._mutate(lambda s: s.pop(key, None) is not None))

    def revoke_prefix(self, prefix: str) -> int:
        def drop(sessions):
            keys = [k for k in sessions if k.startswith(prefix)]
            for k in keys:
                del sessions[k]
            return len(keys)
        return int(self._mutate(drop)) if prefix else 0

    def revoke_all(self) -> int:
        def drop(sessions):
            n = len(sessions)
            sessions.clear()
            return n
        return int(self._mutate(drop))

    def list(self) -> List[Dict[str, object]]:
        with self._lock:
            self._reload(force=True)
            now = self._clock()
            return [dict(v, id=k[:12]) for k, v in sorted(
                self._sessions.items(), key=lambda kv: float(kv[1].get("created", 0)))
                if float(v.get("expires", 0)) > now]


# ── pairing codes ───────────────────────────────────────────────────────────

class PairingCodes:
    """Single-use, short-lived codes that turn into a session when redeemed.

    On disk: ``<dir>/pair/<sha256(code)>`` holding ``{"expires": ts}``. Redeem
    claims the file with ``os.rename`` (atomic: exactly one claimant wins),
    then checks the expiry and deletes it. ``directory=None``: memory only.
    """

    def __init__(self, directory: Optional[Union[str, Path]] = None, clock=time.time) -> None:
        self._dir = Path(directory) / "pair" if directory else None
        self._clock = clock
        self._mem: Dict[str, float] = {}
        self._lock = threading.Lock()
        if self._dir is not None:
            _ensure_dir(self._dir.parent)
            _ensure_dir(self._dir)

    def issue(self, ttl_s: float = PAIRING_TTL_DEFAULT_S) -> str:
        if not 0 < ttl_s <= PAIRING_TTL_MAX_S:
            raise ValueError("ttl must be in (0, %d] s" % PAIRING_TTL_MAX_S)
        code = secrets.token_urlsafe(16)
        expires = self._clock() + ttl_s
        self.sweep()
        if self._dir is None:
            with self._lock:
                self._mem[hash_secret(code)] = expires
        else:
            _write_private(self._dir / hash_secret(code),
                           json.dumps({"expires": expires}).encode())
        return code

    def _valid_shape(self, code: Optional[str]) -> bool:
        return bool(code) and len(code) <= 64 and all(
            c.isalnum() or c in "-_" for c in code)

    def peek(self, code: Optional[str]) -> bool:
        """True if ``code`` would redeem now (does not consume it)."""
        if not self._valid_shape(code):
            return False
        key = hash_secret(code)
        if self._dir is None:
            with self._lock:
                return self._mem.get(key, 0) > self._clock()
        try:
            data = json.loads((self._dir / key).read_text())
            return float(data.get("expires", 0)) > self._clock()
        except (OSError, ValueError):
            return False

    def redeem(self, code: Optional[str]) -> bool:
        if not self._valid_shape(code):
            return False
        key = hash_secret(code)
        now = self._clock()
        if self._dir is None:
            with self._lock:
                expires = self._mem.pop(key, 0)
            return expires > now
        src = self._dir / key
        claimed = self._dir / (".claimed-%s-%s" % (key, secrets.token_hex(4)))
        try:
            os.rename(str(src), str(claimed))
        except FileNotFoundError:
            return False
        try:
            expires = float(json.loads(claimed.read_text()).get("expires", 0))
        except (OSError, ValueError):
            expires = 0.0
        finally:
            try:
                claimed.unlink()
            except OSError:
                pass
        return expires > now

    def sweep(self) -> int:
        now = self._clock()
        n = 0
        if self._dir is None:
            with self._lock:
                for k in [k for k, e in self._mem.items() if e <= now]:
                    del self._mem[k]
                    n += 1
            return n
        for p in self._dir.iterdir():
            try:
                if p.name.startswith("."):
                    if p.stat().st_mtime < now - 3600:
                        p.unlink()
                    continue
                if float(json.loads(p.read_text()).get("expires", 0)) <= now:
                    p.unlink()
                    n += 1
            except (OSError, ValueError):
                continue
        return n


# ── the decision ────────────────────────────────────────────────────────────

@dataclass
class AuthConfig:
    token: Optional[str] = None
    trusted: str = ""                       # create_app default: nobody trusted
    state_dir: Optional[str] = None         # None: sessions/codes in memory only


@dataclass
class Authenticator:
    token: Optional[str]
    trusted: List[Network] = field(default_factory=list)
    sessions: SessionStore = field(default_factory=SessionStore)
    pairing: PairingCodes = field(default_factory=PairingCodes)
    _fails: List[float] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg: AuthConfig) -> "Authenticator":
        state = cfg.state_dir if cfg.token else None
        sessions = pairing = None
        if state:
            try:
                sessions = SessionStore(state)
                pairing = PairingCodes(state)
            except OSError as exc:
                logger.error("auth: state dir %s unusable (%s); sessions are memory-only "
                             "and pairing links from prius-auth will not work", state, exc)
        return cls(token=cfg.token or None,
                   trusted=parse_trusted(cfg.trusted),
                   sessions=sessions or SessionStore(),
                   pairing=pairing or PairingCodes())

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    @staticmethod
    def is_public(path: str) -> bool:
        return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)

    def token_ok(self, presented: Optional[str]) -> bool:
        if not self.token or not presented:
            return False
        return hmac.compare_digest(presented.strip().encode(), self.token.encode())

    def bearer_ok(self, header: str) -> bool:
        parts = (header or "").split(" ", 1)
        return len(parts) == 2 and parts[0].lower() == "bearer" and self.token_ok(parts[1])

    def authorize(self, conn) -> Optional[str]:
        """How ``conn`` (a Starlette HTTPConnection) is allowed in, or None."""
        if not self.enabled:
            return "open"
        client = conn.scope.get("client")
        if client and peer_trusted(client[0], self.trusted):
            return "trusted"
        if self.bearer_ok(conn.headers.get("authorization", "")):
            return "bearer"
        if conn.scope.get("type") == "websocket" and self.token_ok(conn.query_params.get("token")):
            return "bearer"
        if self.sessions.valid(conn.cookies.get(COOKIE_NAME)):
            return "session"
        return None

    # brute-force brake for POST /login (the token is 256 bit; this only keeps
    # a hammering client from costing CPU and log lines).
    def login_throttled(self) -> bool:
        now = time.monotonic()
        self._fails[:] = [t for t in self._fails if now - t < 60.0]
        return len(self._fails) >= 10

    def login_failed(self) -> None:
        self._fails.append(time.monotonic())


# ── ASGI middleware ─────────────────────────────────────────────────────────

class AuthMiddleware:
    """Gate every HTTP request and WebSocket handshake (static files included)."""

    def __init__(self, app, auth: Authenticator) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not self.auth.enabled:
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if self.auth.is_public(path):
            return await self.app(scope, receive, send)
        how = self.auth.authorize(HTTPConnection(scope))
        if how is not None:
            scope["prius_auth"] = how
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            msg = await receive()                       # websocket.connect
            if msg.get("type") == "websocket.connect":
                await send({"type": "websocket.close", "code": 1008})
            return
        await self._refuse(scope, receive, send)

    async def _refuse(self, scope, receive, send):
        path = scope.get("path", "")
        accept = ""
        for k, v in scope.get("headers", []):
            if k == b"accept":
                accept = v.decode("latin-1")
        page = (scope.get("method") in ("GET", "HEAD") and not path.startswith("/api/")
                and "text/html" in accept)
        if page:
            target = path + ("?" + scope["query_string"].decode("latin-1")
                             if scope.get("query_string") else "")
            resp = RedirectResponse("/login?next=" + quote(safe_next(target), safe="/"),
                                    status_code=303)
        else:
            resp = JSONResponse({"detail": "authentication required"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        await resp(scope, receive, send)


# ── pages + routes ──────────────────────────────────────────────────────────

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0c0a18"><meta name="referrer" content="no-referrer">
<link rel="manifest" href="/manifest.webmanifest">
<title>Prius - %(title)s</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0c0a18;color:#ddd;font:16px/1.4 system-ui,sans-serif}
main{width:min(360px,calc(100vw - 32px))}
h1{font-size:20px;color:#0ef;margin:0 0 16px}
input,button{box-sizing:border-box;width:100%%;font:inherit;padding:12px;border-radius:8px}
input{background:#15122a;color:#fff;border:1px solid #444;margin-bottom:12px}
button{background:#f0a;color:#fff;border:0;font-weight:600}
p{color:#aaa}.err{color:#f66}
</style></head><body><main><h1>%(title)s</h1>%(body)s</main></body></html>
"""


def page(title: str, body: str) -> str:
    return _PAGE % {"title": html.escape(title), "body": body}


def login_page(next_path: str, error: str = "") -> str:
    err = '<p class="err">%s</p>' % html.escape(error) if error else ""
    return page("Prius computer", err + (
        '<form method="post" action="/login">'
        '<input type="hidden" name="next" value="%s">'
        '<input type="password" name="token" placeholder="access token" '
        'autocomplete="current-password" autofocus required>'
        '<button type="submit">Log in</button></form>'
        '<p>Or open a pairing link made on the car computer with '
        '<code>sudo prius-auth link</code>.</p>') % html.escape(next_path))


async def read_form(request, limit: int = 4096) -> Dict[str, str]:
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            break
    fields = parse_qs(body[:limit].decode("utf-8", "replace"), keep_blank_values=True)
    return {k: v[0] for k, v in fields.items()}


def register_routes(app, auth: Authenticator) -> None:
    """Login, logout, pairing and session management endpoints."""

    def with_cookie(resp, sid: str):
        resp.set_cookie(COOKIE_NAME, sid, max_age=SESSION_TTL_S, path="/",
                        httponly=True, samesite="lax", secure=False)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def html_resp(text: str, status: int = 200):
        return HTMLResponse(text, status_code=status, headers={"Cache-Control": "no-store"})

    @app.get("/auth/status")
    async def auth_status(request: Request):
        return {"auth": auth.enabled, "authenticated": auth.authorize(request) is not None}

    @app.get("/login")
    async def login_form(request: Request, next: str = "/"):
        target = safe_next(next)
        if auth.authorize(request) is not None:
            return RedirectResponse(target, status_code=303)
        return html_resp(login_page(target))

    @app.post("/login")
    async def login(request: Request):
        form = await read_form(request)
        target = safe_next(form.get("next"))
        if not auth.enabled:
            return RedirectResponse(target, status_code=303)
        if auth.login_throttled():
            return html_resp(login_page(target, "Too many attempts; wait a minute."), 429)
        if not auth.token_ok(form.get("token")):
            auth.login_failed()
            logger.warning("auth: failed login from %s", (request.client or ("?",))[0])
            await asyncio.sleep(1.0)
            return html_resp(login_page(target, "Wrong token."), 401)
        sid = auth.sessions.create("login", request.headers.get("user-agent", ""))
        logger.info("auth: login session created for %s", (request.client or ("?",))[0])
        return with_cookie(RedirectResponse(target, status_code=303), sid)

    @app.get("/logout")
    async def logout_form():
        return html_resp(page("Log out", (
            '<form method="post" action="/logout"><button type="submit">'
            'Log out this browser</button></form>')))

    @app.post("/logout")
    async def logout(request: Request):
        sid = request.cookies.get(COOKIE_NAME)
        if sid:
            auth.sessions.revoke(sid)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.get("/auth/pair")
    async def pair_form(code: str = ""):
        if not auth.enabled:
            return RedirectResponse("/", status_code=303)
        if not auth.pairing.peek(code):
            return html_resp(page("Pairing link", (
                '<p class="err">This link has expired or was already used.</p>'
                '<p>Make a new one on the car computer: '
                '<code>sudo prius-auth link</code></p>')), 403)
        # A confirm step instead of redeeming on GET: link-preview fetchers
        # (chat apps, launchers) must not burn the single-use code.
        return html_resp(page("Pair this browser", (
            '<p>Give this browser access to the Prius computer.</p>'
            '<form method="post" action="/auth/pair">'
            '<input type="hidden" name="code" value="%s">'
            '<button type="submit">Pair this browser</button></form>') % html.escape(code)))

    @app.post("/auth/pair")
    async def pair(request: Request):
        form = await read_form(request)
        if not auth.enabled:
            return RedirectResponse("/", status_code=303)
        if not auth.pairing.redeem(form.get("code")):
            await asyncio.sleep(0.5)
            return html_resp(page("Pairing link", (
                '<p class="err">This link has expired or was already used.</p>'
                '<p>Make a new one on the car computer: '
                '<code>sudo prius-auth link</code></p>')), 403)
        sid = auth.sessions.create("pair", request.headers.get("user-agent", ""))
        logger.info("auth: pairing code redeemed by %s", (request.client or ("?",))[0])
        return with_cookie(RedirectResponse("/", status_code=303), sid)

    @app.post("/auth/revoke-all")
    async def revoke_all(request: Request):
        if auth.authorize(request) is None:
            raise HTTPException(status_code=401, detail="authentication required",
                                headers={"WWW-Authenticate": "Bearer"})
        n = auth.sessions.revoke_all()
        logger.info("auth: all %d sessions revoked over the API", n)
        resp = JSONResponse({"revoked": n})
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp
