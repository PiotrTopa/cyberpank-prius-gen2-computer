import { ChevronRight, Map as MapIcon } from 'lucide-react';
import type { AppState } from '../types';
import { STATE_TONE, reasonText } from '../lib/mapLayer';
import { Chip, DataRow, Panel } from './ui';

interface HealthMap { state?: string | null; available?: boolean | null; reason?: string | null; pack?: string | null; road?: string | null; limit_kmh?: number | null; limit_source?: string | null }
interface HealthFused {
  map_available?: boolean | null; position_state?: string | null; reason?: string | null; road?: string | null;
  speed_limit?: { limit_kmh?: number | null; source?: string | null } | null;
  lanes?: { count?: number | null; ego_index?: number | null } | null;
  tunnel?: { in_tunnel?: boolean | null; exit_in_m?: number | null } | null;
}

/** Overview card of the map layer, from the roadsense health mirror in the
 *  state stream (no polling); the MAP tab has the detail. */
export function MapSummaryPanel({ state, onOpen }: { state: AppState; onOpen?: () => void }) {
  const rs = state.roadsense;
  const h = (rs?.health ?? {}) as { map?: HealthMap | null; fused?: HealthFused | null };
  const running = rs?.state === 'running' || rs?.state === 'grace';
  const m = running ? h.map : null;
  const f = running ? h.fused : null;
  const feeder = rs?.position;
  const st = m?.state ?? f?.position_state ?? null;
  const visionOnly = f ? !f.map_available : null;
  return (
    <Panel title="Map Layer" code="MAP-00" icon={MapIcon}
      right={onOpen && (
        <button onClick={onOpen} className="text-[0.65rem] text-slate-600 hover:text-hud-amber flex items-center gap-1 uppercase tracking-wider">
          map <ChevronRight size={12} />
        </button>
      )}>
      {!running ? (
        <p className="text-xs text-slate-500">roadsense {(rs?.state ?? 'off').toUpperCase()}: map layer not running</p>
      ) : (
        <>
          <div className="flex flex-wrap gap-1.5">
            {st && <Chip tone={STATE_TONE[st] ?? 'dim'}>{st.replace(/_/g, ' ')}</Chip>}
            {visionOnly != null && <Chip tone={visionOnly ? 'amber' : 'green'}>{visionOnly ? 'vision only' : 'map + camera'}</Chip>}
            {(feeder?.synthetic || feeder?.session) && <Chip tone="magenta">synthetic</Chip>}
            {!m && <Chip tone="amber">mapd silent</Chip>}
          </div>
          {visionOnly && (f?.reason || m?.reason) && <DataRow label="Why" value={reasonText(f?.reason ?? m?.reason)} tone="amber" />}
          <DataRow label="Speed limit" value={f?.speed_limit?.limit_kmh != null ? `${f.speed_limit.limit_kmh}` : undefined}
            unit={f?.speed_limit?.source ? `km/h · ${f.speed_limit.source}` : 'km/h'} tone="cyan" />
          <DataRow label="Road" value={f?.road ?? m?.road ?? undefined} />
          <DataRow label="Lanes" value={f?.lanes?.count != null ? `${f.lanes.count}${f.lanes.ego_index != null ? ` · ego ${f.lanes.ego_index + 1}` : ''}` : undefined} />
          {f?.tunnel?.in_tunnel && <DataRow label="Tunnel exit" value={f.tunnel.exit_in_m != null ? Math.round(f.tunnel.exit_in_m) : '?'} unit="m" tone="violet" />}
          <DataRow label="Pack OSM date" value={m?.pack?.slice(0, 10)} />
          <DataRow label="Feeder" value={feeder ? `${feeder.source ?? '?'} · ${feeder.connected ? 'to mapd' : 'not connected'}` : 'off'} />
        </>
      )}
    </Panel>
  );
}
