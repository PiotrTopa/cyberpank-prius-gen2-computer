/**
 * Pure helpers of the MAP tab (tabs/MapTab): roadsense's map layer (`mapd`,
 * topic `map`) and its fusion with the camera (api topic `fused`), as served
 * through the backend's roadsense front (`/api/v1/roadsense/map`, `/fused`,
 * `/health`) plus the backend's position feeder (`/api/v1/position`,
 * `state.roadsense.position`). Contracts: roadsense docs/ARCHITECTURE.md
 * ("mapd", "api-derived topic fused"), docs/POSITION_FEEDER.md here.
 *
 * Every field is optional: a missing value means "not known". No imports,
 * so `node --test` can exercise this file directly.
 */

// ── shapes (subset of the roadsense messages the tab reads) ────────────────

export interface MapPack { region?: string | null; osm_timestamp?: string | null; built?: string | null; format?: number }
export interface MapGnssIn {
  source?: string | null; fix?: string | null; age_s?: number | null; hacc_m?: number | null;
  sats?: number | null; hdop?: number | null; used?: number; ignored?: number; rejected?: number; resets?: number;
}
export interface MapMotionIn { source?: string | null; tag?: string | null; age_s?: number | null }
export interface MapPosition {
  lat: number; lon: number; heading_deg?: number | null; heading_acc_deg?: number | null;
  speed_mps?: number | null; hacc_m?: number | null; confidence?: number | null; source?: string | null;
  fix_age_s?: number | null; dr_distance_m?: number | null; motion_source?: string | null; odometer_m?: number | null;
}
export interface MapMatch {
  matched?: boolean; confidence?: number | null; ambiguous?: boolean; distance_m?: number | null;
  heading_diff_deg?: number | null; lat?: number; lon?: number; bearing_deg?: number | null;
  edge?: string; dir?: number; offset_m?: number | null; candidates?: number;
}
export interface MapRoad {
  class?: string | null; name?: string | null; ref?: string | null; oneway?: boolean;
  lanes?: { total?: number | null; forward?: number | null; backward?: number | null; source?: string | null } | null;
  tunnel?: boolean; bridge?: boolean; roundabout?: boolean; link?: boolean; motorroad?: boolean;
  expressway?: boolean; dual_carriageway?: boolean; lit?: boolean | null; surface?: string | null;
  built_up?: { value?: boolean | null; source?: string | null } | null;
}
export interface AheadFeature { kind: string; dist_m: number; confidence?: number | null }
export interface AheadJunction {
  dist_m: number; kind?: string; branches?: number; turn_deg?: number | null; minor?: boolean; confidence?: number | null;
}
export interface AheadCurve {
  start_m: number; apex_m?: number; end_m?: number; min_radius_m?: number | null; direction?: string; angle_deg?: number | null;
}
export interface AheadLimitChange {
  dist_m: number; limit_kmh?: number | null; source?: string | null; rule?: string | null; detail?: string | null; confidence?: number | null;
}
export interface AheadTunnel { in_tunnel?: boolean; entry_m?: number | null; length_m?: number | null; exit_m?: number | null }
export interface AheadShape { s: number[]; x: number[]; y: number[]; curvature?: number[] }
export interface MapAhead {
  horizon_m?: number; path_confidence?: number | null;
  features?: AheadFeature[] | null; junctions?: AheadJunction[] | null; curves?: AheadCurve[] | null;
  tunnel?: AheadTunnel | null;
  built_up_change?: { dist_m: number; to?: boolean; source?: string | null; confidence?: number | null } | null;
  speed_limit_changes?: AheadLimitChange[] | null;
  road_change?: { dist_m: number; class?: string | null; name?: string | null; ref?: string | null } | null;
  shape?: AheadShape | null;
}
export interface MapMsg {
  state?: string; available?: boolean; reason?: string | null; pack?: MapPack | null;
  input?: { gnss?: MapGnssIn | null; motion?: MapMotionIn | null; errors?: number; last_error?: string | null } | null;
  position?: MapPosition | null; match?: MapMatch | null; road?: MapRoad | null;
  speed_limit?: { limit_kmh?: number | null; source?: string; rule?: string | null; detail?: string | null; confidence?: number | null; variable?: boolean } | null;
  ahead?: MapAhead | null;
  proc?: { step_ms?: number; cpu_pct?: number; feeders?: number; tile_loads?: number } | null;
}

