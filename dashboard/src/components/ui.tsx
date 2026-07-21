import type { ComponentType, ReactNode } from 'react';
import { cx } from '../lib/format';
import { TONE_BG, TONE_BORDER, TONE_TEXT, type Tone } from '../lib/tones';

export type { Tone };

type IconType = ComponentType<{ size?: number | string; className?: string }>;

export const Dot = ({ tone, pulse }: { tone: Tone; pulse?: boolean }) => (
  <span className={cx('h-1.5 w-1.5 shrink-0', TONE_BG[tone], pulse && 'pulse-dot')} />
);

/** Flat technical panel: hairline frame, uppercase header, optional ref code. */
export const Panel = ({ title, code, icon: Icon, tone = 'cyan', right, children, className }: {
  title: string;
  code?: string;
  icon: IconType;
  tone?: Tone;
  right?: ReactNode;
  children?: ReactNode;
  className?: string;
}) => (
  <div className={cx('hud-panel p-4 flex flex-col gap-3', className)}>
    <div className="flex items-center justify-between gap-2 border-b border-ink-700 pb-2">
      <div className={cx('flex items-center gap-2', TONE_TEXT[tone])}>
        <Icon size={14} />
        <h2 className="text-[0.7rem] font-medium uppercase tracking-[0.22em]">{title}</h2>
        {code && <span className="text-[0.6rem] text-slate-600 tracking-[0.15em]">{code}</span>}
      </div>
      {right}
    </div>
    {children}
  </div>
);

export const DataRow = ({ label, value, unit, tone }: {
  label: string;
  value?: string | number | null;
  unit?: string;
  tone?: Tone;
}) => (
  <div className="flex justify-between items-baseline gap-3">
    <span className="text-slate-500 text-xs uppercase tracking-wider">{label}</span>
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
      'border px-3 py-2 text-[0.7rem] uppercase tracking-[0.15em] transition-colors',
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
