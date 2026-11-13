// The gauge on Today: one number for today's free allowance across services
// (docs/spec/02-ux.md section 4.1), worked out from the status snapshot.
//
// The rule, kept simple on purpose:
//   1. A service counts when it is connected, switched on, in the cloud, and has
//      at least one allowance that refills daily (a "day" limit with a known size
//      and a known amount left). Trial credits that never refill, paid services
//      and the AI on this computer (no limit) are not part of the bar.
//   2. What a service has left is its tightest daily limit: the smallest
//      remaining/limit among its day limits. A service the home marks as
//      used up counts as 0.
//   3. Each service is weighted by what it can do: the number of different
//      jobs (router aliases such as fast, smart, long-context) it can serve,
//      at least 1.
//   4. The gauge is the weighted average, rounded to a whole percent.
//   5. It is marked "about" when any figure used came from an estimate rather
//      than from the service itself.
//   6. The next refill is the earliest refill time among the services that are
//      not full.
import type { Limit, Provider, Status } from '../home/types';

export interface ServiceShare {
  id: string;
  name: string;
  /** 0..1 of today's allowance left. */
  left: number;
  weight: number;
  estimated: boolean;
  /** Milliseconds since the epoch, or null when the home does not know. */
  refillAt: number | null;
}

export interface Gauge {
  /** Services with a key on the home (any kind). 0 means "nothing connected". */
  connected: number;
  /** Whole percent left, or null when no connected service has a daily allowance. */
  percent: number | null;
  estimated: boolean;
  nextRefill: { at: number; service: string } | null;
  services: ServiceShare[];
}

function dayLimits(p: Provider): Limit[] {
  return (p.limits ?? []).filter(
    (l) => l.window === 'day' && typeof l.limit === 'number' && l.limit > 0 && typeof l.remaining === 'number' && Number.isFinite(l.remaining),
  );
}

function parseTime(t: string | null | undefined): number | null {
  if (!t) return null;
  const ms = Date.parse(t);
  return Number.isNaN(ms) ? null : ms;
}

function weightOf(p: Provider): number {
  const jobs = new Set<string>();
  for (const d of p.deployments ?? []) for (const a of d.aliases ?? []) jobs.add(a);
  return Math.max(1, jobs.size);
}

export function serviceShare(p: Provider): ServiceShare | null {
  if (!p.configured || p.enabled === false || p.kind !== 'cloud') return null;
  const limits = dayLimits(p);
  if (limits.length === 0) return null;
  let left = 1;
  let estimated = false;
  let refillAt: number | null = null;
  for (const l of limits) {
    const share = Math.min(1, Math.max(0, (l.remaining as number) / (l.limit as number)));
    left = Math.min(left, share);
    if (l.source !== 'header') estimated = true;
    const at = parseTime(l.reset_at);
    if (at !== null && (refillAt === null || at < refillAt)) refillAt = at;
  }
  if (p.state === 'exhausted') left = 0;
  return { id: p.id, name: p.name, left, weight: weightOf(p), estimated, refillAt };
}

export function computeGauge(status: Pick<Status, 'providers'>): Gauge {
  const providers = status.providers ?? [];
  const connected = providers.filter((p) => p.configured && p.enabled !== false).length;
  const services = providers.map(serviceShare).filter((s): s is ServiceShare => s !== null);
  if (services.length === 0) return { connected, percent: null, estimated: false, nextRefill: null, services };
  let sum = 0;
  let weights = 0;
  let next: Gauge['nextRefill'] = null;
  for (const s of services) {
    sum += s.left * s.weight;
    weights += s.weight;
    if (s.left < 1 && s.refillAt !== null && (next === null || s.refillAt < next.at)) next = { at: s.refillAt, service: s.name };
  }
  return {
    connected,
    percent: Math.round((100 * sum) / weights),
    estimated: services.some((s) => s.estimated),
    nextRefill: next,
    services,
  };
}

/** Today's spending against the daily limit, in US dollars as the home reports it. */
export interface Spending {
  spentUsd: number;
  capUsd: number;
  /** 0..100 */
  percent: number;
}

export function computeSpending(status: Pick<Status, 'paid'>): Spending {
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : 0);
  const spentUsd = num(status.paid?.spent_usd) + num(status.paid?.held_usd);
  const capUsd = num(status.paid?.cap_usd);
  return { spentUsd, capUsd, percent: capUsd > 0 ? Math.min(100, Math.round((100 * spentUsd) / capUsd)) : 0 };
}
