// The tip on Today: at most one suggestion, dismissible, never more than one a day
// (docs/spec/02-ux.md section 4.1). What was shown is remembered on this device.
import type { Status, Suggestion } from '../home/types';

export const TIP_STORAGE_KEY = 'muse.tip';

export interface TipMemory {
  /** The local day the tip was shown, YYYY-MM-DD. */
  day: string;
  /** The service suggested that day. */
  id: string;
  dismissed: boolean;
}

export function parseTipMemory(raw: string | null): TipMemory | null {
  if (!raw) return null;
  try {
    const v = JSON.parse(raw) as Partial<TipMemory> | null;
    if (v && typeof v.day === 'string' && typeof v.id === 'string' && typeof v.dismissed === 'boolean') return { day: v.day, id: v.id, dismissed: v.dismissed };
  } catch {
    /* not ours */
  }
  return null;
}

/**
 * The tip to show now, or null.
 * - Nothing connected: no tip (the screen already says "Connect your first service").
 * - A tip was dismissed today: none until tomorrow.
 * - A tip was already shown today: the same one, as long as the home still suggests it.
 * - Otherwise the home's first suggestion.
 */
export function chooseTip(status: Pick<Status, 'suggestions'> | null, connected: number, today: string, memory: TipMemory | null): Suggestion | null {
  if (!status || connected === 0) return null;
  const list = (status.suggestions ?? []).filter((s) => s && typeof s.id === 'string' && typeof s.name === 'string');
  if (list.length === 0) return null;
  if (memory && memory.day === today) {
    if (memory.dismissed) return null;
    return list.find((s) => s.id === memory.id) ?? null;
  }
  return list[0] ?? null;
}
