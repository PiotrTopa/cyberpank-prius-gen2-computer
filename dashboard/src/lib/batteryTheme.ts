// Chart colours of the battery-health tab (dark HUD surface). Series colours
// a/b/c follow the entity; watch / act / ok are reserved for status.
export const C = {
  grid: 'rgba(255,255,255,0.06)', axis: '#64748b', ink: '#cbd5e1', muted: '#94a3b8',
  a: '#6ee7f2', b: '#ffb454', c: '#e879c8', raw: '#64748b',
  watch: '#ffb454', act: '#ff5c57', ok: '#47d17c',
};
export const TICK = { fontSize: 10, fontFamily: 'inherit', fill: C.axis };
export const TIP = {
  contentStyle: { backgroundColor: '#0a0d13', border: '1px solid #3b4757', borderRadius: 0, fontFamily: 'inherit', fontSize: 11 },
  labelStyle: { color: C.muted }, itemStyle: { color: C.ink },
};

