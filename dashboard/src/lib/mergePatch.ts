/** RFC 7386 JSON merge patch (the stream's `delta=1` format, backend api.stream_filter).
 *  null deletes a key; arrays and scalars replace; objects merge recursively.
 *  Returns new objects along the changed paths, so React sees the change. */
export function applyMergePatch(target: unknown, patch: unknown): unknown {
  if (patch === null || typeof patch !== 'object' || Array.isArray(patch)) return patch;
  const base: Record<string, unknown> =
    target !== null && typeof target === 'object' && !Array.isArray(target)
      ? { ...(target as Record<string, unknown>) }
      : {};
  for (const [k, v] of Object.entries(patch as Record<string, unknown>)) {
    if (v === null) delete base[k];
    else base[k] = applyMergePatch(base[k], v);
  }
  return base;
}