export interface FusedLimit {
  limit_kmh?: number | null; source?: string; confidence?: number | null; rule?: string | null; sign?: string | null;
  agreement?: string; note?: string | null; variable?: boolean;
  vision?: { limit_kmh?: number | null; kind?: string | null; sign?: string | null; rule?: string | null; confidence?: number | null } | null;
  map?: { limit_kmh?: number | null; source?: string | null; rule?: string | null; detail?: string | null; confidence?: number | null } | null;
}
export interface FusedMsg {
  map?: { available?: boolean; state?: string | null; reason?: string | null; age_s?: number | null; match_confidence?: number | null } | null;
  speed_limit?: FusedLimit | null;
  built_up?: { value?: boolean | null; source?: string; confidence?: number | null;
    vision?: { value?: boolean | null; source?: string | null; confidence?: number | null } | null;
    map?: { value?: boolean | null; source?: string | null } | null } | null;
  place?: { label?: string | null; confidence?: number | null; source?: string;
    vision?: { label?: string | null; confidence?: number | null } | null;
    map?: { label?: string | null } | null } | null;
  lanes?: { count?: number | null; forward?: number | null; backward?: number | null; ego_index?: number | null;
    oncoming_left?: boolean | null; source?: string | null; confidence?: number | null;
    vision?: { lane_to_left?: boolean | null; lane_to_right?: boolean | null } | null;
    map?: { total?: number | null; forward?: number | null; backward?: number | null; source?: string | null } | null } | null;
  tunnel?: { in_tunnel?: boolean; exit_in_m?: number | null; entry_in_m?: number | null; length_m?: number | null; source?: string;
    vision?: { in_tunnel?: boolean | null; confidence?: number | null } | null;
    map?: { in_tunnel?: boolean | null; exit_m?: number | null; entry_m?: number | null } | null } | null;
  road?: MapRoad | null;
  ahead?: MapAhead | null;
  position?: MapPosition | null;
}

/** Backend position feeder status (`state.roadsense.position`, `GET /api/v1/position`). */
export interface FeederStatus {
  enabled?: boolean; source?: string; synthetic?: boolean; base_source?: string;
  synthetic_control?: boolean;
  session?: { by?: string; started?: number; until?: number } | null;
  last_session_end?: { reason?: string; at?: number } | null;
  active?: boolean; suspended?: string | null; connected?: boolean; socket?: string;
  sent?: number; dropped?: number; connects?: number; motion_sent?: number; gnss_sent?: number; none_sent?: number;
  errors?: number; last_error?: string | null;
  motion?: { yaw_input?: string | null; can_age_s?: number | null; can_frames?: number } | null;
  gnss?: { has_fix?: boolean; last_fix_age_s?: number | null; [k: string]: unknown } | null;
  /** REST only */
  raw_fix?: { lat: number; lon: number; hacc_m?: number | null; age_s?: number | null; source?: string } | null;
  pack_file?: { path?: string; file?: string; size_bytes?: number; mtime?: number } | null;
}

export type Tone = 'amber' | 'cyan' | 'green' | 'red' | 'violet' | 'magenta' | 'dim';

// ── wording ─────────────────────────────────────────────────────────────────

const REASONS: Record<string, string> = {
  no_mapd: 'mapd not running',
  stale: 'map message stale',
  no_pack: 'no map pack loaded',
  no_position: 'no position (no GNSS fix yet)',
  position_unknown: 'position lost (dead reckoning ran out)',
  off_road: 'not on a mapped road',
  low_match_confidence: 'map match below 0.5',
};

export function reasonText(r?: string | null): string {
  if (!r) return '';
  return REASONS[r] ?? r.replace(/_/g, ' ');
}

export const STATE_TONE: Record<string, Tone> = {
  good: 'green', dead_reckoning: 'amber', no_source: 'dim', unknown: 'red',
};

export function fmtM(m?: number | null): string {
  if (m == null || !Number.isFinite(m)) return '--';
  if (Math.abs(m) >= 1000) return `${(m / 1000).toFixed(m >= 10000 ? 0 : 1)} km`;
  return `${Math.round(m)} m`;
}

