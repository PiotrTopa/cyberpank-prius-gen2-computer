import { useState } from 'react';
import {
  CircleAlert, Crosshair, Layers, ListOrdered, Navigation, Radio, Scale,
} from 'lucide-react';
import type { AppState } from '../types';
import { cx, fmtAge } from '../lib/format';
import { TONE_BORDER, TONE_TEXT } from '../lib/tones';
import {
  STATE_TONE, aheadItems, distM, fmtBytes, fmtM, fusedRows, holdRelease, mapBanners, reasonText,
  type Agreement, type FeederStatus, type MapMsg,
} from '../lib/mapLayer';
import { setSynthetic, useRoadsenseMap } from '../hooks/useRoadsenseMap';
import { toast } from '../lib/toast';
import { Btn, Chip, DataRow, Dot, Panel, StatusRow, type Tone } from '../components/ui';
import { MiniMap } from '../components/MiniMap';

/**
 * MAP tab: roadsense's map layer (mapd) and its fusion with the camera, for
 * seeing and debugging it from the phone. Data: the roadsense front
 * (`/api/v1/roadsense/map`, `/fused`, `/health`, polled once a second while
 * this tab is open and the stack is meant to run) and the backend's position
 * feeder (`state.roadsense.position`, `GET /api/v1/position`).
 */

const RUNNING = ['running', 'grace', 'starting'];
const n = (v?: number | null, d = 0) => (v == null || !Number.isFinite(v) ? undefined : v.toFixed(d));
const pct = (v?: number | null) => (v == null ? undefined : `${Math.round(v * 100)}`);

const AGREE: Record<Agreement, { t: string; tone: Tone }> = {
  agree: { t: 'agree', tone: 'green' },
  conflict: { t: 'conflict', tone: 'red' },
  vision_only: { t: 'vision only', tone: 'dim' },
  map_only: { t: 'map only', tone: 'cyan' },
  none: { t: '--', tone: 'dim' },
};

export function MapTab({ state, now }: { state: AppState; now: number }) {
  const rs = state.roadsense;
  const rsState = rs?.state ?? '';
  const meantToRun = rs == null || RUNNING.includes(rsState);
  const data = useRoadsenseMap(meantToRun);
  const feeder: FeederStatus | null = data.position ?? rs?.position ?? null;
  const mapdUnit = rs?.units?.['roadsense-mapd.service'];
  const banners = mapBanners({
    rsState: rs ? rsState : null, apiDown: data.apiDown, mapdDown: data.mapdDown,
    map: data.map, fused: data.fused, feeder, nowS: now / 1000,
  });
  const notRunning = rs != null && !meantToRun;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        {banners.map((b, i) => (
          <div key={i} className={cx('hud-panel px-4 py-2.5 flex flex-wrap items-baseline gap-x-3 gap-y-1 border', TONE_BORDER[b.tone])}>
            <span className={cx('text-[0.7rem] uppercase tracking-[0.22em] flex items-center gap-2', TONE_TEXT[b.tone])}>
              <Dot tone={b.tone} pulse={b.tone === 'green'} /> {b.title}
            </span>
            {b.detail && <span className="text-xs text-slate-400">{b.detail}</span>}
          </div>
        ))}
      </div>

      {notRunning ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <FeederPanel feeder={feeder} state={state} now={now} />
          <Panel title="Map Layer" code="MAP-01" icon={Layers}>
            <DataRow label="roadsense" value={rsState.toUpperCase() || '--'} />
            <DataRow label="mapd unit" value={mapdUnit ?? '--'} />
            <p className="text-[0.65rem] text-slate-600">
              The map layer runs with the roadsense stack (key on). Nothing is polled while it is off.
            </p>
          </Panel>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-3 lg:grid-flow-dense gap-4">
          <Panel title="Position & Match" code="MAP-02" icon={Crosshair} className="lg:col-span-2"
            right={<span className="text-[0.65rem] text-slate-600">{data.updated ? 'live 1 Hz' : 'waiting'}</span>}>
            <MiniMap map={data.map} feeder={feeder} trail={data.trail} />
            <PositionReadout map={data.map} feeder={feeder} />
          </Panel>

          <MapLayerPanel map={data.map} health={data.health} feeder={feeder} mapdUnit={mapdUnit}
            mapdDown={data.mapdDown} apiDown={data.apiDown} />

          <FusedPanel data={data} />

          <AheadPanel map={data.map} />

          <FeederPanel feeder={feeder} state={state} now={now} />
        </div>
      )}
    </div>
  );
}

