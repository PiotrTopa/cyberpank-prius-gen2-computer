// npm test: tsc -p tests (to .unit-tests/), then node --test
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  aheadItems, distM, enu, fmtM, fusedRows, headingUp, holdRelease, mapBanners, reasonText, shapeAt, shapeSlice,
  type FusedMsg, type MapMsg,
} from '../src/lib/mapLayer.ts';

const NOW = 1_800_000_000;

const FUSED: FusedMsg = {
  map: { available: true, state: 'good', reason: null, age_s: 0.2, match_confidence: 0.97 },
  speed_limit: {
    limit_kmh: 40, source: 'sign', agreement: 'conflict', sign: 'B-33',
    vision: { limit_kmh: 40, kind: 'sign', sign: 'B-33', confidence: 0.71 },
    map: { limit_kmh: 70, source: 'maxspeed', detail: 'tagged', confidence: 0.9 },
  },
  built_up: { value: true, source: 'map_tagged', vision: { value: false, source: 'env' }, map: { value: true, source: 'tagged' } },
  place: { label: 'city_street', source: 'fused', vision: { label: 'city_street', confidence: 0.5 }, map: { label: 'city_street' } },
  lanes: { count: 2, forward: 1, backward: 1, ego_index: 0, oncoming_left: true, source: 'map:tagged+vision',
    vision: { lane_to_left: true, lane_to_right: false }, map: { total: 2, forward: 1, backward: 1, source: 'tagged' } },
  tunnel: { in_tunnel: false, source: 'none', vision: { in_tunnel: false, confidence: 0.9 }, map: { in_tunnel: false } },
  road: { class: 'primary', name: 'Ozimska', ref: 'DK46' },
  ahead: {
    junctions: [{ dist_m: 120, kind: 'junction', minor: true }, { dist_m: 412, kind: 'roundabout', branches: 4, turn_deg: 30 }],
    speed_limit_changes: [{ dist_m: 300, limit_kmh: 50, source: 'implicit', rule: 'built_up' }, { dist_m: 655, limit_kmh: 90, source: 'maxspeed' }],
    built_up_change: { dist_m: 655, to: false, source: 'landuse', confidence: 0.4 },
  },
};

test('banners: not running, api down, vision only, synthetic, residue, good', () => {
  assert.equal(mapBanners({ rsState: 'off', nowS: NOW })[0].title, 'roadsense not running');
  assert.equal(mapBanners({ rsState: 'running', apiDown: true, nowS: NOW })[0].title, 'roadsense api not answering');
  const vo = mapBanners({ rsState: 'running', fused: { map: { available: false, reason: 'no_position' } }, nowS: NOW });
  assert.deepEqual(vo.map((b) => b.title), ['VISION ONLY']);
  assert.match(vo[0].detail ?? '', /no position/);
  const md = mapBanners({ rsState: 'running', mapdDown: true, nowS: NOW });
  assert.match(md[0].detail ?? '', /mapd not running/);
  const syn = mapBanners({ rsState: 'running', feeder: { synthetic: true, session: { by: 'dashboard' } }, fused: FUSED, nowS: NOW });
  assert.deepEqual(syn.map((b) => b.title), ['SYNTHETIC POSITION', 'MAP + CAMERA']);
  const residue = mapBanners({
    rsState: 'running', feeder: { source: 'headunit', last_session_end: { reason: 'car_moving', at: NOW - 60 } },
    map: { state: 'dead_reckoning', input: { gnss: { source: 'headunit', fix: 'none' } }, position: { lat: 0, lon: 0, dr_distance_m: 240 } },
    fused: FUSED, nowS: NOW,
  });
  assert.deepEqual(residue.map((b) => b.title), ['synthetic residue', 'DEAD RECKONING']);
  const dr = mapBanners({ rsState: 'running', map: { state: 'dead_reckoning', position: { lat: 0, lon: 0, dr_distance_m: 240 } },
    fused: { map: { available: true } }, nowS: NOW });
  assert.deepEqual(dr.map((b) => b.title), ['DEAD RECKONING']);
  assert.match(dr[0].detail ?? '', /240 m/);
  // an old session does not make a residue
  const old = mapBanners({ rsState: 'running', feeder: { last_session_end: { at: NOW - 3600 } },
    map: { state: 'dead_reckoning' }, nowS: NOW });
  assert.ok(!old.some((b) => b.title === 'synthetic residue'));
});

test('fused rows: values, provenance and agreement', () => {
  const rows = fusedRows(FUSED);
  assert.deepEqual(rows.map((r) => r.fact), ['speed limit', 'built-up', 'place', 'lanes', 'tunnel']);
  const sl = rows[0];
  assert.equal(sl.value, '40 km/h');
  assert.equal(sl.source, 'sign');
  assert.equal(sl.vision, '40 · sign B-33');
  assert.equal(sl.map, '70 · maxspeed tagged');
  assert.equal(sl.agree, 'conflict');
  assert.equal(rows[1].agree, 'conflict');            // env says outside, the map tag says built-up
  assert.equal(rows[2].agree, 'agree');
  assert.equal(rows[3].agree, 'agree');               // left lane is oncoming, nothing right: one own lane
  assert.equal(rows[3].value, '2 (1 own) · ego 1');
  assert.equal(rows[4].agree, 'agree');
  assert.deepEqual(fusedRows(null), []);
});