export function fmtBytes(b?: number | null): string {
  if (b == null) return '--';
  if (b >= 1 << 30) return `${(b / (1 << 30)).toFixed(2)} GB`;
  if (b >= 1 << 20) return `${(b / (1 << 20)).toFixed(0)} MB`;
  return `${(b / 1024).toFixed(0)} kB`;
}

// ── banner ──────────────────────────────────────────────────────────────────

export interface Banner { tone: Tone; title: string; detail?: string }

/** Minutes after a synthetic session in which a dead-reckoned position may still be the synthetic one. */
export const RESIDUE_WINDOW_S = 15 * 60;

/**
 * What the top of the tab says. `rsRunning` = the backend's roadsense manager
 * reports the stack running; `apiDown` = the roadsense front answered 502/503
 * (stack stopped or starting); `mapdDown` = the api is up but has no map
 * message / mapd is not connected. `nowS` = backend wall clock in seconds.
 */
export function mapBanners(o: {
  rsState?: string | null; apiDown?: boolean; mapdDown?: boolean;
  map?: MapMsg | null; fused?: FusedMsg | null; feeder?: FeederStatus | null; nowS: number;
}): Banner[] {
  const out: Banner[] = [];
  const st = o.rsState ?? '';
  const feeder = o.feeder;
  if (st && st !== 'running' && st !== 'grace' && st !== 'starting') {
    return [{ tone: 'dim', title: 'roadsense not running', detail: `stack ${st.toUpperCase()}: parked or switched off; nothing to show` }];
  }
  if (o.apiDown) {
    return [{ tone: 'dim', title: 'roadsense api not answering', detail: st === 'starting' ? 'stack starting' : 'no data from the stack' }];
  }
  const gnssSrc = o.map?.input?.gnss?.source;
  if (feeder?.synthetic || gnssSrc === 'synthetic') {
    out.push({ tone: 'magenta', title: 'SYNTHETIC POSITION', detail: feeder?.session
      ? 'test drive started from the dashboard; not the car\'s real position'
      : feeder?.synthetic ? 'feeder configured with BACKEND_POSITION_SOURCE=synthetic' : 'mapd still hears a synthetic receiver' });
  } else if (feeder?.last_session_end?.at != null && o.map?.state === 'dead_reckoning'
    && o.nowS - feeder.last_session_end.at < RESIDUE_WINDOW_S) {
    out.push({ tone: 'magenta', title: 'synthetic residue', detail: 'mapd is dead-reckoning from the synthetic place (no reset in the position API); the MFD ignores the map until mapd loses or replaces the position' });
  }
  if (o.mapdDown) {
    out.push({ tone: 'amber', title: 'VISION ONLY', detail: 'mapd not running: no map layer' });
    return out;
  }
  const fm = o.fused?.map;
  if (fm && fm.available === false) {
    out.push({ tone: 'amber', title: 'VISION ONLY', detail: reasonText(fm.reason) || 'map layer unavailable' });
  } else if (o.map?.state === 'dead_reckoning') {
    out.push({ tone: 'amber', title: 'DEAD RECKONING', detail: `${fmtM(o.map.position?.dr_distance_m)} since the last fix` });
  } else if (fm?.available) {
    out.push({ tone: 'green', title: 'MAP + CAMERA', detail: `match ${pct(fm.match_confidence)}` });
  }
  return out;
}

function pct(v?: number | null): string {
  return v == null ? '--' : `${Math.round(v * 100)}%`;
}

// ── road ahead ──────────────────────────────────────────────────────────────

export interface AheadItem { dist_m: number; kind: string; label: string; detail: string; tone: Tone; conf?: number | null }

const FEATURE_LABEL: Record<string, string> = {
  traffic_signals: 'traffic signals', crossing: 'crossing', crossing_signals: 'signalled crossing',
  level_crossing: 'level crossing', tram_crossing: 'tram crossing', stop: 'stop sign', give_way: 'give way',
  mini_roundabout: 'mini roundabout', speed_bump: 'speed bump', toll_booth: 'toll booth',
  motorway_junction: 'motorway junction',
};

function turnText(t?: number | null): string {
  if (t == null) return '';
  if (Math.abs(t) < 15) return 'straight on';
  return `${t > 0 ? 'right' : 'left'} ${Math.abs(Math.round(t))}°`;
}

