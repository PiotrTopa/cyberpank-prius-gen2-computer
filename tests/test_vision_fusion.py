"""Speed-limit fusion (map prior + camera sightings), replay/CLI, service → store."""

import os

import pytest

from cyberpunk_computer.api.serialization import serialize_state
from cyberpunk_computer.frontend.deserialize import deserialize_state
from cyberpunk_computer.state.store import Store
from cyberpunk_computer.vision.fusion.service import SpeedLimitService
from cyberpunk_computer.vision.fusion.speed_limit import (
    FusionParams, Sighting, SpeedLimitFusion, load_sightings, load_track, main, replay,
)
from cyberpunk_computer.vision.maps import (
    BASIS_BUILT_UP, BASIS_CLASS, BASIS_EXPLICIT, OsmSpeedMap, Position, SpeedLimit,
)

HERE = os.path.dirname(__file__)
TRACK = os.path.join(HERE, "fixtures", "osm", "track_old_town_to_armii_krajowej.csv")
SIGHTINGS = os.path.join(HERE, "fixtures", "osm", "sightings_armii_krajowej.csv")
SYNTH = os.path.join(HERE, "fixtures", "osm", "synthetic_extract.json")


def map_lim(v, basis=BASIS_EXPLICIT, conf=0.8, built_up=True, urban=50, rural=90, name="Testowa"):
    return SpeedLimit(v, basis=basis, way_name=name, confidence=conf, built_up=built_up,
                      urban_default_kmh=urban, rural_default_kmh=rural)


def drive(f, limit, t0, t1, odo0, v_mps=15.0, junction_at=None):
    """Feed one fix per second; returns the last result."""
    r = None
    for t in range(t0, t1):
        odo = odo0 + (t - t0) * v_mps
        r = f.update_map(float(t), limit, odo,
                         at_junction=junction_at is not None and abs(odo - junction_at) < 20)
    return r


def S(v, t, score=0.9, side="right", kind="limit"):
    return Sighting(v, score, side, float(t), kind)


def test_map_prior_only():
    f = SpeedLimitFusion()
    r = f.update_map(0.0, map_lim(50), 0.0)
    assert (r.value_kmh, r.source, r.basis, r.road_name) == (50, "map", BASIS_EXPLICIT, "Testowa")
    assert r.changed


def test_agreeing_sighting_raises_confidence():
    f = SpeedLimitFusion()
    base = f.update_map(0.0, map_lim(50, conf=0.6), 0.0).confidence
    r = f.add_sighting(S(50, 1, score=0.8))
    assert r.value_kmh == 50 and r.source == "map+camera" and r.confidence == pytest.approx(base + 0.2)
    assert f.add_sighting(S(50, 2, score=1.0)).confidence == pytest.approx(min(1.0, base + 0.45))


def test_disagreeing_overrides_after_two_on_our_side():
    f = SpeedLimitFusion()
    drive(f, map_lim(50), 0, 5, 0.0)
    r1 = f.add_sighting(S(70, 5))
    assert r1.value_kmh == 50 and "pending" in r1.note
    r2 = f.add_sighting(S(70, 6))
    assert (r2.value_kmh, r2.source, r2.changed) == (70, "camera", True)
    assert r2.confidence == pytest.approx(0.6 + 0.3 * 0.9)


def test_disagreeing_ignored_left_side_junction_low_score_or_inconsistent():
    f = SpeedLimitFusion()
    drive(f, map_lim(50), 0, 3, 0.0)
    for s in (S(70, 3, side="left"), S(70, 4, side="left")):
        assert "left side" in f.add_sighting(s).note
    assert f.add_sighting(S(70, 5, score=0.3)).note.startswith("ignored")
    f.update_map(6.0, map_lim(50), 100.0, at_junction=True)
    assert "junction" in f.add_sighting(S(70, 6)).note
    assert "junction" in f.add_sighting(S(70, 7)).note
    f.update_map(8.0, map_lim(50), 150.0)
    f.add_sighting(S(70, 8))
    r = f.add_sighting(S(60, 9))           # a different value restarts the count
    assert r.value_kmh == 50 and "1/2" in r.note
    # left side counts on a oneway road
    f.update_map(10.0, map_lim(50), 160.0, oneway=True)
    f.add_sighting(S(30, 10, side="left"))
    assert f.add_sighting(S(30, 11, side="left")).value_kmh == 30


