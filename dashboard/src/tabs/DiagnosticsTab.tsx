import { useState } from 'react';
import { Download, Eraser, FileSearch, RefreshCw, ScanLine, ShieldAlert, Stethoscope } from 'lucide-react';
import type { DiagAutoClear, DiagAutoClearEntry, DiagDtc, DiagEcu, Diagnostics } from '../types';
import { cx, fmtAge } from '../lib/format';
import { API_URL, sendCommand } from '../lib/api';
import { Btn, Chip, DataRow, Dot, Panel, type Tone } from '../components/ui';

const STATUS_TONE: Record<string, Tone> = {
  ok: 'green', scanning: 'cyan', no_answer: 'amber', error: 'red', unreachable: 'dim', unknown: 'dim',
};
const STATUS_TEXT: Record<string, string> = {
  ok: 'Answered', scanning: 'Scanning', no_answer: 'No answer', error: 'Error',
  unreachable: 'Not on CAN', unknown: 'Not scanned',
};

const fmtTime = (ts?: number | null) =>
  ts ? new Date(ts * 1000).toLocaleString([], { dateStyle: 'short', timeStyle: 'medium' }) : '--';

function DtcRow({ dtc, listed, onToggle }: { dtc: DiagDtc; listed?: boolean; onToggle?: () => void }) {
  return (
    <div className="border-l-2 border-hud-amber/60 pl-3 py-1 flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-hud-amber text-sm tnum tracking-wider">{dtc.code}</span>
        {dtc.status.map((s) => (
          <Chip key={s} tone={s === 'stored' ? 'amber' : s === 'pending' ? 'violet' : 'dim'}>{s.replace('_', ' ')}</Chip>
        ))}
        {dtc.master_warning && dtc.master_warning !== 'none' && (
          <Chip tone="red">master warning{dtc.master_warning === 'some' ? ' (some)' : ''}</Chip>
        )}
        {onToggle && (
          <button
            onClick={onToggle}
            title={listed ? 'Remove from the auto-clear whitelist' : 'Add to the auto-clear whitelist'}
            className={cx('ml-auto text-[0.6rem] uppercase tracking-wider border px-1.5 py-0.5',
              listed ? 'border-hud-violet text-hud-violet' : 'border-ink-500 text-slate-500 hover:text-slate-200')}
          >
            {listed ? 'auto-clear ✓' : '+ auto-clear'}
          </button>
        )}
      </div>
      <div className="text-xs text-slate-300">
        {dtc.description || <span className="text-slate-600">not in the dictionary — raw code</span>}
        {dtc.other_ecu && <span className="text-slate-600"> (text from another system's chart)</span>}
      </div>
      {!!dtc.trouble_area?.length && (
        <div className="text-[0.65rem] text-slate-500">Trouble area: {dtc.trouble_area.join(' · ')}</div>
      )}
      <div className="text-[0.65rem] text-slate-600 flex flex-wrap gap-x-3">
        {dtc.manual_page && (
          dtc.manual_url
            ? <a className="text-hud-cyan/80 hover:text-hud-cyan" href={dtc.manual_url} target="_blank" rel="noreferrer">manual {dtc.manual_page}</a>
            : <span>manual {dtc.manual_page}</span>
        )}
        {!!dtc.sources?.length && <span>via {dtc.sources.join(', ')}</span>}
        {!!dtc.inf_codes?.length && <span>detail (INF) codes: {dtc.inf_codes.join(', ')}</span>}
      </div>
    </div>
  );
}

function EcuCard({ ecu, busy, connected, now, whitelist, onToggleCode }: {
  ecu: DiagEcu;
  busy: boolean;
  connected: boolean;
  now: number;
  whitelist: DiagAutoClearEntry[];
  onToggleCode: (ecu: string, code: string) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const tone = STATUS_TONE[ecu.status] ?? 'dim';
  const dtcs = ecu.dtcs ?? [];
  const addr = ecu.address ? `${ecu.address}${ecu.sub_address ? ` / ${ecu.sub_address}` : ''}` : 'no CAN address';
  const ff = ecu.freeze_frame;

  const clear = async () => {
    setConfirming(false);
    await sendCommand('diag_clear', { ecu: ecu.key, confirm: true });
  };

  return (
    <div className="hud-panel p-3 flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Dot tone={tone} pulse={ecu.status === 'scanning'} />
        <span className="text-xs uppercase tracking-[0.15em] text-slate-200">{ecu.name}</span>
        <span className="text-[0.65rem] text-slate-600 tnum">{addr}</span>
        <Chip tone={tone}>{STATUS_TEXT[ecu.status] ?? ecu.status}</Chip>
        {ecu.mil != null && <Chip tone={ecu.mil ? 'red' : 'dim'}>MIL {ecu.mil ? 'on' : 'off'}</Chip>}
        {dtcs.length > 0 && <Chip tone="amber">{dtcs.length} code{dtcs.length > 1 ? 's' : ''}</Chip>}
        {ecu.partial && <Chip tone="amber">partial list</Chip>}
        <span className="ml-auto text-[0.6rem] text-slate-600">
          {ecu.last_scan_ts ? `scanned ${fmtAge(ecu.last_scan_ts, now)} ago` : ''}
        </span>
      </div>

      {ecu.error && <div className="text-[0.7rem] text-slate-500">{ecu.error}</div>}
      {ecu.status === 'unreachable' && ecu.note && (
        <div className="text-[0.65rem] text-slate-600">{ecu.note}</div>
      )}

      {dtcs.map((d) => (
        <DtcRow
          key={d.code}
          dtc={d}
          listed={whitelist.some((w) => w.ecu === ecu.key && w.code === d.code)}
          onToggle={ecu.can_clear ? () => onToggleCode(ecu.key, d.code) : undefined}
        />
      ))}
      {ecu.status === 'ok' && dtcs.length === 0 && (
        <div className="text-[0.7rem] text-hud-green/80">No codes reported.</div>
      )}

      {ff && (
        <div className="text-[0.65rem] text-slate-500">
          Freeze frame ({ff.dtc}): {Object.entries(ff.values ?? {}).map(([k, v]) => `${k.replace(/_/g, ' ')} ${v.value}${v.unit ? ` ${v.unit}` : ''}`).join(' · ') || 'no values'}
        </div>
      )}
      {!!ecu.monitors?.incomplete?.length && (
        <div className="text-[0.65rem] text-slate-500">Readiness not complete: {ecu.monitors.incomplete.join(', ')}</div>
      )}
      {!!ecu.log?.length && (
        <div className="text-[0.6rem] text-slate-600">{ecu.log.join(' · ')}</div>
      )}
      {ecu.last_clear && (
        <div className={cx('text-[0.65rem]', ecu.last_clear.ok ? 'text-hud-green/80' : 'text-hud-amber')}>
          Last clear {fmtTime(ecu.last_clear.ts)}{ecu.last_clear.origin === 'autoclear' ? ' (auto)' : ''}: {ecu.last_clear.result}
        </div>
      )}

      {ecu.can_clear && (
        <div className="flex flex-wrap items-center gap-2 pt-1">
          {!confirming ? (
            <Btn tone="red" className="py-1" disabled={busy || !connected} onClick={() => setConfirming(true)}>
              <span className="inline-flex items-center gap-1.5"><Eraser size={12} /> Clear codes</span>
            </Btn>
          ) : (
            <>
              <span className="text-[0.65rem] text-hud-amber max-w-md">
                Erases every code, the freeze frame and the readiness monitors of this ECU. Vehicle must be stopped.
              </span>
              <Btn tone="red" active className="py-1" onClick={clear}>Confirm clear</Btn>
              <Btn className="py-1" onClick={() => setConfirming(false)}>Cancel</Btn>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function AutoClearPanel({ ac, ecus, connected }: {
  ac?: DiagAutoClear | null;
  ecus: DiagEcu[];
  connected: boolean;
}) {
  const [ecuKey, setEcuKey] = useState('engine');
  const [code, setCode] = useState('');
  const codes = ac?.codes ?? [];
  const clearable = ecus.filter((e) => e.can_clear);
  const valid = /^[PCBU][0-3][0-9A-F]{3}$/.test(code.trim().toUpperCase());

  const setCodes = (next: DiagAutoClearEntry[]) => sendCommand('diag_autoclear_set', { codes: next });
  const add = async () => {
    const c = code.trim().toUpperCase();
    if (!valid || codes.some((w) => w.ecu === ecuKey && w.code === c)) return;
    if (await setCodes([...codes, { ecu: ecuKey, code: c, enabled: true }])) setCode('');
  };
  const name = (key: string) => ecus.find((e) => e.key === key)?.name ?? key;

  return (
    <Panel
      title="Auto-clear" code="DIAG-AC" icon={ShieldAlert} tone="violet"
      right={
        <Btn tone={ac?.enabled ? 'violet' : 'cyan'} active={!!ac?.enabled} className="py-1" disabled={!connected || !ac}
          onClick={() => sendCommand('diag_autoclear_set', { enabled: !ac?.enabled })}>
          {ac?.enabled ? 'Enabled' : 'Disabled'}
        </Btn>
      }
    >
      <p className="text-[0.65rem] text-slate-500">
        Clears an ECU only when every stored and pending code on it is on this list — a clear erases all of
        the ECU's codes, its freeze frame and the readiness monitors. One other code blocks it.
      </p>

      <div className="flex flex-col gap-1">
        {codes.length === 0 && <span className="text-[0.7rem] text-slate-600">No codes on the whitelist.</span>}
        {codes.map((w) => (
          <div key={`${w.ecu}:${w.code}`} className="flex items-center gap-2 text-xs">
            <span className={cx('tnum tracking-wider', w.enabled === false ? 'text-slate-600' : 'text-hud-violet')}>{w.code}</span>
            <span className="text-slate-500">{name(w.ecu)}</span>
            <button
              className="ml-auto text-[0.6rem] uppercase tracking-wider text-slate-500 hover:text-slate-200"
              onClick={() => setCodes(codes.map((x) => (x === w ? { ...x, enabled: x.enabled === false } : x)))}
            >
              {w.enabled === false ? 'enable' : 'disable'}
            </button>
            <button
              className="text-[0.6rem] uppercase tracking-wider text-slate-500 hover:text-hud-red"
              onClick={() => setCodes(codes.filter((x) => x !== w))}
            >
              remove
            </button>
          </div>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <select
          value={ecuKey} onChange={(e) => setEcuKey(e.target.value)}
          className="bg-ink-900 border border-ink-500 text-xs text-slate-300 px-2 py-1.5"
        >
          {clearable.map((e) => <option key={e.key} value={e.key}>{e.name}</option>)}
        </select>
        <input
          value={code} onChange={(e) => setCode(e.target.value)} placeholder="P0420" maxLength={5}
          className="bg-ink-900 border border-ink-500 text-xs text-slate-200 px-2 py-1.5 w-24 uppercase tnum"
        />
        <Btn className="py-1" disabled={!valid || !connected} onClick={add}>Add</Btn>
        <span className="text-[0.6rem] text-slate-600">or use “+ auto-clear” on a code found by a scan</span>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1 border-t border-ink-700 pt-2">
        <div className="flex justify-between items-center gap-3">
          <span className="text-slate-500 text-xs uppercase tracking-wider">Act on</span>
          <div className="flex gap-1">
            {(['stored', 'pending'] as const).map((t) => (
              <Btn key={t} className="py-0.5 px-2" active={ac?.trigger === t} tone="violet"
                onClick={() => sendCommand('diag_autoclear_set', { trigger: t })}>{t}</Btn>
            ))}
          </div>
        </div>
        <div className="flex justify-between items-center gap-3">
          <span className="text-slate-500 text-xs uppercase tracking-wider">When</span>
          <div className="flex gap-1">
            {(['stopped', 'anytime'] as const).map((t) => (
              <Btn key={t} className="py-0.5 px-2" active={ac?.speed_policy === t} tone={t === 'anytime' ? 'amber' : 'violet'}
                onClick={() => sendCommand('diag_autoclear_set', { speed_policy: t })}>{t}</Btn>
            ))}
          </div>
        </div>
        <DataRow label="Min interval" value={ac?.min_interval_s != null ? Math.round(ac.min_interval_s / 60) : null} unit="min" />
        <DataRow label="Max per day" value={ac?.max_per_day} />
        <DataRow label="Clears / today" value={ac ? `${ac.counters?.clears ?? 0} / ${ac.counters?.today ?? 0}` : null} />
        <DataRow label="Last check" value={fmtTime(ac?.last_check_ts)} />
      </div>

      {Object.entries(ac?.blocked ?? {}).map(([k, b]) => (
        <div key={k} className="text-xs text-hud-amber border border-hud-amber/40 px-2 py-1">
          {name(k)}: {b.reason} <span className="text-slate-500">(since {fmtTime(b.since)})</span>
        </div>
      ))}
      {Object.entries(ac?.waiting ?? {}).map(([k, w]) => (
        <div key={k} className="text-[0.7rem] text-slate-400">{name(k)}: waiting — {w}</div>
      ))}

      {!!ac?.recent?.length && (
        <div className="flex flex-col gap-0.5 border-t border-ink-700 pt-2">
          <span className="text-[0.6rem] uppercase tracking-[0.2em] text-slate-600">Last actions</span>
          {[...ac.recent].reverse().map((a, i) => (
            <div key={`${a.ts}-${i}`} className="text-[0.7rem] flex flex-wrap gap-x-2">
              <span className="text-slate-500 tnum">{fmtTime(a.ts)}</span>
              <span className={a.ok ? 'text-hud-green' : a.deferred ? 'text-slate-400' : 'text-hud-amber'}>
                {a.ok ? 'cleared' : a.deferred ? 'deferred' : 'not cleared'}
              </span>
              <span className="text-slate-300">{name(a.ecu)} {a.codes.join(', ')}</span>
              <span className="text-slate-600">{a.result}</span>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

export function DiagnosticsTab({ diag, connected, now }: {
  diag?: Diagnostics;
  connected: boolean;
  now: number;
}) {
  const ecus = Object.values(diag?.ecus ?? {});
  const scan = diag?.scan;
  const ac = diag?.autoclear;
  const running = scan?.state === 'running';
  const total = ecus.reduce((n, e) => n + (e.dtcs?.length ?? 0), 0);
  const answered = ecus.filter((e) => e.responded).length;
  // K-line / BEAN ECUs stay in the "not reachable" list until a probe was sent to them.
  const isOff = (e: DiagEcu) => e.bus !== 'can' && !e.responded && e.status !== 'no_answer' && e.status !== 'scanning';
  const onCan = ecus.filter((e) => !isOff(e));
  const offCan = ecus.filter(isOff);
  const whitelist = ac?.codes ?? [];

  const toggleCode = (ecu: string, code: string) => {
    const has = whitelist.some((w) => w.ecu === ecu && w.code === code);
    const next = has
      ? whitelist.filter((w) => !(w.ecu === ecu && w.code === code))
      : [...whitelist, { ecu, code, enabled: true }];
    void sendCommand('diag_autoclear_set', { codes: next });
  };

  if (!diag?.ecus) {
    return (
      <Panel title="Diagnostics" code="DIAG" icon={Stethoscope}>
        <span className="text-xs text-slate-500">The backend does not publish diagnostics (older backend build).</span>
      </Panel>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <Panel
        title="All-ECU scan" code="DIAG" icon={Stethoscope}
        right={<Chip tone={running ? 'cyan' : total ? 'amber' : 'dim'}>
          {running ? `scanning ${scan?.done ?? 0}/${scan?.total ?? 0}` : `${total} code${total === 1 ? '' : 's'}`}
        </Chip>}
      >
        <div className="flex flex-wrap items-center gap-2">
          <Btn active tone="cyan" disabled={running || !connected} onClick={() => sendCommand('diag_scan', {})}>
            <span className="inline-flex items-center gap-1.5"><ScanLine size={13} /> Scan</span>
          </Btn>
          <Btn disabled={running || !connected} onClick={() => sendCommand('diag_scan', { probe: true })}
            title="Also sends one tester-present frame to each CAN address that is unverified on this car">
            <span className="inline-flex items-center gap-1.5"><FileSearch size={13} /> Scan + probe</span>
          </Btn>
          <Btn disabled={running || !connected} onClick={() => sendCommand('diag_identify', {})}>
            <span className="inline-flex items-center gap-1.5"><RefreshCw size={13} /> Identify</span>
          </Btn>
          {running && <Btn tone="amber" active onClick={() => sendCommand('diag_cancel', {})}>Cancel</Btn>}
          <a
            href={`${API_URL}/diag/report`} target="_blank" rel="noreferrer"
            className="border border-ink-500 px-3 py-2 text-[0.7rem] uppercase tracking-[0.15em] text-slate-400 hover:text-slate-100 hover:border-slate-400 inline-flex items-center gap-1.5"
          >
            <Download size={13} /> Report JSON
          </a>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-x-6 gap-y-1">
          <DataRow label="Last scan" value={fmtTime(scan?.finished_ts)} />
          <DataRow label="ECUs answering" value={`${answered} / ${onCan.length}`} />
          <DataRow label="VIN" value={diag.vehicle?.vin ?? null} />
          <DataRow label="Report" value={scan?.report_file ?? null} />
        </div>
        {running && scan?.current && (
          <div className="text-[0.7rem] text-hud-cyan">Asking {diag.ecus[scan.current]?.name ?? scan.current}…</div>
        )}
        {scan?.error && <div className="text-[0.7rem] text-hud-amber">{scan.error}</div>}
      </Panel>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
        {onCan.map((e) => (
          <EcuCard key={e.key} ecu={e} busy={running} connected={connected} now={now}
            whitelist={whitelist} onToggleCode={toggleCode} />
        ))}
      </div>

      <AutoClearPanel ac={ac} ecus={ecus} connected={connected} />

      {offCan.length > 0 && (
        <Panel title="Not reachable over CAN" code="K-LINE / BEAN" icon={FileSearch} tone="dim">
          <p className="text-[0.65rem] text-slate-500">
            On this car these ECUs are diagnosed over the K-line (DLC3 pin 7) or sit on the BEAN body bus behind
            the gateway ECU. “Scan + probe” asks their later-model CAN addresses once.
          </p>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-1">
            {offCan.map((e) => (
              <div key={e.key} className="flex justify-between gap-3 text-xs">
                <span className="text-slate-400">{e.name}</span>
                <span className="text-slate-600 tnum">{e.bus === 'bean' ? 'BEAN' : 'K-line'}{e.address ? ` · probe ${e.address}${e.sub_address ? `/${e.sub_address}` : ''}` : ''}</span>
              </div>
            ))}
          </div>
        </Panel>
      )}
    </div>
  );
}
