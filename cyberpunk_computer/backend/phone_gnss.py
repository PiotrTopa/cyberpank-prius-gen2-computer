"""
Phone GNSS source for the position feeder: the Poco F1's own receiver.

On the SDM845 with the mainline kernel the GNSS engine sits in the modem and is
reached only through QMI LOC, whose client is ModemManager (``qcom-soc``
plugin, port ``qrtr0``). There is no ``/dev/gnss*``, no gpsd and no
``gps-unmanaged`` capability; a second QMI client (qmicli, gpsd) would fight
ModemManager for the modem that also carries the LTE link. So this source asks
ModemManager over D-Bus (``org.freedesktop.ModemManager1.Modem.Location``):

    phone-gnss thread (only while wanted):
        Location.Setup(enabled | GPS_NMEA)          once, and again after an MM restart
        Location.SetGpsRefreshRate(0)               MM passes every NMEA burst on
        Location.GetLocation() every 1/poll_hz s    -> NMEA trace -> fix -> queue
    feeder thread: drain() / has_fix() / status()   never block (queue + lock)

Facts read on the phone on 2026-11-15 (docs/POSITION_FEEDER.md section 11):
capabilities gps-raw, gps-nmea, agps-msa, agps-msb; NMEA talkers ``GP``,
``GA``, ``GN`` plus ``$PSTIS``; GST not in the modem's NMEA type list (so
``hacc_m`` = HDOP x ``uere_m``, factor not calibrated: no fix was obtained);
no XTRA injection through ModemManager (``SupportedAssistanceData`` = 0).
In the car (phone in its box) the engine tracked no satellite in 20 min.

Backends: :class:`DbusModemManager` (dbus-python, the system package
``py3-dbus``; one in-process call per poll) and :class:`MmcliModemManager`
(``mmcli`` subprocesses, only when dbus-python is missing; polls at most 1 Hz).
Tests inject a fake with the same methods.

Rules:

* GNSS is switched on only while the feeder wants it (roadsense starting /
  running / grace) and off ``linger_s`` after that ends. Only the bits this
  source added are cleared again; a GNSS the owner enabled by hand stays on.
  The GPS refresh rate is restored to what it was.
* The modem, its bearers, the SIM and the ModemManager / NetworkManager
  services are never touched: only ``Location.Setup`` with the existing
  sources kept, and ``SetGpsRefreshRate``.
* ModemManager restarts or a modem reset (netwatch) make the modem object path
  change and drop the location setup: every error re-resolves the modem after
  ``retry_s`` and the setup is checked again every ``check_s``.
* Positions are never logged above DEBUG.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import threading
import time
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PHONE_TAG = "phone"

# MMModemLocationSource bits
MM_LOC_3GPP = 1
MM_LOC_GPS_RAW = 2
MM_LOC_GPS_NMEA = 4

KNOTS = 0.514444
# Talker preference when several constellations report the same sentence type:
# GN (combined solution) first, then GPS, then the others.
TALKERS = ("GN", "GP", "GL", "GA", "GB", "BD", "GQ")
CLOCK_SYNCED_FLAGS = ("/run/systemd/timesync/synchronized", "/run/prius-clock-synced")


# ── NMEA (pure) ──────────────────────────────────────────────────────────────

def nmea_checksum_ok(line: str) -> bool:
    """True for ``$...*hh`` with a matching XOR checksum."""
    line = line.strip()
    if len(line) < 4 or line[0] not in "$!" or "*" not in line:
        return False
    body, _, cs = line[1:].rpartition("*")
    if len(cs) < 2:
        return False
    x = 0
    for ch in body:
        x ^= ord(ch)
    try:
        return x == int(cs[:2], 16)
    except ValueError:
        return False


def split_trace(text: Any) -> Dict[str, Dict[str, List[str]]]:
    """NMEA text (str, or a list of lines) -> ``{type: {talker: fields}}``.
    Lines with a bad checksum or a short address are skipped; for repeated
    sentences (GSV pages) every page is kept under ``type`` as ``talker#n``."""
    lines = text if isinstance(text, (list, tuple)) else str(text or "").replace("\r", "\n").split("\n")
    out: Dict[str, Dict[str, List[str]]] = {}
    for raw in lines:
        if not isinstance(raw, str):
            continue
        line = raw.strip()
        if not line or not nmea_checksum_ok(line):
            continue
        fields = line[1:line.rindex("*")].split(",")
        addr = fields[0]
        if len(addr) != 5:
            continue
        talker, kind = addr[:2], addr[2:]
        bucket = out.setdefault(kind, {})
        key = talker
        n = 1
        while key in bucket:
            n += 1
            key = "%s#%d" % (talker, n)
        bucket[key] = fields
    return out


