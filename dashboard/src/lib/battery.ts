// Battery-health view model: types of /api/v1/battery/* and pure helpers
// (no React, no DOM: unit-tested in tests/battery.test.ts).

export type Level = 'ok' | 'watch' | 'act' | 'unknown';

export interface Trend {
  n: number;
  span_days: number | null;
  value: number | null;
  value_sigma: number | null;
  baseline: number | null;
  slope_30d: number | null;
  lo_30d: number | null;
  hi_30d: number | null;
  p: number | null;
  significant: boolean;
  log: boolean;
  status: string;
  change: { ts: number; p: number; before: number; after: number } | null;
  coef: { value: number; source: string; pairs: number; learned: number | null } | null;
  series: [number, number | null, number | null][]; // ts, raw, normalised
}

export interface Threshold { watch: number; act: number; dir: 'high' | 'low'; unit: string; origin: string; trend_min?: number }

export interface Indicator {
  key: string;
  title: string;
  value: number | null;
  unit: string;
  level: Level;
  sigma: number | null;
  n: number;
  basis: string;
  can: string;
  cannot: string;
  threshold?: Threshold | null;
  trend: string | null;
  block?: number | null;
  [extra: string]: unknown;
}

export interface Heatmap {
  cols: { ts: number; n: number; ride_id?: number }[];
  dev25_mv: (number | null)[][];
  dev0_mv: (number | null)[][];
  dr_pct: (number | null)[][];
}

export interface RideRow {
  id: number; start_ts: number; dur_s: number | null; source: string; dist_km: number | null;
  grade: string | null; flags: string[]; t_mean: number | null;
  soc_start: number | null; soc_end: number | null; soc_min: number | null; soc_max: number | null;
  ah_thru: number | null; spread_ref_mv: number | null; spread0_mv: number | null; spread_all_mv: number | null;
  weak_block: number | null; weak_dev_mv: number | null; r_sum_mohm: number | null; r_sum25_mohm: number | null;
  max_rel: number | null; pct_per_ah: number | null; ready_v: number | null; n_steps: number | null; n_blk: number | null;
}

export interface RestRow {
  id: number; start_ts: number; end_ts: number; open: boolean; source: string; dur_h: number | null;
  marks: Record<string, number> | null; v_min: number | null; v_end: number | null; v_rested: number | null;
  ma_mean: number | null; mah: number | null; temp_mean: number | null; low_min: number | null; crit_min: number | null;
  slope_mv_per_h: number | null; cap_proxy_ah: number | null; coverage: number | null; acc_min: number | null;
}

export interface BlocksLatest {
  ride_id: number; ts: number; t_mean: number | null;
  dev0_mv: number[]; dev25_mv: number[]; dr_pct: number[] | null;
  r_mohm: number[] | null; r_se: number[] | null; ecu_mohm: number[] | null;
  rest_dev_mv: number[] | null; dis_dev_mv: number[] | null; chg_dev_mv: number[] | null;
}

