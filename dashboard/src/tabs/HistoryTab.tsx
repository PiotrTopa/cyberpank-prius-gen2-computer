import { useState } from 'react';
import type { TimeRange } from '../types';
import { useHistory } from '../hooks/useHistory';
import { SubNav } from '../components/ui';
import { TelemetryTab } from './TelemetryTab';
import { BatteryTab } from './BatteryTab';

type Sub = 'health' | 'charts';

/** Hash: #battery[/...] = health (BatteryTab keeps its own sub-path), #charts = charts. */
const subFromHash = (): Sub => (window.location.hash.replace('#', '').split('/')[0] === 'charts' ? 'charts' : 'health');

function Charts() {
  const [timeRange, setTimeRange] = useState<TimeRange>('24h');
  const hist = useHistory(timeRange);   // polls /metrics only while Charts is open
  return <TelemetryTab hist={hist} timeRange={timeRange} onTimeRange={setTimeRange} />;
}

/** Battery tab: health analysis of the hybrid and 12 V batteries (default) and
 *  the raw metric charts. */
export default function HistoryTab() {
  const [sub, setSubState] = useState<Sub>(subFromHash);
  const setSub = (s: Sub) => {
    setSubState(s);
    window.history.replaceState(null, '', s === 'charts' ? '#charts' : '#battery');
  };
  return (
    <div className="flex flex-col gap-4">
      <SubNav value={sub} onChange={setSub} options={[
        ['health', 'Health', 'Hybrid (HV) and 12 V battery condition, worked out from past rides and parked rest periods: overall status, trends, per-block voltages, each ride.'],
        ['charts', 'Charts', 'Raw history from the car computer: 12 V voltage, power (negative = solar charging), poco draw, cabin temperature.'],
      ]} />
      {sub === 'health' ? <BatteryTab /> : <Charts />}
    </div>
  );
}