def _f(s: str) -> Optional[float]:
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _i(s: str) -> Optional[int]:
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _latlon(v: str, hemi: str, deg_digits: int) -> Optional[float]:
    if not v or hemi not in ("N", "S", "E", "W") or len(v) < deg_digits + 2:
        return None
    d, m = _f(v[:deg_digits]), _f(v[deg_digits:])
    if d is None or m is None or m >= 60.0:
        return None
    x = d + m / 60.0
    return -x if hemi in ("S", "W") else x


def _tod(s: str) -> Optional[float]:
    """hhmmss[.sss] -> seconds of the day."""
    if len(s) < 6:
        return None
    h, m, sec = _i(s[:2]), _i(s[2:4]), _f(s[4:])
    if h is None or m is None or sec is None or h > 23 or m > 59 or sec >= 61.0:
        return None
    return h * 3600 + m * 60 + sec


def _pick(bucket: Optional[Dict[str, List[str]]]) -> Optional[List[str]]:
    if not bucket:
        return None
    for t in TALKERS:
        if t in bucket:
            return bucket[t]
    return next(iter(bucket.values()))


@dataclass
class NmeaFix:
    """One epoch combined from GGA / RMC / GSA / GST / GSV of a trace."""
    tod_s: Optional[float]               # UTC seconds of the day (GGA, else RMC); None while searching
    fix: str                             # none | 2d | 3d | dgps
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_m: Optional[float] = None
    sats: Optional[int] = None           # used in the solution (GGA)
    hdop: Optional[float] = None
    pdop: Optional[float] = None
    speed_mps: Optional[float] = None
    course_deg: Optional[float] = None
    utc_ms: Optional[int] = None         # needs the RMC date
    std_lat_m: Optional[float] = None    # GST (absent on this modem)
    std_lon_m: Optional[float] = None
    sats_view: Optional[int] = None      # GSV, all constellations
    cn0: List[int] = field(default_factory=list)   # GSV C/N0 dB-Hz of tracked satellites


