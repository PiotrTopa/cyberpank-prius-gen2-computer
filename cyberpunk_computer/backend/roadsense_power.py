"""
roadsense perception stack control (single-computer layout).

Since 2026-11-04 the Prius has ONE POCO F1: it runs this backend (power
control, gateway, MFD, API) AND the roadsense road-perception stack
(github.com/PiotrTopa/roadsense, MIT: camerad-dvr / modeld / envd / api, plus
the AGPL sign provider from roadsense-signs as a separate process). roadsense
is a set of systemd units gathered under ``roadsense.target``; nothing is
linked into this process — the backend only drives ``systemctl`` and reads the
stack's HTTP ``/v1/health`` endpoint, which is the documented boundary between
the MIT/AGPL stacks and this repository.

The stack is GPU/CPU heavy (~10-14 road runs/s, 70-95 C without a fan), so it
runs **only while ACC is on** and is stopped cleanly when the key goes away:

    OFF ──ACC on──▶ CAMERA_ON ──settle──▶ STARTING ──/v1/health──▶ RUNNING
     ▲   (relay ch4: camera hub)        (systemctl start)            │ ACC off
     │                                                               ▼
     └── camera hub off ◀── STOPPING ◀── systemctl stop ◀──────── GRACE
                        (units inactive)                     (ACC back: RUNNING)

* ACC on: power the camera hub (powerbox relay ch4, the fvision dashcam the
  stack reads through ``camerad-dvr --auto``), give the hub ``camera_settle_s``
  to enumerate, then ``systemctl start --no-block roadsense.target`` and wait
  for the stack's API to answer (``start_timeout_s``; after that RUNNING is
  entered anyway, degraded, and the API watchdog below takes over).
* ACC off: keep running for ``grace_s`` in case the key comes back, then
  ``systemctl stop --no-block roadsense.target``, wait for every unit to be
  inactive (or ``stop_timeout_s``), then cut the camera hub.
* RUNNING watchdog: units are polled with ``systemctl is-active`` and the API
  with ``/v1/health``; an API silent for ``api_dead_restart_s`` restarts the
  target (at most once per ``restart_cooldown_s``).
* A manual ``mode`` (``auto`` | ``on`` | ``off``, API command ``roadsense_mode``)
  overrides the ACC follower: ``on`` keeps the stack up with the key off (bench
  work; the fan rules still apply), ``off`` keeps it down.
* Backend start (first tick, "reconcile"): in mode ``auto`` the manager waits
  for the powerbox's first ACC report (``acc_known``; at most ``acc_wait_s``,
  then ACC counts as off). A stack found running while it is wanted (ACC on
  or mode ``on``) is adopted: RUNNING, camera hub asserted ON (already on, no
  power glitch). A stack found running while it is NOT wanted (ACC off, e.g.
  units started by hand while parked, or mode ``off``) is stopped at once
  (STOPPING, no grace) and the camera hub is NEVER switched on for it: the
  hub is cut when the units are down. With nothing running and nothing
  wanted, the hub is switched off.
* A **preview hold** (:meth:`RoadsenseManager.set_preview_hold`, driven by the
  dashboard camera preview, ``backend.camera_preview``) powers the camera hub
  WITHOUT the stack while the stack is not wanted::

      OFF ──hold──▶ PREVIEW (relay ch4 on, no units, no ``full`` profile)
       ▲              │ hold released ─▶ camera hub off ─▶ OFF
       │              │ stack wanted (ACC on) ─▶ preview_release() ─▶ STARTING
      a stop that finishes while the hold is set ends in PREVIEW, not OFF

  The hold never starts, stops or delays the stack: with the stack wanted it
  is only remembered for the moment the stack goes away. ``preview_release``
  (callback) is called right before ``systemctl start`` so the preview's own
  block-device reader lets go of the camera before ``camerad-dvr`` opens it
  (both use ``O_EXCL``). PREVIEW counts as "off" for :meth:`is_off` (no unit
  runs: the low CPU profile is allowed) but :meth:`holds_camera` is True, so
  a manual ``set_relay`` on ch4 is still refused.
* **Early camera power** (:meth:`RoadsenseManager.set_early_camera`): the
  early start demand that boots the MFD before ACC (BEAN: doors unlocked)
  also enters PREVIEW, for at most ``early_camera_s``, so the dashcam's own
  power-up sequence (it leaves the USB bus once, for 4 s or for 2 min) runs
  before the drive instead of during its first minutes.

The POCO ``low`` power profile (Gold cluster offline) must not be applied while
modeld is pinned to cpu4-7, so the backend defers the key-off ``low`` switch
until this manager reports :meth:`is_off` (see ``BackendService``).

Every tick also samples ``/sys/class/thermal`` (all zones) into the published
status, whether the stack runs or not: that is the raw material for the heat
model that follows this lane.

All external effects (systemctl, HTTP probe, relay, thermals, clock) are
injectable so the state machine is unit-testable without hardware.
"""