/** Everything ahead in one list, nearest first. */
export function aheadItems(a?: MapAhead | null): AheadItem[] {
  if (!a) return [];
  const out: AheadItem[] = [];
  for (const j of a.junctions ?? []) {
    const kind = j.kind ?? 'junction';
    out.push({ dist_m: j.dist_m, kind: 'junction', tone: j.minor ? 'dim' : 'cyan', conf: j.confidence,
      label: kind.replace(/_/g, ' '),
      detail: [j.branches != null ? `${j.branches} branches` : '', turnText(j.turn_deg), j.minor ? 'service roads only' : '']
        .filter(Boolean).join(' · ') });
  }
  for (const c of a.curves ?? []) {
    out.push({ dist_m: c.start_m, kind: 'curve', tone: (c.min_radius_m ?? 999) < 100 ? 'amber' : 'cyan',
      label: `curve ${c.direction ?? ''}`.trim(),
      detail: [c.min_radius_m != null ? `R ${Math.round(c.min_radius_m)} m` : '',
        c.angle_deg != null ? `${Math.abs(Math.round(c.angle_deg))}°` : '',
        c.end_m != null ? `${Math.round(c.end_m - c.start_m)} m long` : ''].filter(Boolean).join(' · ') });
  }
  for (const f of a.features ?? []) {
    out.push({ dist_m: f.dist_m, kind: 'feature', tone: 'violet', conf: f.confidence,
      label: FEATURE_LABEL[f.kind] ?? f.kind.replace(/_/g, ' '), detail: '' });
  }
  for (const l of a.speed_limit_changes ?? []) {
    out.push({ dist_m: l.dist_m, kind: 'limit', tone: 'red', conf: l.confidence,
      label: l.limit_kmh != null ? `limit ${l.limit_kmh} km/h` : 'limit ends',
      detail: [l.source === 'implicit' ? `implicit${l.rule ? ` (${l.rule.replace(/_/g, ' ')})` : ''}` : l.source ?? '',
        l.detail ?? ''].filter(Boolean).join(' · ') });
  }
  const t = a.tunnel;
  if (t) {
    if (!t.in_tunnel && t.entry_m != null) {
      out.push({ dist_m: t.entry_m, kind: 'tunnel', tone: 'violet', label: 'tunnel entry',
        detail: t.length_m != null ? `${fmtM(t.length_m)} long` : '' });
    }
    if (t.exit_m != null) {
      out.push({ dist_m: t.exit_m, kind: 'tunnel', tone: 'violet', label: 'tunnel exit', detail: t.in_tunnel ? 'in the tunnel now' : '' });
    }
  }
  const b = a.built_up_change;
  if (b) {
    out.push({ dist_m: b.dist_m, kind: 'built_up', tone: 'green', conf: b.confidence,
      label: b.to ? 'built-up area begins' : 'built-up area ends', detail: b.source ?? '' });
  }
  const r = a.road_change;
  if (r) {
    out.push({ dist_m: r.dist_m, kind: 'road', tone: 'dim', label: 'road changes',
      detail: [r.ref, r.name, r.class].filter(Boolean).join(' · ') });
  }
  return out.sort((x, y) => x.dist_m - y.dist_m);
}

// ── fused facts ─────────────────────────────────────────────────────────────

export type Agreement = 'agree' | 'conflict' | 'vision_only' | 'map_only' | 'none';
export interface FusedRow { fact: string; value: string; source: string; vision: string; map: string; agree: Agreement; note?: string }

function yn(v?: boolean | null): string {
  return v == null ? '--' : v ? 'yes' : 'no';
}

function cmp<T>(v: T | null | undefined, m: T | null | undefined): Agreement {
  const hv = v != null, hm = m != null;
  if (hv && hm) return v === m ? 'agree' : 'conflict';
  return hv ? 'vision_only' : hm ? 'map_only' : 'none';
}