def parse_trace(text: Any) -> Optional[NmeaFix]:
    """The epoch in a ModemManager NMEA trace; None without a valid GGA / RMC.
    A receiver that has no time yet gives ``tod_s`` None and ``fix`` none.

    ``fix`` is ``none`` unless GGA quality > 0 or RMC status ``A`` says
    otherwise; position fields only with a valid fix. GGA and RMC must carry
    the same time to be combined (else GGA wins and RMC speed / date are not
    used)."""
    s = split_trace(text)
    gga, rmc = _pick(s.get("GGA")), _pick(s.get("RMC"))
    seen = gga is not None or rmc is not None
    tod = None
    if gga is not None and len(gga) >= 10:
        tod = _tod(gga[1])
    rmc_tod = _tod(rmc[1]) if rmc is not None and len(rmc) >= 10 else None
    if tod is None:
        tod = rmc_tod
        gga = None if gga is None or len(gga) < 10 or _tod(gga[1]) is None else gga
    if tod is None:
        if seen:
            return NmeaFix(tod_s=None, fix="none")      # engine running, no time yet (searching)
        return None
    rmc_ok = rmc is not None and rmc_tod is not None and abs(rmc_tod - tod) < 0.01
    out = NmeaFix(tod_s=tod, fix="none")
    q = _i(gga[6]) if gga is not None and len(gga) > 6 else None
    valid_gga = q is not None and q > 0 and q != 6        # 6 = dead reckoning estimate
    valid_rmc = rmc_ok and len(rmc) > 2 and rmc[2] == "A"
    if gga is not None and valid_gga:
        out.lat, out.lon = _latlon(gga[2], gga[3], 2), _latlon(gga[4], gga[5], 3)
        out.sats = _i(gga[7])
        out.hdop = _f(gga[8])
        out.alt_m = _f(gga[9])
    elif valid_rmc:
        out.lat, out.lon = _latlon(rmc[3], rmc[4], 2), _latlon(rmc[5], rmc[6], 3)
    if out.lat is None or out.lon is None or (abs(out.lat) < 1e-6 and abs(out.lon) < 1e-6):
        out.lat = out.lon = None
    else:
        dim = None
        for kind_bucket in (s.get("GSA") or {}).values():
            if len(kind_bucket) > 2:
                d = _i(kind_bucket[2])
                dim = d if dim is None or (d or 0) > dim else dim
                if out.pdop is None and len(kind_bucket) > 15:
                    out.pdop = _f(kind_bucket[15])
                if out.hdop is None and len(kind_bucket) > 16:
                    out.hdop = _f(kind_bucket[16])
        if q == 2:
            out.fix = "dgps"
        elif dim == 2:
            out.fix = "2d"
        else:
            out.fix = "3d"
    if rmc_ok:
        if out.fix != "none":
            sp = _f(rmc[7]) if len(rmc) > 7 else None
            if sp is not None and sp >= 0:
                out.speed_mps = sp * KNOTS
            co = _f(rmc[8]) if len(rmc) > 8 else None
            if co is not None and 0.0 <= co <= 360.0:
                out.course_deg = 0.0 if co >= 360.0 else co
        date = rmc[9] if len(rmc) > 9 else ""
        if len(date) == 6 and date.isdigit():
            try:
                d0 = datetime(2000 + int(date[4:6]), int(date[2:4]), int(date[0:2]), tzinfo=timezone.utc)
                out.utc_ms = int(round((d0.timestamp() + tod) * 1000))
            except ValueError:
                pass
    gst = _pick(s.get("GST"))
    if gst is not None and len(gst) >= 8 and _tod(gst[1]) is not None and abs(_tod(gst[1]) - tod) < 0.01:
        out.std_lat_m, out.std_lon_m = _f(gst[6]), _f(gst[7])
    view, cn0 = 0, []
    seen_talkers = set()
    for key, g in (s.get("GSV") or {}).items():
        if len(g) < 4:
            continue
        talker = key.split("#")[0]
        if talker not in seen_talkers:
            seen_talkers.add(talker)
            view += _i(g[3]) or 0
        for k in range(4, len(g) - 3, 4):
            c = _i(g[k + 3])
            if c is not None and c > 0:
                cn0.append(c)
    if seen_talkers:
        out.sats_view = view
        out.cn0 = sorted(cn0, reverse=True)
    return out


def hacc_from(fix: NmeaFix, uere_m: float, floor_m: float = 2.0) -> Optional[float]:
    """68 %-ish horizontal accuracy: GST sigmas (DRMS) when the receiver gives
    them, else HDOP x ``uere_m``; never below ``floor_m``."""
    if fix.std_lat_m is not None and fix.std_lon_m is not None:
        h = math.hypot(fix.std_lat_m, fix.std_lon_m)
    elif fix.hdop is not None and fix.hdop > 0:
        h = fix.hdop * uere_m
    else:
        return None
    return round(max(floor_m, h), 1)


# ── ModemManager access ──────────────────────────────────────────────────────

MM_BUS = "org.freedesktop.ModemManager1"
MM_PATH = "/org/freedesktop/ModemManager1"
MM_LOC = "org.freedesktop.ModemManager1.Modem.Location"


