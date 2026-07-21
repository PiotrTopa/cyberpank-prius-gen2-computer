import type { ComponentType } from 'react';
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts';
import type { Series } from '../types';

export function Chart({ title, icon: Icon, data, color, unit }: {
  title: string;
  icon: ComponentType<{ size?: number | string }>;
  data: Series;
  color: string;
  unit: string;
}) {
  return (
    <div className="hud-panel p-4">
      <div className="flex items-center justify-between border-b border-ink-700 pb-2 mb-4">
        <div className="flex items-center gap-2" style={{ color }}>
          <Icon size={14} />
          <h2 className="text-[0.7rem] font-medium uppercase tracking-[0.22em]">{title}</h2>
        </div>
        {data.length > 0 && (
          <span className="text-xs tnum text-slate-500">
            now <span className="text-slate-100">{data[data.length - 1].value}</span> {unit}
          </span>
        )}
      </div>
      <ResponsiveContainer width="100%" height={230}>
        <AreaChart data={data} margin={{ top: 5, right: 8, bottom: 5, left: -18 }}>
          <defs>
            <linearGradient id={`g-${title}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.25} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
          <XAxis dataKey="time" stroke="#64748b" tick={{ fontSize: 10, fontFamily: 'inherit' }} minTickGap={28} />
          <YAxis domain={['auto', 'auto']} stroke="#64748b" tick={{ fontSize: 10, fontFamily: 'inherit' }} width={48} />
          <Tooltip
            contentStyle={{ backgroundColor: '#0a0d13', border: `1px solid ${color}`, borderRadius: 0, fontFamily: 'inherit', fontSize: 12 }}
            itemStyle={{ color }}
            labelStyle={{ color: '#94a3b8' }}
          />
          <Area type="monotone" dataKey="value" stroke={color} strokeWidth={1.5} fill={`url(#g-${title})`} dot={false} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
