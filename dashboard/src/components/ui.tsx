import type { ComponentType, ReactNode } from 'react';
import { cx } from '../lib/format';
import { TONE_BG, TONE_BORDER, TONE_TEXT, type Tone } from '../lib/tones';
import { useToasts } from '../lib/toast';

export type { Tone };

type IconType = ComponentType<{ size?: number | string; className?: string }>;

export const Dot = ({ tone, pulse }: { tone: Tone; pulse?: boolean }) => (
  <span className={cx('h-1.5 w-1.5 shrink-0', TONE_BG[tone], pulse && 'pulse-dot')} />
);

/** Flat technical panel: hairline frame, uppercase header. ``stale`` greys the
 *  body (data older than it should be) and says how old in the header. */
export const Panel = ({ title, icon: Icon, tone = 'cyan', right, children, className, stale }: {
  title: string;
  /** legacy reference code; no longer shown */
  code?: string;
  icon: IconType;
  tone?: Tone;
  right?: ReactNode;
  children?: ReactNode;
  className?: string;
  /** e.g. "stale 3m 10s"; greys the panel body when set */
  stale?: string | false | null;
}) => (
  <div className={cx('hud-panel p-4 flex flex-col gap-3', className)}>
    <div className="flex items-center justify-between gap-2 border-b border-ink-700 pb-2 min-h-7">
      <div className={cx('flex items-center gap-2 min-w-0', TONE_TEXT[tone])}>
        <Icon size={15} />
        <h2 className="text-[0.75rem] font-medium uppercase tracking-[0.18em] truncate">{title}</h2>
      </div>
      {stale ? <span className="text-[0.65rem] text-hud-amber tracking-wider uppercase">{stale}</span> : right}
    </div>
    <div className={cx('flex flex-col gap-3', stale && 'opacity-45')}>{children}</div>
  </div>
);

export const DataRow = ({ label, value, unit, tone }: {
  label: string;
  value?: string | number | null;
  unit?: string;
  tone?: Tone;
}) => (
  <div className="flex justify-between items-baseline gap-3">
    <span className="text-slate-500 text-[0.78rem] uppercase tracking-wider">{label}</span>
    <div className="flex items-baseline gap-1">
      <span className={cx('text-base tnum', tone ? TONE_TEXT[tone] : 'text-slate-100')}>
        {value !== undefined && value !== null && value !== '' ? value : '--'}
      </span>
      {unit && <span className="text-slate-600 text-[0.65rem]">{unit}</span>}
    </div>
  </div>
);

export const StatusRow = ({ label, on, onText = 'ON', offText = 'OFF', unknownText = 'N/A' }: {
  label: string;
  on?: boolean | null;
  onText?: string;
  offText?: string;
  unknownText?: string;
}) => {
  const known = on !== undefined && on !== null;
  const tone: Tone = !known ? 'dim' : on ? 'green' : 'dim';
  return (
    <div className="flex justify-between items-center gap-3">
      <span className="text-slate-500 text-xs uppercase tracking-wider">{label}</span>
      <div className="flex items-center gap-2">
        <Dot tone={tone} pulse={!!on} />
        <span className={cx('text-xs uppercase tracking-wider', !known ? 'text-slate-600' : on ? 'text-hud-green' : 'text-slate-400')}>
          {!known ? unknownText : on ? onText : offText}
        </span>
      </div>
    </div>
  );
};

/** Square bordered status chip. */
export const Chip = ({ tone = 'dim', children, className }: { tone?: Tone; children: ReactNode; className?: string }) => (
  <span className={cx(
    'inline-flex items-center gap-1.5 border px-2 py-0.5 text-[0.65rem] uppercase tracking-wider',
    TONE_TEXT[tone], TONE_BORDER[tone], className,
  )}>
    {children}
  </span>
);

/** Hero metric tile: large tabular readout + one status line. */
export const StatCard = ({ icon: Icon, label, value, unit, sub, tone = 'cyan', state = 'ok' }: {
  icon: IconType;
  label: string;
  value?: string | number | null;
  unit?: string;
  sub?: string;
  tone?: Tone;
  state?: 'ok' | 'warn' | 'danger' | 'idle';
}) => {
  const stTone: Tone = state === 'danger' ? 'red' : state === 'warn' ? 'amber' : state === 'idle' ? 'dim' : 'green';
  return (
    <div className="hud-panel p-4 flex flex-col gap-2.5">
      <div className="flex items-center justify-between">
        <span className="text-[0.65rem] uppercase tracking-[0.22em] text-slate-500">{label}</span>
        <Icon size={14} className={TONE_TEXT[tone]} />
      </div>
      <div className="flex items-baseline gap-1.5">
        <span className="text-3xl tnum text-slate-50">
          {value !== undefined && value !== null && value !== '' ? value : '--'}
        </span>
        {unit && <span className="text-slate-600 text-xs">{unit}</span>}
      </div>
      <div className="flex items-center gap-1.5 border-t border-ink-700 pt-2">
        <Dot tone={stTone} pulse={state === 'ok'} />
        <span className={cx('text-[0.65rem] uppercase tracking-wider', TONE_TEXT[stTone])}>{sub}</span>
      </div>
    </div>
  );
};

