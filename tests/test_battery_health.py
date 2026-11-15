"""Battery-health module: codec, decoder, collector, analysis on synthetic packs
with planted faults and on the car's own recording, trends, storage, API."""

import json
import math
import os
import time

import pytest

from cyberpunk_computer.battery import analysis, backfill, export, frames, model, summary, synth, trends
from cyberpunk_computer.battery.collector import Collector
from cyberpunk_computer.battery.frames import FrameDecoder
from cyberpunk_computer.battery.service import BatteryHealthService
from cyberpunk_computer.battery.store import BatteryStore

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
REAL_RIDE = os.path.join(FIX, "battery_ride_2026-11-13.bh1")
REAL_TRIP = os.path.join(FIX, "battery_trip_excerpt_2026-11-13.ndjson.gz")


# ── codec / decoder ─────────────────────────────────────────────────────────

def test_codec_roundtrip_wide_and_negative():
    tables = {"a": {"t": [0, 5, 70000, 70001], "x": [-32768, 3, -4, 32767]}, "e": {"t": []}}
    meta, back = model.decode_record(model.encode_record({"k": 1}, tables))
    assert meta == {"k": 1} and back == tables


def test_21cf_first_frame_and_full_answer():
    # as recorded on the car (subscription without ISO-TP): first frame only
    first = frames.iso_payload([16, 18, 97, 207, 131, 42, 128, 199])
    assert first[:2] == [0x61, 0xCF]
    d = frames.decode_21cf(first[2:])
    assert d["intake"] == pytest.approx(8.10, abs=0.01) and d["fan_v"] == pytest.approx(0.0)
    assert d["aux_v"] == pytest.approx(14.2) and "tb1" not in d and "dsoc" not in d
    # reassembled answer: 16 payload bytes
    full = [0x61, 0xCF, 131, 42, 128, 199, 89, 85, 12, 0, 2, 0, 131, 100, 131, 200, 132, 44]
    d = frames.decode_21cf(frames.iso_payload(full)[2:])
    assert d["chg_kw"] == 25 and d["dis_kw"] == 21 and d["dsoc"] == pytest.approx(0.12) and d["fan"] == 2
    assert d["tb1"] == pytest.approx(8.68, abs=0.01) and d["tb3"] == pytest.approx(10.68, abs=0.01)


def test_21cf_subscription_uses_isotp():
    from cyberpunk_computer.comm.subscription_groups import BATTERY_CELLS_GROUP
    slot = [s for s in BATTERY_CELLS_GROUP.slots if s.slot == 4][0]
    assert slot.payload.get("isotp") is True and slot.payload["t"] >= 300


def test_decoder_routes_car_frames():
    seen = {}

    class C:
        def __getattr__(self, name):
            return lambda *a: seen.setdefault(name, a)
    dec = FrameDecoder(C())
    dec.feed(10.0, 1, {"i": "0x03B", "d": [0x0F, 0x80, 0x00, 0xDC, 0], "bus": 0})
    assert seen["pack"] == (10.0, 220.0, pytest.approx(-12.8))
    dec.feed(10.0, 1, {"i": "0x3CB", "d": [70, 71, 0, 102, 7, 0xFF, 0], "bus": 0})
    assert seen["ecu"] == (10.0, 51.0, 71, 70, 7, -1)
    ce = [97, 206, 102, 129, 91] + [134, 44] * 14
    dec.feed(10.0, 1, {"a": "sub", "slot": 3, "i": "0x7EB", "d": ce})
    t, soc, i, v = seen["blocks"]
    assert soc == 51.0 and i == pytest.approx(3.47, abs=0.01) and v[0] == pytest.approx(15.80, abs=0.01)
    dec.feed(10.0, 1, {"a": "resp", "err": "TIMEOUT", "i": "0x7EB", "d": ce})
    dec.feed(10.0, 201, {"v": 11.86, "ma": 245.5, "mah": 464.2, "bmp2_t": 4.9})
    assert seen["powerbox"] == (10.0, 11.86, 245.5, 464.2, 4.9)
    assert frames.wanted(1, {"i": "0x03B"}) and not frames.wanted(1, {"i": "0x025"})
    assert not frames.wanted(203, {"bf": []})


# ── collector ───────────────────────────────────────────────────────────────

def _collect():
    rides, rests = [], []
    return Collector(rides.append, rests.append, "test"), rides, rests


