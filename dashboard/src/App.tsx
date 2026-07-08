import { useEffect, useState, useCallback } from 'react';
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts';
import {
  Zap, Thermometer, Car, Gauge, Power, LayoutDashboard, Activity, Clock,
  Battery, BatteryCharging, Cpu, Wifi, WifiOff, Bug, X, Fuel, Wind,
  Radio, Terminal, HeartPulse, SatelliteDish, Plug, TriangleAlert,
  CircuitBoard, ChevronRight,
} from 'lucide-react';

const cx = (...c: (string | false | undefined | null)[]) => c.filter(Boolean).join(' ');

interface Powerbox {
  connected?: boolean;
  acc_on?: boolean;
  batt_present?: boolean;
  system_voltage?: number;
  current_draw_a?: number;
  power_draw_w?: number | null;
  poco_power_w?: number | null;
  poco_core_temp?: number | null;
  poco_gpu_temp?: number | null;
  poco_ema_temp?: number | null;
  fan_duty_pct?: number | null;
  bmp_t?: number;
  bmp_p?: number;
  aht_t?: number;
  aht_h?: number;
  energy_mah?: number;
  undervoltage?: boolean;
  shutdown_requested?: boolean;
  shutdown_reason?: string;
  out1?: boolean | null;
  out2?: boolean | null;
  out3?: boolean | null;
  poco_alive?: boolean | null;
  pm_state?: string;
  powerbox_hb?: number | null;
  last_update_time?: number | null;
  power_mode?: string;
}

interface Vehicle {
  speed_kmh: number;
  gear: string;
  ice_running: boolean;
  ready_mode?: boolean;
  ev_mode?: boolean;
  ig_on?: boolean;
  rpm?: number | null;
  ice_coolant_temp?: number | null;
  fuel_level?: number;
  lpg_level?: number;
  active_fuel?: string;
}

interface Energy {
  hv_battery_voltage?: number | null;
  battery_soc: number;
  motor_power_kw?: number;
  generator_power_kw?: number;
  ice_power_kw?: number;
  regen_active?: boolean;
  charging?: boolean;
}

interface Climate {
  inside_temp?: number | null;
  outside_temp?: number | null;
  ac_on?: boolean;
  fan_speed?: number;
  target_temp?: number;
}

interface Connection {
  connected: boolean;
  gateway_version?: string | null;
  can_ready?: boolean;
  avc_ready?: boolean;
  last_message_time?: number | null;
  gateway_hb?: number | null;
  gateway_uptime_s?: number | null;
  last_heartbeat_time?: number | null;
  gateway_usb_power?: boolean | null;
}

interface AppState {
  powerbox: Powerbox;
  vehicle: Vehicle;
  energy: Energy;
  climate?: Climate;
  connection?: Connection;
}

const WS_URL = import.meta.env.DEV ? 'ws://10.200.0.5:8080/api/v1/stream' : `ws://${window.location.host}/api/v1/stream`;
const API_URL = import.meta.env.DEV ? 'http://10.200.0.5:8080/api/v1' : `/api/v1`;

type TimeRange = '1h' | '24h' | '7d';
type Tab = 'dashboard' | 'charts' | 'controls';
type Series = { time: string; value: number }[];

const fmtAge = (ts?: number | null, now = Date.now()): string => {
  if (ts == null || ts === 0) return '--';
  const s = Math.max(0, Math.floor(now / 1000 - ts));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
};

// ---- Small presentational primitives (module scope = stable identity) ----

const Dot = ({ color, pulse }: { color: string; pulse?: boolean }) => (
  <span className={cx('h-2.5 w-2.5 rounded-full shrink-0', color, pulse && 'pulse-dot')} />
);

const Panel = ({ title, icon: Icon, accent = 'text-prius-blue', border = 'border-prius-blue/20', right, children, delay = 0 }: any) => (
  <div className="glass-panel card-hover p-5 flex flex-col gap-4 fade-slide-in" style={{ animationDelay: `${delay}ms` }}>
    <div className={cx('flex items-center justify-between gap-2 border-b pb-2.5', border)}>
      <div className={cx('flex items-center gap-2', accent)}>
        <Icon size={18} />
        <h2 className="text-sm font-semibold uppercase tracking-[0.18em]">{title}</h2>
      </div>
      {right}
    </div>
    {children}
  </div>
);

const DataRow = ({ label, value, unit, highlight = false, color }: any) => (
  <div className="flex justify-between items-baseline gap-3">
    <span className="text-slate-400 text-sm">{label}</span>
    <div className="flex items-baseline gap-1">
      <span className={cx('text-xl font-mono tnum', color ?? (highlight ? 'text-prius-blue' : 'text-slate-100'))}>
        {value !== undefined && value !== null && value !== '' ? value : '--'}
      </span>
      {unit && <span className="text-slate-500 text-xs">{unit}</span>}
    </div>
  </div>
);

