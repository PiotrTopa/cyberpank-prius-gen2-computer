"""
Diagnostics engine — scans the NHW20's ECUs one request at a time through the
gateway's solicited path.

Threading: everything here runs on the backend engine thread (``tick`` and
``on_gateway_message`` are called from the engine loop). The only cross-thread
entry point is :meth:`DiagEngine.on_bus_frame` (panda reader thread), which
only appends to a deque that ``tick`` drains.

Transport (gateway firmware 3.2.9, see docs/TOYOTA_DIAGNOSTICS.md section 8):

* ``gw`` — normal-addressed ECUs. One ``req`` with ``"isotp": true``; the
  gateway reassembles multi-frame responses and sends the flow control itself.
* ``host`` — sub-addressed ECUs behind 0x750/0x758 (and any ECU forced to this
  mode). The firmware's ISO-TP only knows normal addressing, so the request is
  sent raw (``"isotp": false``, the gateway returns the first matching frame)
  and reassembly happens here: after a first frame the engine sends flow
  control with BlockSize 1 as another raw ``req``, which returns exactly one
  consecutive frame, and repeats. Frames the passive panda listener sees are
  accepted as well (deduplicated), so an ECU that ignores BlockSize still
  completes when the panda is connected.

Scheduling: a job is a generator that yields :class:`Request` objects and
receives a :class:`Result` for each. Exactly one request is outstanding at any
time; new requests are spaced by ``min_gap_s`` and a frames-per-second budget.
An ECU that does not answer its first request is marked ``no_answer`` and the
rest of its job is skipped.

Safety: every request passes ``services.check_request``. Only the clear
services (0x04 / 0x14) write anything, and only from a job created with an
explicit confirmation (``clear`` / auto-clear), after the vehicle-speed gate.
"""

from __future__ import annotations

import collections
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Generator, Iterable, List, Optional, Tuple

from . import dictionary, isotp, services
from .ecus import ECUS, Ecu, SCAN_ORDER

logger = logging.getLogger(__name__)

DEFAULT_KINDS = ("stored", "pending", "freeze")
ALL_KINDS = ("stored", "pending", "freeze", "status", "ident", "probe")


# ── request / result ─────────────────────────────────────────────────────────

@dataclass
class Request:
    """One diagnostic request a job wants performed."""
    ecu: Ecu
    payload: bytes
    allow_clear: bool = False
    note: str = ""


@dataclass
class Result:
    """Outcome of one request.

    ``ok`` means a response belonging to the request arrived, positive or
    negative; ``positive`` narrows it. ``error`` names why there is no usable
    response: ``timeout`` (ECU silent), ``gateway:<ERR>``, ``no_gateway_reply``,
    ``isotp:<why>``, ``pending`` (0x78 and the final answer never came),
    ``blocked:<why>`` (refused locally), ``unexpected`` (a reply that does not
    match the request).
    """
    ok: bool = False
    payload: bytes = b""
    error: Optional[str] = None
    nrc: Optional[int] = None
    partial: bytes = b""
    total: int = 0                  # announced length of a truncated multi-frame response
    ms: float = 0.0

    @property
    def positive(self) -> bool:
        return self.ok and self.nrc is None

    @property
    def silent(self) -> bool:
        """The ECU did not answer at all."""
        return self.error in ("timeout", "no_gateway_reply") or (
            self.error or "").startswith("gateway:")


@dataclass
class DiagConfig:
    gw_timeout_ms: int = 300        # gateway job timeout, normal addressing (>= 300 implies ISO-TP in fw)
    raw_timeout_ms: int = 250       # gateway job timeout, raw frames (must stay < 300)
    host_margin_s: float = 0.7      # extra wait for the gateway's own reply line
    pending_wait_s: float = 5.0     # after NRC 0x78 (response pending)
    min_gap_s: float = 0.05         # between the end of one request and the next
    max_frames_per_s: int = 20      # transmit budget, flow control included
    block_size: int = 1             # flow-control BS in host mode
    st_min_ms: int = 0
    max_trace: int = 400            # exchanges kept for the report
    publish_min_s: float = 0.25
    job_queue_max: int = 64


@dataclass
class _Exchange:
    req: Request
    tx_frame: bytes
    started: float
    deadline: float
    mode: str                                   # "gw" | "host"
    reasm: Optional[isotp.Reassembler] = None
    waiting_pending: bool = False
    gw_outstanding: int = 0
    frames_tx: int = 0
    last_cf_t: float = 0.0
    free_run: bool = False                      # ECU ignores BlockSize (seen on the bus tap)
    seen: List[Tuple[bytes, str, float]] = field(default_factory=list)


@dataclass
class _Job:
    name: str
    gen: Generator[Request, Result, Any]
    ecu_key: Optional[str] = None
    scan: bool = False              # counts towards the scan progress
    started: bool = False
    on_done: Optional[Callable[[], None]] = None


# ── engine ───────────────────────────────────────────────────────────────────

