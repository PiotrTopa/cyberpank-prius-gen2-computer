export const cx = (...c: (string | false | undefined | null)[]) => c.filter(Boolean).join(' ');

export const fmtAge = (ts?: number | null, now = Date.now()): string => {
  if (ts == null || ts === 0) return '--';
  const s = Math.max(0, Math.floor(now / 1000 - ts));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
};
