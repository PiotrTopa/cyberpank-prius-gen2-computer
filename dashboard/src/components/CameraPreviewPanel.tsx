import { useEffect, useRef, useState } from 'react';
import { Camera, Square, Play } from 'lucide-react';
import { API_URL } from '../lib/api';
import { cx } from '../lib/format';
import {
  canAutoResume, clearMarker, fmtMmSs, frameOverlay, readMarker, writeMarker, type StorageLike,
} from '../lib/cameraPreview';
import { TONE_TEXT } from '../lib/tones';
import type { CameraPreview } from '../types';
import { Btn, DataRow, Panel, type Tone } from './ui';

/**
 * On-demand camera preview (backend.camera_preview).
 *
 * Nothing is fetched until START. While previewing, a lease is renewed every
 * ttl/3 and the latest frame is polled one request at a time at an adaptive
 * rate (1 fps, slower when frames take long), downscaled on the server. The
 * preview stops — and the lease is released — on STOP, on unmount, as soon
 * as the page is hidden (Page Visibility API), on pagehide (best-effort
 * sendBeacon), and at the server's maximum session length. The server-side
 * lease TTL (15 s) is the real guarantee; these are the fast path.
 *
 * Only STOP is a hard stop (`?stop=1`: camera off at once). Every other end
 * is soft: the server keeps the camera on for its grace (3 min) and this
 * panel resumes by itself (lease with `resume: true`) when the tab is visible
 * again or the page is reloaded / re-opened — only if this browser was
 * previewing (localStorage marker) and the server still reports the grace or
 * an active preview. A cold camera is never started without a button press.
 */

const BASE = `${API_URL}/camera/preview`;
const MIN_INTERVAL_MS = 1000;
const MAX_INTERVAL_MS = 5000;

type Phase = 'off' | 'requesting' | 'powering' | 'enumerating' | 'waiting_for_frame' | 'live' | 'error' | 'grace';

function storage(): StorageLike | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

const PHASE_LABEL: Record<string, string> = {
  off: 'OFF',
  requesting: 'REQUESTING',
  powering: 'POWERING CAMERA',
  enumerating: 'CAMERA ENUMERATING',
  waiting_for_frame: 'WAITING FOR FRAME',
  live: 'LIVE',
  error: 'ERROR',
  grace: 'PAUSED · CAMERA KEPT ON',
};

const PHASE_TONE: Record<string, Tone> = {
  off: 'dim', requesting: 'amber', powering: 'amber', enumerating: 'amber',
  waiting_for_frame: 'amber', live: 'green', error: 'red', grace: 'amber',
};

interface LeaseReply {
  lease_id: string;
  ttl_s: number;
  max_session_s: number;
  session_left_s: number;
  phase?: string;
  reason?: string;
  source?: string | null;
}

interface FrameInfo {
  url: string;
  time: number;     // server wall clock of the capture (s)
  source: string;
  bytes: number;
  width: number | null;   // null = served full size (no downscaling on the server)
}

function releaseUrl(id: string) {
  return `${BASE}/lease/${encodeURIComponent(id)}/release`;
}

/** Best-effort release that survives page unload. ``stop`` = user STOP
 *  (camera off now); otherwise soft (the server grace keeps the camera). */
function releaseNow(id: string, unloading: boolean, stop = false) {
  if (unloading && !stop && typeof navigator.sendBeacon === 'function') {
    if (navigator.sendBeacon(releaseUrl(id))) return;
  }
  fetch(`${BASE}/lease/${encodeURIComponent(id)}${stop ? '?stop=1' : ''}`,
    { method: 'DELETE', keepalive: true }).catch(() => {});
}

async function detailOf(res: Response): Promise<{ reason?: string; message?: string; phase?: string }> {
  const body = await res.json().catch(() => ({}));
  const d = (body as { detail?: unknown }).detail;
  return typeof d === 'object' && d !== null ? (d as Record<string, string>) : { message: String(d ?? res.statusText) };
}