export function fusedRows(f?: FusedMsg | null): FusedRow[] {
  if (!f) return [];
  const rows: FusedRow[] = [];
  const sl = f.speed_limit;
  if (sl) {
    const v = sl.vision, m = sl.map;
    rows.push({
      fact: 'speed limit',
      value: sl.limit_kmh != null ? `${sl.limit_kmh} km/h` : '--',
      source: sl.source ?? '--',
      vision: v?.limit_kmh != null ? `${v.limit_kmh} · ${[v.kind, v.sign ?? v.rule].filter(Boolean).join(' ')}` : '--',
      map: m?.limit_kmh != null ? `${m.limit_kmh} · ${[m.source, m.rule ?? m.detail].filter(Boolean).join(' ')}` : '--',
      agree: (sl.agreement as Agreement) ?? cmp(v?.limit_kmh, m?.limit_kmh),
      note: sl.note ?? (sl.variable ? 'variable limit (signs may change)' : undefined),
    });
  }
  const bu = f.built_up;
  if (bu) {
    rows.push({ fact: 'built-up', value: yn(bu.value), source: bu.source ?? '--',
      vision: bu.vision ? `${yn(bu.vision.value)} · ${bu.vision.source ?? ''}` : '--',
      map: bu.map ? `${yn(bu.map.value)} · ${bu.map.source ?? ''}` : '--',
      agree: cmp(bu.vision?.value, bu.map?.value) });
  }
  const pl = f.place;
  if (pl) {
    rows.push({ fact: 'place', value: pl.label?.replace(/_/g, ' ') ?? '--', source: pl.source ?? '--',
      vision: pl.vision?.label ? `${pl.vision.label.replace(/_/g, ' ')} · ${pct(pl.vision.confidence)}` : '--',
      map: pl.map?.label?.replace(/_/g, ' ') ?? '--',
      agree: cmp(pl.vision?.label, pl.map?.label) });
  }
  const ln = f.lanes;
  if (ln) {
    const vis = ln.vision;
    const side = vis ? [vis.lane_to_left ? 'lane left' : vis.lane_to_left === false ? 'no lane left' : '',
      vis.lane_to_right ? 'lane right' : vis.lane_to_right === false ? 'no lane right' : ''].filter(Boolean).join(' · ') : '';
    const m = ln.map;
    // a lane seen on both sides needs at least 2 own-direction lanes unless the left one is oncoming
    const needOwn = vis ? 1 + (vis.lane_to_right ? 1 : 0) + (vis.lane_to_left && ln.oncoming_left === false ? 1 : 0) : null;
    const agree: Agreement = !vis || needOwn == null ? (m ? 'map_only' : 'none')
      : m?.forward == null ? 'vision_only' : needOwn > m.forward ? 'conflict' : 'agree';
    rows.push({
      fact: 'lanes',
      value: ln.count != null ? `${ln.count}${ln.forward != null ? ` (${ln.forward} own)` : ''}${ln.ego_index != null ? ` · ego ${ln.ego_index + 1}` : ''}` : '--',
      source: ln.source ?? '--',
      vision: side || '--',
      map: m ? `${m.total ?? '?'} = ${m.forward ?? '?'} fwd + ${m.backward ?? '?'} back · ${m.source ?? ''}` : '--',
      agree,
      note: ln.oncoming_left ? 'lane to the left is oncoming' : undefined,
    });
  }
  const tn = f.tunnel;
  if (tn) {
    rows.push({ fact: 'tunnel', value: tn.in_tunnel ? `in tunnel${tn.exit_in_m != null ? ` · exit ${fmtM(tn.exit_in_m)}` : ''}` : tn.entry_in_m != null ? `entry ${fmtM(tn.entry_in_m)}` : 'no',
      source: tn.source ?? '--',
      vision: tn.vision ? `${yn(tn.vision.in_tunnel)}${tn.vision.confidence != null ? ` · ${pct(tn.vision.confidence)}` : ''}` : '--',
      map: tn.map ? yn(tn.map.in_tunnel) : '--',
      agree: cmp(tn.vision?.in_tunnel ?? null, tn.map?.in_tunnel ?? null) });
  }
  return rows;
}

/**
 * While a camera-read sign is in force with the map available
 * (`sign` = the vision engine still holds it, `sign_held` = the map holds it
 * after the engine dropped it), what will release it, with distances from
 * the look-ahead (roadsense fusion rules, ARCHITECTURE.md "Speed limit").
 */
