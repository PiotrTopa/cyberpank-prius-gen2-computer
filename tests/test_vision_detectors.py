"""Detector slot (vision/detectors): dataclasses, NullDetector, parsing of
recorded yolo_detect.py output (2026-10-19 drive), filtering, the subprocess
protocol against a fake helper, and the real experiment-venv detector (skipped
when .venv-vision or its model is absent)."""

import json
import os
import sys

import pytest

from cyberpunk_computer.vision.detectors import (
    DASHCAM_MOUNT_REGION, Detection, Detector, NullDetector, SubprocessDetector,
    SubprocessYoloDetector, TfliteDetector, filter_detections, parse_line, parse_result)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
RECORDED = os.path.join(FIXTURES, "yolo_detect_2026-10-19_sample.jsonl")
JPEG = os.path.join(FIXTURES, "dashcam_20261019_153601.jpg")


def recorded():
    with open(RECORDED) as f:
        return [json.loads(line) for line in f if line.strip()]


def by_frame(name):
    return next(r for r in recorded() if r.get("frame") == name)


# -- dataclasses / protocol ------------------------------------------------

def test_detection_geometry_and_roundtrip():
    d = Detection("car", 0.8, (10.0, 20.0, 50.0, 40.0), t=1792416134.0)
    assert d.width == 40 and d.height == 20 and d.area == 800
    assert d.center == (30.0, 30.0)
    assert Detection.from_dict(d.to_dict()) == d
    assert Detection.from_dict(d.to_dict(), t=5.0).t == 5.0
    with pytest.raises(Exception):
        d.score = 0.1   # frozen


def test_null_detector():
    n = NullDetector()
    assert isinstance(n, Detector)
    assert n.process("/no/such/frame.jpg", 0.0) == []
    n.close()


def test_tflite_detector_is_a_clear_stub():
    t = TfliteDetector("yolo11n_416_int8.tflite", labels=["person"])
    assert isinstance(t, Detector)
    with pytest.raises(NotImplementedError, match="numpy"):
        t.process(JPEG, 0.0)


# -- parsing recorded output -----------------------------------------------

def test_parse_recorded_coco_frame():
    dets = parse_result(by_frame("20261019_152621.jpg"), t=42.0)
    assert [d.cls for d in dets] == ["car", "stop sign", "car", "car"]
    car = dets[0]
    assert car.score == pytest.approx(0.7963)
    assert car.box == (250.8, 119.3, 412.7, 226.6)
    assert all(d.t == 42.0 for d in dets)


def test_parse_recorded_sign_frame():
    dets = parse_result(by_frame("20261019_153021.jpg"), t=0.0)
    assert dets[0].cls == "prio_priority_road" and dets[0].score > 0.7
    assert 15 < dets[0].width < 25    # signs are ~20 px at 640x360


def test_parse_rejects_banner_error_and_garbage():
    rows = recorded()
    assert parse_result(rows[0], 0.0) is None          # ready banner
    assert parse_result(rows[-1], 0.0) is None         # {"error": ...}
    assert parse_line("Loading model ...", 0.0) is None
    assert parse_line('{"detections": [{"cls": "car"}]}', 0.0) is None
    assert parse_line('{"frame": "a.jpg", "detections": []}', 0.0) == []


# -- filtering -------------------------------------------------------------

def test_filter_drops_mount_and_whole_bonnet_boxes():
    mount = parse_result(by_frame("20261019_155111.jpg"), 0.0)   # "bicycle" = phone mount
    assert [d.cls for d in mount] == ["bicycle"]
    assert filter_detections(mount, ignore_regions=[DASHCAM_MOUNT_REGION]) == []

    garage = parse_result(by_frame("20261019_152351.jpg"), 0.0)  # whole-frame truck/car
    kept = filter_detections(garage, max_area_frac=0.3)
    assert len(kept) == len(garage) - 2
    assert all(d.area <= 0.3 * 640 * 360 for d in kept)


def test_filter_score_and_classes():
    dets = parse_result(by_frame("20261019_153601.jpg"), 0.0)
    assert {d.cls for d in filter_detections(dets, classes={"car"})} == {"car"}
    assert all(d.score >= 0.5 for d in filter_detections(dets, min_score=0.5))


# -- subprocess protocol against a fake helper ------------------------------

FAKE_HELPER = r'''
import json, os, sys, time
rows = {r["frame"]: r for r in map(json.loads, open(sys.argv[1])) if "frame" in r}
mode = sys.argv[2]
print("noise before the banner", flush=True)
if mode == "silent":
    time.sleep(30)
print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    name = os.path.basename(line.strip())
    if mode == "stall":
        time.sleep(30)
    if mode == "die":
        sys.exit(1)
    print(json.dumps(rows.get(name, {"frame": name, "error": "no such file"})), flush=True)
'''


@pytest.fixture
def fake_helper(tmp_path):
    p = tmp_path / "fake_helper.py"
    p.write_text(FAKE_HELPER)
    return lambda mode="ok": [sys.executable, "-I", str(p), RECORDED, mode]


def test_subprocess_detector_roundtrip(fake_helper):
    det = SubprocessDetector(fake_helper(), timeout_s=5, start_timeout_s=10)
    try:
        a = det.process("/frames/20261019_152621.jpg", t=1.0)
        assert [d.cls for d in a][:2] == ["car", "stop sign"] and a[0].t == 1.0
        b = det.process("/frames/20261019_154021.jpg", t=2.0)   # same process, next frame
        assert b[0].cls == "mand_go_left"
        assert det.process("/frames/unknown.jpg", t=3.0) == []
        assert det.last_result["error"] == "no such file"
    finally:
        det.close()


@pytest.mark.parametrize("mode", ["stall", "die", "silent"])
def test_subprocess_detector_never_raises(fake_helper, mode):
    det = SubprocessDetector(fake_helper(mode), timeout_s=0.5, start_timeout_s=0.5,
                             restart_delay_s=60)
    try:
        assert det.process("/frames/20261019_152621.jpg", 0.0) == []
        assert det.process("/frames/20261019_152621.jpg", 0.0) == []   # in restart back-off
    finally:
        det.close()


def test_subprocess_detector_missing_binary():
    det = SubprocessDetector(["/nonexistent/helper"], restart_delay_s=0)
    assert det.process(JPEG, 0.0) == []


# -- the real thing (experiment venv) ----------------------------------------

@pytest.mark.skipif(not SubprocessYoloDetector.available(),
                    reason="experiment venv .venv-vision (torch + ultralytics + model) absent")
def test_subprocess_yolo_detector_on_real_frame():
    det = SubprocessYoloDetector(imgsz=416, conf=0.25)
    try:
        dets = det.process(JPEG, t=1792416134.0)
        assert det.last_result is not None and "error" not in det.last_result
        cars = [d for d in dets if d.cls == "car"]
        # 15:36:01: the car ahead-left, ~(155, 142)-(200, 176) in the recorded 640 run
        assert any(abs(d.center[0] - 177) < 10 and abs(d.center[1] - 159) < 10 for d in cars)
        assert all(0 <= d.box[0] < d.box[2] <= 640 and 0 <= d.box[1] < d.box[3] <= 360
                   for d in dets)
    finally:
        det.close()
