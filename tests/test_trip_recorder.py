"""Trip recorder segmentation: no trip churn while the car is parked.

2026-11-15: with ``BACKEND_RECORD=1`` and the car parked (ACC off) the phone
journal showed an "opened" + "discarded short trip" pair about once per
second: powerbox/BEAN traffic opened a trip and the next powerbox state
update (ACC off) closed it again. While the ignition is known to be off,
traffic must not open a trip, and discards are not logged at INFO.
"""

import json
import logging
import threading
import time

from cyberpunk_computer.io.ports import DEVICE_POWERBOX_BASE, DEVICE_CAN, RawMessage
from cyberpunk_computer.io.trip_recorder import RecordingConfig, TripRecorder


def _msg(dev=DEVICE_POWERBOX_BASE):
    return RawMessage(device_id=dev, timestamp=time.time(), data={"msg": "HB"})


def _rec(tmp_path, **kw):
    cfg = RecordingConfig(enabled=True, directory=str(tmp_path), **kw)
    r = TripRecorder(cfg)
    r.start()
    return r


def _files(tmp_path):
    return sorted(p.name for p in tmp_path.glob("trip_*.ndjson"))


def test_parked_traffic_does_not_churn_trips(tmp_path, caplog):
    r = _rec(tmp_path)
    caplog.set_level(logging.INFO, logger="cyberpunk_computer.io.trip_recorder")
    for _ in range(50):                       # ~50 s parked: heartbeats + ACC off
        r.log_incoming(_msg())
        r.on_ignition(False)
        r.log_incoming(_msg(DEVICE_POWERBOX_BASE + 3))   # BEAN line
        r.tick()
    assert not r.recording
    assert _files(tmp_path) == []
    assert r.discarded <= 1
    noisy = [m for m in caplog.messages if "discarded" in m or "opened" in m]
    assert len(noisy) <= 1, noisy
    r.stop()


