// npm test: tsc -p tests (to .unit-tests/), then node --test
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  divergingColor, fmtDate, fmtDur, fmtHours, fmtNum, niceScale, trendText, unitDigits, type Trend,
} from '../src/lib/battery.ts';

const TREND: Trend = {
  n: 40, span_days: 90, value: 31.2, value_sigma: 0.4, baseline: 30, slope_30d: 0.51, lo_30d: 0.2, hi_30d: 0.83,
  p: 0.001, significant: true, log: false, status: 'ok', change: null, coef: null, series: [],
};

test('diverging colour: neutral midpoint, red on the bad side, clipped', () => {
  assert.equal(divergingColor(0, 100), 'rgb(29,37,49)');
  assert.equal(divergingColor(-100, 100), 'rgb(255,92,87)');
  assert.equal(divergingColor(-500, 100), 'rgb(255,92,87)');
  assert.equal(divergingColor(100, 100), 'rgb(110,231,242)');
  assert.equal(divergingColor(10, 10, 1), 'rgb(255,92,87)');      // resistance: high is bad
  assert.equal(divergingColor(null, 100), 'transparent');
});

test('colour-scale limit: 95th percentile rounded up to 1/2/5', () => {
  assert.equal(niceScale([[3, -12, 40, null], [-78, 5, 9, 11]], 50), 100);
  assert.equal(niceScale([[1, 2, 3]], 50), 50);
  assert.equal(niceScale([[null]], 5), 5);
  assert.equal(niceScale([[130, -140, 150]], 50), 200);
});

test('trend text: slope with interval, significance, log trends in percent, short history', () => {
  assert.equal(trendText(TREND, 'mV'), '+0.510 mV / 30 d  [+0.200 … +0.830]');
  assert.match(trendText({ ...TREND, significant: false }, 'mV'), /not significant$/);
  assert.equal(trendText({ ...TREND, log: true, slope_30d: 0.012, lo_30d: -0.004, hi_30d: 0.03 }, 'mOhm'),
    '+1.20 % / 30 d  [−0.400 … +3.00]');
  assert.equal(trendText({ ...TREND, slope_30d: null, status: 'history too short for a trend (3 points over 2 days; need 8 over 21)' }, 'V'),
    'history too short for a trend (3 points over 2 days; need 8 over 21)');
  assert.equal(trendText({ ...TREND, n: 0 }, 'V'), 'no data');
  assert.equal(trendText(undefined, 'V'), 'no data');
});

test('formatters', () => {
  assert.equal(fmtNum(null), '--');
  assert.equal(fmtNum(12.345, 2), '12.35');
  assert.equal(unitDigits('V'), 2);
  assert.equal(unitDigits('mV'), 0);
  assert.equal(fmtDur(1116), '19 min');
  assert.equal(fmtDur(4380), '1 h 13');
  assert.equal(fmtHours(0.5), '30 min');
  assert.equal(fmtHours(13.6), '13.6 h');
  assert.equal(fmtHours(72), '3.0 d');
  assert.match(fmtDate(1794602888), /^2026-11-1[34] \d\d:\d\d$/);
  assert.equal(fmtDate(0), '--');
});
