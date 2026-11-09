import { useState } from 'react';
import { Bluetooth, Cable, KeyRound, Radar, SatelliteDish, Wifi } from 'lucide-react';
import type { DiscoveredSatellite, SatelliteNode, Satellites, SatTransportStatus } from '../types';
import { cx, fmtAge } from '../lib/format';
import { sendCommand } from '../lib/api';
import { TONE_TEXT } from '../lib/tones';
import {
  ID_RANGE, SLUG_RE, TRANSPORT_CODE, fmtLeft, hasRegistry, pairingLeft, rssiTone, shortFp, slugify,
  transportLabel, transportList, transportOf, transportStatus, transportTone,
} from '../lib/satellites';
import { Btn, Chip, DataRow, Dot, Panel, type Tone } from '../components/ui';
import { RailChip, WakeLocks } from '../components/SatellitesPanel';

const PAIR_SECONDS = 120;

const transportIcon = (t: string) => (t === 'ble' ? Bluetooth : t === 'wifi' ? Wifi : Cable);

/** Tiny labelled status dot (ON / LNK / AUTH). */
const Flag = ({ on, label, title }: { on?: boolean | null; label: string; title: string }) => (
  <span className={cx('flex items-center gap-1', on ? 'text-hud-green' : 'text-slate-600')} title={title}>
    <Dot tone={on ? 'green' : 'dim'} /> {label}
  </span>
);

const SmallBtn = (p: React.ComponentProps<typeof Btn>) => (
  <Btn {...p} className={cx('px-2 py-0.5 text-[0.6rem]', p.className)} />
);

// ── Transport cards ─────────────────────────────────────────────────────────

function TransportCard({ t, st, sats, rail, holders, now, canDiscover, busy, onDiscover, className }: {
  className?: string;
  t: string;
  st: SatTransportStatus;
  sats: Satellites;
  rail?: boolean | null;
  holders: string[];
  now: number;
  canDiscover: boolean;
  busy: boolean;
  onDiscover: () => void;
}) {
  const tone = transportTone(st);
  const q = st.queue_depth ?? 0;
  return (
    <Panel title={transportLabel(t)} code={TRANSPORT_CODE[t]} icon={transportIcon(t)} tone={tone === 'dim' ? 'cyan' : tone}
      className={className}
      right={t === 'rs485'
        ? <RailChip sats={sats} rail={rail} />
        : <Chip tone={tone}><Dot tone={tone} pulse={tone === 'green'} />{st.available ? 'UP' : st.available === false ? 'DOWN' : 'N/A'}</Chip>}>
      <DataRow label="State" value={st.state?.toUpperCase()} tone={tone === 'dim' ? undefined : tone} />
      {st.detail ? <DataRow label="Detail" value={st.detail} /> : null}
      <DataRow label="Job Queue" value={q} tone={q > 0 ? 'cyan' : undefined} />
      {st.active_job ? (
        <div className="flex justify-between items-center gap-2">
          <span className="text-slate-500 text-xs uppercase tracking-wider">Running</span>
          <span className="text-xs text-hud-cyan flex items-center gap-1.5 truncate">
            <Dot tone="cyan" pulse /> {st.active_job}
          </span>
        </div>
      ) : null}
      {st.last_discovery != null && (
        <DataRow label="Last Discovery" value={st.last_discovery ? `${fmtAge(st.last_discovery, now)} ago` : 'never'} />
      )}
      {t === 'rs485' && <WakeLocks holders={holders} />}
      {canDiscover && (
        <SmallBtn tone="cyan" disabled={busy} onClick={onDiscover} className="self-end"
          title={`satellite_discover {transport: ${t}}`}>
          {busy ? '…' : 'Discover'}
        </SmallBtn>
      )}
    </Panel>
  );
}

// ── Pairing window ──────────────────────────────────────────────────────────