def test_ignition_on_opens_and_off_closes(tmp_path, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    r = _rec(tmp_path)
    r.on_ignition(False)
    r.log_incoming(_msg())
    assert not r.recording
    r.on_ignition(True)
    assert r.recording
    for _ in range(30):
        clock["t"] += 1.0
        r.log_incoming(_msg(DEVICE_CAN))
        r.on_ignition(True)                   # repeated state updates are no-ops
    path = r.current_file
    r.on_ignition(False)
    assert not r.recording and path.exists()  # 30 s trip kept
    r.log_incoming(_msg())                    # parked again: nothing opens
    assert not r.recording
    r.stop()


def test_unknown_ignition_still_records_traffic(tmp_path):
    r = _rec(tmp_path)                        # no powerbox ACC report yet
    r.log_incoming(_msg(DEVICE_CAN))
    assert r.recording
    r.stop()


def test_without_ignition_bounding_traffic_opens(tmp_path):
    r = _rec(tmp_path, use_ignition=False)
    r.on_ignition(False)
    r.log_incoming(_msg())
    assert r.recording
    r.stop()


# ── GNSS fixes (position feeder): explicit opt-in only ───────────────────────

def _fix():
    # synthetic test-loop coordinates, not a real place
    return {"type": "gnss", "fix": "3d", "lat": 50.66, "lon": 17.92, "hacc_m": 6.0,
            "ts_mono_ns": 1, "source": "phone"}


def test_position_not_recorded_by_default(tmp_path):
    r = _rec(tmp_path, min_trip_seconds=0)    # include_position default False ("all" never implies it)
    r.on_ignition(True)
    r.log_position(_fix())
    r.log_incoming(_msg(DEVICE_CAN))
    time.sleep(0.4)                           # CommLogger writes on its own thread
    path = r.current_file
    r.stop()
    assert '"POS"' not in path.read_text()


def test_position_recorded_when_named_and_skipped_by_replay(tmp_path):
    from cyberpunk_computer.io.file_io import FileInputPort
    r = _rec(tmp_path, include_position=True, min_trip_seconds=0)
    r.log_position(_fix())                    # no trip open: dropped, does not open one
    assert not r.recording
    r.on_ignition(True)
    r.log_incoming(_msg(DEVICE_CAN))
    r.log_position(_fix())
    r.log_incoming(_msg(DEVICE_CAN))
    time.sleep(0.4)                           # CommLogger writes on its own thread
    path = r.current_file
    r.stop()
    lines = [l for l in path.read_text().splitlines() if '"POS"' in l]
    assert len(lines) == 1 and '"source":"phone"' in lines[0]
    port = FileInputPort(str(path))
    assert port.load()
    assert all('"POS"' not in e.raw_line for e in port._entries)


def test_record_include_position_token():
    import argparse
    from cyberpunk_computer.backend.__main__ import _build_recording
    base = dict(record=True, record_dir="x", record_segmentation="trip", record_idle_timeout=120.0,
                record_min_trip=15.0, record_max_file_mb=64.0, record_no_ignition=False,
                record_max_files=60, record_max_total_mb=512.0, record_max_age_days=30.0)
    assert not _build_recording(argparse.Namespace(record_include="all", **base)).include_position
    c = _build_recording(argparse.Namespace(record_include="all,position", **base))
    assert c.include_position and c.include_can


# ── 2026-11-17: reboot with a stale clock, NUL tails ─────────────────────────

def test_comm_logger_never_reuses_an_existing_file(tmp_path):
    from cyberpunk_computer.io.comm_logger import CommLogger, LogConfig
    old = tmp_path / "trip_20261116_115052_965.ndjson"
    old.write_text('{"meta": "comm_log"}\n{"ts": 1, "dir": "IN", "id": 1, "d": {}}\n')
    before = old.read_text()
    lg = CommLogger(filepath=str(old), config=LogConfig(directory=str(tmp_path)))
    assert lg.start()
    lg.log_raw("IN", 1, {"x": 1})
    lg.stop()
    assert old.read_text() == before                      # neither truncated nor appended
    assert lg.filepath.name == "trip_20261116_115052_965_1.ndjson"
    head = json.loads(lg.filepath.read_text().splitlines()[0])
    assert "boot_id" in head and "mono" in head


def test_redate_moves_pre_step_wall_times(tmp_path):
    from datetime import datetime
    from cyberpunk_computer.io.trip_recorder import redate_trip_file
    step = 82173.4
    t_old = datetime(2026, 11, 16, 11, 50, 52, 965000).timestamp()
    p = tmp_path / "trip_20261116_115052_965.ndjson"
    lines = [json.dumps({"meta": "comm_log", "created": datetime.fromtimestamp(t_old).isoformat()})]
    for k in range(5):                                     # 36 s on the stale clock
        t = t_old + k * 9
        lines.append(json.dumps({"ts": t, "dir": "IN", "id": 1,
                                 "d": {"i": "0x03B", "d": [0, 1], "ts": int(t * 1000)}}))
    lines.append(json.dumps({"ts": t_old + 36 + step + 1, "dir": "IN", "id": 1, "d": {}}))  # after NTP
    lines.append(json.dumps({"ts": 12.5, "dir": "IN", "id": 2, "d": {}}))     # device time: kept
    p.write_text("\n".join(lines) + "\n" + "\0" * 300)
    new = redate_trip_file(p, step, t_old + 36)
    assert not p.exists()
    assert new.name == "trip_" + datetime.fromtimestamp(t_old + step).strftime("%Y%m%d_%H%M%S_%f")[:-3] + ".ndjson"
    recs = [json.loads(l) for l in new.read_text().splitlines()]
    assert recs[0]["clock_step_s"] == round(step, 3)
    walls = [r["ts"] for r in recs[1:7]]
    assert walls[0] == t_old + step and walls[5] == t_old + 36 + step + 1
    assert recs[1]["d"]["ts"] == int(round((t_old + step) * 1000))
    assert recs[7]["ts"] == 12.5 and len(recs) == 8        # NUL tail dropped


def test_clock_step_closes_and_redates_the_open_trip(tmp_path, monkeypatch):
    clock = {"mono": 1000.0, "off": time.time() - 1000.0}   # = datetime.now() of the names
    monkeypatch.setattr(time, "monotonic", lambda: clock["mono"])
    monkeypatch.setattr(time, "time", lambda: clock["mono"] + clock["off"])
    r = _rec(tmp_path)
    r.on_ignition(True)
    first = r.current_file
    for _ in range(20):
        clock["mono"] += 1.0
        r.log_incoming(RawMessage(device_id=DEVICE_CAN, timestamp=time.time(), data={"i": "0x03B"}))
        r.tick()
    assert r.current_file == first and r.clock_steps == 0
    clock["off"] += 82173.0                                # NTP steps the clock
    r.tick()
    assert r.clock_steps == 1 and r.recording and r.current_file != first
    for _ in range(50):                                    # background re-date
        if not first.exists():
            break
        threading.Event().wait(0.02)
    assert not first.exists()
    redated = [p for p in tmp_path.glob("trip_*.ndjson") if p != r.current_file]
    assert len(redated) == 1
    recs = [json.loads(l) for l in redated[0].read_text().splitlines()]
    assert recs[0]["clock_step_s"] == 82173.0
    assert all(rr["ts"] > time.time() - 100 for rr in recs if "ts" in rr and rr.get("dir") == "IN")
    r.stop()


def test_readers_tolerate_nul_tails(tmp_path):
    from cyberpunk_computer.io.comm_logger import iter_ndjson
    from cyberpunk_computer.io.file_io import FileInputPort
    p = tmp_path / "trip_x.ndjson"
    good = [{"meta": "comm_log", "created": "2026-11-17T10:24:19"},
            {"ts": 1794907459.0, "dir": "IN", "id": 1, "d": {"i": "0x03B", "d": [0, 1, 0, 220, 4]}},
            {"ts": 1794907460.0, "dir": "IN", "id": 1, "d": {"i": "0x03B", "d": [0, 2, 0, 220, 5]}}]
    raw = "\n".join(json.dumps(g) for g in good) + "\n" + '{"ts": 17949' + "\0" * 949
    p.write_bytes(raw.encode())
    with open(p, "rb") as f:
        assert len(list(iter_ndjson(f))) == 3
    port = FileInputPort(str(p))
    assert port.start()
    from cyberpunk_computer.battery.backfill import feed_trip_file
    seen = []

    class Dec:
        def feed(self, wall, dev, d):
            seen.append(d)
    feed_trip_file(str(p), Dec(), None, {})
    assert len(seen) == 2
