import { Clock, Cpu, Thermometer, Zap } from 'lucide-react';
import type { TimeRange } from '../types';
import type { HistoryData } from '../hooks/useHistory';
import { cx } from '../lib/format';
import { Chart } from '../components/Chart';

const RANGES: TimeRange[] = ['1h', '24h', '7d'];

export function TelemetryTab({ hist, timeRange, onTimeRange }: {
  hist: HistoryData;
  timeRange: TimeRange;
  onTimeRange: (tr: TimeRange) => void;
}) {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-1 p-1 hud-panel w-fit">
        <Clock className="text-slate-600 ml-2 mr-1" size={13} />
        {RANGES.map((tr) => (
          <button
            key={tr}
            onClick={() => onTimeRange(tr)}
            className={cx(
              'px-3.5 py-1 text-[0.7rem] uppercase tracking-wider transition-colors',
              timeRange === tr ? 'text-hud-cyan border border-hud-cyan/50 bg-white/[0.03]' : 'text-slate-500 hover:text-slate-200 border border-transparent'
            )}
          >
            {tr}
          </button>
        ))}
      </div>
      <Chart title="Aux Battery 12V" icon={Zap} data={hist.voltageHistory} color="#6ee7f2" unit="V" />
      <Chart title="Stack Draw" icon={Cpu} data={hist.currentHistory} color="#ffb454" unit="W" />
      <Chart title="POCO Draw" icon={Cpu} data={hist.pocoHistory} color="#ffb454" unit="W" />
      <Chart title="Cabin Temperature" icon={Thermometer} data={hist.tempHistory} color="#e879c8" unit="°C" />
    </div>
  );
}