class DbusModemManager:
    """ModemManager's Location interface over the system bus (dbus-python).

    Blocking calls with a timeout, made from the phone-gnss thread only, on a
    private connection (no main loop needed for method calls)."""

    name = "dbus"

    def __init__(self, timeout_s: float = 3.0):
        import dbus  # system package py3-dbus (venv has system site-packages)
        self._dbus = dbus
        self._timeout = timeout_s
        self._bus = None

    def _conn(self):
        if self._bus is None:
            self._bus = self._dbus.SystemBus(private=True)
        return self._bus

    def reset(self) -> None:
        bus, self._bus = self._bus, None
        if bus is not None:
            try:
                bus.close()
            except Exception:  # noqa: BLE001
                pass

    def _obj(self, path: str):
        return self._conn().get_object(MM_BUS, path, introspect=False)

    def find(self) -> Optional[str]:
        om = self._dbus.Interface(self._obj(MM_PATH), "org.freedesktop.DBus.ObjectManager")
        objs = om.GetManagedObjects(timeout=self._timeout)
        for path in sorted(str(p) for p in objs):
            if MM_LOC in objs[path]:
                return path
        return None

    def _prop(self, path: str, name: str):
        props = self._dbus.Interface(self._obj(path), "org.freedesktop.DBus.Properties")
        return props.Get(MM_LOC, name, timeout=self._timeout)

    def capabilities(self, path: str) -> int:
        return int(self._prop(path, "Capabilities"))

    def enabled(self, path: str) -> int:
        return int(self._prop(path, "Enabled"))

    def signals(self, path: str) -> bool:
        return bool(self._prop(path, "SignalsLocation"))

    def refresh_rate(self, path: str) -> int:
        return int(self._prop(path, "GpsRefreshRate"))

    def setup(self, path: str, sources: int, signals: bool) -> None:
        loc = self._dbus.Interface(self._obj(path), MM_LOC)
        loc.Setup(self._dbus.UInt32(sources), self._dbus.Boolean(signals), timeout=max(10.0, self._timeout))

    def set_refresh_rate(self, path: str, rate: int) -> None:
        loc = self._dbus.Interface(self._obj(path), MM_LOC)
        loc.SetGpsRefreshRate(self._dbus.UInt32(rate), timeout=self._timeout)

    def nmea(self, path: str) -> Optional[str]:
        loc = self._dbus.Interface(self._obj(path), MM_LOC)
        d = loc.GetLocation(timeout=self._timeout)
        v = d.get(self._dbus.UInt32(MM_LOC_GPS_NMEA)) if hasattr(d, "get") else None
        if v is None:
            for k, val in dict(d).items():
                if int(k) == MM_LOC_GPS_NMEA:
                    v = val
        return None if v is None else str(v)


class MmcliModemManager:
    """Fallback without dbus-python: ``mmcli`` subprocesses (one action per
    call: mmcli refuses several location actions at once)."""

    name = "mmcli"
    SRC = {MM_LOC_GPS_NMEA: "gps-nmea", MM_LOC_GPS_RAW: "gps-raw", MM_LOC_3GPP: "3gpp"}

    def __init__(self, timeout_s: float = 5.0, run: Callable[..., Any] = subprocess.run):
        self._timeout = timeout_s
        self._run = run

    def reset(self) -> None:
        pass

    def _mm(self, *args: str) -> str:
        p = self._run(["mmcli"] + list(args), capture_output=True, text=True, timeout=self._timeout)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "mmcli failed").strip()[:200])
        return p.stdout

    def find(self) -> Optional[str]:
        d = json.loads(self._mm("-L", "-J"))
        mods = d.get("modem-list") or []
        return str(mods[0]) if mods else None

    def _status(self, path: str) -> Dict[str, Any]:
        return json.loads(self._mm("-m", path, "--location-status", "-J")).get("modem", {}).get("location", {})

    def _bits(self, names: List[str]) -> int:
        inv = {"gps-nmea": MM_LOC_GPS_NMEA, "gps-raw": MM_LOC_GPS_RAW, "3gpp-lac-ci": MM_LOC_3GPP,
               "agps-msa": 32, "agps-msb": 64, "cdma-bs": 8, "gps-unmanaged": 16}
        return sum(inv.get(n, 0) for n in names or [])

    def capabilities(self, path: str) -> int:
        return self._bits(self._status(path).get("capabilities"))

    def enabled(self, path: str) -> int:
        return self._bits(self._status(path).get("enabled"))

    def signals(self, path: str) -> bool:
        return str(self._status(path).get("signals")) == "yes"

    def refresh_rate(self, path: str) -> int:
        v = self._status(path).get("gps", {}).get("refresh-rate", "30")
        return int(str(v).split()[0]) if str(v).split() and str(v).split()[0].isdigit() else 30

    def setup(self, path: str, sources: int, signals: bool) -> None:
        cur = self.enabled(path)
        for bit, name in ((MM_LOC_GPS_NMEA, "gps-nmea"), (MM_LOC_GPS_RAW, "gps-raw")):
            if (sources & bit) and not (cur & bit):
                self._mm("-m", path, "--location-enable-" + name)
            elif not (sources & bit) and (cur & bit):
                self._mm("-m", path, "--location-disable-" + name)

    def set_refresh_rate(self, path: str, rate: int) -> None:
        self._mm("-m", path, "--location-set-gps-refresh-rate=%d" % int(rate))

    def nmea(self, path: str) -> Optional[str]:
        d = json.loads(self._mm("-m", path, "--location-get", "-J"))
        lines = (d.get("modem", {}).get("location", {}).get("gps", {}) or {}).get("nmea") or []
        return "\r\n".join(lines) if lines else None


