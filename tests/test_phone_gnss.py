"""Phone GNSS source (backend/phone_gnss.py) and source selection
(position_feeder.SourceSelector). No ModemManager: a fake with the same
methods. Coordinates are synthetic (the Opole test loop), never real ones."""

import os
from datetime import datetime, timezone

import pytest

from cyberpunk_computer.backend import phone_gnss as pg
from cyberpunk_computer.backend.phone_gnss import (
    MM_LOC_3GPP, MM_LOC_GPS_NMEA, PhoneGnssSource, hacc_from, nmea_checksum_ok, parse_trace,
)
from cyberpunk_computer.backend.position_feeder import (
    CanMotionTap, HeadunitGnssSource, PositionFeeder, PositionFeederConfig, SourceSelector,
    make_source, parse_source_list,
)


def nmea(body: str) -> str:
    x = 0
    for ch in body:
        x ^= ord(ch)
    return "$%s*%02X" % (body, x)


# 50.6600 N, 17.9200 E (test loop, not a real parking place)
LAT_S, LON_S = "5039.6000", "01755.2000"


def trace(tod="120000.00", date="151126", q=1, sats=7, hdop=1.4, speed_kn=0.0, course="",
          status="A", gst=None, gsv=True, rmc_tod=None):
    lines = [
        nmea("GPGSA,A,3,03,06,11,,,,,,,,,,2.1,%s,1.8" % hdop),
        nmea("GNGGA,%s,%s,N,%s,E,%d,%02d,%s,212.0,M,40.0,M,," % (tod, LAT_S, LON_S, q, sats, hdop)),
        nmea("GNRMC,%s,%s,%s,N,%s,E,%s,%s,%s,,,A" % (rmc_tod or tod, status, LAT_S, LON_S, speed_kn, course, date)),
        nmea("GPVTG,,T,,M,,N,,K,N"),
    ]
    if gst:
        lines.append(nmea("GPGST,%s,1.0,2.0,1.5,30.0,%s,%s,3.0" % (tod, gst[0], gst[1])))
    if gsv:
        lines.append(nmea("GPGSV,2,1,07,03,45,120,31,06,30,200,28,11,60,080,35,14,10,300,"))
        lines.append(nmea("GPGSV,2,2,07,17,20,100,22,19,05,010,,22,40,250,19"))
        lines.append(nmea("GAGSV,1,1,02,04,50,150,27,09,15,330,"))
    return "\r\n".join(lines) + "\r\n"


SEARCHING = "\r\n".join(["$GPGSA,A,1,,,,,,,,,,,,,,,*1E", "$GNGNS,,,,,,NNN,,,,,,*1D",
                         "$GPRMC,,V,,,,,,,,,,N*53", "$GAGGA,,,,,,0,,,,,,,,*77",
                         "$GPVTG,,T,,M,,N,,K,N*2C", "$GPGGA,,,,,,0,,,,,,,,*66"])


# ── NMEA ─────────────────────────────────────────────────────────────────────

def test_checksum():
    assert nmea_checksum_ok("$GPGGA,,,,,,0,,,,,,,,*66")          # as sent by the phone's modem
    assert not nmea_checksum_ok("$GPGGA,,,,,,0,,,,,,,,*67")
    assert not nmea_checksum_ok("GPGGA,,,,,,0,,,,,,,,*66")
    assert not nmea_checksum_ok("$GPGGA,,,,,,0")
    assert not nmea_checksum_ok("$GPGGA*ZZ")


def test_parse_full_epoch():
    f = parse_trace(trace(speed_kn="10.0", course="87.5"))
    assert f.fix == "3d" and f.tod_s == 12 * 3600
    assert f.lat == pytest.approx(50.66) and f.lon == pytest.approx(17.92)
    assert f.sats == 7 and f.hdop == 1.4 and f.alt_m == 212.0 and f.pdop == 2.1
    assert f.speed_mps == pytest.approx(5.14444) and f.course_deg == 87.5
    exp = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc).timestamp() * 1000
    assert f.utc_ms == int(exp)
    assert f.sats_view == 9 and f.cn0 == [35, 31, 28, 27, 22, 19]


