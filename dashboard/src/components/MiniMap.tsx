import { useState } from 'react';
import type { FeederStatus, MapMsg } from '../lib/mapLayer';
import { enu, fmtM, headingUp, shapeAt, shapeSlice } from '../lib/mapLayer';
import { cx } from '../lib/format';

/**
 * Heading-up sketch of what roadsense's map layer knows, drawn only from its
 * own output: no tiles, no map data in the page. Origin = the matched point
 * (or the filtered position when nothing is matched), "up" = the matched
 * road bearing (the frame of `ahead.shape`), so the road ahead, junctions,
 * curves and features sit where mapd put them; the filtered position (amber
 * chevron), the newest raw fix (magenta cross, REST only) and this browser's
 * trail of filtered positions are placed relative to it.
 */

const W = 360;
const H = 300;
const OX = W / 2;
const OY = H - 52;
const RANGES = [60, 250, 600] as const;
type Range = (typeof RANGES)[number];

const C = {
  grid: '#1d2531', ring: '#3b4757', road: '#6ee7f2', roadDim: 'rgba(110,231,242,0.35)', curve: '#ffb454',
  tunnel: '#a78bfa', junction: '#6ee7f2', feature: '#a78bfa', limit: '#ff5c57', builtUp: '#47d17c',
  match: '#47d17c', ego: '#ffb454', raw: '#e879c8', trail: 'rgba(255,180,84,0.35)', label: '#64748b', text: '#cbd5e1',
};

