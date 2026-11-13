"""
Auto-clear of named, recurring DTCs.

The clear services erase *everything* an ECU holds — every stored and pending
code, the freeze frame and (OBD mode 04) the readiness monitors — never one
code. The rule therefore is:

    clear an ECU only when EVERY stored and pending code it reports is on the
    whitelist; a single other code blocks the clear and is surfaced as
    "auto-clear blocked by <code>".

Further gates: master switch (default off), per-entry enable, vehicle speed 0
unless ``speed_policy`` is "anytime", no clear while a scan runs, at most one
clear per ``min_interval_s`` and ``max_per_day`` per ECU. Before every clear the
codes and the freeze frame are read and written to the clear log together with
the driving context.

Detection is a light poll (``poll_s``, default 30 s) of only the ECUs that have
enabled whitelist entries: OBD mode 01 PID 01 (MIL + stored-code count; one
request frame, one response frame), and the code lists only when that count is
non-zero, pending codes are a trigger, or every ``full_every``-th poll.

:func:`decide` is the pure rule; :class:`AutoClearMonitor` runs it on the
engine's job queue.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generator, Iterable, List, Optional, Tuple

from . import dtc as dtcmod
from . import services
from .engine import DiagEngine, Request, Result

logger = logging.getLogger(__name__)

TRIGGERS = ("stored", "pending")
SPEED_POLICIES = ("stopped", "anytime")
DAY_S = 86400.0


@dataclass
class AutoClearConfig:
    enabled: bool = False
    codes: List[Dict[str, Any]] = field(default_factory=list)   # {"ecu", "code", "enabled"}
    trigger: str = "stored"         # "stored": act on confirmed codes; "pending": also on pending
    speed_policy: str = "stopped"   # "stopped" (speed 0) | "anytime"
    min_interval_s: float = 600.0
    max_per_day: int = 6
    poll_s: float = 30.0
    full_every: int = 10            # every N-th poll reads the code lists regardless of the count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "codes": [dict(c) for c in self.codes],
            "trigger": self.trigger,
            "speed_policy": self.speed_policy,
            "min_interval_s": self.min_interval_s,
            "max_per_day": self.max_per_day,
            "poll_s": self.poll_s,
        }

    def whitelist(self, ecu: str) -> List[str]:
        return [c["code"] for c in self.codes if c.get("ecu") == ecu and c.get("enabled", True)]

    def ecus(self) -> List[str]:
        out: List[str] = []
        for c in self.codes:
            if c.get("enabled", True) and c.get("ecu") not in out:
                out.append(c["ecu"])
        return out


def validate_config(params: Dict[str, Any], known_ecus: Iterable[str],
                    base: Optional[AutoClearConfig] = None) -> AutoClearConfig:
    """Build a config from request parameters (only the given keys change).
    Raises ``ValueError`` with a message fit for an HTTP 400."""
    cfg = AutoClearConfig(**(base.__dict__ if base is not None else {}))
    cfg.codes = [dict(c) for c in cfg.codes]
    known = set(known_ecus)
    if "enabled" in params:
        if not isinstance(params["enabled"], bool):
            raise ValueError("enabled must be a boolean")
        cfg.enabled = params["enabled"]
    if "codes" in params:
        raw = params["codes"]
        if not isinstance(raw, list) or len(raw) > 64:
            raise ValueError("codes must be a list of at most 64 {ecu, code} entries")
        seen = set()
        codes = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("each codes entry must be an object {ecu, code}")
            ecu = item.get("ecu")
            code = str(item.get("code", "")).strip().upper()
            if ecu not in known:
                raise ValueError("unknown ECU %r" % (ecu,))
            if not dtcmod.is_code(code):
                raise ValueError("not a DTC: %r" % (item.get("code"),))
            enabled = item.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ValueError("codes[].enabled must be a boolean")
            if (ecu, code) in seen:
                continue
            seen.add((ecu, code))
            codes.append({"ecu": ecu, "code": code, "enabled": enabled})
        cfg.codes = codes
    if "trigger" in params:
        if params["trigger"] not in TRIGGERS:
            raise ValueError("trigger must be one of %s" % ", ".join(TRIGGERS))
        cfg.trigger = params["trigger"]
    if "speed_policy" in params:
        if params["speed_policy"] not in SPEED_POLICIES:
            raise ValueError("speed_policy must be one of %s" % ", ".join(SPEED_POLICIES))
        cfg.speed_policy = params["speed_policy"]
    if "min_interval_s" in params:
        cfg.min_interval_s = _num(params["min_interval_s"], "min_interval_s", 60, 86400)
    if "max_per_day" in params:
        cfg.max_per_day = int(_num(params["max_per_day"], "max_per_day", 0, 48))
    if "poll_s" in params:
        cfg.poll_s = _num(params["poll_s"], "poll_s", 10, 3600)
    return cfg


def _num(value: Any, key: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a number" % key)
    if not lo <= value <= hi:
        raise ValueError("%s must be within %s..%s" % (key, lo, hi))
    return float(value)


@dataclass
class Decision:
    action: str                     # "none" | "clear" | "blocked" | "wait"
    reason: str = ""
    codes: List[str] = field(default_factory=list)          # whitelisted codes present
    blocked_by: List[str] = field(default_factory=list)     # other codes present


def decide(cfg: AutoClearConfig, ecu: str, stored: Iterable[str], pending: Iterable[str],
           now: float, past_clears: Iterable[float], scanning: bool = False) -> Decision:
    """The auto-clear rule for one ECU (pure).

    ``past_clears`` are the wall-clock times of this ECU's earlier automatic
    clears. The vehicle-speed gate is not decided here: it needs a fresh
    reading and is applied by the clear sequence itself.
    """
    if not cfg.enabled:
        return Decision("none", "auto-clear is off")
    white = set(cfg.whitelist(ecu))
    if not white:
        return Decision("none", "no enabled whitelist entry for this ECU")
    stored_s, pending_s = set(stored), set(pending)
    present = stored_s | pending_s
    trigger = stored_s if cfg.trigger == "stored" else present
    hits = sorted(trigger & white)
    if not hits:
        return Decision("none", "no whitelisted code present")
    others = sorted(present - white)
    if others:
        return Decision("blocked", "auto-clear blocked by %s" % ", ".join(others),
                        codes=hits, blocked_by=others)
    if scanning:
        return Decision("wait", "a diagnostics scan is running", codes=hits)
    clears = sorted(past_clears)
    if clears and now - clears[-1] < cfg.min_interval_s:
        left = cfg.min_interval_s - (now - clears[-1])
        return Decision("wait", "rate limit: next clear in %d s" % left, codes=hits)
    if sum(1 for t in clears if now - t < DAY_S) >= cfg.max_per_day:
        return Decision("wait", "rate limit: %d clears in the last 24 h" % cfg.max_per_day, codes=hits)
    return Decision("clear", "all present codes are whitelisted", codes=sorted(present & white))


class AutoClearMonitor:
    """Polls the whitelisted ECUs and clears them when the rule allows."""

    def __init__(
        self,
        engine: DiagEngine,
        config: Optional[AutoClearConfig] = None,
        save: Optional[Callable[[Dict[str, Any]], None]] = None,
        log: Optional[Callable[[Dict[str, Any]], None]] = None,
        context: Optional[Callable[[], Dict[str, Any]]] = None,
        past_clears: Optional[Iterable[Dict[str, Any]]] = None,
        active: Optional[Callable[[], bool]] = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.engine = engine
        self.config = config or AutoClearConfig()
        self._save = save
        self._log = log
        self._context = context
        self._active = active           # False: vehicle bus asleep -> no polling
        self._clock = clock
        self._wall = wall
        self._next_poll = 0.0
        self._queued = False
        self._polls = 0
        self._clear_times: Dict[str, List[float]] = {}
        for rec in past_clears or []:
            if rec.get("origin") == "autoclear" and rec.get("ecu") and rec.get("ts"):
                self._clear_times.setdefault(rec["ecu"], []).append(float(rec.get("ts", 0)))
        self.counters = {"checks": 0, "clears": 0, "blocked": 0, "deferred": 0, "refused": 0}
        self.blocked: Dict[str, Dict[str, Any]] = {}
        self.pending_reason: Dict[str, str] = {}
        self.last_check_ts: Optional[float] = None
        self.last_action: Optional[Dict[str, Any]] = None
        self.recent: List[Dict[str, Any]] = []
        self._export()

    # ── configuration ────────────────────────────────────────────────────

    def set_config(self, params: Dict[str, Any]) -> Tuple[bool, str]:
        try:
            cfg = validate_config(params, self.engine.ecus.keys(), self.config)
        except ValueError as exc:
            return False, str(exc)
        self.config = cfg
        for key in list(self.blocked):
            if key not in cfg.ecus():
                del self.blocked[key]
        if self._save is not None:
            try:
                self._save(cfg.to_dict())
            except Exception:
                logger.exception("could not persist the auto-clear settings")
        self._next_poll = 0.0
        self._export()
        return True, "auto-clear %s, %d code(s)" % ("on" if cfg.enabled else "off", len(cfg.codes))

    # ── polling ──────────────────────────────────────────────────────────

    def tick(self, now: Optional[float] = None) -> None:
        now = self._clock() if now is None else now
        cfg = self.config
        if not cfg.enabled or not cfg.ecus() or self._queued:
            return
        if now < self._next_poll or self.engine.busy:
            return
        if self._active is not None and not self._active():
            # Ignition off: the bus is asleep, a request would go unacknowledged.
            self._next_poll = now + min(cfg.poll_s, 5.0)
            return
        self._next_poll = now + cfg.poll_s
        self._queued = self.engine.enqueue_job("autoclear", self._job())

    def _job(self) -> Generator[Request, Result, None]:
        try:
            self._polls += 1
            for key in self.config.ecus():
                ecu = self.engine.ecus.get(key)
                if ecu is None or not ecu.reachable:
                    continue
                yield from self._check_ecu(ecu)
            self.last_check_ts = self._wall()
        finally:
            self._queued = False
            self._export()

    def _check_ecu(self, ecu: Any) -> Generator[Request, Result, None]:
        cfg = self.config
        eng = self.engine
        key = ecu.key
        res = eng.results[key]
        self.counters["checks"] += 1

        full = cfg.trigger == "pending" or self._polls % max(1, cfg.full_every) == 1 \
            or key in self.blocked or key in self.pending_reason
        if ecu.obd:
            r = yield Request(ecu, services.obd_monitor_status_request(), note="autoclear poll")
            st = services.obd_parse_monitor_status(r.payload) if r.positive else None
            if st is None:
                return                                  # ECU asleep / IG off: nothing to do
            res.update(mil=st["mil"], dtc_count=st["dtc_count"], responded=True)
            if st["dtc_count"] == 0 and not st["mil"] and not full:
                return
        codes: Dict[str, Dict[str, Any]] = {}
        answered = yield from eng._read_codes(ecu, ("stored", "pending"), codes, res)
        if not answered:
            return
        res["dtcs"] = eng._annotate(ecu, codes)
        res["status"] = "ok"
        res["responded"] = True
        res["last_scan_ts"] = self._wall()
        stored = [c for c, d in codes.items() if "stored" in d["status"]]
        pending = [c for c, d in codes.items() if "pending" in d["status"]]

        now = self._wall()
        d = decide(cfg, key, stored, pending, now, self._clear_times.get(key, []),
                   scanning=eng.scanning)
        if d.action == "blocked":
            if key not in self.blocked:
                self.counters["blocked"] += 1
            self.blocked[key] = {"by": d.blocked_by, "codes": d.codes, "reason": d.reason,
                                 "since": self.blocked.get(key, {}).get("since", now)}
            self.pending_reason.pop(key, None)
            return
        self.blocked.pop(key, None)
        if d.action == "wait":
            self.pending_reason[key] = d.reason
            return
        self.pending_reason.pop(key, None)
        if d.action != "clear":
            return

        record = yield from eng.clear_sequence(ecu, allow_moving=cfg.speed_policy == "anytime",
                                               origin="autoclear")
        record["whitelisted"] = d.codes
        record["trigger"] = cfg.trigger
        if self._context is not None:
            try:
                record["context"] = self._context()
            except Exception:
                logger.exception("auto-clear context failed")
        action = {"ts": record["ts"], "ecu": key, "codes": d.codes, "ok": record["ok"],
                  "result": record["result"]}
        if record["result"].startswith("refused: ") and not record["ok"] and record.get("after") is None:
            # Speed gate (or a silent ECU): nothing was sent, nothing is logged
            # as a clear; try again on the next poll.
            self.counters["deferred"] += 1
            self.pending_reason[key] = record["result"]
            action["deferred"] = True
        else:
            if record["ok"]:
                self.counters["clears"] += 1
            else:
                self.counters["refused"] += 1
            # A refused attempt counts against the rate limit too: an ECU that
            # says no is not asked again every 30 s.
            self._clear_times.setdefault(key, []).append(record["ts"])
            res["last_clear"] = {"ts": record["ts"], "ok": record["ok"], "result": record["result"],
                                 "cleared": d.codes if record["ok"] else [],
                                 "remaining": record.get("after"), "origin": "autoclear"}
            if self._log is not None:
                try:
                    self._log(record)
                except Exception:
                    logger.exception("auto-clear log failed")
        self.last_action = action
        self.recent.append(action)
        del self.recent[:-10]

    # ── state ────────────────────────────────────────────────────────────

    def state(self) -> Dict[str, Any]:
        now = self._wall()
        out = self.config.to_dict()
        out.update(
            last_check_ts=self.last_check_ts,
            last_action=self.last_action,
            recent=list(self.recent),
            blocked={k: dict(v) for k, v in self.blocked.items()},
            waiting=dict(self.pending_reason),
            counters=dict(self.counters,
                          today=sum(1 for ts in self._clear_times.values() for t in ts if now - t < DAY_S)),
        )
        return out

    def _export(self) -> None:
        self.engine.extra_state["autoclear"] = self.state()
        self.engine.mark_dirty()
