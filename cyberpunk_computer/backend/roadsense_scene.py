"""
roadsense -> MFD road scene feed (backend event ``roadsense_scene``).

The MFD main screen draws lanes, road edges, the planned path, the lead
vehicle, the ego traffic light, the speed limit, the signs in effect and the
scenery from a compact "scene" mapping (contract:
docs/MFD_MAIN_SCREEN.md section 7; consumer: ``ui/scene/inputs.SceneFeed``).
This module builds that mapping from what the roadsense stack already
publishes and hands it to the ZMQ event channel:

    roadsense api  ws://127.0.0.1:8090/ws?topics=road,lights,speed_limit,env,rules,fused,map,lead,lanes,view
         │  (road 10-14/s ~16 kB, lights ~3/s, speed_limit and rules >= 1/s, env ~1/s,
         │   fused ~2/s, map 2/s ~4 kB, lead 10-14/s ~0.7 kB)
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
TOPICS = ("road", "lights", "speed_limit", "env", "rules", "fused", "map", "lead", "lanes", "view")

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
    lead_max_age_s: float = 0.5        # lead (tracker): one per road frame, 10-14/s; older -> road.leads[0]
    lanes_max_age_s: float = 0.5       # lanes (road-geometry tracker): one per road frame; older -> road.lane_lines
    view_max_age_s: float = 0.5        # view (roadsense 2.0 display channel): one per road frame; while fresh the
                                       # scene is built per view message (no road decode) and carries "vw"
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


# A tracked line is "present" by roadsense's own hysteresis; the screen still
# gates on prob >= 0.3 (contract 7.1), so a present line never goes out lower.
TRACKED_MIN_PROB = 0.3


def lanes_to_scene(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """roadsense ``lanes`` topic (the api's road-geometry tracker) -> scene
    ``lanes`` and ``edges``.

    lanes[i] = {"y": lines[i].y at SCENE_XS, "prob": max(lines[i].conf, 0.3)}
               for a line the tracker reports ``present`` (its presence has
               hysteresis and its geometry is filtered over time), else null
    edges[i] = {"y": edges[i].y at SCENE_XS} for a present road edge, else null

    None when the message is not usable (then the raw road lines stay).
    """
    xs = msg.get("x")
    lines, edges = msg.get("lines"), msg.get("edges")
    if not isinstance(xs, list) or not isinstance(lines, list) or not isinstance(edges, list):
        return None
    out_l: List[Optional[Dict[str, Any]]] = [None, None, None, None]
    for i, ln in enumerate(lines[:4]):
        if not isinstance(ln, dict) or ln.get("present") is not True:
            continue
        ys = resample(xs, ln.get("y"))
        if ys is not None:
            p = _num(ln.get("conf")) or 0.0
            out_l[i] = {"y": ys, "prob": round(min(1.0, max(TRACKED_MIN_PROB, p)), 2)}
    out_e: List[Optional[Dict[str, Any]]] = [None, None]
    for i, ed in enumerate(edges[:2]):
        if not isinstance(ed, dict) or ed.get("present") is not True:
            continue
        ys = resample(xs, ed.get("y"))
        if ys is not None:
            out_e[i] = {"y": ys}
    return {"seq": msg.get("seq"), "lanes": out_l, "edges": out_e}


def lead_to_scene(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """roadsense ``lead`` topic (the api's lead tracker) -> scene ``lead``.

    d = dist_m, y = lateral_m, v_rel = v_rel_mps, a = a_lead_mps2, prob (the
    frame's vision probability; left out when the tracker coasts without one,
    the UI then treats it as 1), src = "tracked"; only when set: standing /
    departed (true), gap = time_gap_s, d_std = dist_std_m, ttc = ttc_s,
    id = track_id. None when ``present`` is false (no lead tracked).
    """
    if not msg.get("present"):
        return None
    d = _num(msg.get("dist_m"))
    if d is None or d <= 0:
        return None
    out: Dict[str, Any] = {"d": round(d, 1), "y": round(_num(msg.get("lateral_m")) or 0.0, 2)}
    v_rel = _num(msg.get("v_rel_mps"))
    if v_rel is not None:
        out["v_rel"] = round(v_rel, 1)
    a = _num(msg.get("a_lead_mps2"))
    if a is not None:
        out["a"] = round(a, 1)
    p = _num(msg.get("prob"))
    if p is not None:
        out["prob"] = round(p, 2)
    out["src"] = "tracked"
    if msg.get("standing") is True:
        out["standing"] = True
    if msg.get("departed") is True:
        out["departed"] = True
    for key, src, nd in (("gap", "time_gap_s", 2), ("d_std", "dist_std_m", 2), ("ttc", "ttc_s", 2)):
        v = _num(msg.get(src))
        if v is not None:
            out[key] = round(v, nd)
    tid = msg.get("track_id")
    if isinstance(tid, int) and not isinstance(tid, bool):
        out["id"] = tid
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
    # api annotation: how sure the ego assignment is (alerts' green-light half)
    rel = _num(ego.get("relevance"))
    if rel is not None:
        out["rel"] = round(rel, 2)
    agree = _num(ego.get("agree"))
    if agree is not None:
        out["agree"] = int(agree)
    # lane assignment (roadsense >= 2026-11-17): two signal groups in view
    # (``alt`` = the other group's state and side) and how sure the choice is
    if ego.get("amb") is True:
        out["q"] = True
    asg = ego.get("assign")
    if isinstance(asg, dict):
        c = _num(asg.get("confidence"))
        if c is not None:
            out["c"] = round(c, 2)
        alt = asg.get("alt")
        if asg.get("conflict") and isinstance(alt, dict) and isinstance(alt.get("state"), str):
            side = alt.get("side")
            out["alt"] = {"s": alt["state"], "side": side[:1] if isinstance(side, str) and side else ""}
    if ego.get("held") is True:
        out["held"] = True
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
                            "src": ln.get("source") or None, "c": _r(ln.get("confidence"), 2)})
        if isinstance(ln.get("oncoming_left"), bool):
            lanes["onc_left"] = ln["oncoming_left"]
        # roadsense >= L20: the ego-lane filter's probability per own lane, from the left
        pr = ln.get("probs")
        if (ln.get("probs_from") == "left" and isinstance(pr, list) and pr and len(pr) == ln.get("forward")
                and all(isinstance(v, (int, float)) for v in pr)):
            lanes["p"] = [round(float(v), 2) for v in pr]
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
        ahead: Dict[str, Any] = _drop_none({"pc": _r(ah.get("path_confidence"), 2)})
        j = _nearest(ah.get("junctions"), "dist_m")
        if j:
            ahead["junction"] = _drop_none({"m": _r(j.get("dist_m")), "kind": j.get("kind") or None,
                                            "turn": _r(j.get("turn_deg")),
                                            "minor": True if j.get("minor") else None,
                                            "c": _r(j.get("confidence"), 2)})
        jn = _junction_list(ah.get("junctions"))
        if jn:
            ahead["jn"] = jn
        c = _nearest(ah.get("curves"), "start_m")
        if c:
            d = c.get("direction")
            ahead["curve"] = _drop_none({"m": _r(c.get("start_m")), "r": _r(c.get("min_radius_m")),
                                         "dir": d[:1] if isinstance(d, str) and d else None})
        lc = _nearest(ah.get("speed_limit_changes"), "dist_m")
        if lc:
            ahead["limit"] = _drop_none({"m": _r(lc.get("dist_m")), "kmh": _r(lc.get("limit_kmh")),
                                         "src": lc.get("source") or None, "c": _r(lc.get("confidence"), 2)})
        f = _nearest(ah.get("features"), "dist_m")
        if f:
            ahead["feature"] = _drop_none({"m": _r(f.get("dist_m")), "kind": f.get("kind") or None})
        out["ahead"] = ahead or None
    pos = msg.get("position")
    if isinstance(pos, dict):
        out["pos"] = _drop_none({"src": pos.get("source") or None, "hacc": _r(pos.get("hacc_m"), 1)})
    ld = msg.get("lead")
    if isinstance(ld, dict):
        # roadsense's tracked lead (api lead.py; null when the tracker has no
        # fresh estimate). Consumed by the alert functions (stop-and-go).
        out["lead"] = _drop_none({
            "present": bool(ld.get("present")), "d": _r(ld.get("dist_m"), 1), "y": _r(ld.get("lateral_m"), 2),
            "v_rel": _r(ld.get("v_rel_mps"), 2), "v": _r(ld.get("v_lead_mps"), 2),
            "a": _r(ld.get("a_lead_mps2"), 2), "gap": _r(ld.get("time_gap_s"), 2), "ttc": _r(ld.get("ttc_s"), 2),
            "standing": True if ld.get("standing") else None, "departed": True if ld.get("departed") else None,
            "mode": ld.get("mode") or None,
            "id": ld.get("track_id") if isinstance(ld.get("track_id"), int) else None})
    return out


JUNCTION_CODES = {"junction": "j", "roundabout": "r", "roundabout_exit": "x", "dead_end": "d",
                  "no_way_on": "n"}
JUNCTIONS_MAX = 6          # entries of ``ahead.jn``
JUNCTIONS_MAX_M = 700.0    # ... within this distance (the road shape reaches 600 m)
SHAPE_EVERY = 2            # mapd's road shape has a point every 20 m: every second one is passed on


def _junction_list(items: Any) -> List[List[Any]]:
    """``ahead.jn``: the junctions that decide how far the most likely path can be
    trusted, nearest first, as ``[m, confidence, code]`` (codes: ``JUNCTION_CODES``).
    Junctions whose side branches are all service roads (``minor``) are left out."""
    out: List[List[Any]] = []
    for j in items or ():
        if not isinstance(j, dict) or j.get("minor"):
            continue
        m, c = _num(j.get("dist_m")), _num(j.get("confidence"))
        code = JUNCTION_CODES.get(j.get("kind"))
        if m is None or c is None or code is None or m > JUNCTIONS_MAX_M:
            continue
        out.append([int(round(m)), round(c, 2), code])
    out.sort(key=lambda e: e[0])
    return out[:JUNCTIONS_MAX]


def map_shape_to_scene(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """mapd ``map`` message -> scene ``map.shape``: the road ahead along the most
    likely path beyond camera range, ``{"ds": 40, "x": [...], "y": [...]}``: a
    point every ``ds`` metres of path from the matched position, x forward along
    the matched bearing, y RIGHT (modeld's frame), whole metres. None without
    a shape (no match, a path shorter than one step)."""
    ah = msg.get("ahead")
    sh = ah.get("shape") if isinstance(ah, dict) else None
    if not isinstance(sh, dict):
        return None
    s, x, y = sh.get("s"), sh.get("x"), sh.get("y")
    if not (isinstance(s, list) and isinstance(x, list) and isinstance(y, list)):
        return None
    n = min(len(s), len(x), len(y))
    if n < 2 * SHAPE_EVERY + 1:
        return None
    xs, ys = [], []
    for i in range(0, n, SHAPE_EVERY):
        fx, fy = _num(x[i]), _num(y[i])
        if fx is None or fy is None:
            return None
        xs.append(int(round(fx)))
        ys.append(int(round(fy)))
    ds = _num(s[SHAPE_EVERY])
    if ds is None or ds <= 0:
        return None
    return {"ds": int(round(ds)), "x": xs, "y": ys}


_ROAD_RANK = {"motorway": 0, "motorway_link": 0, "trunk": 0, "trunk_link": 0, "primary": 0, "primary_link": 0,
              "secondary": 0, "secondary_link": 0, "tertiary": 1, "tertiary_link": 1, "unclassified": 1,
              "residential": 2, "living_street": 2, "service": 3, "track": 3}
_CONTROL = {"signals": "s", "give_way": "g", "stop": "t"}
JV_MAX_ARMS = 8


def map_junction_to_scene(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """mapd ``map`` message -> scene ``map.jv``: the junction view (roadsense
    ``ahead.junction_view``, the first major junction or roundabout on the most
    likely path), compact:

        {"k": "j" | "r", "m": metres to it, "c": path confidence up to it,
         "ctl": "s" signals | "g" give way | "t" stop (left out: not known),
         "ln": lanes of the approach in the car's direction (tagged only),
         "to": name or number of the road the most likely path takes,
         "arms": [[angle, flags, rank, exit_no], ...],
         "ring": {"r": radius m, "n": exits, "p": exit number of the path,
                  "on": the car is on the ring, "car": its angle on the ring}}

    ``angle``: degrees from "straight on", positive = right, 180 = where the
    car comes from (for a roundabout: where the arm meets the ring, seen from
    its centre). ``flags``: letters x (can be driven into), e (traffic comes
    out of it), p (the most likely path), m (minor: service road), a (the
    approach arm of a roundabout). ``rank``: 0 main roads .. 3 service.
    ``exit_no``: roundabout exits in driving order (0 = not an exit).
    None without a view."""
    ah = msg.get("ahead")
    return junction_to_scene(ah.get("junction_view") if isinstance(ah, dict) else None)


def junction_to_scene(jv: Any) -> Optional[Dict[str, Any]]:
    """A junction view (roadsense ``view.road.junction``; the same object as mapd's
    ``ahead.junction_view`` with the distance carried to the frame and the marks
    ``approximate`` / ``why``) -> scene ``map.jv``. ``ap``: true when roadsense marks it
    approximate (dead reckoning, a weak or held position)."""
    if not isinstance(jv, dict) or jv.get("kind") not in ("junction", "roundabout"):
        return None
    m, c = _num(jv.get("dist_m")), _num(jv.get("confidence"))
    if m is None or c is None:
        return None
    out: Dict[str, Any] = {"k": "r" if jv["kind"] == "roundabout" else "j", "m": int(round(m)), "c": round(c, 2)}
    ctl = _CONTROL.get(jv.get("control"))
    if ctl:
        out["ctl"] = ctl
    if jv.get("approximate"):
        out["ap"] = True
    ap = jv.get("approach")
    if isinstance(ap, dict) and ap.get("lanes_source") in ("tagged", "tagged_total"):
        n = _num(ap.get("lanes"))
        if n is not None and n >= 1:
            out["ln"] = int(n)
    arms: List[List[Any]] = []
    for a in jv.get("arms") or ():
        if not isinstance(a, dict):
            continue
        ang = _num(a.get("angle_deg"))
        if ang is None:
            continue
        flags = "".join(ch for ch, key in (("x", "exit"), ("e", "entry"), ("p", "path"), ("m", "minor"),
                                           ("a", "approach")) if a.get(key))
        no = a.get("exit_no")
        arms.append([int(round(ang)), flags, _ROAD_RANK.get(a.get("class"), 2),
                     int(no) if isinstance(no, int) and not isinstance(no, bool) else 0])
        if a.get("path"):
            to = a.get("ref") or a.get("name")
            if isinstance(to, str) and to:
                out["to"] = to[:24]
        if len(arms) >= JV_MAX_ARMS:
            break
    if not arms:
        return None
    out["arms"] = arms
    rg = jv.get("ring")
    if isinstance(rg, dict):
        out["ring"] = _drop_none({"r": _r(rg.get("radius_m")), "n": _r(rg.get("exits")),
                                  "p": _r(rg.get("path_exit_no")), "car": _r(rg.get("car_angle_deg"))})
        if rg.get("on_ring"):
            out["ring"]["on"] = True
    return out


# ── fused road geometry (roadsense view.road) ────────────────────────────────

GEO_MAX_M = 400.0          # the centre line is passed on this far
GEO_MAX_POINTS = 28
GEO_BRANCH_MAX = 8
GEO_BRANCH_M = 200.0       # side roads up to here; service roads up to GEO_MINOR_M
GEO_MINOR_M = 70.0
GEO_STEER_DEG = 40.0       # the centre line steers the picture while it runs within this of straight ahead
_CLASS_RANK = {"motorway": 0, "trunk": 0, "primary": 0, "secondary": 0, "tertiary": 1, "unclassified": 1,
               "residential": 2, "living_street": 2, "service": 3, "track": 3}
_END_CODE = {"intent": "i", "dead_end": "d", "no_way_on": "n", "path_end": "p", "horizon": "h"}


def _thin(pts: List[Tuple[float, float, float]], tol_near: float = 0.25, tol_far: float = 1.0) -> List[int]:
    """Indices of a polyline's points that keep its shape (Douglas-Peucker; the
    tolerance grows beyond 120 m, where a metre is a fraction of a pixel)."""
    keep = [0, len(pts) - 1]
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay, bx, by = pts[a][0], pts[a][1], pts[b][0], pts[b][1]
        dx, dy = bx - ax, by - ay
        n = math.hypot(dx, dy) or 1.0
        worst, wi = 0.0, -1
        for i in range(a + 1, b):
            d = abs((pts[i][0] - ax) * dy - (pts[i][1] - ay) * dx) / n
            if d > worst:
                worst, wi = d, i
        if wi >= 0 and worst > (tol_near if math.hypot(pts[wi][0], pts[wi][1]) < 120.0 else tol_far):
            keep.append(wi)
            stack.append((a, wi))
            stack.append((wi, b))
            continue
        # straight enough: keep the points where the confidence steps (a junction on the path)
        for i in range(a + 1, b + 1):
            if abs(pts[i][2] - pts[i - 1][2]) > 0.1:
                keep.append(i - 1)
                keep.append(i)
    return sorted(set(keep))


def roadgeom_to_scene(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """roadsense's fused road geometry (``view.road``; api 1: the topic ``roadgeom``)
    -> the scene's ``geo`` block (docs/MFD_MAIN_SCREEN.md 7.3), or None when the map
    part is not in use (the camera's part alone is what the scene's ``lanes`` already
    carry) or the position is synthetic. ``ap``: side roads and the ring rest on an
    approximate position."""
    mp = msg.get("map")
    cen = msg.get("centre")
    if not isinstance(mp, dict) or mp.get("used") is not True or mp.get("synthetic") or not isinstance(cen, dict):
        return None
    xs, ys, cf, src, ss = cen.get("x"), cen.get("y"), cen.get("conf"), cen.get("src"), cen.get("s")
    if not (isinstance(xs, list) and isinstance(ys, list) and isinstance(cf, list) and isinstance(src, str)
            and isinstance(ss, list)) or not (len(xs) == len(ys) == len(cf) == len(src) == len(ss)):
        return None
    v = _num(cen.get("vision_to_m")) or 0.0
    first = src.find("m")
    if first < 0:
        # the centre line ends within the camera's reach (the way on turns off at a junction):
        # no map part of the lane, but the junction's roads still belong to the picture
        if len(xs) < 2 or not (msg.get("branches") or msg.get("ring")):
            return None
        first = len(xs) - 1
    start = max(first - 1, 0)                           # the last point of the camera's part: where the two join
    pts = [(float(xs[i]), float(ys[i]), float(cf[i])) for i in range(start, len(xs)) if ss[i] <= GEO_MAX_M]
    if len(pts) < 2:
        return None
    idx = _thin(pts)
    if len(idx) > GEO_MAX_POINTS:
        idx = idx[:GEO_MAX_POINTS]
    # how far the centre line may steer the picture: while it runs forward (not round a ring, not into a turn)
    steer = pts[0][0]
    ring = msg.get("ring") if isinstance(msg.get("ring"), dict) else None
    ring_in = None
    if ring is not None:
        ring_in = 0.0 if ring.get("on_ring") else _num(ring.get("entry_m"))
    lim = math.tan(math.radians(GEO_STEER_DEG))
    for i in range(1, len(pts)):
        dx, dy = pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]
        if dx <= 0.5 or abs(dy) > lim * dx or (ring_in is not None and pts[i][0] > ring_in + 1.0):
            break
        steer = pts[i][0]
    out: Dict[str, Any] = {"v": round(v, 1), "w": _r(msg.get("lane_width_m"), 1) or 3.4,
                           "p": [[round(pts[i][0], 1), round(pts[i][1], 1), int(max(0.0, min(0.99, pts[i][2])) * 10)]
                                 for i in idx],
                           "s": round(steer, 1)}
    end = _END_CODE.get(cen.get("end") or "")
    if end and len(idx) == len(_thin(pts)):
        out["end"] = end
    br = []
    for b in msg.get("branches") or ():
        bp = b.get("pts") if isinstance(b, dict) else None
        if not isinstance(bp, list) or len(bp) < 2:
            continue
        try:
            d0 = math.hypot(float(bp[0][0]), float(bp[0][1]))
            flat = [round(float(c), 1) for pt in bp[:6] for c in pt[:2]]
        except (TypeError, ValueError, IndexError):
            continue
        minor = bool(b.get("minor"))
        if d0 > (GEO_MINOR_M if minor else GEO_BRANCH_M) or float(bp[0][0]) < -8.0:
            continue
        rank = 3 if minor else _CLASS_RANK.get(str(b.get("class") or "").replace("_link", ""), 2)
        lanes = _num(b.get("lanes"))
        width = 3.5 if minor else max(5.5, min(14.0, (lanes or 2) * 3.0))
        flags = ("x" if b.get("exit") else "") + ("e" if b.get("entry") else "") + ("m" if minor else "") \
            + ("r" if b.get("ring") else "") + ("w" if b.get("way") else "")
        br.append((d0, [flags, rank, round(width, 1), flat]))
    br.sort(key=lambda t: t[0])
    if br:
        out["br"] = [b for _, b in br[:GEO_BRANCH_MAX]]
    if ring is not None and isinstance(ring.get("centre"), list) and len(ring["centre"]) == 2:
        r = _num(ring.get("radius_m"))
        cx, cy = _num(ring["centre"][0]), _num(ring["centre"][1])
        if r and cx is not None and cy is not None and math.hypot(cx, cy) <= GEO_BRANCH_M + r:
            lanes = _num(ring.get("lanes")) or 1
            out["ring"] = [round(cx, 1), round(cy, 1), round(r, 1), round(max(4.5, min(9.0, lanes * 4.0)), 1),
                           1 if ring.get("on_ring") else 0]
    way = msg.get("way")
    if isinstance(way, dict) and way.get("source") == "intent":
        out["way"] = "i"
    pos = msg.get("position")
    if isinstance(pos, dict) and pos.get("approximate") and (out.get("br") or out.get("ring")):
        out["ap"] = True
    return out


TURN_ARROWS_AHEAD_M = 120.0    # painted turn arrows are shown in the lane boxes from this far before their stretch


def road_lanes_to_scene(rd: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """``view.road.lanes`` -> scene ``map.lanes`` (the lane boxes): n / fwd / back counts, ``ego`` the lit lane
    (from the left), ``lk`` the likely lane when none is lit, ``p`` the probability per own lane, ``c`` the
    confidence of the lit lane, ``onc_left``; near a junction with painted arrows ``tl`` (per own lane a string
    of l / t / r / u / m, "" = no arrow) and ``tp`` (1 per lane that leads where the most likely path goes)."""
    ln = rd.get("lanes") if isinstance(rd, dict) else None
    if not isinstance(ln, dict) or _num(ln.get("forward")) is None:
        return None
    fwd = int(ln["forward"])
    out = _drop_none({"n": _r(ln.get("count")), "fwd": fwd, "back": _r(ln.get("backward")),
                      "ego": _r(ln.get("ego_index")), "lk": _r(ln.get("likely_index")),
                      "src": ln.get("count_source") or None, "c": _r(ln.get("confidence"), 2)})
    if isinstance(ln.get("oncoming_left"), bool):
        out["onc_left"] = ln["oncoming_left"]
    pr = ln.get("probs")
    if (ln.get("probs_from") == "left" and isinstance(pr, list) and len(pr) == fwd
            and all(isinstance(v, (int, float)) for v in pr)):
        out["p"] = [round(float(v), 2) for v in pr]
    tn = ln.get("turn")
    if isinstance(tn, dict) and isinstance(tn.get("arrows"), list) and len(tn["arrows"]) == fwd \
            and all(isinstance(a, str) for a in tn["arrows"]):
        d = _num(tn.get("dist_m"))
        if tn.get("here") or (d is not None and d <= TURN_ARROWS_AHEAD_M):
            out["tl"] = [a[:4] for a in tn["arrows"]]
            path = tn.get("path")
            if isinstance(path, list) and len(path) == fwd:
                out["tp"] = [1 if v else 0 for v in path]
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


class SyntheticTaint:
    """Is mapd's position (still) derived from a synthetic drive?

    mapd keeps dead reckoning on the last position for up to 120 s (vision
    odometry) or 900 s (vehicle motion) after the fixes stop, and the
    position input API v1 has no reset. So when a synthetic drive ends (stop,
    session expiry, the real car moves) mapd may go on serving the synthetic
    place, matched road and map limit while ``input.gnss.source`` already says
    ``headunit``. Fed with every ``map`` message: tainted once the receiver
    tag was ``synthetic``; clean again when mapd has no position
    (``no_source`` / ``unknown``) or a non-synthetic fix is in use (``good``
    with a real fix as the newest gnss message). Survives WebSocket reconnects
    (mapd keeps its state across them); a backend restart forgets it.
    """

    def __init__(self) -> None:
        self.tainted = False

    def update(self, msg: Dict[str, Any]) -> bool:
        state = msg.get("state")
        inp = msg.get("input")
        g = inp.get("gnss") if isinstance(inp, dict) else None
        src = g.get("source") if isinstance(g, dict) else None
        fix = g.get("fix") if isinstance(g, dict) else None
        if src == "synthetic":
            self.tainted = True
        elif state in ("no_source", "unknown"):
            self.tainted = False
        elif state == "good" and isinstance(g, dict) and fix not in (None, "none"):
            self.tainted = False
        return self.tainted


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


def _line_to_scene(xs: Any, ln: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ys = ln.get("y")
    if not isinstance(ys, list):
        return None
    pts = [(x, y) for x, y in zip(xs, ys) if _num(y) is not None]
    if len(pts) < 2:
        return None
    out = resample([p[0] for p in pts], [p[1] for p in pts])
    if out is None:
        return None
    res: Dict[str, Any] = {"y": out}
    p = _num(ln.get("conf"))
    if p is not None:
        res["prob"] = round(min(1.0, max(TRACKED_MIN_PROB, p)), 2)
    a = _num(ln.get("alpha"))
    if a is not None and a < 1.0:
        res["f"] = round(a, 2)
    return res


def view_to_scene(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """roadsense ``view`` topic (2.0 display channel) -> scene ``vw``: what the screen draws.

    t      the frame time of the view message in ms (roadsense CLOCK_MONOTONIC): the screen spaces the samples
           by it, so the transport's jitter (WebSocket, publish cap, ZMQ poll, Wi-Fi) does not move the picture
    lanes  4 x {"y", "f"} (lane:0..3, f = fade 0..1, only while < 1) or null; edges 2 x the same; path {"y"}
    lead   {"id", "d", "y", "v" (relative speed m/s, the screen extrapolates with it), "f"} or null
    light  the ego light: {"id", "state", "d", "y" (car frame, y right), "vx", "vy", "f", "flashing", "q",
           "held", "alt"} or null
    """
    xs = msg.get("x")
    if not isinstance(xs, list):
        return None
    ts = msg.get("ts_mono_ns")
    out: Dict[str, Any] = {"t": int(ts // 1_000_000) if isinstance(ts, int) else None,
                           "lanes": [None] * 4, "edges": [None] * 2}
    for ln in msg.get("lines") or ():
        if not isinstance(ln, dict) or not isinstance(ln.get("id"), str):
            continue
        kind, _, idx = ln["id"].partition(":")
        i = int(idx) if idx.isdigit() else -1
        key = {"lane": "lanes", "edge": "edges"}.get(kind)
        if key is None or not 0 <= i < len(out[key]):
            continue
        out[key][i] = _line_to_scene(xs, ln)
    pth = msg.get("path")
    if isinstance(pth, dict):
        p = _line_to_scene(xs, pth)
        if p is not None:
            out["path"] = {"y": p["y"]}
    out["lead"] = None
    out["light"] = None
    for o in msg.get("objects") or ():
        if not isinstance(o, dict):
            continue
        x, y = _num(o.get("x")), _num(o.get("y"))
        if x is None or y is None:
            continue
        f = _num(o.get("alpha"))
        if o.get("role") == "lead" and out["lead"] is None:
            ld: Dict[str, Any] = {"id": o.get("id"), "d": round(x, 2), "y": round(y, 2),
                                  "v": round(_num(o.get("vx")) or 0.0, 2)}
            if f is not None and f < 1.0:
                ld["f"] = round(f, 2)
            out["lead"] = ld
        elif o.get("role") == "ego" and o.get("kind") == "traffic_light" and out["light"] is None:
            at = o.get("attrs") if isinstance(o.get("attrs"), dict) else {}
            st = at.get("ego_state") or at.get("state")
            if not isinstance(st, str) or not st:
                continue
            lt: Dict[str, Any] = {"id": o.get("id"), "state": st, "d": round(x, 1), "y": round(y, 2),
                                  "vx": round(_num(o.get("vx")) or 0.0, 2), "vy": round(_num(o.get("vy")) or 0.0, 2)}
            if f is not None and f < 1.0:
                lt["f"] = round(f, 2)
            if at.get("ego_flashing"):
                lt["flashing"] = True
            if at.get("amb"):
                lt["q"] = True
            if at.get("held"):
                lt["held"] = True
            alt = at.get("alt")
            if isinstance(alt, dict) and isinstance(alt.get("state"), str):
                side = alt.get("side")
                lt["alt"] = {"s": alt["state"], "side": side[:1] if isinstance(side, str) and side else ""}
            out["light"] = lt
    return out


class SceneBuilder:
    """Collects roadsense messages, builds a scene mapping when one is due.

    Not thread-safe: used by the feed thread only.
    """

    def __init__(self, cfg: SceneFeedConfig):
        self.cfg = cfg
        self.taint = SyntheticTaint()
        self.reset()

    def reset(self) -> None:
        self._road_raw: Optional[str] = None
        self._road_t = 0.0
        self._road_new = False
        self._last_seq: Optional[int] = None
        self._last_pub = -1e9
        self._slow: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
        self._lead: Optional[Tuple[float, Optional[Dict[str, Any]], bool]] = None
        self._lanes: Optional[Tuple[float, Optional[Dict[str, Any]]]] = None
        self._view: Optional[Tuple[float, Dict[str, Any]]] = None
        self._view_new = False
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
        elif mtype == "lead":
            self._lead = (now, lead_to_scene(msg), msg.get("departed") is True)
        elif mtype == "lanes":
            self._lanes = (now, lanes_to_scene(msg))
        elif mtype == "view":
            # the last message of a road frame (roadsense sends road, lead, lanes, view): a scene built now
            # has the lead, the lanes and the road picture (view.road) of the same frame
            self._view, self._view_new = (now, msg), True
        elif mtype == "map":
            self.taint.update(msg)
            self._slow["map_in"] = (now, map_input_to_scene(msg))
            self._slow["map_shape"] = (now, map_shape_to_scene(msg))

    def view_mode(self, now: float) -> bool:
        """roadsense sends the display channel: scenes are built per view message."""
        return self._view is not None and now - self._view[0] <= self.cfg.view_max_age_s

    def time_to_due(self, now: float) -> Optional[float]:
        """Seconds until a pending road frame may be published; None if none."""
        if not (self._view_new if self.view_mode(now) else self._road_new):
            return None
        return max(0.0, self._last_pub + 1.0 / max(0.1, self.cfg.max_hz) - now)

    def build(self, now: float) -> Optional[Dict[str, Any]]:
        """The scene for the newest road frame, or None (not due, nothing new,
        too old, same seq as the last publish, undecodable)."""
        wait = self.time_to_due(now)
        if wait is None or wait > 0.0:
            return None
        vw = None
        if self.view_mode(now):
            # roadsense 2.0: the frame's view message is complete; the 15 kB road message is not decoded
            self._view_new = self._road_new = False
            view = self._view[1]  # type: ignore[index]
            vw = view_to_scene(view)
            if vw is None:
                return None
            seq = view.get("seq")
            scene = {"seq": int(seq) if _num(seq) is not None else 0,
                     "lanes": [None] * 4, "edges": [None] * 2, "lead": None}
            if vw.get("path"):
                scene["path"] = vw["path"]
        else:
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
        # the tracked lead (roadsense >= 2026-11-15) replaces modeld's leads[0]
        # while its topic is fresh, including "no lead" (null); an older
        # roadsense never sends it and the raw leads[0] stays
        if self._lead is not None and now - self._lead[0] <= self.cfg.lead_max_age_s:
            scene["lead"] = self._lead[1]
            scene["lead_src"] = "tracked"
            if self._lead[1] is None and self._lead[2]:
                # the lead was lost after it pulled away; the cue outlives it
                scene["lead_departed"] = True
        # the tracked road geometry (roadsense >= 2026-11-16: presence with
        # hysteresis, filtered lines and edges) replaces the raw lane lines and
        # road edges while its topic is fresh; an older roadsense never sends it
        if (self._lanes is not None and self._lanes[1] is not None
                and now - self._lanes[0] <= self.cfg.lanes_max_age_s):
            scene["lanes"] = self._lanes[1]["lanes"]
            scene["edges"] = self._lanes[1]["edges"]
            scene["lanes_src"] = "tracked"
        ages = {"light": self.cfg.lights_max_age_s, "limit": self.cfg.limit_max_age_s,
                "env": self.cfg.env_max_age_s, "signs": self.cfg.rules_max_age_s,
                "map": self.cfg.map_max_age_s, "map_in": self.cfg.map_max_age_s,
                "map_shape": self.cfg.map_max_age_s}
        for key, (t, val) in self._slow.items():
            if now - t <= ages[key]:
                scene[key] = val
        # mapd's input echo rides inside "map" (only when fused is fresh too)
        map_in = scene.pop("map_in", None)
        if map_in and isinstance(scene.get("map"), dict):
            scene["map"] = dict(scene["map"], **{"in": map_in})
        # mapd's road shape ahead, only while the fused map layer is in use
        shape = scene.pop("map_shape", None)
        if shape and isinstance(scene.get("map"), dict) and scene["map"].get("ok"):
            scene["map"] = dict(scene["map"], shape=shape)
        if self.taint.tainted and isinstance(scene.get("map"), dict):
            scene["map"] = dict(scene["map"], syn=True)
        # the road picture (roadsense 2.0: view.road): the road beyond the camera with side roads and a
        # roundabout (geo), the junction ahead (map.jv) and the lane boxes (map.lanes) all come from the
        # display channel, of the same frame as the lanes they continue; roadsense has decided there what a
        # weak position still allows and marks it approximate. Drawn only while the fused map layer is in
        # use as well (the screen's own gate) and the position is real.
        rd = self._view[1].get("road") if vw is not None else None      # type: ignore[index]
        if isinstance(rd, dict) and isinstance(scene.get("map"), dict):
            mp = dict(scene["map"])
            pos = rd.get("position") if isinstance(rd.get("position"), dict) else {}
            if mp.get("ok") and pos.get("trusted") and not pos.get("synthetic") and not self.taint.tainted:
                jv = junction_to_scene(rd.get("junction"))
                if jv:
                    mp["jv"] = jv
                geo = roadgeom_to_scene(rd)
                if geo:
                    scene["geo"] = geo
            mp["lanes"] = road_lanes_to_scene(rd) if pos.get("trusted") else None
            if pos.get("trusted") and mp.get("ok"):
                mp["tr"] = True      # roadsense's own trust decision (smoothed, with hysteresis): the screen adopts it
            scene["map"] = mp
        if vw is not None:
            scene["vw"] = vw
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
