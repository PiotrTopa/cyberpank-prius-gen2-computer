import { useEffect, useState } from 'react';
import type { AppState } from '../types';
import { WS_URL } from '../lib/api';

/** Live AppState over WebSocket with auto-reconnect. */
export function useAppStream(): { state: AppState | null; connected: boolean } {
  const [state, setState] = useState<AppState | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let ws: WebSocket;
    let reconnectTimer: number;
    const connect = () => {
      ws = new WebSocket(WS_URL);
      ws.onopen = () => setConnected(true);
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.state) setState(data.state);
        } catch (e) {
          console.error('Failed to parse message', e);
        }
      };
      ws.onclose = () => {
        setConnected(false);
        reconnectTimer = window.setTimeout(connect, 3000);
      };
    };
    connect();
    return () => {
      clearTimeout(reconnectTimer);
      if (ws) ws.close();
    };
  }, []);

  return { state, connected };
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