export interface BatterySummary {
  generated_ts: number;
  compute_s: number;
  overall: { hv: Level; aux: Level };
  indicators: Indicator[];
  trends: Record<string, Trend>;
  blocks: {
    latest: BlocksLatest | null;
    heatmaps: { rides?: Heatmap; weeks?: Heatmap };
    weakest: [number, number, number, number][]; // ts, ride id, block, dev mV
    weak_share: number[] | null;
    persistent: { block: number; count: number; of: number } | null;
    r_max_block: number | null;
    ecu_sum_mohm: number | null;
    arrhenius_b?: number | null;
    block_trends: { block: number; dev25: BlockTrend; dr_pct: BlockTrend }[] | null;
  };
  self_discharge: { block: number; mv_per_day: number; per_block: number[]; n: number; method: 'drift' | 'pairs' } | null;
  aux: { rests: RestRow[]; pb_offset: number | null };
  usage: {
    total: Record<string, number>;
    full_cycles_equiv: number | null;
    hist_s: number[] | null;
    months: { month: string; rides: number; ah_thru: number; hours: number; km: number; half_cycles: number; frac_low: number | null; frac_high: number | null }[];
    limits: [number, number, number][];
  };
  quality: {
    grades: Record<string, number>; flags: Record<string, number>; rides: number; with_blocks: number;
    with_pack_stream: number; with_21cf_full: number; with_21d0: number; lite: number; blk_rate_hz: number | null;
    last_ride_ts: number | null; last_ride_age_h: number | null; rests: number; rest_coverage_median: number | null;
    grade_meaning: Record<string, string>;
  };
  rides: RideRow[];
  stats: Record<string, number | string | null>;
  thresholds: Record<string, Threshold>;
  live?: { running: boolean; ride_open: boolean; queue: number; rides_stored: number; last_error: string | null;
    summary_s: number | null; analysis_s: number | null; counts: Record<string, number>; dropped: number };
}

export interface BlockTrend { value: number | null; slope_30d: number | null; significant: boolean }

export interface RideDetail {
  id: number; start_ts: number; end_ts: number; source: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  features: any;
  view: { dev0_mv: number[]; dev25_mv: number[]; dr_pct: number[] | null; weak_block: number; weak_dev25_mv: number } | null;
  series: {
    bucket_s: number;
    pack: ([number, number, number, number] | null)[]; // I mean, I min, I max, V
    dev_mv: (number[] | null)[];
    blk_i: (number | null)[];
    spread_mv: (number | null)[];
    slow: Record<string, (number | null)[]>;
    scatter: { weak: number; strong: number; pts: [number, number, number][] } | null;
    wake: { t: number[]; v: number[] };
  } | null;
}

export interface RestDetail {
  id: number; start_ts: number; end_ts: number; closed: boolean;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  features: any;
  series: { t_min: number[]; v: number[]; vmin: number[]; ma: (number | null)[]; temp: (number | null)[]; acc: number[] } | null;
}

// ── status ───────────────────────────────────────────────────────────────────

export const LEVEL_LABEL: Record<Level, string> = { ok: 'OK', watch: 'WATCH', act: 'ACT', unknown: 'NO DATA' };
export const LEVEL_TONE: Record<Level, 'green' | 'amber' | 'red' | 'dim'> = {
  ok: 'green', watch: 'amber', act: 'red', unknown: 'dim',
};

// ── formatting ───────────────────────────────────────────────────────────────

export function fmtNum(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return '--';
  return v.toFixed(digits);
}

/** Digits that suit a value of this unit. */
export function unitDigits(unit: string): number {
  if (unit === 'V' || unit === '%/Ah') return 2;
  if (unit === 'mV' || unit === 'mA' || unit === 'mΩ') return 0;
  return 1;
}

export function fmtDate(ts: number | null | undefined, withTime = true): string {
  if (!ts) return '--';
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  const day = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  return withTime ? `${day} ${p(d.getHours())}:${p(d.getMinutes())}` : day;
}

export function fmtDur(s: number | null | undefined): string {
  if (s == null) return '--';
  if (s < 3600) return `${Math.round(s / 60)} min`;
  const h = Math.floor(s / 3600);
  return `${h} h ${String(Math.round((s % 3600) / 60)).padStart(2, '0')}`;
}

export function fmtHours(h: number | null | undefined): string {
  if (h == null) return '--';
  if (h < 1) return `${Math.round(h * 60)} min`;
  if (h < 48) return `${h.toFixed(1)} h`;
  return `${(h / 24).toFixed(1)} d`;
}

