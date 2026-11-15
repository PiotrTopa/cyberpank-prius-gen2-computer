import { useEffect, useState } from 'react';
import {
  Activity, BatteryCharging, BellRing, Bug, Car, Clock, Cpu, LayoutDashboard, Map as MapIcon, Power, SatelliteDish, Stethoscope,
  Thermometer, Wifi, WifiOff, Zap,
} from 'lucide-react';
import type { Satellites, Tab, TimeRange } from './types';
import { cx } from './lib/format';
import { useAppStream, useNow } from './hooks/useAppStream';
import { useHistory } from './hooks/useHistory';
import { Chip, Dot, StatCard, Toasts } from './components/ui';
import { DebugDrawer } from './components/DebugDrawer';
import { OverviewTab } from './tabs/OverviewTab';
import { TelemetryTab } from './tabs/TelemetryTab';
import { ControlsTab } from './tabs/ControlsTab';
import { SatellitesTab } from './tabs/SatellitesTab';
import { DiagnosticsTab } from './tabs/DiagnosticsTab';
import { MapTab } from './tabs/MapTab';
import { AlertsTab } from './tabs/AlertsTab';
import { BatteryTab } from './tabs/BatteryTab';
import { transportOf } from './lib/satellites';

const TABS: [Tab, string, typeof LayoutDashboard][] = [
  ['dashboard', 'Overview', LayoutDashboard],
  ['map', 'Map', MapIcon],
  ['satellites', 'Satellites', SatelliteDish],
  ['charts', 'Telemetry', Activity],
  ['battery', 'Battery', BatteryCharging],
  ['diagnostics', 'Diagnostics', Stethoscope],
  ['alerts', 'Alerts', BellRing],
  ['controls', 'Controls', Power],
];