def test_collector_segments_rides_and_rests_and_integrates():
    col, rides, rests = _collect()
    t = 1000.0
    for k in range(10):                                   # parked: powerbox only
        col.powerbox(t + 60 * k, 12.5, 110.0, k * 2.0, 5.0)
    t += 600
    for k in range(4 * 120):                              # 120 s at +36 A
        col.pack(t + k * 0.25, 220.0, 36.0)
        if k % 4 == 0:
            col.ecu(t + k * 0.25, 55.0, 80, 80, 20, 21)
    col.tick(t + 120 + model.RIDE_GAP_S + 1)
    assert len(rides) == 1 and len(rests) == 1
    m = rides[0]["meta"]
    assert m["ah_dis"] == pytest.approx(36 * 120 / 3600, rel=0.01) and m["ah_chg"] == 0
    assert m["hist_s"][6] == pytest.approx(120, abs=1)    # 20..40 A bin
    assert len(rides[0]["tables"]["sec"]["t"]) == 120
    assert rests[0]["meta"]["start_known"] is False and len(rests[0]["tables"]["min"]["t"]) == 10
    for k in range(5):                                    # the rest after the ride
        col.powerbox(t + 200 + 60 * k, 12.6, 100.0, None, None)
    snap = col.snapshot()
    assert snap["ride"] is None and snap["rest"]["meta"]["start_known"] is True


def test_collector_snapshot_of_open_ride_is_analysable():
    col, rides, _ = _collect()
    for k in range(400):
        col.pack(50.0 + k * 0.25, 220.0, 5.0)
    snap = col.snapshot()["ride"]
    assert snap["meta"]["closed"] is False
    f = analysis.analyse_ride(snap)
    assert "open" in f["quality"]["flags"]


# ── synthetic packs ─────────────────────────────────────────────────────────

def _simulate(tmp_path_factory, name, **kw):
    path = str(tmp_path_factory.mktemp("bat") / (name + ".db"))
    store = BatteryStore(path)
    cfg = synth.SynthConfig(**kw)
    col = Collector(lambda r: store.put_ride(r), lambda r: store.put_rest(r), source="synth")
    car = synth.simulate(cfg, col)
    return store, summary.build_summary(store, now=car.t), car


@pytest.fixture(scope="module")
def faulty(tmp_path_factory):
    return _simulate(tmp_path_factory, "faulty", seed=3, days=70, rides_per_day=1.0, ride_s=450.0,
                     weak_block=7, weak_start_day=10, weak_r_growth_per_day=0.012,
                     leaky_block=3, leak_soc_per_day=0.6, pack_r_growth_per_year=0.0,
                     aux_fade_per_year=2.5, temp_amp=2.0, temp_mean=20.0)


@pytest.fixture(scope="module")
def healthy_seasons(tmp_path_factory):
    # healthy pack, autumn into winter, a temperature coefficient unlike the prior
    return _simulate(tmp_path_factory, "healthy", seed=5, days=150, rides_per_day=0.5, ride_s=450.0,
                     start_ts=1_788_000_000.0, temp_mean=10.0, temp_amp=12.0, arrhenius_b=3200.0)


def _ind(s, key):
    return [i for i in s["indicators"] if i["key"] == key][0]


def test_planted_weak_block_is_found(faulty):
    store, s, car = faulty
    weak = _ind(s, "weak_block")
    assert s["blocks"]["persistent"]["block"] in (7, 3)
    assert s["blocks"]["r_max_block"] == 7
    rs = _ind(s, "r_spread")
    true_pct = 100 * (car.r25(6) / sorted(car.r25(k) for k in range(14))[7] - 1)
    assert rs["value"] == pytest.approx(true_pct, rel=0.15)
    assert rs["level"] in ("watch", "act") and rs["block"] == 7
    tr = s["trends"]["r_spread"]
    assert tr["significant"] and tr["slope_30d"] > 0 and tr["lo_30d"] > 0
    # the block under load diverges; at rest it does not (resistance, not charge)
    b = s["blocks"]["latest"]
    assert b["dev25_mv"][6] < -100 and abs(b["dev0_mv"][6]) < 40
    assert weak["level"] in ("watch", "act")
    bt = s["blocks"]["block_trends"][6]
    assert bt["dr_pct"]["significant"] and bt["dr_pct"]["slope_30d"] > 0
    assert s["overall"]["hv"] in ("watch", "act")