from __future__ import annotations

import glob
import json
import logging
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Manager phases (exposed verbatim in AppState.roadsense.state).
STATE_DISABLED = "disabled"
STATE_OFF = "off"
STATE_CAMERA_ON = "camera_on"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_GRACE = "grace"
STATE_STOPPING = "stopping"
STATE_PREVIEW = "preview"     # camera hub powered for a dashboard preview, stack down

# Operator modes.
MODE_AUTO = "auto"   # follow ACC
MODE_ON = "on"       # keep the stack up regardless of ACC
MODE_OFF = "off"     # keep the stack down regardless of ACC
MODES = (MODE_AUTO, MODE_ON, MODE_OFF)

# Default unit set of the reference deployment (roadsense + roadsense-signs).
DEFAULT_UNITS = (
    "roadsense-camerad-dvr.service",
    "roadsense-modeld.service",
    "roadsense-envd.service",
    "roadsense-mapd.service",       # position filter + map matching (needs no camera)
    "roadsense-api.service",
    "roadsense-signd.service",
)

# /v1/health keys worth mirroring into the backend state (the rest stays in
# the roadsense API; the dashboard links to it). "map" / "fused" are roadsense's
# short map-layer summaries (state, availability, pack date, road, limit and
# source; ~0.4 kB together), shown on the dashboard overview without polling.
_HEALTH_KEYS = ("fps", "camera_fps", "model_ms", "detect_ms", "env_ms",
                "lights_ms", "env", "speed_limit", "lights", "cpu_temp_c", "map", "fused")


@dataclass
class RoadsenseConfig:
    """Configuration for the roadsense stack manager (BACKEND_ROADSENSE_*)."""
    target: str = "roadsense.target"
    units: Sequence[str] = DEFAULT_UNITS
    api_host: str = "127.0.0.1"      # where this backend probes the stack's API
    api_port: int = 8090             # ROADSENSE_API_PORT on the car computer (8080 is ours)
    health_path: str = "/v1/health"
    api_timeout_s: float = 2.0
    camera_relay: bool = True        # power the camera hub (relay ch4) with the stack
    camera_settle_s: float = 8.0     # hub + dashcam enumerate ~6 s after power
    early_camera_s: float = 180.0    # early demand (door unlocked): camera hub on for at most this
                                     # long before ACC, without the stack (0 = never)
    grace_s: float = 30.0            # key-off grace before the stop
    start_timeout_s: float = 120.0   # API must answer within this after the start
    stop_timeout_s: float = 60.0     # units must be inactive within this after the stop
    tick_s: float = 5.0              # manager cadence within the backend loop
    api_dead_restart_s: float = 180.0  # RUNNING: API silent this long -> restart (0 = never)
    restart_cooldown_s: float = 300.0  # min spacing between automatic restarts
    mode: str = MODE_AUTO            # initial operator mode
    systemctl_timeout_s: float = 15.0
    acc_wait_s: float = 20.0         # start-up: wait this long for the first ACC report
    thermal_glob: str = "/sys/class/thermal/thermal_zone*"


