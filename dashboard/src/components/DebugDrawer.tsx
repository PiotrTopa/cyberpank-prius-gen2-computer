import type { ReactNode, ComponentType } from 'react';
import {
  Bug, CircuitBoard, HeartPulse, Plug, SatelliteDish, Terminal, TriangleAlert, X,
} from 'lucide-react';
import type { AppState, Connection, Powerbox } from '../types';
import { cx } from '../lib/format';

const railColor = (x?: boolean | null) => (x == null ? 'text-slate-600' : x ? 'text-hud-green' : 'text-hud-red');
const railText = (x?: boolean | null) => (x == null ? 'N/A' : x ? 'HIGH' : 'LOW');
const railDot = (x?: boolean | null) => (x == null ? 'bg-slate-600' : x ? 'bg-hud-green' : 'bg-hud-red');

const Row = ({ k, children }: { k: string; children: ReactNode }) => (
  <div className="flex justify-between items-center gap-3 py-1.5 border-b border-ink-700/60 last:border-0">
    <span className="text-slate-500 text-xs">{k}</span>
    <span className="text-xs tnum text-slate-100 text-right">{children}</span>
  </div>
);

const Rail = ({ name, desc, val }: { name: string; desc: string; val?: boolean | null }) => (
  <div className="flex items-center justify-between gap-2 bg-ink-850 border border-ink-700 px-3 py-2">
    <div className="min-w-0">
      <div className="text-sm text-slate-100">{name}</div>
      <div className="text-[0.65rem] text-slate-600 truncate">{desc}</div>
    </div>
    <div className="flex items-center gap-2 shrink-0">
      <span className={cx('h-1.5 w-1.5', railDot(val))} />
      <span className={cx('text-sm uppercase', railColor(val))}>{railText(val)}</span>
    </div>
  </div>
);

const Section = ({ title, icon: Icon, children }: {
  title: string;
  icon: ComponentType<{ size?: number | string }>;
  children: ReactNode;
}) => (
  <div className="flex flex-col gap-1">
    <div className="flex items-center gap-2 text-hud-amber mt-4 mb-1">
      <Icon size={13} />
      <h3 className="text-[0.65rem] font-medium uppercase tracking-[0.22em]">{title}</h3>
    </div>
    {children}
  </div>
);

