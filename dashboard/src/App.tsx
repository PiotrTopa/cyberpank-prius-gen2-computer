import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { Bug, Car, Gauge, History, Home, Settings, Stethoscope, Wifi, WifiOff } from 'lucide-react';
import type { Satellites, Tab } from './types';
import { cx } from './lib/format';
import { useAppStream, useNow, type StreamSub } from './hooks/useAppStream';
import { usePowerSummary } from './hooks/usePowerSummary';
import { Chip, Toasts } from './components/ui';
import { HomeTab } from './tabs/HomeTab';

// Everything but Home loads on demand: charts (recharts), the map and the
// diagnostics tables stay out of the first download on a phone.
const DriveTab = lazy(() => import('./tabs/DriveTab'));
const HistoryTab = lazy(() => import('./tabs/HistoryTab'));
const CarTab = lazy(() => import('./tabs/CarTab'));
const SystemTab = lazy(() => import('./tabs/SystemTab'));
const DebugDrawer = lazy(() => import('./components/DebugDrawer').then((m) => ({ default: m.DebugDrawer })));

const TABS: [Tab, string, typeof Home][] = [
  ['home', 'Home', Home],
  ['drive', 'Drive', Gauge],
  ['history', 'History', History],
  ['car', 'Car', Stethoscope],
  ['system', 'System', Settings],
];

/** Old hash links (#map, #controls, ...) land on the tab that now holds them. */
const LEGACY: Record<string, Tab> = {
  dashboard: 'home', map: 'drive', charts: 'history', battery: 'history',
  diagnostics: 'car', alerts: 'car', controls: 'system', satellites: 'system',
};

/** State sections each view reads (header always needs powerbox + connection). */
const KEYS: Record<Tab, string[]> = {
  home: ['powerbox', 'connection', 'hosts', 'poco2', 'roadsense', 'satellites', 'headunit', 'climate', 'vehicle'],
  drive: ['powerbox', 'connection', 'vehicle', 'energy', 'dynamics', 'roadsense', 'climate', 'vision'],
  history: ['powerbox', 'connection', 'vehicle'],
  car: ['powerbox', 'connection', 'vehicle', 'diagnostics', 'fuel_log'],
  system: ['powerbox', 'connection', 'satellites', 'poco2', 'hosts', 'roadsense'],
};

function tabFromHash(): Tab {
  const h = window.location.hash.replace('#', '').split('/')[0];
  if (TABS.some(([id]) => id === h)) return h as Tab;
  return LEGACY[h] ?? 'home';
}

const Loading = () => (
  <div className="py-16 flex justify-center">
    <div className="animate-spin h-6 w-6 border-t border-b border-hud-cyan rounded-full" />
  </div>
);

