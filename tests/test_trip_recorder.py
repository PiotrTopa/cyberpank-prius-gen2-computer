"""Trip recorder segmentation: no trip churn while the car is parked.

2026-11-15: with ``BACKEND_RECORD=1`` and the car parked (ACC off) the phone
journal showed an "opened" + "discarded short trip" pair about once per
second: powerbox/BEAN traffic opened a trip and the next powerbox state
update (ACC off) closed it again. While the ignition is known to be off,
traffic must not open a trip, and discards are not logged at INFO.
"""

import logging
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