// ── position readout ────────────────────────────────────────────────────────

function PositionReadout({ map, feeder }: { map: MapMsg | null; feeder: FeederStatus | null }) {
  const p = map?.position;
  const m = map?.match;
  const raw = feeder?.raw_fix;
  const rawToFilt = raw && p ? distM(raw, p) : null;
  const filtToMatch = p && m?.lat != null && m.lon != null ? distM(p, { lat: m.lat, lon: m.lon }) : null;
  const road = map?.road;
  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-x-5 gap-y-1.5">
      <div className="flex flex-col gap-1.5">
        <h3 className="text-[0.6rem] uppercase tracking-[0.2em] text-hud-amber">Filtered</h3>
        <DataRow label="Source" value={p?.source?.replace(/_/g, ' ')} tone={p?.source === 'dead_reckoning' ? 'amber' : undefined} />
        <DataRow label="Accuracy" value={n(p?.hacc_m, 1)} unit="m" />
        <DataRow label="Heading" value={p?.heading_deg != null ? `${n(p.heading_deg)}°${p.heading_acc_deg != null ? ` ±${n(p.heading_acc_deg)}` : ''}` : undefined} />
        <DataRow label="Speed" value={p?.speed_mps != null ? n(p.speed_mps * 3.6) : undefined} unit="km/h" />
        <DataRow label="Fix age" value={n(p?.fix_age_s, 1)} unit="s" />
        <DataRow label="DR distance" value={n(p?.dr_distance_m)} unit="m" tone={(p?.dr_distance_m ?? 0) > 0 ? 'amber' : undefined} />
        <DataRow label="Odometer" value={p?.odometer_m != null ? (p.odometer_m / 1000).toFixed(2) : undefined} unit="km" />
        <DataRow label="Lat / lon" value={p ? `${p.lat.toFixed(5)} ${p.lon.toFixed(5)}` : undefined} />
      </div>
      <div className="flex flex-col gap-1.5">
        <h3 className="text-[0.6rem] uppercase tracking-[0.2em] text-hud-green">Matched</h3>
        <DataRow label="Road" value={road ? [road.ref, road.name].filter(Boolean).join(' ') || road.class : undefined} />
        <DataRow label="Class" value={road?.class?.replace(/_/g, ' ')} />
        <DataRow label="Confidence" value={pct(m?.confidence)} unit="%" tone={m?.confidence != null && m.confidence < 0.5 ? 'amber' : undefined} />
        <DataRow label="Off road by" value={n(m?.distance_m, 1)} unit="m" />
        <DataRow label="Heading diff" value={n(m?.heading_diff_deg, 1)} unit="°" />
        <DataRow label="Candidates" value={m?.candidates} />
        <StatusRow label="Ambiguous" on={m?.ambiguous ?? null} onText="YES" offText="NO" />
      </div>
      <div className="flex flex-col gap-1.5">
        <h3 className="text-[0.6rem] uppercase tracking-[0.2em] text-hud-magenta">Raw fix</h3>
        <DataRow label="Source" value={raw?.source} tone={raw?.source === 'synthetic' ? 'magenta' : undefined} />
        <DataRow label="Age" value={n(raw?.age_s, 1)} unit="s" />
        <DataRow label="Accuracy" value={n(raw?.hacc_m, 1)} unit="m" />
        <DataRow label="Raw → filtered" value={n(rawToFilt, 1)} unit="m" />
        <DataRow label="Filtered → matched" value={n(filtToMatch, 1)} unit="m" />
        {!raw && <p className="text-[0.6rem] text-slate-600">raw fix: none yet (no GNSS source, or the feeder is off)</p>}
      </div>
    </div>
  );
}

// ── map layer status ────────────────────────────────────────────────────────