def test_block_resistance_estimate_matches_model(faulty):
    store, s, car = faulty
    f = store.rides()[-1]["features"]
    fac = analysis.arrhenius_factor(f["r"]["t_mean"])
    for k in (0, 6, 13):
        ohmic = 1000 * car.r25(k) * fac
        # 2 s steps see the ohmic part plus part of the polarisation (<= 1.5x)
        assert ohmic * 0.95 <= f["r"]["blk_mohm"][k] <= ohmic * 1.6
    # the ECU-reported value is carried through unchanged
    assert f["ecu"]["r_mohm"][6] == pytest.approx(1000 * car.r25(6), rel=0.08)


def test_planted_self_discharge_is_found(faulty):
    store, s, car = faulty
    sd = s["self_discharge"]
    assert sd is not None and sd["method"] == "drift" and sd["block"] == 3 and sd["mv_per_day"] < -3.0
    others = [x for k, x in enumerate(sd["per_block"]) if k not in (2, 6)]
    assert max(abs(x) for x in others) < abs(sd["mv_per_day"]) / 2
    # and it accumulates as an offset at zero current
    assert s["blocks"]["latest"]["dev0_mv"][2] < -100


def test_planted_12v_fade_is_found(faulty):
    store, s, car = faulty
    tr = s["trends"]["aux_rest_v"]
    assert tr["significant"] and tr["slope_30d"] < 0
    assert _ind(s, "aux_drain")["value"] == pytest.approx(120.0, rel=0.05)
    assert _ind(s, "aux_charge")["value"] == pytest.approx(13.95, abs=0.05)


def test_winter_does_not_look_like_ageing(healthy_seasons):
    store, s, car = healthy_seasons
    rides = [r["features"] for r in store.rides()]
    raw_first = sum(f["r"]["sum_mohm"] for f in rides[:5]) / 5
    raw_last = sum(f["r"]["sum_mohm"] for f in rides[-5:]) / 5
    temps = [f["temp"]["t_mean"] for f in rides]
    assert max(temps) - min(temps) > 12
    assert raw_last / raw_first > 1.3                    # the raw value rises with the cold
    tr = s["trends"]["r_sum"]
    assert tr["coef"]["source"] != "prior"
    assert tr["coef"]["learned"] == pytest.approx(3200.0, rel=0.25)
    assert abs(tr["slope_30d"]) < 0.03                   # under 3 % per 30 days after normalisation
    assert _ind(s, "r_pack")["level"] == "ok"
    assert _ind(s, "r_spread")["level"] == "ok" and _ind(s, "weak_block")["level"] == "ok"
    assert s["overall"]["hv"] == "ok"


def test_soc_per_ah_recovers_the_ecu_scale(healthy_seasons):
    store, s, car = healthy_seasons
    v = _ind(s, "soc_stability")["value"]
    assert v == pytest.approx(100 / model.NOMINAL_AH, rel=0.15)


def test_summary_is_json_and_bounded(faulty):
    store, s, _ = faulty
    text = json.dumps(s)
    assert len(text) < 600_000 and "NaN" not in text
    hm = s["blocks"]["heatmaps"]["weeks"]
    assert len(hm["dev25_mv"]) == 14 and len(hm["cols"]) == len(hm["dev25_mv"][0]) >= 9
    assert s["quality"]["grades"].get("A", 0) > 50 and set(s["quality"]["grades"]) == {"A"}
    assert s["usage"]["total"]["ah_thru"] > 10 and s["usage"]["months"]


# ── quality gating ──────────────────────────────────────────────────────────

def test_short_and_poll_less_rides_are_gated():
    col, rides, _ = _collect()
    for k in range(4 * 30):
        col.pack(100.0 + k * 0.25, 220.0, 10.0)
    col.finish()
    assert analysis.analyse_ride(rides[0])["quality"]["grade"] == "X"
    col, rides, _ = _collect()
    for k in range(4 * 600):
        col.pack(100.0 + k * 0.25, 220.0, 30.0 * math.sin(k / 9.0))
        if k % 4 == 0:
            col.ecu(100.0 + k * 0.25, 55.0, 80, 80, 20, 21)
    col.finish()
    f = analysis.analyse_ride(rides[0])
    assert f["quality"]["grade"] == "C" and "no_block_poll" in f["quality"]["flags"]
    assert f["r"]["pack_fast_mohm"] is None or f["r"]["blk_mohm"] is None
    assert f["stress"]["ah_thru"] > 0 and f["bal"]["fit"] is None


