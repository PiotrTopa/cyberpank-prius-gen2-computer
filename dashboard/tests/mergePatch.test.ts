import { test } from 'node:test';
import assert from 'node:assert/strict';
import { applyMergePatch } from '../src/lib/mergePatch.ts';

test('merge patch merges, replaces and deletes', () => {
  const a = { pb: { v: 12, relays: [1, 0], x: 1 }, keep: { y: 2 } };
  const out = applyMergePatch(a, { pb: { v: 12.1, relays: [1, 1], x: null } }) as typeof a;
  assert.deepEqual(out, { pb: { v: 12.1, relays: [1, 1] }, keep: { y: 2 } });
  assert.equal(out.keep, a.keep);           // untouched branch shared
  assert.notEqual(out.pb, a.pb);            // changed branch is a new object
  assert.deepEqual(a.pb, { v: 12, relays: [1, 0], x: 1 }); // input not mutated
});
