import { useEffect, useState } from 'react';
import { API_URL } from '../lib/api';

/** Signed W: stack > 0 = drawn from the 12 V battery, < 0 = net solar charge. */
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
  /** net Wh over 24 h (+ drawn from the battery, − net solar charge) */
  energy_24h_wh: number | null;
  consumed_24h_wh: number | null;
  charged_24h_wh: number | null;
  /** [epoch s, W] signed stack power, 15-minute buckets over 24 h */
  power_24h: [number, number][];
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