export function DebugDrawer({ pb, conn, state, now, onClose }: {
  pb: Powerbox;
  conn?: Connection;
  state: AppState;
  now: number;
  onClose: () => void;
}) {
  const age = (ts?: number | null) => {
    if (ts == null || ts === 0) return '--';
    const s = Math.max(0, Math.floor(now / 1000 - ts));
    return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m` : `${Math.floor(s / 3600)}h`;
  };

  const pbAge = now / 1000 - (pb.last_update_time ?? 0);
  const fresh = pb.last_update_time != null && pbAge < 4;

  return (
    <div className="fixed inset-0 z-50">
      <div className="absolute inset-0 bg-black/70" onClick={onClose} />
      <aside className="absolute right-0 top-0 h-full w-full max-w-md bg-ink-950 border-l border-hud-amber/40 flex flex-col">
        <div className="flex items-center justify-between px-5 py-4 border-b border-ink-700">
          <div className="flex items-center gap-2 text-hud-amber">
            <Terminal size={15} />
            <h2 className="text-xs font-medium uppercase tracking-[0.22em]">Debug · Powerbox</h2>
          </div>
          <button onClick={onClose} className="p-1.5 text-slate-400 hover:text-white hover:bg-white/10">
            <X size={16} />
          </button>
        </div>

        <div className="overflow-y-auto px-5 pb-8 flex-1">
          <div className="flex items-center gap-2 mt-3 text-xs">
            <span className={cx('h-1.5 w-1.5', fresh ? 'bg-hud-green pulse-dot' : 'bg-hud-amber')} />
            <span className={fresh ? 'text-hud-green' : 'text-hud-amber'}>
              {fresh ? 'STREAMING' : 'STALE'} · last update {age(pb.last_update_time)} ago
            </span>
          </div>

          <Section title="Power Rails (MOSFET drivers)" icon={CircuitBoard}>
            <div className="flex flex-col gap-2">
              <Rail name="OUT1 · GP29" desc="Master rail — POCO + RP2040 + hub (latch)" val={pb.out1} />
              <Rail name="OUT2 · GP28" desc="RS485 satellite power" val={pb.out2} />
              <Rail name="OUT3 · GP27" desc="Spare" val={pb.out3} />
            </div>
          </Section>

          <Section title="Heartbeat & State Machine" icon={HeartPulse}>
            <Row k="poco_alive">
              <span className={pb.poco_alive ? 'text-hud-green' : 'text-hud-red'}>
                {pb.poco_alive == null ? 'N/A' : pb.poco_alive ? 'ALIVE' : 'LOST'}
              </span>
            </Row>
            <Row k="pm_state"><span className="text-hud-cyan uppercase">{pb.pm_state || '--'}</span></Row>
            <Row k="powerbox_hb">{pb.powerbox_hb ?? '--'}</Row>
            <Row k="undervoltage">
              <span className={pb.undervoltage ? 'text-hud-red' : 'text-slate-300'}>{pb.undervoltage ? 'TRIPPED' : 'ok'}</span>
            </Row>
            <Row k="batt_present">{pb.batt_present == null ? '--' : pb.batt_present ? 'yes' : 'no'}</Row>
            <Row k="shutdown_requested">
              <span className={pb.shutdown_requested ? 'text-hud-amber' : 'text-slate-300'}>{pb.shutdown_requested ? 'YES' : 'no'}</span>
            </Row>
            {pb.shutdown_reason ? <Row k="shutdown_reason">{pb.shutdown_reason}</Row> : null}
          </Section>

          <Section title="INA219 / Sensors (raw)" icon={Plug}>
            <Row k="system_voltage">{pb.system_voltage?.toFixed(3) ?? '--'} V</Row>
            <Row k="current_draw">{pb.current_draw_a != null ? (pb.current_draw_a * 1000).toFixed(1) : '--'} mA</Row>
            <Row k="power_draw">{pb.power_draw_w != null ? pb.power_draw_w.toFixed(2) : '--'} W</Row>
            <Row k="energy_mah">{pb.energy_mah?.toFixed(3) ?? '--'} mAh</Row>
            <Row k="bmp_t / bmp_p">{pb.bmp_t?.toFixed(2) ?? '--'} °C · {pb.bmp_p ? (pb.bmp_p / 100).toFixed(1) : '--'} hPa</Row>
            <Row k="aht_t / aht_h">{pb.aht_t?.toFixed(2) ?? '--'} °C · {pb.aht_h?.toFixed(1) ?? '--'} %</Row>
            <Row k="acc_on · GP11 (ignition)">
              <span className={pb.acc_on ? 'text-hud-green' : 'text-slate-400'}>{pb.acc_on ? 'ON' : 'OFF'}</span>
            </Row>
          </Section>

          <Section title="Link / Gateway" icon={SatelliteDish}>
            <Row k="powerbox.connected">
              <span className={pb.connected ? 'text-hud-green' : 'text-hud-red'}>{pb.connected ? 'true' : 'false'}</span>
            </Row>
            <Row k="gateway.connected">
              <span className={conn?.connected ? 'text-hud-green' : 'text-hud-red'}>{conn?.connected ? 'true' : 'false'}</span>
            </Row>
            <Row k="gateway_version">{conn?.gateway_version ?? '--'}</Row>
            <Row k="gateway_hb (alive)">{conn?.gateway_hb == null ? '--' : conn.gateway_hb}</Row>
            <Row k="gateway_uptime">{conn?.gateway_uptime_s == null ? '--' : `${conn.gateway_uptime_s}s`}</Row>
            <Row k="last_heartbeat">{age(conn?.last_heartbeat_time)} ago</Row>
            <Row k="can_ready">{conn?.can_ready == null ? '--' : String(conn.can_ready)}</Row>
            <Row k="avc_ready">{conn?.avc_ready == null ? '--' : String(conn.avc_ready)}</Row>
            <Row k="last_message">{age(conn?.last_message_time)} ago</Row>
          </Section>

          <Section title="Raw State JSON" icon={Bug}>
            <pre className="text-[0.65rem] leading-relaxed text-slate-400 bg-black/40 border border-ink-700 p-3 overflow-x-auto max-h-60 overflow-y-auto">
              {JSON.stringify(state.powerbox, null, 2)}
            </pre>
          </Section>

          <p className="text-center text-[0.65rem] text-slate-600 mt-4 flex items-center justify-center gap-1">
            <TriangleAlert size={12} /> press <span className="text-slate-400">D</span> or <span className="text-slate-400">Esc</span> to close
          </p>
        </div>
      </aside>
    </div>
  );
}
