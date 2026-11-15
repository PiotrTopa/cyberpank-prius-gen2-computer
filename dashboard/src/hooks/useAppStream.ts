import { useEffect, useState } from 'react';
import type { AppState } from '../types';
import { WS_URL } from '../lib/api';

/**
 * Live AppState over WebSocket with auto-reconnect.
 *
 * Also tracks `clockOffsetMs` = viewer clock − backend clock, sampled from the
 * `ts` field of each stream message. All timestamps in the state come from the
 * POCO, which has no reliable RTC/NTP in the car — ages must be computed
 * against `Date.now() - clockOffsetMs`, never the raw viewer clock, or
 * telemetry looks stale/fresh by however far the clocks have drifted apart.
 */
export function useAppStream(): { state: AppState | null; connected: boolean; clockOffsetMs: number } {
  const [state, setState] = useState<AppState | null>(null);
  const [connected, setConnected] = useState(false);
  const [clockOffsetMs, setClockOffsetMs] = useState(0);

  useEffect(() => {
    let ws: WebSocket;
    let reconnectTimer: number;
    const connect = () => {
      ws = new WebSocket(WS_URL);
      ws.onopen = () => setConnected(true);
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (typeof data.ts === 'number') {
            const off = Date.now() - data.ts * 1000;
            // Re-render only on meaningful drift, not per-message network jitter.
            setClockOffsetMs((prev) => (Math.abs(off - prev) > 1000 ? off : prev));
          }
          if (data.state) setState(data.state);
        } catch (e) {
          console.error('Failed to parse message', e);
        }
      };
      ws.onclose = () => {
        setConnected(false);
        // A refused handshake looks like a network drop to the browser: ask
        // the backend whether this browser's session is gone (revoked/expired)
        // and go to the login page instead of retrying forever.
        if (!import.meta.env.DEV) void checkSession();
        reconnectTimer = window.setTimeout(connect, 3000);
      };
    };
    connect();
    return () => {
      clearTimeout(reconnectTimer);
      if (ws) ws.close();
    };
  }, []);

  return { state, connected, clockOffsetMs };
}

async function checkSession(): Promise<void> {
  try {
    const res = await fetch('/auth/status', { cache: 'no-store' });
    if (!res.ok) return;
    const st = await res.json();
    if (st.auth && !st.authenticated) {
      const next = window.location.pathname + window.location.hash;
      window.location.assign(`/login?next=${encodeURIComponent(next)}`);
    }
  } catch {
    // backend unreachable: keep retrying the stream
  }
}

/** 1 Hz wall clock for freshness / age readouts. */
export function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  return now;
}