function PairingPanel({ sats, now, busy, run }: {
  sats: Satellites;
  now: number;
  busy: string | null;
  run: (key: string, name: string, params?: Record<string, unknown>) => void;
}) {
  const left = pairingLeft(sats.pairing, now);
  const open = left != null;
  const scope = sats.pairing?.transport;
  return (
    <Panel title="Pairing Window" code="SAT-PAIR" icon={KeyRound} tone={open ? 'amber' : 'cyan'}
      right={<Chip tone={open ? 'amber' : 'dim'}><Dot tone={open ? 'amber' : 'dim'} pulse={open} />{open ? 'OPEN' : 'CLOSED'}</Chip>}>
      <div className="flex items-baseline justify-center gap-2 py-2">
        <span className={cx('text-3xl tnum', open ? 'text-hud-amber' : 'text-slate-600')}>
          {open ? fmtLeft(left) : '--'}
        </span>
        {open && <span className="text-[0.65rem] text-slate-500 uppercase tracking-wider">left · {scope ? transportLabel(scope) : 'all transports'}</span>}
      </div>
      <div className="flex gap-3">
        <Btn className="flex-1" tone="amber" active={open} disabled={busy === 'pair'}
          onClick={() => run('pair', 'satellite_pair_open', { seconds: PAIR_SECONDS })}>
          {open ? `Extend ${PAIR_SECONDS} s` : `Open ${PAIR_SECONDS} s`}
        </Btn>
        <Btn className="flex-1" tone="red" disabled={!open || busy === 'pair'}
          onClick={() => run('pair', 'satellite_pair_open', { seconds: 0 })}>
          Close
        </Btn>
      </div>
      <Btn tone="cyan" disabled={busy === 'disc:all'} onClick={() => run('disc:all', 'satellite_discover', {})}>
        <span className="flex items-center justify-center gap-2"><Radar size={12} /> Discover all</span>
      </Btn>
      <p className="text-[0.65rem] text-slate-600 text-center">
        While open, unknown BLE / Wi-Fi devices stay listed under Available with
        their offered key so they can be enrolled; closed, they are disconnected.
      </p>
    </Panel>
  );
}

// ── Available (discovered, not enrolled) ────────────────────────────────────

function EnrolForm({ d, onCancel, onSubmit, busy }: {
  d: DiscoveredSatellite;
  onCancel: () => void;
  onSubmit: (name: string, satId?: number) => void;
  busy: boolean;
}) {
  const range = ID_RANGE[d.transport];
  const defId = d.transport === 'rs485' && /^\d+$/.test(d.address) ? d.address : '';
  const [name, setName] = useState(() => slugify(d.name));
  const [id, setId] = useState(defId);
  const idNum = id.trim() === '' ? undefined : Number(id);
  const idOk = idNum === undefined
    ? d.transport !== 'rs485'
    : Number.isInteger(idNum) && (!range || (idNum >= range[0] && idNum <= range[1]));
  const nameOk = SLUG_RE.test(name);
  const input = 'bg-ink-950 border border-ink-500 focus:border-hud-cyan/60 outline-none px-2 py-1 text-xs text-slate-100 tnum';
  return (
    <div className="flex flex-wrap items-end gap-2 border-t border-ink-700 pt-2 mt-1">
      <label className="flex flex-col gap-1">
        <span className="text-[0.6rem] text-slate-500 uppercase tracking-wider">Name (slug)</span>
        <input className={cx(input, 'w-32', !nameOk && name && 'border-hud-red/60')} value={name}
          placeholder="garage" onChange={(e) => setName(e.target.value.toLowerCase())} />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-[0.6rem] text-slate-500 uppercase tracking-wider">
          Id {range ? `${range[0]}–${range[1]}` : ''}{d.transport !== 'rs485' ? ' · opt' : ''}
        </span>
        <input className={cx(input, 'w-20', !idOk && id && 'border-hud-red/60')} value={id}
          inputMode="numeric" placeholder="auto" onChange={(e) => setId(e.target.value.replace(/[^0-9]/g, ''))} />
      </label>
      <SmallBtn tone="green" active disabled={!nameOk || !idOk || busy} className="py-1"
        onClick={() => onSubmit(name, idNum)}>
        {busy ? '…' : 'Enrol'}
      </SmallBtn>
      <SmallBtn className="py-1" onClick={onCancel}>Cancel</SmallBtn>
      {!nameOk && name && <span className="text-[0.6rem] text-hud-red">a–z 0–9 _ -, max 24</span>}
    </div>
  );
}

