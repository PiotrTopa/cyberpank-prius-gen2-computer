"""Light features (vision/light_features.py): sampler CSV rows and one real
dashcam JPEG (2026-10-19 drive, sun straight in the picture)."""

import csv
import os
import shutil

import pytest

from cyberpunk_computer.vision import ffmpeg
from cyberpunk_computer.vision.frames import DirectorySource, parse_frame_time
from cyberpunk_computer.vision.light_features import chroma, extract, from_sample_row

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CSV = os.path.join(FIXTURES, "dashcam_drive_home_2026-10-19.csv")
JPEG = os.path.join(FIXTURES, "dashcam_20261019_153601.jpg")


def rows():
    with open(CSV, newline="") as f:
        return list(csv.DictReader(f))


def test_csv_fixture_complete():
    assert len(rows()) == 174


def test_from_sample_row_garage():
    r = rows()[0]   # 15:22:14, garage
    f = from_sample_row(r)
    assert f.t == 1792416134
    assert f.sky_y == 99.5 and f.road_y == 56.5 and f.y_avg == 71.0
    assert f.sky_chroma == pytest.approx(chroma(127.6, 126.7))
    assert f.sky_chroma < 2
    assert f.sky_clip is None


def test_from_sample_row_daylight_is_blue():
    f = from_sample_row(next(r for r in rows() if r["file"] == "20261019_153601.jpg"))
    assert f.sky_y == 181.0
    assert f.sky_chroma > 25      # U 152 / V 103


def test_from_sample_row_rejects_missing_and_sentinel():
    assert from_sample_row({"epoch": "1", "sky_y": "", "sky_u": "128", "sky_v": "128"}) is None
    assert from_sample_row({"epoch": "1", "sky_y": "-1", "sky_u": "128", "sky_v": "128"}) is None
    f = from_sample_row({"epoch": "1", "sky_y": "100", "sky_u": "128", "sky_v": "128",
                         "road_y": "-1", "sky_clip": "0.2"})
    assert f.road_y is None and f.sky_clip == 0.2


def test_extract_returns_none_without_file():
    assert extract("/nonexistent/frame.jpg", t=0) is None


def test_extract_returns_none_without_ffprobe(monkeypatch):
    monkeypatch.setattr(ffmpeg, "FFPROBE", "/nonexistent/ffprobe")
    assert extract(JPEG, t=0) is None


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
def test_extract_real_jpeg_matches_sampler():
    f = extract(JPEG, t=1792416961)
    assert f is not None
    # same numbers the sampler wrote for this frame (CSV row 20261019_153601)
    assert f.sky_y == pytest.approx(181.0, abs=0.2)
    assert f.sky_u == pytest.approx(152.1, abs=0.2)
    assert f.sky_v == pytest.approx(103.4, abs=0.2)
    assert f.road_y == pytest.approx(81.0, abs=0.2)
    assert f.y_avg == pytest.approx(116.5, abs=0.2)
    # sun in the frame: a clear share of the sky band is clipped (> 250)
    assert 0.10 < f.sky_clip < 0.30


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
def test_extract_path_with_special_characters(tmp_path):
    d = tmp_path / "we ird,dir:x[1]"
    d.mkdir()
    p = d / "a'b;c.jpg"
    shutil.copy(JPEG, p)
    f = extract(str(p), t=0)
    assert f is not None and f.sky_y == pytest.approx(181.0, abs=0.2)


def test_directory_source_order_and_times(tmp_path):
    for name in ("20261019_153611.jpg", "20261019_153601.jpg", "notes.txt", "x.jpg"):
        (tmp_path / name).write_bytes(b"")
    src = DirectorySource(str(tmp_path))
    frames = list(src)
    assert [os.path.basename(f.path) for f in frames] == [
        "20261019_153601.jpg", "20261019_153611.jpg", "x.jpg"]
    assert frames[1].t - frames[0].t == 10
    assert frames[0].t == parse_frame_time("20261019_153601.jpg")
    assert src.grab() is None


def test_directory_source_missing_folder():
    assert DirectorySource("/nonexistent/folder").grab() is None
