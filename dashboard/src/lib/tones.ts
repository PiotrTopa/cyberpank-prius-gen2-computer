export type Tone = 'amber' | 'cyan' | 'green' | 'red' | 'violet' | 'magenta' | 'dim';

export const TONE_TEXT: Record<Tone, string> = {
  amber: 'text-hud-amber',
  cyan: 'text-hud-cyan',
  green: 'text-hud-green',
  red: 'text-hud-red',
  violet: 'text-hud-violet',
  magenta: 'text-hud-magenta',
  dim: 'text-slate-500',
};

export const TONE_BG: Record<Tone, string> = {
  amber: 'bg-hud-amber',
  cyan: 'bg-hud-cyan',
  green: 'bg-hud-green',
  red: 'bg-hud-red',
  violet: 'bg-hud-violet',
  magenta: 'bg-hud-magenta',
  dim: 'bg-slate-600',
};

export const TONE_BORDER: Record<Tone, string> = {
  amber: 'border-hud-amber/50',
  cyan: 'border-hud-cyan/50',
  green: 'border-hud-green/50',
  red: 'border-hud-red/50',
  violet: 'border-hud-violet/50',
  magenta: 'border-hud-magenta/50',
  dim: 'border-ink-500',
};
