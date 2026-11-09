import { ChevronRight, SatelliteDish } from 'lucide-react';
import type { SatelliteNode, Satellites, SatTransportStatus } from '../types';
import { cx, fmtAge } from '../lib/format';
import {
  fmtLeft, hasRegistry, pairingLeft, rssiTone, transportLabel, transportList, transportOf,
  transportStatus, transportTone,
} from '../lib/satellites';
import { TONE_TEXT } from '../lib/tones';
import { Chip, DataRow, Dot, Panel, type Tone } from './ui';

const holderTone = (h: string): Tone => (h === 'acc' ? 'green' : h === 'queue' ? 'cyan' : 'amber');

/** RS485 rail chip: LIVE/OFF plus the in-flight target when requested ≠ actual. */
export function RailChip({ sats, rail }: { sats: Satellites; rail?: boolean | null }) {
  const railOn = rail === true;
  const requested = sats.power_requested === true;
  // Rail vs desired mismatch = command in flight (or lost, being re-asserted).
  const pending = rail != null && requested !== railOn;
  return (
    <Chip tone={rail == null ? 'dim' : railOn ? 'green' : 'dim'}>
      <Dot tone={rail == null ? 'dim' : railOn ? 'green' : 'dim'} pulse={railOn} />
      RAIL {rail == null ? 'N/A' : railOn ? 'LIVE' : 'OFF'}
      {pending && <span className="text-hud-amber">→{requested ? 'ON' : 'OFF'}</span>}
    </Chip>
  );
}

/** Transport health chip: name + state (+ queue when busy). */
export function TransportChip({ t, st }: { t: string; st: SatTransportStatus }) {
  const tone = transportTone(st);
  const q = st.queue_depth ?? 0;
  return (
    <Chip tone={tone} className="normal-case">
      <Dot tone={tone} pulse={tone === 'green' && !!st.active_job} />
      <span className="uppercase">{transportLabel(t)}</span>
      <span className="text-slate-500">{st.state || (st.available === false ? 'n/a' : '--')}</span>
      {q > 0 && <span className="text-hud-cyan">q{q}</span>}
    </Chip>
  );
}

export function WakeLocks({ holders }: { holders: string[] }) {
  return (
    <div className="flex items-center justify-between py-1 gap-2">
      <span className="text-slate-500 text-xs uppercase tracking-wider">Wake-locks</span>
      <div className="flex flex-wrap gap-1.5 justify-end">
        {holders.length === 0
          ? <span className="text-xs text-slate-600">none</span>
          : holders.map((h) => <Chip key={h} tone={holderTone(h)}>{h}</Chip>)}
      </div>
    </div>
  );
}

/** Compact node row (Overview summary, Controls ping list). */
export function SatelliteNodeRow({ n, now, right, showTransport }: {
  n: SatelliteNode;
  now: number;
  right?: React.ReactNode;
  showTransport?: boolean;
}) {
  const t = transportOf(n);
  return (
    <div className="flex items-center justify-between gap-2 bg-ink-850 border border-ink-700 px-3 py-1.5">
      <div className="flex items-center gap-2 min-w-0">
        <Dot tone={n.online ? 'green' : 'dim'} pulse={n.online} />
        <span className="text-xs text-slate-100 truncate uppercase">{n.name || `dev-${n.device_id}`}</span>
        <span className="text-[0.6rem] text-slate-600">#{n.device_id}</span>
        {showTransport && <span className="text-[0.6rem] text-slate-500 uppercase">{transportLabel(t)}</span>}
      </div>
      <div className="flex items-center gap-2.5 shrink-0 text-[0.65rem]">
        {n.rssi != null && (
          <span className={TONE_TEXT[rssiTone(n.rssi)]} title="RSSI">{n.rssi} dBm</span>
        )}
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

/** Overview summary; the full view lives in the Satellites tab. */
export function SatellitesPanel({ sats, nodes, holders, rail, now, onOpen }: {
  sats: Satellites;
  nodes: SatelliteNode[];
  holders: string[];
  rail?: boolean | null;
  now: number;
  onOpen?: () => void;
}) {
  const multi = hasRegistry(sats);
  const transports = transportList(sats);
  const left = pairingLeft(sats.pairing, now);
  const avail = (sats.discovered ?? []).filter((d) => !d.trusted).length;

  return (
    <Panel
      title={multi ? 'Satellites' : 'RS485 Satellites'} code="SAT-02" icon={SatelliteDish} tone="green"
      right={<RailChip sats={sats} rail={rail} />}
    >
      {multi && (
        <div className="flex flex-wrap gap-1.5">
          {transports.map((t) => <TransportChip key={t} t={t} st={transportStatus(sats, t, rail)} />)}
        </div>
      )}
      <WakeLocks holders={holders} />

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
      {(left != null || avail > 0) && (
        <div className="flex flex-wrap gap-1.5">
          {left != null && <Chip tone="amber"><Dot tone="amber" pulse /> Pairing {fmtLeft(left)}</Chip>}
          {avail > 0 && <Chip tone="cyan">{avail} available</Chip>}
        </div>
      )}

      <div className={cx('mt-1 flex flex-col gap-1.5', nodes.length === 0 && 'items-center')}>
        {nodes.length === 0 && (
          <span className="text-xs text-slate-600 py-2">no satellites known</span>
        )}
        {nodes.map((n) => <SatelliteNodeRow key={n.device_id} n={n} now={now} showTransport={multi} />)}
      </div>

      {onOpen && (
        <button onClick={onOpen}
          className="self-end text-[0.65rem] text-slate-600 hover:text-hud-green flex items-center gap-1 uppercase tracking-wider">
          manage <ChevronRight size={12} />
        </button>
      )}
    </Panel>
  );
}