class DiagEngine:
    """All-ECU diagnostics over the gateway's solicited CAN path."""

    def __init__(
        self,
        send_can: Callable[[Dict[str, Any]], bool],
        publish: Optional[Callable[[Dict[str, Any]], None]] = None,
        vehicle_speed: Optional[Callable[[], Optional[float]]] = None,
        ecus: Optional[Dict[str, Ecu]] = None,
        config: Optional[DiagConfig] = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        on_scan_done: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
    ) -> None:
        self._send_can = send_can
        self._publish_cb = publish
        self._vehicle_speed = vehicle_speed
        self.ecus: Dict[str, Ecu] = dict(ecus if ecus is not None else ECUS)
        self.config = config or DiagConfig()
        self._clock = clock
        self._wall = wall
        self._on_scan_done = on_scan_done

        self._jobs: Deque[_Job] = collections.deque()
        self._job: Optional[_Job] = None
        self._pending_req: Optional[Request] = None
        self._xchg: Optional[_Exchange] = None
        self._next_tx = 0.0
        self._tx_times: Deque[float] = collections.deque()
        self._bus_frames: Deque[Tuple[int, bytes, float]] = collections.deque(maxlen=512)
        self._bus_seen = 0

        self.results: Dict[str, Dict[str, Any]] = {k: self._blank(e) for k, e in self.ecus.items()}
        self.trace: Deque[Dict[str, Any]] = collections.deque(maxlen=self.config.max_trace)
        self.vehicle: Dict[str, Any] = {}
        self.scan: Dict[str, Any] = {"state": "idle", "op": None, "total": 0, "done": 0,
                                     "current": None, "started_ts": None, "finished_ts": None,
                                     "report_file": None, "error": None, "kinds": []}
        self.last_read: Optional[Dict[str, Any]] = None
        self.stats = {"requests": 0, "frames_tx": 0, "responses": 0, "timeouts": 0,
                      "negative": 0, "blocked": 0, "bus_frames": 0}
        self._dirty = True
        self._last_publish = 0.0
        self.extra_state: Dict[str, Any] = {}       # e.g. {"autoclear": {...}} from the monitor

    # ── public API ───────────────────────────────────────────────────────

    @property
    def busy(self) -> bool:
        return self._job is not None or bool(self._jobs)

    @property
    def scanning(self) -> bool:
        return self.scan["state"] == "running"

    def start_scan(self, ecus: Optional[Iterable[str]] = None,
                   kinds: Optional[Iterable[str]] = None, op: str = "scan") -> Tuple[bool, str]:
        """Queue a scan of ``ecus`` (default: all, in registry order)."""
        if self.scanning:
            return False, "a scan is already running"
        keys = self._resolve_ecus(ecus)
        kinds_t = tuple(kinds) if kinds else DEFAULT_KINDS
        bad = [k for k in kinds_t if k not in ALL_KINDS]
        if bad:
            return False, "unknown kind(s): %s" % ", ".join(bad)
        if not keys:
            return False, "no ECU selected"
        self.scan.update(state="running", op=op, total=len(keys), done=0, current=None,
                         started_ts=self._wall(), finished_ts=None, report_file=None,
                         error=None, kinds=list(kinds_t))
        for i, key in enumerate(keys):
            ecu = self.ecus[key]
            last = i == len(keys) - 1
            self._enqueue(_Job("%s:%s" % (op, key), self._job_scan(ecu, kinds_t), key, scan=True,
                               on_done=self._finish_scan if last else None))
        self._mark()
        return True, "scan of %d ECU(s) queued" % len(keys)

    def start_identify(self, ecus: Optional[Iterable[str]] = None) -> Tuple[bool, str]:
        return self.start_scan(ecus, ("ident",), op="identify")

    def start_read(self, ecu_key: str, item: str) -> Tuple[bool, str]:
        """Queue one read-only request (``item`` is a hex string such as
        ``21C3`` or ``0101``, or a named item of the ECU's data list)."""
        ecu = self.ecus.get(ecu_key)
        if ecu is None:
            return False, "unknown ECU %r" % (ecu_key,)
        named = ecu.items.get(item)
        try:
            payload = bytes(named) if named is not None else services.parse_hex_request(item)
            services.check_request(payload)
            isotp.encode_single(payload, ecu.sub_addr)
        except (ValueError, services.DiagSafetyError) as exc:
            return False, str(exc)
        if not self._enqueue(_Job("read:%s" % ecu_key, self._job_read(ecu, item, payload), ecu_key)):
            return False, "diagnostics job queue is full"
        return True, "read queued"

    def start_clear(self, ecu_key: str, confirm: bool = False,
                    allow_moving: bool = False) -> Tuple[bool, str]:
        """Queue a DTC clear of one ECU. ``confirm`` must be ``True``."""
        if confirm is not True:
            return False, "clear needs confirm=true"
        ecu = self.ecus.get(ecu_key)
        if ecu is None:
            return False, "unknown ECU %r" % (ecu_key,)
        if not ecu.clear_requests():
            return False, "no clear service known for %s" % ecu.name
        if self.scanning:
            return False, "a scan is running"
        if not self._enqueue(_Job("clear:%s" % ecu_key,
                                  self._job_clear(ecu, allow_moving=allow_moving), ecu_key)):
            return False, "diagnostics job queue is full"
        return True, "clear queued"

    def enqueue_job(self, name: str, gen: Generator[Request, Result, Any],
                    ecu_key: Optional[str] = None) -> bool:
        """Queue a custom job (used by the auto-clear monitor)."""
        return self._enqueue(_Job(name, gen, ecu_key))

    def cancel(self) -> None:
        """Drop queued jobs and abandon the running one (nothing is sent to
        the bus; a request already on the wire simply goes unanswered here)."""
        self._jobs.clear()
        if self._job is not None:
            self._job.gen.close()
            if self._job.ecu_key:
                r = self.results[self._job.ecu_key]
                if r["status"] == "scanning":
                    r["status"] = "unknown"
        self._job = None
        self._pending_req = None
        self._xchg = None
        if self.scanning:
            self.scan.update(state="cancelled", finished_ts=self._wall(), current=None)
        self._mark()

    # ── inputs ───────────────────────────────────────────────────────────

    def on_gateway_message(self, data: Dict[str, Any]) -> bool:
        """Offer one gateway CAN-channel payload. Returns ``True`` when it was
        the reply to the outstanding request (the caller then skips its own
        decoding)."""
        x = self._xchg
        if x is None or data.get("a") != "resp" or "slot" in data:
            return False
        rx = _as_int(data.get("i"))
        req_id = _as_int(data.get("req"))
        ecu = x.req.ecu
        err = data.get("err")
        if err:
            if req_id != ecu.tx_id and rx != ecu.rx_id:
                return False
            x.gw_outstanding = max(0, x.gw_outstanding - 1)
            self._gateway_error(str(err))
            return True
        if rx != ecu.rx_id:
            return False
        raw = data.get("d")
        if not isinstance(raw, list):
            return False
        try:
            payload = bytes(int(b) & 0xFF for b in raw)
        except (TypeError, ValueError):
            return False
        x.gw_outstanding = max(0, x.gw_outstanding - 1)
        if x.mode == "gw":
            self._response(payload)
        else:
            self._raw_frame(payload, "gw", self._clock())
        return True

    def on_bus_frame(self, can_id: int, data: bytes, t_mono: Optional[float] = None) -> None:
        """Passive bus tap (panda reader thread): diagnostic-range frames only."""
        if 0x700 <= can_id <= 0x7FF:
            self._bus_frames.append((can_id, bytes(data), t_mono if t_mono is not None else self._clock()))

    def tick(self, now: Optional[float] = None) -> None:
        now = self._clock() if now is None else now
        self._drain_bus(now)
        x = self._xchg
        if x is not None:
            if now >= x.deadline:
                self._timeout(now)
        if self._xchg is None:
            if self._job is None and self._jobs:
                self._begin_job(self._jobs.popleft())
            if self._job is not None and not self._job.started:
                self._advance(None)
            if self._pending_req is not None and self._xchg is None and self._may_transmit(now):
                req, self._pending_req = self._pending_req, None
                self._start_exchange(req, now)
        self._publish(now)

    # ── state / report ───────────────────────────────────────────────────

    def state(self) -> Dict[str, Any]:
        """JSON-friendly snapshot for ``diagnostics.*``."""
        out = {
            "ecus": {k: _public(r) for k, r in self.results.items()},
            "scan": dict(self.scan),
            "vehicle": dict(self.vehicle),
            "last_read": self.last_read,
            "stats": dict(self.stats),
        }
        out.update(self.extra_state)
        return out

    def report(self) -> Dict[str, Any]:
        """Full scan report (state plus the request/response trace)."""
        return {
            "format": "prius-diag-report/1",
            "ts": self._wall(),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self._wall())),
            "vin": self.vehicle.get("vin"),
            "vehicle": dict(self.vehicle),
            "scan": dict(self.scan),
            "ecus": {k: dict(r) for k, r in self.results.items()},
            "trace": list(self.trace),
            "stats": dict(self.stats),
            "dictionary": dictionary.load().get("meta", {}),
        }

    def all_codes(self) -> List[Tuple[str, str, List[str]]]:
        """``[(code, ecu_legacy_name, status)]`` over every scanned ECU."""
        out = []
        for key, r in self.results.items():
            for d in r.get("dtcs", []):
                out.append((d["code"], self.ecus[key].legacy_name, list(d.get("status", []))))
        return out

    # ── job plumbing ─────────────────────────────────────────────────────

    def _resolve_ecus(self, ecus: Optional[Iterable[str]]) -> List[str]:
        if not ecus:
            return [k for k in SCAN_ORDER if k in self.ecus] + [
                k for k in self.ecus if k not in SCAN_ORDER]
        out = []
        for k in ecus:
            if k in self.ecus and k not in out:
                out.append(k)
        return out

    def _enqueue(self, job: _Job) -> bool:
        if len(self._jobs) >= self.config.job_queue_max:
            job.gen.close()
            return False
        self._jobs.append(job)
        return True

    def _begin_job(self, job: _Job) -> None:
        self._job = job
        if job.scan:
            self.scan["current"] = job.ecu_key
        self._mark()

    def _advance(self, result: Optional[Result]) -> None:
        job = self._job
        if job is None:
            return
        try:
            if not job.started:
                job.started = True
                req = next(job.gen)
            else:
                req = job.gen.send(result if result is not None else Result(error="internal"))
        except StopIteration:
            self._end_job()
            return
        except Exception:
            logger.exception("diagnostics job %s failed", job.name)
            if job.ecu_key:
                r = self.results[job.ecu_key]
                r["status"] = "error"
                r["error"] = "internal error (see backend log)"
            self._end_job()
            return
        self._pending_req = req

    def _end_job(self) -> None:
        job, self._job = self._job, None
        self._pending_req = None
        if job is None:
            return
        if job.scan and self.scanning:
            self.scan["done"] += 1
        if job.on_done is not None:
            try:
                job.on_done()
            except Exception:
                logger.exception("diagnostics job %s completion hook failed", job.name)
        self._mark()

    def _finish_scan(self) -> None:
        self.scan.update(state="done", finished_ts=self._wall(), current=None)
        if self._on_scan_done is not None:
            try:
                self.scan["report_file"] = self._on_scan_done(self.report())
            except Exception as exc:
                logger.exception("scan report export failed")
                self.scan["error"] = "report export failed: %s" % exc

    # ── transmit side ────────────────────────────────────────────────────

    def _may_transmit(self, now: float) -> bool:
        if now < self._next_tx:
            return False
        while self._tx_times and now - self._tx_times[0] > 1.0:
            self._tx_times.popleft()
        return len(self._tx_times) < self.config.max_frames_per_s

    def _send_frame(self, ecu: Ecu, frame: bytes, timeout_ms: int, use_isotp: bool) -> bool:
        ok = bool(self._send_can({
            "a": "req",
            "i": "0x%03X" % ecu.tx_id,
            "d": list(frame),
            "r": ["0x%03X" % ecu.rx_id],
            "t": int(timeout_ms),
            "isotp": bool(use_isotp),
        }))
        if ok:
            self._tx_times.append(self._clock())
            self.stats["frames_tx"] += 1
        return ok

    def _start_exchange(self, req: Request, now: float) -> None:
        cfg = self.config
        ecu = req.ecu
        try:
            services.check_request(req.payload, allow_clear=req.allow_clear)
            frame = isotp.encode_single(req.payload, ecu.sub_addr)
        except (services.DiagSafetyError, isotp.IsoTpError) as exc:
            self.stats["blocked"] += 1
            self._trace(req, None, "blocked: %s" % exc, 0.0)
            self._advance(Result(error="blocked:%s" % exc))
            return
        mode = ecu.transport
        if mode == "gw":
            timeout_ms = cfg.gw_timeout_ms
            deadline = now + 2 * timeout_ms / 1000.0 + cfg.host_margin_s
            x = _Exchange(req, frame, now, deadline, mode)
        else:
            timeout_ms = min(cfg.raw_timeout_ms, 299)
            deadline = now + timeout_ms / 1000.0 + cfg.host_margin_s
            x = _Exchange(req, frame, now, deadline, mode,
                          reasm=isotp.Reassembler(extended=ecu.sub_addr is not None,
                                                  rx_addr=ecu.rx_sub_addr))
        self.stats["requests"] += 1
        if not self._send_frame(ecu, frame, timeout_ms, mode == "gw"):
            self._trace(req, None, "gateway port not connected", 0.0)
            self._advance(Result(error="gateway:NOT_CONNECTED"))
            return
        x.gw_outstanding = 1
        x.frames_tx = 1
        self._xchg = x

    def _send_flow_control(self, x: _Exchange, now: float) -> None:
        ecu = x.req.ecu
        cfg = self.config
        fc = isotp.flow_control(ecu.sub_addr, cfg.block_size, cfg.st_min_ms)
        timeout_ms = min(cfg.raw_timeout_ms, 299)
        if self._send_frame(ecu, fc, timeout_ms, False):
            x.gw_outstanding += 1
            x.frames_tx += 1
            x.deadline = now + timeout_ms / 1000.0 + cfg.host_margin_s
        else:
            self._finish(Result(error="gateway:NOT_CONNECTED", partial=x.reasm.partial if x.reasm else b""))

    # ── receive side ─────────────────────────────────────────────────────

    def _drain_bus(self, now: float) -> None:
        while self._bus_frames:
            can_id, data, _t = self._bus_frames.popleft()
            self.stats["bus_frames"] += 1
            x = self._xchg
            if x is None or x.mode != "host" or can_id != x.req.ecu.rx_id:
                continue
            self._raw_frame(data, "bus", _t)

    def _raw_frame(self, frame: bytes, source: str, t_frame: float) -> None:
        """One raw CAN payload of the response id (host-side reassembly)."""
        x = self._xchg
        if x is None or x.reasm is None:
            return
        now = self._clock()
        # The same physical frame can arrive from the gateway and from the bus
        # tap; the second copy is dropped.
        for i, (data, src, t) in enumerate(x.seen):
            if data == frame and src != source and now - t < 0.75:
                del x.seen[i]
                return
        x.seen.append((frame, source, now))
        if len(x.seen) > 64:
            del x.seen[:32]

        what = x.reasm.feed(frame)
        if what in ("single", "done"):
            payload = x.reasm.payload or b""
            nrc = services.negative_code(x.req.payload, payload)
            if nrc == services.NRC_RESPONSE_PENDING:
                # The final answer follows unsolicited; only the bus tap can
                # catch it (the gateway has no job listening).
                x.reasm.reset()
                x.waiting_pending = True
                x.deadline = now + self.config.pending_wait_s
                return
            self._response(payload)
        elif what == "first":
            self._send_flow_control(x, now)
        elif what == "consecutive":
            # BlockSize 1: the ECU sends one frame and waits for the next flow
            # control. An ECU that ignores BlockSize keeps sending; that shows
            # as consecutive frames arriving on the bus tap faster than any
            # host round trip — then no further flow control is sent.
            if source == "bus" and x.last_cf_t and t_frame - x.last_cf_t < 0.008:
                x.free_run = True
            x.last_cf_t = t_frame
            if self.config.block_size != 1 or x.free_run:
                x.deadline = max(x.deadline, now + 0.3)
            else:
                self._send_flow_control(x, now)
        elif what == "error":
            self._finish(Result(error="isotp:%s" % x.reasm.error, partial=x.reasm.partial))
        # "ignored" / "duplicate": keep waiting until the deadline.

    def _response(self, payload: bytes) -> None:
        x = self._xchg
        if x is None:
            return
        req = x.req.payload
        nrc = services.negative_code(req, payload)
        if nrc == services.NRC_RESPONSE_PENDING and x.mode == "gw":
            self._finish(Result(error="pending", payload=payload, nrc=nrc))
            return
        if nrc is not None:
            self.stats["negative"] += 1
            self._finish(Result(ok=True, payload=payload, nrc=nrc))
        elif services.is_positive(req, payload):
            self._finish(Result(ok=True, payload=payload))
        else:
            self._finish(Result(error="unexpected", payload=payload))

    def _gateway_error(self, err: str) -> None:
        x = self._xchg
        if x is None:
            return
        if x.waiting_pending:
            return                      # still listening on the bus tap
        partial = x.reasm.partial if x.reasm is not None else b""
        if x.reasm is not None and x.reasm.in_progress and x.gw_outstanding > 0:
            return                      # an earlier flow-control job timed out; a later one is pending
        if err == "TIMEOUT":
            if partial:
                self._finish(Result(error="isotp:incomplete multi-frame response "
                                          "(%d of %d bytes)" % (len(partial), x.reasm.total),
                                    partial=partial, total=x.reasm.total))
            else:
                self.stats["timeouts"] += 1
                self._finish(Result(error="timeout"))
        else:
            self._finish(Result(error="gateway:%s" % err, partial=partial))

    def _timeout(self, now: float) -> None:
        x = self._xchg
        if x is None:
            return
        partial = x.reasm.partial if x.reasm is not None else b""
        if x.waiting_pending:
            self._finish(Result(error="pending", nrc=services.NRC_RESPONSE_PENDING))
        elif partial:
            self._finish(Result(error="isotp:incomplete multi-frame response (%d of %d bytes)"
                                      % (len(partial), x.reasm.total), partial=partial,
                                total=x.reasm.total))
        else:
            self._finish(Result(error="no_gateway_reply"))

    def _finish(self, result: Result) -> None:
        x, self._xchg = self._xchg, None
        if x is None:
            return
        now = self._clock()
        result.ms = round((now - x.started) * 1000.0, 1)
        if result.ok:
            self.stats["responses"] += 1
        self._next_tx = now + self.config.min_gap_s
        if x.gw_outstanding > 0:
            # Gateway jobs of this exchange are still queued or waiting (each
            # ends within its timeout). Their replies must not meet the next
            # request, so nothing is sent until they have drained.
            self._next_tx = now + x.gw_outstanding * (min(self.config.raw_timeout_ms, 299) / 1000.0
                                                      if x.mode == "host" else
                                                      2 * self.config.gw_timeout_ms / 1000.0) + 0.1
        self._trace(x.req, result.payload if (result.ok or result.payload) else None,
                    result.error, result.ms, result.partial)
        self._advance(result)

    def _trace(self, req: Request, payload: Optional[bytes], error: Optional[str], ms: float,
               partial: bytes = b"") -> None:
        entry: Dict[str, Any] = {
            "t": round(self._wall(), 3),
            "ecu": req.ecu.key,
            "tx": "0x%03X" % req.ecu.tx_id,
            "req": services.hexs(req.payload),
            "ms": ms,
        }
        if req.ecu.sub_addr is not None:
            entry["sub"] = "0x%02X" % req.ecu.sub_addr
        if req.note:
            entry["note"] = req.note
        if payload is not None:
            entry["resp"] = services.hexs(payload)
        if error:
            entry["error"] = error
        if partial:
            entry["partial"] = services.hexs(partial)
        self.trace.append(entry)

    # ── publishing ───────────────────────────────────────────────────────

    def _mark(self) -> None:
        self._dirty = True

    def mark_dirty(self) -> None:
        self._dirty = True

    def _publish(self, now: float) -> None:
        if not self._dirty or self._publish_cb is None:
            return
        if now - self._last_publish < self.config.publish_min_s and self.busy:
            return
        self._dirty = False
        self._last_publish = now
        try:
            self._publish_cb(self.state())
        except Exception:
            logger.exception("diagnostics state publish failed")

    # ── result helpers ───────────────────────────────────────────────────

    def _blank(self, ecu: Ecu) -> Dict[str, Any]:
        return {
            "key": ecu.key, "name": ecu.name,
            "address": "0x%03X" % ecu.tx_id if ecu.tx_id else None,
            "response_address": "0x%03X" % ecu.rx_id if ecu.rx_id else None,
            "sub_address": None if ecu.sub_addr is None else "0x%02X" % ecu.sub_addr,
            "protocol": ecu.protocol_label, "bus": ecu.bus, "confidence": ecu.confidence,
            "probe": ecu.probe, "note": ecu.note, "can_clear": bool(ecu.clear_requests()),
            "status": "unknown", "responded": False, "ident": {}, "dtcs": [],
            "mil": None, "dtc_count": None, "monitors": None, "freeze_frame": None,
            "last_scan_ts": None, "last_clear": None, "error": None, "partial": False,
            "log": [],
        }

    def _note(self, key: str, text: str) -> None:
        log = self.results[key]["log"]
        log.append(text)
        if len(log) > 20:
            del log[:-20]

    @staticmethod
    def _merge_code(codes: Dict[str, Dict[str, Any]], code: str, status: str, source: str) -> None:
        d = codes.setdefault(code, {"code": code, "status": [], "sources": []})
        if status not in d["status"]:
            d["status"].append(status)
        if source not in d["sources"]:
            d["sources"].append(source)

    def _annotate(self, ecu: Ecu, codes: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for code in sorted(codes):
            d = dict(codes[code])
            ann = dictionary.annotate(code, ecu.dict_key)
            ann.pop("code", None)
            d.update(ann)
            out.append(d)
        return out

    # ── jobs ─────────────────────────────────────────────────────────────

    def _read_codes(self, ecu: Ecu, kinds: Tuple[str, ...], codes: Dict[str, Dict[str, Any]],
                    res: Dict[str, Any], specs: Optional[List[Any]] = None,
                    ) -> Generator[Request, Result, bool]:
        """Read the DTC lists of ``ecu`` into ``codes``. Returns whether the
        ECU answered anything."""
        key = ecu.key
        answered = False
        silent = 0
        specs = specs if specs is not None else ecu.dtc_reads(kinds)
        for spec in specs:
            r = yield Request(ecu, spec.request, note=spec.label)
            if r.ok and not r.positive and spec.alt is not None and r.nrc in (0x12, 0x13, 0x31):
                # The ECU knows the service but not this form: one other form.
                self._note(key, "%s: %s, trying %s" % (spec.label, services.nrc_text(r.nrc),
                                                        services.hexs(spec.alt)))
                answered = True
                r = yield Request(ecu, spec.alt, note=spec.label + " (alt)")
            if r.positive:
                answered = True
                for code in spec.parse(r.payload):
                    self._merge_code(codes, code, spec.status, spec.label)
            elif r.ok:
                answered = True
                self._note(key, "%s: negative response %s" % (spec.label, services.nrc_text(r.nrc or 0)))
            elif r.silent:
                silent += 1
                self._note(key, "%s: no answer (%s)" % (spec.label, r.error))
                if not answered and silent >= ecu.silent_limit:
                    break
            else:
                self._note(key, "%s: %s" % (spec.label, r.error))
                if r.partial:
                    # A truncated multi-frame list still names its first codes.
                    answered = True
                    res["partial"] = True
                    # Zero-fill to the announced length: zero pairs are filler,
                    # and the length decides the response layout.
                    start = 2 if (r.total - 1) % 2 else 1      # with / without a count byte
                    whole = r.partial[:len(r.partial) - ((len(r.partial) - start) % 2)]
                    padded = whole + bytes(max(0, r.total - len(whole)))
                    for code in spec.parse(padded):
                        self._merge_code(codes, code, spec.status, spec.label + " (partial)")
        return answered

    def _job_scan(self, ecu: Ecu, kinds: Tuple[str, ...]) -> Generator[Request, Result, None]:
        key = ecu.key
        res = self.results[key]
        res.update(status="scanning", error=None, partial=False, log=[])
        self._mark()
        answered = False
        codes: Dict[str, Dict[str, Any]] = {}

        if not ecu.reachable:
            if not (ecu.probe and "probe" in kinds):
                res.update(status="unreachable", responded=False, last_scan_ts=self._wall(),
                           error="not on the DLC3 CAN (%s)" % ("K-line / SIL" if ecu.bus == "kline"
                                                               else "BEAN behind the gateway ECU"))
                return
            # Probe: one tester-present frame to an address that is unverified
            # on this car. Only an ECU that answers is asked anything else.
            r = yield Request(ecu, services.kwp_tester_present(), note="probe")
            res["last_scan_ts"] = self._wall()
            if not (r.ok or r.partial):
                res.update(status="no_answer", responded=False,
                           error="probe: no answer on 0x%03X%s (%s)" % (
                               ecu.tx_id, "" if ecu.sub_addr is None else " sub-address 0x%02X" % ecu.sub_addr,
                               r.error))
                return
            self._note(key, "probe answered: %s" % services.hexs(r.payload))
            yield from self._read_codes(ecu, ("stored",), codes, res, specs=ecu.probe_follow_up())
            res["dtcs"] = self._annotate(ecu, codes)
            res.update(status="ok", responded=True)
            return

        want_codes = any(k in kinds for k in ("stored", "pending"))

        if "status" in kinds or (want_codes and ecu.obd):
            if ecu.obd:
                r = yield Request(ecu, services.obd_monitor_status_request(), note="monitor status")
                st = services.obd_parse_monitor_status(r.payload) if r.positive else None
                if st is not None:
                    answered = True
                    res.update(mil=st["mil"], dtc_count=st["dtc_count"],
                               monitors={"complete": st["monitors_complete"],
                                         "incomplete": st["monitors_incomplete"]})
                elif r.ok:
                    answered = True
                elif r.silent:
                    # Not even the mandatory OBD PID: the ECU is off (IG off) or
                    # the gateway cannot transmit. Nothing else is sent to it.
                    res.update(status="no_answer", responded=False, last_scan_ts=self._wall(),
                               error="no answer on 0x%03X (%s)" % (ecu.tx_id, r.error))
                    return

        if want_codes:
            got = yield from self._read_codes(ecu, kinds, codes, res)
            answered = answered or got
            if got or answered:
                res["dtcs"] = self._annotate(ecu, codes)

        if "freeze" in kinds and ecu.obd and answered and any(
                "stored" in c["status"] or "pending" in c["status"] for c in codes.values()):
            ff = yield from self._read_freeze_frame(ecu)
            res["freeze_frame"] = ff
            if ff and ff.get("dtc") in codes:
                self._merge_code(codes, ff["dtc"], "freeze_frame", "obd 02")
                res["dtcs"] = self._annotate(ecu, codes)

        if "ident" in kinds:
            got = yield from self._read_ident(ecu, res)
            answered = answered or got

        res["responded"] = answered
        res["last_scan_ts"] = self._wall()
        if answered:
            res["status"] = "ok"
        else:
            res["status"] = "no_answer"
            res["error"] = "no answer on 0x%03X%s" % (
                ecu.tx_id, "" if ecu.sub_addr is None else " sub-address 0x%02X" % ecu.sub_addr)

    def _read_freeze_frame(self, ecu: Ecu) -> Generator[Request, Result, Optional[Dict[str, Any]]]:
        r = yield Request(ecu, services.obd_freeze_request(0x02), note="freeze frame DTC")
        if not r.positive:
            return None
        code = services.obd_parse_freeze_dtc(r.payload)
        if code is None:
            return None
        ff: Dict[str, Any] = {"dtc": code, "values": {}, "raw": {"02": services.hexs(r.payload)}}
        # Supported freeze-frame PIDs (02 00): an unsupported PID is simply not
        # answered, and every silence costs a full timeout.
        r = yield Request(ecu, services.obd_freeze_request(0x00), note="freeze frame supported PIDs")
        supported = None
        if r.positive and len(r.payload) >= 7 and r.payload[1] == 0x00:
            mask = int.from_bytes(r.payload[3:7], "big")
            supported = {pid for pid in range(1, 33) if mask & (1 << (32 - pid))}
        silent = 0
        for pid, name, unit, _fn in services.FREEZE_PIDS:
            if supported is not None and pid not in supported:
                continue
            r = yield Request(ecu, services.obd_freeze_request(pid), note="freeze frame %s" % name)
            if not r.positive:
                if r.silent:
                    silent += 1
                    if silent >= 2:
                        break
                continue
            silent = 0
            ff["raw"]["%02X" % pid] = services.hexs(r.payload)
            value = services.obd_parse_freeze_value(pid, r.payload)
            if value is not None:
                ff["values"][name] = {"value": value, "unit": unit}
        return ff

    def _read_ident(self, ecu: Ecu, res: Dict[str, Any]) -> Generator[Request, Result, bool]:
        answered = False
        ident = dict(res.get("ident") or {})
        for spec in ecu.ident_reads():
            r = yield Request(ecu, spec.request, note=spec.label)
            if r.positive:
                answered = True
                value = spec.parse(r.payload)
                if value:
                    ident[spec.status] = value
                    if spec.status == "vin" and isinstance(value, str):
                        self.vehicle["vin"] = value
                ident.setdefault("raw", {})[services.hexs(spec.request)] = services.hexs(r.payload)
            elif r.ok:
                answered = True
                ident.setdefault("raw", {})[services.hexs(spec.request)] = "NRC " + services.nrc_text(r.nrc or 0)
            elif r.silent and not answered:
                break
        res["ident"] = ident
        return answered

    def _job_read(self, ecu: Ecu, item: str, payload: bytes) -> Generator[Request, Result, None]:
        r = yield Request(ecu, payload, note="read %s" % item)
        out: Dict[str, Any] = {"ecu": ecu.key, "item": item, "request": services.hexs(payload),
                               "ts": self._wall(), "ms": r.ms}
        if r.ok:
            out["response"] = services.hexs(r.payload)
            self.results[ecu.key]["responded"] = True
            if r.nrc is not None:
                out["error"] = "negative response " + services.nrc_text(r.nrc)
            else:
                decoder = ecu.decoders.get(item)
                if decoder is not None:
                    try:
                        out["values"] = decoder(r.payload)
                    except Exception as exc:        # decoder bug must not kill the job
                        out["decode_error"] = str(exc)
        else:
            out["error"] = r.error
            if r.partial:
                out["partial"] = services.hexs(r.partial)
        self.last_read = out

    def speed_gate(self) -> Generator[Request, Result, Tuple[bool, str, Optional[float]]]:
        """Vehicle-speed check before a clear: asks the ECM (mode 01 PID 0D),
        falls back to the broadcast speed in the state. Returns
        ``(stopped, reason, speed)``."""
        engine = self.ecus.get("engine")
        if engine is not None and engine.obd:
            r = yield Request(engine, services.obd_pid_request(0x0D), note="vehicle speed")
            data = services.obd_parse_pid_value(0x0D, r.payload) if r.positive else None
            if data:
                speed = float(data[0])
                return speed == 0.0, "ECM reports %d km/h" % speed, speed
        speed = self._vehicle_speed() if self._vehicle_speed is not None else None
        if speed is None:
            return False, "vehicle speed unknown", None
        return speed == 0.0, "broadcast speed %.0f km/h" % speed, float(speed)

    def clear_sequence(self, ecu: Ecu, allow_moving: bool, origin: str,
                       ) -> Generator[Request, Result, Dict[str, Any]]:
        """Read everything, clear, read back. Returns the clear record."""
        key = ecu.key
        res = self.results[key]
        record: Dict[str, Any] = {"ts": self._wall(), "ecu": key, "origin": origin, "ok": False,
                                  "result": "", "before": {}, "after": None, "speed": None}
        stopped, why, speed = yield from self.speed_gate()
        record["speed"] = speed
        if not stopped and not allow_moving:
            record["result"] = "refused: %s (clear only at standstill)" % why
            return record

        # Pre-read: the clear erases codes AND freeze frames, keep them.
        codes: Dict[str, Dict[str, Any]] = {}
        before: Dict[str, Any] = {}
        if ecu.obd:
            r = yield Request(ecu, services.obd_monitor_status_request(), note="monitor status")
            st = services.obd_parse_monitor_status(r.payload) if r.positive else None
            if st is not None:
                before["monitor_status"] = st
                # Distance driven since the previous clear (PID 0x31), if supported.
                r = yield Request(ecu, services.obd_pid_request(0x31), note="km since clear")
                data = services.obd_parse_pid_value(0x31, r.payload) if r.positive else None
                if data and len(data) >= 2:
                    before["km_since_previous_clear"] = (data[0] << 8) | data[1]
        answered = yield from self._read_codes(ecu, ("stored", "pending"), codes, res)
        before["dtcs"] = self._annotate(ecu, codes)
        if ecu.obd and codes:
            before["freeze_frame"] = yield from self._read_freeze_frame(ecu)
        record["before"] = before
        if not answered:
            record["result"] = "refused: %s does not answer" % ecu.name
            return record

        results = []
        any_ok = False
        for label, payload in ecu.clear_requests():
            r = yield Request(ecu, payload, allow_clear=True, note="CLEAR " + label)
            if r.positive:
                any_ok = True
                results.append("%s: cleared" % label)
            elif r.ok:
                results.append("%s: refused by ECU (%s)" % (label, services.nrc_text(r.nrc or 0)))
            else:
                results.append("%s: %s" % (label, r.error))
        record["ok"] = any_ok
        record["result"] = "; ".join(results)

        after: Dict[str, Dict[str, Any]] = {}
        yield from self._read_codes(ecu, ("stored", "pending"), after, res)
        record["after"] = [c["code"] for c in self._annotate(ecu, after)]
        res["dtcs"] = self._annotate(ecu, after)
        res["freeze_frame"] = None if any_ok else res.get("freeze_frame")
        res["responded"] = True
        res["status"] = "ok"
        res["last_scan_ts"] = self._wall()
        if ecu.obd:
            r = yield Request(ecu, services.obd_monitor_status_request(), note="monitor status")
            st = services.obd_parse_monitor_status(r.payload) if r.positive else None
            if st is not None:
                res.update(mil=st["mil"], dtc_count=st["dtc_count"],
                           monitors={"complete": st["monitors_complete"],
                                     "incomplete": st["monitors_incomplete"]})
        return record

    def _job_clear(self, ecu: Ecu, allow_moving: bool) -> Generator[Request, Result, None]:
        record = yield from self.clear_sequence(ecu, allow_moving, origin="manual")
        self.results[ecu.key]["last_clear"] = {
            "ts": record["ts"], "ok": record["ok"], "result": record["result"],
            "cleared": [d["code"] for d in record["before"].get("dtcs", [])] if record["ok"] else [],
            "remaining": record["after"],
        }
        if self.on_clear is not None:
            try:
                self.on_clear(record)
            except Exception:
                logger.exception("clear record hook failed")

    #: ``fn(record)`` called after every manual clear (report log).
    on_clear: Optional[Callable[[Dict[str, Any]], None]] = None


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16)
        except ValueError:
            return None
    return None


def _public(result: Dict[str, Any]) -> Dict[str, Any]:
    """ECU result for the state: the manual URL and trouble-area lists stay,
    internal bookkeeping does not."""
    return dict(result)