function MapLayerPanel({ map, health, feeder, mapdUnit, mapdDown, apiDown }: {
  map: MapMsg | null;
  health: ReturnType<typeof useRoadsenseMap>['health'];
  feeder: FeederStatus | null;
  mapdUnit?: string;
  mapdDown: boolean;
  apiDown: boolean;
}) {
  const g = map?.input?.gnss;
  const mo = map?.input?.motion;
  const st = map?.state;
  const pack = map?.pack;
  const pf = feeder?.pack_file;
  return (
    <Panel title="Map Layer" code="MAP-01" icon={Layers}
      right={st ? <Chip tone={STATE_TONE[st] ?? 'dim'}>{st.replace(/_/g, ' ')}</Chip> : undefined}>
      <DataRow label="mapd unit" value={mapdUnit ?? '--'} tone={mapdUnit === 'active' ? 'green' : mapdUnit ? 'amber' : undefined} />
      <StatusRow label="mapd → api" on={apiDown ? null : health?.daemons?.mapd?.connected ?? !mapdDown}
        onText="CONNECTED" offText="DOWN" />
      <DataRow label="Pack" value={pack ? `${pack.region ?? '?'} · OSM ${pack.osm_timestamp?.slice(0, 10) ?? '?'}` : map ? 'NO PACK' : undefined}
        tone={map && !pack ? 'red' : undefined} />
      <DataRow label="Pack file" value={pf ? `${pf.file ?? '?'} · ${fmtBytes(pf.size_bytes)}` : undefined} />
      <DataRow label="Available" value={map ? (map.available ? 'YES' : 'NO') : undefined} tone={map?.available ? 'green' : 'amber'} />
      {map?.reason && <DataRow label="Reason" value={reasonText(map.reason)} tone="amber" />}
      <DataRow label="Match conf." value={pct(map?.match?.confidence)} unit="%" />
      <div className="border-t border-ink-700 pt-2 flex flex-col gap-1.5">
        <h3 className="text-[0.6rem] uppercase tracking-[0.2em] text-slate-500">mapd input</h3>
        <DataRow label="GNSS" value={g ? `${g.source ?? '?'} · ${g.fix ?? '?'}` : 'NONE YET'}
          tone={g?.source === 'synthetic' ? 'magenta' : g?.fix === 'none' ? 'amber' : undefined} />
        <DataRow label="Fix age" value={n(g?.age_s, 1)} unit="s" />
        <DataRow label="Fix acc. / sats" value={g ? `${n(g.hacc_m, 1) ?? '--'} m · ${g.sats ?? '--'}` : undefined} />
        <DataRow label="Used / ign / rej" value={g ? `${g.used ?? 0} / ${g.ignored ?? 0} / ${g.rejected ?? 0}` : undefined} />
        <DataRow label="Filter resets" value={g?.resets} tone={(g?.resets ?? 0) > 0 ? 'amber' : undefined} />
        <DataRow label="Motion" value={mo ? `${mo.source ?? '?'}${mo.tag ? ` · ${mo.tag}` : ''}` : undefined}
          tone={mo?.source === 'none' ? 'amber' : mo?.tag === 'synthetic' ? 'magenta' : undefined} />
        <DataRow label="Motion age" value={n(mo?.age_s, 2)} unit="s" />
        <DataRow label="Input errors" value={map?.input?.errors} tone={(map?.input?.errors ?? 0) > 0 ? 'amber' : undefined} />
        {map?.input?.last_error && <p className="text-[0.6rem] text-hud-amber break-words">{map.input.last_error}</p>}
        <DataRow label="mapd CPU" value={n(map?.proc?.cpu_pct, 1)} unit="%" />
      </div>
    </Panel>
  );
}

// ── fused facts ─────────────────────────────────────────────────────────────

