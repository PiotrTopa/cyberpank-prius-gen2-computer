// Battery health: long-run statistics of the HV pack and the 12 V battery.
// Everything here is precomputed by the backend after each ride
// (GET /api/v1/battery/summary); nothing on this tab is live drive data.
import { useCallback, useEffect, useState, type ReactNode } from 'react';
import {
  Activity, BatteryCharging, BatteryMedium, Database, Download, Grid3x3, ListOrdered, RefreshCw, X,
} from 'lucide-react';
import {
  CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis,
} from 'recharts';
import { API_URL } from '../lib/api';
import { cx } from '../lib/format';
import {
  FLAG_TEXT, HEAT_METRICS, HIST_LABELS, fmtDate, fmtDur, fmtHours, fmtNum, niceScale, trendText, unitDigits,
  type BatterySummary, type HeatMetric, type Indicator, type RestDetail, type RideDetail, type Trend,
} from '../lib/battery';
import {
  BlockBars, BlockHeatmap, DivergingLegend, Empty, Legend, LevelChip, ShareBars, Sparkline, TrendChart,
} from '../components/battery/charts';
import { C, TICK, TIP } from '../lib/batteryTheme';
import { Btn, Chip, DataRow, Panel } from '../components/ui';

type View = 'overview' | 'blocks' | 'rides' | 'aux' | 'data';
const VIEWS: [View, string][] = [
  ['overview', 'Overview'], ['blocks', 'Blocks'], ['rides', 'Rides'], ['aux', '12 V'], ['data', 'Data & export'],
];

function useJson<T>(path: string | null): { data: T | null; error: string | null; loading: boolean; reload: () => void } {
  // one result object per request key, so a stale answer never shows under a new path
  const [res, setRes] = useState<{ key: string; data: T | null; error: string | null } | null>(null);
  const [tick, setTick] = useState(0);
  const key = `${path}#${tick}`;
  useEffect(() => {
    if (!path) return;
    let live = true;
    fetch(`${API_URL}${path}`, { credentials: 'same-origin' })
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `${r.status} ${r.statusText}`);
        return r.json();
      })
      .then((j) => { if (live) setRes({ key, data: j as T, error: null }); })
      .catch((e) => { if (live) setRes((old) => ({ key, data: old && old.key.split('#')[0] === path ? old.data : null, error: e instanceof Error ? e.message : String(e) })); });
    return () => { live = false; };
  }, [path, key]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  const mine = res != null && res.key.split('#')[0] === String(path);
  return { data: mine ? res.data : null, error: mine ? res.error : null, loading: res?.key !== key, reload };
}

export function BatteryTab() {
  const { data: s, error, loading, reload } = useJson<BatterySummary>('/battery/summary');
  const [view, setView] = useState<View>(() => {
    const h = window.location.hash.split('/')[1];
    return VIEWS.some(([id]) => id === h) ? (h as View) : 'overview';
  });
  const go = (v: View) => {
    setView(v);
    window.history.replaceState(null, '', v === 'overview' ? '#battery' : `#battery/${v}`);
  };
  // #battery/rides/<id> opens that ride (links, reload)
  const [rideId, setRideIdState] = useState<number | null>(() => {
    const p = window.location.hash.split('/');
    return p[1] === 'rides' && /^\d+$/.test(p[2] ?? '') ? Number(p[2]) : null;
  });
  const setRideId = (id: number | null) => {
    setRideIdState(id);
    window.history.replaceState(null, '', id == null ? '#battery/rides' : `#battery/rides/${id}`);
  };
  const openRide = (id: number) => { setView('rides'); setRideId(id); };

  if (!s) {
    return (
      <Panel title="Battery health" icon={BatteryMedium}>
        <Empty>{error ? `Battery module: ${error}` : 'Loading…'}</Empty>
        {error && <Btn onClick={reload} className="w-fit">Retry</Btn>}
      </Panel>
    );
  }
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <nav className="flex gap-1 p-1 hud-panel overflow-x-auto max-w-full">
          {VIEWS.map(([id, label]) => (
            <button key={id} onClick={() => go(id)}
              className={cx('whitespace-nowrap px-3.5 py-1 text-[0.7rem] uppercase tracking-wider transition-colors border',
                view === id ? 'text-hud-cyan border-hud-cyan/50 bg-white/[0.03]' : 'text-slate-500 hover:text-slate-200 border-transparent')}>
              {label}
            </button>
          ))}
        </nav>
        <div className="flex items-center gap-2 text-[0.65rem] text-slate-500">
          <span className="tnum">computed {fmtDate(s.generated_ts)}</span>
          <button onClick={reload} title="Reload" className={cx('p-1.5 hover:text-hud-cyan', loading && 'animate-spin')}>
            <RefreshCw size={13} />
          </button>
        </div>
      </div>
      {view === 'overview' && <Overview s={s} onOpenRide={openRide} go={go} />}
      {view === 'blocks' && <Blocks s={s} onOpenRide={openRide} />}
      {view === 'rides' && <Rides s={s} rideId={rideId} setRideId={setRideId} />}
      {view === 'aux' && <Aux s={s} />}
      {view === 'data' && <DataView s={s} />}
    </div>
  );
}

// ── overview ─────────────────────────────────────────────────────────────────

function historyNote(s: BatterySummary): string | null {
  const q = s.quality;
  const graded = (q.grades.A ?? 0) + (q.grades.B ?? 0);
  const first = s.stats.first_ts as number | null;
  const days = first ? (s.generated_ts - first) / 86400 : 0;
  if (graded < 8 || days < 21) {
    return `Short history: ${graded} graded ride${graded === 1 ? '' : 's'} with block data over ${days.toFixed(0)} days. `
      + 'Values are single measurements, levels are provisional, and no trend is given before 8 rides over 3 weeks.';
  }
  return null;
}