test('lanes: a lane seen on the right with one own lane on the map is a conflict', () => {
  const r = fusedRows({ lanes: { count: 2, forward: 1, oncoming_left: true, vision: { lane_to_left: true, lane_to_right: true },
    map: { total: 2, forward: 1, backward: 1 } } })[0];
  // oncoming_left true: own lanes needed = 1 + right = 2 > 1
  assert.equal(r.agree, 'conflict');
});

test('hold release lists junction, tagged change, boundary and the cap', () => {
  const h = holdRelease(FUSED);
  assert.ok(h && !h.held);
  assert.match(h.lines[0], /next real junction in 412 m \(roundabout\)/);   // the minor one at 120 m does not count
  assert.match(h.lines[1], /655 m/);                                       // implicit change at 300 m does not count
  assert.match(h.lines[2], /built-up boundary in 655 m \(landuse: inferred/);
  assert.match(h.lines[3], /5 km/);
  const held = holdRelease({ ...FUSED, speed_limit: { ...FUSED.speed_limit, source: 'sign_held' } });
  assert.ok(held?.held);
  assert.match(held.by, /held by the map/);
  const mw = holdRelease({ ...FUSED, road: { class: 'motorway' } });
  assert.match(mw?.lines[0] ?? '', /motorway/);
  assert.equal(holdRelease({ ...FUSED, speed_limit: { source: 'map', limit_kmh: 70 } }), null);
  const nomap = holdRelease({ ...FUSED, map: { available: false } });
  assert.deepEqual(nomap?.lines, []);
});

test('ahead items: merged and sorted', () => {
  const m: MapMsg = { ahead: {
    junctions: [{ dist_m: 412, kind: 'junction', branches: 3, turn_deg: -2 }],
    curves: [{ start_m: 300, apex_m: 380, end_m: 480, min_radius_m: 80, direction: 'left', angle_deg: -88 }],
    features: [{ kind: 'traffic_signals', dist_m: 230, confidence: 0.9 }],
    tunnel: { in_tunnel: false, entry_m: 840, length_m: 2310, exit_m: 3150 },
    built_up_change: { dist_m: 655, to: false, source: 'landuse' },
    speed_limit_changes: [{ dist_m: 655, limit_kmh: 90, source: 'implicit', rule: 'outside' }],
    road_change: { dist_m: 1210, class: 'secondary', ref: '414' },
  } };
  const it = aheadItems(m.ahead);
  assert.deepEqual(it.map((x) => x.label), ['traffic signals', 'curve left', 'junction', 'limit 90 km/h',
    'built-up area ends', 'tunnel entry', 'road changes', 'tunnel exit']);
  assert.equal(it[1].detail, 'R 80 m · 88° · 180 m long');
  assert.equal(it[1].tone, 'amber');
  assert.equal(it[2].detail, '3 branches · straight on');
  assert.equal(it[3].detail, 'implicit (outside)');
  assert.deepEqual(aheadItems(null), []);
});

test('geometry: enu, heading-up, shape sampling, distance', () => {
  const [e, n] = enu(50.0, 17.0, 50.001, 17.0);
  assert.ok(Math.abs(e) < 1e-9 && Math.abs(n - 111.13) < 0.01);
  const [e2] = enu(50.0, 17.0, 50.0, 17.001);
  assert.ok(Math.abs(e2 - 71.55) < 0.05);
  // heading east: a point to the east is straight ahead, a point to the north is on the left
  const [r1, f1] = headingUp([10, 0], 90);
  assert.ok(Math.abs(r1) < 1e-9 && Math.abs(f1 - 10) < 1e-9);
  const [r2, f2] = headingUp([0, 10], 90);
  assert.ok(Math.abs(r2 + 10) < 1e-9 && Math.abs(f2) < 1e-9);
  const shape = { s: [0, 20, 40], x: [0, 20, 38], y: [0, 0, 6] };
  assert.deepEqual(shapeAt(shape, 10), [0, 10]);
  assert.deepEqual(shapeAt(shape, 30), [3, 29]);
  assert.equal(shapeAt(shape, 50), null);
  assert.deepEqual(shapeSlice(shape, 10, 30), [[0, 10], [0, 20], [3, 29]]);
  assert.ok(Math.abs(distM({ lat: 50, lon: 17 }, { lat: 50.001, lon: 17 }) - 111.13) < 0.01);
});

test('wording', () => {
  assert.equal(reasonText('low_match_confidence'), 'map match below 0.5');
  assert.equal(reasonText('something_new'), 'something new');
  assert.equal(fmtM(1500), '1.5 km');
  assert.equal(fmtM(12.4), '12 m');
  assert.equal(fmtM(null), '--');
});