/** Horizontal meter: thin flat bar, no glow. */
export const Meter = ({ label, value, unit, pct, tone = 'cyan' }: {
  label: string;
  value?: string | number | null;
  unit?: string;
  pct?: number;
  tone?: Tone;
}) => (
  <div className="flex flex-col gap-1.5">
    <div className="flex justify-between items-baseline">
      <span className="text-slate-500 text-xs uppercase tracking-wider">{label}</span>
      <span className="text-xs tnum text-slate-100">
        {value ?? '--'}<span className="text-slate-600 text-[0.65rem] ml-0.5">{unit}</span>
      </span>
    </div>
    <div className="h-1 bg-ink-700 overflow-hidden">
      <div
        className={cx('h-full transition-[width] duration-500', TONE_BG[tone])}
        style={{ width: `${Math.max(0, Math.min(100, pct ?? 0))}%` }}
      />
    </div>
  </div>
);

/** Command feedback stack (bottom-right); fed by lib/toast. */
export const Toasts = () => {
  const list = useToasts();
  if (list.length === 0) return null;
  return (
    <div className="fixed bottom-20 md:bottom-4 right-4 left-4 md:left-auto z-[60] flex flex-col gap-2 items-end">
      {list.map((t) => (
        <div
          key={t.id}
          className={cx(
            'bg-ink-900 border px-3 py-2 text-[0.7rem] uppercase tracking-wider flex items-center gap-2',
            TONE_TEXT[t.tone], TONE_BORDER[t.tone],
          )}
        >
          <Dot tone={t.tone} /> {t.text}
        </div>
      ))}
    </div>
  );
};

/** Bordered mono action button. */
export const Btn = ({ tone = 'cyan', active, disabled, onClick, children, className, title }: {
  tone?: Tone;
  active?: boolean;
  disabled?: boolean;
  onClick?: () => void;
  children: ReactNode;
  className?: string;
  title?: string;
}) => (
  <button
    onClick={onClick}
    disabled={disabled}
    title={title}
    className={cx(
      'border px-3 py-2 text-[0.72rem] uppercase tracking-[0.12em] transition-colors min-h-10 md:min-h-0 select-none',
      disabled
        ? 'border-ink-700 text-slate-600 cursor-not-allowed'
        : active
          ? cx(TONE_BORDER[tone], TONE_TEXT[tone], 'bg-white/[0.04]')
          : 'border-ink-500 text-slate-400 hover:text-slate-100 hover:border-slate-400',
      className,
    )}
  >
    {children}
  </button>
);

/** One-of-N control (fan Auto/On/Off, poco2 mode...). Big touch targets. */
export function Segmented<T extends string>({ value, options, onChange, disabled, tone = 'cyan' }: {
  value: T | null | undefined;
  options: [T, string][];
  onChange: (v: T) => void;
  disabled?: boolean;
  tone?: Tone;
}) {
  return (
    <div className="flex">
      {options.map(([id, label], i) => (
        <button key={id} disabled={disabled} onClick={() => id !== value && onChange(id)}
          className={cx(
            'flex-1 border px-2 py-1.5 min-h-10 md:min-h-8 text-[0.7rem] uppercase tracking-[0.12em] transition-colors select-none',
            i > 0 && '-ml-px',
            value === id ? cx(TONE_BORDER[tone], TONE_TEXT[tone], 'bg-white/[0.05] relative z-10') : 'border-ink-500 text-slate-500 hover:text-slate-200',
            disabled && 'opacity-50 cursor-not-allowed',
          )}>
          {label}
        </button>
      ))}
    </div>
  );
}

/** Tiny inline SVG line (no chart library on the home view). */
export const Sparkline = ({ points, height = 36, tone = 'cyan', lo, hi }: {
  points: number[];
  height?: number;
  tone?: Tone;
  lo?: number;
  hi?: number;
}) => {
  if (points.length < 2) return <div className="text-[0.65rem] text-slate-600" style={{ height }}>no history yet</div>;
  const min = lo ?? Math.min(...points);
  const max = hi ?? Math.max(...points);
  const span = max - min || 1;
  const w = 100;
  const d = points.map((v, i) => {
    const x = (i / (points.length - 1)) * w;
    const y = height - 2 - ((v - min) / span) * (height - 4);
    return `${i ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(' ');
  return (
    <svg viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none" className={cx('w-full', TONE_TEXT[tone])} style={{ height }}>
      <path d={d} fill="none" stroke="currentColor" strokeWidth={1.4} vectorEffect="non-scaling-stroke" />
    </svg>
  );
};
