"""
roadsense -> MFD road scene feed (backend event ``roadsense_scene``).

The MFD main screen draws lanes, road edges, the planned path, the lead
vehicle, the ego traffic light, the speed limit, the signs in effect and the
scenery from a compact "scene" mapping (contract:
docs/MFD_MAIN_SCREEN.md section 7; consumer: ``ui/scene/inputs.SceneFeed``).
This module builds that mapping from what the roadsense stack already
publishes and hands it to the ZMQ event channel:

    roadsense api  ws://127.0.0.1:8090/ws?topics=road,lights,speed_limit,env,rules,fused,map
         │  (road 10-14/s ~16 kB, lights ~3/s, speed_limit and rules >= 1/s, env ~1/s,
         │   fused ~2/s, map 2/s ~4 kB)
         ▼
    RoadsenseSceneFeed (own thread)  ──SceneBuilder──▶  publish(scene)  <= 10/s, ~0.7 kB

Boundary: roadsense's documented WebSocket only (roadsense
docs/ARCHITECTURE.md, "api"); nothing is imported from the roadsense repos.
The client is a minimal RFC 6455 reader on the stdlib (text frames, ping ->
pong, close), so the backend needs no WebSocket package.

Rules:

* The feed only consumes. It never starts, stops or reconfigures roadsense
  and never changes its duty: the backend loop calls :meth:`set_active` with
  "roadsense manager RUNNING and the MFD powered"; inactive = socket closed,
  thread asleep.
* Nothing stale goes out. A scene is published only for a road frame that is
  new (its ``seq`` was not published before) and younger than
  ``road_max_age_s``; the slow topics ride along only while younger than their
  own max age, otherwise their key is left out ("not known"). When roadsense
  stops, publishing stops and the frontend's own staleness (SIGNAL LOST / NO
  SIGNAL) takes over.
* Cheap: road messages are kept as raw text and only the newest one is decoded
  when a publish is due (at most ``max_hz`` decodes/s); everything else is
  small. Reconnects back off from 1 s to 10 s.

Frames and units: as roadsense (openpilot device frame): metres, x forward,
y RIGHT, origin at the camera; the frontend flips y.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

EVENT_NAME = "roadsense_scene"
TOPICS = ("road", "lights", "speed_limit", "env", "rules", "fused", "map")

# Caps of the sign lists (payload and screen room: the MFD shows this many).
MAX_SIGNS_ON = 6
MAX_SIGNS_AHEAD = 3

# Sample distances of the scene polylines (m ahead of the camera). This is the
# contract's default "x", so the key is not sent.
SCENE_XS: Tuple[float, ...] = (0.0, 5.0, 10.0, 15.0, 20.0, 30.0, 40.0, 50.0, 65.0, 80.0, 100.0)


@dataclass
class SceneFeedConfig:
    """Knobs of the scene feed (BACKEND_ROADSENSE_SCENE_*)."""
    enabled: bool = True
    host: str = ""                     # roadsense api; "" / 0 = the roadsense manager's
    port: int = 0                      #   api_host / api_port (127.0.0.1:8090 on the car)
    path: str = "/ws?topics=" + ",".join(TOPICS)
    max_hz: float = 10.0               # publish cap (contract: 5..10 Hz)
    road_max_age_s: float = 1.0        # a road frame older than this is never published
    lights_max_age_s: float = 2.0      # lights ~3/s
    limit_max_age_s: float = 3.0       # speed_limit: on change and >= 1/s
    env_max_age_s: float = 5.0         # env ~1/s
    rules_max_age_s: float = 3.0       # rules: on change and >= 1/s
    map_max_age_s: float = 3.0         # fused / map: ~2/s (roadsense's own map staleness is 3 s)
    lane_min_prob: float = 0.2         # below this a lane line goes out as null (UI draws >= 0.3)
    lead_min_prob: float = 0.3         # below this the lead goes out as null (UI draws >= 0.5)
    connect_timeout_s: float = 3.0
    silence_s: float = 5.0             # connected but no message for this long -> reconnect
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 10.0
    max_message_bytes: int = 4 * 1024 * 1024


# ── conversion (pure) ────────────────────────────────────────────────────────

def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def resample(xs: Any, ys: Any, xq: Sequence[float] = SCENE_XS) -> Optional[List[float]]:
    """Linear interpolation of the polyline (xs, ys) at ``xq``.

    Returns None when the input is unusable (missing, too short, a null /
    non-finite value where it is needed, x not increasing). Query points past
    the last sample are dropped (the list is shorter), points before the first
    are clamped to it.
    """
    if not isinstance(xs, (list, tuple)) or not isinstance(ys, (list, tuple)):
        return None
    n = min(len(xs), len(ys))
    if n < 2:
        return None
    fx = [_num(v) for v in xs[:n]]
    if any(v is None for v in fx):
        return None
    out: List[float] = []
    j = 0
    for q in xq:
        if q > fx[n - 1]:          # type: ignore[operator]
            break
        while j < n - 2 and fx[j + 1] < q:   # type: ignore[operator]
            j += 1
        x0, x1 = fx[j], fx[j + 1]
        y0, y1 = _num(ys[j]), _num(ys[j + 1])
        if x0 is None or x1 is None or y0 is None or y1 is None or x1 <= x0:
            return None
        t = (q - x0) / (x1 - x0)
        if t < 0.0:
            t = 0.0
        out.append(round(y0 + (y1 - y0) * t, 2))
    return out if len(out) >= 2 else None


def road_to_scene(road: Dict[str, Any], cfg: SceneFeedConfig) -> Dict[str, Any]:
    """modeld ``road`` message -> scene keys ``seq``, ``lanes``, ``edges``,
    ``path``, ``lead``.

    lanes[i] = {"y": lane_lines[i].y at SCENE_XS, "prob": lane_lines[i].prob}
    edges[i] = {"y": road_edges[i].y at SCENE_XS}
    path     = {"y": plan.y at SCENE_XS} (the model's planned path; the list
               ends where the plan ends, so it is short at low speed)
    lead     = leads[0] (the "now" hypothesis): d = x[0], y = y[0],
               v_rel = v[0] - v_ego (the model's own speed estimate),
               a = a[0] (its acceleration, m/s2), prob
    """
    lanes: List[Optional[Dict[str, Any]]] = [None, None, None, None]
    for i, ln in enumerate((road.get("lane_lines") or [])[:4]):
        if not isinstance(ln, dict):
            continue
        p = _num(ln.get("prob"))
        if p is None or p < cfg.lane_min_prob:
            continue
        ys = resample(ln.get("x"), ln.get("y"))
        if ys is not None:
            lanes[i] = {"y": ys, "prob": round(p, 2)}
    edges: List[Optional[Dict[str, Any]]] = [None, None]
    for i, ed in enumerate((road.get("road_edges") or [])[:2]):
        if not isinstance(ed, dict):
            continue
        ys = resample(ed.get("x"), ed.get("y"))
        if ys is not None:
            edges[i] = {"y": ys}
    path = None
    plan = road.get("plan")
    if isinstance(plan, dict):
        ys = resample(plan.get("x"), plan.get("y"))
        if ys is not None:
            path = {"y": ys}
    lead = None
    leads = road.get("leads") or []
    if leads and isinstance(leads[0], dict):
        ld = leads[0]
        p = _num(ld.get("prob"))
        xs, ys, vs = ld.get("x") or [], ld.get("y") or [], ld.get("v") or []
        d = _num(xs[0]) if xs else None
        if p is not None and p >= cfg.lead_min_prob and d is not None and d > 0:
            y = _num(ys[0]) if ys else None
            lead = {"d": round(d, 1), "y": round(y or 0.0, 2), "prob": round(p, 2)}
            v = _num(vs[0]) if vs else None
            v_ego = _num(road.get("v_ego"))
            if v_ego is None:
                v_ego = _num((road.get("pose") or {}).get("vx"))
            if v is not None and v_ego is not None:
                lead["v_rel"] = round(v - v_ego, 1)
            acc = ld.get("a") or []
            a = _num(acc[0]) if acc else None
            if a is not None:
                lead["a"] = round(a, 1)
    seq = road.get("seq")
    out = {"seq": int(seq) if _num(seq) is not None else 0,
           "lanes": lanes, "edges": edges, "lead": lead}
    if path is not None:
        out["path"] = path
    return out


def lights_to_scene(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """api-annotated ``lights`` message -> scene ``light`` (the ego light) or None."""
    ego = msg.get("ego")
    if not isinstance(ego, dict):
        return None
    st = ego.get("state")
    if not isinstance(st, str) or not st:
        return None
    out: Dict[str, Any] = {"state": st}
    d, y = _num(ego.get("dist_m")), _num(ego.get("lateral_m"))
    if d is not None:
        out["d"] = round(d, 1)
    if y is not None:
        out["y"] = round(y, 1)
    if ego.get("flashing"):
        out["flashing"] = True
    return out


def limit_to_scene(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """api ``speed_limit`` message -> scene ``limit`` or None (unknown)."""
    k = _num(msg.get("limit_kmh"))
    if k is None or k <= 0:
        return None
    src = msg.get("source")
    kind = src.get("kind") if isinstance(src, dict) else None
    out: Dict[str, Any] = {"kmh": int(round(k))}
    if isinstance(kind, str) and kind:
        out["src"] = kind
    conf = _num(msg.get("confidence"))
    if conf is not None:
        out["conf"] = round(conf, 2)
    return out


def rules_to_scene(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """api ``rules`` message -> scene ``signs`` or None (nothing in effect).

    on    = rules in effect that bind the car, without the speed limit (it has
            its own key): {"id", "s": sign code, "v": value, "side": "l"|"r",
            "q": true when the engine marks the entry uncertain}
    ahead = warning signs, nearest first: {"id", "s", "m": expected distance}
    """
    on: List[Dict[str, Any]] = []
    for e in msg.get("in_effect") or ():
        if not isinstance(e, dict) or e.get("id") == "speed_limit":
            continue
        if e.get("applies_to_car") is False:
            continue
        rid = e.get("id")
        if not isinstance(rid, str) or not rid:
            continue
        item: Dict[str, Any] = {"id": rid}
        code = e.get("sign")
        if isinstance(code, str) and code:
            item["s"] = code[:8]
        val = _num(e.get("value"))
        if val is not None:
            item["v"] = int(val) if val == int(val) else round(val, 1)
        side = e.get("side")
        if isinstance(side, str) and side[:1] in ("l", "r"):
            item["side"] = side[:1]
        if e.get("uncertain"):
            item["q"] = True
        on.append(item)
        if len(on) >= MAX_SIGNS_ON:
            break
    ahead: List[Dict[str, Any]] = []
    for w in msg.get("ahead") or ():
        if not isinstance(w, dict):
            continue
        rid = w.get("id")
        if not isinstance(rid, str) or not rid:
            continue
        item = {"id": rid}
        code = w.get("sign")
        if isinstance(code, str) and code:
            item["s"] = code[:8]
        m = _num(w.get("expected_in_m"))
        if m is not None:
            item["m"] = int(round(m))
        ahead.append(item)
        if len(ahead) >= MAX_SIGNS_AHEAD:
            break
    if not on and not ahead:
        return None
    out: Dict[str, Any] = {}
    if on:
        out["on"] = on
    if ahead:
        out["ahead"] = ahead
    return out


def env_to_scene(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """envd ``env`` message -> scene ``env`` labels or None."""
    out: Dict[str, Any] = {}
    for g in ("light", "place", "weather"):
        grp = msg.get(g)
        lab = grp.get("label") if isinstance(grp, dict) else None
        if isinstance(lab, str) and lab:
            out[g] = lab
    return out or None


def _r(v: Any, nd: int = 0) -> Optional[float]:
    f = _num(v)
    if f is None:
        return None
    return int(round(f)) if nd == 0 else round(f, nd)


def _nearest(items: Any, key: str) -> Optional[Dict[str, Any]]:
    best = None
    for it in items or ():
        if isinstance(it, dict) and _num(it.get(key)) is not None:
            if best is None or it[key] < best[key]:
                best = it
    return best


def _drop_none(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def fused_to_scene(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """api ``fused`` message -> scene ``map`` (map-backed facts with provenance).

    Always a dict when the message is usable: ``ok`` false means the map layer
    is not used (``why`` = roadsense's reason, e.g. ``no_position``) and every
    fact below is roadsense's vision value. Field list: docs/MFD_MAIN_SCREEN.md 7.2.
    """
    mp = msg.get("map")
    if not isinstance(mp, dict):
        return None
    out: Dict[str, Any] = {"ok": bool(mp.get("available"))}
    for dst, src in (("state", "state"), ("why", "reason")):
        v = mp.get(src)
        if isinstance(v, str) and v:
            out[dst] = v
    conf = _r(mp.get("match_confidence"), 2)
    if conf is not None:
        out["conf"] = conf
    sl = msg.get("speed_limit")
    if isinstance(sl, dict):
        lim = _drop_none({"kmh": _r(sl.get("limit_kmh")), "src": sl.get("source") or None,
                          "conf": _r(sl.get("confidence"), 2), "agree": sl.get("agreement") or None,
                          "rule": sl.get("rule") or None, "sign": sl.get("sign") or None})
        if sl.get("variable"):
            lim["var"] = True
        out["limit"] = lim if lim.get("kmh") else None
    rd = msg.get("road")
    if isinstance(rd, dict):
        road = _drop_none({"cls": rd.get("class") or None, "name": rd.get("name") or None,
                           "ref": rd.get("ref") or None})
        for dst, src in (("oneway", "oneway"), ("rbt", "roundabout"), ("link", "link"),
                         ("expr", "expressway"), ("mroad", "motorroad"), ("dual", "dual_carriageway"),
                         ("bridge", "bridge"), ("toll", "toll")):
            if rd.get(src):
                road[dst] = True
        out["road"] = road
    ln = msg.get("lanes")
    if isinstance(ln, dict):
        lanes = _drop_none({"n": _r(ln.get("count")), "fwd": _r(ln.get("forward")),
                            "back": _r(ln.get("backward")), "ego": _r(ln.get("ego_index")),
                            "src": ln.get("source") or None})
        if isinstance(ln.get("oncoming_left"), bool):
            lanes["onc_left"] = ln["oncoming_left"]
        out["lanes"] = lanes or None
    tn = msg.get("tunnel")
    if isinstance(tn, dict):
        out["tunnel"] = _drop_none({"in": bool(tn.get("in_tunnel")), "exit_m": _r(tn.get("exit_in_m")),
                                    "entry_m": _r(tn.get("entry_in_m")), "len_m": _r(tn.get("length_m")),
                                    "src": tn.get("source") or None})
    bu = msg.get("built_up")
    if isinstance(bu, dict) and isinstance(bu.get("value"), bool):
        out["built_up"] = _drop_none({"v": bu["value"], "src": bu.get("source") or None})
    ah = msg.get("ahead")
    if isinstance(ah, dict):
        ahead: Dict[str, Any] = {}
        j = _nearest(ah.get("junctions"), "dist_m")
        if j:
            ahead["junction"] = _drop_none({"m": _r(j.get("dist_m")), "kind": j.get("kind") or None,
                                            "turn": _r(j.get("turn_deg")),
                                            "minor": True if j.get("minor") else None})
        c = _nearest(ah.get("curves"), "start_m")
        if c:
            d = c.get("direction")
            ahead["curve"] = _drop_none({"m": _r(c.get("start_m")), "r": _r(c.get("min_radius_m")),
                                         "dir": d[:1] if isinstance(d, str) and d else None})
        lc = _nearest(ah.get("speed_limit_changes"), "dist_m")
        if lc:
            ahead["limit"] = _drop_none({"m": _r(lc.get("dist_m")), "kmh": _r(lc.get("limit_kmh"))})
        f = _nearest(ah.get("features"), "dist_m")
        if f:
            ahead["feature"] = _drop_none({"m": _r(f.get("dist_m")), "kind": f.get("kind") or None})
        out["ahead"] = ahead or None
    pos = msg.get("position")
    if isinstance(pos, dict):
        out["pos"] = _drop_none({"src": pos.get("source") or None, "hacc": _r(pos.get("hacc_m"), 1)})
    return out


def map_input_to_scene(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """mapd ``map`` message -> scene ``map.in``: what mapd hears from the
    position feeders (``gnss`` = the receiver's ``source`` tag, e.g.
    ``headunit`` / ``synthetic``; ``motion`` = vehicle|vision|none; ``tag`` =
    the motion message's ``source``)."""
    inp = msg.get("input")
    if not isinstance(inp, dict):
        return None
    out: Dict[str, Any] = {}
    g = inp.get("gnss")
    if isinstance(g, dict):
        out.update(_drop_none({"gnss": g.get("source") or None, "fix": g.get("fix") or None,
                               "fix_age": _r(g.get("age_s"), 1)}))
    m = inp.get("motion")
    if isinstance(m, dict):
        out.update(_drop_none({"motion": m.get("source") or None, "tag": m.get("tag") or None}))
    return out or None


def peek_type(line: str) -> Optional[str]:
    """``type`` of a roadsense JSON line without decoding it (every daemon
    message starts with ``{"type":"..."``); None if it does not."""
    head = line[:48]
    i = head.find('"type"')
    if i < 0:
        return None
    i = head.find('"', head.find(":", i) + 1)
    if i < 0:
        return None
    j = head.find('"', i + 1)
    return head[i + 1:j] if j > i else None


class SceneBuilder:
    """Collects roadsense messages, builds a scene mapping when one is due.

    Not thread-safe: used by the feed thread only.
    """

    def __init__(self, cfg: SceneFeedConfig):
        self.cfg = cfg
        self.reset()

    def reset(self) -> None:
        self._road_raw: Optional[str] = None
        self._road_t = 0.0
        self._road_new = False
        self._last_seq: Optional[int] = None
        self._last_pub = -1e9
        self._slow: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
        self.decode_errors = 0

    def feed(self, line: str, now: float) -> None:
        mtype = peek_type(line)
        if mtype == "road":
            self._road_raw, self._road_t, self._road_new = line, now, True
            return
        try:
            msg = json.loads(line)
        except ValueError:
            self.decode_errors += 1
            return
        if not isinstance(msg, dict):
            return
        mtype = msg.get("type")
        if mtype == "road":
            self._road_raw, self._road_t, self._road_new = line, now, True
        elif mtype == "lights":
            self._slow["light"] = (now, lights_to_scene(msg))
        elif mtype == "speed_limit":
            self._slow["limit"] = (now, limit_to_scene(msg))
        elif mtype == "env":
            self._slow["env"] = (now, env_to_scene(msg))
        elif mtype == "rules":
            self._slow["signs"] = (now, rules_to_scene(msg))
        elif mtype == "fused":
            self._slow["map"] = (now, fused_to_scene(msg))
        elif mtype == "map":
            self._slow["map_in"] = (now, map_input_to_scene(msg))

    def time_to_due(self, now: float) -> Optional[float]:
        """Seconds until a pending road frame may be published; None if none."""
        if not self._road_new:
            return None
        return max(0.0, self._last_pub + 1.0 / max(0.1, self.cfg.max_hz) - now)

    def build(self, now: float) -> Optional[Dict[str, Any]]:
        """The scene for the newest road frame, or None (not due, nothing new,
        too old, same seq as the last publish, undecodable)."""
        wait = self.time_to_due(now)
        if wait is None or wait > 0.0:
            return None
        self._road_new = False
        if now - self._road_t > self.cfg.road_max_age_s or self._road_raw is None:
            return None
        try:
            road = json.loads(self._road_raw)
        except ValueError:
            self.decode_errors += 1
            return None
        if not isinstance(road, dict):
            return None
        scene = road_to_scene(road, self.cfg)
        if scene["seq"] and scene["seq"] == self._last_seq:
            return None
        self._last_seq = scene["seq"]
        self._last_pub = now
        ages = {"light": self.cfg.lights_max_age_s, "limit": self.cfg.limit_max_age_s,
                "env": self.cfg.env_max_age_s, "signs": self.cfg.rules_max_age_s,
                "map": self.cfg.map_max_age_s, "map_in": self.cfg.map_max_age_s}
        for key, (t, val) in self._slow.items():
            if now - t <= ages[key]:
                scene[key] = val
        # mapd's input echo rides inside "map" (only when fused is fresh too)
        map_in = scene.pop("map_in", None)
        if map_in and isinstance(scene.get("map"), dict):
            scene["map"] = dict(scene["map"], **{"in": map_in})
        return scene


# ── minimal WebSocket client (RFC 6455, client side, text messages) ──────────

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WsClosed(ConnectionError):
    """The server closed the WebSocket (or the TCP stream ended)."""


class MiniWebSocket:
    """Blocking WebSocket client for one roadsense ``/ws`` stream.

    ``recv(timeout)`` returns the next text message, or None on timeout (a
    partial frame stays buffered). Pings are answered, close frames end the
    stream with :class:`WsClosed`. No extensions are negotiated, so frames are
    never compressed.
    """

    def __init__(self, sock: socket.socket, max_message: int = 4 * 1024 * 1024,
                 buffered: bytes = b""):
        self.sock = sock
        self.max_message = max_message
        self._buf = bytearray(buffered)
        self._frag: Optional[bytearray] = None

    @classmethod
    def connect(cls, host: str, port: int, path: str, timeout: float = 3.0,
                max_message: int = 4 * 1024 * 1024) -> "MiniWebSocket":
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                   "Sec-WebSocket-Version: 13\r\nUser-Agent: prius-backend\r\n\r\n"
                   % (path, host, port, key))
            sock.sendall(req.encode("ascii"))
            head = b""
            deadline = time.monotonic() + timeout
            while b"\r\n\r\n" not in head:
                if len(head) > 16384 or time.monotonic() > deadline:
                    raise ConnectionError("bad WebSocket handshake (no header end)")
                chunk = sock.recv(4096)
                if not chunk:
                    raise WsClosed("closed during handshake")
                head += chunk
            head, rest = head.split(b"\r\n\r\n", 1)
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ", 2)
            if len(parts) < 2 or parts[1] != "101":
                raise ConnectionError("WebSocket upgrade refused: %r" % lines[0][:80])
            hdrs = {}
            for ln in lines[1:]:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    hdrs[k.strip().lower()] = v.strip()
            want = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode("ascii")).digest())
            if hdrs.get("sec-websocket-accept", "").encode("ascii") != want:
                raise ConnectionError("bad Sec-WebSocket-Accept")
        except Exception:
            sock.close()
            raise
        return cls(sock, max_message, rest)

    def _send(self, opcode: int, payload: bytes = b"") -> None:
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            hdr = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            hdr = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            hdr = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self.sock.sendall(hdr + mask + masked)

    def _frame(self) -> Optional[Tuple[bool, int, bytes]]:
        """Pop one complete frame from the buffer: (fin, opcode, payload)."""
        b = self._buf
        if len(b) < 2:
            return None
        fin, opcode = bool(b[0] & 0x80), b[0] & 0x0F
        masked, n = bool(b[1] & 0x80), b[1] & 0x7F
        pos = 2
        if n == 126:
            if len(b) < 4:
                return None
            n = struct.unpack_from("!H", b, 2)[0]
            pos = 4
        elif n == 127:
            if len(b) < 10:
                return None
            n = struct.unpack_from("!Q", b, 2)[0]
            pos = 10
        if n > self.max_message:
            raise ConnectionError("WebSocket frame of %d bytes" % n)
        mask = b""
        if masked:                       # servers must not mask; tolerate it
            if len(b) < pos + 4:
                return None
            mask = bytes(b[pos:pos + 4])
            pos += 4
        if len(b) < pos + n:
            return None
        payload = bytes(b[pos:pos + n])
        del b[:pos + n]
        if mask:
            payload = bytes(c ^ mask[i & 3] for i, c in enumerate(payload))
        return fin, opcode, payload

    def recv(self, timeout: float) -> Optional[str]:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            fr = self._frame()
            if fr is not None:
                fin, opcode, payload = fr
                if opcode == 0x9:                       # ping
                    self._send(0xA, payload)
                    continue
                if opcode == 0xA:                       # pong
                    continue
                if opcode == 0x8:                       # close
                    try:
                        self._send(0x8, payload[:2])
                    except OSError:
                        pass
                    raise WsClosed("server closed (%r)" % payload[2:60])
                if opcode in (0x1, 0x2):
                    if fin:
                        return payload.decode("utf-8", "replace")
                    self._frag = bytearray(payload)
                    continue
                if opcode == 0x0 and self._frag is not None:
                    self._frag += payload
                    if len(self._frag) > self.max_message:
                        raise ConnectionError("WebSocket message too large")
                    if fin:
                        data, self._frag = bytes(self._frag), None
                        return data.decode("utf-8", "replace")
                    continue
                raise ConnectionError("unexpected WebSocket opcode %d" % opcode)
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                raise WsClosed("connection closed")
            self._buf += chunk

    def close(self) -> None:
        try:
            self._send(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ── the feed thread ──────────────────────────────────────────────────────────

class RoadsenseSceneFeed:
    """Background reader of the roadsense WebSocket that publishes scenes.

    ``publish(scene)`` is called on the feed thread and must be non-blocking
    and thread-safe (``ZmqServer.enqueue_latest_event`` is). The backend loop
    calls :meth:`set_active` every tick; the thread connects only while active.
    """

    def __init__(self, cfg: SceneFeedConfig, publish: Callable[[Dict[str, Any]], None],
                 clock: Callable[[], float] = time.monotonic,
                 connect: Optional[Callable[[], Any]] = None):
        self.cfg = cfg
        cfg.host = cfg.host or "127.0.0.1"
        cfg.port = cfg.port or 8090
        self._publish = publish
        self._clock = clock
        self._connect = connect or (lambda: MiniWebSocket.connect(
            cfg.host, cfg.port, cfg.path, cfg.connect_timeout_s, cfg.max_message_bytes))
        self.builder = SceneBuilder(cfg)
        self._active = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.stats: Dict[str, Any] = {"connects": 0, "connect_errors": 0, "messages": 0,
                                      "published": 0, "last_error": None, "connected": False}

    # ── control (any thread) ─────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._active

    def set_active(self, on: bool) -> None:
        on = bool(on)
        if on != self._active:
            logger.info("roadsense scene feed %s", "active" if on else "idle")
            self._active = on
            self._wake.set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="roadsense-scene", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    # ── thread ───────────────────────────────────────────────────────────

    def _sleep(self, s: float) -> None:
        self._wake.wait(s)
        self._wake.clear()

    def _run(self) -> None:
        ws = None
        backoff = self.cfg.reconnect_min_s
        last_rx = 0.0
        while not self._stop.is_set():
            if not self._active:
                if ws is not None:
                    ws.close()
                    ws = None
                    self.stats["connected"] = False
                self.builder.reset()
                backoff = self.cfg.reconnect_min_s
                self._sleep(1.0)
                continue
            if ws is None:
                try:
                    ws = self._connect()
                except Exception as exc:  # noqa: BLE001 - stack starting / gone
                    self.stats["connect_errors"] += 1
                    self.stats["last_error"] = "connect: %s" % exc
                    logger.debug("roadsense scene: connect failed: %s", exc)
                    self._sleep(backoff)
                    backoff = min(backoff * 2.0, self.cfg.reconnect_max_s)
                    continue
                self.stats["connects"] += 1
                self.stats["connected"] = True
                logger.info("roadsense scene: connected to %s:%d", self.cfg.host, self.cfg.port)
                self.builder.reset()
                last_rx = self._clock()
            now = self._clock()
            due = self.builder.time_to_due(now)
            timeout = 0.25 if due is None else min(0.25, due)
            try:
                line = ws.recv(timeout)
            except Exception as exc:  # noqa: BLE001 - closed / reset / protocol
                self.stats["last_error"] = "recv: %s" % exc
                logger.info("roadsense scene: stream ended (%s)", exc)
                ws.close()
                ws = None
                self.stats["connected"] = False
                self.builder.reset()
                self._sleep(self.cfg.reconnect_min_s)
                continue
            now = self._clock()
            if line is not None:
                last_rx = now
                self.stats["messages"] += 1
                self.builder.feed(line, now)
            elif now - last_rx > self.cfg.silence_s:
                self.stats["last_error"] = "silent %.0fs" % (now - last_rx)
                logger.info("roadsense scene: no message for %.0fs, reconnecting",
                            now - last_rx)
                ws.close()
                ws = None
                self.stats["connected"] = False
                self.builder.reset()
                continue
            scene = self.builder.build(now)
            if scene is not None and self._active:
                try:
                    self._publish(scene)
                    self.stats["published"] += 1
                except Exception:  # noqa: BLE001 - never kill the feed
                    logger.exception("roadsense scene publish failed")
        if ws is not None:
            ws.close()
        self.stats["connected"] = False