function FusedPanel({ data }: { data: ReturnType<typeof useRoadsenseMap> }) {
  const rows = fusedRows(data.fused);
  const hold = holdRelease(data.fused);
  const fm = data.fused?.map;
  return (
    <Panel title="Fused Facts" code="FUS-01" icon={Scale} className="lg:col-span-2"
      right={fm ? <Chip tone={fm.available ? 'green' : 'amber'}>{fm.available ? 'map used' : 'vision only'}</Chip> : undefined}>
      {rows.length === 0 ? (
        <p className="text-xs text-slate-600">no fused message (roadsense api without the fusion topic, or not answering)</p>
      ) : (
        <div className="flex flex-col">
          <div className="hidden sm:grid grid-cols-[6rem_1fr_7rem_1fr_1fr_5.5rem] gap-2 text-[0.6rem] uppercase tracking-[0.18em] text-slate-600 pb-1.5 border-b border-ink-700">
            <span>Fact</span><span>Fused</span><span>Source</span><span>Vision</span><span>Map</span><span>Agreement</span>
          </div>
          {rows.map((r) => {
            const a = AGREE[r.agree] ?? AGREE.none;
            return (
              <div key={r.fact} className={cx('grid grid-cols-2 sm:grid-cols-[6rem_1fr_7rem_1fr_1fr_5.5rem] gap-x-2 gap-y-0.5 py-2 border-b border-ink-700/60 last:border-0 text-xs',
                r.agree === 'conflict' && 'bg-hud-red/[0.06]')}>
                <span className="text-slate-500 uppercase tracking-wider">{r.fact}</span>
                <span className="text-slate-100 tnum text-right sm:text-left">{r.value}</span>
                <span className={cx('tnum', sourceTone(r.source))}><L>src</L>{r.source}</span>
                <span className={cx('sm:hidden text-right uppercase tracking-wider text-[0.65rem]', TONE_TEXT[a.tone])}>{a.t}</span>
                <span className="col-span-2 sm:col-span-1 text-slate-400 tnum"><L>vision</L>{r.vision}</span>
                <span className="col-span-2 sm:col-span-1 text-slate-400 tnum"><L>map</L>{r.map}</span>
                <span className={cx('hidden sm:inline uppercase tracking-wider text-[0.65rem]', TONE_TEXT[a.tone])}>{a.t}</span>
                {r.note && <span className="col-span-2 sm:col-span-6 text-[0.65rem] text-slate-500">{r.note}</span>}
              </div>
            );
          })}
        </div>
      )}
      {hold && (
        <div className="border border-ink-700 bg-ink-850 px-3 py-2 flex flex-col gap-1">
          <span className="text-[0.65rem] uppercase tracking-[0.18em] text-hud-red">
            sign {data.fused?.speed_limit?.sign ?? ''} {data.fused?.speed_limit?.limit_kmh ?? ''} · {hold.by}
          </span>
          {hold.lines.length > 0 && <span className="text-[0.6rem] uppercase tracking-wider text-slate-600">released by the first of</span>}
          {hold.lines.map((l) => <span key={l} className="text-xs text-slate-300">· {l}</span>)}
        </div>
      )}
    </Panel>
  );
}

/** mobile-only inline column label */
const L = ({ children }: { children: string }) => (
  <span className="sm:hidden text-slate-600 text-[0.6rem] uppercase mr-1.5">{children}</span>
);

function sourceTone(src: string): string {
  if (src.startsWith('sign')) return 'text-hud-red';
  if (src === 'map' || src.startsWith('map:') || src === 'map_tagged') return 'text-hud-cyan';
  if (src.startsWith('map') || src.startsWith('fused')) return 'text-hud-cyan/70';
  if (src === 'none' || src === 'unknown' || src === '--') return 'text-slate-600';
  return 'text-slate-300';
}

// ── road ahead ──────────────────────────────────────────────────────────────

const AHEAD_SHOWN = 12;

