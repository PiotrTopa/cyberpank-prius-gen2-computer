import { SatelliteDish } from 'lucide-react';
import type { SatelliteNode, Satellites } from '../types';
import { cx, fmtAge } from '../lib/format';
import { Chip, DataRow, Dot, Panel, type Tone } from './ui';

const holderTone = (h: string): Tone => (h === 'acc' ? 'green' : h === 'queue' ? 'cyan' : 'amber');

export function SatelliteNodeRow({ n, now, right }: {
  n: SatelliteNode;
  now: number;
  right?: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-2 bg-ink-850 border border-ink-700 px-3 py-1.5">
      <div className="flex items-center gap-2 min-w-0">
        <Dot tone={n.online ? 'green' : 'dim'} pulse={n.online} />
        <span className="text-xs text-slate-100 truncate uppercase">{n.name || `dev-${n.device_id}`}</span>
        <span className="text-[0.6rem] text-slate-600">#{n.device_id}</span>
      </div>
      <div className="flex items-center gap-2.5 shrink-0 text-[0.65rem]">
        {n.online && n.desired_config && Object.keys(n.desired_config).length > 0 && (
          <span className={n.config_synced ? 'text-hud-green' : 'text-hud-amber'}
            title={n.config_synced ? 'config synced' : 'config push pending'}>
            {n.config_synced ? 'CFG✓' : 'CFG…'}
          </span>
        )}
        {(n.boot_id ?? 0) > 1 && (
          <span className="text-slate-600" title="restarts observed">↻{(n.boot_id ?? 1) - 1}</span>
        )}
        <span className={n.online ? 'text-slate-300' : 'text-slate-600'}>
          {n.online ? fmtAge(n.last_seen, now) : n.last_seen ? `seen ${fmtAge(n.last_seen, now)}` : 'never'}
        </span>
        {right}
      </div>
    </div>
  );
}

export function SatellitesPanel({ sats, nodes, holders, out2, now }: {
  sats: Satellites;
  nodes: SatelliteNode[];
  holders: string[];
  out2?: boolean | null;
  now: number;
}) {
  const railOn = out2 === true;
  const requested = sats.power_requested === true;
  // Rail vs desired mismatch = command in flight (or lost, being re-asserted).
  const pending = out2 != null && requested !== railOn;

  return (
    <Panel
      title="RS485 Satellites" code="SAT-02" icon={SatelliteDish} tone="green"
      right={
        <Chip tone={out2 == null ? 'dim' : railOn ? 'green' : 'dim'}>
          <Dot tone={out2 == null ? 'dim' : railOn ? 'green' : 'dim'} pulse={railOn} />
          OUT2 {out2 == null ? 'N/A' : railOn ? 'LIVE' : 'OFF'}
          {pending && <span className="text-hud-amber">→{requested ? 'ON' : 'OFF'}</span>}
        </Chip>
      }
    >
      <div className="flex items-center justify-between py-1">
        <span className="text-slate-500 text-xs uppercase tracking-wider">Wake-locks</span>
        <div className="flex flex-wrap gap-1.5 justify-end">
          {holders.length === 0
            ? <span className="text-xs text-slate-600">none</span>
            : holders.map((h) => <Chip key={h} tone={holderTone(h)}>{h}</Chip>)}
        </div>
      </div>

      <DataRow label="Job Queue" value={sats.queue_depth ?? 0}
        tone={(sats.queue_depth ?? 0) > 0 ? 'cyan' : undefined} />
      {sats.active_job ? (
        <div className="flex justify-between items-center py-1">
          <span className="text-slate-500 text-xs uppercase tracking-wider">Running</span>
          <span className="text-xs text-hud-cyan flex items-center gap-1.5">
            <Dot tone="cyan" pulse /> {sats.active_job}
          </span>
        </div>
      ) : null}

      <div className={cx('mt-1 flex flex-col gap-1.5', nodes.length === 0 && 'items-center')}>
        {nodes.length === 0 && (
          <span className="text-xs text-slate-600 py-2">no satellites known</span>
        )}
        {nodes.map((n) => <SatelliteNodeRow key={n.device_id} n={n} now={now} />)}
      </div>
    </Panel>
  );
}