function Overview({ s, onOpenRide, go }: { s: BatterySummary; onOpenRide: (id: number) => void; go: (v: View) => void }) {
  const [sel, setSel] = useState<string>('weak_block');
  const hv = s.indicators.filter((i) => !i.key.startsWith('aux'));
  const aux = s.indicators.filter((i) => i.key.startsWith('aux'));
  const ind = s.indicators.find((i) => i.key === sel) ?? s.indicators[0];
  const tr = ind.trend ? s.trends[ind.trend] : undefined;
  const note = historyNote(s);
  const last = s.rides[0];
  return (
    <>
      {note && <div className="border border-hud-amber/50 text-hud-amber text-xs px-3 py-2">{note}</div>}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        <StatusPanel title="Hybrid battery" icon={BatteryCharging} level={s.overall.hv} list={hv} sel={sel} onSel={setSel} trends={s.trends} />
        <StatusPanel title="12 V battery" icon={BatteryMedium} level={s.overall.aux} list={aux} sel={sel} onSel={setSel} trends={s.trends} />
      </div>
      <Panel title={ind.title} code="TREND" icon={Activity} right={<LevelChip level={ind.level} />}>
        <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
          <span className="text-2xl tnum text-slate-50">
            {fmtNum(ind.value, unitDigits(ind.unit))}<span className="text-xs text-slate-500 ml-1">{ind.unit}</span>
            {ind.sigma != null && <span className="text-xs text-slate-500 ml-2">± {fmtNum(ind.sigma, unitDigits(ind.unit) + 1)}</span>}
          </span>
          <span className="text-xs text-slate-400 tnum">{trendText(tr, ind.unit)}</span>
          <span className="text-xs text-slate-600 tnum">{ind.n} point{ind.n === 1 ? '' : 's'}</span>
        </div>
        {tr && tr.series.length > 0
          ? <TrendChart series={tr.series} unit={ind.unit} digits={unitDigits(ind.unit)} normalised={!!tr.coef}
              watch={trendThreshold(ind, tr, 'watch')} act={trendThreshold(ind, tr, 'act')} change={tr.change} />
          : <Empty>No series for this indicator yet.</Empty>}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
          <Fact label="How it is measured">{ind.basis}</Fact>
          <Fact label="What it can tell">{ind.can}</Fact>
          <Fact label="What it cannot tell">{ind.cannot}</Fact>
        </div>
        {ind.threshold && (
          <p className="text-[0.65rem] text-slate-600">
            Limits: watch {ind.threshold.watch} / act {ind.threshold.act} {ind.threshold.unit}. Origin: {ind.threshold.origin}.
            {tr?.coef && ` Temperature coefficient: ${tr.coef.source} (${tr.coef.pairs} ride pairs).`}
          </p>
        )}
      </Panel>
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
        <Panel title="Usage" icon={ListOrdered} tone="amber">
          <DataRow label="Rides recorded" value={s.usage.total.rides} />
          <DataRow label="Time" value={fmtNum(s.usage.total.hours, 1)} unit="h" />
          <DataRow label="Distance" value={fmtNum(s.usage.total.km, 0)} unit="km" />
          <DataRow label="Charge throughput" value={fmtNum(s.usage.total.ah_thru, 1)} unit="Ah" />
          <DataRow label="Equivalent full cycles" value={s.usage.full_cycles_equiv} />
          <DataRow label="Near current limit" value={fmtNum(s.usage.total.near_limit_s, 0)} unit="s" />
        </Panel>
        <Panel title="Time per current" code="A · + = DISCHARGE" icon={Activity} tone="amber" className="lg:col-span-2">
          {s.usage.hist_s
            ? <ShareBars labels={HIST_LABELS} values={s.usage.hist_s.map((x) => x / 60)} unit="min" digits={1} />
            : <Empty>Needs a ride with the broadcast current stream.</Empty>}
        </Panel>
      </div>
      {last && (
        <div className="flex flex-wrap items-center gap-3 text-xs text-slate-500">
          <span>Last ride {fmtDate(last.start_ts)} · {fmtDur(last.dur_s)} · grade {last.grade}</span>
          <Btn onClick={() => onOpenRide(last.id)}>Open ride</Btn>
          <Btn onClick={() => go('blocks')}>Per-block view</Btn>
        </div>
      )}
    </>
  );
}

/** Thresholds are drawn only where the trend is in the threshold's own quantity. */
function trendThreshold(ind: Indicator, tr: Trend, which: 'watch' | 'act'): number | null {
  const th = ind.threshold;
  if (!th) return null;
  if (ind.key === 'r_pack') return tr.baseline != null ? tr.baseline * (1 + th[which] / 100) : null;
  if (ind.key === 'aux_rest') {
    const off = (ind.pb_offset as number | null) ?? 0;
    return th[which] - off;
  }
  if (ind.key === 'self_discharge' || ind.key === 'aux_drain') return null;
  return th[which];
}

const Fact = ({ label, children }: { label: string; children: ReactNode }) => (
  <div className="bg-ink-850 border border-ink-700 p-2.5">
    <div className="text-[0.6rem] uppercase tracking-[0.18em] text-slate-500 mb-1">{label}</div>
    <div className="text-slate-300 leading-relaxed">{children}</div>
  </div>
);

function StatusPanel({ title, icon, level, list, sel, onSel, trends }: {
  title: string; icon: typeof Activity; level: BatterySummary['overall']['hv']; list: Indicator[];
  sel: string; onSel: (k: string) => void; trends: Record<string, Trend>;
}) {
  return (
    <Panel title={title} icon={icon} right={<LevelChip level={level} />}>
      <div className="flex flex-col">
        {list.map((i) => {
          const tr = i.trend ? trends[i.trend] : undefined;
          const d = unitDigits(i.unit);
          return (
            <button key={i.key} onClick={() => onSel(i.key)}
              className={cx('grid grid-cols-[1fr_auto] sm:grid-cols-[1fr_6rem_auto_5.5rem] items-center gap-x-3 gap-y-0.5 px-2 py-2 text-left border-l-2 border-b border-b-ink-700 last:border-b-0',
                sel === i.key ? 'border-l-hud-cyan bg-white/[0.03]' : 'border-l-transparent hover:bg-white/[0.02]')}>
              <span className="min-w-0">
                <span className="block text-xs text-slate-200 truncate">{i.title}{i.block ? <span className="text-slate-500"> · block {i.block}</span> : null}</span>
                <span className="block text-[0.65rem] text-slate-500 tnum truncate">{trendText(tr, i.unit)}</span>
              </span>
              <span className="hidden sm:block"><Sparkline values={(tr?.series ?? []).slice(-40).map((p) => p[2])} /></span>
              <span className="text-sm tnum text-slate-50 text-right whitespace-nowrap">
                {fmtNum(i.value, d)}<span className="text-[0.65rem] text-slate-500 ml-1">{i.unit}</span>
              </span>
              <LevelChip level={i.level} className="justify-self-end hidden sm:inline-flex" />
            </button>
          );
        })}
      </div>
    </Panel>
  );
}

