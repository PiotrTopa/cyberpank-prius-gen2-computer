// Alert framework view helpers (backend: cyberpunk_computer/alerts, GET /api/v1/alerts).
// Pure functions only, so they are unit-tested in tests/alerts.test.ts.

export type AlertMode = 'off' | 'shadow' | 'on';
export const MODES: AlertMode[] = ['off', 'shadow', 'on'];

export interface AlertLogEntry {
  ts: number;
  seq?: number;
  event: 'fire' | 'show' | 'clear' | 'ack' | 'action' | 'note' | 'stat' | 'mode' | 'drive' | string;
  id?: string;
  kind?: string;
  mode?: AlertMode;
  drive?: number;
  text?: string;
  key?: string | null;
  severity?: string;
  evidence?: Record<string, unknown>;
  inputs?: Record<string, unknown>;
  action?: string;
  executed?: boolean;
  what?: string;
  why?: string;
  from?: string;
  to?: string;
  long?: boolean;
  data?: Record<string, unknown>;
}

export interface AlertType {
  id: string;
  title: string;
  text: string;
  function: string;
  kind: 'alert' | 'automation' | 'event';
  severity: string;
  priority: number;
  icon: string;
  sound: string | null;
  default_mode: AlertMode;
  advisory: boolean;
  description: string;
  needs: string[];
  mode: AlertMode;
  mode_setting: AlertMode;
  on_blocked: string | null;
  fires_total: number;
  fires_drive: number;
  fires_session: number;
  active: boolean;
  shown: boolean;
  muted_drive: boolean;
  last: AlertLogEntry | null;
}

export interface AlertsStatus {
  types: AlertType[];
  shown: { id: string; text: string; severity: string } | null;
  drive: number;
  functions: Record<string, Record<string, unknown>>;
  sound: { sink: string; available?: boolean; note?: string; requests?: number };
  notify: { sink: string; available?: boolean; note?: string; events?: number };
  scene?: { received: number; age_s: number | null };
  actions?: { executed: number };
  log?: { recent: number; written: number; dropped: number };
  errors?: number;
}

export type ToneName = 'cyan' | 'amber' | 'red' | 'green' | 'dim' | 'magenta';

export const MODE_TONE: Record<AlertMode, ToneName> = { off: 'dim', shadow: 'magenta', on: 'green' };

export function severityTone(sev?: string): ToneName {
  if (sev === 'warning') return 'red';
  if (sev === 'notice') return 'amber';
  return 'cyan';
}

export const KIND_LABEL: Record<string, string> = {
  alert: 'driver alert',
  automation: 'automation',
  event: 'parked-car event',
};

/** One line for a log entry (dashboard list). */
export function describeEntry(e: AlertLogEntry): string {
  switch (e.event) {
    case 'fire':
      return `${e.mode === 'on' ? 'fired' : 'would fire'}: ${e.text ?? e.id ?? ''}`;
    case 'action':
      return `${e.executed ? 'pressed' : 'would press'}: ${e.action ?? ''}`;
    case 'show':
      return `shown on the MFD${e.evidence ? '' : ''}`;
    case 'clear':
      return `cleared (${e.why ?? '?'})`;
    case 'ack':
      return e.long ? 'muted for the drive (long press)' : 'acknowledged';
    case 'note':
      return `note: ${e.what ?? ''}`;
    case 'mode':
      return `mode ${e.from ?? '?'} → ${e.to ?? '?'}`;
    case 'drive':
      return `drive ${e.drive ?? ''} started`;
    case 'stat':
      return `stat ${e.id ?? ''}`;
    default:
      return e.event;
  }
}

/** Compact "k=v" summary of the evidence object (first n keys). */
export function evidenceText(ev?: Record<string, unknown> | null, n = 6): string {
  if (!ev) return '';
  return Object.entries(ev)
    .slice(0, n)
    .map(([k, v]) => `${k}=${typeof v === 'number' ? (Number.isInteger(v) ? v : v.toFixed(2)) : typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
    .join(' · ');
}

/** Types grouped by kind, each group sorted by function then id. */
export function groupTypes(types: AlertType[]): [string, AlertType[]][] {
  const order = ['alert', 'automation', 'event'];
  const groups = new Map<string, AlertType[]>();
  for (const t of types) {
    const g = groups.get(t.kind) ?? [];
    g.push(t);
    groups.set(t.kind, g);
  }
  return order
    .filter((k) => groups.has(k))
    .map((k) => [k, groups.get(k)!.sort((a, b) => (a.function + a.id).localeCompare(b.function + b.id))]);
}

/** Can the dashboard offer this mode? ("on" is refused while a required input is missing.) */
export function modeAllowed(t: AlertType, m: AlertMode): boolean {
  return !(m === 'on' && t.on_blocked);
}
