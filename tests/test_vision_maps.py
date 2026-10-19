"""OSM speed-limit map: loader, matching, Polish limit inference, extractor."""

import gzip
import json
import os
import sys
import time

import pytest

from cyberpunk_computer.vision.maps import (
    BASIS_BUILT_UP, BASIS_CLASS, BASIS_EXPLICIT, DEFAULT_EXTRACT, OsmSpeedMap, Position,
    implicit_limit, parse_maxspeed, speed_limit,
)

HERE = os.path.dirname(__file__)
SYNTH = os.path.join(HERE, "fixtures", "osm", "synthetic_extract.json")
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import osm_extract  # noqa: E402


def at(east_m, north_m, heading=None, speed=40.0, t=0.0):
    """Position in the synthetic fixture's frame (origin 54.0 N, 18.0 E)."""
    return Position((5400000 + north_m * 0.9044) / 1e5, (1800000 + east_m * 1.5285) / 1e5,
                    heading, speed, t)


@pytest.fixture(scope="module")
def synth():
    return OsmSpeedMap.load(SYNTH)


@pytest.fixture(scope="module")
def gdansk():
    return OsmSpeedMap.load(DEFAULT_EXTRACT)


# ── loader ────────────────────────────────────────────────────────────────

def test_loader_synthetic(synth):
    st = synth.stats()
    assert st["ways"] == 13 and st["places"] == 1 and st["explicit_maxspeed"] == 2
    w1 = next(w for w in synth.ways if w.id == 1)
    assert w1.junctions == [1]           # shares (0,100) with the residential street
    assert synth.meta["format"] == "osm-speed-extract"


def test_loader_reads_gzip_and_rejects_other_json(tmp_path, synth):
    ext = json.load(open(SYNTH))
    p = tmp_path / "x.json.gz"
    p.write_bytes(gzip.compress(json.dumps(ext).encode()))
    assert OsmSpeedMap.load(str(p)).stats()["ways"] == 13
    bad = tmp_path / "bad.json"
    bad.write_text('{"elements": []}')
    with pytest.raises(ValueError):
        OsmSpeedMap.load(str(bad))


def test_bundled_extract_is_small_and_complete(gdansk):
    assert os.path.getsize(DEFAULT_EXTRACT) < 5 * 1024 * 1024
    st = gdansk.stats()
    assert st["ways"] > 20000 and st["explicit_maxspeed"] > 5000


# ── matching ──────────────────────────────────────────────────────────────

def test_nearest_way(synth):
    m = synth.match(at(3, 50))
    assert m.way.id == 1 and m.distance_m == pytest.approx(3, abs=0.5)
    assert synth.match(at(150, 50)) is None          # 150 m from anything: no match
    assert synth.match(at(30, 50)) is None           # 30 m > 25 m radius


def test_service_penalty_prefers_main_road(synth):
    # 5 m from the parking aisle (x=-8), 3 m from the primary: primary anyway,
    # and even slightly closer to the aisle the primary wins by the penalty.
    assert synth.match(at(-3, 50, heading=0)).way.id == 1
    assert synth.match(at(-5, 50, heading=0)).way.id == 1


def test_heading_preference_at_crossing(synth):
    # 4 m from both the N-S primary and the E-W residential near the junction
    east = synth.match(at(4, 96, heading=90))
    north = synth.match(at(4, 96, heading=0))
    assert east.way.id == 2 and north.way.id == 1
    assert north.at_junction and north.junction_dist_m < 10


def test_direction_selects_forward_backward_limit(synth):
    n = synth.match(at(2, 50, heading=2))
    s = synth.match(at(2, 50, heading=182))
    assert n.direction == "forward" and speed_limit(n).value_kmh == 70
    assert s.direction == "backward" and speed_limit(s).value_kmh == 50
    # no heading (standing): unknown direction, the lower value (conservative)
    st = synth.match(at(2, 50, heading=0, speed=0))
    assert st.direction == "unknown" and speed_limit(st).value_kmh == 50


