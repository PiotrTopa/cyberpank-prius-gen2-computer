/**
 * Pure helpers of the camera preview panel (components/CameraPreviewPanel):
 * the "was previewing" marker that lets a reload / re-opened page / tab that
 * becomes visible again resume the preview inside the server's grace window,
 * the auto-resume rule, and the stale-frame overlay. No imports, so
 * `node --test tests/` can exercise them directly.
 */

export const MARKER_KEY = 'prius.cameraPreview';
/** A marker older than this is ignored (the server grace is 3 min by default). */
export const MARKER_MAX_AGE_MS = 15 * 60 * 1000;
/** A frame older than this is never presented as live. */
export const STALE_AFTER_S = 5;

export interface PreviewMarker {
  t: number;               // Date.now() of the last successful lease / renewal
  leaseId: string | null;  // last lease id (for "turn off now" during the grace)
}

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface ServerPreview {
  phase?: string;
  active?: boolean;
  grace?: boolean;
}

export function readMarker(store: StorageLike | null | undefined): PreviewMarker | null {
  try {
    const raw = store?.getItem(MARKER_KEY);
    if (!raw) return null;
    const m = JSON.parse(raw) as Partial<PreviewMarker>;
    if (typeof m.t !== 'number') return null;
    return { t: m.t, leaseId: typeof m.leaseId === 'string' ? m.leaseId : null };
  } catch {
    return null;
  }
}

export function writeMarker(store: StorageLike | null | undefined, m: PreviewMarker): void {
  try {
    store?.setItem(MARKER_KEY, JSON.stringify(m));
  } catch {
    /* private mode / storage blocked: auto-resume just does not happen */
  }
}

export function clearMarker(store: StorageLike | null | undefined): void {
  try {
    store?.removeItem(MARKER_KEY);
  } catch {
    /* ignore */
  }
}

/** Resume without a button press only when this browser was previewing
 *  recently AND the server still keeps the camera (grace) or previews. */
export function canAutoResume(marker: PreviewMarker | null, server: ServerPreview | null | undefined,
  nowMs: number): boolean {
  if (!marker || nowMs - marker.t > MARKER_MAX_AGE_MS || nowMs < marker.t - 60_000) return false;
  if (!server) return false;
  return server.grace === true || server.phase === 'grace' || server.active === true;
}

const OVERLAY_TEXT: Record<string, string> = {
  requesting: 'starting preview…',
  powering: 'powering camera…',
  enumerating: 'camera re-enumerating…',
  waiting_for_frame: 'waiting for camera…',
  error: 'camera error',
  live: 'frame stale',
};

/** How to present the last frame: greyed + text unless live and fresh. */
export function frameOverlay(phase: string, ageS: number | null): { stale: boolean; text: string | null } {
  const fresh = ageS != null && ageS <= STALE_AFTER_S;
  if (phase === 'live' && fresh) return { stale: false, text: null };
  const base = OVERLAY_TEXT[phase] ?? phase.replace(/_/g, ' ');
  const age = ageS != null ? ` — frame ${Math.round(ageS)} s old` : '';
  return { stale: true, text: base + age };
}

export function fmtMmSs(s: number): string {
  const v = Math.max(0, Math.round(s));
  return `${Math.floor(v / 60)}:${String(v % 60).padStart(2, '0')}`;
}