def default_modem_manager():
    try:
        return DbusModemManager()
    except ImportError:
        logger.warning("phone GNSS: dbus-python missing, falling back to mmcli (1 Hz)")
        return MmcliModemManager()


def clock_synced() -> bool:
    return any(os.path.exists(p) for p in CLOCK_SYNCED_FLAGS)


# ── the source ───────────────────────────────────────────────────────────────

class PhoneGnssSource:
    """GnssSource (position_feeder interface) on the phone's modem GNSS.

    ``set_wanted(on)`` is called by the feeder (roadsense up); the thread does
    all ModemManager I/O. :meth:`step` is one thread iteration (tests call it
    with a fake clock and a fake ModemManager)."""

    name = "phone"
    tag = PHONE_TAG
    reports_fix = True
    provides_motion = False

    def __init__(self, cfg, mm=None, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 synced: Callable[[], bool] = clock_synced):
        self.cfg = cfg
        self._mm = mm
        self._clock, self._wall, self._synced = clock, wall, synced
        self._lock = threading.Lock()
        self._queue: deque = deque(maxlen=16)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_raw: Optional[Tuple[float, float, Optional[float], float]] = None
        # wanted / lifecycle
        self._wanted = False
        self._unwanted_since: Optional[float] = None
        self._path: Optional[str] = None
        self._on = False                     # GNSS set up by us right now
        self._added = 0                      # source bits we added (cleared again on off)
        self._old_rate: Optional[int] = None  # refresh rate to restore
        self._next_try = 0.0
        self._next_check = 0.0
        self._on_since: Optional[float] = None
        self.ttff_s: Optional[float] = None
        # fixes
        self._last_tod: Optional[float] = None
        self._last_t: Optional[float] = None       # mono time of the newest fix (with position)
        self._last_epoch_t: Optional[float] = None  # mono receipt of the newest epoch (any)
        self._meta: Dict[str, Any] = {}
        self.counts = {"polls": 0, "epochs": 0, "fixes": 0, "no_fix_epochs": 0, "errors": 0,
                       "setups": 0, "resetups": 0, "time_utc": 0, "time_rx": 0, "malformed": 0,
                       "searching": 0}
        self.last_error: Optional[str] = None
        self.backend: Optional[str] = None

    # ── feeder side (never blocks) ───────────────────────────────────────

    def set_wanted(self, on: bool) -> None:
        on = bool(on)
        with self._lock:
            if on == self._wanted:
                return
            self._wanted = on
            self._unwanted_since = None if on else self._clock()
        if on:
            self.start()
        self._wake.set()

    def drain(self, now: float) -> List[Dict[str, Any]]:
        with self._lock:
            out = list(self._queue)
            self._queue.clear()
        return out

    def _has_fix_locked(self, now: float) -> bool:
        return self._on and self._last_t is not None and now - self._last_t <= self.cfg.fix_timeout_s \
            and self._meta.get("fix", "none") != "none"

    def has_fix(self, now: float) -> bool:
        with self._lock:
            return self._has_fix_locked(now)

    def motion(self, now: float) -> Optional[Dict[str, Any]]:
        return None

    def status(self, now: float) -> Dict[str, Any]:
        with self._lock:
            age = None if self._last_t is None else round(now - self._last_t, 1)
            st: Dict[str, Any] = {"has_fix": self._has_fix_locked(now), "last_fix_age_s": age,
                                  "wanted": self._wanted, "gnss_on": self._on, "modem": self._path,
                                  "backend": self.backend, "ttff_s": self.ttff_s,
                                  "last_error": self.last_error}
            st.update(self.counts)
            st.update({k: v for k, v in self._meta.items() if v is not None})
        return st

    # ── thread ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="phone-gnss", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 5.0) -> None:
        """Stop the thread; GNSS is switched off on the way out."""
        self._stop.set()
        self._wake.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    delay = self.step()
                except Exception:  # noqa: BLE001 - never kill the thread
                    logger.exception("phone GNSS step failed")
                    delay = self.cfg.phone_retry_s
                self._wake.wait(delay)
                self._wake.clear()
        finally:
            try:
                self._switch_off("shutdown")
            except Exception:  # noqa: BLE001
                logger.exception("phone GNSS: switching off at shutdown failed")

    def _error(self, what: str, exc: BaseException) -> None:
        msg = "%s: %s" % (what, str(exc).strip().splitlines()[0][:160] if str(exc).strip() else type(exc).__name__)
        if msg != self.last_error:
            logger.warning("phone GNSS %s", msg)
        self.last_error = msg
        self.counts["errors"] += 1
        self._path = None
        with self._lock:
            self._on = False                # the setup state is unknown now: check it again
        if self._mm is not None and hasattr(self._mm, "reset"):
            self._mm.reset()
        self._next_try = self._clock() + self.cfg.phone_retry_s

    def step(self) -> float:
        """One iteration; returns the time to wait before the next one."""
        now = self._clock()
        cfg = self.cfg
        with self._lock:
            wanted = self._wanted
            since = self._unwanted_since
        if not wanted:
            if self._on and (since is None or now - since >= cfg.phone_linger_s):
                self._switch_off("idle")
            if self._on:
                return min(1.0, max(0.05, cfg.phone_linger_s - (now - (since or now))))
            return 3600.0 if not self._stop.is_set() else 0.0
        if self._mm is None:
            self._mm = default_modem_manager()
        self.backend = getattr(self._mm, "name", None)
        if now < self._next_try:
            return self._next_try - now
        if self._path is None or not self._on or now >= self._next_check:
            if not self._ensure_on(now):
                return max(0.05, self._next_try - self._clock())
        period = 1.0 / max(0.2, min(10.0, cfg.phone_poll_hz if self.backend != "mmcli" else 1.0))
        try:
            text = self._mm.nmea(self._path)
        except Exception as exc:  # noqa: BLE001
            self._error("poll", exc)
            return cfg.phone_retry_s
        self.counts["polls"] += 1
        if text:
            self._ingest(text, self._clock(), self._wall())
        return period

    def _ensure_on(self, now: float) -> bool:
        cfg = self.cfg
        try:
            if self._path is None:
                self._path = self._mm.find()
                if self._path is None:
                    raise RuntimeError("no modem with a Location interface")
            en = self._mm.enabled(self._path)
            if not en & MM_LOC_GPS_NMEA:
                caps = self._mm.capabilities(self._path)
                if not caps & MM_LOC_GPS_NMEA:
                    raise RuntimeError("modem has no gps-nmea location capability")
                was_on = self._on_since is not None
                self._mm.setup(self._path, en | MM_LOC_GPS_NMEA, self._mm.signals(self._path))
                self._added |= MM_LOC_GPS_NMEA
                self.counts["setups"] += 1
                if was_on:
                    self.counts["resetups"] += 1
                    logger.info("phone GNSS: location set up again (ModemManager restart or modem reset)")
                else:
                    logger.info("phone GNSS on (ModemManager %s, %s)", self._path, self._mm.name)
                if not was_on:
                    self._on_since = now
                    self.ttff_s = None
            elif self._on_since is None:
                self._on_since = now
                self.ttff_s = None
                logger.info("phone GNSS: gps-nmea already enabled on %s (left as found at the end)", self._path)
            rate = self._mm.refresh_rate(self._path)
            if rate != cfg.phone_refresh_rate:
                if self._old_rate is None:
                    self._old_rate = rate
                self._mm.set_refresh_rate(self._path, cfg.phone_refresh_rate)
        except Exception as exc:  # noqa: BLE001
            self._error("setup", exc)
            return False
        with self._lock:
            self._on = True
        self._next_check = now + cfg.phone_check_s
        return True

    def _switch_off(self, why: str) -> None:
        mm, path = self._mm, self._path
        added, old_rate = self._added, self._old_rate
        with self._lock:
            was = self._on
            self._on = False
        self._on_since = None
        self._last_tod = None
        if mm is None or (not was and not added and old_rate is None):
            return
        try:
            if path is None:
                path = mm.find()
            if path is not None and added:
                en = mm.enabled(path)
                if en & added:
                    mm.setup(path, en & ~added, mm.signals(path))
            if path is not None and old_rate is not None:
                mm.set_refresh_rate(path, old_rate)
            self._added, self._old_rate = 0, None
            logger.info("phone GNSS off (%s)", why)
        except Exception as exc:  # noqa: BLE001
            # an MM restart resets the location setup anyway
            logger.warning("phone GNSS: switching off failed (%s): %s", why, str(exc)[:160])
            self._added, self._old_rate = 0, None

    # ── NMEA -> fix ──────────────────────────────────────────────────────

    def _ingest(self, text: str, rx_mono: float, rx_wall: float) -> None:
        cfg = self.cfg
        try:
            fx = parse_trace(text)
        except Exception:  # noqa: BLE001 - malformed input never kills the source
            fx = None
        if fx is None:
            self.counts["malformed"] += 1
            return
        if fx.tod_s is None:                        # searching: no time from the sky yet
            self.counts["searching"] += 1
            with self._lock:
                self._meta = {"fix": "none", "searching": True}
            return
        if self._last_tod is not None and abs(fx.tod_s - self._last_tod) < 0.01:
            return                                  # same epoch as the previous poll
        self._last_tod = fx.tod_s
        self.counts["epochs"] += 1
        meta: Dict[str, Any] = {"fix": fx.fix, "sats": fx.sats, "sats_view": fx.sats_view,
                                "hdop": fx.hdop}
        if fx.cn0:
            top = fx.cn0[:4]
            meta["cn0_max"] = fx.cn0[0]
            meta["cn0_top4"] = round(sum(top) / len(top), 1)
            meta["tracked"] = len(fx.cn0)
        if fx.fix == "none" or fx.lat is None:
            self.counts["no_fix_epochs"] += 1
            with self._lock:
                self._meta = meta
                self._last_epoch_t = rx_mono
            return
        # fix time on CLOCK_MONOTONIC: the receiver's UTC against a synced
        # wall clock when plausible, else receipt minus the configured latency
        t = None
        if fx.utc_ms is not None and self._synced():
            lag = rx_wall - fx.utc_ms / 1000.0
            if -0.2 <= lag <= cfg.max_fix_age_s:
                t = rx_mono - max(0.0, lag)
                self.counts["time_utc"] += 1
        if t is None:
            t = rx_mono - cfg.phone_latency_s
            self.counts["time_rx"] += 1
        if self._last_t is not None and t <= self._last_t:
            t = self._last_t + 1e-3
        hacc = hacc_from(fx, cfg.phone_uere_m)
        msg: Dict[str, Any] = {"type": "gnss", "ts_mono_ns": int(t * 1e9), "fix": fx.fix,
                               "lat": round(fx.lat, 7), "lon": round(fx.lon, 7)}
        if hacc is not None:
            msg["hacc_m"] = hacc
        if fx.speed_mps is not None:
            msg["speed_mps"] = round(fx.speed_mps, 2)
            # a receiver course at walking pace is noise; mapd then uses the displacement
            if fx.course_deg is not None and fx.speed_mps >= cfg.phone_course_min_mps:
                msg["course_deg"] = round(fx.course_deg, 1)
        if fx.alt_m is not None:
            msg["alt_m"] = round(fx.alt_m, 1)
        if fx.hdop is not None:
            msg["hdop"] = fx.hdop
        if fx.sats is not None:
            msg["sats"] = fx.sats
        if fx.utc_ms is not None:
            msg["utc_ms"] = fx.utc_ms
        msg["source"] = PHONE_TAG
        meta["hacc_m"] = hacc
        with self._lock:
            self._queue.append(msg)
            self._last_t = t
            self._last_epoch_t = rx_mono
            self._meta = meta
            self.last_raw = (msg["lat"], msg["lon"], hacc, t)
            if self.ttff_s is None and self._on_since is not None:
                self.ttff_s = round(rx_mono - self._on_since, 1)
                logger.info("phone GNSS first fix after %.1f s (%s, %s sats)", self.ttff_s, fx.fix, fx.sats)
        self.counts["fixes"] += 1
        logger.debug("phone GNSS fix %s %.7f %.7f hacc %s", fx.fix, fx.lat, fx.lon, hacc)
