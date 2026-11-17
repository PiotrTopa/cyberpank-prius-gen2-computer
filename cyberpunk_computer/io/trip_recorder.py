"""
Trip recorder — per-trip communication logging with rotation.

The backend can record every byte of gateway/satellite/powerbox traffic to
NDJSON files that are byte-compatible with :class:`FileInputPort` (replay). This
module adds two things the legacy :class:`CommLogger` lacked:

    * **Trip segmentation** — instead of one giant file, traffic is split into
      one file per *trip*. A trip is bounded by the ignition (powerbox ACC) when
      available, or by an idle gap in traffic otherwise. Long trips are further
      split by size. While the ignition is known to be OFF, traffic (powerbox
      heartbeats, BEAN, satellites) does not open a trip: the car parked with
      ACC off is not a trip. Traffic opens trips only while the ignition state
      is unknown (no powerbox ACC report yet, or ``use_ignition`` off).
    * **Rotation** — the trip directory is pruned to stay within a configurable
      number of files, total size and age, so an always-on backend never fills
      the phone's storage.

What gets recorded is fully configurable (:class:`RecordingConfig`): system / CAN
/ AVC-LAN / satellite / powerbox / outgoing commands can each be toggled.

The recorder is transport-agnostic: it is fed by the ingress/egress log callbacks
(``ingress.add_message_log_callback`` / ``egress.set_message_log_callback``) and
forwards accepted messages to a per-trip :class:`CommLogger`, preserving the
device-domain timestamps the replay loader expects.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .comm_logger import CommLogger, LogConfig, iter_ndjson
from .ports import (
    DEVICE_AVC,
    DEVICE_CAN,
    DEVICE_POWERBOX_BASE,
    DEVICE_SATELLITE_BASE,
    DEVICE_SYSTEM,
)

logger = logging.getLogger(__name__)

_MB = 1024 * 1024

#: A wall-clock step larger than this (wall - monotonic offset change) while
#: a trip is open closes the trip and re-dates it. The phone has no RTC: it
#: boots with an old time and NTP steps it later (2026-11-17 10:41: the
#: first 36 s after a reboot were written as "2026-11-16 11:50" into
#: trip_20261116_115052_965, which read like a stale file being reused).
CLOCK_STEP_S = 5.0


def redate_trip_file(path: Path, step_s: float, t_step_old: float) -> Optional[Path]:
    """Rewrite ``path`` with its pre-step wall times moved by ``step_s``
    and rename it after its corrected start time. A wall time counts as
    pre-step when it is nearer the old clock at the step (``t_step_old``)
    than the new one. Returns the new path (None on failure, the original
    file is then left as it was)."""
    half = abs(step_s) / 2.0

    def fix(t):
        if isinstance(t, (int, float)) and t > 1e9 and abs(t - t_step_old) < half:
            return t + step_s
        return t

    try:
        tmp = path.with_name(path.name + ".redate")
        start = None
        with open(path, "rb") as src, open(tmp, "w", encoding="utf-8") as dst:
            for rec in iter_ndjson(src):
                if rec.get("meta") == "comm_log":
                    try:
                        t0 = datetime.fromisoformat(rec.get("created", "")).timestamp()
                        start = fix(t0)
                        rec["created"] = datetime.fromtimestamp(start).isoformat()
                    except (TypeError, ValueError):
                        pass
                    rec["clock_step_s"] = round(step_s, 3)
                elif "ts" in rec:
                    rec["ts"] = fix(rec["ts"])
                    d = rec.get("d")
                    if isinstance(d, dict) and isinstance(d.get("ts"), (int, float)) and d["ts"] > 1e12:
                        d["ts"] = int(round(fix(d["ts"] / 1000.0) * 1000))
                dst.write(json.dumps(rec, separators=(",", ":")) + "\n")
        name = path.name
        if start is not None:
            stamp = datetime.fromtimestamp(start).strftime("%Y%m%d_%H%M%S_%f")[:-3]
            part = "_p" + name.rsplit("_p", 1)[1].split(".")[0] if "_p" in name else ""
            name = "trip_%s%s.ndjson" % (stamp, part)
        new = path.with_name(name)
        n = 1
        while new.exists() and new != path:
            new = path.with_name(name.replace(".ndjson", "_%d.ndjson" % n))
            n += 1
        os.replace(tmp, new)
        if new != path:
            path.unlink(missing_ok=True)
        logger.info("Trip recorder: re-dated %s -> %s (clock step %+.0f s)", path.name, new.name, step_s)
        return new
    except OSError:
        logger.exception("Trip recorder: re-dating %s failed", path)
        try:
            path.with_name(path.name + ".redate").unlink(missing_ok=True)
        except OSError:
            pass
        return None


@dataclass
class RotationPolicy:
    """Limits enforced on the trip-log directory after each trip closes."""

    max_files: int = 60          # keep at most N trip files (0 = unlimited)
    max_total_mb: float = 512.0  # cap total size of the trip dir (0 = unlimited)
    max_age_days: float = 30.0   # delete files older than this (0 = unlimited)


@dataclass
class RecordingConfig:
    """Configuration for trip recording (what/where/how to record)."""

    enabled: bool = False
    directory: str = "logs/trips"

    # Segmentation strategy:
    #   "trip"       new file per detected trip (ignition cycle or idle gap)
    #   "session"    one file per backend run (still size-split)
    #   "continuous" alias of session (kept for clarity)
    segmentation: str = "trip"

    idle_timeout_s: float = 120.0   # gap without traffic that ends a trip
    min_trip_seconds: float = 15.0  # discard trips shorter than this (noise)
    max_file_mb: float = 64.0       # split the current file at this size (0 = no split)
    use_ignition: bool = True       # bound trips by powerbox ACC when available

    # What to record (each category independently toggleable).
    include_system: bool = True
    include_can: bool = True
    include_avc: bool = True
    include_satellite: bool = True
    include_powerbox: bool = True
    include_outgoing: bool = True
    # GNSS fixes the position feeder sends to roadsense mapd (lat/lon, i.e. a
    # location track). Explicit only: not part of "all" (BACKEND_RECORD_INCLUDE
    # must name "position"). docs/POSITION_FEEDER.md section 11.
    include_position: bool = False

    rotation: RotationPolicy = field(default_factory=RotationPolicy)


class TripRecorder:
    """Record gateway traffic into rotating, per-trip NDJSON files.

    Thread-safety: all public methods are expected to be called from the engine
    thread (the same thread that drives ingress/egress callbacks and the run
    loop). The underlying :class:`CommLogger` does its file writes on its own
    background thread, so the engine loop is never blocked on disk I/O.
    """

    def __init__(self, config: Optional[RecordingConfig] = None) -> None:
        self.config = config or RecordingConfig()
        self._dir = Path(self.config.directory)

        self._current: Optional[CommLogger] = None
        self._trip_start: float = 0.0
        self._last_activity: float = 0.0
        self._part_index: int = 0
        self._started = False
        # Last powerbox ACC state seen by on_ignition(); None = unknown.
        self._ignition: Optional[bool] = None
        self.discarded = 0           # short trips dropped (whole run)
        self._discarded_quiet = 0    # dropped since the last INFO line
        # wall - monotonic when the current file was opened (clock steps)
        self._clock_offset: Optional[float] = None
        self.clock_steps = 0

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("Trip recorder: cannot create directory %s", self._dir)
            self._started = False
            return
        self._prune()
        # For session/continuous modes, open immediately; trip mode waits for
        # the first message (or ignition) to avoid empty files while parked.
        if self.config.segmentation in ("session", "continuous"):
            self._open(reason="session-start")
        logger.info(
            "Trip recorder started (dir=%s, segmentation=%s)",
            self._dir, self.config.segmentation,
        )

    def stop(self) -> None:
        if not self._started:
            return
        self._close(reason="shutdown")
        self._started = False
        logger.info("Trip recorder stopped")

    # ── message intake (ingress/egress callbacks) ────────────────────────────

    def log_incoming(self, msg, direction: str = "IN") -> None:
        if not self._started or not self._should_log(msg.device_id):
            return
        self._open_for_traffic()
        if self._current is None:
            return
        self._last_activity = time.monotonic()
        self._current.log_incoming(msg, "IN")
        self._maybe_split()

    def log_outgoing(self, msg, direction: str = "OUT") -> None:
        if not self._started or not self.config.include_outgoing:
            return
        if not self._should_log(msg.device_id):
            return
        self._open_for_traffic()
        if self._current is None:
            return
        self._last_activity = time.monotonic()
        self._current.log_outgoing(msg, "OUT")
        self._maybe_split()

    def log_position(self, fix: dict) -> None:
        """A GNSS fix sent to mapd (position feeder thread), only with
        ``include_position``. Written as ``{"dir": "POS", "id": 0, "d": fix}``
        into the trip file that is open; never opens a trip and does not count
        as traffic (a parked car with a fix does not keep a trip alive).
        Replay skips ``POS`` lines. CommLogger queues the write, so this is
        safe from another thread (a trip closing at the same moment drops it)."""
        if not self._started or not self.config.include_position:
            return
        cur = self._current
        if cur is None:
            return
        cur.log_raw("POS", DEVICE_SYSTEM, dict(fix))

    # ── periodic + event-driven segmentation ─────────────────────────────────

    def tick(self, now: Optional[float] = None, wall: Optional[float] = None) -> None:
        """Close the current trip after an idle gap or a wall-clock step
        (call from the run loop)."""
        if not self._started or self._current is None:
            return
        mono = time.monotonic()
        self._check_clock(mono, wall if wall is not None else time.time())
        if self._current is None or self.config.segmentation != "trip":
            return
        now = now if now is not None else mono
        if (now - self._last_activity) >= self.config.idle_timeout_s:
            self._close(reason="idle")

    def on_ignition(self, acc_on: bool) -> None:
        """Bound trips by the powerbox ignition (ACC) when enabled."""
        if not self._started:
            return
        if not self.config.use_ignition or self.config.segmentation != "trip":
            return
        acc_on = bool(acc_on)
        changed = acc_on != self._ignition
        self._ignition = acc_on
        if not changed:
            return
        if acc_on:
            self._ensure_open(reason="ignition-on")
        else:
            self._close(reason="ignition-off")

    # ── internals ─────────────────────────────────────────────────────────────

    def _check_clock(self, mono: float, wall: float) -> None:
        """Wall clock stepped since the file was opened (NTP after a boot
        with a stale time): close the file, re-date it in the background,
        and let the next traffic open a correctly named one."""
        if self._clock_offset is None:
            return
        step = (wall - mono) - self._clock_offset
        if abs(step) < CLOCK_STEP_S:
            return
        self.clock_steps += 1
        t_step_old = mono + self._clock_offset
        cur = self._current
        path = cur.filepath if cur is not None else None
        logger.warning("Trip recorder: wall clock stepped %+.0f s; closing %s",
                       step, path.name if path else "-")
        self._close(reason="clock-step", keep=True)
        if path is not None and path.exists():
            threading.Thread(target=redate_trip_file, args=(path, step, t_step_old),
                             daemon=True, name="trip-redate").start()
        if self.config.segmentation in ("session", "continuous") or self._ignition:
            self._open(reason="clock-step")

    def _should_log(self, device_id: int) -> bool:
        cfg = self.config
        if device_id == DEVICE_SYSTEM:
            return cfg.include_system
        if device_id == DEVICE_CAN:
            return cfg.include_can
        if device_id == DEVICE_AVC:
            return cfg.include_avc
        if device_id >= DEVICE_POWERBOX_BASE:
            return cfg.include_powerbox
        if device_id >= DEVICE_SATELLITE_BASE:
            return cfg.include_satellite
        return True

    def _open_for_traffic(self) -> None:
        """Traffic opens a trip only in trip mode while the ignition is not
        known to be off (session modes are always open)."""
        if self._current is not None:
            return
        if self.config.segmentation == "trip" and self.config.use_ignition \
                and self._ignition is False:
            return
        self._open(reason="traffic")

    def _ensure_open(self, reason: str = "traffic") -> None:
        if self._current is None:
            self._open(reason=reason)

    def _open(self, reason: str) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        self._part_index = 0
        filename = f"trip_{timestamp}.ndjson"
        # All-permissive LogConfig: filtering is done by the recorder so we get
        # precise per-category control (incl. a dedicated powerbox toggle).
        cfg = LogConfig(directory=str(self._dir))
        path = self._dir / filename
        self._current = CommLogger(filepath=str(path), config=cfg)
        if not self._current.start():
            logger.error("Trip recorder: failed to open %s", path)
            self._current = None
            return
        self._trip_start = time.monotonic()
        self._last_activity = self._trip_start
        self._clock_offset = time.time() - self._trip_start
        if self._discarded_quiet:
            logger.info("Trip recorder: opened %s (%s; %d short trip(s) discarded before)",
                        path, reason, self._discarded_quiet)
            self._discarded_quiet = 0
        else:
            logger.info("Trip recorder: opened %s (%s)", path, reason)

    def _close(self, reason: str, keep: bool = False) -> None:
        if self._current is None:
            return
        logger_obj = self._current
        self._current = None
        self._clock_offset = None
        path = logger_obj.filepath
        duration = max(0.0, time.monotonic() - self._trip_start)
        logger_obj.stop()

        # Drop trips that are too short to be useful (power blips, brief acc).
        if not keep and duration < self.config.min_trip_seconds and self._part_index == 0:
            try:
                path.unlink(missing_ok=True)
                self.discarded += 1
                self._discarded_quiet += 1
                # DEBUG only: the count is reported with the next opened trip.
                logger.debug(
                    "Trip recorder: discarded short trip %s (%.1fs < %.1fs, %s)",
                    path.name, duration, self.config.min_trip_seconds, reason,
                )
            except OSError:
                logger.exception("Trip recorder: failed to delete short trip %s", path)
        else:
            logger.info(
                "Trip recorder: closed %s (%.1fs, %s, %d msgs)",
                path.name, duration, reason, logger_obj.messages_logged,
            )
        self._prune()

    def _maybe_split(self) -> None:
        """Split a long-running file once it exceeds ``max_file_mb``."""
        if self._current is None or self.config.max_file_mb <= 0:
            return
        if self._current.bytes_written >= self.config.max_file_mb * _MB:
            # Roll to a new part: keep the same logical trip but a fresh file.
            self._part_index += 1
            old = self._current
            old_path = old.filepath
            old.stop()
            self._prune()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            filename = f"trip_{timestamp}_p{self._part_index}.ndjson"
            cfg = LogConfig(directory=str(self._dir))
            self._current = CommLogger(filepath=str(self._dir / filename), config=cfg)
            if not self._current.start():
                logger.error("Trip recorder: failed to open split part %s", filename)
                self._current = None
                return
            self._last_activity = time.monotonic()
            logger.info(
                "Trip recorder: split %s -> %s (size cap %.0f MB)",
                old_path.name, filename, self.config.max_file_mb,
            )

    def _prune(self) -> None:
        """Enforce the rotation policy on the trip directory."""
        pol = self.config.rotation
        try:
            files = sorted(
                (p for p in self._dir.glob("trip_*.ndjson") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
            )
        except OSError:
            logger.exception("Trip recorder: prune scan failed")
            return

        # Never delete the file we are currently writing to.
        current_path = self._current.filepath if self._current else None
        files = [p for p in files if p != current_path]

        now = time.time()

        # 1) Age limit.
        if pol.max_age_days and pol.max_age_days > 0:
            cutoff = now - pol.max_age_days * 86400
            survivors: List[Path] = []
            for p in files:
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink(missing_ok=True)
                        logger.info("Trip recorder: pruned aged %s", p.name)
                    else:
                        survivors.append(p)
                except OSError:
                    survivors.append(p)
            files = survivors

        # 2) File-count limit (delete oldest first).
        if pol.max_files and pol.max_files > 0:
            while len(files) > pol.max_files:
                victim = files.pop(0)
                try:
                    victim.unlink(missing_ok=True)
                    logger.info("Trip recorder: pruned (count) %s", victim.name)
                except OSError:
                    pass

        # 3) Total-size limit (delete oldest first).
        if pol.max_total_mb and pol.max_total_mb > 0:
            limit = pol.max_total_mb * _MB
            try:
                total = sum(p.stat().st_size for p in files)
            except OSError:
                total = 0
            while files and total > limit:
                victim = files.pop(0)
                try:
                    size = victim.stat().st_size
                    victim.unlink(missing_ok=True)
                    total -= size
                    logger.info("Trip recorder: pruned (size) %s", victim.name)
                except OSError:
                    pass

    # ── introspection ─────────────────────────────────────────────────────────

    @property
    def recording(self) -> bool:
        return self._current is not None

    @property
    def current_file(self) -> Optional[Path]:
        return self._current.filepath if self._current else None
