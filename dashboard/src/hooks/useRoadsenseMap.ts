import { useEffect, useRef, useState } from 'react';
import { API_URL } from '../lib/api';
import type { FeederStatus, FusedMsg, MapMsg } from '../lib/mapLayer';

/** roadsense /v1/health subset the MAP tab reads. */
export interface RsHealth {
  daemons?: Record<string, { connected?: boolean; last_msg_age_s?: number | null }>;
  map?: { state?: string; available?: boolean; reason?: string | null; pack?: string | null; cpu_pct?: number | null } | null;
}

export interface RoadsenseMapData {
  map: MapMsg | null;
  fused: FusedMsg | null;
  health: RsHealth | null;
  /** GET /api/v1/position (feeder status + raw_fix + pack_file); null when the feeder is off */
  position: FeederStatus | null;
  /** the roadsense front answered 502/503 or did not answer (stack stopped / starting) */
  apiDown: boolean;
  /** api up, but no map message (mapd not running) */
  mapdDown: boolean;
  /** viewer-clock ms of the last complete poll */
  updated: number | null;
  /** filtered positions of this browser session, oldest first (trail on the mini map) */
  trail: { lat: number; lon: number }[];
}

const EMPTY: RoadsenseMapData = {
  map: null, fused: null, health: null, position: null, apiDown: false, mapdDown: false, updated: null, trail: [],
};
const TRAIL_MAX = 180;
const TRAIL_MIN_STEP_M = 2;

async function getJson(path: string): Promise<{ status: number; body: unknown }> {
  try {
    const r = await fetch(`${API_URL}${path}`, { cache: 'no-store' });
    const body = await r.json().catch(() => null);
    return { status: r.status, body };
  } catch {
    return { status: 0, body: null };
  }
}

/**
 * Polls the roadsense map layer through the backend's roadsense front while
 * `enabled` (the MAP tab is open, the page visible and the stack is meant to
 * run): map + fused + health once a second, the feeder status every 2 s. One
 * request round at a time; nothing is fetched while disabled.
 */
export function useRoadsenseMap(enabled: boolean): RoadsenseMapData {
  const [data, setData] = useState<RoadsenseMapData>(EMPTY);
  const trail = useRef<{ lat: number; lon: number }[]>([]);

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    let timer = 0;
    let round = 0;
    const tick = async () => {
      if (document.visibilityState === 'hidden') {
        timer = window.setTimeout(tick, 1000);
        return;
      }
      const wantPos = round % 2 === 0;
      round += 1;
      const [m, f, h, p] = await Promise.all([
        getJson('/roadsense/map'), getJson('/roadsense/fused'), getJson('/roadsense/health'),
        wantPos ? getJson('/position') : Promise.resolve(null),
      ]);
      if (!alive) return;
      const apiDown = h.status === 0 || h.status === 502 || (h.status === 503 && f.status !== 200);
      const map = m.status === 200 ? (m.body as MapMsg) : null;
      const health = h.status === 200 ? (h.body as RsHealth) : null;
      const mapdConnected = health?.daemons?.mapd?.connected;
      const mapdDown = !apiDown && (map == null || mapdConnected === false);
      const pos = map?.position;
      if (pos && Number.isFinite(pos.lat) && Number.isFinite(pos.lon)) {
        const last = trail.current[trail.current.length - 1];
        const dlat = last ? (pos.lat - last.lat) * 111132 : Infinity;
        const dlon = last ? (pos.lon - last.lon) * 111320 * Math.cos((pos.lat * Math.PI) / 180) : Infinity;
        if (!last || Math.hypot(dlat, dlon) >= TRAIL_MIN_STEP_M) {
          trail.current = [...trail.current.slice(-(TRAIL_MAX - 1)), { lat: pos.lat, lon: pos.lon }];
        }
      }
      setData((prev) => ({
        map: mapdDown ? null : map,
        fused: f.status === 200 ? (f.body as FusedMsg) : null,
        health,
        position: p == null ? prev.position : p.status === 200 ? (p.body as FeederStatus) : null,
        apiDown,
        mapdDown,
        updated: Date.now(),
        trail: trail.current,
      }));
      timer = window.setTimeout(tick, 1000);
    };
    void tick();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [enabled]);

  return enabled ? data : EMPTY;
}

/** POST /api/v1/position/synthetic {"on"}; returns the reason on refusal. */
export async function setSynthetic(on: boolean): Promise<{ ok: boolean; reason?: string; status?: FeederStatus }> {
  try {
    const r = await fetch(`${API_URL}/position/synthetic`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = body?.detail;
      return { ok: false, reason: typeof d === 'string' ? d : d?.reason ?? `HTTP ${r.status}` };
    }
    return { ok: true, status: body as FeederStatus };
  } catch (e) {
    return { ok: false, reason: e instanceof Error ? e.message : String(e) };
  }
}