def test_oneway_pair_picks_the_carriageway_we_drive(synth):
    # midway between the northbound (x=300) and the southbound (x=315) oneway
    nb = synth.match(at(309, 100, heading=0))
    sb = synth.match(at(306, 100, heading=180))
    assert nb.way.id == 3 and nb.direction == "forward"
    assert sb.way.id == 4 and sb.direction == "forward"   # way 4 is drawn N->S
    # wrong way on a oneway only wins when nothing else is near
    lone = synth.match(at(2003, 100, heading=180))
    assert lone.way.id == 7 and lone.direction == "backward" and lone.cost > 40


def test_lookup_speed(gdansk):
    s, w, n, e = gdansk.meta["bbox"]
    pts = [Position(s + (n - s) * i / 50, w + (e - w) * j / 40, (i * 37) % 360, 50)
           for i in range(50) for j in range(40)]
    t0 = time.perf_counter()
    for p in pts:
        gdansk.match(p)
    per = (time.perf_counter() - t0) / len(pts)
    assert per < 0.005


def test_sample_match_near_home(gdansk):
    # 54.35, 18.65 is inside an Old Town block; within 25 m only a driveway.
    m = gdansk.match(Position(54.35, 18.65, 0.0, 30.0))
    assert m is not None and m.way.highway == "service"
    lim = speed_limit(m)
    assert lim.value_kmh == 50 and lim.basis == BASIS_BUILT_UP and lim.confidence < 0.3
    # a real street a few metres off: Podwale Przedmiejskie, explicit limit
    m2 = gdansk.match(Position(54.34628, 18.65298, None, None))
    assert m2.way.name == "Podwale Przedmiejskie"
    assert speed_limit(m2).basis == BASIS_EXPLICIT


# ── limit inference ───────────────────────────────────────────────────────

def lim_of(synth, wid, direction="forward"):
    from cyberpunk_computer.vision.maps import infer_limit
    return infer_limit(next(w for w in synth.ways if w.id == wid), direction)


def test_explicit_maxspeed(synth):
    r = lim_of(synth, 5)
    assert (r.value_kmh, r.basis, r.way_name, r.source) == (90, BASIS_EXPLICIT, "Wiejska", "map")
    assert r.confidence >= 0.8


def test_implicit_built_up(synth):
    assert (lim_of(synth, 2).value_kmh, lim_of(synth, 2).basis) == (50, BASIS_BUILT_UP)   # residential
    assert (lim_of(synth, 3).value_kmh, lim_of(synth, 3).basis) == (50, BASIS_BUILT_UP)   # PL:urban tag
    assert (lim_of(synth, 9).value_kmh, lim_of(synth, 9).basis) == (20, BASIS_BUILT_UP)   # living street
    w11 = lim_of(synth, 11)                                    # near a village node
    assert (w11.value_kmh, w11.basis, w11.built_up) == (50, BASIS_BUILT_UP, True)
    assert lim_of(synth, 3).confidence > w11.confidence       # tag beats place heuristic


def test_implicit_road_class(synth):
    assert (lim_of(synth, 8).value_kmh, lim_of(synth, 8).basis) == (140, BASIS_CLASS)     # motorway
    assert lim_of(synth, 7).value_kmh == 120                  # expressway, oneway carriageway
    assert lim_of(synth, 10).value_kmh == 100                 # rural dual primary, 2 lanes
    assert lim_of(synth, 6).value_kmh == 90                   # rural via neighbour 90
    assert lim_of(synth, 12).value_kmh == 90                  # nothing known: rural default
    assert lim_of(synth, 12).confidence < lim_of(synth, 6).confidence


