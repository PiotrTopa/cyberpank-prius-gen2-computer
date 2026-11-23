import { useState, type ReactNode } from 'react';
import { Usb } from 'lucide-react';
import type { AppState } from '../types';
import { sendCommand } from '../lib/api';
import { Btn, Dot, Panel } from './ui';
import { cx, fmtUp } from '../lib/format';

const power = (on: boolean | null) =>
  on === null
    ? { text: '?', cls: 'text-slate-600', tone: 'dim' as const }
    : on
      ? { text: 'ON', cls: 'text-hud-green', tone: 'green' as const }
      : { text: 'OFF', cls: 'text-slate-500', tone: 'dim' as const };

const SocketRow = ({ ch, name, detail, powered, action }: {
  ch: string;
  name: string;
  detail?: ReactNode;
  powered: boolean | null;
  action?: ReactNode;
}) => {
  const p = power(powered);
  return (
    <div className="flex items-center justify-between gap-2 min-h-10">
      <div className="flex items-center gap-2.5 min-w-0">
        <Dot tone={p.tone} pulse={!!powered} />
        <div className="min-w-0">
          <div className="text-[0.8rem] text-slate-200 uppercase tracking-wider truncate">
            {name} <span className={cx('text-[0.65rem] ml-1', p.cls)}>{p.text}</span>
          </div>
          <div className="text-[0.65rem] text-slate-600 tracking-wider uppercase truncate">
            {ch}{detail ? <> · {detail}</> : null}
          </div>
        </div>
      </div>
      {action}
    </div>
  );
};

/**
 * Powered USB hub: socket 1 = powerbox on the hub's native port, the rest get
 * VBUS through the powerbox relay board (powerbox/README.md, since 2026-11):
 * ch2 = MFD Pi, ch3 = CAN hub (gateway + panda), ch4 = camera hub (dashcam for
 * roadsense and the preview), ch5 = RS485 satellite rail. Relay states are the
 * powerbox STATUS "rly" read-back — hardware truth, not the commanded value.
 *
 * Each load has an owner on the backend, so the actions here go through that
 * owner, never a raw relay cut: MFD = `mfd_hold`, rail = satellite wake-lock;
 * the CAN hub follows ACC / the rail holders and the camera hub follows
 * roadsense or a camera preview.
 */
export function UsbHubPanel({ state, manualHeld }: { state: AppState; manualHeld?: boolean }) {
  const pb = state.powerbox;
  const conn = state.connection ?? { connected: false };
  const rs = state.roadsense;
  const relays = pb.relays ?? [];
  const relay = (ch: number): boolean | null => {
    const v = relays[ch - 1];
    return v === undefined || v === null ? null : Boolean(v);
  };
  const live = pb.connected ?? false;
  const [busy, setBusy] = useState<string | null>(null);
  const run = async (id: string, name: string, params: Record<string, unknown>) => {
    setBusy(id);
    await sendCommand(name, params);
    setBusy(null);
  };

  const mfdOn = relay(2);
  const railOn = relay(5) ?? pb.rs485_power ?? null;
  const preview = rs?.camera_preview;

  return (
    <Panel title="USB Hub · relays" icon={Usb}
      stale={!live ? 'telemetry stale' : null}>
      <SocketRow ch="native port" name="Powerbox" powered={true}
        detail={pb.connected ? `link up · fw up ${fmtUp(pb.uptime_s)}` : 'no link'} />
      <SocketRow ch="relay 2" name="MFD Pi" powered={mfdOn}
        detail={`${conn.mfd_state ?? '—'}${conn.mfd_reachable ? ' · net' : ''}`}
        action={
          <div className="flex gap-1.5">
            <Btn className="px-2.5 py-1 text-[0.65rem]" tone="green" disabled={busy !== null}
              title="Keep the MFD powered with the key off (30 min max)"
              onClick={() => run('mfd1', 'mfd_hold', { on: true })}>
              {busy === 'mfd1' ? '…' : 'Hold'}
            </Btn>
            <Btn className="px-2.5 py-1 text-[0.65rem]" disabled={busy !== null}
              title="Back to the normal policy (grace, then clean shutdown)"
              onClick={() => run('mfd0', 'mfd_hold', { on: false })}>
              {busy === 'mfd0' ? '…' : 'Release'}
            </Btn>
          </div>
        } />
      <SocketRow ch="relay 3" name="CAN hub" powered={relay(3)}
        detail={<>
          gateway {conn.connected ? 'up' : 'down'}
          {conn.panda_enabled ? ` · panda ${conn.panda_connected ? 'up' : 'down'}` : ''}
        </>} />
      <SocketRow ch="relay 4" name="Camera hub" powered={relay(4)}
        detail={preview?.active ? `preview ${preview.phase ?? ''}` : rs?.state ? `roadsense ${rs.state}` : undefined} />
      <SocketRow ch="relay 5" name="RS485 rail" powered={railOn}
        detail={manualHeld ? 'held from dashboard' : 'satellites'}
        action={
          <Btn className="px-2.5 py-1 text-[0.65rem]" tone={manualHeld ? 'red' : 'green'} disabled={busy !== null}
            title="Manual wake-lock: rail + gateway stay powered while held"
            onClick={() => run('rail', 'satellite_power_hold', { name: 'dash', on: !manualHeld })}>
            {busy === 'rail' ? '…' : manualHeld ? 'Release' : 'Hold'}
          </Btn>
        } />
      {conn.gateway_usb_power_desired != null
        && relay(3) != null
        && conn.gateway_usb_power_desired !== relay(3) && (
        <div className="text-[0.65rem] text-hud-amber tracking-wider">
          CAN HUB CONVERGING (desired {conn.gateway_usb_power_desired ? 'ON' : 'OFF'})
        </div>
      )}
    </Panel>
  );
}
