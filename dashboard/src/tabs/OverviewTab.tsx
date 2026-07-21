import {
  Battery, BatteryCharging, ChevronRight, Fuel, Gauge, Radio, Wind, Zap,
} from 'lucide-react';
import type { AppState, SatelliteNode, Satellites } from '../types';
import { fmtAge } from '../lib/format';
import type { HistoryData } from '../hooks/useHistory';
import { DataRow, Meter, Panel, StatusRow } from '../components/ui';
import { SatellitesPanel } from '../components/SatellitesPanel';

export function OverviewTab({ state, hist, now, satNodes, satHolders, onOpenDebug }: {
  state: AppState;
  hist: HistoryData;
  now: number;
  satNodes: SatelliteNode[];
  satHolders: string[];
  onOpenDebug: () => void;
}) {
  const pb = state.powerbox;
  const veh = state.vehicle;
  const energy = state.energy;
  const climate = state.climate ?? {};
  const sats: Satellites = state.satellites ?? {};

  const v = pb.system_voltage;
  const powerW = pb.power_draw_w?.toFixed(2) ?? '--';
  const pocoPowerW = pb.poco_power_w?.toFixed(2) ?? '--';
  const soc = Math.round((energy.battery_soc ?? 0) * 100);

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      <Panel title="Vehicle" code="VEH-01" icon={Gauge}>
        <DataRow label="Speed" value={veh.speed_kmh?.toFixed(0)} unit="km/h" tone="cyan" />
        <DataRow label="Gear" value={veh.gear?.replace('GearPosition.', '')} />
        <StatusRow label="Ready Mode" on={veh.ready_mode} onText="READY" offText="OFF" />
        <StatusRow label="ICE" on={veh.ice_running} onText="RUNNING" offText="OFF" />
        <StatusRow label="EV Mode" on={veh.ev_mode} onText="EV" offText="OFF" />
      </Panel>

      <Panel title="Hybrid Energy" code="HV-01" icon={Battery} tone="green"
        right={energy.charging
          ? <span className="text-xs text-hud-green flex items-center gap-1"><BatteryCharging size={13} /> CHG</span>
          : energy.regen_active ? <span className="text-xs text-hud-cyan">REGEN</span> : undefined}>
        <Meter label="HV Battery SOC" value={soc} unit="%" pct={soc} tone={soc < 30 ? 'amber' : 'green'} />
        <DataRow label="Motor (MG2)" value={energy.motor_power_kw?.toFixed(1)} unit="kW" />
        <DataRow label="Generator (MG1)" value={energy.generator_power_kw?.toFixed(1)} unit="kW" />
        <DataRow label="Engine" value={energy.ice_power_kw?.toFixed(1)} unit="kW" />
      </Panel>

      <Panel title="Computer Power" code="PWR-01" icon={Zap} tone="amber"
        right={<span className="text-[0.65rem] text-slate-600">age {fmtAge(pb.last_update_time, now)}</span>}>
        <DataRow label="Stack Live Draw" value={powerW} unit="W" tone="amber" />
        <DataRow label="Stack 1m Avg" value={hist.avgPower1m} unit="W" />
        <DataRow label="Stack 5m Avg" value={hist.avgPower5m} unit="W" />
        <DataRow label="Stack 1h Avg" value={hist.avgPower1h} unit="W" />
        <DataRow label="POCO Live Draw" value={pocoPowerW} unit="W" tone="amber" />
        <DataRow label="POCO 1m Avg" value={hist.avgPocoPower1m} unit="W" />
        <DataRow label="POCO 5m Avg" value={hist.avgPocoPower5m} unit="W" />
        <DataRow label="POCO 1h Avg" value={hist.avgPocoPower1h} unit="W" />
        <DataRow label="Aux Voltage" value={v?.toFixed(2)} unit="V" />
        <DataRow label="Energy Used" value={pb.energy_mah?.toFixed(1)} unit="mAh" />
      </Panel>

      <Panel title="Environment" code="ENV-01" icon={Wind} tone="magenta">
        <DataRow label="Cabin Temp" value={pb.aht_t?.toFixed(1)} unit="°C" tone="magenta" />
        <DataRow label="Humidity" value={pb.aht_h?.toFixed(1)} unit="%" />
        <DataRow label="Pressure" value={pb.bmp_p ? (pb.bmp_p / 100).toFixed(1) : undefined} unit="hPa" />
        <DataRow label="Outside" value={climate.outside_temp?.toFixed?.(1)} unit="°C" />
      </Panel>

      <Panel title="Thermal Management" code="THM-01" icon={Wind}>
        <DataRow label="POCO CPU" value={pb.poco_core_temp?.toFixed(1)} unit="°C"
          tone={pb.poco_core_temp && pb.poco_core_temp > 60 ? 'amber' : 'cyan'} />
        <DataRow label="POCO GPU" value={pb.poco_gpu_temp?.toFixed(1)} unit="°C"
          tone={pb.poco_gpu_temp && pb.poco_gpu_temp > 60 ? 'amber' : 'cyan'} />
        <DataRow label="Simulated Temp" value={pb.poco_ema_temp?.toFixed(1)} unit="°C" tone="cyan" />
        <DataRow label="Chassis Fan" value={pb.fan_duty_pct?.toFixed(0)} unit="%" tone="cyan" />
      </Panel>

      <Panel title="Fuel" code="FUE-01" icon={Fuel} tone="violet">
        <Meter label="Petrol" value={veh.fuel_level} unit="L" pct={((veh.fuel_level ?? 0) / 45) * 100} tone="violet" />
        <Meter label="LPG" value={veh.lpg_level} unit="L" pct={((veh.lpg_level ?? 0) / 45) * 100} tone="magenta" />
        <DataRow label="Active Fuel" value={veh.active_fuel} />
      </Panel>

      <Panel title="System Link" code="LNK-01" icon={Radio}
        right={
          <button onClick={onOpenDebug}
            className="text-[0.65rem] text-slate-600 hover:text-hud-amber flex items-center gap-1 uppercase tracking-wider">
            debug <ChevronRight size={12} />
          </button>
        }>
        <StatusRow label="Powerbox Link" on={pb.connected} onText="UP" offText="DOWN" />
        <StatusRow label="POCO Heartbeat" on={pb.poco_alive} onText="ALIVE" offText="LOST" />
        <DataRow label="PM State" value={(pb.pm_state || '—').toUpperCase()} />
        {pb.undervoltage && <StatusRow label="Under-voltage" on={true} onText="TRIPPED" />}
      </Panel>

      <SatellitesPanel sats={sats} nodes={satNodes} holders={satHolders} out2={pb.out2} now={now} />
    </div>
  );
}