export function holdRelease(f?: FusedMsg | null): { held: boolean; by: string; lines: string[] } | null {
  const sl = f?.speed_limit;
  if (!sl || (sl.source !== 'sign' && sl.source !== 'sign_held')) return null;
  const by = sl.source === 'sign_held' ? 'held by the map (the camera engine has dropped it)' : 'read by the camera, in force';
  if (!f?.map?.available) return { held: false, by: 'camera only (no map): the vision engine decides when it ends', lines: [] };
  const a = f.ahead ?? {};
  const road = f.road ?? {};
  const lines: string[] = [];
  if (road.class === 'motorway' || road.expressway || road.motorroad) {
    lines.push('motorway / expressway: lasts until a sign cancels it (no junction release)');
  } else {
    const j = (a.junctions ?? []).filter((x) => !x.minor).sort((x, y) => x.dist_m - y.dist_m)[0];
    lines.push(j ? `next real junction in ${fmtM(j.dist_m)} (${(j.kind ?? 'junction').replace(/_/g, ' ')})`
      : 'next real junction: none within the horizon');
  }
  const lc = (a.speed_limit_changes ?? []).filter((x) => x.source !== 'implicit').sort((x, y) => x.dist_m - y.dist_m)[0];
  lines.push(lc ? `tagged map limit changes in ${fmtM(lc.dist_m)} (to ${lc.limit_kmh ?? '--'} km/h; releases if it is a third value, 100 m past the sign)`
    : 'tagged map limit change: none ahead');
  const b = a.built_up_change;
  lines.push(b ? `built-up boundary in ${fmtM(b.dist_m)} (${b.source ?? '?'}${b.source === 'landuse' ? ': inferred, releases only when tagged or signed' : ''})`
    : 'built-up boundary: none ahead');
  lines.push('5 km after the sign at the latest');
  return { held: sl.source === 'sign_held', by, lines };
}

// ── mini map geometry ───────────────────────────────────────────────────────

const M_PER_DEG_LAT = 111132.954;
const M_PER_DEG_LON_EQ = 111319.49;

/** East/north metres of (lat, lon) from (lat0, lon0) (local tangent plane). */
export function enu(lat0: number, lon0: number, lat: number, lon: number): [number, number] {
  const e = (lon - lon0) * M_PER_DEG_LON_EQ * Math.cos((lat0 * Math.PI) / 180);
  const n = (lat - lat0) * M_PER_DEG_LAT;
  return [e, n];
}

/** East/north -> heading-up [right, forward] for a heading in degrees clockwise from north. */
export function headingUp(en: [number, number], headingDeg: number): [number, number] {
  const h = (headingDeg * Math.PI) / 180;
  const [e, n] = en;
  return [e * Math.cos(h) - n * Math.sin(h), e * Math.sin(h) + n * Math.cos(h)];
}

/** A point of the road-ahead shape at path distance s (linear), as [right, forward]; null outside.
 *  The shape's frame (mapd context._shape): x forward along the matched bearing, y right, origin at
 *  the matched position. */
export function shapeAt(shape: AheadShape | null | undefined, s: number): [number, number] | null {
  if (!shape || !shape.s?.length) return null;
  const S = shape.s;
  if (s < S[0] || s > S[S.length - 1]) return null;
  for (let i = 1; i < S.length; i++) {
    if (s <= S[i]) {
      const t = S[i] === S[i - 1] ? 0 : (s - S[i - 1]) / (S[i] - S[i - 1]);
      return [shape.y[i - 1] + t * (shape.y[i] - shape.y[i - 1]), shape.x[i - 1] + t * (shape.x[i] - shape.x[i - 1])];
    }
  }
  return [shape.y[S.length - 1], shape.x[S.length - 1]];
}

/** Shape points between s0 and s1 as [right, forward] pairs. */
export function shapeSlice(shape: AheadShape | null | undefined, s0: number, s1: number): [number, number][] {
  if (!shape || !shape.s?.length) return [];
  const out: [number, number][] = [];
  const a = shapeAt(shape, Math.max(s0, shape.s[0]));
  if (a) out.push(a);
  shape.s.forEach((s, i) => { if (s > s0 && s < s1) out.push([shape.y[i], shape.x[i]]); });
  const b = shapeAt(shape, Math.min(s1, shape.s[shape.s.length - 1]));
  if (b) out.push(b);
  return out;
}

/** Distance in metres between two lat/lon points (local plane; fine below a few km). */
export function distM(a: { lat: number; lon: number }, b: { lat: number; lon: number }): number {
  const [e, n] = enu(a.lat, a.lon, b.lat, b.lon);
  return Math.hypot(e, n);
}