export default function App() {
  const [activeTab, setActiveTabState] = useState<Tab>(tabFromHash);
  const [debugOpen, setDebugOpen] = useState(false);

  // Cadence: 1 s where values move (driving, debug), 2 s with the key on,
  // 5 s for a parked car. Commands boost to 1 s for a while (useAppStream).
  const sub: StreamSub = useMemo(() => ({
    keys: debugOpen ? null : KEYS[activeTab],
    interval: debugOpen ? { acc: 1, parked: 1 } : activeTab === 'drive' ? { acc: 1, parked: 5 } : { acc: 2, parked: 5 },
  }), [activeTab, debugOpen]);

  const { state, connected, clockOffsetMs } = useAppStream(sub);
  const now = useNow();
  const sum = usePowerSummary();
  // Backend-clock "now" — the POCO has no reliable RTC/NTP, so ages computed
  // against the raw viewer clock drift by whatever the skew happens to be.
  const serverNow = now - clockOffsetMs;


  const setActiveTab = (t: Tab) => {
    setActiveTabState(t);
    window.history.replaceState(null, '', t === 'home' ? window.location.pathname : `#${t}`);
    window.scrollTo({ top: 0 });
  };

  useEffect(() => {
    const onHash = () => setActiveTabState(tabFromHash());
    window.addEventListener('hashchange', onHash);
    // Desktop shortcut: "D" toggles the debug drawer (System tab has a button).
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA') return;
      if (e.key === 'd' || e.key === 'D') setDebugOpen((v) => !v);
      if (e.key === 'Escape') setDebugOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('hashchange', onHash);
      window.removeEventListener('keydown', onKey);
    };
  }, []);

  if (!state?.powerbox) {
    return (
      <div className="min-h-screen flex items-center justify-center text-hud-cyan relative">
        <div className="grid-overlay" />
        <div className="flex flex-col items-center gap-4 relative z-10">
          <div className="animate-spin h-10 w-10 border-t border-b border-hud-cyan rounded-full" />
          <p className="tracking-[0.22em] text-xs uppercase">
            Connecting to Prius {connected ? '· awaiting state' : '· linking…'}
          </p>
        </div>
      </div>
    );
  }

  const pb = state.powerbox;
  const acc = !!(pb.acc_on ?? state.vehicle?.acc_on);
  const sats: Satellites = state.satellites ?? {};
  const satNodes = Object.values(sats.nodes ?? {}).sort((a, b) => a.device_id - b.device_id);
  const satHolders = sats.power_holders ?? [];
  const v = pb.system_voltage;
  const pbFresh = pb.last_update_time != null && serverNow / 1000 - pb.last_update_time < 15;

  return (
    <div className="min-h-screen relative text-slate-300">
      <div className="grid-overlay" />
      <div className="relative z-10 max-w-7xl mx-auto px-3 pt-3 pb-24 md:p-6 flex flex-col gap-3 md:gap-4">

        <header className="hud-panel px-3 sm:px-4 py-2.5 flex items-center justify-between gap-2 sticky top-0 z-30">
          <div className="flex items-center gap-2 min-w-0">
            <Car className="text-hud-amber shrink-0" size={20} />
            <h1 className="text-sm font-medium tracking-[0.24em] text-hud-amber leading-none truncate">PRIUS</h1>
          </div>
          <div className="flex items-center gap-1.5 sm:gap-2">
            <Chip tone={!pbFresh ? 'amber' : v == null ? 'dim' : v < 11.8 ? 'red' : v < 12.2 ? 'amber' : 'cyan'}>
              <span className="tnum">{v != null ? `${v.toFixed(2)} V` : '-- V'}</span>
            </Chip>
            <Chip tone={acc ? 'green' : 'dim'}>{acc ? 'ACC' : 'OFF'}</Chip>
            <Chip tone={connected ? 'green' : 'red'}>
              {connected ? <Wifi size={12} /> : <WifiOff size={12} />}
              <span className="hidden sm:inline">{connected ? 'Online' : 'Offline'}</span>
            </Chip>
            <button onClick={() => setDebugOpen(true)} title="Debug (press D)"
              className="hidden md:block p-1.5 text-slate-600 hover:text-hud-amber transition-colors">
              <Bug size={16} />
            </button>
          </div>
        </header>

        {/* Desktop / tablet tabs */}
        <nav className="hidden md:flex gap-1 p-1 hud-panel w-fit mx-auto">
          {TABS.map(([id, label, Icon]) => (
            <button key={id} onClick={() => setActiveTab(id)}
              className={cx(
                'flex items-center gap-2 px-5 py-2 text-[0.72rem] uppercase tracking-[0.15em] transition-colors border',
                activeTab === id ? 'border-hud-cyan/50 text-hud-cyan bg-white/[0.03]' : 'border-transparent text-slate-500 hover:text-slate-200',
              )}>
              <Icon size={14} /> {label}
            </button>
          ))}
        </nav>

        <main className="flex-1">
          <Suspense fallback={<Loading />}>
            {activeTab === 'home' && (
              <HomeTab state={state} now={serverNow} sum={sum} satNodes={satNodes} satHolders={satHolders}
                onOpenSystem={() => setActiveTab('system')} />
            )}
            {activeTab === 'drive' && <DriveTab state={state} now={serverNow} />}
            {activeTab === 'history' && <HistoryTab />}
            {activeTab === 'car' && <CarTab state={state} connected={connected} now={serverNow} />}
            {activeTab === 'system' && (
              <SystemTab state={state} connected={connected} now={serverNow}
                satNodes={satNodes} satHolders={satHolders} onOpenDebug={() => setDebugOpen(true)} />
            )}
          </Suspense>
        </main>
      </div>

      {/* Phone: bottom tab bar, thumb reach, safe-area aware */}
      <nav className="md:hidden fixed bottom-0 inset-x-0 z-40 bg-ink-900/95 border-t border-ink-700 grid grid-cols-5"
        style={{ paddingBottom: 'env(safe-area-inset-bottom)' }}>
        {TABS.map(([id, label, Icon]) => (
          <button key={id} onClick={() => setActiveTab(id)}
            className={cx(
              'flex flex-col items-center justify-center gap-1 h-16 text-[0.62rem] uppercase tracking-[0.12em]',
              activeTab === id ? 'text-hud-cyan' : 'text-slate-500',
            )}>
            <Icon size={20} />
            {label}
          </button>
        ))}
      </nav>

      <Toasts />

      {debugOpen && (
        <Suspense fallback={null}>
          <DebugDrawer pb={pb} conn={state.connection} state={state} now={serverNow} onClose={() => setDebugOpen(false)} />
        </Suspense>
      )}
    </div>
  );
}