def read_thermal_zones(pattern: str = "/sys/class/thermal/thermal_zone*") -> Dict[str, float]:
    """Return ``{zone_type: temperature_C}`` for every readable thermal zone."""
    out: Dict[str, float] = {}
    for zone in glob.glob(pattern):
        try:
            with open(zone + "/type", "r", encoding="ascii") as f:
                ztype = f.read().strip()
            with open(zone + "/temp", "r", encoding="ascii") as f:
                out[ztype] = round(int(f.read().strip()) / 1000.0, 1)
        except (OSError, ValueError):
            continue
    return out


def fetch_health(url: str, timeout: float) -> Optional[dict]:
    """GET ``url`` and decode JSON; ``None`` on any failure."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - localhost
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


class _AsyncProbe:
    """Run a blocking probe in a worker thread; :meth:`poll` never blocks.

    ``poll()`` starts a probe if none is running and returns the latest
    completed result as ``(result, completed_at_monotonic)`` (``(None, None)``
    before the first completion).
    """

    def __init__(self, fn: Callable[[], object], clock: Callable[[], float]) -> None:
        self._fn = fn
        self._clock = clock
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._result: object = None
        self._done_at: Optional[float] = None

    def _run(self) -> None:
        try:
            res = self._fn()
        except Exception:
            res = None
        with self._lock:
            self._result = res
            self._done_at = self._clock()

    def poll(self) -> Tuple[object, Optional[float]]:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            snapshot = (self._result, self._done_at)
        if not running:
            t = threading.Thread(target=self._run, name="roadsense-probe", daemon=True)
            self._thread = t
            t.start()
        return snapshot

    def reset(self) -> None:
        with self._lock:
            self._result = None
            self._done_at = None


class RoadsenseManager:
    """ACC-follower for the roadsense stack. Call :meth:`tick` from the backend
    loop; it self-limits to ``tick_s``. ``publish`` receives a status dict on
    every observable change (and periodically)."""

    PUBLISH_REFRESH_S = 10.0

    def __init__(
        self,
        config: RoadsenseConfig,
        publish: Optional[Callable[[dict], None]] = None,
        runner: Optional[Callable[..., "subprocess.CompletedProcess"]] = None,
        camera: Optional[object] = None,
        health_fetch: Optional[Callable[[str, float], Optional[dict]]] = None,
        thermals: Optional[Callable[[], Dict[str, float]]] = None,
        clock: Callable[[], float] = time.monotonic,
        async_probe: bool = True,
        preview_release: Optional[Callable[[], None]] = None,
    ) -> None:
        self.config = config
        # Called right before the stack starts while a preview may hold the
        # camera device (backend.camera_preview closes its O_EXCL reader).
        self.preview_release = preview_release
        self._preview_hold = False
        self._early_until: Optional[float] = None   # early camera power (set_early_camera) ends then
        self._early_seen = False
        self._publish = publish
        self._run = runner or self._default_runner
        self._camera = camera
        self._health_fetch = health_fetch or fetch_health
        self._thermals = thermals or (lambda: read_thermal_zones(config.thermal_glob))
        self._clock = clock
        self._mode = config.mode if config.mode in MODES else MODE_AUTO

        self._probe: Optional[_AsyncProbe] = None
        if async_probe:
            self._probe = _AsyncProbe(self._fetch_health_now, clock)

        self._state = STATE_OFF
        self._deadline: Optional[float] = None
        self._last_tick = 0.0
        self._reconciled = False
        self._state_since: float = time.time()
        self._camera_power: Optional[bool] = None
        self._api_ok: Optional[bool] = None
        self._health: Optional[dict] = None
        self._last_api_ok: Optional[float] = None       # wall clock
        self._api_silent_since: Optional[float] = None  # monotonic (RUNNING)
        self._units: Optional[Dict[str, str]] = None
        self._temps: Dict[str, float] = {}
        self._restarts = 0
        self._last_restart: Optional[float] = None
        self._restart_requested = False
        self._last_error: Optional[str] = None
        self._last_published: Optional[tuple] = None
        self._last_publish_t: float = 0.0
        self._last_acc: Optional[bool] = None
        self._born: float = clock()

    # ── external effects (injectable) ────────────────────────────────────

    @staticmethod
    def _default_runner(cmd, timeout=15):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def _systemctl(self, *args: str) -> Optional["subprocess.CompletedProcess"]:
        cmd = ["systemctl", *args]
        try:
            return self._run(cmd, timeout=self.config.systemctl_timeout_s)
        except Exception as exc:  # noqa: BLE001 - never let systemctl kill the loop
            self._last_error = "%s: %s" % (" ".join(cmd), exc)
            logger.exception("roadsense: %s failed", " ".join(cmd))
            return None

    def _start_target(self) -> None:
        logger.info("roadsense: starting %s", self.config.target)
        r = self._systemctl("start", "--no-block", self.config.target)
        if r is not None and r.returncode != 0:
            self._last_error = "start: " + (r.stderr or r.stdout or "").strip()
            logger.error("roadsense: start failed: %s", self._last_error)

    def _stop_target(self) -> None:
        logger.info("roadsense: stopping %s", self.config.target)
        r = self._systemctl("stop", "--no-block", self.config.target)
        if r is not None and r.returncode != 0:
            self._last_error = "stop: " + (r.stderr or r.stdout or "").strip()
            logger.error("roadsense: stop failed: %s", self._last_error)

    def _restart_target(self, why: str) -> None:
        self._restarts += 1
        self._last_restart = self._clock()
        logger.warning("roadsense: restart #%d (%s)", self._restarts, why)
        r = self._systemctl("restart", "--no-block", self.config.target)
        if r is not None and r.returncode != 0:
            self._last_error = "restart: " + (r.stderr or r.stdout or "").strip()
        self._api_silent_since = None
        if self._probe is not None:
            self._probe.reset()

    def _query_units(self) -> Dict[str, str]:
        """``systemctl is-active u1 u2 ...`` -> {unit: active|inactive|failed|...}."""
        units = list(self.config.units)
        r = self._systemctl("is-active", *units)
        if r is None:
            return {u: "unknown" for u in units}
        lines = (r.stdout or "").splitlines()
        out: Dict[str, str] = {}
        for i, u in enumerate(units):
            out[u] = lines[i].strip() if i < len(lines) and lines[i].strip() else "unknown"
        return out

    def _target_active(self) -> bool:
        """True when the target or any of its units is up (a target only turns
        ``active`` once every wanted unit finished activating, so a backend
        restart during the stack's start-up must still adopt it)."""
        r = self._systemctl("is-active", self.config.target, *self.config.units)
        if r is None:
            return False
        states = [ln.strip() for ln in (r.stdout or "").splitlines()]
        return any(st in ("active", "activating", "reloading") for st in states)

    def _set_camera(self, on: bool) -> None:
        if not self.config.camera_relay:
            return
        if self._camera is None:
            self._camera_power = on
            return
        try:
            self._camera.set(on)
            self._camera_power = on
            logger.info("roadsense: camera hub power -> %s", "ON" if on else "OFF")
        except Exception:
            self._last_error = "camera relay %s failed" % ("on" if on else "off")
            logger.exception("roadsense: camera hub power %s failed", "on" if on else "off")

    @property
    def health_url(self) -> str:
        cfg = self.config
        return "http://%s:%d%s" % (cfg.api_host, cfg.api_port, cfg.health_path)

    def _fetch_health_now(self) -> Optional[dict]:
        return self._health_fetch(self.health_url, self.config.api_timeout_s)

    def _probe_health(self) -> None:
        """Refresh ``_api_ok`` / ``_health`` without blocking the engine loop."""
        if self._probe is not None:
            res, done_at = self._probe.poll()
            if done_at is None:
                return  # nothing completed yet; keep the previous view
        else:
            res = self._fetch_health_now()
        ok = isinstance(res, dict)
        self._api_ok = ok
        if ok:
            self._health = {k: res.get(k) for k in _HEALTH_KEYS if k in res}
            if isinstance(res.get("daemons"), dict):
                self._health["daemons"] = {
                    n: bool((d or {}).get("connected")) if isinstance(d, dict) else bool(d)
                    for n, d in res["daemons"].items()
                }
            self._last_api_ok = time.time()
            self._api_silent_since = None
        else:
            self._health = None

    def _sample_thermals(self) -> None:
        try:
            self._temps = dict(self._thermals() or {})
        except Exception:
            self._temps = {}

    # ── operator interface ───────────────────────────────────────────────

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> bool:
        """Set the operator mode (``auto`` / ``on`` / ``off``). Returns True on change."""
        mode = (mode or "").strip().lower()
        if mode not in MODES:
            raise ValueError("roadsense mode must be one of %s" % ", ".join(MODES))
        if mode == self._mode:
            return False
        logger.info("roadsense: mode %s -> %s", self._mode, mode)
        self._mode = mode
        self._last_tick = 0.0  # react on the next loop iteration
        return True

    def request_restart(self) -> None:
        """Restart the stack on the next tick (only acted on while it runs)."""
        self._restart_requested = True
        self._last_tick = 0.0

    def is_off(self) -> bool:
        """True when no roadsense unit is meant to run (safe for the low profile).

        PREVIEW counts as off: only the camera hub is powered."""
        return self._state in (STATE_OFF, STATE_DISABLED, STATE_PREVIEW)

    def holds_camera(self) -> bool:
        """True while this manager owns the camera hub relay (stack phases and
        PREVIEW): a manual ``set_relay`` on ch4 must be refused then."""
        return self._state not in (STATE_OFF, STATE_DISABLED)

    # ── preview hold (backend.camera_preview) ────────────────────────────

    @property
    def preview_hold(self) -> bool:
        return self._preview_hold

    def set_preview_hold(self, on: bool) -> bool:
        """Ask for the camera hub without the stack (dashboard preview).
        Returns True on change; the next :meth:`tick` acts on it."""
        on = bool(on)
        if on == self._preview_hold:
            return False
        logger.info("roadsense: preview hold %s", "ON" if on else "off")
        self._preview_hold = on
        self._last_tick = 0.0  # react on the next loop iteration
        return True

    # ── early camera power ───────────────────────────────────────────────

    def set_early_camera(self, on: bool) -> None:
        """Early start demand (the same BEAN "doors unlocked" signal that boots
        the MFD before ACC): power the camera hub now, without the stack, for
        at most ``early_camera_s``.

        Why: the dashcam (USB 1902:3241) drops off the bus once, 23-39 s after
        it gets power, and comes back 4-5 s later, or 122 s later on the first
        drive after a long stand (journals of 2026-11-16 / 17: both such drives
        lost the road picture from 0:45 to 2:55 after ACC on). Nothing on the
        phone can shorten that; powering the camera when the door is unlocked
        moves the whole sequence ahead of the drive by the time between the
        door and the key. It is the PREVIEW state (hub on, no units), so the
        stack starts without the settle wait when ACC comes."""
        on = bool(on) and self.config.early_camera_s > 0 and self.config.camera_relay
        if on and not self._early_seen:
            self._early_until = self._clock() + self.config.early_camera_s
            logger.info("roadsense: early camera power (at most %.0f s)", self.config.early_camera_s)
            self._last_tick = 0.0
        elif not on and self._early_seen:
            self._early_until = None
        self._early_seen = on

    def _early_camera(self) -> bool:
        return self._early_until is not None and self._clock() < self._early_until

    def shutdown_preview(self) -> None:
        """Backend shutdown: drop the hold and, if only the preview powers the
        camera hub, cut it now (the stack itself is left alone, as on every
        backend restart; the next start's reconcile adopts or cuts)."""
        self._preview_hold = False
        if self._state == STATE_PREVIEW:
            self._set_camera(False)
            self._enter(STATE_OFF)
            self._emit()

    def _release_preview_device(self) -> None:
        if self.preview_release is None:
            return
        try:
            self.preview_release()
        except Exception:  # noqa: BLE001 - never block the stack start
            logger.exception("roadsense: preview release hook failed")

    def is_active(self) -> bool:
        return not self.is_off()

    @property
    def state(self) -> str:
        return self._state

    # ── observability ─────────────────────────────────────────────────────

    def status(self) -> dict:
        hottest = max(self._temps.items(), key=lambda kv: kv[1]) if self._temps else None
        return {
            "state": self._state,
            "mode": self._mode,
            "desired": self._desired(bool(self._last_acc)),
            "acc_on": self._last_acc,
            "camera_power": self._camera_power if self.config.camera_relay else None,
            "api_ok": self._api_ok,
            "api_url": "http://%s:%d" % (self.config.api_host, self.config.api_port),
            "api_port": self.config.api_port,
            "health": self._health,
            "units": self._units,
            "temps": dict(self._temps),
            "hottest_zone": hottest[0] if hottest else None,
            "hottest_c": hottest[1] if hottest else None,
            "state_since": self._state_since,
            "last_api_ok": self._last_api_ok,
            "restarts": self._restarts,
            "last_error": self._last_error,
            "preview_hold": self._preview_hold,
            "deadline_in_s": (
                max(0.0, self._deadline - self._clock())
                if self._deadline is not None else None
            ),
        }

    def _emit(self) -> None:
        key = (self._state, self._mode, self._camera_power, self._api_ok,
               self._restarts, self._last_error, self._preview_hold,
               tuple(sorted((self._units or {}).items())))
        now = self._clock()
        if key == self._last_published and \
                (now - self._last_publish_t) < self.PUBLISH_REFRESH_S:
            return
        self._last_published = key
        self._last_publish_t = now
        if self._publish is not None:
            try:
                self._publish(self.status())
            except Exception:
                logger.exception("roadsense: status publish failed")

    def _enter(self, state: str, deadline_in: Optional[float] = None) -> None:
        if state != self._state:
            logger.info("roadsense: %s -> %s", self._state, state)
            self._state_since = time.time()
        self._state = state
        self._deadline = (self._clock() + deadline_in) if deadline_in is not None else None

    def _desired(self, acc_on: bool) -> bool:
        if self._mode == MODE_ON:
            return True
        if self._mode == MODE_OFF:
            return False
        return bool(acc_on)

    def _grace_for(self) -> float:
        # An explicit operator "off" skips the key-off grace.
        return 0.0 if self._mode == MODE_OFF else self.config.grace_s

    # ── state machine ─────────────────────────────────────────────────────

    def _begin_start(self) -> None:
        if self.config.camera_relay:
            self._set_camera(True)
            self._enter(STATE_CAMERA_ON, self.config.camera_settle_s)
        else:
            self._release_preview_device()
            self._start_target()
            self._enter(STATE_STARTING, self.config.start_timeout_s)

    def _enter_preview(self) -> None:
        self._set_camera(True)
        self._enter(STATE_PREVIEW)

    def _begin_stop(self) -> None:
        self._stop_target()
        self._enter(STATE_STOPPING, self.config.stop_timeout_s)

    def _finish_stop(self) -> None:
        if self._preview_hold:
            self._set_camera(True)   # the preview keeps the camera hub
        else:
            self._set_camera(False)
        self._api_ok = None
        self._health = None
        self._api_silent_since = None
        if self._probe is not None:
            self._probe.reset()
        self._enter(STATE_PREVIEW if self._preview_hold else STATE_OFF)

    def tick(self, acc_on: bool, acc_known: bool = True) -> None:
        """Advance the manager. ``acc_known`` is False until the powerbox has
        reported ACC since this backend started (only the first reconcile
        uses it)."""
        now = self._clock()
        if (now - self._last_tick) < self.config.tick_s:
            return
        self._last_tick = now
        self._last_acc = bool(acc_on)
        cfg = self.config
        desired = self._desired(acc_on)

        self._sample_thermals()

        if not self._reconciled:
            if (not desired and self._mode == MODE_AUTO and not acc_known
                    and now - self._born < cfg.acc_wait_s):
                # ACC not reported yet: decide nothing (a running stack may be
                # a legitimate one while driving).
                self._emit()
                return
            self._reconciled = True
            if not acc_known and not desired and self._mode == MODE_AUTO:
                logger.warning("roadsense: no ACC report %.0fs after start; "
                               "treating ACC as off", cfg.acc_wait_s)
            if self._target_active():
                if desired:
                    # Backend restart while the stack is legitimately up:
                    # adopt it; the hub is already powered (set is idempotent).
                    logger.info("roadsense: adopting the already-running stack")
                    if cfg.camera_relay:
                        self._set_camera(True)
                    self._enter(STATE_RUNNING)
                else:
                    # Not wanted (ACC off in mode auto, or mode off): units
                    # started by hand or left over. Stop them without ever
                    # powering the camera hub; _finish_stop cuts it.
                    logger.warning("roadsense: stack running while not wanted "
                                   "(acc_on=%s mode=%s): stopping it, camera hub "
                                   "not switched on", bool(acc_on), self._mode)
                    self._begin_stop()
            elif not desired:
                self._set_camera(False)

        if self._state == STATE_OFF:
            self._units = None
            if desired:
                self._begin_start()
            elif self._preview_hold or self._early_camera():
                self._enter_preview()

        elif self._state == STATE_PREVIEW:
            self._units = None
            if desired:
                # The stack wins: the preview's reader lets go of the device,
                # the hub is already powered and enumerated (no settle).
                self._release_preview_device()
                self._set_camera(True)
                self._start_target()
                self._enter(STATE_STARTING, cfg.start_timeout_s)
            elif not self._preview_hold and not self._early_camera():
                self._set_camera(False)
                self._enter(STATE_OFF)

        elif self._state == STATE_CAMERA_ON:
            if not desired:
                self._finish_stop()
            elif now >= (self._deadline or 0):
                self._release_preview_device()
                self._start_target()
                self._enter(STATE_STARTING, cfg.start_timeout_s)

        elif self._state == STATE_STARTING:
            self._units = self._query_units()
            self._probe_health()
            if self._api_ok:
                self._enter(STATE_RUNNING)
            elif now >= (self._deadline or 0):
                logger.warning("roadsense: API not answering %.0fs after start; "
                               "running degraded (units: %s)", cfg.start_timeout_s,
                               ", ".join("%s=%s" % kv for kv in (self._units or {}).items()))
                self._api_silent_since = now
                self._enter(STATE_RUNNING)
            if not desired and self._state != STATE_RUNNING:
                self._enter(STATE_GRACE, self._grace_for())

        elif self._state == STATE_RUNNING:
            self._units = self._query_units()
            self._probe_health()
            if self._restart_requested:
                self._restart_requested = False
                self._restart_target("operator request")
            elif self._api_ok is False:
                if self._api_silent_since is None:
                    self._api_silent_since = now
                elif (cfg.api_dead_restart_s > 0
                      and now - self._api_silent_since >= cfg.api_dead_restart_s
                      and (self._last_restart is None
                           or now - self._last_restart >= cfg.restart_cooldown_s)):
                    self._restart_target("API silent for %.0fs" % (now - self._api_silent_since))
            if not desired:
                self._enter(STATE_GRACE, self._grace_for())

        elif self._state == STATE_GRACE:
            self._restart_requested = False
            if desired:
                logger.info("roadsense: wanted again during grace, staying up")
                self._enter(STATE_RUNNING)
            elif now >= (self._deadline or 0):
                self._begin_stop()
            else:
                self._units = self._query_units()
                self._probe_health()

        elif self._state == STATE_STOPPING:
            # A key-on now is deliberately ignored until the stop completes;
            # OFF restarts the stack on the next tick if still wanted.
            self._units = self._query_units()
            still_up = [u for u, s in self._units.items()
                        if s in ("active", "activating", "deactivating", "reloading")]
            if not still_up:
                self._finish_stop()
            elif now >= (self._deadline or 0):
                logger.error("roadsense: units still up %.0fs after stop: %s",
                             cfg.stop_timeout_s, ", ".join(still_up))
                self._last_error = "stop timeout: " + ", ".join(still_up)
                self._finish_stop()

        self._emit()
