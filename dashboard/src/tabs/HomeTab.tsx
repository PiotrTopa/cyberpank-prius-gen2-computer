import { useState, type ReactNode } from 'react';
import { ChevronDown, Cpu, Fan, Server, Thermometer, Wind, Zap } from 'lucide-react';
import type { AppState, HostStats, SatelliteNode } from '../types';
import { cx, fmtAge, fmtUp } from '../lib/format';
import { sendCommand } from '../lib/api';
import type { Tone } from '../lib/tones';
import { TONE_TEXT } from '../lib/tones';
import type { PowerSummary } from '../hooks/usePowerSummary';
import { Btn, DataRow, Dot, Panel, Segmented, Sparkline } from '../components/ui';
import { SatellitesPanel } from '../components/SatellitesPanel';
import { UsbHubPanel } from '../components/UsbHubPanel';
import { CameraPreviewPanel } from '../components/CameraPreviewPanel';

const n1 = (v?: number | null) => (v == null ? undefined : v.toFixed(1));
/** Signed watts: + drawn from the 12 V battery, − net charge (solar charger). */
const w = (v?: number | null) => (v == null ? '--' : `${v < 0 ? '−' : ''}${Math.abs(v).toFixed(1)}`);
/** Amber while drawing, green while the battery is being charged. */
const flowTone = (v?: number | null): Tone => (v == null ? 'dim' : v < 0 ? 'green' : 'amber');

/** A host record older than this is shown as stale. */
const HOST_STALE_S = 30;

function hostAge(h: HostStats | null | undefined, nowS: number): number | null {
  if (!h?.t) return null;
  return Math.max(0, nowS - h.t);
}

function hostLine(h?: HostStats | null): string {
  if (!h) return '';
  const parts = [`up ${fmtUp(h.up_s)}`];
  if (h.cpu_pct != null) parts.push(`cpu ${h.cpu_pct.toFixed(0)}%`);
  if (h.hot_c != null) parts.push(`${h.hot_c.toFixed(0)}°C`);
  if (h.mem_used_pct != null) parts.push(`mem ${h.mem_used_pct.toFixed(0)}%`);
  return parts.join(' · ');
}

const TRANSITIONAL = ['booting', 'starting', 'grace', 'stopping', 'shutting_down', 'powering_off', 'camera_on', 'preview'];
function stateTone(state?: string | null, err?: boolean): Tone {
  if (err) return 'red';
  if (!state) return 'dim';
  if (state === 'on' || state === 'running') return 'green';
  if (TRANSITIONAL.includes(state)) return 'amber';
  return 'dim';
}

/** One "what is running" row; tap to open its actions. */
function SysRow({ id, name, state, tone, detail, open, onToggle, children }: {
  id: string;
  name: string;
  state: string;
  tone: Tone;
  detail?: ReactNode;
  open: boolean;
  onToggle: (id: string) => void;
  children?: ReactNode;
}) {
  const has = !!children;
  return (
    <div className="border-b border-ink-700 last:border-b-0">
      <button
        className={cx('w-full flex items-center justify-between gap-3 py-2 min-h-12 text-left', !has && 'cursor-default')}
        onClick={() => has && onToggle(id)}>
        <div className="flex items-center gap-2.5 min-w-0">
          <Dot tone={tone} pulse={tone === 'green'} />
          <div className="min-w-0">
            <div className="text-[0.82rem] text-slate-100 uppercase tracking-wider truncate">{name}</div>
            {detail && <div className="text-[0.68rem] text-slate-500 tracking-wide truncate">{detail}</div>}
          </div>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <span className={cx('text-[0.7rem] uppercase tracking-wider', TONE_TEXT[tone])}>{state}</span>
          {has && <ChevronDown size={14} className={cx('text-slate-600 transition-transform', open && 'rotate-180')} />}
        </div>
      </button>
      {has && open && <div className="pb-3 pl-4 flex flex-col gap-2">{children}</div>}
    </div>
  );
}

const RESET_CAUSE: Record<number, string> = { 1: 'power-on', 3: 'watchdog' };

/** The powerbox itself: link, state machine, firmware, protection. Everything
 *  else on the car hangs off it, so it sits above "What's running". */