export function CameraPreviewPanel({ server, now }: { server?: CameraPreview | null; now: number }) {
  const [on, setOn] = useState(false);
  const [phase, setPhase] = useState<Phase>('off');
  const [reason, setReason] = useState('');
  const [frame, setFrame] = useState<FrameInfo | null>(null);
  const [interval, setIntervalMs] = useState(MIN_INTERVAL_MS);
  const [ended, setEnded] = useState('');
  const [sessionEnd, setSessionEnd] = useState<number | null>(null);   // local ms
  const leaseRef = useRef<string | null>(null);
  const resumeRef = useRef(false);      // first lease of this run continues a viewing
  const hardStopRef = useRef(false);    // the cleanup sends ?stop=1
  const onRef = useRef(false);

  const start = (resume = false) => {
    resumeRef.current = resume;
    hardStopRef.current = false;
    setEnded('');
    setReason('');
    setPhase('requesting');
    setOn(true);
  };
  const stop = (why: string) => {
    hardStopRef.current = true;
    clearMarker(storage());
    setOn(false);
    setEnded(why);
  };
  const turnOffNow = () => {
    const m = readMarker(storage());
    clearMarker(storage());
    releaseNow(m?.leaseId ?? '0', false, true);
    setEnded('stopped');
  };

  useEffect(() => {
    onRef.current = on;
  }, [on]);

  // Auto-resume: on mount (reload / re-open / back to this tab of the app)
  // and when the browser tab becomes visible again.
  useEffect(() => {
    let cancelled = false;
    const tryResume = async () => {
      if (onRef.current || document.visibilityState !== 'visible') return;
      const marker = readMarker(storage());
      if (!marker) return;
      try {
        const res = await fetch(BASE, { cache: 'no-store' });
        if (!res.ok || cancelled || onRef.current) return;
        const st = (await res.json()) as CameraPreview;
        if (cancelled || onRef.current) return;
        // No clearing on "off": the marker ages out by itself (MARKER_MAX_AGE_MS).
        if (canAutoResume(marker, st, Date.now())) start(true);
      } catch {
        /* offline: the user can press START */
      }
    };
    const onVisible = () => {
      if (document.visibilityState === 'visible') tryResume();
    };
    tryResume();
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('pageshow', onVisible);
    return () => {
      cancelled = true;
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('pageshow', onVisible);
    };
  }, []);

  useEffect(() => {
    if (!on) return;
    let alive = true;
    const ctrl = new AbortController();
    const timers: number[] = [];
    const later = (fn: () => void, ms: number) => { timers.push(window.setTimeout(fn, ms)); };
    let ttlMs = 15_000;
    let sessionLeftMs = 600_000;
    let etag: string | null = null;
    let width = 640;
    let objectUrl: string | null = null;

    const end = (why: string) => {
      if (!alive) return;
      alive = false;
      setOn(false);
      setEnded(why);
    };

    async function lease(): Promise<boolean> {
      const id = leaseRef.current;
      const resume = !id && resumeRef.current;
      try {
        const res = await fetch(`${BASE}/lease`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(id ? { lease_id: id } : resume ? { resume: true } : {}),
          signal: ctrl.signal,
        });
        if (!alive) return false;
        if (res.ok) {
          const d = (await res.json()) as LeaseReply;
          if (!alive) return false;
          leaseRef.current = d.lease_id;
          resumeRef.current = true;     // any later new lease continues this viewing
          ttlMs = Math.max(3000, d.ttl_s * 1000);
          sessionLeftMs = d.session_left_s * 1000;
          writeMarker(storage(), { t: Date.now(), leaseId: d.lease_id });
          if (!id) setSessionEnd(Date.now() + sessionLeftMs);
          if (d.phase && d.phase !== 'live') setPhase(d.phase as Phase);
          return true;
        }
        const d = await detailOf(res);
        if (res.status === 410 && d.reason === 'expired') {
          // The lease lapsed (network gap): continue the viewing silently.
          leaseRef.current = null;
          resumeRef.current = true;
          return lease();
        }
        leaseRef.current = null;
        clearMarker(storage());
        end(d.reason === 'max_session' ? 'maximum session length reached'
          : d.reason === 'undervoltage' ? 'refused: 12 V under-voltage protection'
          : d.reason === 'not_resumable' ? ''
          : d.message || `lease refused (${res.status})`);
        return false;
      } catch {
        return alive;   // network hiccup: keep trying; the server TTL is the guard
      }
    }

    async function renewLoop() {
      if (!alive) return;
      await lease();
      if (alive) later(renewLoop, Math.max(2000, ttlMs / 3));
    }

    async function frameLoop() {
      if (!alive) return;
      const t0 = performance.now();
      let next: number;
      try {
        const id = leaseRef.current;
        const res = await fetch(`${BASE}/frame.jpg?w=${width}${id ? `&lease=${id}` : ''}`, {
          headers: etag ? { 'If-None-Match': etag } : {},
          signal: ctrl.signal,
          cache: 'no-store',
        });
        if (!alive) return;
        const took = performance.now() - t0;
        if (res.status === 200) {
          const blob = await res.blob();
          if (!alive) return;
          etag = res.headers.get('ETag');
          if (objectUrl) URL.revokeObjectURL(objectUrl);
          objectUrl = URL.createObjectURL(blob);
          setFrame({
            url: objectUrl,
            time: Number(res.headers.get('X-Frame-Time') ?? 0),
            source: res.headers.get('X-Frame-Source') ?? '',
            bytes: blob.size,
            width: res.headers.get('X-Preview-Scaled') === '1' ? width : null,
          });
          setPhase('live');
          setReason('');
          // Back off when the link is slow; shrink the picture as well.
          const total = performance.now() - t0;
          next = Math.min(MAX_INTERVAL_MS, Math.max(MIN_INTERVAL_MS, total * 2));
          width = total > 3000 ? 320 : total > 1500 ? 480 : 640;
        } else if (res.status === 304) {
          next = Math.min(MAX_INTERVAL_MS, Math.max(MIN_INTERVAL_MS, took * 2) * 1.5);
        } else {
          const d = await detailOf(res);
          if (!alive) return;
          if (d.phase) setPhase(d.phase as Phase);
          setReason(d.reason || d.message || '');
          next = res.status === 409 ? 1500 : MIN_INTERVAL_MS;
        }
      } catch {
        if (!alive) return;
        next = 3000;
      }
      setIntervalMs(Math.round(next));
      if (alive) later(frameLoop, next);
    }

    // Soft ends: the marker stays, the server grace keeps the camera.
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') end('paused (page hidden)');
    };
    const onPageHide = () => {
      const id = leaseRef.current;
      if (id) releaseNow(id, true);
      leaseRef.current = null;
      end('paused (page closed)');
    };
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('pagehide', onPageHide);

    (async () => {
      if (await lease()) {
        later(renewLoop, Math.max(2000, ttlMs / 3));
        frameLoop();
        later(() => {
          clearMarker(storage());
          end('maximum session length reached');
        }, sessionLeftMs);
      }
    })();

    return () => {
      alive = false;
      ctrl.abort();
      timers.forEach((t) => window.clearTimeout(t));
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('pagehide', onPageHide);
      const id = leaseRef.current;
      leaseRef.current = null;
      if (id) releaseNow(id, false, hardStopRef.current);
      hardStopRef.current = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      setFrame(null);
      setPhase('off');
      setSessionEnd(null);
      setIntervalMs(MIN_INTERVAL_MS);
    };
  }, [on]);

  const shown = on ? phase : server?.phase === 'grace' ? 'grace' : 'off';
  const tone = PHASE_TONE[shown] ?? 'dim';
  const age = frame && frame.time > 0 ? Math.max(0, now / 1000 - frame.time) : null;
  const left = sessionEnd != null ? Math.max(0, Math.round((sessionEnd - now) / 1000)) : null;
  const others = !on && server?.active ? server.leases ?? 0 : 0;
  const overlay = frameOverlay(phase, age);
  const graceLeft = !on && server?.phase === 'grace' ? server.grace_left_s ?? null : null;

  return (
    <Panel title="Camera" code="CAM-01" icon={Camera}
      right={<span className={cx('text-[0.65rem] tracking-wider', TONE_TEXT[tone])}>
        {PHASE_LABEL[shown] ?? shown.toUpperCase()}
      </span>}>
      {on ? (
        <>
          <div className="relative aspect-video bg-ink-900 border border-ink-700 flex items-center justify-center overflow-hidden">
            {frame
              ? <img src={frame.url} alt="camera preview"
                  className={cx('w-full h-full object-contain transition-[filter,opacity]',
                    overlay.stale && 'grayscale opacity-40')} />
              : <span className="text-[0.65rem] text-slate-500 uppercase tracking-wider px-3 text-center">
                  {PHASE_LABEL[phase] ?? phase}{reason ? ` — ${reason}` : ''}
                </span>}
            {frame && overlay.stale && (
              <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 px-3 text-center">
                <span className="bg-ink-900/85 px-2 py-1 text-[0.7rem] text-hud-amber uppercase tracking-wider">
                  {overlay.text}
                </span>
                {reason && <span className="bg-ink-900/85 px-2 text-[0.6rem] text-slate-400">{reason}</span>}
              </div>
            )}
          </div>
          <DataRow label="Frame Age" value={age != null ? age.toFixed(1) : undefined} unit="s"
            tone={age != null && age > 5 ? 'amber' : 'cyan'} />
          <DataRow label="Refresh" value={(interval / 1000).toFixed(1)} unit="s" />
          <DataRow label="Source / Size"
            value={frame ? `${frame.source} · ${frame.width ? `${frame.width}px` : 'full'} · ${(frame.bytes / 1024).toFixed(0)} KB` : undefined} />
          <DataRow label="Session Left" value={left != null ? `${Math.floor(left / 60)}m ${left % 60}s` : undefined} />
          <Btn tone="red" active onClick={() => stop('stopped')}>
            <span className="flex items-center justify-center gap-2"><Square size={12} /> Stop preview</span>
          </Btn>
        </>
      ) : (
        <>
          <p className="text-[0.65rem] text-slate-500 leading-relaxed">
            Live still from the road camera (~1 frame/s, downscaled). With the car off this powers
            the camera hub (relay ch4) only while you watch. Leaving or hiding the page pauses it and
            keeps the camera on for {fmtMmSs(server?.grace_s ?? 180)} so it comes back at once; STOP turns it off.
          </p>
          {graceLeft != null && (
            <div className="flex items-center justify-between gap-2 border border-hud-amber/40 px-2 py-1.5">
              <span className="text-[0.65rem] text-hud-amber uppercase tracking-wider">
                Paused — camera kept on for {fmtMmSs(graceLeft)}
              </span>
              <span className="flex gap-2">
                <Btn tone="cyan" onClick={() => start(true)} className="px-2 py-1">Resume</Btn>
                <Btn tone="red" onClick={turnOffNow} className="px-2 py-1">Off now</Btn>
              </span>
            </div>
          )}
          {ended && <p className="text-[0.65rem] text-slate-400 uppercase tracking-wider">Last session: {ended}</p>}
          {server?.phase === 'error' && server.reason && (
            <p className="text-[0.65rem] text-hud-red uppercase tracking-wider">{server.reason}</p>
          )}
          {others > 0 && (
            <p className="text-[0.65rem] text-hud-amber uppercase tracking-wider">
              {others} other viewer{others > 1 ? 's' : ''} watching ({server?.source ?? '—'})
            </p>
          )}
          <Btn tone="cyan" onClick={() => start(false)} disabled={server?.enabled === false}>
            <span className="flex items-center justify-center gap-2"><Play size={12} /> Start camera preview</span>
          </Btn>
        </>
      )}
    </Panel>
  );
}