export function MiniMap({ map, feeder, trail }: {
  map: MapMsg | null;
  feeder: FeederStatus | null;
  /** this browser session's filtered positions, oldest first */
  trail: { lat: number; lon: number }[];
}) {
  const [range, setRange] = useState<Range>(250);
  const k = (OY - 18) / range;           // px per metre
  const pos = map?.position ?? null;
  const match = map?.match?.matched && map.match.lat != null && map.match.lon != null ? map.match : null;
  const origin = match ? { lat: match.lat as number, lon: match.lon as number } : pos;
  const up = match?.bearing_deg ?? pos?.heading_deg ?? 0;
  const shape = match ? map?.ahead?.shape ?? null : null;
  const ahead = match ? map?.ahead ?? null : null;

  const P = (rf: [number, number] | null): [number, number] | null => (rf ? [OX + rf[0] * k, OY - rf[1] * k] : null);
  const rel = (lat: number, lon: number): [number, number] | null =>
    origin ? headingUp(enu(origin.lat, origin.lon, lat, lon), up) : null;
  const path = (pts: [number, number][]) => pts.map((p, i) => `${i ? 'L' : 'M'}${(OX + p[0] * k).toFixed(1)},${(OY - p[1] * k).toFixed(1)}`).join(' ');

  const egoRel = pos ? rel(pos.lat, pos.lon) : null;
  const ego = P(egoRel);
  const egoRot = (pos?.heading_deg ?? up) - up;
  const raw = feeder?.raw_fix && origin ? P(rel(feeder.raw_fix.lat, feeder.raw_fix.lon)) : null;
  const trailPts = origin
    ? trail.map((p) => rel(p.lat, p.lon)).filter((p): p is [number, number] => p != null)
    : [];

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[0.6rem] text-slate-600 uppercase tracking-wider">
          heading-up · {match ? 'origin = matched point' : pos ? 'origin = filtered position (no match)' : 'no position'}
        </span>
        <div className="flex gap-1">
          {RANGES.map((r) => (
            <button key={r} onClick={() => setRange(r)}
              className={cx('border px-2 py-0.5 text-[0.6rem] uppercase tracking-wider tnum',
                range === r ? 'border-hud-cyan/50 text-hud-cyan' : 'border-ink-700 text-slate-500 hover:text-slate-200')}>
              {r} m
            </button>
          ))}
        </div>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full bg-ink-850 border border-ink-700" role="img"
        aria-label="map layer sketch">
        <defs>
          <clipPath id="mm-clip"><rect x="0" y="0" width={W} height={H} /></clipPath>
        </defs>
        <g clipPath="url(#mm-clip)" fill="none">
          {/* range rings */}
          {[1, 2, 3].map((i) => {
            const r = (range * i / 3) * k;
            return (
              <g key={i}>
                <circle cx={OX} cy={OY} r={r} stroke={C.grid} strokeWidth={1} />
                <text x={OX + 3} y={OY - r - 2} fill={C.label} fontSize={9} className="tnum">{fmtM(range * i / 3)}</text>
              </g>
            );
          })}
          <line x1={OX} y1={0} x2={OX} y2={H} stroke={C.grid} strokeDasharray="2 4" />
          {/* trail */}
          {trailPts.length > 1 && <path d={path(trailPts)} stroke={C.trail} strokeWidth={1.5} />}
          {/* road ahead */}
          {shape && <path d={path(shapeSlice(shape, 0, range * 1.3))} stroke={C.roadDim} strokeWidth={9} strokeLinecap="round" />}
          {shape && <path d={path(shapeSlice(shape, 0, range * 1.3))} stroke={C.road} strokeWidth={1.5} />}
          {ahead?.curves?.map((c, i) => {
            const pts = shapeSlice(shape, c.start_m, c.end_m ?? c.start_m + 50);
            return pts.length > 1 ? <path key={`c${i}`} d={path(pts)} stroke={C.curve} strokeWidth={3} /> : null;
          })}
          {ahead?.tunnel && (ahead.tunnel.in_tunnel || ahead.tunnel.entry_m != null) && (() => {
            const t = ahead.tunnel;
            const s0 = t.in_tunnel ? 0 : t.entry_m ?? 0;
            const s1 = t.exit_m ?? s0 + (t.length_m ?? 0);
            const pts = shapeSlice(shape, s0, s1);
            return pts.length > 1 ? <path d={path(pts)} stroke={C.tunnel} strokeWidth={5} strokeDasharray="6 4" /> : null;
          })()}
          {ahead?.built_up_change && (() => {
            const p = P(shapeAt(shape, ahead.built_up_change.dist_m));
            return p ? <line x1={p[0] - 9} y1={p[1]} x2={p[0] + 9} y2={p[1]} stroke={C.builtUp} strokeWidth={2} /> : null;
          })()}
          {ahead?.junctions?.map((j, i) => {
            const p = P(shapeAt(shape, j.dist_m));
            if (!p) return null;
            return (
              <g key={`j${i}`}>
                <circle cx={p[0]} cy={p[1]} r={j.kind?.startsWith('roundabout') ? 6 : 4}
                  stroke={C.junction} strokeWidth={1.5} fill="#0e1219" opacity={j.minor ? 0.45 : 1} />
                <text x={p[0] + 8} y={p[1] + 3} fill={j.minor ? C.label : C.text} fontSize={9} className="tnum">{fmtM(j.dist_m)}</text>
              </g>
            );
          })}
          {ahead?.features?.map((f, i) => {
            const p = P(shapeAt(shape, f.dist_m));
            return p ? <rect key={`f${i}`} x={p[0] - 3} y={p[1] - 3} width={6} height={6} stroke={C.feature}
              transform={`rotate(45 ${p[0]} ${p[1]})`} fill="#0e1219" /> : null;
          })}
          {ahead?.speed_limit_changes?.map((l, i) => {
            const p = P(shapeAt(shape, l.dist_m));
            if (!p) return null;
            return (
              <g key={`l${i}`}>
                <circle cx={p[0] - 16} cy={p[1]} r={8} stroke={C.limit} strokeWidth={l.source === 'implicit' ? 1 : 2} fill="#000" />
                <text x={p[0] - 16} y={p[1] + 3} fill={C.text} fontSize={8} textAnchor="middle" className="tnum">{l.limit_kmh ?? '-'}</text>
              </g>
            );
          })}
          {/* matched point, filtered position, raw fix */}
          {match && <circle cx={OX} cy={OY} r={3} fill={C.match} />}
          {ego && pos?.hacc_m != null && <circle cx={ego[0]} cy={ego[1]} r={Math.max(2, pos.hacc_m * k)} stroke={C.ego} strokeDasharray="2 3" opacity={0.6} />}
          {raw && feeder?.raw_fix?.hacc_m != null && <circle cx={raw[0]} cy={raw[1]} r={Math.max(2, feeder.raw_fix.hacc_m * k)} stroke={C.raw} strokeDasharray="1 3" opacity={0.7} />}
          {raw && (
            <g stroke={C.raw} strokeWidth={1.5}>
              <line x1={raw[0] - 4} y1={raw[1] - 4} x2={raw[0] + 4} y2={raw[1] + 4} />
              <line x1={raw[0] - 4} y1={raw[1] + 4} x2={raw[0] + 4} y2={raw[1] - 4} />
            </g>
          )}
          {ego && (
            <path d="M0,-9 L6,6 L0,2 L-6,6 Z" fill={C.ego} stroke="#05070b" strokeWidth={0.8}
              transform={`translate(${ego[0]} ${ego[1]}) rotate(${egoRot})`} />
          )}
          {!origin && (
            <text x={W / 2} y={H / 2} fill={C.label} fontSize={11} textAnchor="middle" letterSpacing={2}>NO POSITION</text>
          )}
          {/* north arrow */}
          {origin && (
            <g transform={`translate(${W - 18} 18) rotate(${-up})`}>
              <path d="M0,-10 L4,4 L0,1 L-4,4 Z" fill={C.label} />
              <text x={0} y={14} fill={C.label} fontSize={8} textAnchor="middle" transform={`rotate(${up} 0 11)`}>N</text>
            </g>
          )}
        </g>
      </svg>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[0.6rem] uppercase tracking-wider text-slate-500">
        <Key c={C.ego} t="filtered" /><Key c={C.match} t="matched" /><Key c={C.raw} t="raw fix" />
        <Key c={C.road} t="road ahead" /><Key c={C.curve} t="curve" /><Key c={C.tunnel} t="tunnel" />
        <Key c={C.junction} t="junction" /><Key c={C.feature} t="feature" /><Key c={C.limit} t="limit change" />
        <Key c={C.builtUp} t="built-up edge" />
      </div>
    </div>
  );
}

const Key = ({ c, t }: { c: string; t: string }) => (
  <span className="inline-flex items-center gap-1"><span className="h-1.5 w-1.5" style={{ background: c }} />{t}</span>
);