def test_implicit_limit_table():
    assert implicit_limit({"highway": "trunk", "motorroad": "yes", "oneway": "no"}, False)[0] == 100
    assert implicit_limit({"highway": "primary"}, True) == (50, BASIS_BUILT_UP)
    assert implicit_limit({"highway": "primary"}, False) == (90, BASIS_CLASS)
    assert implicit_limit({"highway": "motorway"}, True)[0] == 140
    assert implicit_limit({"highway": "tertiary", "source:maxspeed": "PL:living_street"}, False)[0] == 20


def test_parse_maxspeed():
    assert parse_maxspeed("50") == (50, None)
    assert parse_maxspeed("30 mph") == (48, None)
    assert parse_maxspeed("50;70") == (50, None)
    assert parse_maxspeed("PL:urban") == (None, "PL:urban")
    assert parse_maxspeed("none") == (None, None)
    assert parse_maxspeed(None) == (None, None)


def test_zone_value_in_maxspeed_counts_as_explicit():
    from cyberpunk_computer.vision.maps import Way, infer_limit
    w = Way(1, {"highway": "residential", "maxspeed": "PL:zone30"}, [(0, 0), (0, 10)])
    r = infer_limit(w, "forward")
    assert (r.value_kmh, r.basis) == (30, BASIS_EXPLICIT)


# ── extractor ─────────────────────────────────────────────────────────────

OVERPASS_SAMPLE = {
    "osm3s": {"timestamp_osm_base": "2026-10-19T15:00:00Z"},
    "elements": [
        {"type": "way", "id": 2, "tags": {"highway": "primary", "maxspeed": "50", "name": "A",
                                          "surface": "asphalt"},
         "geometry": [{"lat": 54.35, "lon": 18.65}, {"lat": 54.351, "lon": 18.65}]},
        {"type": "way", "id": 1, "tags": {"highway": "footway"},
         "geometry": [{"lat": 54.35, "lon": 18.65}, {"lat": 54.351, "lon": 18.651}]},
        {"type": "way", "id": 3, "tags": {"highway": "service"},
         "geometry": [{"lat": 54.35, "lon": 18.65}, None]},
        {"type": "node", "id": 9, "lat": 54.348, "lon": 18.654,
         "tags": {"place": "city", "name": "Gdańsk"}},
    ]}


def test_extractor_convert_write_read_clip(tmp_path):
    bbox = osm_extract.bbox_around(54.35, 18.65, 15)
    assert bbox[0] < 54.35 < bbox[2] and abs((bbox[2] - bbox[0]) * 111.32 - 15) < 0.01
    q = osm_extract.build_query(bbox)
    assert q.count("out ") == 1 and "motorway" in q and "footway" not in q
    ext = osm_extract.convert(OVERPASS_SAMPLE, bbox)
    assert [w["id"] for w in ext["ways"]] == [2]           # footway dropped, clipped stub dropped
    assert ext["ways"][0]["t"] == {"highway": "primary", "maxspeed": "50", "name": "A"}
    assert ext["ways"][0]["c"] == [5435000, 1865000, 5435100, 1865000]
    assert ext["places"][0]["name"] == "Gdańsk" and ext["osm_base"].startswith("2026")
    p = str(tmp_path / "e.json.gz")
    osm_extract.write(ext, p)
    m = OsmSpeedMap.load(p)
    assert speed_limit(m.match(Position(54.3505, 18.65, 0, 30))).value_kmh == 50
    small = osm_extract.clip(osm_extract.read(p), (54.36, 18.6, 54.37, 18.7))
    assert small["ways"] == [] and small["places"] == []


def test_extractor_fetch_retries_with_backoff():
    import io
    import urllib.error
    calls, sleeps = [], []

    def opener(req, timeout):
        calls.append(req)
        if len(calls) < 3:
            raise urllib.error.HTTPError(req.full_url, 429, "busy", {}, None)
        return io.BytesIO(json.dumps({"elements": []}).encode())

    assert osm_extract.fetch("q", sleep=sleeps.append, opener=opener) == {"elements": []}
    assert len(calls) == 3 and sleeps == [30.0, 60.0]
    assert "cyberpunk-prius" in calls[0].get_header("User-agent")