def test_parse_searching_and_no_fix():
    f = parse_trace(SEARCHING)                       # what the modem sends before it has time
    assert f is not None and f.tod_s is None and f.fix == "none"
    f = parse_trace(trace(q=0, status="V"))           # time, no position
    assert f.fix == "none" and f.lat is None and f.tod_s == 43200.0
    assert parse_trace("") is None and parse_trace(None) is None


def test_parse_malformed_never_raises():
    bad = [
        "garbage", "$GNGGA,12,,*00", "$$$$", "\x00\xff", "$GNGGA" + "," * 40 + "*00",
        nmea("GNGGA,120000,5099.0000,N,01755.2,E,1,07,1.0,1,M,,M,,"),      # minutes >= 60
        nmea("GNGGA,120000,5039.6,X,01755.2,E,1,07,1.0,1,M,,M,,"),        # bad hemisphere
        nmea("GNGGA,250000,5039.6,N,01755.2,E,1,07,1.0,1,M,,M,,"),        # hour 25
        nmea("GNGGA,120000,0000.0000,N,00000.0000,E,1,07,1.0,1,M,,M,,"),  # 0,0
        nmea("GNRMC,120000,A,5039.6,N,01755.2,E,abc,xyz,99,,,A"),
        nmea("GPGSV,1,1,xx,03,45,120,yy"),
        nmea("GNGGA,120000.00,5039.6,N"),                                  # truncated
    ]
    for b in bad:
        parse_trace(b)
        parse_trace(b + "\r\n" + trace())
    f = parse_trace(bad[5])
    assert f is None or f.lat is None
    f = parse_trace(bad[8])
    assert f.fix == "none" and f.lat is None
    # a bad checksum hides the sentence
    t = trace().replace("GNGGA,120000.00", "GNGGA,120001.00")
    f = parse_trace(t)
    assert f.tod_s == 43200.0 and f.sats is None and f.lat == pytest.approx(50.66)     # RMC only


def test_parse_mismatched_rmc_time_and_talker_choice():
    f = parse_trace(trace(speed_kn="10.0", course="90", rmc_tod="115959.00"))
    assert f.fix == "3d" and f.speed_mps is None and f.utc_ms is None     # RMC of another epoch unused
    t = trace() + nmea("GPGGA,120000.00,5039.0000,N,01755.0000,E,1,04,3.0,1,M,,M,,") + "\r\n"
    assert parse_trace(t).sats == 7                                         # GN wins over GP
    dgps = parse_trace(trace(q=2))
    assert dgps.fix == "dgps"


def test_hacc():
    f = parse_trace(trace(hdop=1.5))
    assert hacc_from(f, 4.0) == 6.0
    assert hacc_from(parse_trace(trace(hdop=0.3)), 4.0) == 2.0                # floor
    g = parse_trace(trace(gst=("3.0", "4.0")))
    assert hacc_from(g, 4.0) == 5.0                                           # GST sigmas win
    f.hdop = None
    assert hacc_from(f, 4.0) is None


# ── fake ModemManager ────────────────────────────────────────────────────────

class FakeMM:
    name = "fake"

    def __init__(self, enabled=MM_LOC_3GPP, rate=30):
        self.path = "/org/freedesktop/ModemManager1/Modem/0"
        self.en, self.rate, self.sig = enabled, rate, False
        self.caps = 103
        self.text = SEARCHING
        self.down = False
        self.calls = []

    def _chk(self):
        if self.down:
            raise RuntimeError("org.freedesktop.DBus.Error.ServiceUnknown")

    def reset(self):
        self.calls.append("reset")

    def find(self):
        self._chk()
        return self.path

    def capabilities(self, p):
        self._chk()
        return self.caps

    def enabled(self, p):
        self._chk()
        assert p == self.path
        return self.en

    def signals(self, p):
        return self.sig

    def refresh_rate(self, p):
        self._chk()
        return self.rate

    def setup(self, p, sources, signals):
        self._chk()
        assert p == self.path
        self.calls.append(("setup", sources))
        self.en = sources

    def set_refresh_rate(self, p, r):
        self._chk()
        self.calls.append(("rate", r))
        self.rate = r

    def nmea(self, p):
        self._chk()
        if p != self.path:
            raise RuntimeError("UnknownObject")
        return self.text

    def restart(self):
        """ModemManager restarted: new object path, location setup back to default."""
        self.path = "/org/freedesktop/ModemManager1/Modem/1"
        self.en, self.rate = MM_LOC_3GPP, 30


