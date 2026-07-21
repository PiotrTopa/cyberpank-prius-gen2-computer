import { LayoutDashboard, Power, SatelliteDish } from 'lucide-react';
import type { AppState, SatelliteNode } from '../types';
import { fmtAge } from '../lib/format';
import { sendCommand } from '../lib/api';
import { Btn, Chip, DataRow, Panel, StatusRow } from '../components/ui';
import { SatelliteNodeRow } from '../components/SatellitesPanel';

export function ControlsTab({ state, connected, now, satNodes, manualHeld }: {
  state: AppState;
  connected: boolean;
  now: number;
  satNodes: SatelliteNode[];
  manualHeld: boolean;
}) {
  const pb = state.powerbox;
  const conn = state.connection;

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      <Panel title="Remote Control" code="CTL-02" icon={Power}>
        <button
          className="w-full py-7 border border-dashed border-ink-500 text-slate-400 hover:text-hud-cyan hover:border-hud-cyan/50 transition-colors flex flex-col items-center justify-center gap-2"
          onClick={() => alert('Remote start functionality to be added!')}
        >
          <Power size={26} />
          <span className="uppercase tracking-[0.22em] text-xs">Start Vehicle Remotely</span>
        </button>
        <p className="text-xs text-slate-600 text-center">Sends ACC wake via powerbox · coming soon</p>
      </Panel>

      <Panel title="Satellite Rail (OUT2)" code="CTL-03" icon={SatelliteDish} tone="green"
        right={
          <Chip tone={pb.out2 == null ? 'dim' : pb.out2 ? 'green' : 'red'}>
            {pb.out2 == null ? 'N/A' : pb.out2 ? 'LIVE' : 'OFF'}
          </Chip>
        }>
        <div className="flex gap-3">
          <Btn className="flex-1 py-3" tone="green" active={manualHeld}
            onClick={() => sendCommand('satellite_power_hold', { name: 'dash', on: true })}>
            Hold ON
          </Btn>
          <Btn className="flex-1 py-3" tone="red" disabled={!manualHeld}
            onClick={() => sendCommand('satellite_power_hold', { name: 'dash', on: false })}>
            Release
          </Btn>
        </div>
        <p className="text-xs text-slate-600 text-center">
          Manual wake-lock: rail + gateway (CAN/AVC + RS485) stay powered while any holder
          (acc / queue / manual) is held. Drops ~10s after the last release.
        </p>
        {satNodes.length > 0 && (
          <div className="flex flex-col gap-1.5">
            {satNodes.map((n) => (
              <SatelliteNodeRow key={n.device_id} n={n} now={now}
                right={
                  <Btn tone="cyan" className="px-2 py-0.5 text-[0.6rem]"
                    title="Enqueue a status poll (powers the rail if needed)"
                    onClick={() => sendCommand('satellite_send', { device_id: n.device_id, payload: { a: 'status' } })}>
                    Ping
                  </Btn>
                }
              />
            ))}
          </div>
        )}
      </Panel>

      <Panel title="System Status" code="SYS-01" icon={LayoutDashboard} className="md:col-span-2">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-1.5">
          <StatusRow label="Backend Stream" on={connected} onText="LIVE" offText="DOWN" />
          <StatusRow label="Powerbox Firmware" on={pb.connected} onText="ACTIVE" offText="OFFLINE" />
          <StatusRow label="Gateway Link" on={conn?.connected} onText="UP" offText="DOWN" />
          <StatusRow label="Gateway USB Power" on={conn?.gateway_usb_power} onText="POWERED" offText="OFF" />
          <StatusRow label="CAN Gateway" on={conn?.can_ready} onText="READY" offText="WAIT" />
          <DataRow label="Telemetry Age" value={fmtAge(pb.last_update_time, now)} />
          <DataRow label="Gateway Ver" value={conn?.gateway_version ?? undefined} />
        </div>
      </Panel>
    </div>
  );
}