/** One line for a trend: slope per 30 days with its 95 % interval, or why there is none. */
export function trendText(tr: Trend | undefined | null, unit: string): string {
  if (!tr || tr.n === 0) return 'no data';
  if (tr.slope_30d == null || tr.lo_30d == null || tr.hi_30d == null) return tr.status;
  const k = tr.log ? 100 : 1;
  const u = tr.log ? '%' : unit;
  const sig = (x: number) => {
    const a = Math.abs(x * k);
    const d = a >= 100 ? 0 : a >= 10 ? 1 : a >= 1 ? 2 : 3;
    return `${x >= 0 ? '+' : '−'}${a.toFixed(d)}`;
  };
  return `${sig(tr.slope_30d)} ${u} / 30 d  [${sig(tr.lo_30d)} … ${sig(tr.hi_30d)}]${tr.significant ? '' : '  not significant'}`;
}

// ── diverging colour scale (heat map, block bars) ────────────────────────────
// Two poles and a neutral midpoint. "bad" (red) = the block is worse than the
// pack median: lower voltage, or higher resistance.

const MID: [number, number, number] = [29, 37, 49];     // ink-700
const BAD: [number, number, number] = [255, 92, 87];    // hud-red
const GOOD: [number, number, number] = [110, 231, 242]; // hud-cyan

export function divergingColor(v: number | null | undefined, scale: number, badSign: -1 | 1 = -1): string {
  if (v == null || !Number.isFinite(v)) return 'transparent';
  const x = Math.max(-1, Math.min(1, v / scale));
  const pole = x * badSign > 0 ? BAD : GOOD;
  const a = Math.abs(x);
  const c = MID.map((m, i) => Math.round(m + (pole[i] - m) * a));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

/** Symmetric colour-scale limit: the 95th percentile of |v|, rounded up to 1/2/5 x 10^n. */
export function niceScale(rows: (number | null)[][], floor = 1): number {
  const vals: number[] = [];
  for (const r of rows) for (const v of r) if (v != null && Number.isFinite(v)) vals.push(Math.abs(v));
  if (vals.length === 0) return floor;
  vals.sort((a, b) => a - b);
  const p = Math.max(floor, vals[Math.min(vals.length - 1, Math.floor(0.95 * vals.length))]);
  const e = Math.pow(10, Math.floor(Math.log10(p)));
  for (const m of [1, 2, 5, 10]) if (m * e >= p) return m * e;
  return 10 * e;
}

export const HEAT_METRICS = {
  dev25_mv: { label: 'under load (50 A, 25 °C)', unit: 'mV', badSign: -1 as const, floor: 50, digits: 0,
    help: 'block voltage against the pack median at 50 A discharge' },
  dev0_mv: { label: 'at zero current', unit: 'mV', badSign: -1 as const, floor: 50, digits: 0,
    help: 'block voltage against the pack median at rest (charge imbalance)' },
  dr_pct: { label: 'resistance', unit: '%', badSign: 1 as const, floor: 5, digits: 1,
    help: 'block resistance above the median block' },
};
export type HeatMetric = keyof typeof HEAT_METRICS;

/** Bin labels of the current histogram (edges in A; > 0 = discharge). */
export const HIST_LABELS = ['< −80', '−80…−40', '−40…−20', '−20…−5', '±5', '5…20', '20…40', '40…80', '> 80'];

export const FLAG_TEXT: Record<string, string> = {
  '21cf_first_frame_only': '21CF answer cut to its first frame: no battery temperatures 1–3, limits in kW, delta SOC or fan step',
  no_21cf: 'no 21CF answers',
  no_block_poll: 'no block voltages (21CE not answered or switched off)',
  block_poll_gappy: 'block voltage polls arrived at under half their nominal rate',
  no_pack_stream: 'no broadcast current/voltage (panda): ampere-hours from the 2 s polls only',
  no_temperature: 'no battery temperature',
  cold: 'battery below 0 °C: resistance is strongly non-linear, kept out of the resistance trend',
  truncated: 'recording cut by a restart or power loss',
  low_current_span: 'too little current variation for the per-block fit',
  lite_1min: 'known only from the 1-minute metrics (backfill): no block data',
  open: 'ride in progress',
};