function PowerboxPanel({ state, now }: { state: AppState; now: number }) {
  const pb = state.powerbox;
  const link = !!pb.connected;
  const pm = pb.pm_state ?? (link ? 'normal' : null);
  const tone: Tone = !link ? 'red' : pb.undervoltage || (pm && pm !== 'normal') ? 'amber' : 'green';
  const errs = (pb.relay_err_count ?? 0) + (pb.relay_fix_count ?? 0);
  return (
    <Panel title="Powerbox" icon={Cpu} tone={tone}
      right={<span className={cx('text-[0.7rem] uppercase tracking-wider', TONE_TEXT[tone])}>
        {!link ? 'no link' : pb.undervoltage ? 'under-voltage' : pm}
      </span>}>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-[0.75rem]">
        <span className="text-slate-500 uppercase tracking-wider">Link</span>
        <span className="text-right text-slate-200">{link ? `up · ${fmtAge(pb.last_update_time, now)} ago` : 'down'}</span>
        <span className="text-slate-500 uppercase tracking-wider">Firmware up</span>
        <span className="text-right text-slate-200 tnum">
          {fmtUp(pb.uptime_s)}{pb.reset_cause != null ? ` · ${RESET_CAUSE[pb.reset_cause] ?? `reset ${pb.reset_cause}`}` : ''}
        </span>
        <span className="text-slate-500 uppercase tracking-wider">ACC line</span>
        <span className={cx('text-right', pb.acc_on ? 'text-hud-green' : 'text-slate-400')}>{pb.acc_on == null ? '--' : pb.acc_on ? 'on' : 'off'}</span>
        <span className="text-slate-500 uppercase tracking-wider">UV cut / recover</span>
        <span className="text-right text-slate-200 tnum">
          {pb.uv_threshold != null ? `${pb.uv_threshold.toFixed(1)} / ${pb.uv_recover?.toFixed(1) ?? '--'} V` : '--'}
        </span>
        {errs > 0 && <>
          <span className="text-slate-500 uppercase tracking-wider">Relay repairs / errors</span>
          <span className="text-right text-hud-amber tnum">{pb.relay_fix_count ?? 0} / {pb.relay_err_count ?? 0}</span>
        </>}
      </div>
    </Panel>
  );
}

