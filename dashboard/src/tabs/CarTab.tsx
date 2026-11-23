import { useState } from 'react';
import { Fuel } from 'lucide-react';
import type { AppState } from '../types';
import { DataRow, Meter, Panel, SubNav } from '../components/ui';
import { DiagnosticsTab } from './DiagnosticsTab';
import { AlertsTab } from './AlertsTab';

type FuelEntry = { id: number; ts: number; fuel: string; litres: number; total?: number | null; odometer_km?: number | null };
type Sub = 'codes' | 'alerts' | 'fuel';

/** Hash: #car/<sub>; the old #diagnostics and #alerts links land on their section. */
function subFromHash(): Sub {
  const [a, b] = window.location.hash.replace('#', '').split('/');
  if (a === 'alerts' || b === 'alerts') return 'alerts';
  if (b === 'fuel') return 'fuel';
  return 'codes';
}

function FuelPanel({ state }: { state: AppState }) {
  const veh = state.vehicle;
  const entries = ((state.fuel_log?.entries ?? []) as FuelEntry[]).slice(-5).reverse();
  return (
    <Panel title="Fuel" icon={Fuel} tone="violet" className="md:max-w-xl">
      <Meter label="Petrol" value={veh.fuel_level} unit="L" pct={((veh.fuel_level ?? 0) / 45) * 100} tone="violet" />
      <Meter label="LPG" value={veh.lpg_level} unit="L" pct={((veh.lpg_level ?? 0) / 45) * 100} tone="magenta" />
      <DataRow label="Active fuel" value={veh.active_fuel} />
      {entries.length > 0 && <div className="text-[0.65rem] text-slate-600 uppercase tracking-wider pt-1">Last fill-ups</div>}
      {entries.map((e) => (
        <DataRow key={e.id}
          label={`${new Date(e.ts * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' })} ${e.fuel}`}
          value={`${e.litres.toFixed(1)} L${e.total != null ? ` · ${e.total.toFixed(0)}` : ''}`} />
      ))}
    </Panel>
  );
}

/** Car details for when they matter: trouble codes, driver alerts, fuel. */
export default function CarTab({ state, connected, now }: { state: AppState; connected: boolean; now: number }) {
  const [sub, setSubState] = useState<Sub>(subFromHash);
  const setSub = (s: Sub) => {
    setSubState(s);
    window.history.replaceState(null, '', `#car/${s}`);
  };
  return (
    <div className="flex flex-col gap-4">
      <SubNav value={sub} onChange={setSub} options={[
        ['codes', 'Trouble codes', 'Fault codes stored in the car’s control units (engine, hybrid system, HV battery, ...). Scan reads them over CAN while the car is on; you can clear them or auto-clear known harmless ones.'],
        ['alerts', 'Driver alerts', 'Warnings the MFD can give while driving, each set to off, shadow (only logged, to check it before trusting it) or on. Also the braking score, service reminders, parked-car guardian and the log of what fired.'],
        ['fuel', 'Fuel', 'Petrol and LPG levels and the last fill-ups.'],
      ]} />
      {sub === 'codes' && <DiagnosticsTab diag={state.diagnostics} connected={connected} now={now} />}
      {sub === 'alerts' && <AlertsTab />}
      {sub === 'fuel' && <FuelPanel state={state} />}
    </div>
  );
}