def test_override_expires_at_next_junction_after_grace():
    f = SpeedLimitFusion()
    drive(f, map_lim(50), 0, 2, 0.0)
    f.add_sighting(S(30, 2))
    f.add_sighting(S(30, 2))
    # sign right after a junction (within the 50 m grace): still 30
    r = drive(f, map_lim(50), 2, 4, 15.0, junction_at=20.0)
    assert r.value_kmh == 30
    # next junction 300 m on: back to the map's 50
    r = drive(f, map_lim(50), 4, 30, 45.0, junction_at=345.0)
    assert r.value_kmh == 50 and r.source == "map"


def test_override_expires_by_distance_and_age():
    p = FusionParams(expire_at_junction=False, override_max_distance_m=500, override_max_age_s=60)
    f = SpeedLimitFusion(p)
    drive(f, map_lim(50), 0, 2, 0.0)
    f.add_sighting(S(70, 2)); f.add_sighting(S(70, 2))
    assert drive(f, map_lim(50), 2, 30, 15.0).value_kmh == 70        # 420 m
    notes = [f.update_map(float(t), map_lim(50), 435.0 + (t - 30) * 15.0).note
             for t in range(30, 40)]
    assert f.last.value_kmh == 50 and any("expired (distance)" in n for n in notes)
    f.add_sighting(S(70, 40)); f.add_sighting(S(70, 40))
    notes = [f.update_map(float(t), map_lim(50), 600.0).note for t in range(40, 105)]
    assert f.last.value_kmh == 50 and any("expired (age)" in n for n in notes)  # standing 65 s


def test_repeated_agreeing_sign_refreshes_override():
    p = FusionParams(expire_at_junction=False, override_max_distance_m=500)
    f = SpeedLimitFusion(p)
    drive(f, map_lim(50), 0, 2, 0.0)
    f.add_sighting(S(70, 2)); f.add_sighting(S(70, 2))
    drive(f, map_lim(50), 2, 30, 15.0)                                 # 435 m from the sign
    f.add_sighting(S(70, 30))                                          # repeated sign
    assert drive(f, map_lim(50), 30, 50, 450.0).value_kmh == 70       # +285 m: still valid


def test_end_of_limit_resets_to_default():
    f = SpeedLimitFusion()
    drive(f, map_lim(70, built_up=False), 0, 3, 0.0)     # map: explicit 70 outside built-up
    r = f.add_sighting(S(None, 3, kind="end_limit"))
    assert (r.value_kmh, r.source, r.basis) == (90, "default", BASIS_CLASS)
    # ends at the next junction like any sign limit: map's 70 back
    assert drive(f, map_lim(70, built_up=False), 3, 40, 45.0, junction_at=400.0).value_kmh == 70


def test_built_up_signs_reset_defaults():
    f = SpeedLimitFusion()
    imp = map_lim(90, basis=BASIS_CLASS, built_up=False)       # map thinks: rural
    drive(f, imp, 0, 2, 0.0)
    f.add_sighting(S(70, 2)); f.add_sighting(S(70, 2))
    r = f.add_sighting(S(None, 3, kind="built_up_begin"))
    assert (r.value_kmh, r.source, r.basis) == (50, "camera", BASIS_BUILT_UP)
    # survives junctions (area sign), but an explicit map limit still wins
    assert drive(f, imp, 3, 60, 45.0, junction_at=300.0).value_kmh == 50
    assert drive(f, map_lim(60), 60, 62, 900.0).value_kmh == 60
    r = f.add_sighting(S(None, 62, kind="built_up_end"))
    assert drive(f, imp, 62, 64, 930.0).value_kmh == 90
    # a built-up sign on the other side of the road is ignored
    assert "ignored" in f.add_sighting(S(None, 65, side="left", kind="built_up_begin")).note


