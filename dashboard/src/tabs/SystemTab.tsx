import { Bug } from 'lucide-react';
import type { AppState, SatelliteNode, Satellites } from '../types';
import { transportOf } from '../lib/satellites';
import { Btn, Panel } from '../components/ui';
import { ControlsTab } from './ControlsTab';
import { SatellitesTab } from './SatellitesTab';

/** Settings and maintenance: power protection, satellites (pairing, rail),
 *  gateway status and the raw debug view. */
export default function SystemTab({ state, connected, now, satNodes, satHolders, onOpenDebug }: {
  state: AppState;
  connected: boolean;
  now: number;
  satNodes: SatelliteNode[];
  satHolders: string[];
  onOpenDebug: () => void;
}) {
  const sats: Satellites = state.satellites ?? {};
  return (
    <div className="flex flex-col gap-4">
      <ControlsTab state={state} connected={connected} now={now}
        satNodes={satNodes.filter((n) => transportOf(n) === 'rs485')} manualHeld={satHolders.includes('manual:dash')} />
      <SatellitesTab sats={sats} nodes={satNodes} holders={satHolders} rail={state.powerbox.rs485_power} now={now} />
      <Panel title="Debug" icon={Bug} tone="amber">
        <p className="text-xs text-slate-500">
          Raw state of every subsystem. Opening it streams the full state at 1 Hz (heavier on mobile data).
        </p>
        <Btn tone="amber" onClick={onOpenDebug}>Open debug view</Btn>
      </Panel>
    </div>
  );
}
