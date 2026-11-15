// Chart primitives of the battery-health tab. Dark HUD surface only (the
// dashboard has one theme). Series colours follow the entity, status colours
// (green / amber / red) are reserved for levels and thresholds.
import { useRef, useState, type ReactNode } from 'react';
import {
  CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Scatter, Tooltip, XAxis, YAxis,
} from 'recharts';
import { cx } from '../../lib/format';
import { divergingColor, fmtDate, fmtNum, type Level } from '../../lib/battery';
import { LEVEL_LABEL, LEVEL_TONE } from '../../lib/battery';
import { Chip, Dot } from '../ui';

import { C, TICK, TIP } from '../../lib/batteryTheme';

export const LevelChip = ({ level, className }: { level: Level; className?: string }) => (
  <Chip tone={LEVEL_TONE[level]} className={className}>
    <Dot tone={LEVEL_TONE[level]} /> {LEVEL_LABEL[level]}
  </Chip>
);

export const Legend = ({ items }: { items: { color: string; label: string; dash?: boolean; dot?: boolean }[] }) => (
  <div className="flex flex-wrap gap-x-4 gap-y-1 text-[0.65rem] text-slate-400">
    {items.map((it) => (
      <span key={it.label} className="inline-flex items-center gap-1.5">
        {it.dot
          ? <span className="h-2 w-2 rounded-full" style={{ background: it.color }} />
          : <span className="w-4 border-t-2" style={{ borderColor: it.color, borderTopStyle: it.dash ? 'dashed' : 'solid' }} />}
        {it.label}
      </span>
    ))}
  </div>
);

export const Empty = ({ children }: { children: ReactNode }) => (
  <div className="text-xs text-slate-500 border border-dashed border-ink-500 px-3 py-6 text-center">{children}</div>
);

/** Tiny inline trend line (no axes): the last points of a normalised series. */
export function Sparkline({ values, color = C.a, width = 96, height = 24 }: {
  values: (number | null)[]; color?: string; width?: number; height?: number;
}) {
  const v = values.filter((x): x is number => x != null);
  if (v.length < 2) return <svg width={width} height={height} aria-hidden />;
  const lo = Math.min(...v), hi = Math.max(...v);
  const span = hi - lo || 1;
  const pts = v.map((y, i) => `${(i / (v.length - 1)) * (width - 4) + 2},${height - 3 - ((y - lo) / span) * (height - 6)}`);
  return (
    <svg width={width} height={height} aria-hidden>
      <polyline points={pts.join(' ')} fill="none" stroke={color} strokeWidth={1.5} />
      <circle cx={width - 2} cy={Number(pts[pts.length - 1].split(',')[1])} r={2} fill={color} />
    </svg>
  );
}

