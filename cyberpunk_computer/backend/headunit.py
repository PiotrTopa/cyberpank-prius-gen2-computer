"""
Head unit audio control over adb (docs/HEADUNIT_INTEGRATION.md §8).

The Android head unit hangs on the phone's USB (camera hub, relay ch4). The
adb server on the phone belongs to ``prius-gnss-bridge.service`` (tcp
127.0.0.1:5037, §7.4). This module NEVER spawns the ``adb`` binary (a second
server started as root wedged the unit's link on 2026-11-17) and never kills
the server: it speaks the adb smart-socket protocol to the running server
directly (:class:`AdbClient`).

Only the cheap binder forms run on the unit (30–40 ms each)::

    dumpsys audio                                    state (volume 0..63, mute)
    cmd activity broadcast -a xy.android.gtpkey.*    volume / mute / media keys
    cmd activity start-foreground-service -n pl.topa.priusagent/.AlertService ...

All adb work runs on one worker thread (:class:`HeadunitController`); the
engine thread only queues requests (volume coalesced, latest wins) and reads
a status snapshot. A missing server or no device in state ``device`` means
the unit is simply absent (normal with ACC off): edges are logged, nothing
else.

Alert sounds: :class:`HeadunitSoundSink` plugs into the alert framework's
sound sink interface (alerts/sinks.py) and plays a tone preset per sound id /
severity through the agent app, rate-limited, only while the unit is present.
"""

from __future__ import annotations

import collections
import logging
import re
import shlex
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── unit facts (one place; §8.1) ─────────────────────────────────────────────

VOLUME_MAX = 63
BROADCAST_VOLSET = "xy.android.gtpkey.volset"
BROADCAST_VOLADD = "xy.android.gtpkey.voladd"
BROADCAST_VOLDEC = "xy.android.gtpkey.voldec"
BROADCAST_MUTE = "xy.android.gtpkey.mute"          # a toggle; there is no absolute form

#: media key -> broadcast action. Names from the vendor framework, NOT yet
#: exercised on the unit (§8.1): change here if one does nothing.
MEDIA_KEYS: Dict[str, str] = {
    "play_pause": "xy.android.gtpkey.playpause",
    "next": "xy.android.gtpkey.frd",
    "prev": "xy.android.gtpkey.rev",
}

AGENT_COMPONENT = "pl.topa.priusagent/.AlertService"

#: Named alert sounds (tone = "frequency_hz:duration_ms,..." with 0 = pause).
#: Short and soft on purpose: the system informs, it must not startle.
PRESETS: Dict[str, Dict[str, Any]] = {
    "info": {"tone": "880:120", "gain": 0.5},
    "chime": {"tone": "660:120,0:60,880:180", "gain": 0.6},
    "notice": {"tone": "880:150,0:80,1320:200", "gain": 0.7},
    "warning": {"tone": "1320:120,0:70,1320:120", "gain": 0.8},
    "critical": {"tone": "1320:150,0:60,1320:150,0:60,1320:150", "gain": 0.8},
}
#: alert framework sound id (alerts/sinks.py SOUND_IDS) -> preset
ALERT_SOUND_PRESETS: Dict[str, str] = {"soft": "chime", "notice": "notice", "warning": "warning"}
#: alert severity (alerts/model.py SEVERITIES) -> preset, when the sound id is unknown
SEVERITY_PRESETS: Dict[str, str] = {"info": "info", "notice": "notice", "warning": "warning"}

GAIN_MAX = 0.8          # never louder than this from the backend (no auto volume raising either)

DEFAULT_SERIAL = "MJU8DY59WS8LMB85"


class AdbError(Exception):
    """adb protocol / command failure."""


class AdbAbsent(AdbError):
    """No adb server listening or no device in state ``device``."""


# ── command text (pure, unit-tested) ─────────────────────────────────────────

_TONE_RE = re.compile(r"^\d{1,5}:\d{1,5}(,\d{1,5}:\d{1,5}){0,63}$")
_LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,48}$")
_FILE_RE = re.compile(r"^/[A-Za-z0-9_./-]{1,200}$")


def broadcast_cmd(action: str, extras: str = "") -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.]+", action):
        raise ValueError("bad broadcast action %r" % action)
    return "cmd activity broadcast -a %s%s" % (action, (" " + extras) if extras else "")


