import { useCallback, useEffect, useState } from 'react';
import { BellRing, Download, Gauge, ListChecks, ScrollText, Wrench } from 'lucide-react';
import { API_URL } from '../lib/api';
import { cx } from '../lib/format';
import { toast } from '../lib/toast';
import {
  KIND_LABEL, MODES, MODE_TONE, describeEntry, evidenceText, groupTypes, modeAllowed, severityTone,
  type AlertLogEntry, type AlertMode, type AlertType, type AlertsStatus,
} from '../lib/alerts';
import { Btn, Chip, DataRow, Dot, Panel } from '../components/ui';

/**
 * ALERTS tab: the driver-alert framework (backend cyberpunk_computer/alerts).
 * Every type with its mode (off / shadow / on, persisted on the backend; new
 * types arrive in shadow), fire counts and the last fire with the inputs that
 * fired it; the recent log; the shadow-log export; per-function statistics
 * (braking score, range, service intervals, parked-car events).
 * Data: GET /api/v1/alerts and /alerts/log, polled every 3 s while open.
 */

const fmtTime = (ts?: number | null) =>
  ts ? new Date(ts * 1000).toLocaleString([], { dateStyle: 'short', timeStyle: 'medium' }) : '--';

async function getJson<T>(path: string): Promise<T | null> {
  try {
    const r = await fetch(`${API_URL}${path}`, { cache: 'no-store' });
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}

async function postJson(path: string, body: unknown): Promise<{ ok: boolean; detail?: string }> {
  try {
    const r = await fetch(`${API_URL}${path}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (r.ok) return { ok: true };
    const err = await r.json().catch(() => ({}));
    const d = err?.detail;
    return { ok: false, detail: typeof d === 'string' ? d : d?.reason ?? r.statusText };
  } catch (e) {
    return { ok: false, detail: e instanceof Error ? e.message : String(e) };
  }
}

function TypeRow({ t, onMode }: { t: AlertType; onMode: (m: AlertMode) => void }) {
  const last = t.last;
  return (
    <div className="border-b border-ink-700 last:border-0 py-2.5 flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <Dot tone={MODE_TONE[t.mode]} pulse={t.shown} />
        <span className="text-slate-100 text-sm">{t.title}</span>
        <Chip tone={severityTone(t.severity)}>{t.severity}</Chip>
        {t.advisory && <Chip tone="dim">advisory</Chip>}
        {t.shown && <Chip tone="amber">on screen</Chip>}
        {t.muted_drive && <Chip tone="dim">muted this drive</Chip>}
        <div className="ml-auto flex gap-1">
          {MODES.map((m) => (
            <Btn key={m} tone={MODE_TONE[m] === 'dim' ? 'cyan' : MODE_TONE[m]} active={t.mode_setting === m}
              disabled={!modeAllowed(t, m)} onClick={() => onMode(m)}
              title={!modeAllowed(t, m) ? `refused: ${t.on_blocked}` : `set ${m}`}
              className="px-2 py-1">
              {m}
            </Btn>
          ))}
        </div>
      </div>
      <div className="text-xs text-slate-500 flex flex-wrap gap-x-4 gap-y-0.5">
        <span className="font-mono text-slate-400">{t.id}</span>
        <span>fires: <span className="text-slate-300 tnum">{t.fires_total}</span> total · <span className="text-slate-300 tnum">{t.fires_drive}</span> this drive</span>
        {t.sound && <span>sound: {t.sound}</span>}
        {t.mode !== t.mode_setting && <span className="text-hud-amber">effective: {t.mode}</span>}
      </div>
      {t.on_blocked && <div className="text-xs text-hud-amber">"on" refused: {t.on_blocked}</div>}
      {t.description && <div className="text-xs text-slate-500">{t.description}</div>}
      {last && (
        <div className="text-xs text-slate-400 border-l-2 border-ink-500 pl-2">
          <span className="text-slate-500">{fmtTime(last.ts)}</span> · {describeEntry(last)}
          {last.evidence && <div className="text-slate-500 break-words">{evidenceText(last.evidence, 8)}</div>}
        </div>
      )}
    </div>
  );
}

function LogRow({ e }: { e: AlertLogEntry }) {
  const tone = e.event === 'fire' ? (e.mode === 'on' ? severityTone(e.severity) : 'magenta')
    : e.event === 'action' ? (e.executed ? 'green' : 'magenta') : 'dim';
  return (
    <div className="py-1.5 border-b border-ink-700 last:border-0 text-xs flex flex-col gap-0.5">
      <div className="flex flex-wrap items-center gap-2">
        <Dot tone={tone} />
        <span className="text-slate-500 tnum">{fmtTime(e.ts)}</span>
        {e.id && <span className="font-mono text-slate-300">{e.id}</span>}
        {e.mode && <Chip tone={MODE_TONE[e.mode] ?? 'dim'}>{e.mode}</Chip>}
        <span className="text-slate-300">{describeEntry(e)}</span>
      </div>
      {(e.evidence || e.data) && (
        <div className="text-slate-500 break-words pl-3.5">{evidenceText((e.evidence ?? e.data) as Record<string, unknown>, 10)}</div>
      )}
    </div>
  );
}

function FunctionsPanel({ fns, onServiceDone }: {
  fns: AlertsStatus['functions']; onServiceDone: (id: string) => void;
}) {
  const eco = fns.eco_coach as { drive?: { stops?: number; regen_share?: number | null }; stops?: { ts: number; from_kmh: number; regen_share: number | null }[] } | undefined;
  const mt = fns.maintenance as {
    range_km?: number | null; l100?: number | null; l100_basis?: string; fuel_l?: number | null; note?: string;
    odometer_km?: number | null; known_dtcs?: string[];
    services?: { id: string; name: string; remaining_km: number | null; remaining_days: number | null; done_km: number | null; status: string }[];
  } | undefined;
  const gd = fns.parked_guardian as { off_for_s?: number | null; events?: { ts: number; id: string; text: string }[] } | undefined;
  const tn = fns.tunnel_recirc as { state?: string; presses_last_hour?: number; last?: string | null } | undefined;
  const wn = fns.winter as { outside_c?: number | null; source?: string } | undefined;
  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <Panel title="Braking score" code="REGEN / FRICTION" icon={Gauge} tone="green">
        <DataRow label="Stops this drive" value={eco?.drive?.stops ?? 0} />
        <DataRow label="Regen share (drive)" value={eco?.drive?.regen_share != null ? Math.round(eco.drive.regen_share * 100) : undefined} unit="%" tone="green" />
        <div className="text-[0.65rem] text-slate-600">21C3 regen torque vs master-cylinder torque at 1 Hz while braking; relative figure (units not verified on the car).</div>
        <div className="flex flex-col gap-0.5 max-h-40 overflow-y-auto">
          {(eco?.stops ?? []).slice(-12).reverse().map((s, i) => (
            <div key={i} className="text-xs text-slate-400 flex justify-between">
              <span>{fmtTime(s.ts)} · from {Math.round(s.from_kmh)} km/h</span>
              <span className="tnum text-slate-200">{s.regen_share != null ? `${Math.round(s.regen_share * 100)} %` : '--'}</span>
            </div>
          ))}
        </div>
      </Panel>
      <Panel title="Range and service" code="PETROL ONLY" icon={Wrench} tone="amber">
        <DataRow label="Range estimate" value={mt?.range_km != null ? Math.round(mt.range_km) : undefined} unit="km" tone="amber" />
        <DataRow label="Consumption basis" value={mt?.l100 != null ? mt.l100.toFixed(1) : undefined} unit={`L/100 km ${mt?.l100_basis ?? ''}`} />
        <DataRow label="Fuel level (0x5A4)" value={mt?.fuel_l ?? undefined} unit="L" />
        <DataRow label="Odometer (BEAN 52:3F)" value={mt?.odometer_km ?? undefined} unit="km" />
        {mt?.note && <div className="text-[0.65rem] text-slate-600">{mt.note}</div>}
        <div className="flex flex-col gap-1 mt-1">
          {(mt?.services ?? []).map((s) => (
            <div key={s.id} className="flex flex-wrap items-center gap-2 text-xs">
              <span className="text-slate-300 w-36">{s.name}</span>
              <span className={cx('tnum', s.status === 'due' || s.status === 'overdue' ? 'text-hud-amber' : 'text-slate-400')}>
                {s.status === 'unset' ? 'not set' : `${s.remaining_km != null ? `${Math.round(s.remaining_km)} km` : ''}${s.remaining_days != null ? ` / ${Math.round(s.remaining_days)} d` : ''}`}
              </span>
              <button className="ml-auto text-[0.6rem] uppercase tracking-wider border border-ink-500 px-1.5 py-0.5 text-slate-500 hover:text-slate-200"
                onClick={() => onServiceDone(s.id)} title="Record this service as done at the current odometer">done now</button>
            </div>
          ))}
        </div>
      </Panel>
      <Panel title="Parked-car guardian" code="EVENTS ONLY" icon={BellRing} tone="magenta">
        <DataRow label="Parked for" value={gd?.off_for_s != null ? `${Math.round(gd.off_for_s / 60)} min` : undefined} />
        <div className="flex flex-col gap-0.5 max-h-40 overflow-y-auto">
          {(gd?.events ?? []).slice(-15).reverse().map((e, i) => (
            <div key={i} className="text-xs text-slate-400">{fmtTime(e.ts)} · <span className="text-slate-200">{e.text}</span> <span className="text-slate-600">{e.id}</span></div>
          ))}
          {!(gd?.events ?? []).length && <div className="text-xs text-slate-600">no events recorded</div>}
        </div>
      </Panel>
      <Panel title="Tunnel recirc · winter" code="AVC-LAN" icon={ListChecks} tone="cyan">
        <DataRow label="Tunnel automation state" value={tn?.state} />
        <DataRow label="Presses last hour" value={tn?.presses_last_hour ?? 0} />
        <DataRow label="Last" value={tn?.last ?? undefined} />
        <DataRow label="Outside temperature" value={wn?.outside_c ?? undefined} unit={`°C ${wn?.source ?? ''}`} />
      </Panel>
    </div>
  );
}

export function AlertsTab() {
  const [st, setSt] = useState<AlertsStatus | null>(null);
  const [log, setLog] = useState<AlertLogEntry[]>([]);
  const [filter, setFilter] = useState<string>('');
  const [down, setDown] = useState(false);

  const refresh = useCallback(async () => {
    const [s, l] = await Promise.all([
      getJson<AlertsStatus>('/alerts'),
      getJson<{ entries: AlertLogEntry[] }>(`/alerts/log?limit=150${filter ? `&id=${encodeURIComponent(filter)}` : ''}`),
    ]);
    setDown(s == null);
    if (s) setSt(s);
    if (l) setLog(l.entries);
  }, [filter]);

  useEffect(() => {
    let alive = true;
    let timer = 0;
    const tick = async () => {
      if (document.visibilityState !== 'hidden') await refresh();
      if (alive) timer = window.setTimeout(tick, 3000);
    };
    tick();
    return () => { alive = false; window.clearTimeout(timer); };
  }, [refresh]);

  const setMode = async (t: AlertType, m: AlertMode) => {
    const r = await postJson(`/alerts/${t.id}/mode`, { mode: m });
    toast(r.ok ? `${t.id}: ${m}` : `${t.id}: ${r.detail}`, r.ok ? 'green' : 'red', r.ok ? 2500 : 6000);
    refresh();
  };
  const serviceDone = async (id: string) => {
    const r = await postJson(`/alerts/service/${id}/done`, {});
    toast(r.ok ? `${id}: recorded` : `${id}: ${r.detail}`, r.ok ? 'green' : 'red', 4000);
    refresh();
  };

  if (!st) {
    return (
      <div className="hud-panel p-6 text-sm text-slate-500">
        {down ? 'Alert framework not reachable (backend without /api/v1/alerts, or BACKEND_ALERTS=0).' : 'Loading…'}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="hud-panel px-4 py-2.5 flex flex-wrap items-center gap-3 text-xs">
        <span className="text-hud-amber uppercase tracking-[0.22em] text-[0.7rem]">Driver alerts</span>
        <span className="text-slate-500">informs only · never steers, brakes or accelerates · climate is the only actuation</span>
        <span className="ml-auto text-slate-400">drive #{st.drive}</span>
        {st.shown ? <Chip tone={severityTone(st.shown.severity)}>on MFD: {st.shown.text}</Chip> : <Chip tone="dim">nothing on the MFD</Chip>}
        <Chip tone={st.sound.available ? 'green' : 'dim'}>sound: {st.sound.sink}</Chip>
        <Chip tone={st.notify.available ? 'green' : 'dim'}>push: {st.notify.sink}</Chip>
        <a href={`${API_URL}/alerts/log/export`} className="flex items-center gap-1 border border-ink-500 px-2 py-1 text-slate-400 hover:text-slate-100" download>
          <Download size={12} /> shadow log (JSONL)
        </a>
      </div>

      {groupTypes(st.types).map(([kind, types]) => (
        <Panel key={kind} title={`${KIND_LABEL[kind] ?? kind}s`} code={`${types.length} TYPES`} icon={BellRing}
          tone={kind === 'alert' ? 'amber' : kind === 'automation' ? 'cyan' : 'magenta'}>
          {types.map((t) => <TypeRow key={t.id} t={t} onMode={(m) => setMode(t, m)} />)}
        </Panel>
      ))}

      <FunctionsPanel fns={st.functions} onServiceDone={serviceDone} />

      <Panel title="Recent log" code="NEWEST FIRST" icon={ScrollText} tone="cyan"
        right={
          <select value={filter} onChange={(e) => setFilter(e.target.value)}
            className="bg-ink-900 border border-ink-500 text-xs text-slate-300 px-1 py-0.5">
            <option value="">all types</option>
            {st.types.map((t) => <option key={t.id} value={t.id}>{t.id}</option>)}
          </select>
        }>
        <div className="max-h-[32rem] overflow-y-auto">
          {log.map((e) => <LogRow key={`${e.seq}-${e.ts}`} e={e} />)}
          {!log.length && <div className="text-xs text-slate-600">no entries yet</div>}
        </div>
      </Panel>
    </div>
  );
}
