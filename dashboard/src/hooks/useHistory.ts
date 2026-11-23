import { useCallback, useEffect, useState } from 'react';
import type { Series, TimeRange } from '../types';
import { API_URL } from '../lib/api';

interface MetricPoint {
  t: number;
  avg: number;
}

export interface HistoryData {
  voltageHistory: Series;
  tempHistory: Series;
  currentHistory: Series;
  pocoHistory: Series;
}

const EMPTY: HistoryData = {
  voltageHistory: [],
  tempHistory: [],
  currentHistory: [],
  pocoHistory: [],
};

/** Polls /metrics for the History tab's chart series, refreshed every 60 s while mounted. */
export function useHistory(timeRange: TimeRange): HistoryData {
  const [data, setData] = useState<HistoryData>(EMPTY);

  const fetchHistory = useCallback(async () => {
    try {
      const nowS = Math.floor(Date.now() / 1000);
      const fromS = timeRange === '1h' ? nowS - 3600 : timeRange === '24h' ? nowS - 86400 : nowS - 7 * 86400;
      const fromStr = `&from=${fromS}`;

      const [voltRes, tempRes, pbPowerRes, pocoPowerRes] = await Promise.all([
        fetch(`${API_URL}/metrics?signal=powerbox_voltage&res=auto${fromStr}`),
        fetch(`${API_URL}/metrics?signal=powerbox_bmp2_t&res=auto${fromStr}`),
        fetch(`${API_URL}/metrics?signal=powerbox_power&res=auto${fromStr}`),
        fetch(`${API_URL}/metrics?signal=poco_power&res=auto${fromStr}`),
      ]);

      const formatTime = (ts: number) => {
        const d = new Date(ts * 1000);
        if (timeRange === '7d') return d.toLocaleDateString([], { month: 'short', day: 'numeric', hour: '2-digit' });
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      };

      // Draw is plotted as a magnitude: the INA219 sign depends on its wiring.
      const series = async (res: Response, digits: number, abs = false): Promise<Series> => {
        if (!res.ok) return [];
        const d = await res.json();
        return d.points.map((p: MetricPoint) => ({
          time: formatTime(p.t), value: Number((abs ? Math.abs(p.avg) : p.avg).toFixed(digits)),
        }));
      };

      const [voltageHistory, tempHistory, currentHistory, pocoHistory] = await Promise.all([
        series(voltRes, 2), series(tempRes, 1), series(pbPowerRes, 2, true), series(pocoPowerRes, 2, true),
      ]);
      setData({ voltageHistory, tempHistory, currentHistory, pocoHistory });
    } catch (e) {
      console.error('Failed to fetch history', e);
    }
  }, [timeRange]);

  useEffect(() => {
    const first = setTimeout(fetchHistory, 0);
    const interval = setInterval(fetchHistory, 60000);
    return () => {
      clearTimeout(first);
      clearInterval(interval);
    };
  }, [fetchHistory]);

  return data;
}