def volset_cmd(level: int) -> str:
    """Absolute volume. Always with the extra (without it the unit sets 20;
    100/101 are DSP switches, not levels)."""
    if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= VOLUME_MAX:
        raise ValueError("volume must be an int 0..%d" % VOLUME_MAX)
    return broadcast_cmd(BROADCAST_VOLSET, "--ei volset %d" % level)


def media_cmd(key: str) -> str:
    if key not in MEDIA_KEYS:
        raise ValueError("media key must be one of %s" % ", ".join(MEDIA_KEYS))
    return broadcast_cmd(MEDIA_KEYS[key])


def check_text(text: str, max_len: int = 300) -> str:
    """Free text for speech: no control characters, bounded length."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")
    if len(text) > max_len:
        raise ValueError("text longer than %d characters" % max_len)
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise ValueError("text must not contain control characters")
    return text


def alert_cmd(tone: Optional[str] = None, say: Optional[str] = None, lang: Optional[str] = None,
              gain: Optional[float] = None, alert_id: Optional[str] = None,
              file: Optional[str] = None) -> str:
    """``cmd activity start-foreground-service`` intent for the agent app.
    Every value is validated and shell-quoted (the remote shell parses it)."""
    parts: List[str] = ["cmd", "activity", "start-foreground-service", "-n", AGENT_COMPONENT]
    if not (tone or say or file):
        raise ValueError("an alert needs tone, say or file")
    if tone:
        if not _TONE_RE.match(tone):
            raise ValueError("tone must be 'hz:ms,hz:ms,...'")
        if sum(int(p.split(":")[1]) for p in tone.split(",")) > 15000:
            raise ValueError("tone longer than 15 s")
        parts += ["--es", "tone", shlex.quote(tone)]
    if file:
        if not _FILE_RE.match(file) or ".." in file:
            raise ValueError("file must be an absolute path on the unit")
        parts += ["--es", "file", shlex.quote(file)]
    if say:
        parts += ["--es", "say", shlex.quote(check_text(say))]
        if lang:
            if not _LANG_RE.match(lang):
                raise ValueError("lang must look like 'pl' or 'en-GB'")
            parts += ["--es", "lang", shlex.quote(lang)]
    if gain is not None:
        g = float(gain)
        if not 0.0 <= g <= 1.0:
            raise ValueError("gain must be 0..1")
        parts += ["--ef", "gain", "%.2f" % min(GAIN_MAX, g)]
    if alert_id:
        if not _ID_RE.match(alert_id):
            raise ValueError("id must be 1..48 of [A-Za-z0-9_.:-]")
        parts += ["--es", "id", shlex.quote(alert_id)]
    return " ".join(parts)


def alert_stop_cmd() -> str:
    return "cmd activity start-foreground-service -n %s --ez stop true" % AGENT_COMPONENT


_CURRENT_RE = re.compile(r"^\s*Current:\s*[0-9a-fA-Fx]+\s*\([^)]*\):\s*(\d+)")
_MUTED_RE = re.compile(r"^\s*Muted:\s*(true|false)\s*$")


def parse_dumpsys_audio(text: str) -> Tuple[Optional[int], Optional[bool]]:
    """(volume 0..63, muted) from the ``- STREAM_MUSIC:`` block of
    ``dumpsys audio``; None for what is not found."""
    vol: Optional[int] = None
    muted: Optional[bool] = None
    inside = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("- STREAM_"):
            if inside:
                break
            inside = s.startswith("- STREAM_MUSIC:")
            continue
        if not inside:
            continue
        if muted is None:
            m = _MUTED_RE.match(line)
            if m:
                muted = m.group(1) == "true"
                continue
        if vol is None:
            m = _CURRENT_RE.match(line)
            if m:
                vol = int(m.group(1))
    return vol, muted


# ── adb smart-socket client ──────────────────────────────────────────────────

class AdbClient:
    """Minimal client of a running adb server (never starts or kills one)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 5037, serial: Optional[str] = None,
                 connect_timeout: float = 0.5, cmd_timeout: float = 3.0,
                 max_output: int = 2 * 1024 * 1024) -> None:
        self.host = host
        self.port = port
        self.serial = serial
        self.connect_timeout = connect_timeout
        self.cmd_timeout = cmd_timeout
        self.max_output = max_output

    def _open(self) -> socket.socket:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except OSError as exc:
            raise AdbAbsent("adb server not listening (%s)" % exc)
        sock.settimeout(self.cmd_timeout)
        return sock

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise AdbError("connection closed by the adb server")
            buf += chunk
        return buf

    def _request(self, sock: socket.socket, payload: str) -> None:
        data = payload.encode("utf-8")
        if len(data) > 0xFFFF:
            raise AdbError("request too long")
        sock.sendall(b"%04x" % len(data) + data)
        status = self._recv_exact(sock, 4)
        if status == b"OKAY":
            return
        if status == b"FAIL":
            n = int(self._recv_exact(sock, 4), 16)
            msg = self._recv_exact(sock, n).decode("utf-8", "replace")
            raise AdbError(msg)
        raise AdbError("bad adb status %r" % status)

    def devices(self) -> List[Tuple[str, str]]:
        sock = self._open()
        try:
            self._request(sock, "host:devices")
            n = int(self._recv_exact(sock, 4), 16)
            body = self._recv_exact(sock, n).decode("utf-8", "replace") if n else ""
        except socket.timeout:
            raise AdbError("adb server timeout")
        finally:
            sock.close()
        out = []
        for line in body.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                out.append((parts[0], parts[1]))
        return out

    def pick_device(self) -> str:
        """Serial of the device to talk to (configured serial first, else the
        first in state ``device``); AdbAbsent when there is none."""
        devs = [s for s, st in self.devices() if st == "device"]
        if not devs:
            raise AdbAbsent("no device in state 'device'")
        if self.serial and self.serial in devs:
            return self.serial
        return devs[0]

    def shell(self, serial: str, cmd: str) -> str:
        sock = self._open()
        try:
            try:
                self._request(sock, "host:transport:%s" % serial)
            except AdbError as exc:
                raise AdbAbsent("transport: %s" % exc)
            self._request(sock, "shell:%s" % cmd)
            chunks = []
            total = 0
            deadline = time.monotonic() + self.cmd_timeout
            while True:
                if time.monotonic() > deadline:
                    raise AdbError("shell command timeout")
                chunk = sock.recv(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > self.max_output:
                    raise AdbError("shell output too long")
                chunks.append(chunk)
        except socket.timeout:
            raise AdbError("shell command timeout")
        finally:
            sock.close()
        return b"".join(chunks).decode("utf-8", "replace").replace("\r\n", "\n")


# ── controller (worker thread) ───────────────────────────────────────────────

@dataclass
class HeadunitConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 5037
    serial: Optional[str] = DEFAULT_SERIAL
    connect_timeout_s: float = 0.5
    cmd_timeout_s: float = 3.0
    poll_present_s: float = 2.0
    poll_absent_s: float = 5.0
    alert_sounds: bool = False
    alert_min_gap_s: float = 3.0
    alert_per_id_gap_s: float = 30.0


class HeadunitController:
    """Owns the adb worker thread. Public methods are non-blocking."""

    def __init__(self, config: HeadunitConfig, client: Optional[AdbClient] = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self.config = config
        self.client = client or AdbClient(config.host, config.port, config.serial,
                                          config.connect_timeout_s, config.cmd_timeout_s)
        self._clock = clock
        self._wall = wall
        self._cv = threading.Condition()
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        # pending requests (under _cv)
        self._vol_target: Optional[int] = None
        self._vol_delta: int = 0
        self._mute_want: Optional[Any] = None       # True / False / "toggle"
        self._ops: Deque[Tuple[str, Dict[str, Any]]] = collections.deque(maxlen=8)
        self._poll_now = True
        # observed state (under _lock)
        self._lock = threading.Lock()
        self._present = False
        self._serial = ""
        self._volume: Optional[int] = None
        self._muted: Optional[bool] = None
        self._last_seen = 0.0
        self._agent: Dict[str, Any] = {}
        self._error = ""
        self._counters: Dict[str, int] = {"commands": 0, "polls": 0, "errors": 0,
                                          "alerts": 0, "absent_edges": 0}
        self._version = 0

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="headunit-adb", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    # ── requests (any thread, non-blocking) ──────────────────────────────────

    @property
    def present(self) -> bool:
        with self._lock:
            return self._present

    def set_volume(self, level: int) -> None:
        level = max(0, min(VOLUME_MAX, int(level)))
        with self._cv:
            self._vol_target = level
            self._vol_delta = 0
            self._cv.notify_all()

    def step_volume(self, delta: int) -> None:
        delta = max(-VOLUME_MAX, min(VOLUME_MAX, int(delta)))
        with self._cv:
            if self._vol_target is not None:
                self._vol_target = max(0, min(VOLUME_MAX, self._vol_target + delta))
            else:
                self._vol_delta += delta
            self._cv.notify_all()

    def set_mute(self, want: Any) -> None:
        """True / False (absolute: read, toggle if different, read back) or "toggle"."""
        if want not in (True, False, "toggle"):
            raise ValueError("mute must be true, false or 'toggle'")
        with self._cv:
            self._mute_want = want
            self._cv.notify_all()

    def media(self, key: str) -> None:
        media_cmd(key)                       # validate now (raises ValueError)
        self._enqueue("media", {"key": key})

    def alert(self, tone: Optional[str] = None, say: Optional[str] = None,
              lang: Optional[str] = None, gain: Optional[float] = None,
              alert_id: Optional[str] = None, file: Optional[str] = None) -> None:
        kw = dict(tone=tone, say=say, lang=lang, gain=gain, alert_id=alert_id, file=file)
        alert_cmd(**kw)                      # validate now (raises ValueError)
        self._enqueue("alert", kw)

    def alert_stop(self) -> None:
        self._enqueue("alert_stop", {})

    def _enqueue(self, op: str, params: Dict[str, Any]) -> None:
        with self._cv:
            self._ops.append((op, params))
            self._cv.notify_all()

    # ── status ───────────────────────────────────────────────────────────────

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": True, "present": self._present, "serial": self._serial,
                "volume": self._volume, "muted": self._muted,
                "last_seen": self._last_seen, "agent": dict(self._agent),
                "error": self._error, "counters": dict(self._counters),
            }

    def _bump(self, **changes: Any) -> None:
        with self._lock:
            for k, v in changes.items():
                setattr(self, "_" + k, v)
            self._version += 1

    def _count(self, name: str) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + 1
            self._version += 1

    # ── worker ───────────────────────────────────────────────────────────────

    def _has_work(self) -> bool:
        return (self._vol_target is not None or self._vol_delta != 0
                or self._mute_want is not None or bool(self._ops))

    def _run(self) -> None:
        next_poll = 0.0
        while True:
            with self._cv:
                while not self._stop and not self._has_work() and not self._poll_now:
                    wait = next_poll - self._clock()
                    if wait <= 0:
                        break
                    self._cv.wait(wait)
                if self._stop:
                    return
                vol_target, self._vol_target = self._vol_target, None
                vol_delta, self._vol_delta = self._vol_delta, 0
                mute_want, self._mute_want = self._mute_want, None
                ops = list(self._ops)
                self._ops.clear()
                self._poll_now = False
            try:
                self.cycle(vol_target, vol_delta, mute_want, ops)
            except Exception:                 # never let the worker die
                logger.exception("head unit worker cycle failed")
            period = self.config.poll_present_s if self.present else self.config.poll_absent_s
            next_poll = self._clock() + period

    def cycle(self, vol_target: Optional[int] = None, vol_delta: int = 0,
              mute_want: Any = None, ops: Optional[List[Tuple[str, Dict[str, Any]]]] = None) -> None:
        """One worker pass: find the device, run pending commands, read state.
        Synchronous (the tests call it directly)."""
        ops = ops or []
        try:
            serial = self.client.pick_device()
        except AdbAbsent as exc:
            self._set_absent(str(exc))
            if vol_target is not None or vol_delta or mute_want is not None or ops:
                logger.debug("head unit absent: request dropped")
            return
        except AdbError as exc:
            self._set_absent("adb: %s" % exc)
            return
        try:
            if not self.present:
                logger.info("head unit present (adb %s)", serial)
                self._bump(present=True, serial=serial, error="")
            did = False
            failed = False
            if vol_target is None and vol_delta:
                vol, _m = self._read(serial)
                if vol is not None:
                    vol_target = max(0, min(VOLUME_MAX, vol + vol_delta))
                else:
                    action = BROADCAST_VOLADD if vol_delta > 0 else BROADCAST_VOLDEC
                    for _ in range(min(5, abs(vol_delta))):
                        self._sh(serial, broadcast_cmd(action))
                    did = True
            if vol_target is not None:
                self._sh(serial, volset_cmd(vol_target))
                did = True
            if mute_want is not None:
                if mute_want == "toggle":
                    self._sh(serial, broadcast_cmd(BROADCAST_MUTE))
                else:
                    _v, muted = self._read(serial)
                    if muted is None:
                        raise AdbError("mute state not readable")
                    if muted != mute_want:
                        self._sh(serial, broadcast_cmd(BROADCAST_MUTE))
                        _v, muted = self._read(serial)
                        if muted != mute_want:
                            self._bump(error="mute did not follow (wanted %s)" % mute_want)
                            self._count("errors")
                            failed = True
                did = True
            for op, params in ops:
                if op == "media":
                    self._sh(serial, media_cmd(params["key"]))
                elif op == "alert":
                    self._sh(serial, alert_cmd(**params))
                    self._count("alerts")
                    self._bump(agent={"ts": self._wall(), "id": params.get("alert_id"),
                                      "tone": params.get("tone"), "say": bool(params.get("say")),
                                      "sent": True})
                elif op == "alert_stop":
                    self._sh(serial, alert_stop_cmd())
                did = True
            self._read(serial)                # state now (also right after commands)
            if did and not failed:
                self._bump(error="")
        except AdbAbsent as exc:
            self._set_absent(str(exc))
        except (AdbError, ValueError) as exc:
            self._count("errors")
            self._bump(error=str(exc))
            logger.warning("head unit: %s", exc)

    def _sh(self, serial: str, cmd: str) -> str:
        out = self.client.shell(serial, cmd)
        self._count("commands")
        logger.debug("head unit $ %s", cmd)
        return out

    def _read(self, serial: str) -> Tuple[Optional[int], Optional[bool]]:
        text = self.client.shell(serial, "dumpsys audio")
        vol, muted = parse_dumpsys_audio(text)
        with self._lock:
            self._counters["polls"] = self._counters.get("polls", 0) + 1
            self._last_seen = self._wall()
            if vol is not None:
                self._volume = vol
            if muted is not None:
                self._muted = muted
            self._version += 1
        return vol, muted

    def _set_absent(self, why: str) -> None:
        if self.present:
            logger.info("head unit absent (%s)", why)
            self._count("absent_edges")
        with self._lock:
            changed = self._present or self._error != ""
            self._present = False
            self._error = ""
            if changed:
                self._version += 1


# ── alert framework sound sink ───────────────────────────────────────────────

def preset_for(sound_id: Optional[str], severity: Optional[str]) -> str:
    if sound_id in PRESETS:
        return sound_id
    if sound_id in ALERT_SOUND_PRESETS:
        return ALERT_SOUND_PRESETS[sound_id]
    return SEVERITY_PRESETS.get(severity or "", "info")


class HeadunitSoundSink:
    """alerts/sinks.py SoundSink that plays presets through the agent app.

    At most one sound per alert id within ``per_id_gap_s``, a global gap of
    ``min_gap_s``, nothing while the unit is absent. ``play`` only queues."""

    name = "headunit"

    def __init__(self, controller: HeadunitController, min_gap_s: float = 3.0,
                 per_id_gap_s: float = 30.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.controller = controller
        self.min_gap_s = min_gap_s
        self.per_id_gap_s = per_id_gap_s
        self._clock = clock
        self._last = -1e9
        self._last_by_id: Dict[str, float] = {}
        self.played = 0
        self.skipped: Dict[str, int] = {"absent": 0, "rate": 0, "same_id": 0}
        self.last: Dict[str, Any] = {}

    def play(self, sound_id: str, severity: str, alert_id: str) -> bool:
        now = self._clock()
        if not self.controller.present:
            self.skipped["absent"] += 1
            return False
        if now - self._last_by_id.get(alert_id, -1e9) < self.per_id_gap_s:
            self.skipped["same_id"] += 1
            return False
        if now - self._last < self.min_gap_s:
            self.skipped["rate"] += 1
            return False
        name = preset_for(sound_id, severity)
        p = PRESETS[name]
        try:
            self.controller.alert(tone=p["tone"], gain=min(GAIN_MAX, p.get("gain", GAIN_MAX)),
                                  alert_id=_safe_id(alert_id))
        except ValueError:
            logger.exception("alert sound preset %s invalid", name)
            return False
        self._last = now
        self._last_by_id[alert_id] = now
        self.played += 1
        self.last = {"ts": time.time(), "alert": alert_id, "preset": name}
        return True

    def stop(self, alert_id: str) -> None:
        """Sounds are short; an alert clearing does not cut them off."""

    def status(self) -> Dict[str, Any]:
        return {"sink": self.name, "available": self.controller.present, "played": self.played,
                "skipped": dict(self.skipped), "last": dict(self.last),
                "min_gap_s": self.min_gap_s, "per_id_gap_s": self.per_id_gap_s}


def _safe_id(alert_id: str) -> Optional[str]:
    s = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(alert_id or ""))[:48]
    return s or None
