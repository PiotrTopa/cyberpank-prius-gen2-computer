// npm test: tsc -p tests (to .unit-tests/), then node --test
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  describeEntry, evidenceText, groupTypes, modeAllowed, severityTone, type AlertType,
} from '../src/lib/alerts.ts';

const T = (over: Partial<AlertType>): AlertType => ({
  id: 'a', title: 'A', text: 'A', function: 'f', kind: 'alert', severity: 'notice', priority: 50,
  icon: 'info', sound: null, default_mode: 'shadow', advisory: false, description: '', needs: [],
  mode: 'shadow', mode_setting: 'shadow', on_blocked: null, fires_total: 0, fires_drive: 0,
  fires_session: 0, active: false, shown: false, muted_drive: false, last: null, ...over,
});

test('shadow fires read "would fire", on fires "fired"', () => {
  assert.equal(describeEntry({ ts: 1, event: 'fire', mode: 'shadow', text: 'LIMIT 50' }), 'would fire: LIMIT 50');
  assert.equal(describeEntry({ ts: 1, event: 'fire', mode: 'on', text: 'LIMIT 50' }), 'fired: LIMIT 50');
  assert.equal(describeEntry({ ts: 1, event: 'action', executed: false, action: 'recirc_on' }), 'would press: recirc_on');
  assert.equal(describeEntry({ ts: 1, event: 'mode', from: 'shadow', to: 'on' }), 'mode shadow → on');
});

test('"on" is not offered while a required input is missing', () => {
  const lane = T({ id: 'lane_departure', on_blocked: 'turn-signal state not decoded' });
  assert.equal(modeAllowed(lane, 'on'), false);
  assert.equal(modeAllowed(lane, 'shadow'), true);
  assert.equal(modeAllowed(T({}), 'on'), true);
});

test('types group by kind in a fixed order', () => {
  const g = groupTypes([T({ id: 'g', kind: 'event' }), T({ id: 'b', kind: 'alert', function: 'z' }),
    T({ id: 'c', kind: 'automation' }), T({ id: 'a', kind: 'alert', function: 'a' })]);
  assert.deepEqual(g.map(([k, ts]) => [k, ts.map((t) => t.id)]), [['alert', ['a', 'b']], ['automation', ['c']], ['event', ['g']]]);
});

test('evidence summary and severity tones', () => {
  assert.equal(evidenceText({ limit: 50, real: 61.234, src: 'sign', x: { a: 1 } }), 'limit=50 · real=61.23 · src=sign · x={"a":1}');
  assert.equal(severityTone('warning'), 'red');
  assert.equal(severityTone('notice'), 'amber');
  assert.equal(severityTone('info'), 'cyan');
});