def test_cold_ride_is_flagged_and_kept_out_of_resistance_trend(tmp_path):
    store = BatteryStore(str(tmp_path / "b.db"))
    cfg = synth.SynthConfig(seed=2, days=3, rides_per_day=1.0, ride_s=450.0, temp_mean=-12.0, temp_amp=0.0)
    synth.simulate(cfg, Collector(store.put_ride, store.put_rest, "synth"))
    fs = [r["features"] for r in store.rides()]
    assert fs and all("cold" in f["quality"]["flags"] and f["quality"]["grade"] == "B" for f in fs)
    assert summary.build_summary(store)["trends"]["r_sum"]["n"] == 0


# ── trends ──────────────────────────────────────────────────────────────────

def test_theil_sen_and_change_point():
    day = 86400.0
    pts = [(k * day, 10.0 + 0.1 * k + (0.3 if k % 2 else -0.3), None) for k in range(60)]
    tr = trends.analyse_trend(pts)
    assert tr["significant"] and tr["slope_30d"] == pytest.approx(3.0, rel=0.1)
    assert tr["lo_30d"] <= 3.0 <= tr["hi_30d"]
    flat = [(k * day, 5.0 + (0.2 if k % 3 else -0.4), None) for k in range(60)]
    assert not trends.analyse_trend(flat)["significant"]
    step = [(k * day, (5.0 if k < 30 else 6.0) + 0.05 * (k % 3), None) for k in range(60)]
    cp = trends.analyse_trend(step)["change"]
    assert cp and abs(cp["ts"] / day - 30) <= 2 and cp["after"] - cp["before"] == pytest.approx(1.0, abs=0.1)
    short = trends.analyse_trend(pts[:5])
    assert short["slope_30d"] is None and "too short" in short["status"]


# ── the car's own recording ─────────────────────────────────────────────────

def test_real_ride_record_2026_11_13():
    meta, tables = model.decode_record(open(REAL_RIDE, "rb").read())
    f = analysis.analyse_ride({"meta": meta, "tables": tables})
    assert f["quality"]["grade"] == "A" and f["quality"]["flags"] == ["21cf_first_frame_only"]
    assert f["n_blk"] == 539 and f["blk_rate_hz"] == pytest.approx(0.48, abs=0.02)
    assert f["temp"]["t_mean"] == pytest.approx(8.4, abs=0.3) and f["temp"]["source"] == "0x3CB T1/T2"
    r = f["r"]
    assert r["n_steps"] >= 80 and 28.0 < r["med_mohm"] < 34.0
    assert r["max_rel_block"] == 12 and r["max_rel"] < 1.12
    assert min(range(14), key=lambda k: r["blk_mohm"][k]) == 9          # block 10 lowest
    assert f["bal"]["rest"]["spread_mean_mv"] < 150
    assert f["soc"]["pct_per_ah"] == pytest.approx(16.4, abs=1.0)
    assert f["ecu"]["r_mohm"] == [19.0] * 14
    assert f["aux"]["ready_v_obd"] == pytest.approx(13.95, abs=0.05)
    v = summary.block_view(f)
    assert v["weak_block"] == 12 and -120 < v["weak_dev25_mv"] < 0


def test_backfill_from_trip_recording_is_idempotent(tmp_path):
    store = BatteryStore(str(tmp_path / "b.db"))
    res = backfill.backfill_trips(store, [REAL_TRIP])
    assert res["rides"] == 1 and res["rides_new"] == 1
    assert res["counts"]["21ce"] > 100 and res["counts"]["21cf_first"] > 100 and "21cf_full" not in res["counts"]
    again = backfill.backfill_trips(store, [REAL_TRIP])
    assert again["rides_new"] == 0 and len(store.rides()) == 1
    row = store.rides(with_raw=True)[0]
    assert row["source"] == "trip" and row["features"]["dur_s"] > 300
    assert time.gmtime(row["start_ts"])[:3] == (2026, 11, 13)
    det = summary.ride_detail(row, store.record(row))
    assert len(det["series"]["pack"]) == 180 and det["series"]["slow"]["soc2"]
    assert export.ride_series_csv(store.record(row)).count("\n") == row["features"]["n_blk"] + 1
    assert export.rides_csv(store).splitlines()[0].startswith("start_ts,end_ts,source,grade")


