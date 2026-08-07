export const WS_URL = import.meta.env.DEV
  ? 'ws://10.200.0.5:8080/api/v1/stream'
  : `ws://${window.location.host}/api/v1/stream`;

export const API_URL = import.meta.env.DEV ? 'http://10.200.0.5:8080/api/v1' : `/api/v1`;

import { toast } from './toast';

export async function sendCommand(name: string, params: Record<string, unknown> = {}): Promise<boolean> {
  try {
    const res = await fetch(`${API_URL}/commands/${name}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      toast(`${name} failed: ${err.detail || res.statusText}`, 'red', 6000);
      return false;
    }
    toast(`${name} accepted`, 'green', 2500);
    return true;
  } catch (e) {
    toast(`${name} failed: ${e instanceof Error ? e.message : String(e)}`, 'red', 6000);
    return false;
  }
}