function AvailablePanel({ list, pairingOpen, now, busy, run }: {
  list: DiscoveredSatellite[];
  pairingOpen: boolean;
  now: number;
  busy: string | null;
  run: (key: string, name: string, params?: Record<string, unknown>) => Promise<boolean>;
}) {
  const [formKey, setFormKey] = useState<string | null>(null);
  return (
    <Panel title="Available" code="SAT-NEW" icon={Radar} className="lg:col-span-2"
      right={<span className="text-[0.65rem] text-slate-600 uppercase tracking-wider">{list.length} seen</span>}>
      {list.length === 0 && (
        <span className="text-xs text-slate-600 py-2 text-center">no unenrolled devices seen — open pairing, then Discover</span>
      )}
      <div className="flex flex-col gap-1.5">
        {list.map((d) => {
          const key = `${d.transport}:${d.address}`;
          const needsKey = d.transport !== 'rs485';
          const blocked = needsKey && !pairingOpen ? 'pairing window closed'
            : needsKey && !d.pubkey ? 'device offered no public key' : null;
          return (
            <div key={key} className="bg-ink-850 border border-ink-700 px-3 py-1.5">
              <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
                <div className="flex items-center gap-2 min-w-0">
                  <Dot tone={d.linked ? 'cyan' : 'dim'} pulse={d.linked} />
                  <span className="text-[0.6rem] text-slate-500 uppercase w-10 shrink-0">{transportLabel(d.transport)}</span>
                  <span className="text-xs text-slate-100 truncate uppercase">{d.name || 'unnamed'}</span>
                  <span className="text-[0.6rem] text-slate-600 truncate normal-case">{d.address}</span>
                </div>
                <div className="flex items-center gap-2.5 shrink-0 text-[0.65rem]">
                  {d.rssi != null && <span className={TONE_TEXT[rssiTone(d.rssi)]}>{d.rssi} dBm</span>}
                  <Flag on={d.linked} label="LNK" title={d.linked ? 'link up' : 'not linked'} />
                  {needsKey && (
                    <span className={d.pubkey ? 'text-hud-green' : 'text-slate-600'}
                      title={d.pubkey ? `offered pubkey · fp ${d.fp ?? '?'}` : 'no public key offered'}>
                      {d.pubkey ? `KEY ${shortFp(d.fp) || '✓'}` : 'NO KEY'}
                    </span>
                  )}
                  <span className="text-slate-500">{fmtAge(d.last_seen, now)}</span>
                  <SmallBtn tone="green" disabled={!!blocked || formKey === key}
                    title={blocked ?? 'enrol into the keychain'} onClick={() => setFormKey(key)}>
                    Enrol
                  </SmallBtn>
                </div>
              </div>
              {blocked && formKey !== key && (
                <div className="text-[0.6rem] text-slate-600 text-right mt-0.5">{blocked}</div>
              )}
              {formKey === key && (
                <EnrolForm d={d} busy={busy === `enrol:${key}`} onCancel={() => setFormKey(null)}
                  onSubmit={async (name, satId) => {
                    const params: Record<string, unknown> = { transport: d.transport, address: d.address, name };
                    if (satId !== undefined) params.sat_id = satId;
                    if (await run(`enrol:${key}`, 'satellite_enrol', params)) setFormKey(null);
                  }} />
              )}
            </div>
          );
        })}
      </div>
    </Panel>
  );
}