const StatusRow = ({ label, on, onText = 'ON', offText = 'OFF', unknownText = 'N/A' }: any) => {
  const known = on !== undefined && on !== null;
  const color = !known ? 'text-slate-500' : on ? 'text-prius-green' : 'text-slate-400';
  const dot = !known ? 'bg-slate-600' : on ? 'bg-prius-green' : 'bg-slate-600';
  return (
    <div className="flex justify-between items-center gap-3">
      <span className="text-slate-400 text-sm">{label}</span>
      <div className="flex items-center gap-2">
        <Dot color={dot} pulse={!!on} />
        <span className={cx('text-sm font-mono uppercase tracking-wider', color)}>
          {!known ? unknownText : on ? onText : offText}
        </span>
      </div>
    </div>
  );
};

// Hero metric tile.
const StatCard = ({ icon: Icon, label, value, unit, sub, accent = '#00d2ff', state: st = 'ok', delay = 0 }: any) => {
  const ring =
    st === 'danger' ? 'text-prius-red' : st === 'warn' ? 'text-prius-amber' : st === 'idle' ? 'text-slate-500' : 'text-prius-green';
  return (
    <div
      className="glass-panel card-hover accent-top p-4 sm:p-5 flex flex-col gap-3 fade-slide-in"
      style={{ ['--accent' as any]: accent, animationDelay: `${delay}ms` }}
    >
      <div className="flex items-center justify-between">
        <span className="text-[0.7rem] uppercase tracking-[0.2em] text-slate-400">{label}</span>
        <Icon size={18} style={{ color: accent }} />
      </div>
      <div className="flex items-baseline gap-1.5">
        <span className="text-3xl sm:text-4xl font-bold font-mono tnum text-slate-50">
          {value !== undefined && value !== null && value !== '' ? value : '--'}
        </span>
        {unit && <span className="text-slate-500 text-sm">{unit}</span>}
      </div>
      <div className="flex items-center gap-1.5">
        <Dot color={ring.replace('text-', 'bg-')} pulse={st === 'ok'} />
        <span className={cx('text-xs font-mono uppercase tracking-wider', ring)}>{sub}</span>
      </div>
    </div>
  );
};

// Horizontal meter (SOC, fuel, voltage band, etc.).
const Meter = ({ label, value, unit, pct, color = '#00d2ff' }: any) => (
  <div className="flex flex-col gap-1.5">
    <div className="flex justify-between items-baseline">
      <span className="text-slate-400 text-sm">{label}</span>
      <span className="text-sm font-mono tnum text-slate-100">
        {value ?? '--'}<span className="text-slate-500 text-xs ml-0.5">{unit}</span>
      </span>
    </div>
    <div className="h-2 rounded-full bg-slate-700/50 overflow-hidden">
      <div
        className="h-full rounded-full transition-all duration-500"
        style={{ width: `${Math.max(0, Math.min(100, pct ?? 0))}%`, background: color, boxShadow: `0 0 10px ${color}` }}
      />
    </div>
  </div>
);

