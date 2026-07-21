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
  avgPower1m: number | null;
  avgPower5m: number | null;
  avgPower1h: number | null;
  avgPocoPower1m: number | null;
  avgPocoPower5m: number | null;
  avgPocoPower1h: number | null;
}

const EMPTY: HistoryData = {
  voltageHistory: [],
  tempHistory: [],
  currentHistory: [],
  pocoHistory: [],
  avgPower1m: null,
  avgPower5m: null,
  avgPower1h: null,
  avgPocoPower1m: null,
  avgPocoPower5m: null,
  avgPocoPower1h: null,
};

async function calcAverages(res1h: Response, res5m: Response, nowS: number) {
  let avg1h = null, avg5m = null, avg1m = null;
  if (res1h.ok) {
    const data = await res1h.json();
    if (data.points?.length > 0) {
      const sum = data.points.reduce((acc: number, p: MetricPoint) => acc + p.avg, 0);
      avg1h = Number((sum / data.points.length).toFixed(2));
    }
  }
  if (res5m.ok) {
    const data = await res5m.json();
    if (data.points?.length > 0) {
      const sum5m = data.points.reduce((acc: number, p: MetricPoint) => acc + p.avg, 0);
      avg5m = Number((sum5m / data.points.length).toFixed(2));
      const points1m = data.points.filter((p: MetricPoint) => p.t >= nowS - 60);
      if (points1m.length > 0) {
        const sum1m = points1m.reduce((acc: number, p: MetricPoint) => acc + p.avg, 0);
        avg1m = Number((sum1m / points1m.length).toFixed(2));
      }
    }
  }
  return { avg1h, avg5m, avg1m };
}

/** Polls /metrics for chart series + rolling power averages, refreshed every 60 s. */
export function useHistory(timeRange: TimeRange): HistoryData {
  const [data, setData] = useState<HistoryData>(EMPTY);

  const fetchHistory = useCallback(async () => {
    try {
      const nowS = Math.floor(Date.now() / 1000);
      const fromS = timeRange === '1h' ? nowS - 3600 : timeRange === '24h' ? nowS - 86400 : nowS - 7 * 86400;
      const fromStr = `&from=${fromS}`;

      const [voltRes, tempRes, pbPowerRes, pb1h, pb5m, pocoPowerRes, poco1h, poco5m] = await Promise.all([
        fetch(`${API_URL}/metrics?signal=powerbox_voltage&res=auto${fromStr}`),
        fetch(`${API_URL}/metrics?signal=powerbox_aht_t&res=auto${fromStr}`),
        fetch(`${API_URL}/metrics?signal=powerbox_power&res=auto${fromStr}`),
        fetch(`${API_URL}/metrics?signal=powerbox_power&res=1m&from=${nowS - 3600}`),
        fetch(`${API_URL}/metrics?signal=powerbox_power&res=raw&from=${nowS - 300}`),
        fetch(`${API_URL}/metrics?signal=poco_power&res=auto${fromStr}`),
        fetch(`${API_URL}/metrics?signal=poco_power&res=1m&from=${nowS - 3600}`),
        fetch(`${API_URL}/metrics?signal=poco_power&res=raw&from=${nowS - 300}`),
      ]);

      const formatTime = (ts: number) => {
        const d = new Date(ts * 1000);
        if (timeRange === '7d') return d.toLocaleDateString([], { month: 'short', day: 'numeric', hour: '2-digit' });
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      };

      const series = async (res: Response, digits: number): Promise<Series> => {
        if (!res.ok) return [];
        const d = await res.json();
        return d.points.map((p: MetricPoint) => ({ time: formatTime(p.t), value: Number(p.avg.toFixed(digits)) }));
      };

      const [voltageHistory, tempHistory, currentHistory, pocoHistory] = await Promise.all([
        series(voltRes, 2), series(tempRes, 1), series(pbPowerRes, 2), series(pocoPowerRes, 2),
      ]);
      const pbAvgs = await calcAverages(pb1h, pb5m, nowS);
      const pocoAvgs = await calcAverages(poco1h, poco5m, nowS);

      setData({
        voltageHistory, tempHistory, currentHistory, pocoHistory,
        avgPower1m: pbAvgs.avg1m, avgPower5m: pbAvgs.avg5m, avgPower1h: pbAvgs.avg1h,
        avgPocoPower1m: pocoAvgs.avg1m, avgPocoPower5m: pocoAvgs.avg5m, avgPocoPower1h: pocoAvgs.avg1h,
      });
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