function AheadPanel({ map }: { map: MapMsg | null }) {
  const [all, setAll] = useState(false);
  const every = aheadItems(map?.ahead);
  const items = all ? every : every.slice(0, AHEAD_SHOWN);
  const a = map?.ahead;
  const road = map?.road;
  const flags = road ? [road.oneway && 'one-way', road.roundabout && 'roundabout', road.bridge && 'bridge',
    road.tunnel && 'tunnel', road.expressway && 'expressway', road.dual_carriageway && 'dual', road.link && 'link',
    road.built_up?.value != null && (road.built_up.value ? 'built-up' : 'outside built-up')].filter(Boolean) : [];
  return (
    <Panel title="Road Ahead" code="AHD-01" icon={ListOrdered}
      right={a ? <span className="text-[0.6rem] text-slate-600 tnum">{fmtM(a.horizon_m)} · path {pct(a.path_confidence) ?? '--'}%</span> : undefined}>
      {road && (
        <div className="flex flex-col gap-1 border-b border-ink-700 pb-2">
          <span className="text-sm text-slate-100">{[road.ref, road.name].filter(Boolean).join(' · ') || '(unnamed)'}</span>
          <span className="text-[0.65rem] text-slate-500 uppercase tracking-wider">
            {[road.class?.replace(/_/g, ' '), ...flags,
              road.lanes?.total != null ? `${road.lanes.total} lanes (${road.lanes.source ?? '?'})` : '',
              map?.speed_limit?.limit_kmh != null ? `map ${map.speed_limit.limit_kmh} km/h ${map.speed_limit.source ?? ''}` : '']
              .filter(Boolean).join(' · ')}
          </span>
        </div>
      )}
      {items.length === 0 ? (
        <p className="text-xs text-slate-600">{map?.available ? 'nothing within the horizon' : 'no look-ahead without a matched road'}</p>
      ) : (
        <ul className="flex flex-col">
          {items.map((it, i) => (
            <li key={i} className="flex items-baseline gap-2 py-1.5 border-b border-ink-700/60 last:border-0">
              <span className={cx('w-16 shrink-0 text-right tnum text-sm', TONE_TEXT[it.tone])}>{fmtM(it.dist_m)}</span>
              <span className="flex flex-col min-w-0">
                <span className="text-xs text-slate-100">{it.label}</span>
                {(it.detail || it.conf != null) && (
                  <span className="text-[0.65rem] text-slate-500 truncate">
                    {[it.detail, it.conf != null ? `conf ${Math.round(it.conf * 100)}%` : ''].filter(Boolean).join(' · ')}
                  </span>
                )}
              </span>
            </li>
          ))}
          {every.length > AHEAD_SHOWN && (
            <li className="pt-2">
              <button onClick={() => setAll((v) => !v)}
                className="text-[0.65rem] uppercase tracking-wider text-slate-500 hover:text-hud-cyan">
                {all ? 'show the nearest only' : `+${every.length - AHEAD_SHOWN} more within ${fmtM(every[every.length - 1].dist_m)}`}
              </button>
            </li>
          )}
        </ul>
      )}
    </Panel>
  );
}

// ── position feeder + synthetic test drive ──────────────────────────────────

