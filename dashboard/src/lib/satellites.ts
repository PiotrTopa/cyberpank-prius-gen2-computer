// Helpers for the multi-transport satellite view (docs/SATELLITES.md §5/§6).
// Every field beyond `nodes` + the RS485 rail is optional: an RS485-only
// backend yields one transport (rs485) and no pairing / discovered lists.
import type { SatelliteNode, Satellites, SatPairing, SatTransportStatus } from '../types';
import type { Tone } from './tones';

export const KNOWN_TRANSPORTS = ['rs485', 'ble', 'wifi'] as const;

export const TRANSPORT_LABEL: Record<string, string> = { rs485: 'RS485', ble: 'BLE', wifi: 'Wi-Fi' };
export const TRANSPORT_CODE: Record<string, string> = { rs485: 'SAT-485', ble: 'SAT-BLE', wifi: 'SAT-WIF' };
export const ID_RANGE: Record<string, [number, number]> = { rs485: [100, 199], ble: [300, 399], wifi: [400, 499] };

export const transportLabel = (t: string) => TRANSPORT_LABEL[t] ?? t.toUpperCase();
export const transportOf = (n: SatelliteNode): string => n.transport || 'rs485';

/** True once the backend serves the multi-transport registry. */
export const hasRegistry = (s: Satellites) => s.transports != null && typeof s.transports === 'object';

/** Transports to show, known ones first. Old backend: RS485 only. */
export function transportList(s: Satellites): string[] {
  if (!hasRegistry(s)) return ['rs485'];
  const keys = Object.keys(s.transports ?? {});
  const known = KNOWN_TRANSPORTS.filter((t) => keys.includes(t));
  return [...known, ...keys.filter((k) => !(KNOWN_TRANSPORTS as readonly string[]).includes(k)).sort()];
}

/** Status for a transport; the RS485 one is synthesised from the old fields. */
export function transportStatus(s: Satellites, t: string, rail?: boolean | null): SatTransportStatus {
  const st = s.transports?.[t];
  if (st) return st;
  if (t === 'rs485') {
    return {
      available: rail == null ? undefined : true,
      state: rail == null ? undefined : rail ? 'rail-on' : 'rail-off',
      queue_depth: s.queue_depth,
      active_job: s.active_job,
    };
  }
  return {};
}

export function transportTone(st: SatTransportStatus): Tone {
  const state = (st.state ?? '').toLowerCase();
  if (/err|fail|fault/.test(state)) return 'red';
  if (st.available === false) return 'dim';
  if (st.available === true) return 'green';
  return 'dim';
}

/** Seconds left in the pairing window, null when closed. `nowMs` = backend clock. */
export function pairingLeft(p: SatPairing | undefined, nowMs: number): number | null {
  if (!p?.open) return null;
  if (!p.until) return Infinity;
  const left = Math.ceil(p.until - nowMs / 1000);
  return left > 0 ? left : null;
}

export const fmtLeft = (s: number) =>
  !Number.isFinite(s) ? 'open' : s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` : `${s}s`;

export const shortFp = (fp?: string | null) => (fp ? (fp.length > 8 ? `${fp.slice(0, 8)}…` : fp) : '');

export const rssiTone = (r?: number | null): Tone =>
  r == null ? 'dim' : r >= -65 ? 'green' : r >= -80 ? 'amber' : 'red';

export const SLUG_RE = /^[a-z0-9][a-z0-9_-]{0,23}$/;

export const slugify = (s?: string | null) =>
  (s ?? '').toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 24);