const Chart = ({ title, icon: Icon, data, color, unit }: any) => (
  <div className="glass-panel p-5 fade-slide-in">
    <div className="flex items-center justify-between border-b border-white/10 pb-2.5 mb-4">
      <div className="flex items-center gap-2" style={{ color }}>
        <Icon size={18} />
        <h2 className="text-sm font-semibold uppercase tracking-[0.18em]">{title}</h2>
      </div>
      {data.length > 0 && (
        <span className="text-xs font-mono tnum text-slate-400">
          now <span className="text-slate-100">{data[data.length - 1].value}</span> {unit}
        </span>
      )}
    </div>
    <ResponsiveContainer width="100%" height={230}>
      <AreaChart data={data} margin={{ top: 5, right: 8, bottom: 5, left: -18 }}>
        <defs>
          <linearGradient id={`g-${title}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity={0.35} />
            <stop offset="100%" stopColor={color} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.07)" />
        <XAxis dataKey="time" stroke="#64748b" tick={{ fontSize: 11 }} minTickGap={28} />
        <YAxis domain={['auto', 'auto']} stroke="#64748b" tick={{ fontSize: 11 }} width={48} />
        <Tooltip
          contentStyle={{ backgroundColor: 'rgba(2,6,23,0.92)', border: `1px solid ${color}`, borderRadius: '0.6rem' }}
          itemStyle={{ color }}
          labelStyle={{ color: '#94a3b8' }}
        />
        <Area type="monotone" dataKey="value" stroke={color} strokeWidth={2} fill={`url(#g-${title})`} dot={false} isAnimationActive={false} />
      </AreaChart>
    </ResponsiveContainer>
  </div>
);

export default function App() {
  const [state, setState] = useState<AppState | null>(null);
  const [voltageHistory, setVoltageHistory] = useState<Series>([]);
  const [tempHistory, setTempHistory] = useState<Series>([]);
  const [currentHistory, setCurrentHistory] = useState<Series>([]);
  const [pocoHistory, setPocoHistory] = useState<Series>([]);
  const [connected, setConnected] = useState(false);
  const [activeTab, setActiveTab] = useState<Tab>('dashboard');
  const [timeRange, setTimeRange] = useState<TimeRange>('1h');
  const [avgPower1m, setAvgPower1m] = useState<number | null>(null);
  const [avgPower5m, setAvgPower5m] = useState<number | null>(null);
  const [avgPower1h, setAvgPower1h] = useState<number | null>(null);
  const [avgPocoPower1m, setAvgPocoPower1m] = useState<number | null>(null);
  const [avgPocoPower5m, setAvgPocoPower5m] = useState<number | null>(null);
  const [avgPocoPower1h, setAvgPocoPower1h] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const [debugOpen, setDebugOpen] = useState(false);

  // 1 Hz clock for freshness / age readouts.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

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

  const fetchHistory = useCallback(async () => {
    try {
      const nowS = Math.floor(Date.now() / 1000);
      let fromStr = '';
      if (timeRange === '1h') fromStr = `&from=${nowS - 3600}`;
      else if (timeRange === '24h') fromStr = `&from=${nowS - 86400}`;
      else if (timeRange === '7d') fromStr = `&from=${nowS - 7 * 86400}`;

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

      if (voltRes.ok) {
        const data = await voltRes.json();
        setVoltageHistory(data.points.map((p: any) => ({ time: formatTime(p.t), value: Number(p.avg.toFixed(2)) })));
      }
      if (tempRes.ok) {
        const data = await tempRes.json();
        setTempHistory(data.points.map((p: any) => ({ time: formatTime(p.t), value: Number(p.avg.toFixed(1)) })));
      }
      if (pbPowerRes.ok) {
        const data = await pbPowerRes.json();
        setCurrentHistory(data.points.map((p: any) => ({ time: formatTime(p.t), value: Number(p.avg.toFixed(2)) })));
      }
      if (pocoPowerRes.ok) {
        const data = await pocoPowerRes.json();
        setPocoHistory(data.points.map((p: any) => ({ time: formatTime(p.t), value: Number(p.avg.toFixed(2)) })));
      }
      
      const calcAverages = async (res1h: Response, res5m: Response) => {
        let avg1h = null, avg5m = null, avg1m = null;
        if (res1h.ok) {
          const data = await res1h.json();
          if (data.points && data.points.length > 0) {
            const sum = data.points.reduce((acc: number, p: any) => acc + p.avg, 0);
            avg1h = Number((sum / data.points.length).toFixed(2));
          }
        }
        if (res5m.ok) {
          const data = await res5m.json();
          if (data.points && data.points.length > 0) {
            const sum5m = data.points.reduce((acc: number, p: any) => acc + p.avg, 0);
            avg5m = Number((sum5m / data.points.length).toFixed(2));
            const points1m = data.points.filter((p: any) => p.t >= nowS - 60);
            if (points1m.length > 0) {
              const sum1m = points1m.reduce((acc: number, p: any) => acc + p.avg, 0);
              avg1m = Number((sum1m / points1m.length).toFixed(2));
            }
          }
        }
        return { avg1h, avg5m, avg1m };
      };

      const pbAvgs = await calcAverages(pb1h, pb5m);
      setAvgPower1h(pbAvgs.avg1h);
      setAvgPower5m(pbAvgs.avg5m);
      setAvgPower1m(pbAvgs.avg1m);

      const pocoAvgs = await calcAverages(poco1h, poco5m);
      setAvgPocoPower1h(pocoAvgs.avg1h);
      setAvgPocoPower5m(pocoAvgs.avg5m);
      setAvgPocoPower1m(pocoAvgs.avg1m);
    } catch (e) {
      console.error('Failed to fetch history', e);
    }
  }, [timeRange]);

  useEffect(() => {
    fetchHistory();
    const interval = setInterval(fetchHistory, 60000);
    return () => clearInterval(interval);
  }, [fetchHistory]);

  const sendCommand = async (name: string, params: Record<string, any> = {}) => {
    try {
      const res = await fetch(`${API_URL}/commands/${name}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params)
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(`Command failed: ${err.detail || res.statusText}`);
      }
    } catch (e: any) {
      alert(`Command failed: ${e.message}`);
    }
  };

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

  // ---- Loading screen ----------------------------------------------------
  if (!state) {
    return (
      <div className="min-h-screen flex items-center justify-center text-prius-blue font-mono relative">
        <div className="grid-overlay" />
        <div className="flex flex-col items-center gap-4 relative z-10">
          <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-b-2 border-prius-blue" />
          <p className="tracking-widest text-sm">
            CONNECTING TO CYBERPUNK PRIUS {connected ? '· AWAITING STATE' : '· LINKING…'}
          </p>
        </div>
      </div>
    );
  }

  const pb = state.powerbox;
  const conn = state.connection;
  const climate = state.climate ?? {};
  const energy = state.energy;
  const veh = state.vehicle;

  const v = pb.system_voltage;
  const vState = v == null ? 'idle' : v < 11 ? 'danger' : v < 11.8 ? 'warn' : 'ok';
  const powerW = pb.power_draw_w?.toFixed(2) ?? '--';
  const pocoPowerW = pb.poco_power_w?.toFixed(2) ?? '--';
  const pbAge = now / 1000 - (pb.last_update_time ?? 0);
  const pbFresh = pb.last_update_time != null && pbAge < 4;
  const cabin = pb.aht_t ?? climate.inside_temp ?? undefined;
  const soc = Math.round((energy.battery_soc ?? 0) * 100);

  const clockStr = new Date(now).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  return (
    <div className="min-h-screen relative text-slate-200">
      <div className="grid-overlay" />
      <div className="relative z-10 max-w-7xl mx-auto p-4 md:p-7 flex flex-col gap-6">

        {/* Header */}
        <header className="glass-panel px-4 sm:px-6 py-3.5 flex items-center justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <Car className="text-prius-blue text-glow" size={30} />
            <div className="min-w-0">
              <h1 className="text-xl sm:text-2xl font-bold tracking-[0.2em] text-prius-blue text-glow leading-none">PRIUS&nbsp;OS</h1>
              <p className="text-[0.65rem] text-slate-400 tracking-[0.25em] mt-0.5">CYBERPUNK REMOTE · v2.0</p>
            </div>
          </div>

          <div className="flex items-center gap-2 sm:gap-3">
            <div className="hidden sm:flex items-center gap-1.5 text-slate-400 font-mono text-sm tnum">
              <Clock size={14} /> {clockStr}
            </div>
            <span className="hidden md:flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-slate-800/60 border border-white/10 text-xs font-mono">
              <Dot color={pbFresh ? 'bg-prius-green' : 'bg-prius-amber'} pulse={pbFresh} />
              <span className={pbFresh ? 'text-prius-green' : 'text-prius-amber'}>POWERBOX</span>
            </span>
            <span className={cx(
              'flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-xs font-mono uppercase tracking-wider',
              connected ? 'bg-prius-green/10 border-prius-green/40 text-prius-green' : 'bg-prius-red/10 border-prius-red/40 text-prius-red'
            )}>
              {connected ? <Wifi size={14} /> : <WifiOff size={14} />}
              {connected ? 'Online' : 'Offline'}
            </span>
            <button
              onClick={() => setDebugOpen(true)}
              title="Debug (press D)"
              className="p-1.5 rounded-md text-slate-500 hover:text-prius-magenta hover:bg-prius-magenta/10 transition-colors"
            >
              <Bug size={18} />
            </button>
          </div>
        </header>

        {/* Hero stat strip */}
        <section className="grid grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-4">
          <StatCard
            icon={Zap} label="Aux Voltage" value={v?.toFixed(2)} unit="V" accent="#00d2ff"
            state={vState}
            sub={vState === 'danger' ? 'UNDER-VOLT' : vState === 'warn' ? 'LOW' : vState === 'idle' ? 'NO DATA' : 'NOMINAL'}
            delay={0}
          />
          <StatCard
            icon={Cpu} label="Computer Draw" value={powerW} unit="W" accent="#f59e0b"
            state={pbFresh ? 'ok' : 'idle'} sub={avgPower1h != null ? `1h avg ${avgPower1h} W` : 'LIVE'}
            delay={60}
          />
          <StatCard
            icon={Power} label="Power Mode" value={pb.power_mode?.toUpperCase() ?? 'LOW'} unit="" accent="#10b981"
            state={pb.power_mode === 'full' ? 'ok' : 'warn'}
            sub={pb.power_mode === 'full' ? 'HIGH PERFORMANCE' : 'POWER SAVING'} delay={120}
          />
          <StatCard
            icon={Thermometer} label="Cabin Temp" value={cabin?.toFixed(1)} unit="°C" accent="#e431d6"
            state={cabin == null ? 'idle' : cabin > 30 ? 'warn' : 'ok'}
            sub={pb.aht_h != null ? `${pb.aht_h.toFixed(0)}% RH` : 'AHT20'} delay={180}
          />
        </section>

        {/* Tabs */}
        <nav className="flex gap-1.5 p-1 glass-panel rounded-xl w-full sm:w-fit sm:mx-auto overflow-x-auto">
          {([
            ['dashboard', 'Overview', LayoutDashboard],
            ['charts', 'Telemetry', Activity],
            ['controls', 'Controls', Power],
          ] as [Tab, string, any][]).map(([id, label, Icon]) => (
            <button
              key={id}
              onClick={() => setActiveTab(id)}
              className={cx(
                'flex-1 sm:flex-none whitespace-nowrap flex items-center justify-center gap-2 px-5 py-2 rounded-lg font-mono text-xs uppercase tracking-wider transition-all duration-300',
                activeTab === id
                  ? 'bg-prius-blue/15 text-prius-blue shadow-[inset_0_0_12px_rgba(0,210,255,0.18)]'
                  : 'text-slate-400 hover:text-slate-200'
              )}
            >
              <Icon size={15} /> {label}
            </button>
          ))}
        </nav>

        {/* Content */}
        <div className="flex-1">
          {activeTab === 'dashboard' && (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 sm:gap-6">
              <Panel title="Vehicle" icon={Gauge} delay={0}>
                <DataRow label="Speed" value={veh.speed_kmh?.toFixed(0)} unit="km/h" highlight />
                <DataRow label="Gear" value={veh.gear?.replace('GearPosition.', '')} />
                <StatusRow label="Ready Mode" on={veh.ready_mode} onText="READY" offText="OFF" />
                <StatusRow label="ICE" on={veh.ice_running} onText="RUNNING" offText="OFF" />
                <StatusRow label="EV Mode" on={veh.ev_mode} onText="EV" offText="OFF" />
              </Panel>

              <Panel title="Hybrid Energy" icon={Battery} accent="text-prius-green" border="border-prius-green/20" delay={60}
                right={energy.charging ? <span className="text-xs font-mono text-prius-green flex items-center gap-1"><BatteryCharging size={14} /> CHG</span> : energy.regen_active ? <span className="text-xs font-mono text-prius-blue">REGEN</span> : undefined}>
                <Meter label="HV Battery SOC" value={soc} unit="%" pct={soc} color={soc < 30 ? '#f59e0b' : '#10b981'} />
                <DataRow label="Motor (MG2)" value={energy.motor_power_kw?.toFixed(1)} unit="kW" />
                <DataRow label="Generator (MG1)" value={energy.generator_power_kw?.toFixed(1)} unit="kW" />
                <DataRow label="Engine" value={energy.ice_power_kw?.toFixed(1)} unit="kW" />
              </Panel>

              <Panel title="Computer Power" icon={Zap} accent="text-prius-amber" border="border-prius-amber/20" delay={120}
                right={<span className="text-[0.65rem] font-mono text-slate-500">age {fmtAge(pb.last_update_time, now)}</span>}>
                <DataRow label="Stack Live Draw" value={powerW} unit="W" color="text-prius-amber" />
                <DataRow label="Stack 1m Avg" value={avgPower1m} unit="W" />
                <DataRow label="Stack 5m Avg" value={avgPower5m} unit="W" />
                <DataRow label="Stack 1h Avg" value={avgPower1h} unit="W" />
                <DataRow label="POCO Live Draw" value={pocoPowerW} unit="W" color="text-prius-amber" />
                <DataRow label="POCO 1m Avg" value={avgPocoPower1m} unit="W" />
                <DataRow label="POCO 5m Avg" value={avgPocoPower5m} unit="W" />
                <DataRow label="POCO 1h Avg" value={avgPocoPower1h} unit="W" />
                <DataRow label="Aux Voltage" value={v?.toFixed(2)} unit="V" />
                <DataRow label="Energy Used" value={pb.energy_mah?.toFixed(1)} unit="mAh" />
              </Panel>

              <Panel title="Environment" icon={Wind} accent="text-prius-magenta" border="border-prius-magenta/20" delay={180}>
                <DataRow label="Cabin Temp" value={pb.aht_t?.toFixed(1)} unit="°C" color="text-prius-magenta" />
                <DataRow label="Humidity" value={pb.aht_h?.toFixed(1)} unit="%" />
                <DataRow label="Pressure" value={pb.bmp_p ? (pb.bmp_p / 100).toFixed(1) : undefined} unit="hPa" />
                <DataRow label="Outside" value={climate.outside_temp?.toFixed?.(1)} unit="°C" />
              </Panel>

              <Panel title="Thermal Management" icon={Wind} accent="text-prius-blue" border="border-prius-blue/20" delay={200}>
                <DataRow label="POCO CPU" value={pb.poco_core_temp?.toFixed(1)} unit="°C" color={pb.poco_core_temp && pb.poco_core_temp > 60 ? "text-prius-amber" : "text-prius-blue"} />
                <DataRow label="POCO GPU" value={pb.poco_gpu_temp?.toFixed(1)} unit="°C" color={pb.poco_gpu_temp && pb.poco_gpu_temp > 60 ? "text-prius-amber" : "text-prius-blue"} />
                <DataRow label="Simulated Temp" value={pb.poco_ema_temp?.toFixed(1)} unit="°C" color="text-prius-blue" />
                <DataRow label="Chassis Fan" value={pb.fan_duty_pct?.toFixed(0)} unit="%" highlight />
              </Panel>

              <Panel title="Fuel" icon={Fuel} accent="text-prius-violet" border="border-prius-violet/20" delay={240}>
                <Meter label="Petrol" value={veh.fuel_level} unit="L" pct={((veh.fuel_level ?? 0) / 45) * 100} color="#8b5cf6" />
                <Meter label="LPG" value={veh.lpg_level} unit="L" pct={((veh.lpg_level ?? 0) / 45) * 100} color="#e431d6" />
                <DataRow label="Active Fuel" value={veh.active_fuel} />
              </Panel>

              <Panel title="System Link" icon={Radio} delay={300}
                right={<button onClick={() => setDebugOpen(true)} className="text-[0.65rem] font-mono text-slate-500 hover:text-prius-magenta flex items-center gap-1">DEBUG <ChevronRight size={12} /></button>}>
                <StatusRow label="Powerbox Link" on={pb.connected} onText="UP" offText="DOWN" />
                <StatusRow label="POCO Heartbeat" on={pb.poco_alive} onText="ALIVE" offText="LOST" />
                <DataRow label="PM State" value={(pb.pm_state || '—').toUpperCase()} />
                {pb.undervoltage && <StatusRow label="Under-voltage" on={true} onText="TRIPPED" />}
              </Panel>
            </div>
          )}

          {activeTab === 'charts' && (
            <div className="flex flex-col gap-5">
              <div className="flex items-center gap-2 p-1 glass-panel rounded-lg w-fit">
                <Clock className="text-slate-500 ml-2 mr-1" size={15} />
                {(['1h', '24h', '7d'] as TimeRange[]).map((tr) => (
                  <button
                    key={tr}
                    onClick={() => setTimeRange(tr)}
                    className={cx(
                      'px-3.5 py-1 rounded-md font-mono text-xs uppercase tracking-wider transition-all duration-300',
                      timeRange === tr ? 'bg-prius-blue/15 text-prius-blue' : 'text-slate-400 hover:text-slate-200'
                    )}
                  >
                    {tr}
                  </button>
                ))}
              </div>
              <Chart title="Aux Battery 12V" icon={Zap} data={voltageHistory} color="#00d2ff" unit="V" />
              <Chart title="Stack Draw" icon={Cpu} data={currentHistory} color="#f59e0b" unit="W" />
              <Chart title="POCO Draw" icon={Cpu} data={pocoHistory} color="#f59e0b" unit="W" />
              <Chart title="Cabin Temperature" icon={Thermometer} data={tempHistory} color="#e431d6" unit="°C" />
            </div>
          )}

          {activeTab === 'controls' && (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 sm:gap-6">
              <Panel title="Gateway USB Power" icon={Radio}>
                <div className="flex items-center justify-between mb-3">
                  <span className="text-xs font-mono uppercase tracking-wider text-slate-400">Current State</span>
                  <span className={`text-xs font-mono uppercase font-bold px-2 py-0.5 rounded ${
                    conn?.gateway_usb_power == null ? 'text-slate-500 bg-slate-700/40'
                      : conn.gateway_usb_power ? 'text-prius-green bg-prius-green/10'
                      : 'text-prius-red bg-prius-red/10'}`}>
                    {conn?.gateway_usb_power == null ? 'N/A' : conn.gateway_usb_power ? 'POWERED' : 'OFF'}
                  </span>
                </div>
                <div className="flex gap-3">
                  <button
                    className={`flex-1 py-4 border rounded-xl transition-all duration-300 flex flex-col items-center gap-1 ${
                      conn?.gateway_usb_power
                        ? 'border-prius-green/60 text-prius-green bg-prius-green/10'
                        : 'border-prius-blue/40 text-prius-blue hover:bg-prius-blue/10 hover:border-prius-blue'}`}
                    onClick={() => sendCommand('gateway_power', { on: true })}
                  >
                    <span className="font-mono uppercase text-xs font-bold">Power ON</span>
                  </button>
                  <button
                    className={`flex-1 py-4 border rounded-xl transition-all duration-300 flex flex-col items-center gap-1 ${
                      conn?.gateway_usb_power === false
                        ? 'border-prius-red/60 text-prius-red bg-prius-red/10'
                        : 'border-slate-700/60 text-slate-400 hover:bg-white/5 hover:border-slate-500'}`}
                    onClick={() => sendCommand('gateway_power', { on: false })}
                  >
                    <span className="font-mono uppercase text-xs font-bold">Power OFF</span>
                  </button>
                </div>
                <p className="text-xs text-slate-500 mt-3 text-center">Cuts/restores the CAN gateway board's USB hub-port power</p>
              </Panel>

              <Panel title="Remote Control" icon={Power}>
                <button
                  className="w-full py-8 border-2 border-dashed border-prius-blue/40 rounded-xl text-prius-blue hover:bg-prius-blue/10 hover:border-prius-blue transition-all duration-300 flex flex-col items-center justify-center gap-2"
                  onClick={() => alert('Remote start functionality to be added!')}
                >
                  <Power size={30} />
                  <span className="font-mono uppercase tracking-[0.2em] font-bold text-sm">Start Vehicle Remotely</span>
                </button>
                <p className="text-xs text-slate-500 text-center">Sends ACC wake via powerbox · coming soon</p>
              </Panel>

              <Panel title="System Status" icon={LayoutDashboard} className="md:col-span-2">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-1">
                  <StatusRow label="Backend Stream" on={connected} onText="LIVE" offText="DOWN" />
                  <StatusRow label="Powerbox Firmware" on={pb.connected} onText="ACTIVE" offText="OFFLINE" />
                  <StatusRow label="Gateway Link" on={conn?.connected} onText="UP" offText="DOWN" />
                  <StatusRow label="Gateway USB Power" on={conn?.gateway_usb_power} onText="POWERED" offText="OFF" />
                  <StatusRow label="CAN Gateway" on={conn?.can_ready} onText="READY" offText="WAIT" />
                  <DataRow label="Telemetry Age" value={fmtAge(pb.last_update_time, now)} />
                  <DataRow label="Gateway Ver" value={conn?.gateway_version ?? undefined} />
                </div>
              </Panel>
            </div>
          )}

        </div>

        <footer className="text-center text-[0.65rem] font-mono text-slate-600 tracking-wider pb-2">
          CYBERPUNK PRIUS · press <span className="text-slate-400">D</span> for debug ·
          <button onClick={() => setDebugOpen(true)} className="ml-1 hover:text-prius-magenta" title="debug">⌬</button>
        </footer>
      </div>

      {debugOpen && (
        <DebugDrawer pb={pb} conn={conn} state={state} now={now} onClose={() => setDebugOpen(false)} />
      )}
    </div>
  );
}

// ---- Debug drawer (slide-over) -------------------------------------------

function DebugDrawer({ pb, conn, state, now, onClose }: any) {
  const railColor = (x?: boolean | null) => (x == null ? 'text-slate-500' : x ? 'text-prius-green' : 'text-prius-red');
  const railText = (x?: boolean | null) => (x == null ? 'N/A' : x ? 'HIGH' : 'LOW');
  const railDot = (x?: boolean | null) => (x == null ? 'bg-slate-600' : x ? 'bg-prius-green' : 'bg-prius-red');
  const age = (ts?: number | null) => {
    if (ts == null || ts === 0) return '--';
    const s = Math.max(0, Math.floor(now / 1000 - ts));
    return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m` : `${Math.floor(s / 3600)}h`;
  };

  const Row = ({ k, children }: any) => (
    <div className="flex justify-between items-center gap-3 py-1.5 border-b border-white/5 last:border-0">
      <span className="text-slate-400 text-xs font-mono">{k}</span>
      <span className="text-xs font-mono tnum text-slate-100 text-right">{children}</span>
    </div>
  );

  const Rail = ({ name, desc, val }: any) => (
    <div className="flex items-center justify-between gap-2 bg-slate-800/40 rounded-lg px-3 py-2">
      <div className="min-w-0">
        <div className="text-sm font-mono text-slate-100">{name}</div>
        <div className="text-[0.65rem] text-slate-500 truncate">{desc}</div>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <span className={cx('h-2.5 w-2.5 rounded-full', railDot(val))} />
        <span className={cx('text-sm font-mono uppercase', railColor(val))}>{railText(val)}</span>
      </div>
    </div>
  );

  const Section = ({ title, icon: Icon, children }: any) => (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2 text-prius-magenta/90 mt-4 mb-1">
        <Icon size={14} />
        <h3 className="text-[0.7rem] font-semibold uppercase tracking-[0.2em]">{title}</h3>
      </div>
      {children}
    </div>
  );

  const pbAge = now / 1000 - (pb.last_update_time ?? 0);
  const fresh = pb.last_update_time != null && pbAge < 4;

  return (
    <div className="fixed inset-0 z-50">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <aside className="drawer-in absolute right-0 top-0 h-full w-full max-w-md bg-[#070b18]/95 border-l border-prius-magenta/30 shadow-2xl flex flex-col">
        <div className="flex items-center justify-between px-5 py-4 border-b border-prius-magenta/20">
          <div className="flex items-center gap-2 text-prius-magenta">
            <Terminal size={18} />
            <h2 className="text-sm font-bold uppercase tracking-[0.2em]">Debug · Powerbox</h2>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-md text-slate-400 hover:text-white hover:bg-white/10">
            <X size={18} />
          </button>
        </div>

        <div className="overflow-y-auto px-5 pb-8 flex-1">
          <div className="flex items-center gap-2 mt-3 text-xs font-mono">
            <span className={cx('h-2 w-2 rounded-full', fresh ? 'bg-prius-green pulse-dot' : 'bg-prius-amber')} />
            <span className={fresh ? 'text-prius-green' : 'text-prius-amber'}>
              {fresh ? 'STREAMING' : 'STALE'} · last update {age(pb.last_update_time)} ago
            </span>
          </div>

          <Section title="Power Rails (MOSFET drivers)" icon={CircuitBoard}>
            <div className="flex flex-col gap-2">
              <Rail name="OUT1 · GP29" desc="Master rail — POCO + RP2040 + hub (latch)" val={pb.out1} />
              <Rail name="OUT2 · GP28" desc="RS485 satellite power" val={pb.out2} />
              <Rail name="OUT3 · GP27" desc="Spare" val={pb.out3} />
            </div>
          </Section>

          <Section title="Heartbeat & State Machine" icon={HeartPulse}>
            <Row k="poco_alive">
              <span className={pb.poco_alive ? 'text-prius-green' : 'text-prius-red'}>
                {pb.poco_alive == null ? 'N/A' : pb.poco_alive ? 'ALIVE' : 'LOST'}
              </span>
            </Row>
            <Row k="pm_state"><span className="text-prius-blue uppercase">{pb.pm_state || '--'}</span></Row>
            <Row k="powerbox_hb">{pb.powerbox_hb ?? '--'}</Row>
            <Row k="undervoltage">
              <span className={pb.undervoltage ? 'text-prius-red' : 'text-slate-300'}>{pb.undervoltage ? 'TRIPPED' : 'ok'}</span>
            </Row>
            <Row k="batt_present">{pb.batt_present == null ? '--' : pb.batt_present ? 'yes' : 'no'}</Row>
            <Row k="shutdown_requested">
              <span className={pb.shutdown_requested ? 'text-prius-amber' : 'text-slate-300'}>{pb.shutdown_requested ? 'YES' : 'no'}</span>
            </Row>
            {pb.shutdown_reason ? <Row k="shutdown_reason">{pb.shutdown_reason}</Row> : null}
          </Section>

          <Section title="INA219 / Sensors (raw)" icon={Plug}>
            <Row k="system_voltage">{pb.system_voltage?.toFixed(3) ?? '--'} V</Row>
            <Row k="current_draw">{pb.current_draw_a != null ? (pb.current_draw_a * 1000).toFixed(1) : '--'} mA</Row>
            <Row k="power_draw">{pb.power_draw_w != null ? pb.power_draw_w.toFixed(2) : '--'} W</Row>
            <Row k="energy_mah">{pb.energy_mah?.toFixed(3) ?? '--'} mAh</Row>
            <Row k="bmp_t / bmp_p">{pb.bmp_t?.toFixed(2) ?? '--'} °C · {pb.bmp_p ? (pb.bmp_p / 100).toFixed(1) : '--'} hPa</Row>
            <Row k="aht_t / aht_h">{pb.aht_t?.toFixed(2) ?? '--'} °C · {pb.aht_h?.toFixed(1) ?? '--'} %</Row>
            <Row k="acc_on · GP11 (ignition)">
              <span className={pb.acc_on ? 'text-prius-green' : 'text-slate-400'}>{pb.acc_on ? 'ON' : 'OFF'}</span>
            </Row>
          </Section>

          <Section title="Link / Gateway" icon={SatelliteDish}>
            <Row k="powerbox.connected">
              <span className={pb.connected ? 'text-prius-green' : 'text-prius-red'}>{pb.connected ? 'true' : 'false'}</span>
            </Row>
            <Row k="gateway.connected">
              <span className={conn?.connected ? 'text-prius-green' : 'text-prius-red'}>{conn?.connected ? 'true' : 'false'}</span>
            </Row>
            <Row k="gateway_version">{conn?.gateway_version ?? '--'}</Row>
            <Row k="gateway_hb (alive)">{conn?.gateway_hb == null ? '--' : conn.gateway_hb}</Row>
            <Row k="gateway_uptime">{conn?.gateway_uptime_s == null ? '--' : `${conn.gateway_uptime_s}s`}</Row>
            <Row k="last_heartbeat">{age(conn?.last_heartbeat_time)} ago</Row>
            <Row k="can_ready">{conn?.can_ready == null ? '--' : String(conn.can_ready)}</Row>
            <Row k="avc_ready">{conn?.avc_ready == null ? '--' : String(conn.avc_ready)}</Row>
            <Row k="last_message">{age(conn?.last_message_time)} ago</Row>
          </Section>

          <Section title="Raw State JSON" icon={Bug}>
            <pre className="text-[0.65rem] leading-relaxed text-slate-400 bg-black/40 rounded-lg p-3 overflow-x-auto max-h-60 overflow-y-auto">
              {JSON.stringify(state.powerbox, null, 2)}
            </pre>
          </Section>

          <p className="text-center text-[0.65rem] font-mono text-slate-600 mt-4 flex items-center justify-center gap-1">
            <TriangleAlert size={12} /> press <span className="text-slate-400">D</span> or <span className="text-slate-400">Esc</span> to close
          </p>
        </div>
      </aside>
    </div>
  );
}