function FeederPanel({ feeder, state, now }: { feeder: FeederStatus | null; state: AppState; now: number }) {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const veh = state.vehicle;
  const gear = veh?.gear?.replace('GearPosition.', '') ?? '';
  const moving = (veh?.speed_kmh ?? 0) > 1;
  const session = feeder?.session ?? null;
  const left = session?.until != null ? Math.max(0, session.until - now / 1000) : null;
  const canStart = !!feeder?.synthetic_control && !!feeder.active && !moving && !session;
  const why = !feeder ? 'feeder off' : !feeder.synthetic_control
    ? (feeder.base_source === 'synthetic' ? 'configured as the source (env)' : 'disabled (BACKEND_POSITION_SYNTHETIC_CONTROL=0)')
    : !feeder.active ? 'roadsense not running' : moving ? 'car moving' : '';

  const act = async (on: boolean) => {
    setBusy(true);
    setArmed(false);
    const r = await setSynthetic(on);
    setBusy(false);
    if (r.ok) toast(on ? 'synthetic drive started' : 'synthetic drive stopped', on ? 'magenta' : 'green', 3000);
    else toast(`synthetic ${on ? 'start' : 'stop'} refused: ${(r.reason ?? '').replace(/_/g, ' ')}`, 'red', 6000);
  };

  if (!feeder) {
    return (
      <Panel title="Position Feeder" code="POS-01" icon={Radio}>
        <p className="text-xs text-slate-500">
          Feeder off: the backend sends no position to mapd (<span className="text-slate-300">BACKEND_POSITION_FEEDER=1</span> turns it on).
        </p>
      </Panel>
    );
  }
  return (
    <Panel title="Position Feeder" code="POS-01" icon={Radio}
      right={<Chip tone={feeder.connected ? 'green' : feeder.active ? 'amber' : 'dim'}>
        {feeder.connected ? 'to mapd' : feeder.active ? 'mapd away' : 'idle'}</Chip>}>
      <DataRow label="Source" value={feeder.source} tone={feeder.synthetic ? 'magenta' : undefined} />
      {feeder.base_source && feeder.base_source !== feeder.source && <DataRow label="Configured" value={feeder.base_source} />}
      <StatusRow label="Active" on={feeder.active} onText="RUNNING" offText="IDLE (stack down)" />
      {feeder.suspended && <DataRow label="Suspended" value={feeder.suspended.replace(/_/g, ' ')} tone="amber" />}
      <DataRow label="Sent / dropped" value={`${feeder.sent ?? 0} / ${feeder.dropped ?? 0}`} tone={(feeder.dropped ?? 0) > 0 ? 'amber' : undefined} />
      <DataRow label="Motion / gnss / none" value={`${feeder.motion_sent ?? 0} / ${feeder.gnss_sent ?? 0} / ${feeder.none_sent ?? 0}`} />
      <DataRow label="CAN 0x0B4 age" value={n(feeder.motion?.can_age_s, 1)} unit="s" />
      <DataRow label="Yaw input" value={feeder.motion?.yaw_input ?? undefined} />
      <StatusRow label="GNSS fix" on={feeder.gnss?.has_fix ?? null} onText="YES" offText="NO" />
      <DataRow label="Last fix age" value={n(feeder.gnss?.last_fix_age_s as number | null | undefined, 1)} unit="s" />
      <DataRow label="mapd errors" value={feeder.errors} tone={(feeder.errors ?? 0) > 0 ? 'amber' : undefined} />
      {feeder.last_error && <p className="text-[0.6rem] text-hud-amber break-words">{feeder.last_error}</p>}

      <div className="border border-hud-magenta/40 bg-hud-magenta/[0.04] px-3 py-2.5 flex flex-col gap-2">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[0.65rem] uppercase tracking-[0.18em] text-hud-magenta flex items-center gap-1.5">
            <Navigation size={12} /> Synthetic test drive
          </span>
          <Chip tone={session || feeder.synthetic ? 'magenta' : 'dim'}>{session ? 'running' : feeder.synthetic ? 'env' : 'off'}</Chip>
        </div>
        <p className="text-[0.65rem] text-slate-500">
          Fake GNSS + motion over a 5.6 km Opole loop, tagged <span className="text-slate-300">synthetic</span>. Parked
          only: refused while the car moves or is not in P, ends when it moves, when roadsense stops, after the session
          time, or on STOP. The MFD ignores a synthetic map.
        </p>
        {session && (
          <DataRow label="Ends in" value={left != null ? `${Math.floor(left / 60)}:${String(Math.floor(left % 60)).padStart(2, '0')}` : undefined} tone="magenta" />
        )}
        {feeder.gnss && feeder.synthetic && (
          <DataRow label="Along route" value={`${feeder.gnss.along_m ?? '--'} / ${feeder.gnss.route_m ?? '--'} m`} />
        )}
        {feeder.last_session_end?.reason && !session && (
          <DataRow label="Last session" value={`${feeder.last_session_end.reason.replace(/_/g, ' ')} · ${fmtAge(feeder.last_session_end.at, now)} ago`} />
        )}
        <div className="flex gap-2 items-center flex-wrap">
          {session ? (
            <Btn tone="green" active disabled={busy} onClick={() => act(false)}>Stop synthetic</Btn>
          ) : armed ? (
            <>
              <Btn tone="magenta" active disabled={busy || !canStart} onClick={() => act(true)}>Confirm start</Btn>
              <Btn onClick={() => setArmed(false)}>Cancel</Btn>
            </>
          ) : (
            <Btn tone="magenta" disabled={!canStart || busy} onClick={() => setArmed(true)}
              title={why || 'start a synthetic drive (car parked)'}>Start synthetic</Btn>
          )}
          {!session && why && (
            <span className="text-[0.65rem] text-slate-600 flex items-center gap-1"><CircleAlert size={11} /> {why}</span>
          )}
          {!session && !why && gear && gear !== 'P' && (
            <span className="text-[0.65rem] text-slate-600">gear {gear}: the backend will refuse unless in P</span>
          )}
        </div>
      </div>
    </Panel>
  );
}