function SystemsPanel({ state, nowS }: { state: AppState; nowS: number }) {
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const toggle = (id: string) => setOpen((o) => (o === id ? null : id));
  const run = async (name: string, params: Record<string, unknown> = {}) => {
    setBusy(true);
    await sendCommand(name, params);
    setBusy(false);
  };

  const pb = state.powerbox;
  const conn = state.connection ?? { connected: false };
  const hosts = state.hosts ?? {};
  const p2 = state.poco2;
  const rs = state.roadsense;
  const hu = state.headunit;

  const pocoAge = hostAge(hosts.poco, nowS);
  const pocoFresh = pocoAge != null && pocoAge < HOST_STALE_S;
  const p2Host = hosts.poco2;
  const p2Fresh = (hostAge(p2Host, nowS) ?? 1e9) < HOST_STALE_S;

  // Power profile (/etc/prius/power-mode via the backend): full is held with
  // ACC on or while roadsense is meant to run; full with the key off drains 12 V.
  const profile = pb.power_mode ?? null;
  const accOn = !!(pb.acc_on ?? state.vehicle?.acc_on);
  const rsHolds = !!rs && rs.state != null && rs.state !== 'off';
  const fullWhileOff = profile === 'full' && !accOn;
  const units = Object.entries(rs?.units ?? {});
  const unitsUp = units.filter(([, s]) => s === 'active').length;
  const chris = rs?.units?.['chris-integrator.service'];

  return (
    <Panel title="What's running" icon={Server} tone="green">
      <div className="-mt-1">
        <SysRow id="poco" name="Car computer · poco" open={open === 'poco'} onToggle={toggle}
          state={profile ? `${profile} power` : pocoFresh ? 'on' : pb.poco_alive ? 'alive' : 'no stats'}
          tone={!pocoFresh && !pb.poco_alive ? 'amber' : fullWhileOff ? 'amber' : 'green'}
          detail={fullWhileOff
            ? `full profile with the car off${rsHolds ? ` · held by roadsense (mode ${rs?.mode})` : ' · no owner'}`
            : hosts.poco ? hostLine(hosts.poco) : undefined}>
          <div className="text-[0.68rem] text-slate-500">
            {hosts.poco ? <>{hostLine(hosts.poco)}<br /></> : null}
            Power profile: <span className="text-slate-300">{profile ?? '—'}</span>.
            Full = all 8 cores, used with the key on and whenever roadsense runs;
            low = big cores offline and Wi-Fi off after its grace, for the parked car.
          </div>
          {fullWhileOff && rsHolds && rs?.mode === 'on' && (
            <Btn disabled={busy} onClick={() => run('roadsense_mode', { mode: 'auto' })}>
              Roadsense back to auto (lets poco go low)
            </Btn>
          )}
        </SysRow>

        <SysRow id="poco2" name="poco2 · headless" open={open === 'poco2'} onToggle={toggle}
          state={p2?.enabled === false ? 'disabled' : p2?.state ?? '—'}
          tone={stateTone(p2?.state, !!p2?.last_error && p2?.state !== 'off')}
          detail={p2Fresh ? hostLine(p2Host) : p2 ? `mode ${p2.mode ?? '—'}${p2.state_since ? ` · since ${fmtAge(p2.state_since, nowS * 1000)}` : ''}` : undefined}>
          <div className="text-[0.65rem] text-slate-500 uppercase tracking-wider">Mode · auto follows the MFD</div>
          <Segmented value={p2?.mode as 'auto' | 'on' | 'off' | undefined} disabled={busy || !p2?.enabled}
            options={[['auto', 'Auto'], ['on', 'On'], ['off', 'Off']]}
            onChange={(m) => run('poco2_mode', { mode: m })} />
          <div className="flex gap-2">
            <Btn className="flex-1" disabled={busy || p2?.state !== 'on'} onClick={() => run('poco2_restart')}>
              Restart (clean)
            </Btn>
          </div>
          {p2?.last_error && <div className="text-[0.65rem] text-hud-amber break-words">{p2.last_error}</div>}
        </SysRow>

        <SysRow id="mfd" name="MFD Pi · video" open={open === 'mfd'} onToggle={toggle}
          state={conn.mfd_state ?? '—'} tone={stateTone(conn.mfd_state)}
          detail={[
            conn.mfd_reachable ? 'reachable' : conn.mfd_reachable === false ? 'no network' : null,
            conn.mfd_state_since ? `since ${fmtAge(conn.mfd_state_since, nowS * 1000)}` : null,
            conn.mfd_last_boot_s ? `boot ${conn.mfd_last_boot_s.toFixed(0)} s` : null,
            (conn.mfd_power_cycles ?? 0) > 0 ? `${conn.mfd_power_cycles} power cycles` : null,
          ].filter(Boolean).join(' · ')} />

        <SysRow id="gw" name="Gateway · CAN + AVC" open={open === 'gw'} onToggle={toggle}
          state={conn.connected ? 'on' : 'off'} tone={conn.connected ? 'green' : 'dim'}
          detail={conn.connected
            ? `fw ${conn.gateway_version ?? '?'} · CAN ${conn.can_ready ? 'ready' : 'wait'}${conn.panda_enabled ? ` · panda ${conn.panda_connected ? 'up' : 'down'}` : ''}`
            : 'follows ACC / satellite rail'} />

        <SysRow id="rs" name="Roadsense · chris" open={open === 'rs'} onToggle={toggle}
          state={rs?.enabled === false ? 'disabled' : rs?.state ?? '—'}
          tone={stateTone(rs?.state, !!rs?.last_error)}
          detail={rs ? `mode ${rs.mode ?? '—'}${units.length ? ` · ${unitsUp}/${units.length} units` : ''}${chris ? ` · integrator ${chris}` : ''}` : undefined}>
          <div className="text-[0.65rem] text-slate-500 uppercase tracking-wider">Mode · auto follows ACC</div>
          <Segmented value={rs?.mode as 'auto' | 'on' | 'off' | undefined} disabled={busy || rs?.enabled === false}
            options={[['auto', 'Auto'], ['on', 'On'], ['off', 'Off']]}
            onChange={(m) => run('roadsense_mode', { mode: m })} />
          {units.length > 0 && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-0.5">
              {units.map(([u, s]) => (
                <div key={u} className="flex justify-between text-[0.68rem] gap-2">
                  <span className="text-slate-500 truncate">{u.replace(/\.service$/, '')}</span>
                  <span className={s === 'active' ? 'text-hud-green' : 'text-slate-500'}>{s}</span>
                </div>
              ))}
            </div>
          )}
          <Btn disabled={busy || rs?.state !== 'running'} onClick={() => run('roadsense_restart')}>Restart stack</Btn>
        </SysRow>


        {hu?.enabled !== false && (
          <SysRow id="hu" name="Head unit" open={open === 'hu'} onToggle={toggle}
            state={hu?.present ? 'on' : 'off'} tone={hu?.present ? 'green' : 'dim'}
            detail={hu?.present ? `volume ${hu.volume ?? '—'}` : hu?.last_seen ? `last seen ${fmtAge(hu.last_seen, nowS * 1000)} ago` : undefined} />
        )}
      </div>
    </Panel>
  );
}

