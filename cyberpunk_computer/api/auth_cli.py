"""
prius-auth: manage network API access on the car computer (run as root).

    prius-auth link [--host ADDR] [--ttl 15m]   one-time pairing link for a browser
    prius-auth hosts                            addresses a link may use
    prius-auth sessions                         list browser sessions
    prius-auth revoke ID-PREFIX                 revoke one session
    prius-auth revoke-all                       revoke every browser session
    prius-auth token new [--stdout]             new BACKEND_AUTH_TOKEN (restart the backend)
    prius-auth status                           token set? state dir, counts

The link carries a single-use code (default 15 min), never the token. State
(sessions.json, pair/) lives in BACKEND_AUTH_STATE_DIR (/var/lib/prius/auth),
the token in BACKEND_AUTH_ENV (/etc/prius/backend-auth.env), both root 0600.
The running backend picks up revokes and new codes from disk; a new token needs
``systemctl restart prius-backend``. docs/AUTH.md.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

from .auth import (
    DEFAULT_STATE_DIR,
    PAIRING_TTL_MAX_S,
    PairingCodes,
    SessionStore,
    _write_private,
    parse_duration,
)

DEFAULT_AUTH_ENV = "/etc/prius/backend-auth.env"


def state_dir() -> str:
    return os.environ.get("BACKEND_AUTH_STATE_DIR") or DEFAULT_STATE_DIR


def auth_env_path() -> Path:
    return Path(os.environ.get("BACKEND_AUTH_ENV") or DEFAULT_AUTH_ENV)


def api_port() -> int:
    return int(os.environ.get("BACKEND_API_PORT") or 8080)


def token_configured() -> bool:
    try:
        text = auth_env_path().read_text()
    except OSError:
        return bool((os.environ.get("BACKEND_AUTH_TOKEN") or "").strip())
    m = re.search(r"^BACKEND_AUTH_TOKEN=(\S+)", text, re.M)
    return bool(m)


def local_hosts(ip_output: Optional[str] = None) -> List[Tuple[str, str]]:
    """(interface, IPv4) pairs a pairing link may use: WireGuard first, then
    the car's Wi-Fi AP (10.42.0.1), then the USB link. Mirrors the firewall."""
    if ip_output is None:
        try:
            ip_output = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True,
                                       text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            ip_output = ""
    found = []
    for line in ip_output.splitlines():
        m = re.match(r"\d+:\s+(\S+)\s+inet\s+([\d.]+)/", line)
        if not m:
            continue
        iface, addr = m.group(1).split("@")[0], m.group(2)
        if iface.startswith("wg"):
            found.append((0, iface, addr))
        elif iface.startswith("wlan") and addr == "10.42.0.1":
            found.append((1, iface, addr))
        elif iface.startswith("usb"):
            found.append((2, iface, addr))
    return [(i, a) for _, i, a in sorted(found)]


def _require_root() -> None:
    if os.geteuid() != 0:
        sys.exit("prius-auth: run as root (sudo prius-auth ...)")


def cmd_link(args) -> int:
    _require_root()
    if not token_configured():
        print("prius-auth: WARNING: no BACKEND_AUTH_TOKEN configured; the API is open and "
              "the link does nothing", file=sys.stderr)
    ttl = parse_duration(args.ttl)
    if not 0 < ttl <= PAIRING_TTL_MAX_S:
        sys.exit("prius-auth: --ttl must be between 1s and 24h")
    host = args.host
    if not host:
        hosts = local_hosts()
        if not hosts:
            sys.exit("prius-auth: no WireGuard/AP address found; give --host")
        host = hosts[0][1]
    code = PairingCodes(state_dir()).issue(ttl)
    port = args.port or api_port()
    print("http://%s:%d/auth/pair?code=%s" % (host, port, code))
    print("single use, valid until %s (%d min); open it on the device to pair"
          % (time.strftime("%H:%M %Z", time.localtime(time.time() + ttl)), round(ttl / 60)),
          file=sys.stderr)
    return 0


def cmd_hosts(args) -> int:
    for iface, addr in local_hosts():
        print("%-12s %s" % (iface, addr))
    return 0


def cmd_sessions(args) -> int:
    _require_root()
    rows = SessionStore(state_dir()).list()
    if not rows:
        print("no sessions")
    for r in rows:
        print("%s  %-6s created %s  expires %s  %s" % (
            r["id"], r.get("label", ""),
            time.strftime("%Y-%m-%d %H:%M", time.localtime(float(r["created"]))),
            time.strftime("%Y-%m-%d", time.localtime(float(r["expires"]))),
            r.get("agent", "")))
    return 0


def cmd_revoke(args) -> int:
    _require_root()
    if len(args.prefix) < 6:
        sys.exit("prius-auth: give at least 6 characters of the session id")
    n = SessionStore(state_dir()).revoke_prefix(args.prefix)
    print("revoked %d session(s)" % n)
    return 0 if n else 1


def cmd_revoke_all(args) -> int:
    _require_root()
    n = SessionStore(state_dir()).revoke_all()
    print("revoked %d session(s)" % n)
    return 0


def cmd_token(args) -> int:
    _require_root()
    if args.action != "new":
        sys.exit("prius-auth: token new [--stdout]")
    token = secrets.token_urlsafe(32)
    path = auth_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_private(path, (
        "# prius-backend API token (root 0600; prius-auth token new). docs/AUTH.md\n"
        "BACKEND_AUTH_TOKEN=%s\n" % token).encode())
    if args.stdout:
        sys.stdout.write(token + "\n")
    print("prius-auth: new token written to %s; restart prius-backend to apply"
          % path, file=sys.stderr)
    return 0


def cmd_status(args) -> int:
    print("token     : %s (%s)" % ("set" if token_configured() else "NOT SET (API open)",
                                   auth_env_path()))
    print("state dir : %s" % state_dir())
    if os.geteuid() == 0:
        print("sessions  : %d" % len(SessionStore(state_dir()).list()))
    print("hosts     : %s" % (", ".join(a for _, a in local_hosts()) or "-"))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="prius-auth", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("link", help="print a one-time pairing link")
    s.add_argument("--host", help="address in the link (default: WireGuard, else the AP)")
    s.add_argument("--port", type=int, help="API port (default BACKEND_API_PORT or 8080)")
    s.add_argument("--ttl", default="15m", help="validity, e.g. 15m, 1h (max 24h)")
    s.set_defaults(fn=cmd_link)
    sub.add_parser("hosts", help="addresses a link may use").set_defaults(fn=cmd_hosts)
    sub.add_parser("sessions", help="list browser sessions").set_defaults(fn=cmd_sessions)
    s = sub.add_parser("revoke", help="revoke one session by id prefix")
    s.add_argument("prefix")
    s.set_defaults(fn=cmd_revoke)
    sub.add_parser("revoke-all", help="revoke every browser session").set_defaults(fn=cmd_revoke_all)
    s = sub.add_parser("token", help="token new [--stdout]")
    s.add_argument("action", choices=["new"])
    s.add_argument("--stdout", action="store_true",
                   help="also print the new token (pipe it into the vault, nowhere else)")
    s.set_defaults(fn=cmd_token)
    sub.add_parser("status", help="token/state summary").set_defaults(fn=cmd_status)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
