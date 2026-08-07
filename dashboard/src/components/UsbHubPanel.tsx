import { useState, type ReactNode } from 'react';
import { Usb } from 'lucide-react';
import type { AppState } from '../types';
import { sendCommand } from '../lib/api';
import { Btn, Dot, Panel } from './ui';
import { cx } from '../lib/format';

const power = (on: boolean | null) =>
  on === null
    ? { text: 'PWR ?', cls: 'text-slate-600' }
    : on
      ? { text: 'PWR ON', cls: 'text-hud-green' }
      : { text: 'PWR OFF', cls: 'text-slate-500' };

const SocketRow = ({ n, name, powered, right }: {
  n: string;
  name: string;
  powered: boolean | null;
  right?: ReactNode;
}) => {
  const p = power(powered);
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-slate-500 text-xs uppercase tracking-wider">
        <span className="text-slate-600">{n}</span> {name}
      </span>
      <div className="flex items-center gap-2">
        {right}
        <span className={cx('text-[0.65rem] tracking-wider', p.cls)}>{p.text}</span>
      </div>
    </div>
  );
};

const LinkChip = ({ up, upText = 'LINK', downText = 'NO LINK' }: {
  up?: boolean | null; upText?: string; downText?: string;
}) => (
  <span className={cx(
    'flex items-center gap-1 text-[0.6rem] tracking-wider',
    up ? 'text-hud-cyan' : 'text-slate-600',
  )}>
    <Dot tone={up ? 'cyan' : 'red'} pulse={!!up} />
    {up ? upText : downText}
  </span>
);

/**
 * USB hub socket map + live power/link statuses.
 *
 * Topology (bring-up verified 2026-08-01): socket 1 = powerbox on the hub's
 * only native-PPS port; sockets 2-4 get their VBUS through powerbox relays
 * (ch4 = gateway, ch3 = MFD Pi, ch2 = RTL-SDR; ch1 = spare). Relay states come
 * from the powerbox STATUS "rly" telemetry — actual hardware truth, not the
 * commanded value. The SDR port is the only manually-owned one, so it gets a
 * toggle (backend `set_relay` with desired-state enforcement).
 */
export function UsbHubPanel({ state, now }: { state: AppState; now: number }) {
  const pb = state.powerbox;
  const conn = state.connection ?? { connected: false };
  const relays = pb.relays ?? [];
  const relay = (ch: number): boolean | null => {
    const v = relays[ch - 1];
    return v === undefined || v === null ? null : Boolean(v);
  };

  const sdrOn = relay(2);
  const telemetryLive = pb.connected ?? false;

  // SDR relay is desired-state with a 15 s enforcement tick on the backend.
  // Pending is derived, not cleared by an effect: it expires when the mirrored
  // `rly` telemetry converges or the deadline passes (`now` ticks at 1 Hz).
  const [sdrCmd, setSdrCmd] = useState<{ want: boolean; until: number } | null>(null);
  const sdrPending = sdrCmd !== null && sdrOn !== sdrCmd.want && now < sdrCmd.until;

  const toggleSdr = async () => {
    const want = !sdrOn;
    setSdrCmd({ want, until: now + 25000 });
    const ok = await sendCommand('set_relay', { channel: 2, on: want });
    if (!ok) setSdrCmd(null);
  };

  return (
    <Panel title="USB Hub" code="USB-01" icon={Usb}
      right={!telemetryLive
        ? <span className="text-[0.6rem] text-hud-amber tracking-wider">TELEMETRY STALE</span>
        : undefined}>
      <SocketRow n="S1" name="Powerbox" powered={true}
        right={<LinkChip up={pb.connected} />} />
      <SocketRow n="S2" name="Gateway" powered={relay(4)}
        right={<LinkChip up={conn.connected} />} />
      <SocketRow n="S3" name="MFD Pi" powered={relay(3)}
        right={
          <span className="text-[0.6rem] text-slate-500 tracking-wider uppercase">
            {(conn.mfd_state || '—')}{conn.mfd_reachable ? ' · NET' : ''}
          </span>
        } />
      <SocketRow n="S4" name="RTL-SDR" powered={sdrOn}
        right={
          <Btn tone={sdrOn ? 'red' : 'green'} className="px-2 py-0.5 text-[0.6rem]"
            disabled={!telemetryLive || sdrPending}
            onClick={toggleSdr}>
            {sdrPending ? 'WAIT…' : sdrOn ? 'CUT' : 'POWER'}
          </Btn>
        } />
      {conn.gateway_usb_power_desired != null
        && relay(4) != null
        && conn.gateway_usb_power_desired !== relay(4) && (
        <div className="text-[0.6rem] text-hud-amber tracking-wider">
          GATEWAY PORT CONVERGING (desired {conn.gateway_usb_power_desired ? 'ON' : 'OFF'})
        </div>
      )}
    </Panel>
  );
}
