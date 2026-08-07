import { useEffect, useState } from 'react';
import type { Tone } from './tones';

export interface Toast {
  id: number;
  text: string;
  tone: Tone;
}

type Listener = (toasts: Toast[]) => void;

let toasts: Toast[] = [];
let nextId = 1;
const listeners = new Set<Listener>();
const emit = () => listeners.forEach((l) => l(toasts));

/** Fire-and-forget notification; keeps at most the 4 newest. */
export function toast(text: string, tone: Tone = 'cyan', ttlMs = 4000) {
  const t: Toast = { id: nextId++, text, tone };
  toasts = [...toasts.slice(-3), t];
  emit();
  window.setTimeout(() => {
    toasts = toasts.filter((x) => x.id !== t.id);
    emit();
  }, ttlMs);
}

export function useToasts(): Toast[] {
  const [list, setList] = useState<Toast[]>(toasts);
  useEffect(() => {
    listeners.add(setList);
    return () => {
      listeners.delete(setList);
    };
  }, []);
  return list;
}