export default function App() {
  const { state, connected, clockOffsetMs } = useAppStream();
  const now = useNow();
  // Backend-clock "now" — the POCO has no reliable RTC/NTP, so ages computed
  // against the raw viewer clock drift by whatever the skew happens to be.
  const serverNow = now - clockOffsetMs;
  // Tab mirrors the URL hash (#satellites) so a view can be linked/reloaded.
  const [activeTab, setActiveTabState] = useState<Tab>(() => {
    const h = window.location.hash.replace('#', '').split('/')[0];
    return TABS.some(([id]) => id === h) ? (h as Tab) : 'dashboard';
  });
  const setActiveTab = (t: Tab) => {
    setActiveTabState(t);
    window.history.replaceState(null, '', t === 'dashboard' ? window.location.pathname : `#${t}`);
  };
  const [timeRange, setTimeRange] = useState<TimeRange>('1h');
  const [debugOpen, setDebugOpen] = useState(false);
  const hist = useHistory(timeRange);

  // Hidden debug: press "D" anywhere (outside inputs) to toggle the drawer.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA') return;
      if (e.key === 'd' || e.key === 'D') setDebugOpen((v) => !v);
      if (e.key === 'Escape') setDebugOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  if (!state) {
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
  const sats: Satellites = state.satellites ?? {};
  const satNodes = Object.values(sats.nodes ?? {}).sort((a, b) => a.device_id - b.device_id);
  const satHolders = sats.power_holders ?? [];
  const manualHeld = satHolders.includes('manual:dash');
  const rs485Nodes = satNodes.filter((n) => transportOf(n) === 'rs485');

  const v = pb.system_voltage;
  const vState = v == null ? 'idle' : v < 11 ? 'danger' : v < 11.8 ? 'warn' : 'ok';
  const pbAge = serverNow / 1000 - (pb.last_update_time ?? 0);
  const pbFresh = pb.last_update_time != null && pbAge < 4;
  const cabin = pb.bmp2_t ?? state.climate?.inside_temp ?? undefined;

  const clockStr = new Date(now).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  return (
    <div className="min-h-screen relative text-slate-300">
      <div className="grid-overlay" />
      <div className="relative z-10 max-w-7xl mx-auto p-4 md:p-6 flex flex-col gap-4">

        {/* Header */}
        <header className="hud-panel px-4 sm:px-5 py-3 flex items-center justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <Car className="text-hud-amber" size={24} />
            <div className="min-w-0">
              <h1 className="text-lg font-medium tracking-[0.28em] text-hud-amber leading-none">PRIUS OS</h1>
              <p className="text-[0.6rem] text-slate-600 tracking-[0.28em] mt-1">REMOTE TELEMETRY · v2.1</p>
            </div>
          </div>

          <div className="flex items-center gap-2 sm:gap-3">
            <div className="hidden sm:flex items-center gap-1.5 text-slate-500 text-xs tnum">
              <Clock size={12} /> {clockStr}
            </div>
            <Chip tone={pbFresh ? 'green' : 'amber'} className="hidden md:inline-flex">
              <Dot tone={pbFresh ? 'green' : 'amber'} pulse={pbFresh} /> POWERBOX
            </Chip>
            <Chip tone={connected ? 'green' : 'red'}>
              {connected ? <Wifi size={12} /> : <WifiOff size={12} />}
              {connected ? 'Online' : 'Offline'}
            </Chip>
            <button
              onClick={() => setDebugOpen(true)}
              title="Debug (press D)"
              className="p-1.5 text-slate-600 hover:text-hud-amber transition-colors"
            >
              <Bug size={16} />
            </button>
          </div>
        </header>

        {/* Hero stat strip */}
        <section className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <StatCard
            icon={Zap} label="Aux Voltage" value={v?.toFixed(2)} unit="V" tone="cyan"
            state={vState}
            sub={vState === 'danger' ? 'UNDER-VOLT' : vState === 'warn' ? 'LOW' : vState === 'idle' ? 'NO DATA' : 'NOMINAL'}
          />
          <StatCard
            icon={Cpu} label="Computer Draw" value={pb.power_draw_w?.toFixed(2)} unit="W" tone="amber"
            state={pbFresh ? 'ok' : 'idle'} sub={hist.avgPower1h != null ? `1h avg ${hist.avgPower1h} W` : 'LIVE'}
          />
          <StatCard
            icon={Power} label="Power Mode" value={pb.power_mode?.toUpperCase() ?? 'LOW'} tone="green"
            state={pb.power_mode === 'full' ? 'ok' : 'warn'}
            sub={pb.power_mode === 'full' ? 'HIGH PERFORMANCE' : 'POWER SAVING'}
          />
          <StatCard
            icon={Thermometer} label="Cabin Temp" value={cabin?.toFixed(1)} unit="°C" tone="magenta"
            state={cabin == null ? 'idle' : cabin > 30 ? 'warn' : 'ok'}
            sub={cabin == null ? 'SENSOR OFFLINE' : pb.bmp2_t != null ? 'BMP280' : 'CLIMATE ECU'}
          />
        </section>

        {/* Tabs */}
        <nav className="flex gap-1 p-1 hud-panel w-full sm:w-fit sm:mx-auto overflow-x-auto">
          {TABS.map(([id, label, Icon]) => (
            <button
              key={id}
              onClick={() => setActiveTab(id)}
              className={cx(
                'flex-1 sm:flex-none whitespace-nowrap flex items-center justify-center gap-2 px-5 py-2 text-[0.7rem] uppercase tracking-[0.15em] transition-colors border',
                activeTab === id
                  ? 'border-hud-cyan/50 text-hud-cyan bg-white/[0.03]'
                  : 'border-transparent text-slate-500 hover:text-slate-200'
              )}
            >
              <Icon size={13} /> {label}
            </button>
          ))}
        </nav>

        {/* Content */}
        <div className="flex-1">
          {activeTab === 'dashboard' && (
            <OverviewTab state={state} hist={hist} now={serverNow}
              satNodes={satNodes} satHolders={satHolders} onOpenDebug={() => setDebugOpen(true)}
              onOpenSatellites={() => setActiveTab('satellites')} onOpenMap={() => setActiveTab('map')} />
          )}
          {activeTab === 'map' && <MapTab state={state} now={serverNow} />}
          {activeTab === 'satellites' && (
            <SatellitesTab sats={sats} nodes={satNodes} holders={satHolders} rail={pb.rs485_power} now={serverNow} />
          )}
          {activeTab === 'charts' && (
            <TelemetryTab hist={hist} timeRange={timeRange} onTimeRange={setTimeRange} />
          )}
          {activeTab === 'battery' && <BatteryTab />}
          {activeTab === 'diagnostics' && (
            <DiagnosticsTab diag={state.diagnostics} connected={connected} now={serverNow} />
          )}
          {activeTab === 'alerts' && <AlertsTab />}
          {activeTab === 'controls' && (
            <ControlsTab state={state} connected={connected} now={serverNow}
              satNodes={rs485Nodes} manualHeld={manualHeld} />
          )}
        </div>

        <footer className="text-center text-[0.6rem] text-slate-700 tracking-[0.2em] pb-2 uppercase">
          Cyberpunk Prius · press <span className="text-slate-500">D</span> for debug
          <button onClick={() => setDebugOpen(true)} className="ml-1.5 hover:text-hud-amber" title="debug">⌬</button>
        </footer>
      </div>

      <Toasts />

      {debugOpen && (
        <DebugDrawer pb={pb} conn={state.connection} state={state} now={serverNow} onClose={() => setDebugOpen(false)} />
      )}
    </div>
  );
}
