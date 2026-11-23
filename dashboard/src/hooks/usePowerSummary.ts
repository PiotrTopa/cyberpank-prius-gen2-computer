import { useEffect, useState } from 'react';
import { API_URL } from '../lib/api';

export interface PowerAvgs {
  avg_1m: number | null;
  avg_5m: number | null;
  avg_1h: number | null;
  avg_24h: number | null;
}

export interface PowerSummary {
  ts: number;
  stack: PowerAvgs;
  poco: PowerAvgs;
  energy_24h_wh: number | null;
  /** [epoch s, V] 15-minute buckets over 24 h */
  voltage_24h: [number, number][];
  voltage_min_24h: number | null;
}

/** GET /summary/power once a minute while mounted (and the page is visible). */
export function usePowerSummary(): PowerSummary | null {
  const [data, setData] = useState<PowerSummary | null>(null);
  useEffect(() => {
    let alive = true;
    const load = async () => {
      if (document.hidden) return;
      try {
        const res = await fetch(`${API_URL}/summary/power`);
        if (res.ok && alive) setData(await res.json());
      } catch {
        // offline: keep the last summary
      }
    };
    void load();
    const t = setInterval(load, 60_000);
    const onVis = () => { if (!document.hidden) void load(); };
    document.addEventListener('visibilitychange', onVis);
    return () => {
      alive = false;
      clearInterval(t);
      document.removeEventListener('visibilitychange', onVis);
    };
  }, []);
  return data;
}