/** Time series over months: per-ride points (raw, grey) and the normalised series; thresholds as lines. */
export function TrendChart({ series, unit, digits = 1, normalised, watch, act, change, height = 240 }: {
  series: [number, number | null, number | null][];
  unit: string; digits?: number; normalised: boolean;
  watch?: number | null; act?: number | null;
  change?: { ts: number } | null; height?: number;
}) {
  if (series.length === 0) return <Empty>No points yet.</Empty>;
  if (series.length < 3) return <Empty>{series.length} point{series.length === 1 ? '' : 's'} so far: a chart starts at three.</Empty>;
  const data = series.map(([ts, raw, norm]) => ({ ts, raw, norm }));
  const ys = data.flatMap((d) => [d.raw, d.norm]).filter((x): x is number => x != null);
  let lo = Math.min(...ys), hi = Math.max(...ys);
  // a threshold is drawn only when it lies near the data: the axis stays on the data
  const near = (t?: number | null) => t != null && t >= lo - (hi - lo + 1e-9) * 1.5 && t <= hi + (hi - lo + 1e-9) * 1.5;
  for (const t of [watch, act]) if (near(t)) { lo = Math.min(lo, t as number); hi = Math.max(hi, t as number); }
  const pad = (hi - lo || Math.abs(hi) * 0.1 || 1) * 0.08;
  const t0 = data[0].ts, t1 = data[data.length - 1].ts;
  const spanDays = (t1 - t0) / 86400;
  return (
    <div className="flex flex-col gap-2">
      <ResponsiveContainer width="100%" height={height}>
        <ComposedChart data={data} margin={{ top: 6, right: 10, bottom: 2, left: -8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={C.grid} />
          <XAxis dataKey="ts" type="number" scale="time" domain={[t0 - 1, t1 + 1]} tick={TICK} stroke={C.axis} minTickGap={40}
            tickFormatter={(t: number) => fmtDate(t, spanDays < 3).slice(spanDays < 3 ? 5 : 2, spanDays < 3 ? 16 : 10)} />
          <YAxis domain={[lo - pad, hi + pad]} tick={TICK} stroke={C.axis} width={52}
            tickFormatter={(v: number) => fmtNum(v, digits)} />
          <Tooltip {...TIP} labelFormatter={(t) => fmtDate(Number(t))}
            formatter={(v, name) => [`${fmtNum(Number(v), digits + 1)} ${unit}`, name === 'norm' ? (normalised ? 'normalised' : 'value') : 'as measured']} />
          {near(watch) && <ReferenceLine y={watch as number} stroke={C.watch} strokeDasharray="6 4"
            label={{ value: 'watch', fill: C.watch, fontSize: 10, position: 'insideTopRight' }} />}
          {near(act) && <ReferenceLine y={act as number} stroke={C.act} strokeDasharray="6 4"
            label={{ value: 'act', fill: C.act, fontSize: 10, position: 'insideTopRight' }} />}
          {change && <ReferenceLine x={change.ts} stroke={C.muted} strokeDasharray="2 4"
            label={{ value: 'change point', fill: C.muted, fontSize: 10, position: 'insideTopLeft' }} />}
          {normalised && <Scatter dataKey="raw" fill={C.raw} isAnimationActive={false} shape={<Dotmark r={2} />} />}
          <Line dataKey="norm" stroke={C.a} strokeWidth={2} dot={data.length < 40 ? { r: 2.5, fill: C.a, strokeWidth: 0 } : false}
            isAnimationActive={false} connectNulls />
        </ComposedChart>
      </ResponsiveContainer>
      {normalised && <Legend items={[
        { color: C.a, label: 'normalised (temperature removed)' }, { color: C.raw, label: 'as measured', dot: true },
      ]} />}
    </div>
  );
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const Dotmark = (p: any) => (p.cy == null || p.cx == null ? null : <circle cx={p.cx} cy={p.cy} r={p.r ?? 2} fill={p.fill} />);

/** Diverging colour legend for the heat map and the block bars. */
export const DivergingLegend = ({ scale, unit, badSign, digits = 0 }: {
  scale: number; unit: string; badSign: -1 | 1; digits?: number;
}) => {
  const lo = badSign < 0 ? -scale : scale;
  return (
    <div className="flex items-center gap-2 text-[0.65rem] text-slate-400 tnum">
      <span>worse {lo > 0 ? '+' : '−'}{fmtNum(Math.abs(lo), digits)} {unit}</span>
      <span className="h-2 w-28" style={{
        background: `linear-gradient(90deg, ${divergingColor(lo, scale, badSign)}, ${divergingColor(0, scale, badSign)}, ${divergingColor(-lo, scale, badSign)})`,
      }} />
      <span>better {-lo > 0 ? '+' : '−'}{fmtNum(Math.abs(lo), digits)} {unit}</span>
      <span className="text-slate-600">· median block = neutral</span>
    </div>
  );
};

/** Blocks (rows) against time (columns). Hover or tap a cell for the value. */
export function BlockHeatmap({ rows, cols, scale, badSign, unit, digits = 0, colLabel, onPick, highlight }: {
  rows: (number | null)[][];
  cols: number;
  scale: number; badSign: -1 | 1; unit: string; digits?: number;
  colLabel: (col: number) => string;
  onPick?: (col: number) => void;
  highlight?: number | null; // 1-based block
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<{ r: number; c: number; x: number; y: number; w: number } | null>(null);
  const nRows = rows.length;
  const ROW = 15;
  if (cols === 0) return <Empty>No graded rides with block data yet.</Empty>;
  const locate = (e: { clientX: number; clientY: number }) => {
    const box = ref.current?.getBoundingClientRect();
    if (!box) return null;
    const c = Math.floor(((e.clientX - box.left) / box.width) * cols);
    const r = Math.floor((e.clientY - box.top) / ROW);
    if (c < 0 || c >= cols || r < 0 || r >= nRows) return null;
    return { r, c, x: e.clientX - box.left, y: e.clientY - box.top, w: box.width };
  };
  const val = hover ? rows[hover.r][hover.c] : null;
  return (
    <div className="flex gap-2">
      <div className="flex flex-col text-[0.6rem] text-slate-500 tnum select-none" aria-hidden>
        {rows.map((_, r) => (
          <span key={r} style={{ height: ROW, lineHeight: `${ROW}px` }}
            className={cx('text-right w-5', highlight === r + 1 && 'text-hud-amber')}>{r + 1}</span>
        ))}
      </div>
      <div ref={ref} className="relative flex-1 min-w-0 cursor-crosshair"
        onMouseMove={(e) => setHover(locate(e))} onMouseLeave={() => setHover(null)}
        onClick={(e) => { const h = locate(e); setHover(h); if (h && onPick) onPick(h.c); }}>
        <svg width="100%" height={nRows * ROW} viewBox={`0 0 ${cols} ${nRows}`} preserveAspectRatio="none"
          role="img" aria-label="block deviation heat map" style={{ display: 'block' }}>
          {rows.map((row, r) => row.map((v, c) => (v == null ? null : (
            <rect key={`${r}-${c}`} x={c + 0.04} y={r + 0.06} width={0.92} height={0.88} fill={divergingColor(v, scale, badSign)} />
          ))))}
          {hover && <rect x={hover.c} y={0} width={1} height={nRows} fill="none" stroke="#cbd5e1" strokeWidth={0.06} vectorEffect="non-scaling-stroke" />}
        </svg>
        {hover && (
          <div className="absolute z-10 pointer-events-none bg-ink-900 border border-ink-500 px-2 py-1 text-[0.7rem] whitespace-nowrap"
            style={{ left: Math.max(0, Math.min(hover.x + 12, hover.w - 190)), top: Math.max(0, hover.y - 44) }}>
            <div className="text-slate-400">{colLabel(hover.c)}</div>
            <div className="text-slate-100 tnum">block {hover.r + 1}: {val == null ? 'no data' : `${val > 0 ? '+' : ''}${fmtNum(val, digits)} ${unit}`}</div>
          </div>
        )}
      </div>
    </div>
  );
}

/** One value per block, drawn from a zero baseline; colour = the same diverging scale as the heat map. */
export function BlockBars({ values, errors, scale, badSign, unit, digits = 0, highlight }: {
  values: (number | null)[]; errors?: (number | null)[] | null;
  scale: number; badSign: -1 | 1; unit: string; digits?: number; highlight?: number | null;
}) {
  const H = 150, mid = H / 2;
  const lim = Math.max(scale, ...values.map((v) => Math.abs(v ?? 0))) * 1.08;
  const [hover, setHover] = useState<number | null>(null);
  return (
    <div>
      <div className="relative">
        <svg width="100%" height={H} viewBox={`0 0 ${values.length} ${H}`} preserveAspectRatio="none" style={{ display: 'block' }}>
          <line x1={0} x2={values.length} y1={mid} y2={mid} stroke={C.axis} strokeWidth={1} vectorEffect="non-scaling-stroke" />
          {values.map((v, k) => {
            if (v == null) return null;
            const h = (Math.abs(v) / lim) * (mid - 4);
            const e = errors?.[k] ? ((errors[k] as number) / lim) * (mid - 4) : 0;
            const y = v >= 0 ? mid - h : mid;
            const tip = v >= 0 ? mid - h : mid + h;
            return (
              <g key={k} onMouseEnter={() => setHover(k)} onMouseLeave={() => setHover(null)} onClick={() => setHover(k)}>
                <rect x={k} y={0} width={1} height={H} fill="transparent" />
                <rect x={k + 0.18} y={y} width={0.64} height={Math.max(h, 0.5)} fill={divergingColor(v, scale, badSign)}
                  stroke={hover === k || highlight === k + 1 ? '#cbd5e1' : 'none'} strokeWidth={1} vectorEffect="non-scaling-stroke" />
                {e > 0 && <line x1={k + 0.5} x2={k + 0.5} y1={tip - e} y2={tip + e} stroke={C.ink} strokeWidth={1} vectorEffect="non-scaling-stroke" />}
              </g>
            );
          })}
        </svg>
        <span className="absolute left-0 top-0 text-[0.6rem] text-slate-600 tnum">+{fmtNum(lim, digits)} {unit}</span>
        <span className="absolute left-0 bottom-0 text-[0.6rem] text-slate-600 tnum">−{fmtNum(lim, digits)} {unit}</span>
      </div>
      <div className="grid text-[0.6rem] text-slate-500 tnum text-center mt-1" style={{ gridTemplateColumns: `repeat(${values.length}, 1fr)` }}>
        {values.map((_, k) => (
          <span key={k} className={cx(highlight === k + 1 && 'text-hud-amber', hover === k && 'text-slate-100')}>{k + 1}</span>
        ))}
      </div>
      <div className="text-[0.7rem] text-slate-400 tnum h-4 mt-1">
        {hover != null && values[hover] != null
          ? `block ${hover + 1}: ${(values[hover] as number) > 0 ? '+' : ''}${fmtNum(values[hover], digits)} ${unit}${errors?.[hover] ? ` ± ${fmtNum(errors[hover], digits)}` : ''}`
          : 'hover or tap a bar for its value'}
      </div>
    </div>
  );
}

/** Horizontal share bars (e.g. time per current bin). One hue: magnitude only. */
export function ShareBars({ labels, values, unit, digits = 0 }: { labels: string[]; values: number[]; unit: string; digits?: number }) {
  const max = Math.max(...values, 1e-9);
  return (
    <div className="flex flex-col gap-1">
      {labels.map((l, k) => (
        <div key={l} className="grid grid-cols-[5.5rem_1fr_4.5rem] items-center gap-2 text-[0.7rem]">
          <span className="text-slate-500 tnum">{l}</span>
          <div className="h-2 bg-ink-850">
            <div className="h-full bg-hud-cyan/80" style={{ width: `${(values[k] / max) * 100}%` }} />
          </div>
          <span className="text-slate-300 tnum text-right">{fmtNum(values[k], digits)} {unit}</span>
        </div>
      ))}
    </div>
  );
}