// ── Enrolled ────────────────────────────────────────────────────────────────

type Pending = { id: number; action: 'forget' | 'revoke' } | null;

function EnrolledRow({ n, now, actions, pending, setPending, busy, run }: {
  n: SatelliteNode;
  now: number;
  actions: boolean;
  pending: Pending;
  setPending: (p: Pending) => void;
  busy: string | null;
  run: (key: string, name: string, params?: Record<string, unknown>) => Promise<boolean>;
}) {
  const t = transportOf(n);
  const wired = t === 'rs485';
  const confirming = pending?.id === n.device_id ? pending.action : null;
  const caps = Array.isArray(n.caps) ? n.caps : [];
  const restarts = (n.boot_id ?? 0) > 1 ? (n.boot_id ?? 1) - 1 : 0;
  const hasCfg = !!n.desired_config && Object.keys(n.desired_config).length > 0;
  const confirmTone: Tone = confirming === 'revoke' ? 'red' : 'amber';
  return (
    <div className="bg-ink-850 border border-ink-700 px-3 py-1.5 flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
      <div className="flex items-center gap-2 min-w-0">
        <Dot tone={n.online ? 'green' : 'dim'} pulse={n.online} />
        <span className="text-xs text-slate-100 truncate uppercase">{n.name || `dev-${n.device_id}`}</span>
        <span className="text-[0.6rem] text-slate-600">#{n.device_id}</span>
        {n.fp && <span className="text-[0.6rem] text-slate-500 normal-case" title={`fp ${n.fp}`}>{shortFp(n.fp)}</span>}
        {n.fw_version && <span className="text-[0.6rem] text-slate-600 normal-case">v{n.fw_version}</span>}
        {caps.map((c) => (
          <span key={c} className="text-[0.55rem] border border-ink-500 text-slate-400 px-1 uppercase tracking-wider">{c}</span>
        ))}
      </div>
      <div className="flex items-center gap-2.5 shrink-0 text-[0.65rem]">
        <Flag on={n.online} label="ON" title="online (recent traffic)" />
        {(!wired || n.linked != null) && <Flag on={n.linked} label="LNK" title="link up" />}
        {(!wired || n.authenticated != null) && <Flag on={n.authenticated} label="AUTH" title="handshake done on this link" />}
        {n.rssi != null && <span className={TONE_TEXT[rssiTone(n.rssi)]} title="RSSI">{n.rssi} dBm</span>}
        {hasCfg && (
          <span className={n.config_synced ? 'text-hud-green' : 'text-hud-amber'}
            title={n.config_synced ? 'config synced' : 'config push pending'}>
            {n.config_synced ? 'CFG✓' : 'CFG…'}
          </span>
        )}
        {restarts > 0 && <span className="text-slate-600" title="restarts observed">↻{restarts}</span>}
        <span className={n.online ? 'text-slate-300' : 'text-slate-600'}>
          {n.online ? fmtAge(n.last_seen, now) : n.last_seen ? `seen ${fmtAge(n.last_seen, now)}` : 'never'}
        </span>
        {actions && (confirming ? (
          <span className="flex items-center gap-1.5">
            <span className={cx('uppercase tracking-wider', TONE_TEXT[confirmTone])}>{confirming}?</span>
            <SmallBtn tone={confirmTone} active disabled={busy === `${confirming}:${n.device_id}`}
              onClick={async () => {
                await run(`${confirming}:${n.device_id}`, `satellite_${confirming}`, { sat_id: n.device_id });
                setPending(null);
              }}>
              Yes
            </SmallBtn>
            <SmallBtn onClick={() => setPending(null)}>No</SmallBtn>
          </span>
        ) : (
          <span className="flex items-center gap-1.5">
            <SmallBtn tone="amber" title="remove from keychain and disconnect"
              onClick={() => setPending({ id: n.device_id, action: 'forget' })}>Forget</SmallBtn>
            <SmallBtn tone="red" disabled={!n.fp}
              title={n.fp ? 'revoke the fingerprint (refused even with a valid cert) and disconnect' : 'no key to revoke (auth none)'}
              onClick={() => setPending({ id: n.device_id, action: 'revoke' })}>Revoke</SmallBtn>
          </span>
        ))}
      </div>
    </div>
  );
}

