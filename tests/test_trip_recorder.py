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
