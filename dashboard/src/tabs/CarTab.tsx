import { Fuel } from 'lucide-react';
import type { AppState } from '../types';
import { DataRow, Meter, Panel } from '../components/ui';
import { DiagnosticsTab } from './DiagnosticsTab';
import { AlertsTab } from './AlertsTab';

type FuelEntry = { id: number; ts: number; fuel: string; litres: number; total?: number | null; odometer_km?: number | null };

/** Car details for when it matters, not every glance: trouble codes, alerts, fuel. */
export default function CarTab({ state, connected, now }: { state: AppState; connected: boolean; now: number }) {
  const veh = state.vehicle;
  const entries = ((state.fuel_log?.entries ?? []) as FuelEntry[]).slice(-3).reverse();
  return (
    <div className="flex flex-col gap-4">
      <DiagnosticsTab diag={state.diagnostics} connected={connected} now={now} />
      <AlertsTab />
      <Panel title="Fuel" icon={Fuel} tone="violet" className="md:max-w-xl">
        <Meter label="Petrol" value={veh.fuel_level} unit="L" pct={((veh.fuel_level ?? 0) / 45) * 100} tone="violet" />
        <Meter label="LPG" value={veh.lpg_level} unit="L" pct={((veh.lpg_level ?? 0) / 45) * 100} tone="magenta" />
        <DataRow label="Active fuel" value={veh.active_fuel} />
        {entries.map((e) => (
          <DataRow key={e.id}
            label={`${new Date(e.ts * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' })} ${e.fuel}`}
            value={`${e.litres.toFixed(1)} L${e.total != null ? ` · ${e.total.toFixed(0)}` : ''}`} />
        ))}
      </Panel>
    </div>
  );
}