function EnrolledGroup({ t, nodes, now, actions, busy, run }: {
  t: string;
  nodes: SatelliteNode[];
  now: number;
  actions: boolean;
  busy: string | null;
  run: (key: string, name: string, params?: Record<string, unknown>) => Promise<boolean>;
}) {
  const [pending, setPending] = useState<Pending>(null);
  const online = nodes.filter((n) => n.online).length;
  return (
    <Panel title={`Enrolled · ${transportLabel(t)}`} code={TRANSPORT_CODE[t]} icon={transportIcon(t)} tone="green"
      className="lg:col-span-3"
      right={<span className="text-[0.65rem] text-slate-500 uppercase tracking-wider">{online}/{nodes.length} online</span>}>
      {nodes.length === 0
        ? <span className="text-xs text-slate-600 py-1 text-center">none enrolled</span>
        : (
          <div className="flex flex-col gap-1.5">
            {nodes.map((n) => (
              <EnrolledRow key={n.device_id} n={n} now={now} actions={actions}
                pending={pending} setPending={setPending} busy={busy} run={run} />
            ))}
          </div>
        )}
    </Panel>
  );
}

// ── Tab ─────────────────────────────────────────────────────────────────────

export function SatellitesTab({ sats, nodes, holders, rail, now }: {
  sats: Satellites;
  nodes: SatelliteNode[];
  holders: string[];
  rail?: boolean | null;
  now: number;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const run = async (key: string, name: string, params: Record<string, unknown> = {}) => {
    setBusy(key);
    const ok = await sendCommand(name, params);
    setBusy(null);
    return ok;
  };

  const multi = hasRegistry(sats);
  const transports = transportList(sats);
  const groups = [...transports, ...nodes.map(transportOf).filter((t) => !transports.includes(t))]
    .filter((t, i, a) => a.indexOf(t) === i);
  const discovered = Array.isArray(sats.discovered) ? sats.discovered.filter((d) => !d.trusted) : [];
  const showPairing = multi || sats.pairing != null;
  const pairingOpen = pairingLeft(sats.pairing, now) != null;

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      {transports.map((t) => (
        <TransportCard key={t} t={t} st={transportStatus(sats, t, rail)} sats={sats} rail={rail}
          holders={holders} now={now} canDiscover={multi}
          className={transports.length === 1 ? 'md:col-span-2 lg:col-span-3' : undefined} busy={busy === `disc:${t}`}
          onDiscover={() => run(`disc:${t}`, 'satellite_discover', { transport: t })} />
      ))}

      {showPairing && <PairingPanel sats={sats} now={now} busy={busy} run={run} />}
      {(showPairing || discovered.length > 0) && (
        <AvailablePanel list={discovered} pairingOpen={pairingOpen} now={now} busy={busy} run={run} />
      )}

      {groups.map((t) => (
        <EnrolledGroup key={t} t={t} nodes={nodes.filter((n) => transportOf(n) === t)} now={now}
          actions={multi} busy={busy} run={run} />
      ))}

      {!multi && (
        <p className="lg:col-span-3 md:col-span-2 text-[0.65rem] text-slate-600 text-center flex items-center justify-center gap-1.5">
          <SatelliteDish size={12} /> backend serves RS485 only — BLE / Wi-Fi, pairing and enrolment appear with the satellite registry
        </p>
      )}
    </div>
  );
}