def test_backfill_from_metrics_database(tmp_path):
    import sqlite3
    mdb = str(tmp_path / "metrics.db")
    c = sqlite3.connect(mdb)
    c.execute("CREATE TABLE metrics_1min (signal TEXT, bucket INTEGER, min REAL, max REAL, sum REAL, "
              "count INTEGER, last REAL, PRIMARY KEY (signal, bucket)) WITHOUT ROWID")
    t0 = 1_794_000_000 // 60 * 60
    rows = []
    for m in range(600):
        b = t0 + 60 * m
        driving = 100 <= m < 130
        rows.append(("powerbox_voltage", b, 12.4, 12.5, 12.45 * 60, 60, 12.45))
        rows.append(("powerbox_current", b, 0.1, 0.12, 0.11 * 60, 60, 0.11))
        rows.append(("powerbox_acc", b, 0, 1, 60.0 if driving else 0.0, 60, 0))
        # after IG-OFF the state keeps the last current: constant, not a ride
        rows.append(("hv_current", b, -20.0 if driving else 1.5, 30.0 if driving else 1.5, 60.0, 60, 1.5))
        rows.append(("battery_soc", b, 55, 60, 57 * 60, 60, 57))
    c.executemany("INSERT INTO metrics_1min VALUES (?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()
    store = BatteryStore(str(tmp_path / "b.db"))
    res = backfill.backfill_metrics(store, mdb)
    assert res["sessions"] == 1 and res["rides_new"] == 1 and res["rests_new"] == 2
    ride = store.rides()[0]
    assert ride["features"]["quality"]["grade"] == "L" and ride["features"]["dur_s"] == 1800
    rest = store.rests()[-1]["features"]
    assert rest["start_known"] and rest["marks"]["6"] == pytest.approx(12.45) and rest["ma_mean"] == pytest.approx(110.0)
    assert backfill.backfill_metrics(store, mdb)["rides_new"] == 0


# ── storage: spool, recovery, versions ──────────────────────────────────────

def test_spool_and_power_loss_recovery(tmp_path):
    path = str(tmp_path / "b.db")
    store = BatteryStore(path)
    col = Collector(store.put_ride, store.put_rest, "live")
    for k in range(4 * 400):
        col.pack(2000.0 + k * 0.25, 220.0, 20.0 * math.sin(k / 7.0))
        if k % 8 == 0:
            col.blocks(2000.0 + k * 0.25, 55.0, 20.0 * math.sin(k / 7.0), [16.0] * 14)
    snap = col.snapshot()
    store.put_ride(snap["ride"], {"v": 0, "quality": {"grade": None, "flags": ["open"]}})
    store.put_ride(col.snapshot()["ride"], {"v": 0, "quality": {"grade": None, "flags": ["open"]}})
    assert len(store.rides(closed_only=False)) == 1 and store.rides() == []
    store.close()                                         # power cut: nothing else is written
    store2 = BatteryStore(path)
    res = store2.close_open()
    assert res["rides_closed"] == 1
    r = store2.rides()[0]
    assert "truncated" in r["features"]["quality"]["flags"] and r["features"]["n_blk"] == 200
    assert r["analysis_ver"] == model.ANALYSIS_VERSION


def test_reanalyse_on_version_bump_and_schema_guard(tmp_path, monkeypatch):
    path = str(tmp_path / "b.db")
    store = BatteryStore(path)
    meta, tables = model.decode_record(open(REAL_RIDE, "rb").read())
    store.put_ride({"meta": meta, "tables": tables})
    assert store.reanalyse() == {"rides": 0, "rests": 0}
    store.conn().execute("UPDATE rides SET analysis_ver = 0, features = '{}'")
    store.conn().commit()
    assert store.reanalyse()["rides"] == 1
    assert store.rides()[0]["features"]["quality"]["grade"] == "A"
    store.conn().execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    store.conn().commit()
    store.close()
    with pytest.raises(RuntimeError):
        BatteryStore(path)


def test_richer_source_replaces_lite_ride(tmp_path):
    store = BatteryStore(str(tmp_path / "b.db"))
    meta, tables = model.decode_record(open(REAL_RIDE, "rb").read())
    lite = {"v": 1, "quality": {"grade": "L", "flags": ["lite_1min"]}}
    assert store.put_lite_ride(meta["start_ts"] - 30, meta["end_ts"] + 30, lite) is not None
    assert store.put_ride({"meta": meta, "tables": tables}) is not None
    rows = store.rides()
    assert len(rows) == 1 and rows[0]["source"] == "trip"
    assert store.put_lite_ride(meta["start_ts"] - 30, meta["end_ts"] + 30, lite) is None


# ── service + API ───────────────────────────────────────────────────────────

class _Msg:
    def __init__(self, device_id, data):
        self.device_id, self.data = device_id, data


def test_service_tap_to_stored_ride(tmp_path, monkeypatch):
    from cyberpunk_computer.battery import service as svcmod
    clock = [5000.0]
    monkeypatch.setattr(svcmod, "_boottime", lambda: clock[0])
    svc = BatteryHealthService(str(tmp_path / "b.db"))
    svc.start()
    try:
        deadline = time.time() + 5
        while not svc.status["running"] and time.time() < deadline:
            time.sleep(0.02)
        for k in range(8 * 90):
            clock[0] = 5000.0 + k * 0.125
            raw = int(200 * math.sin(k / 11.0)) & 0xFFF
            svc.tap(_Msg(1, {"i": "0x03B", "d": [raw >> 8, raw & 0xFF, 0, 220, 0], "bus": 0}))
            svc.tap(_Msg(1, {"i": "0x025", "d": [0] * 8, "bus": 0}))          # not wanted
            if k % 16 == 0:
                svc.tap(_Msg(1, {"a": "sub", "slot": 3, "i": "0x7EB", "d": [97, 206, 110, 128, 0] + [134, 44] * 14}))
        clock[0] += model.RIDE_GAP_S + 5                                       # IG-OFF
        deadline = time.time() + 8
        while svc.status["rides_stored"] == 0 and time.time() < deadline:
            time.sleep(0.05)
        assert svc.status["rides_stored"] == 1 and svc.dropped == 0
        s = svc.get_summary()
        assert s["quality"]["rides"] == 1 and s["live"]["counts"]["pack"] > 0
        assert s["rides"][0]["n_blk"] == 45
    finally:
        svc.stop()


def _serve(app):
    import socket
    import threading
    import uvicorn
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio"))
    server.install_signal_handlers = lambda: None
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    return server, t, port


def _http(port, path, method="GET", headers=None):
    import urllib.error
    import urllib.request
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method, headers=headers or {},
                                 data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_api_routes(tmp_path):
    from fastapi import FastAPI
    from cyberpunk_computer.battery import api, service as svcmod
    app = FastAPI()
    api.register(app, "/api/v1/battery", lambda: None)
    server, th, port = _serve(app)
    svc = BatteryHealthService(str(tmp_path / "b.db"))
    try:
        svcmod.set_active(None)
        assert _http(port, "/api/v1/battery/summary")[0] == 503
        meta, tables = model.decode_record(open(REAL_RIDE, "rb").read())
        rid = svc.store.put_ride({"meta": meta, "tables": tables})
        svc.store.set_cache("summary", summary.build_summary(svc.store))
        svcmod.set_active(svc)
        s = json.loads(_http(port, "/api/v1/battery/summary")[2])
        assert s["rides"][0]["id"] == rid and len(s["indicators"]) == 11
        d = json.loads(_http(port, "/api/v1/battery/rides/%d" % rid)[2])
        assert d["view"]["weak_block"] == 12 and len(d["series"]["dev_mv"]) == 180
        assert _http(port, "/api/v1/battery/rides/999")[0] == 404
        for name in ("rides", "blocks", "rests"):
            st, hdr, _ = _http(port, "/api/v1/battery/export/%s.csv" % name)
            assert st == 200 and hdr["content-type"].startswith("text/csv")
        assert _http(port, "/api/v1/battery/export/blocks.csv")[2].count(b"\n") == 15
        assert _http(port, "/api/v1/battery/export/ride/%d.csv" % rid)[2].count(b"\n") == 540
        assert _http(port, "/api/v1/battery/export/nothing.csv")[0] == 404
        assert _http(port, "/api/v1/battery/export/battery.db")[2][:15] == b"SQLite format 3"
        assert json.loads(_http(port, "/api/v1/battery/recompute", "POST")[2]) == {"accepted": True}
    finally:
        svcmod.set_active(None)
        server.should_exit = True
        th.join(5)


def test_backend_app_mounts_battery_routes_behind_auth():
    from cyberpunk_computer.api import app as app_mod

    class _Bridge:
        def latest_snapshot(self):
            return None

        def attach_loop(self, loop):
            pass
    app = app_mod.create_app(_Bridge(), db=object(), auth_token="secret-token")
    server, th, port = _serve(app)
    try:
        assert _http(port, "/api/v1/battery/summary")[0] == 401
        st, _, _ = _http(port, "/api/v1/battery/summary", headers={"Authorization": "Bearer secret-token"})
        assert st == 503
    finally:
        server.should_exit = True
        th.join(5)
