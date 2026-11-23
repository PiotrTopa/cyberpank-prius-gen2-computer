import { useState } from 'react';
import type { TimeRange } from '../types';
import { useHistory } from '../hooks/useHistory';
import { TelemetryTab } from './TelemetryTab';
import { BatteryTab } from './BatteryTab';

/** Charts and battery health: everything that needs history queries lives here,
 *  so nothing polls /metrics while another tab is open. */
export default function HistoryTab() {
  const [timeRange, setTimeRange] = useState<TimeRange>('24h');
  const hist = useHistory(timeRange);
  return (
    <div className="flex flex-col gap-4">
      <TelemetryTab hist={hist} timeRange={timeRange} onTimeRange={setTimeRange} />
      <BatteryTab />
    </div>
  );
}