function Tile({ label, value, unit, sub, tone = 'cyan' }: {
  label: string; value?: string; unit?: string; sub?: ReactNode; tone?: Tone;
}) {
  return (
    <div className="hud-panel px-3 py-2.5 flex flex-col gap-0.5 min-w-0">
      <span className="text-[0.62rem] uppercase tracking-[0.18em] text-slate-500 truncate">{label}</span>
      <div className="flex items-baseline gap-1">
        <span className={cx('text-xl tnum', TONE_TEXT[tone])}>{value ?? '--'}</span>
        {unit && <span className="text-slate-600 text-[0.7rem]">{unit}</span>}
      </div>
      {sub && <span className="text-[0.62rem] text-slate-500 uppercase tracking-wider truncate">{sub}</span>}
    </div>
  );
}

const PowerRow = ({ label, now, s }: { label: string; now?: number | null; s?: PowerSummary['stack'] }) => (
  <div className="grid grid-cols-4 items-baseline text-right tnum">
    <span className="text-left text-slate-500 text-[0.78rem] uppercase tracking-wider">{label}</span>
    <span className={cx('text-base', TONE_TEXT[flowTone(now)])}>{w(now)}</span>
    <span className={cx('text-sm', s?.avg_1h != null && s.avg_1h < 0 ? 'text-hud-green' : 'text-slate-200')}>{w(s?.avg_1h)}</span>
    <span className={cx('text-sm', s?.avg_24h != null && s.avg_24h < 0 ? 'text-hud-green' : 'text-slate-400')}>{w(s?.avg_24h)}</span>
  </div>
);

/** Signed power over 24 h as bars around zero: amber up = drawn from the
 *  battery, green down = net charge from the solar charger. */
