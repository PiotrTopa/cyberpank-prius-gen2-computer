// npm test: tsc -p tests (to .unit-tests/), then node --test
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  MARKER_KEY, canAutoResume, clearMarker, fmtMmSs, frameOverlay, readMarker, writeMarker,
  type StorageLike,
} from '../src/lib/cameraPreview.ts';

function memStore(): StorageLike & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => { data.set(k, v); },
    removeItem: (k) => { data.delete(k); },
  };
}

test('live and fresh frame is shown as is', () => {
  assert.deepEqual(frameOverlay('live', 0.4), { stale: false, text: null });
  assert.deepEqual(frameOverlay('live', 5), { stale: false, text: null });
});

test('stale frame is greyed with its age even while "live"', () => {
  const o = frameOverlay('live', 33.2);
  assert.equal(o.stale, true);
  assert.equal(o.text, 'frame stale — frame 33 s old');
});

test('re-enumerating camera: the last frame is never presented as live', () => {
  const o = frameOverlay('enumerating', 33);
  assert.equal(o.stale, true);
  assert.equal(o.text, 'camera re-enumerating… — frame 33 s old');
  assert.equal(frameOverlay('waiting_for_frame', 1).stale, true);   // phase wins over age
  assert.equal(frameOverlay('error', null).text, 'camera error');
});

test('marker round trip and storage failures', () => {
  const s = memStore();
  assert.equal(readMarker(s), null);
  writeMarker(s, { t: 1000, leaseId: 'abcd' });
  assert.deepEqual(readMarker(s), { t: 1000, leaseId: 'abcd' });
  s.data.set(MARKER_KEY, '{bad json');
  assert.equal(readMarker(s), null);
  clearMarker(s);
  assert.equal(s.data.size, 0);
  const broken: StorageLike = {
    getItem: () => { throw new Error('blocked'); },
    setItem: () => { throw new Error('blocked'); },
    removeItem: () => { throw new Error('blocked'); },
  };
  assert.equal(readMarker(broken), null);
  writeMarker(broken, { t: 1, leaseId: null });   // must not throw
  clearMarker(broken);
  assert.equal(readMarker(null), null);
});

test('auto-resume only with a recent marker and a server grace/active state', () => {
  const now = 10_000_000;
  const m = { t: now - 60_000, leaseId: 'ab' };
  assert.equal(canAutoResume(m, { phase: 'grace', grace: true }, now), true);
  assert.equal(canAutoResume(m, { phase: 'live', active: true }, now), true);
  assert.equal(canAutoResume(m, { phase: 'off', active: false, grace: false }, now), false); // cold camera
  assert.equal(canAutoResume(null, { phase: 'grace', grace: true }, now), false);           // never previewed here
  assert.equal(canAutoResume({ t: now - 16 * 60_000, leaseId: null }, { grace: true }, now), false);
  assert.equal(canAutoResume(m, null, now), false);
});

test('mm:ss', () => {
  assert.equal(fmtMmSs(180), '3:00');
  assert.equal(fmtMmSs(65.4), '1:05');
  assert.equal(fmtMmSs(-3), '0:00');
});
