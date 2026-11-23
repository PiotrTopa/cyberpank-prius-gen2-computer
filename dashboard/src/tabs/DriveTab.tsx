import { Battery, BatteryCharging, Eye, Gauge } from 'lucide-react';
import type { AppState } from '../types';
import { fmtAge } from '../lib/format';
import { DataRow, Meter, Panel, StatusRow } from '../components/ui';
import { MapTab } from './MapTab';

/** Car-on data: vehicle, hybrid energy, roadsense perception and the map. With
 *  the key off the values are the last ones seen and the panels say so. */
export default function DriveTab({ state, now }: { state: AppState; now: number }) {
  const veh = state.vehicle;
  const energy = state.energy;
  const acc = state.powerbox.acc_on ?? veh.acc_on;
  const off = acc === false && !veh.ready_mode;
  const lastSeen = off ? 'car off · last seen' : null;
  const soc = Math.round((energy.battery_soc ?? 0) * 100);
  const rs = state.roadsense;
  const health = (rs?.health ?? {}) as {
    camera_fps?: number; model_ms?: number; cpu_temp_c?: number;
    env?: { place?: string; weather?: string; light?: string };
    speed_limit?: { limit_kmh?: number | null; source?: string };
  };
  const rsRunning = rs?.state === 'running';

  return (
    <div className="flex flex-col gap-3">
      {off && (
        <div className="hud-panel px-4 py-3 text-[0.75rem] text-slate-400 uppercase tracking-wider">
          The car is off. This tab is for driving data; the MFD is the live dashboard in the car.
        </div>
      )}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3 items-start">
        <Panel title="Vehicle" icon={Gauge} stale={lastSeen}>
          <DataRow label="Speed" value={veh.speed_kmh?.toFixed(0)} unit="km/h" tone="cyan" />
          <DataRow label="Gear" value={veh.gear?.replace('GearPosition.', '')} />
          <StatusRow label="Ready" on={veh.ready_mode} onText="READY" offText="OFF" />
          <StatusRow label="Engine" on={veh.ice_running} onText="RUNNING" offText="OFF" />
          <StatusRow label="EV mode" on={veh.ev_mode} onText="EV" offText="OFF" />
          {veh.odometer_km != null && <DataRow label="Odometer" value={veh.odometer_km.toFixed(0)} unit="km" />}
        </Panel>

        <Panel title="Hybrid energy" icon={Battery} tone="green" stale={lastSeen}
          right={energy.charging
            ? <span className="text-xs text-hud-green flex items-center gap-1"><BatteryCharging size={13} /> CHG</span>
            : energy.regen_active ? <span className="text-xs text-hud-cyan">REGEN</span> : undefined}>
          <Meter label="HV battery SOC" value={soc} unit="%" pct={soc} tone={soc < 30 ? 'amber' : 'green'} />
          <DataRow label="Motor (MG2)" value={energy.motor_power_kw?.toFixed(1)} unit="kW" />
          <DataRow label="Generator (MG1)" value={energy.generator_power_kw?.toFixed(1)} unit="kW" />
          <DataRow label="Engine" value={energy.ice_power_kw?.toFixed(1)} unit="kW" />
        </Panel>

        <Panel title="Roadsense" icon={Eye} tone="violet"
          stale={!rsRunning && rs?.state ? `${rs.state}${rs.state_since ? ` · ${fmtAge(rs.state_since, now)}` : ''}` : null}>
          <DataRow label="Camera" value={health.camera_fps?.toFixed(1)} unit="fps" />
          <DataRow label="Model" value={health.model_ms?.toFixed(0)} unit="ms" />
          <DataRow label="Scene" value={health.env ? [health.env.place, health.env.weather, health.env.light].filter(Boolean).join(' · ').replace(/_/g, ' ') : undefined} />
          <DataRow label="Speed limit" value={health.speed_limit?.limit_kmh ?? undefined} unit="km/h" />
          <DataRow label="Phone hottest" value={rs?.hottest_c?.toFixed(1)} unit="°C" />
          {(rs?.restarts ?? 0) > 0 && <DataRow label="Restarts" value={rs?.restarts} tone="amber" />}
        </Panel>
      </div>
      <MapTab state={state} now={now} />
    </div>
  );
}
