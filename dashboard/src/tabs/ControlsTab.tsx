import { useState } from 'react';
import { BatteryWarning, Fan, LayoutDashboard, Power, SatelliteDish } from 'lucide-react';
import type { AppState, SatelliteNode } from '../types';
import { fmtAge } from '../lib/format';
import { sendCommand } from '../lib/api';
import { Btn, Chip, DataRow, Panel, StatusRow } from '../components/ui';
import { SatelliteNodeRow } from '../components/SatellitesPanel';

/** Stepper row for a voltage threshold: −/＋ in 0.1 V steps around a draft value. */
function VoltStepper({ label, value, draft, onDraft, lo, hi }: {
  label: string;
  value?: number | null;
  draft: number | null;
  onDraft: (v: number | null) => void;
  lo: number;
  hi: number;
}) {
  const shown = draft ?? value;
  const step = (d: number) => {
    if (shown == null) return;
    const next = Math.round((shown + d) * 10) / 10;
    if (next < lo || next > hi) return;
    onDraft(next === value ? null : next);
  };
  return (
    <div className="flex justify-between items-center gap-3">
      <span className="text-slate-500 text-xs uppercase tracking-wider">{label}</span>
      <div className="flex items-center gap-2">
        <Btn className="px-2 py-0.5 text-[0.7rem]" disabled={shown == null} onClick={() => step(-0.1)}>−</Btn>
        <span className={`text-base tnum w-14 text-center ${draft != null ? 'text-hud-amber' : 'text-slate-100'}`}>
          {shown != null ? shown.toFixed(1) : '--'}
          <span className="text-slate-600 text-[0.65rem] ml-0.5">V</span>
        </span>
        <Btn className="px-2 py-0.5 text-[0.7rem]" disabled={shown == null} onClick={() => step(0.1)}>＋</Btn>
      </div>
    </div>
  );
}