// ── blocks ───────────────────────────────────────────────────────────────────

function Blocks({ s, onOpenRide }: { s: BatterySummary; onOpenRide: (id: number) => void }) {
  const [metric, setMetric] = useState<HeatMetric>('dev25_mv');
  const [axis, setAxis] = useState<'weeks' | 'rides'>('weeks');
  const b = s.blocks;
  const m = HEAT_METRICS[metric];
  const hm = b.heatmaps[axis];
  const rows = hm ? hm[metric] : [];
  const scale = niceScale([...(rows ?? []), b.latest?.[metric] ?? []], m.floor);
  const weak = b.persistent?.block ?? null;
  if (!b.latest || !hm) {
    return <Panel title="Blocks" icon={Grid3x3}><Empty>No graded ride with block voltages yet (needs the 21CE poll and at least 5 minutes of driving).</Empty></Panel>;
  }
  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        {(Object.keys(HEAT_METRICS) as HeatMetric[]).map((k) => (
          <Btn key={k} active={metric === k} onClick={() => setMetric(k)}>{HEAT_METRICS[k].label}</Btn>
        ))}
        <span className="text-[0.65rem] text-slate-500">{m.help}</span>
      </div>
      <Panel title="Latest graded ride" code={`${fmtDate(b.latest.ts)} · ${fmtNum(b.latest.t_mean, 1)} °C`} icon={Grid3x3}
        right={<Btn onClick={() => onOpenRide(b.latest!.ride_id)}>Open ride</Btn>}>
        <BlockBars values={b.latest[metric] ?? []} scale={scale} badSign={m.badSign} unit={m.unit} digits={m.digits} highlight={weak} />
        <DivergingLegend scale={scale} unit={m.unit} badSign={m.badSign} digits={m.digits} />
      </Panel>
      <Panel title="Block deviation over time" code="14 BLOCKS × TIME" icon={Grid3x3}
        right={(
          <div className="flex gap-1">
            <Btn active={axis === 'weeks'} onClick={() => setAxis('weeks')} className="!py-1">Weeks</Btn>
            <Btn active={axis === 'rides'} onClick={() => setAxis('rides')} className="!py-1">Last rides</Btn>
          </div>
        )}>
        <BlockHeatmap rows={rows} cols={hm.cols.length} scale={scale} badSign={m.badSign} unit={m.unit} digits={m.digits} highlight={weak}
          colLabel={(c) => axis === 'weeks'
            ? `week of ${fmtDate(hm.cols[c].ts, false)} · ${hm.cols[c].n} ride${hm.cols[c].n === 1 ? '' : 's'}`
            : `ride ${fmtDate(hm.cols[c].ts)}`}
          onPick={axis === 'rides' ? (c) => { const id = hm.cols[c].ride_id; if (id) onOpenRide(id); } : undefined} />
        <div className="flex flex-wrap justify-between gap-2 text-[0.65rem] text-slate-500 tnum">
          <span>{fmtDate(hm.cols[0].ts, false)}</span>
          <DivergingLegend scale={scale} unit={m.unit} badSign={m.badSign} digits={m.digits} />
          <span>{fmtDate(hm.cols[hm.cols.length - 1].ts, false)}</span>
        </div>
        <p className="text-[0.65rem] text-slate-600">
          A failing module shows as one row turning red under load while staying neutral at zero current, and staying red week after week.
          {axis === 'rides' && ' Tap a column to open that ride.'}
        </p>
      </Panel>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        <Panel title="Weakest block history" code="UNDER LOAD" icon={ListOrdered}>
          <WeakestStrip s={s} />
        </Panel>
        <Panel title="Per block" code="LATEST · TREND / 30 D" icon={ListOrdered}>
          <div className="overflow-x-auto">
            <table className="w-full text-[0.7rem] tnum whitespace-nowrap">
              <thead className="text-slate-500 text-left">
                <tr><th className="font-normal pr-2">Blk</th><th className="font-normal text-right px-2">R mΩ</th>
                  <th className="font-normal text-right px-2">ECU mΩ</th><th className="font-normal text-right px-2">ΔV load mV</th>
                  <th className="font-normal text-right px-2">ΔV rest mV</th><th className="font-normal text-right px-2">ΔV / 30 d</th>
                  <th className="font-normal text-right pl-2">ΔR / 30 d</th></tr>
              </thead>
              <tbody>
                {b.latest.dev25_mv.map((_, k) => {
                  const bt = b.block_trends?.[k];
                  return (
                    <tr key={k} className={cx('border-t border-ink-700', weak === k + 1 && 'text-hud-amber')}>
                      <td className="pr-2 py-0.5">{k + 1}</td>
                      <td className="text-right px-2">{fmtNum(b.latest!.r_mohm?.[k], 1)}{b.latest!.r_se?.[k] != null && <span className="text-slate-600"> ±{fmtNum(b.latest!.r_se![k], 1)}</span>}</td>
                      <td className="text-right px-2 text-slate-500">{fmtNum(b.latest!.ecu_mohm?.[k], 0)}</td>
                      <td className="text-right px-2">{fmtNum(b.latest!.dev25_mv[k], 0)}</td>
                      <td className="text-right px-2">{fmtNum(b.latest!.dev0_mv[k], 0)}</td>
                      <td className="text-right px-2">{bt?.dev25.slope_30d != null ? `${fmtNum(bt.dev25.slope_30d, 1)}${bt.dev25.significant ? ' *' : ''}` : '--'}</td>
                      <td className="text-right pl-2">{bt?.dr_pct.slope_30d != null ? `${fmtNum(bt.dr_pct.slope_30d, 2)} %${bt.dr_pct.significant ? ' *' : ''}` : '--'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="text-[0.65rem] text-slate-600">
            R: own estimate from current steps between 2 s polls (± standard error). ECU: the battery ECU's figure (21D0, 1 mΩ steps).
            * = trend significant at 5 %. Highlighted: the block most often weakest in the last {b.persistent?.of ?? 0} rides.
          </p>
        </Panel>
      </div>
      {s.self_discharge && (
        <Panel title="Charge imbalance drift" icon={Grid3x3}
          code={`MV / DAY AGAINST THE MEDIAN · ${s.self_discharge.method === 'drift' ? `LAST 90 DAYS, ${s.self_discharge.n} RIDES` : `PROVISIONAL, ${s.self_discharge.n} RESTS`}`}>
          <BlockBars values={s.self_discharge.per_block} scale={niceScale([s.self_discharge.per_block], 0.5)} badSign={-1} unit="mV/d" digits={1}
            highlight={s.self_discharge.block} />
        </Panel>
      )}
    </>
  );
}

function WeakestStrip({ s }: { s: BatterySummary }) {
  const w = s.blocks.weakest;
  const share = s.blocks.weak_share;
  if (w.length === 0) return <Empty>No data.</Empty>;
  const data = w.map(([ts, id, block, dev]) => ({ ts, id, block, dev }));
  return (
    <>
      <ResponsiveContainer width="100%" height={210}>
        <ScatterChart margin={{ top: 6, right: 10, bottom: 2, left: -24 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={C.grid} />
          <XAxis dataKey="ts" type="number" domain={['dataMin - 1', 'dataMax + 1']} tick={TICK} stroke={C.axis} minTickGap={40}
            tickFormatter={(t: number) => fmtDate(t, false).slice(2)} />
          <YAxis dataKey="block" type="number" domain={[0.5, 14.5]} ticks={[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]}
            interval={0} tick={TICK} stroke={C.axis} width={44} />
          <Tooltip {...TIP} cursor={{ stroke: C.axis }}
            formatter={(v, name) => (name === 'block' ? [`${v}`, 'weakest block'] : [fmtDate(Number(v)), 'ride'])} />
          <Scatter data={data} fill={C.b} isAnimationActive={false} />
        </ScatterChart>
      </ResponsiveContainer>
      <p className="text-[0.65rem] text-slate-500">
        One dot per graded ride: the block with the lowest voltage at 50 A.
        {s.blocks.persistent && share && ` Last ${s.blocks.persistent.of} rides: block ${s.blocks.persistent.block} in ${s.blocks.persistent.count}. `}
        A healthy pack scatters over several blocks; one block taking every ride is the sign to watch.
      </p>
    </>
  );
}

// ── rides ────────────────────────────────────────────────────────────────────

const GRADE_TONE: Record<string, 'green' | 'cyan' | 'amber' | 'dim'> = { A: 'green', B: 'cyan', C: 'amber', X: 'dim', L: 'dim' };

function Rides({ s, rideId, setRideId }: { s: BatterySummary; rideId: number | null; setRideId: (id: number | null) => void }) {
  return (
    <>
      {rideId != null && <RideView id={rideId} onClose={() => setRideId(null)} />}
      <Panel title="Rides" code={`${s.rides.length} NEWEST`} icon={ListOrdered}>
        <div className="overflow-x-auto">
          <table className="w-full text-[0.7rem] tnum whitespace-nowrap">
            <thead className="text-slate-500 text-left">
              <tr>
                <th className="font-normal pr-3">Start</th><th className="font-normal px-2">Grade</th>
                <th className="font-normal text-right px-2">Time</th><th className="font-normal text-right px-2">km</th>
                <th className="font-normal text-right px-2">Batt °C</th><th className="font-normal text-right px-2">SOC %</th>
                <th className="font-normal text-right px-2">Ah</th><th className="font-normal text-right px-2">Weak blk</th>
                <th className="font-normal text-right px-2">ΔV load mV</th><th className="font-normal text-right px-2">Spread mV</th>
                <th className="font-normal text-right px-2">R 25 °C mΩ</th><th className="font-normal text-right pl-2">%/Ah</th>
              </tr>
            </thead>
            <tbody>
              {s.rides.map((r) => (
                <tr key={r.id} onClick={() => setRideId(r.id)}
                  className={cx('border-t border-ink-700 cursor-pointer hover:bg-white/[0.03]', rideId === r.id && 'bg-white/[0.04]')}>
                  <td className="pr-3 py-1 text-slate-200">{fmtDate(r.start_ts)}</td>
                  <td className="px-2"><Chip tone={GRADE_TONE[r.grade ?? 'X'] ?? 'dim'}>{r.grade ?? '?'}</Chip></td>
                  <td className="text-right px-2">{fmtDur(r.dur_s)}</td>
                  <td className="text-right px-2">{fmtNum(r.dist_km, 1)}</td>
                  <td className="text-right px-2">{fmtNum(r.t_mean, 1)}</td>
                  <td className="text-right px-2">{r.soc_min != null ? `${fmtNum(r.soc_min, 0)}–${fmtNum(r.soc_max, 0)}` : '--'}</td>
                  <td className="text-right px-2">{fmtNum(r.ah_thru, 2)}</td>
                  <td className="text-right px-2">{r.weak_block ?? '--'}</td>
                  <td className="text-right px-2">{fmtNum(r.weak_dev_mv, 0)}</td>
                  <td className="text-right px-2">{fmtNum(r.spread0_mv ?? r.spread_all_mv, 0)}</td>
                  <td className="text-right px-2">{fmtNum(r.r_sum25_mohm, 0)}</td>
                  <td className="text-right pl-2">{fmtNum(r.pct_per_ah, 1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="text-[0.65rem] text-slate-600">
          Grades: {Object.entries(s.quality.grade_meaning).map(([g, t]) => `${g} = ${t}`).join(' · ')}.
          Spread: at zero current from the per-ride fit; for 1-minute backfill rides the mean max − min over the ride.
        </p>
      </Panel>
    </>
  );
}

function RideView({ id, onClose }: { id: number; onClose: () => void }) {
  const { data: d, error } = useJson<RideDetail>(`/battery/rides/${id}`);
  if (!d) return <Panel title="Ride" icon={Activity}><Empty>{error ?? 'Loading…'}</Empty></Panel>;
  const f = d.features;
  const ser = d.series;
  const w = ser?.bucket_s ?? 1;
  const flags: string[] = f.quality?.flags ?? [];
  const pack = (ser?.pack ?? []).map((p, k) => ({ t: (k * w) / 60, i: p?.[0] ?? null, lo: p?.[1] ?? null, hi: p?.[2] ?? null, v: p?.[3] ?? null }));
  const slow = ser?.slow;
  const slowRows = (slow?.t ?? []).map((t, k) => ({
    t: (t as number) / 60, soc: slow!.soc2?.[k] ?? null,
    temp: slow!.tb1?.[k] ?? slow!.t1?.[k] ?? null, temp2: slow!.tb2?.[k] ?? slow!.t2?.[k] ?? null, intake: slow!.intake?.[k] ?? null,
  }));
  const devRows: (number | null)[][] = Array.from({ length: 14 }, (_, b) => (ser?.dev_mv ?? []).map((x) => (x ? x[b] : null)));
  const scale = niceScale(devRows, 50);
  const tMax = (pack.length * w) / 60;
  return (
    <Panel title={`Ride ${fmtDate(d.start_ts)}`} code={`${fmtDur(f.dur_s)} · ${d.source}`} icon={Activity}
      right={(
        <div className="flex items-center gap-2">
          <Chip tone={GRADE_TONE[f.quality?.grade ?? 'X'] ?? 'dim'}>grade {f.quality?.grade ?? '?'}</Chip>
          <button onClick={onClose} title="Close" className="p-1 text-slate-500 hover:text-slate-100"><X size={14} /></button>
        </div>
      )}>
      {flags.length > 0 && (
        <ul className="text-[0.7rem] text-hud-amber list-disc pl-4">
          {flags.map((fl) => <li key={fl}>{FLAG_TEXT[fl] ?? fl}</li>)}
        </ul>
      )}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-1">
        <DataRow label="Battery temp" value={`${fmtNum(f.temp?.t_min, 0)}–${fmtNum(f.temp?.t_max, 0)}`} unit="°C" />
        <DataRow label="SOC" value={`${fmtNum(f.soc?.min, 1)}–${fmtNum(f.soc?.max, 1)}`} unit="%" />
        <DataRow label="Discharged" value={fmtNum(f.stress?.ah_dis, 2)} unit="Ah" />
        <DataRow label="Charged" value={fmtNum(f.stress?.ah_chg, 2)} unit="Ah" />
        <DataRow label="Peak discharge" value={fmtNum(f.stress?.i_max, 0)} unit="A" />
        <DataRow label="Peak charge" value={fmtNum(f.stress?.i_min, 0)} unit="A" />
        <DataRow label="Current limit min" value={`${fmtNum(f.stress?.ccl_min, 0)} / ${fmtNum(f.stress?.cdl_min, 0)}`} unit="A chg/dis" />
        <DataRow label="Near limit" value={fmtNum(f.stress?.near_limit_s, 1)} unit="s" />
        <DataRow label="Block R median" value={fmtNum(f.r?.med_mohm, 1)} unit="mΩ" />
        <DataRow label="Sum of blocks" value={fmtNum(f.r?.sum_mohm, 0)} unit="mΩ" />
        <DataRow label="Pack R 0.25 s" value={fmtNum(f.r?.pack_fast_mohm, 0)} unit="mΩ" />
        <DataRow label="Current steps" value={f.r?.n_steps} />
        <DataRow label="SOC per Ah" value={fmtNum(f.soc?.pct_per_ah, 1)} unit="%/Ah" />
        <DataRow label="SOC jumps" value={f.soc?.jumps} />
        <DataRow label="12 V in READY" value={fmtNum(f.aux?.ready_v_obd, 2)} unit="V" />
        <DataRow label="12 V min at start" value={fmtNum(f.aux?.wake_min_v, 2)} unit="V" />
      </div>
      {!ser && <Empty>No raw record for this ride (1-minute backfill): only the figures above.</Empty>}
      {ser && (
        <>
          <Small title="Battery current" unit="A · + = discharge">
            <ComposedChart data={pack} margin={{ top: 4, right: 10, bottom: 0, left: -14 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={C.grid} />
              <XAxis dataKey="t" type="number" domain={[0, tMax]} tick={TICK} stroke={C.axis} tickFormatter={(t: number) => `${t.toFixed(0)}′`} />
              <YAxis tick={TICK} stroke={C.axis} width={48} />
              <Tooltip {...TIP} labelFormatter={(t) => `${Number(t).toFixed(1)} min`}
                formatter={(v, n) => [`${fmtNum(Number(v), 1)} A`, n === 'i' ? 'mean' : n === 'hi' ? 'max' : 'min']} />
              <ReferenceLine y={0} stroke={C.axis} />
              <Line dataKey="hi" stroke={C.raw} strokeWidth={1} dot={false} isAnimationActive={false} />
              <Line dataKey="lo" stroke={C.raw} strokeWidth={1} dot={false} isAnimationActive={false} />
              <Line dataKey="i" stroke={C.a} strokeWidth={2} dot={false} isAnimationActive={false} />
            </ComposedChart>
          </Small>
          <Legend items={[{ color: C.a, label: `mean per ${w.toFixed(0)} s` }, { color: C.raw, label: 'min / max' }]} />
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <Small title="State of charge" unit="%">
              <ComposedChart data={slowRows} margin={{ top: 4, right: 10, bottom: 0, left: -14 }}>
                <CartesianGrid strokeDasharray="3 3" stroke={C.grid} />
                <XAxis dataKey="t" type="number" domain={[0, tMax]} tick={TICK} stroke={C.axis} tickFormatter={(t: number) => `${t.toFixed(0)}′`} />
                <YAxis domain={['dataMin - 2', 'dataMax + 2']} tick={TICK} stroke={C.axis} width={48} />
                <Tooltip {...TIP} labelFormatter={(t) => `${Number(t).toFixed(1)} min`} formatter={(v) => [`${fmtNum(Number(v), 1)} %`, 'SOC']} />
                <Line dataKey="soc" type="stepAfter" stroke={C.a} strokeWidth={2} dot={false} isAnimationActive={false} />
              </ComposedChart>
            </Small>
            <div>
              <Small title="Temperatures" unit="°C">
                <ComposedChart data={slowRows} margin={{ top: 4, right: 10, bottom: 0, left: -14 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={C.grid} />
                  <XAxis dataKey="t" type="number" domain={[0, tMax]} tick={TICK} stroke={C.axis} tickFormatter={(t: number) => `${t.toFixed(0)}′`} />
                  <YAxis domain={['dataMin - 1', 'dataMax + 1']} tick={TICK} stroke={C.axis} width={48} />
                  <Tooltip {...TIP} labelFormatter={(t) => `${Number(t).toFixed(1)} min`}
                    formatter={(v, n) => [`${fmtNum(Number(v), 1)} °C`, n === 'temp' ? 'sensor 1' : n === 'temp2' ? 'sensor 2' : 'intake air']} />
                  <Line dataKey="temp" stroke={C.a} strokeWidth={2} dot={false} isAnimationActive={false} />
                  <Line dataKey="temp2" stroke={C.b} strokeWidth={2} dot={false} isAnimationActive={false} />
                  <Line dataKey="intake" stroke={C.c} strokeWidth={2} dot={false} isAnimationActive={false} connectNulls />
                </ComposedChart>
              </Small>
              <Legend items={[{ color: C.a, label: 'battery sensor 1' }, { color: C.b, label: 'battery sensor 2' }, { color: C.c, label: 'intake air' }]} />
            </div>
          </div>
          {f.n_blk > 0 && (
            <>
              <div className="text-[0.65rem] uppercase tracking-[0.18em] text-slate-500">Block voltage against the pack median over the ride · mV</div>
              <BlockHeatmap rows={devRows} cols={devRows[0].length} scale={scale} badSign={-1} unit="mV" highlight={d.view?.weak_block}
                colLabel={(c) => `${((c * w) / 60).toFixed(1)} min · ${fmtNum(ser.blk_i[c], 0)} A`} />
              <DivergingLegend scale={scale} unit="mV" badSign={-1} />
              {ser.scatter && (
                <div>
                  <Small title={`Blocks ${ser.scatter.weak} and ${ser.scatter.strong} against current`} unit="mV from the median" height={220}>
                    <ScatterChart margin={{ top: 4, right: 10, bottom: 14, left: -14 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke={C.grid} />
                      <XAxis dataKey="i" type="number" tick={TICK} stroke={C.axis}
                        label={{ value: 'battery current, A (+ = discharge)', position: 'insideBottom', offset: -8, fill: C.axis, fontSize: 10 }} />
                      <YAxis dataKey="mv" type="number" tick={TICK} stroke={C.axis} width={48} />
                      <Tooltip {...TIP} cursor={false} formatter={(v, n) => [`${v} ${n === 'i' ? 'A' : 'mV'}`, n === 'i' ? 'current' : 'deviation']} />
                      <ReferenceLine y={0} stroke={C.axis} />
                      <Scatter name="weak" data={ser.scatter.pts.map((p) => ({ i: p[0], mv: p[1] }))} fill={C.b} isAnimationActive={false} shape={<Dot2 />} />
                      <Scatter name="strong" data={ser.scatter.pts.map((p) => ({ i: p[0], mv: p[2] }))} fill={C.a} isAnimationActive={false} shape={<Dot2 />} />
                    </ScatterChart>
                  </Small>
                  <Legend items={[
                    { color: C.b, label: `block ${ser.scatter.weak} (lowest under load)`, dot: true },
                    { color: C.a, label: `block ${ser.scatter.strong} (highest under load)`, dot: true },
                  ]} />
                  <p className="text-[0.65rem] text-slate-600 mt-1">
                    The slope is the block's resistance above the median block; the height at 0 A is its charge offset.
                  </p>
                </div>
              )}
            </>
          )}
          <a href={`${API_URL}/battery/export/ride/${d.id}.csv`} className="text-[0.7rem] text-hud-cyan hover:underline w-fit inline-flex items-center gap-1.5">
            <Download size={12} /> Block series of this ride (CSV)
          </a>
        </>
      )}
    </Panel>
  );
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const Dot2 = (p: any) => (p.cx == null || p.cy == null ? null : <circle cx={p.cx} cy={p.cy} r={2} fill={p.fill} fillOpacity={0.75} />);

const Small = ({ title, unit, children, height = 170 }: { title: string; unit: string; children: React.ReactElement; height?: number }) => (
  <div>
    <div className="flex justify-between text-[0.65rem] uppercase tracking-[0.18em] text-slate-500 mb-1">
      <span>{title}</span><span className="normal-case tracking-normal">{unit}</span>
    </div>
    <ResponsiveContainer width="100%" height={height}>{children}</ResponsiveContainer>
  </div>
);

// ── 12 V ─────────────────────────────────────────────────────────────────────

function Aux({ s }: { s: BatterySummary }) {
  const [restId, setRestId] = useState<number | null>(null);
  const [key, setKey] = useState('aux_rest_v');
  const aux = s.indicators.filter((i) => i.key.startsWith('aux'));
  const TR: [string, string, string, number][] = [
    ['aux_rest_v', 'Rested voltage (6 h)', 'V', 2], ['aux_min_v', 'Lowest voltage per rest', 'V', 2],
    ['aux_ready_v', 'Charge voltage in READY', 'V', 2], ['aux_drain_ma', 'Parked drain', 'mA', 0],
    ['aux_wake_sag', 'Sag at wake', 'V', 2],
  ];
  const cur = TR.find((t) => t[0] === key)!;
  const tr = s.trends[key];
  const rest = aux.find((i) => i.key === 'aux_rest');
  const off = s.aux.pb_offset ?? 0;
  return (
    <>
      <div className="grid grid-cols-2 lg:grid-cols-3 gap-3">
        {aux.map((i) => (
          <div key={i.key} className="hud-panel p-3 flex flex-col gap-1.5">
            <div className="flex items-start justify-between gap-2">
              <span className="text-[0.65rem] uppercase tracking-[0.18em] text-slate-500">{i.title}</span>
              <LevelChip level={i.level} />
            </div>
            <span className="text-2xl tnum text-slate-50">{fmtNum(i.value, unitDigits(i.unit))}<span className="text-xs text-slate-500 ml-1">{i.unit}</span></span>
            <span className="text-[0.65rem] text-slate-500 tnum">{trendText(i.trend ? s.trends[i.trend] : undefined, i.unit)}</span>
          </div>
        ))}
      </div>
      <Panel title={cur[1]} code="TREND" icon={Activity}
        right={<span className="text-[0.65rem] text-slate-500 tnum hidden sm:block">{trendText(tr, cur[2])}</span>}>
        <div className="flex flex-wrap gap-1">
          {TR.map(([k, label]) => <Btn key={k} active={key === k} onClick={() => setKey(k)} className="!py-1">{label}</Btn>)}
        </div>
        <TrendChart series={tr?.series ?? []} unit={cur[2]} digits={cur[3]} normalised={!!tr?.coef}
          watch={key === 'aux_rest_v' && rest?.threshold ? rest.threshold.watch - off : key === 'aux_ready_v' ? s.thresholds.aux_charge.watch : null}
          act={key === 'aux_rest_v' && rest?.threshold ? rest.threshold.act - off : key === 'aux_ready_v' ? s.thresholds.aux_charge.act : null}
          change={tr?.change} />
        <p className="text-[0.65rem] text-slate-600">
          Voltages are read by the powerbox on the computer's feed while the computer draws its parked current: not an open-circuit voltage.
          {s.aux.pb_offset != null && ` The powerbox reads ${fmtNum(off, 2)} V below the ECM (OBD 0142) in READY; the limits are drawn shifted by that.`}
        </p>
      </Panel>
      {restId != null && <RestView id={restId} onClose={() => setRestId(null)} />}
      <Panel title="Rest periods" code={`${s.aux.rests.length} NEWEST`} icon={ListOrdered}>
        <div className="overflow-x-auto">
          <table className="w-full text-[0.7rem] tnum whitespace-nowrap">
            <thead className="text-slate-500 text-left">
              <tr>
                <th className="font-normal pr-3">Key-off</th><th className="font-normal text-right px-2">Length</th>
                <th className="font-normal text-right px-2">1 h</th><th className="font-normal text-right px-2">6 h</th>
                <th className="font-normal text-right px-2">12 h</th><th className="font-normal text-right px-2">24 h</th>
                <th className="font-normal text-right px-2">Min V</th><th className="font-normal text-right px-2">Below 11.8 V</th>
                <th className="font-normal text-right px-2">Drain mA</th><th className="font-normal text-right px-2">Drawn mAh</th>
                <th className="font-normal text-right pl-2">°C</th>
              </tr>
            </thead>
            <tbody>
              {s.aux.rests.map((r) => (
                <tr key={r.id} onClick={() => setRestId(r.id)}
                  className={cx('border-t border-ink-700 cursor-pointer hover:bg-white/[0.03]', restId === r.id && 'bg-white/[0.04]')}>
                  <td className="pr-3 py-1 text-slate-200">{fmtDate(r.start_ts)}{r.open && <span className="text-hud-cyan"> · now</span>}</td>
                  <td className="text-right px-2">{fmtHours(r.dur_h)}</td>
                  {['1', '6', '12', '24'].map((h) => <td key={h} className="text-right px-2">{fmtNum(r.marks?.[h], 2)}</td>)}
                  <td className={cx('text-right px-2', r.v_min != null && r.v_min < 11.5 ? 'text-hud-red' : r.v_min != null && r.v_min < 11.8 ? 'text-hud-amber' : '')}>{fmtNum(r.v_min, 2)}</td>
                  <td className="text-right px-2">{r.low_min ? `${r.low_min} min` : '–'}</td>
                  <td className="text-right px-2">{fmtNum(r.ma_mean, 0)}</td>
                  <td className="text-right px-2">{fmtNum(r.mah, 0)}</td>
                  <td className="text-right pl-2">{fmtNum(r.temp_mean, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </>
  );
}

function RestView({ id, onClose }: { id: number; onClose: () => void }) {
  const { data: d, error } = useJson<RestDetail>(`/battery/rests/${id}`);
  if (!d || !d.series) return <Panel title="Rest" icon={BatteryMedium}><Empty>{error ?? (d ? 'No series.' : 'Loading…')}</Empty></Panel>;
  const rows = d.series.t_min.map((t, k) => ({ h: t / 60, v: d.series!.v[k], vmin: d.series!.vmin[k], ma: d.series!.ma[k] }));
  return (
    <Panel title={`Rest from ${fmtDate(d.start_ts)}`} code={fmtHours(d.features.dur_h)} icon={BatteryMedium}
      right={<button onClick={onClose} title="Close" className="p-1 text-slate-500 hover:text-slate-100"><X size={14} /></button>}>
      <Small title="12 V at the powerbox" unit="V" height={200}>
        <ComposedChart data={rows} margin={{ top: 4, right: 10, bottom: 0, left: -14 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={C.grid} />
          <XAxis dataKey="h" type="number" domain={[0, 'dataMax']} tick={TICK} stroke={C.axis} tickFormatter={(t: number) => `${t.toFixed(0)} h`} />
          <YAxis domain={['dataMin - 0.05', 'dataMax + 0.05']} tick={TICK} stroke={C.axis} width={48} tickFormatter={(v: number) => v.toFixed(2)} />
          <Tooltip {...TIP} labelFormatter={(t) => `${Number(t).toFixed(1)} h after key-off`}
            formatter={(v, n) => [`${fmtNum(Number(v), 3)} V`, n === 'v' ? 'mean' : 'minimum']} />
          <Line dataKey="vmin" stroke={C.raw} strokeWidth={1} dot={false} isAnimationActive={false} />
          <Line dataKey="v" stroke={C.a} strokeWidth={2} dot={false} isAnimationActive={false} />
        </ComposedChart>
      </Small>
      <Legend items={[{ color: C.a, label: 'mean' }, { color: C.raw, label: 'minimum in the interval' }]} />
      <Small title="Computer current" unit="mA" height={130}>
        <ComposedChart data={rows} margin={{ top: 4, right: 10, bottom: 0, left: -14 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={C.grid} />
          <XAxis dataKey="h" type="number" domain={[0, 'dataMax']} tick={TICK} stroke={C.axis} tickFormatter={(t: number) => `${t.toFixed(0)} h`} />
          <YAxis tick={TICK} stroke={C.axis} width={48} />
          <Tooltip {...TIP} labelFormatter={(t) => `${Number(t).toFixed(1)} h after key-off`} formatter={(v) => [`${fmtNum(Number(v), 0)} mA`, 'current']} />
          <Line dataKey="ma" stroke={C.b} strokeWidth={2} dot={false} isAnimationActive={false} connectNulls />
        </ComposedChart>
      </Small>
    </Panel>
  );
}

// ── data quality, storage, export ────────────────────────────────────────────

function DataView({ s }: { s: BatterySummary }) {
  const q = s.quality;
  const st = s.stats;
  const kb = (x: unknown) => (typeof x === 'number' ? `${(x / 1024).toFixed(0)} kB` : '--');
  const hours = (st.ride_seconds as number) / 3600 || 0;
  return (
    <>
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
        <Panel title="Ride grades" icon={Database}>
          {Object.entries(q.grade_meaning).map(([g, text]) => (
            <div key={g} className="grid grid-cols-[2.2rem_2.5rem_1fr] gap-2 items-baseline text-xs">
              <Chip tone={GRADE_TONE[g] ?? 'dim'} className="justify-center">{g}</Chip>
              <span className="tnum text-slate-100 text-right">{q.grades[g] ?? 0}</span>
              <span className="text-slate-500 text-[0.7rem]">{text}</span>
            </div>
          ))}
        </Panel>
        <Panel title="What the rides contain" icon={Database}>
          <DataRow label="Rides" value={q.rides} />
          <DataRow label="With block voltages (21CE)" value={q.with_blocks} />
          <DataRow label="With broadcast current (0x03B)" value={q.with_pack_stream} />
          <DataRow label="With full 21CF answer" value={q.with_21cf_full} tone={q.with_21cf_full ? undefined : 'amber'} />
          <DataRow label="With ECU resistances (21D0)" value={q.with_21d0} />
          <DataRow label="Block poll rate" value={fmtNum(q.blk_rate_hz, 2)} unit="Hz" />
          <DataRow label="Rests" value={q.rests} />
          <DataRow label="12 V coverage of rests" value={q.rest_coverage_median != null ? fmtNum(q.rest_coverage_median * 100, 0) : null} unit="%" />
          <DataRow label="Last ride" value={fmtDate(q.last_ride_ts)} />
        </Panel>
        <Panel title="Storage" icon={Database}>
          <DataRow label="Database" value={kb(st.db_bytes)} />
          <DataRow label="Ride records" value={kb(st.ride_raw_bytes)} />
          <DataRow label="Ride features" value={kb(st.ride_feature_bytes)} />
          <DataRow label="Rest records" value={kb(st.rest_raw_bytes)} />
          <DataRow label="Per hour of driving" value={hours > 0.2 ? fmtNum(((st.ride_raw_bytes as number) + (st.ride_feature_bytes as number)) / 1024 / hours, 0) : null} unit="kB" />
          <DataRow label="Schema / analysis" value={`${st.schema} / ${st.analysis_ver}`} />
          <DataRow label="Summary build" value={fmtNum(s.live?.summary_s ?? s.compute_s, 2)} unit="s" />
          {s.live && <DataRow label="Collector" value={s.live.running ? (s.live.ride_open ? 'ride open' : 'waiting') : 'stopped'} tone={s.live.running ? 'green' : 'red'} />}
          {s.live?.last_error && <p className="text-[0.7rem] text-hud-red break-all">{s.live.last_error}</p>}
        </Panel>
      </div>
      {Object.keys(q.flags).length > 0 && (
        <Panel title="Flags" code="RIDES AFFECTED" icon={Database} tone="amber">
          {Object.entries(q.flags).sort((a, b) => b[1] - a[1]).map(([fl, n]) => (
            <div key={fl} className="grid grid-cols-[3rem_1fr] gap-3 text-xs items-baseline">
              <span className="tnum text-slate-100 text-right">{n}</span>
              <span className="text-slate-400">{FLAG_TEXT[fl] ?? fl}</span>
            </div>
          ))}
        </Panel>
      )}
      <Panel title="Export" icon={Download}>
        <div className="flex flex-wrap gap-2">
          {[['rides.csv', 'Rides (one row per ride)'], ['blocks.csv', 'Blocks (one row per ride and block)'],
            ['rests.csv', 'Rests (12 V)'], ['battery.db', 'Whole database (SQLite)']].map(([f, label]) => (
            <a key={f} href={`${API_URL}/battery/export/${f}`}
              className="border border-ink-500 px-3 py-2 text-[0.7rem] uppercase tracking-[0.15em] text-slate-300 hover:text-hud-cyan hover:border-hud-cyan/50 inline-flex items-center gap-2">
              <Download size={12} /> {label}
            </a>
          ))}
        </div>
        <p className="text-[0.65rem] text-slate-600">
          The database holds the raw per-ride records; on a laptop: python -m cyberpunk_computer.battery summary --db battery.db
          (docs/BATTERY_HEALTH.md).
        </p>
      </Panel>
      <Panel title="Limits" code="WATCH / ACT" icon={Database}>
        <div className="overflow-x-auto">
          <table className="w-full text-[0.7rem]">
            <tbody>
              {s.indicators.filter((i) => i.threshold).map((i) => (
                <tr key={i.key} className="border-t border-ink-700 align-top">
                  <td className="py-1 pr-3 text-slate-200 whitespace-nowrap">{i.title}</td>
                  <td className="py-1 pr-3 tnum whitespace-nowrap text-slate-300">{i.threshold!.watch} / {i.threshold!.act} {i.threshold!.unit}</td>
                  <td className="py-1 text-slate-500">{i.threshold!.origin}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </>
  );
}
