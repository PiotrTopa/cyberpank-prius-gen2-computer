import { useEffect, useRef, useState } from 'react';
import type { AppState } from '../types';
import { WS_URL } from '../lib/api';
import { applyMergePatch } from '../lib/mergePatch';

/** What the current view needs from the state feed (backend api.stream_filter). */
export interface StreamSub {
  /** top-level state sections; null = everything (debug drawer) */
  keys: string[] | null;
  /** minimum seconds between messages (1..60): with the key on / parked */
  interval: { acc: number; parked: number };
}

/** The resolved form sent to the backend. */
interface WireSub {
  keys: string[] | null;
  interval: number;
}

/** After a command, stream at 1 s for this long so its effect shows quickly. */
const BOOST_MS = 30_000;
/** Close the socket after the page has been hidden this long (no LTE use in a pocket). */
const HIDDEN_CLOSE_MS = 20_000;

const subKey = (s: WireSub) => `${s.keys ? [...s.keys].sort().join(',') : '*'}|${s.interval}`;

/**
 * Live AppState over WebSocket with auto-reconnect.
 *
 * The socket subscribes to only the sections the current view shows, at the
 * view's cadence, as merge-patch deltas (`delta=1`): a parked-car home view
 * costs well under 1 KB/s instead of the full ~30 KB/s snapshot feed. A
 * changed subscription is sent over the open socket (the backend answers with
 * a full resync of the new sections). The socket closes while the page is
 * hidden and reopens when it is visible again.
 *
 * Also tracks `clockOffsetMs` = viewer clock − backend clock, sampled from the
 * `ts` field of each stream message. All timestamps in the state come from the
 * POCO, which has no reliable RTC/NTP in the car — ages must be computed
 * against `Date.now() - clockOffsetMs`, never the raw viewer clock.
 */
export function useAppStream(sub: StreamSub): { state: AppState | null; connected: boolean; clockOffsetMs: number } {
  const [state, setState] = useState<AppState | null>(null);
  const [connected, setConnected] = useState(false);
  const [clockOffsetMs, setClockOffsetMs] = useState(0);
  const [boosted, setBoosted] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  const acc = !!(state?.powerbox?.acc_on ?? state?.vehicle?.acc_on);
  const effective: WireSub = { keys: sub.keys, interval: boosted ? 1 : acc ? sub.interval.acc : sub.interval.parked };
  const effKey = subKey(effective);
  const subRef = useRef(effective);
  useEffect(() => { subRef.current = effective; });

  // Command boost: lib/api dispatches `prius-command` after each accepted command.
  useEffect(() => {
    let t: number | undefined;
    const onCmd = () => {
      setBoosted(true);
      clearTimeout(t);
      t = window.setTimeout(() => setBoosted(false), BOOST_MS);
    };
    window.addEventListener('prius-command', onCmd);
    return () => {
      clearTimeout(t);
      window.removeEventListener('prius-command', onCmd);
    };
  }, []);

  // Re-subscribe on the open socket when the view's needs change.
  useEffect(() => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      const s = subRef.current;
      ws.send(JSON.stringify({ type: 'subscribe', keys: s.keys ?? '*', interval: s.interval, delta: true }));
    }
  }, [effKey]);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let hiddenTimer: number | undefined;
    let stopped = false;

    const url = () => {
      const s = subRef.current;
      const q = new URLSearchParams({ keys: s.keys ? s.keys.join(',') : '*', interval: String(s.interval), delta: '1' });
      return `${WS_URL}?${q.toString()}`;
    };

    const connect = () => {
      if (stopped || ws) return;
      const sock = new WebSocket(url());
      ws = sock;
      wsRef.current = sock;
      sock.onopen = () => setConnected(true);
      sock.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type !== 'state') return;
          if (typeof data.ts === 'number') {
            const off = Date.now() - data.ts * 1000;
            // Re-render only on meaningful drift, not per-message network jitter.
            setClockOffsetMs((prev) => (Math.abs(off - prev) > 1000 ? off : prev));
          }
          if (data.state) {
            // A full (re)sync replaces only the sections it carries: sections of
            // the previous view stay as last seen instead of blanking.
            setState((prev) => ({ ...(prev ?? {}), ...data.state } as AppState));
          } else if (data.patch && Object.keys(data.patch).length) {
            setState((prev) => (prev ? (applyMergePatch(prev, data.patch) as AppState) : prev));
          }
        } catch (e) {
          console.error('Failed to parse message', e);
        }
      };
      sock.onclose = () => {
        if (wsRef.current === sock) wsRef.current = null;
        ws = null;
        setConnected(false);
        if (stopped || document.hidden) return;
        // A refused handshake looks like a network drop to the browser: ask
        // the backend whether this browser's session is gone (revoked/expired)
        // and go to the login page instead of retrying forever.
        if (!import.meta.env.DEV) void checkSession();
        reconnectTimer = window.setTimeout(connect, 3000);
      };
    };

    const onVisibility = () => {
      clearTimeout(hiddenTimer);
      if (document.hidden) {
        hiddenTimer = window.setTimeout(() => {
          clearTimeout(reconnectTimer);
          ws?.close();
        }, HIDDEN_CLOSE_MS);
      } else if (!ws) {
        clearTimeout(reconnectTimer);
        connect();
      }
    };

    connect();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      stopped = true;
      clearTimeout(reconnectTimer);
      clearTimeout(hiddenTimer);
      document.removeEventListener('visibilitychange', onVisibility);
      ws?.close();
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