class Clock:
    def __init__(self, t=1000.0, w=None):
        self.t = t
        self.w0 = w if w is not None else datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc).timestamp() - t

    def mono(self):
        return self.t

    def wall(self):
        return self.w0 + self.t

    def tick(self, dt):
        self.t += dt


def _phone(clock, mm=None, synced=True, **kw):
    cfg = PositionFeederConfig(enabled=True, source="phone", **kw)
    return PhoneGnssSource(cfg, mm=mm or FakeMM(), clock=clock.mono, wall=clock.wall,
                           synced=lambda: synced)


def _tod_of(clock, lag):
    """hhmmss.ss of the wall time minus lag."""
    d = datetime.fromtimestamp(clock.wall() - lag, timezone.utc)
    return d.strftime("%H%M%S.") + "%02d" % (d.microsecond // 10000), d.strftime("%d%m%y")


def test_phone_lifecycle_setup_and_restore():
    c = Clock()
    mm = FakeMM()
    src = _phone(c, mm)
    assert src.step() == 3600.0 and mm.calls == []              # not wanted: nothing touched
    src._wanted = True                                          # (set_wanted would start the thread)
    src.step()
    assert mm.en == MM_LOC_3GPP | MM_LOC_GPS_NMEA               # 3gpp kept, NMEA added
    assert mm.rate == 0
    st = src.status(c.t)
    assert st["gnss_on"] and not st["has_fix"] and st["searching"] == 1 and st["fix"] == "none"
    assert src.drain(c.t) == []
    # unwanted: stays on for linger_s, then exactly what was added is removed
    src._wanted, src._unwanted_since = False, c.t
    c.tick(60)
    src.step()
    assert mm.en & MM_LOC_GPS_NMEA
    c.tick(61)
    src.step()
    assert mm.en == MM_LOC_3GPP and mm.rate == 30 and not src.status(c.t)["gnss_on"]


def test_phone_leaves_owner_enabled_gnss_on():
    c = Clock()
    mm = FakeMM(enabled=MM_LOC_3GPP | MM_LOC_GPS_NMEA | 2, rate=0)
    src = _phone(c, mm, phone_linger_s=0)
    src._wanted = True
    src.step()
    assert not any(isinstance(x, tuple) and x[0] == "setup" for x in mm.calls)
    src._wanted, src._unwanted_since = False, c.t
    src.step()
    assert mm.en == MM_LOC_3GPP | MM_LOC_GPS_NMEA | 2 and mm.rate == 0


def test_phone_fix_time_utc_and_rx():
    c = Clock()
    mm = FakeMM()
    src = _phone(c, mm)
    src._wanted = True
    src.step()                                        # searching
    tod, date = _tod_of(c, 0.35)
    mm.text = trace(tod=tod, date=date, speed_kn="20.0", course="45.0")
    c.tick(0.25)
    tod, date = _tod_of(c, 0.35)
    mm.text = trace(tod=tod, date=date, speed_kn="20.0", course="45.0")
    src.step()
    msgs = src.drain(c.t)
    assert len(msgs) == 1
    m = msgs[0]
    assert m["source"] == "phone" and m["fix"] == "3d" and m["hacc_m"] == 5.6
    assert abs(m["ts_mono_ns"] / 1e9 - (c.t - 0.35)) < 0.011       # receiver UTC against the synced clock
    assert m["course_deg"] == 45.0 and m["speed_mps"] == pytest.approx(10.29, abs=0.01)
    assert m["sats"] == 7 and m["hdop"] == 1.4 and "utc_ms" in m
    st = src.status(c.t)
    assert st["has_fix"] and st["time_utc"] == 1 and st["ttff_s"] == 0.2 or st["ttff_s"] == 0.3
    assert st["cn0_max"] == 35 and st["sats_view"] == 9 and "lat" not in st
    src.step()                                        # same epoch polled again: nothing new
    assert src.drain(c.t) == []
    # slow speed: no course
    c.tick(1.0)
    tod, date = _tod_of(c, 0.3)
    mm.text = trace(tod=tod, date=date, speed_kn="0.5", course="200.0")
    src.step()
    m = src.drain(c.t)[0]
    assert "course_deg" not in m
    # fix times out
    c.tick(3.5)
    assert not src.has_fix(c.t)


def test_phone_fix_time_unsynced_clock():
    c = Clock()
    mm = FakeMM()
    src = _phone(c, mm, synced=False)
    src._wanted = True
    tod, date = _tod_of(c, 0.1)
    mm.text = trace(tod=tod, date=date)
    src.step()
    m = src.drain(c.t)[0]
    assert abs(m["ts_mono_ns"] / 1e9 - (c.t - 0.5)) < 1e-6           # receipt - phone_latency_s
    assert src.status(c.t)["time_rx"] == 1
    # an implausible UTC (clock wrong by an hour) falls back to receipt time too
    src2 = _phone(c, FakeMM(), synced=True)
    src2._wanted = True
    tod, date = _tod_of(c, 3600)
    src2._mm.text = trace(tod=tod, date=date)
    src2.step()
    assert src2.status(c.t)["time_rx"] == 1


def test_phone_survives_modemmanager_restart():
    c = Clock()
    mm = FakeMM()
    src = _phone(c, mm)
    src._wanted = True
    src.step()
    mm.down = True                                     # MM stopped
    d = src.step()
    st = src.status(c.t)
    assert d == 5.0 and st["errors"] == 1 and not st["gnss_on"] and "ServiceUnknown" in st["last_error"]
    assert src.step() == pytest.approx(5.0)            # waits retry_s, no busy loop
    mm.down = False
    mm.restart()
    c.tick(5.0)
    src.step()
    st = src.status(c.t)
    assert st["gnss_on"] and st["modem"].endswith("/1") and st["resetups"] == 1
    assert mm.en == MM_LOC_3GPP | MM_LOC_GPS_NMEA and mm.rate == 0
    # a silent reset (same path, setup lost) is caught by the periodic check
    mm.en, mm.rate = MM_LOC_3GPP, 30
    c.tick(31)
    src.step()
    assert mm.en & MM_LOC_GPS_NMEA and src.status(c.t)["resetups"] == 2


def test_phone_no_capability_reports_error():
    c = Clock()
    mm = FakeMM()
    mm.caps = MM_LOC_3GPP
    src = _phone(c, mm)
    src._wanted = True
    src.step()
    st = src.status(c.t)
    assert not st["gnss_on"] and "gps-nmea" in st["last_error"] and not src.has_fix(c.t)


def test_phone_thread_start_stop():
    c = Clock()
    mm = FakeMM()
    cfg = PositionFeederConfig(enabled=True, source="phone", phone_linger_s=0)
    src = PhoneGnssSource(cfg, mm=mm, synced=lambda: True)
    src.set_wanted(True)
    import time as _t
    for _ in range(100):
        if mm.en & MM_LOC_GPS_NMEA:
            break
        _t.sleep(0.01)
    assert mm.en & MM_LOC_GPS_NMEA
    src.close()
    assert mm.en == MM_LOC_3GPP and mm.rate == 30      # switched off on the way out


def test_mmcli_backend_parses_status():
    calls = []

    class P:
        def __init__(self, out, rc=0):
            self.stdout, self.stderr, self.returncode = out, "", rc

    def run(args, **kw):
        calls.append(args[1:])
        if args[1:] == ["-L", "-J"]:
            return P('{"modem-list": ["/org/freedesktop/ModemManager1/Modem/0"]}')
        if "--location-status" in args:
            return P('{"modem": {"location": {"capabilities": ["3gpp-lac-ci", "gps-raw", "gps-nmea"], '
                     '"enabled": ["3gpp-lac-ci"], "signals": "no", "gps": {"refresh-rate": "30"}}}}')
        if "--location-get" in args:
            return P('{"modem": {"location": {"gps": {"nmea": ["$GPGGA,,,,,,0,,,,,,,,*66"]}}}}')
        return P("ok")

    mm = pg.MmcliModemManager(run=run)
    p = mm.find()
    assert mm.capabilities(p) == 7 and mm.enabled(p) == 1 and mm.refresh_rate(p) == 30 and not mm.signals(p)
    mm.setup(p, 1 | 4, False)
    assert [p, "--location-enable-gps-nmea"] == calls[-1][1:] or calls[-1][-1] == "--location-enable-gps-nmea"
    assert parse_trace(mm.nmea(p)).fix == "none"


# ── source selection ─────────────────────────────────────────────────────────

class StubSource:
    reports_fix = True
    provides_motion = False

    def __init__(self, name):
        self.name = self.tag = name
        self.fix = False
        self.q = []
        self.last_raw = None
        self.wanted = None

    def drain(self, now):
        out, self.q = self.q, []
        return out

    def has_fix(self, now):
        return self.fix

    def status(self, now):
        return {"has_fix": self.fix, "last_fix_age_s": 0.1 if self.fix else None}

    def set_wanted(self, on):
        self.wanted = on

    def push(self, t):
        self.q.append({"type": "gnss", "fix": "3d", "ts_mono_ns": int(t * 1e9), "lat": 50.66, "lon": 17.92,
                       "source": self.name})
        self.last_raw = (50.66, 17.92, 5.0, t)


def test_selector_fallback_and_hysteresis():
    cfg = PositionFeederConfig(enabled=True, switch_up_s=5.0)
    hu, ph = StubSource("headunit"), StubSource("phone")
    sel = SourceSelector(cfg, [hu, ph])
    assert sel.name == "headunit,phone" and sel.tag == "headunit" and not hasattr(sel, "ingest")
    t = 100.0
    assert sel.drain(t) == [] and not sel.has_fix(t)
    ph.fix = True
    ph.push(t)
    out = sel.drain(t)
    assert [m["source"] for m in out] == ["phone"] and sel.selected is ph and sel.tag == "phone"
    # head unit comes: not used before 5 s of continuous fix; its fixes are drained and dropped
    for k in range(1, 7):
        t += 1.0
        hu.fix = True
        hu.push(t - 0.05)
        ph.push(t)
        out = sel.drain(t)
        if k < 6:
            assert [m["source"] for m in out] == ["phone"]
    assert sel.selected is hu and [m["source"] for m in out] == ["headunit"]
    # head unit loses its fix: phone at once, and a fix older than the last sent is dropped
    t += 1.0
    hu.fix = False
    ph.push(t - 1.2)
    ph.push(t)
    out = sel.drain(t)
    assert sel.selected is ph and len(out) == 1 and out[0]["ts_mono_ns"] == int(t * 1e9)
    # a flicker of the head unit does not switch
    t += 1.0
    hu.fix = True
    sel.drain(t)
    t += 1.0
    hu.fix = False
    sel.drain(t)
    t += 1.0
    hu.fix = True
    sel.drain(t)
    assert sel.selected is ph
    # both gone
    hu.fix = ph.fix = False
    t += 1.0
    assert sel.drain(t) == [] and sel.selected is None and not sel.has_fix(t)
    st = sel.status(t)
    assert st["order"] == ["headunit", "phone"] and st["selected"] is None and st["switches"] == 4
    assert set(st["sources"]) == {"headunit", "phone"}


def test_parse_source_list_and_make_source():
    assert parse_source_list(" HeadUnit , phone ") == ("headunit", "phone")
    assert parse_source_list("synthetic") == ("synthetic",)
    for bad in ("headunit,synthetic", "phone,none", "phone,phone", "gps", ""):
        with pytest.raises(ValueError):
            parse_source_list(bad)
    s = make_source(PositionFeederConfig(enabled=True, source="headunit,phone"))
    assert isinstance(s, SourceSelector) and s.name == "headunit,phone" and hasattr(s, "ingest")
    assert make_source(PositionFeederConfig(enabled=True, source="phone")).name == "phone"


class FakeFeed:
    def __init__(self):
        self.msgs, self.path = [], "/fake/position.sock"
        self.sent = self.dropped = self.connects = self.errors_total = 0
        self.errors = []
        self.connected = True

    def send(self, m):
        self.msgs.append(dict(m))
        self.sent += 1
        return True

    def close(self):
        pass


def test_feeder_with_headunit_and_phone():
    c = Clock()
    cfg = PositionFeederConfig(enabled=True, source="headunit,phone", switch_up_s=2.0)
    hu = HeadunitGnssSource(cfg, clock=c.mono, wall=c.wall)
    mm = FakeMM()
    ph = PhoneGnssSource(cfg, mm=mm, clock=c.mono, wall=c.wall, synced=lambda: True)
    ph.start = lambda: None                          # thread driven by hand below
    sel = SourceSelector(cfg, [hu, ph])
    feed = FakeFeed()
    f = PositionFeeder(cfg, sel, tap=CanMotionTap(), feed=feed, clock=c.mono)
    f.set_active(True)
    assert ph._wanted
    ph.step()
    f.step(c.t)
    assert feed.msgs[-1] == {"type": "gnss", "fix": "none", "ts_mono_ns": int(c.t * 1e9), "source": "headunit"}
    st = f.status(c.t)
    assert st["source"] == "headunit,phone" and st["gnss_source"] is None
    assert st["gnss"]["sources"]["phone"]["gnss_on"]
    c.tick(1.0)
    tod, date = _tod_of(c, 0.3)
    mm.text = trace(tod=tod, date=date)
    ph.step()
    f.step(c.t)
    assert feed.msgs[-1]["source"] == "phone" and feed.msgs[-1]["fix"] == "3d"
    assert f.status(c.t)["gnss_source"] == "phone"
    r = f.raw_fix(c.t)
    assert r["source"] == "phone" and r["lat"] == pytest.approx(50.66)
    # head unit arrives through REST (selector forwards ingest) and takes over after 2 s
    for _ in range(3):
        c.tick(1.0)
        sel.ingest({"lat": 50.661, "lon": 17.921, "age_ms": 50})
        f.step(c.t)
    assert f.status(c.t)["gnss_source"] == "headunit" and feed.msgs[-1]["source"] == "headunit"
    f.set_active(False)
    assert not ph._wanted


def test_env_gnss_list(monkeypatch):
    from cyberpunk_computer.backend.__main__ import _build_position
    for k in list(os.environ):
        if k.startswith("BACKEND_POSITION_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("BACKEND_POSITION_FEEDER", "1")
    monkeypatch.setenv("BACKEND_POSITION_SOURCE", "synthetic")
    assert _build_position().source == "synthetic"                # old name still works
    monkeypatch.setenv("BACKEND_POSITION_GNSS", "headunit, phone")
    monkeypatch.setenv("BACKEND_POSITION_PHONE_UERE_M", "5")
    monkeypatch.setenv("BACKEND_POSITION_SWITCH_UP_S", "bad")
    c = _build_position()
    assert c.source == "headunit,phone" and c.phone_uere_m == 5.0 and c.switch_up_s == 5.0
    monkeypatch.setenv("BACKEND_POSITION_GNSS", "phone,synthetic")
    assert _build_position() is None                               # synthetic never in a list