export function ControlsTab({ state, connected, now, satNodes, manualHeld }: {
  state: AppState;
  connected: boolean;
  now: number;
  satNodes: SatelliteNode[];
  manualHeld: boolean;
}) {
  const pb = state.powerbox;
  const conn = state.connection;
  const [holdBusy, setHoldBusy] = useState(false);

  const setHold = async (on: boolean) => {
    setHoldBusy(true);
    await sendCommand('satellite_power_hold', { name: 'dash', on });
    setHoldBusy(false);
  };

  // Undervoltage threshold drafts (null = tracking the live value).
  const [uvThrDraft, setUvThrDraft] = useState<number | null>(null);
  const [uvRecDraft, setUvRecDraft] = useState<number | null>(null);
  const [uvBusy, setUvBusy] = useState(false);
  const [fanBusy, setFanBusy] = useState(false);

  // Fan override: null = auto, 0 = forced off, 100 = forced on.
  const fanOverride = pb.fan_override_pct ?? null;
  const fanMode: 'off' | 'on' | 'auto' = fanOverride == null ? 'auto' : fanOverride === 0 ? 'off' : 'on';
  const setFanMode = async (mode: 'off' | 'on' | 'auto') => {
    setFanBusy(true);
    if (mode === 'auto') await sendCommand('fan_auto');
    else await sendCommand('set_fan', { pct: mode === 'on' ? 100 : 0 });
    setFanBusy(false);
  };
  const uvThr = uvThrDraft ?? pb.uv_threshold ?? null;
  const uvRec = uvRecDraft ?? pb.uv_recover ?? null;
  const uvDirty = uvThrDraft != null || uvRecDraft != null;
  const uvValid = uvThr != null && uvRec != null && uvRec >= uvThr + 0.2;

  const applyUv = async () => {
    if (!uvValid || uvThr == null || uvRec == null) return;
    setUvBusy(true);
    const ok = await sendCommand('set_undervoltage', { threshold: uvThr, recover: uvRec });
    if (ok) {
      setUvThrDraft(null);
      setUvRecDraft(null);
    }
    setUvBusy(false);
  };

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      <Panel title="Remote Control" code="CTL-02" icon={Power}>
        <div className="w-full py-7 border border-dashed border-ink-700 text-slate-600 flex flex-col items-center justify-center gap-2 cursor-not-allowed select-none">
          <Power size={26} />
          <span className="uppercase tracking-[0.22em] text-xs">Start Vehicle Remotely</span>
          <span className="uppercase tracking-[0.15em] text-[0.6rem] text-hud-amber">Not wired yet</span>
        </div>
        <p className="text-xs text-slate-600 text-center">Will send ACC wake via powerbox · coming soon</p>
      </Panel>

      <Panel title="Satellite Rail (OUT2)" code="CTL-03" icon={SatelliteDish} tone="green"
        right={
          <Chip tone={pb.out2 == null ? 'dim' : pb.out2 ? 'green' : 'red'}>
            {pb.out2 == null ? 'N/A' : pb.out2 ? 'LIVE' : 'OFF'}
          </Chip>
        }>
        <div className="flex gap-3">
          <Btn className="flex-1 py-3" tone="green" active={manualHeld} disabled={holdBusy}
            onClick={() => setHold(true)}>
            {holdBusy ? '…' : 'Hold ON'}
          </Btn>
          <Btn className="flex-1 py-3" tone="red" disabled={!manualHeld || holdBusy}
            onClick={() => setHold(false)}>
            {holdBusy ? '…' : 'Release'}
          </Btn>
        </div>
        <p className="text-xs text-slate-600 text-center">
          Manual wake-lock: rail + gateway (CAN/AVC + RS485) stay powered while any holder
          (acc / queue / manual) is held. Drops ~10s after the last release.
        </p>
        {satNodes.length > 0 && (
          <div className="flex flex-col gap-1.5">
            {satNodes.map((n) => (
              <SatelliteNodeRow key={n.device_id} n={n} now={now}
                right={
                  <Btn tone="cyan" className="px-2 py-0.5 text-[0.6rem]"
                    title="Enqueue a status poll (powers the rail if needed)"
                    onClick={() => sendCommand('satellite_send', { device_id: n.device_id, payload: { a: 'status' } })}>
                    Ping
                  </Btn>
                }
              />
            ))}
          </div>
        )}
      </Panel>

      <Panel title="Chassis Fan" code="CTL-05" icon={Fan} tone="cyan"
        right={
          <Chip tone={fanMode === 'auto' ? 'green' : fanMode === 'on' ? 'cyan' : 'red'}>
            {fanMode === 'auto' ? `AUTO · ${pb.fan_duty_pct?.toFixed(0) ?? '--'}%` : fanMode === 'on' ? 'FORCED ON' : 'FORCED OFF'}
          </Chip>
        }>
        <div className="flex gap-3">
          <Btn className="flex-1 py-3" tone="red" active={fanMode === 'off'} disabled={fanBusy}
            onClick={() => setFanMode('off')}>
            {fanBusy ? '…' : 'Off'}
          </Btn>
          <Btn className="flex-1 py-3" tone="cyan" active={fanMode === 'on'} disabled={fanBusy}
            onClick={() => setFanMode('on')}>
            {fanBusy ? '…' : 'On'}
          </Btn>
          <Btn className="flex-1 py-3" tone="green" active={fanMode === 'auto'} disabled={fanBusy}
            onClick={() => setFanMode('auto')}>
            {fanBusy ? '…' : 'Auto'}
          </Btn>
        </div>
        <p className="text-xs text-slate-600 text-center">
          Auto = POCO-delta + box-purge controllers (BMP1 in-box vs BMP2 outside).
          Off/On pin the fan at 0%/100% until Auto is restored.
        </p>
      </Panel>

      <Panel title="Power Protection" code="CTL-04" icon={BatteryWarning} tone="amber"
        right={pb.undervoltage
          ? <Chip tone="red">UV TRIPPED</Chip>
          : <Chip tone={pb.uv_threshold != null ? 'green' : 'dim'}>{pb.uv_threshold != null ? 'ARMED' : 'N/A'}</Chip>}>
        <VoltStepper label="Cut-off Below" value={pb.uv_threshold} draft={uvThrDraft} onDraft={setUvThrDraft} lo={9.0} hi={12.5} />
        <VoltStepper label="Recover Above" value={pb.uv_recover} draft={uvRecDraft} onDraft={setUvRecDraft} lo={9.2} hi={13.0} />
        {uvDirty && !uvValid && (
          <p className="text-[0.65rem] text-hud-red text-center">recover must be ≥ cut-off + 0.2 V</p>
        )}
        <div className="flex gap-3">
          <Btn className="flex-1 py-2" tone="amber" disabled={!uvDirty || !uvValid || uvBusy} onClick={applyUv}>
            {uvBusy ? '…' : 'Apply'}
          </Btn>
          <Btn className="flex-1 py-2" disabled={!uvDirty || uvBusy}
            onClick={() => { setUvThrDraft(null); setUvRecDraft(null); }}>
            Revert
          </Btn>
        </div>
        <p className="text-xs text-slate-600 text-center">
          Cuts POCO power after 5 s below cut-off. Persists across restarts.
          Firmware last-resort backstop stays at 10.0 V.
        </p>
      </Panel>

      <Panel title="System Status" code="SYS-01" icon={LayoutDashboard} className="md:col-span-2">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-1.5">
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
  );
}