function FlowBars({ points, end, height = 56 }: { points: [number, number][]; end: number; height?: number }) {
  if (points.length < 2) return <div className="text-[0.65rem] text-slate-600" style={{ height }}>no history yet</div>;
  const start = end - 86400;
  const maxUp = Math.max(0.5, ...points.map(([, v]) => v));
  const maxDn = Math.max(0, ...points.map(([, v]) => -v));
  const span = maxUp + maxDn;
  const zero = (maxUp / span) * height;
  const bw = 100 / 96;
  return (
    <svg viewBox={`0 0 100 ${height}`} preserveAspectRatio="none" className="w-full" style={{ height }}>
      {points.map(([t, v]) => {
        const x = ((t - start) / 86400) * 100;
        const h = (Math.abs(v) / span) * height;
        return (
          <rect key={t} x={x} width={bw * 0.8} y={v >= 0 ? zero - h : zero} height={Math.max(h, 0.4)}
            className={v >= 0 ? 'fill-hud-amber' : 'fill-hud-green'} opacity={0.85} />
        );
      })}
      <line x1={0} x2={100} y1={zero} y2={zero} className="stroke-slate-600" strokeWidth={0.6} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function PowerPanel({ state, sum }: { state: AppState; sum: PowerSummary | null }) {
  const pb = state.powerbox;
  const volts = (sum?.voltage_24h ?? []).map(([, v]) => v);
  const flow = sum?.power_24h ?? [];
  return (
    <Panel title="Power" icon={Zap} tone="amber"
      right={sum?.energy_24h_wh != null
        ? <span className={cx('text-[0.7rem] tnum', sum.energy_24h_wh < 0 ? 'text-hud-green' : 'text-slate-400')}>
            net {w(sum.energy_24h_wh).replace(/\.\d$/, '')} Wh / 24 h
          </span>
        : undefined}>
      <div className="flex flex-col gap-1.5">
        <div className="grid grid-cols-4 text-right text-[0.6rem] text-slate-600 uppercase tracking-wider">
          <span className="text-left">W</span><span>now</span><span>1 h</span><span>24 h</span>
        </div>
        <PowerRow label="Stack" now={pb.power_draw_w} s={sum?.stack} />
        <PowerRow label="poco" now={pb.poco_power_w} s={sum?.poco} />
      </div>
      <div className="flex flex-col gap-1">
        <div className="flex justify-between text-[0.65rem] uppercase tracking-wider tnum">
          <span className="text-slate-500">Flow · 24 h</span>
          <span>
            <span className="text-hud-amber">▲ used {sum?.consumed_24h_wh?.toFixed(0) ?? '--'} Wh</span>
            <span className="text-hud-green ml-2">▼ solar {sum?.charged_24h_wh?.toFixed(0) ?? '--'} Wh</span>
          </span>
        </div>
        <FlowBars points={flow} end={sum?.ts ?? 0} />
      </div>
      <div className="flex flex-col gap-1">
        <div className="flex justify-between text-[0.65rem] text-slate-500 uppercase tracking-wider">
          <span>12 V · 24 h</span>
          <span className="tnum">
            {volts.length ? `${Math.min(...volts).toFixed(2)}–${Math.max(...volts).toFixed(2)} V` : ''}
          </span>
        </div>
        <Sparkline points={volts} tone="cyan" />
      </div>
    </Panel>
  );
}

function ThermalPanel({ state }: { state: AppState }) {
  const pb = state.powerbox;
  const h = state.hosts ?? {};
  const th = h.thermal;
  const [busy, setBusy] = useState(false);
  const fanOverride = pb.fan_override_pct ?? null;
  const fanMode: 'off' | 'on' | 'auto' = fanOverride == null ? 'auto' : fanOverride === 0 ? 'off' : 'on';
  const setFan = async (m: 'off' | 'on' | 'auto') => {
    setBusy(true);
    if (m === 'auto') await sendCommand('fan_auto');
    else await sendCommand('set_fan', { pct: m === 'on' ? 100 : 0 });
    setBusy(false);
  };
  const hot = (v?: number | null) => (v != null && v > 60 ? 'amber' : undefined);
  const cpu = h.poco?.cpu_c ?? pb.poco_core_temp;
  const gpu = h.poco?.gpu_c ?? pb.poco_gpu_temp;
  return (
    <Panel title="Thermal" icon={Thermometer}>
      <DataRow label="poco CPU / GPU" value={cpu != null || gpu != null ? `${n1(cpu) ?? '--'} / ${n1(gpu) ?? '--'}` : undefined} unit="°C"
        tone={hot(Math.max(cpu ?? 0, gpu ?? 0))} />
      {h.poco?.hot_zone && h.poco.hot_c != null && h.poco.hot_c > Math.max(cpu ?? 0, gpu ?? 0) && (
        <DataRow label="poco hottest" value={`${n1(h.poco.hot_c)}`} unit={`°C ${h.poco.hot_zone.replace(/-thermal$/, '')}`} />
      )}
      <DataRow label="poco2 hottest" value={n1(h.poco2?.hot_c)} unit="°C" tone={hot(h.poco2?.hot_c)} />
      <div className="flex items-center justify-between gap-3">
        <span className="text-slate-500 text-[0.78rem] uppercase tracking-wider flex items-center gap-1.5"><Fan size={13} /> Fan</span>
        <span className="text-sm tnum text-slate-100">
          {pb.fan_duty_pct?.toFixed(0) ?? '--'}%<span className="text-slate-600 text-[0.65rem] ml-1">{pb.fan_rpm ?? '--'} rpm</span>
        </span>
      </div>
      <Segmented value={fanMode} disabled={busy} options={[['auto', 'Auto'], ['on', 'On'], ['off', 'Off']]} onChange={setFan} />
      {th?.reason && fanMode === 'auto' && (
        <div className="text-[0.65rem] text-slate-600 uppercase tracking-wider">auto: {th.reason}</div>
      )}
    </Panel>
  );
}

function EnvironmentPanel({ state }: { state: AppState }) {
  const pb = state.powerbox;
  const env = (state.roadsense?.health as { env?: { place?: string; weather?: string; light?: string } } | null | undefined)?.env;
  const camLive = state.roadsense?.state === 'running';
  const offline = pb.bmp2_t == null && pb.aht_t == null && pb.bmp_t == null;
  return (
    <Panel title="Environment" icon={Wind} tone="magenta"
      right={offline ? <span className="text-[0.65rem] text-hud-amber tracking-wider">SENSORS OFFLINE</span> : undefined}>
      <DataRow label="Cabin" value={n1(pb.bmp2_t)} unit="°C" tone="magenta" />
      <DataRow label="Pressure" value={pb.bmp2_p != null ? (pb.bmp2_p / 100).toFixed(1) : undefined} unit="hPa" />
      {(pb.aht_t != null || pb.bmp_t != null) && (
        <DataRow label="Box inside" value={n1(pb.aht_t ?? pb.bmp_t)} unit="°C" />
      )}
      {pb.aht_h != null && <DataRow label="Box humidity" value={pb.aht_h.toFixed(0)} unit="%" />}
      {state.climate?.outside_temp != null && <DataRow label="Outside" value={n1(state.climate.outside_temp)} unit="°C" />}
      {camLive && env && (
        <div className="flex justify-between items-baseline gap-3">
          <span className="text-slate-500 text-[0.78rem] uppercase tracking-wider shrink-0">Camera sees</span>
          <span className="text-sm text-slate-200 text-right">
            {[env.place, env.weather, env.light].filter(Boolean).join(' · ').replace(/_/g, ' ')}
          </span>
        </div>
      )}
    </Panel>
  );
}

export function HomeTab({ state, now, sum, satNodes, satHolders, onOpenSystem }: {
  state: AppState;
  now: number;
  sum: PowerSummary | null;
  satNodes: SatelliteNode[];
  satHolders: string[];
  onOpenSystem: () => void;
}) {
  const pb = state.powerbox;
  const nowS = now / 1000;
  const v = pb.system_voltage;
  const vTone: Tone = v == null ? 'dim' : v < 11.8 ? 'red' : v < 12.2 ? 'amber' : 'cyan';
  const acc = pb.acc_on ?? state.vehicle?.acc_on;
  const pbAge = pb.last_update_time != null ? nowS - pb.last_update_time : null;
  const stale = pbAge == null || pbAge > 15 ? `stale ${fmtAge(pb.last_update_time, now)}` : null;

  return (
    <div className="flex flex-col gap-3">
      <section className="grid grid-cols-2 md:grid-cols-4 gap-2">
        <Tile label="12 V battery" value={v?.toFixed(2)} unit="V" tone={vTone}
          sub={sum?.voltage_min_24h != null ? `min ${sum.voltage_min_24h.toFixed(2)} V / 24 h` : stale ?? 'live'} />
        <Tile label="Car" value={acc == null ? '--' : acc ? 'ACC ON' : 'OFF'} tone={acc ? 'green' : 'dim'}
          sub={state.vehicle?.ready_mode ? 'ready' : pb.undervoltage ? 'under-voltage' : pb.pm_state ?? undefined} />
        <Tile label={pb.power_draw_w != null && pb.power_draw_w < 0 ? 'Charging now' : 'Draw now'}
          value={w(pb.power_draw_w)} unit="W" tone={flowTone(pb.power_draw_w)}
          sub={pb.power_draw_w != null && pb.power_draw_w < 0 ? 'solar > load'
            : sum?.stack.avg_1h != null ? `1 h avg ${w(sum.stack.avg_1h)} W` : undefined} />
        <Tile label="Net · 24 h" value={sum?.energy_24h_wh != null ? w(sum.energy_24h_wh).replace(/\.\d$/, '') : undefined} unit="Wh"
          tone={flowTone(sum?.energy_24h_wh)}
          sub={sum?.consumed_24h_wh != null ? `used ${sum.consumed_24h_wh.toFixed(0)} · solar ${sum.charged_24h_wh?.toFixed(0) ?? 0}` : undefined} />
      </section>

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3 items-start">
        {/* Phone order (columns stack): power, environment, what's running,
            powerbox, thermal, then camera, USB hub, satellites. */}
        <div className="flex flex-col gap-3">
          <PowerPanel state={state} sum={sum} />
          <EnvironmentPanel state={state} />
        </div>
        <div className="flex flex-col gap-3">
          <SystemsPanel state={state} nowS={nowS} />
          <PowerboxPanel state={state} now={now} />
        </div>
        <div className="flex flex-col gap-3 md:col-span-2 xl:col-span-1">
          <ThermalPanel state={state} />
          <CameraPreviewPanel server={state.roadsense?.camera_preview} now={now} />
          <UsbHubPanel state={state} manualHeld={satHolders.includes('manual:dash')} />
          <SatellitesPanel sats={state.satellites ?? {}} nodes={satNodes} holders={satHolders}
            rail={pb.rs485_power} now={now} onOpen={onOpenSystem} />
        </div>
      </div>
      {stale && <div className="text-center text-[0.7rem] text-hud-amber uppercase tracking-wider">powerbox telemetry {stale}</div>}
    </div>
  );
}