def test_map_debounce_and_hold():
    f = SpeedLimitFusion()
    f.update_map(0.0, map_lim(50), 0.0)
    assert f.update_map(1.0, map_lim(70), 15.0).value_kmh == 50   # one fix on a crossing way
    assert f.update_map(2.0, map_lim(50), 30.0).value_kmh == 50
    f.update_map(3.0, map_lim(70), 45.0)
    assert f.update_map(4.0, map_lim(70), 60.0).value_kmh == 70   # two in a row: taken
    assert f.update_map(10.0, None, 100.0).value_kmh == 70        # match lost: held
    r = f.update_map(15.0, None, 150.0)
    assert r.value_kmh is None and r.source == "none"


# ── replay / CLI / loaders ────────────────────────────────────────────────

@pytest.fixture(scope="module")
def gdansk():
    return OsmSpeedMap.load()


def test_track_loader_derives_heading_and_speed(tmp_path):
    pts = load_track(TRACK)
    assert len(pts) > 400
    assert all(p.heading_deg is not None and p.speed_kmh is not None for p in pts[1:])
    assert 50 < sorted(p.speed_kmh for p in pts)[len(pts) // 2] < 70
    gpx = tmp_path / "t.gpx"
    gpx.write_text('<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
                   '<trkpt lat="54.35" lon="18.65"><time>2026-10-19T12:00:00Z</time></trkpt>'
                   '<trkpt lat="54.3501" lon="18.65"><time>2026-10-19T12:00:01Z</time></trkpt>'
                   '</trkseg></trk></gpx>')
    g = load_track(str(gpx))
    assert len(g) == 2 and g[1].heading_deg == pytest.approx(0.0, abs=0.1)
    assert g[1].speed_kmh == pytest.approx(40.0, abs=0.5) and g[0].t == 1792411200.0


def test_replay_track_through_extract(gdansk):
    tl = replay(load_track(TRACK), gdansk)
    values = [d.value_kmh for d in tl]
    assert None not in values
    names = {d.road_name for d in tl}
    assert {"Podwale Przedmiejskie", "Aleja Armii Krajowej"} <= names
    assert values[-1] == 100 and 70 in values and 80 in values
    assert sum(1 for d in tl if d.changed) <= 8          # debounce keeps it calm


def test_replay_with_sightings(gdansk):
    track, sights = load_track(TRACK), load_sightings(SIGHTINGS)
    tl = replay(track, gdansk, sights)
    notes = [d.note for d in tl]
    assert any(n.startswith("override 60") for n in notes)
    assert any(d.source == "map+camera" for d in tl)
    assert any("expired" in n for n in notes)


def test_cli_prints_timeline(capsys):
    assert main(["--track", TRACK, "--sightings", SIGHTINGS]) == 0
    out = capsys.readouterr().out
    assert "Aleja Armii Krajowej" in out and "fixes" in out


# ── service → store ───────────────────────────────────────────────────────

def test_service_action_applies_to_store():
    svc = SpeedLimitService(osm_map=OsmSpeedMap.load(SYNTH))
    store = Store()
    north = lambda n, t: Position((5400000 + n * 0.9044) / 1e5, (1800000 + 2 * 1.5285) / 1e5,
                                  0.0, 50.0, t)
    store.dispatch(svc.update(north(40, 0.0)))
    v = store.state.vision
    assert (v.speed_limit_kmh, v.speed_limit_source, v.road_name, v.position_ok) == \
        (70, "map", "Testowa", True)
    assert 0.5 < v.speed_limit_confidence <= 1.0
    store.dispatch(svc.add_sighting(Sighting(70, 0.9, "right", 1.0)))
    assert store.state.vision.speed_limit_source == "map+camera"
    back = deserialize_state(serialize_state(store.state))
    assert back.vision == store.state.vision
    store.dispatch(svc.update(Position(50.0, 20.0, 0.0, 50.0, 2.0)))   # outside the extract
    assert store.state.vision.position_ok is False and store.state.vision.road_name is None
    assert svc.odo_m == pytest.approx(0.0)
