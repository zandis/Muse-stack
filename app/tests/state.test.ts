import { describe, expect, it, vi } from 'vitest';
import { computeGauge, computeSpending, serviceShare } from '../src/state/gauge';
import { nextDelay, Poller } from '../src/state/poller';
import { initialState, isPaused, problemOf, reducer, waitingCards, type State } from '../src/state/reducer';
import { chooseTip, parseTipMemory } from '../src/state/tip';
import { CARDS, NOW, provider, status } from './fixtures';

const day = (limit: number, remaining: number, reset = '2026-11-13T08:00:00Z', source = 'header') => ({ window: 'day', metric: 'requests', limit, remaining, reset_at: reset, source });

describe('gauge', () => {
  it('uses the tightest daily limit of a service', () => {
    const s = serviceShare(provider({ limits: [day(100, 80), { ...day(1000, 300), metric: 'tokens' }] }));
    expect(s?.left).toBeCloseTo(0.3);
  });
  it('ignores minute limits, trial credits, paid and local services', () => {
    expect(serviceShare(provider({ limits: [{ window: 'minute', metric: 'requests', limit: 10, remaining: 0, reset_at: null }] }))).toBeNull();
    expect(serviceShare(provider({ limits: [{ window: 'month', metric: 'requests', limit: 1000, remaining: 0, reset_at: null }] }))).toBeNull();
    expect(serviceShare(provider({ kind: 'local', limits: [day(10, 10)] }))).toBeNull();
    expect(serviceShare(provider({ configured: false, limits: [day(10, 10)] }))).toBeNull();
    expect(serviceShare(provider({ enabled: false, limits: [day(10, 10)] }))).toBeNull();
    expect(serviceShare(provider({ limits: [{ window: 'day', metric: 'tokens', limit: null, remaining: null, reset_at: null }] }))).toBeNull();
  });
  it('weights each service by the number of different jobs it can do', () => {
    const g = computeGauge({
      providers: [
        provider({ id: 'a', limits: [day(100, 100)], deployments: [{ id: '1', aliases: ['fast', 'cheap'] }, { id: '2', aliases: ['smart', 'fast'] }] }), // weight 3, full
        provider({ id: 'b', limits: [day(100, 0)], deployments: [{ id: '3', aliases: ['fast'] }] }), // weight 1, empty
      ],
    });
    expect(g.percent).toBe(75);
    expect(g.connected).toBe(2);
  });
  it('counts a service the home marks as used up as zero', () => {
    expect(computeGauge({ providers: [provider({ state: 'exhausted', limits: [day(100, 40)] })] }).percent).toBe(0);
  });
  it('marks the gauge as an estimate when any figure is not from the service itself', () => {
    expect(computeGauge({ providers: [provider({ limits: [day(100, 50)] })] }).estimated).toBe(false);
    expect(computeGauge({ providers: [provider({ limits: [day(100, 50, '2026-11-13T08:00:00Z', 'estimate')] })] }).estimated).toBe(true);
  });
  it('gives the earliest refill among services that are not full', () => {
    const g = computeGauge({
      providers: [
        provider({ id: 'a', name: 'A', limits: [day(100, 100, '2026-11-13T07:00:00Z')] }), // full: its refill does not matter
        provider({ id: 'b', name: 'B', limits: [day(100, 10, '2026-11-13T09:00:00Z')] }),
        provider({ id: 'c', name: 'C', limits: [day(100, 10, '2026-11-13T08:00:00Z')] }),
      ],
    });
    expect(g.nextRefill).toEqual({ at: Date.parse('2026-11-13T08:00:00Z'), service: 'C' });
  });
  it('says nothing is connected, or that there is no daily allowance', () => {
    expect(computeGauge({ providers: [provider({ configured: false })] })).toMatchObject({ connected: 0, percent: null });
    expect(computeGauge({ providers: [provider({ kind: 'local' })] })).toMatchObject({ connected: 1, percent: null });
  });
  it('clamps odd figures', () => {
    expect(computeGauge({ providers: [provider({ limits: [day(100, 250)] })] }).percent).toBe(100);
    expect(computeGauge({ providers: [provider({ limits: [day(100, -5)] })] }).percent).toBe(0);
  });
});

describe('spending', () => {
  it('adds what is held to what is spent, against the limit', () => {
    expect(computeSpending({ paid: { cap_usd: 1, spent_usd: 0.25, held_usd: 0.25 } })).toEqual({ spentUsd: 0.5, capUsd: 1, percent: 50 });
  });
  it('copes with no limit and with bad numbers', () => {
    expect(computeSpending({ paid: { cap_usd: 0, spent_usd: 0 } }).percent).toBe(0);
    expect(computeSpending({ paid: { cap_usd: Number.NaN, spent_usd: -3 } })).toEqual({ spentUsd: 0, capUsd: 0, percent: 0 });
    expect(computeSpending({ paid: { cap_usd: 1, spent_usd: 5 } }).percent).toBe(100);
  });
});

describe('polling', () => {
  it('backs off after failures up to a ceiling, and honours retry_after', () => {
    expect(nextDelay(0)).toBe(5000);
    expect(nextDelay(1)).toBe(10000);
    expect(nextDelay(2)).toBe(20000);
    expect(nextDelay(20)).toBe(60000);
    expect(nextDelay(0, 30)).toBe(30000);
    expect(nextDelay(0, 9999)).toBe(60000);
  });

  function harness(visible = true) {
    const timers: { fn: () => void; ms: number }[] = [];
    const state = { visible, results: [] as boolean[] };
    const run = vi.fn(async () => ({ ok: state.results.shift() ?? true }));
    const poller = new Poller({ run, isVisible: () => state.visible, setTimer: (fn, ms) => timers.push({ fn, ms }), clearTimer: (id) => void timers.splice((id as number) - 1, 1) });
    const flush = () => new Promise((r) => setTimeout(r, 0));
    return { timers, state, run, poller, flush };
  }

  it('asks at once, then on the interval', async () => {
    const h = harness();
    h.poller.start();
    await h.flush();
    expect(h.run).toHaveBeenCalledTimes(1);
    expect(h.timers.at(-1)?.ms).toBe(5000);
    h.timers.at(-1)?.fn();
    await h.flush();
    expect(h.run).toHaveBeenCalledTimes(2);
  });
  it('slows down while the home fails and recovers after a success', async () => {
    const h = harness();
    h.state.results = [false, false, true];
    h.poller.start();
    await h.flush();
    expect(h.timers.at(-1)?.ms).toBe(10000);
    h.timers.at(-1)?.fn();
    await h.flush();
    expect(h.timers.at(-1)?.ms).toBe(20000);
    h.timers.at(-1)?.fn();
    await h.flush();
    expect(h.timers.at(-1)?.ms).toBe(5000);
  });
  it('does not poll while hidden, and asks again as soon as it is shown', async () => {
    const h = harness(false);
    h.poller.start();
    await h.flush();
    expect(h.run).not.toHaveBeenCalled();
    expect(h.timers).toHaveLength(0);
    h.state.visible = true;
    h.poller.visibilityChanged();
    await h.flush();
    expect(h.run).toHaveBeenCalledTimes(1);
  });
  it('schedules nothing after a round that ends while hidden', async () => {
    const h = harness();
    h.poller.start();
    h.state.visible = false;
    await h.flush();
    expect(h.timers).toHaveLength(0);
  });
  it('stops', async () => {
    const h = harness();
    h.poller.start();
    await h.flush();
    h.poller.stop();
    h.poller.kick();
    await h.flush();
    expect(h.run).toHaveBeenCalledTimes(1);
  });
});

describe('reducer', () => {
  const loaded = (): State => reducer(initialState(NOW), { type: 'refresh/ok', status: status(), cards: [CARDS.save_file, CARDS.message], jobs: [], at: NOW });

  it('starts with nothing known and keeps the last seen time', () => {
    expect(initialState(NOW, 123)).toMatchObject({ loaded: false, status: null, lastOkAt: 123 });
  });
  it('keeps the last known content when a refresh fails', () => {
    const s = reducer(loaded(), { type: 'refresh/fail', problem: 'unreachable', at: NOW + 5000 });
    expect(s.problem).toBe('unreachable');
    expect(s.status).not.toBeNull();
    expect(s.cards).toHaveLength(2);
    expect(s.lastOkAt).toBe(NOW);
  });
  it('clears the problem on the next success', () => {
    const failed = reducer(reducer(loaded(), { type: 'refresh/fail', problem: 'generic', at: NOW }), { type: 'device/use-this' });
    const s = reducer(failed, { type: 'refresh/ok', status: status(), cards: [], jobs: [], at: NOW + 1 });
    expect(s).toMatchObject({ problem: null, useThisDevice: false, lastOkAt: NOW + 1 });
  });
  it('hides a card at once when it is answered (optimistic)', () => {
    const s = reducer(loaded(), { type: 'card/answer', id: CARDS.save_file.id, decision: 'approve' });
    expect(waitingCards(s).map((c) => c.id)).toEqual([CARDS.message.id]);
    expect(s.cards).toHaveLength(2);
  });
  it('rolls the card back when the answer fails', () => {
    const answered = reducer(loaded(), { type: 'card/answer', id: CARDS.save_file.id, decision: 'approve' });
    const s = reducer(answered, { type: 'card/failed', id: CARDS.save_file.id, reason: 'other' });
    expect(waitingCards(s)).toHaveLength(2);
    expect(s.notice?.kind).toBe('card_failed');
  });
  it('drops the card when the home confirms, or says it was answered elsewhere or expired', () => {
    const answered = reducer(loaded(), { type: 'card/answer', id: CARDS.save_file.id, decision: 'approve' });
    const ok = reducer(answered, { type: 'card/confirmed', id: CARDS.save_file.id, decision: 'approve', always: true });
    expect(ok.cards).toHaveLength(1);
    expect(ok.notice?.kind).toBe('approved_always');
    expect(reducer(answered, { type: 'card/confirmed', id: CARDS.save_file.id, decision: 'decline', always: false }).notice?.kind).toBe('declined');
    expect(reducer(answered, { type: 'card/failed', id: CARDS.save_file.id, reason: 'answered' })).toMatchObject({ notice: { kind: 'card_answered' } });
    const expired = reducer(answered, { type: 'card/failed', id: CARDS.save_file.id, reason: 'expired' });
    expect(expired.cards).toHaveLength(1);
    expect(expired.notice?.kind).toBe('card_expired');
  });
  it('does not show a card past its time', () => {
    const s = reducer(loaded(), { type: 'tick', at: Date.parse(CARDS.save_file.expires_at) + 1 });
    expect(waitingCards(s)).toHaveLength(0);
  });
  it('shows pause at once, confirms it, and puts it back on failure', () => {
    const set = reducer(loaded(), { type: 'pause/set', paused: true });
    expect(isPaused(set)).toBe(true);
    expect(set.status?.paused).toBe(false);
    const ok = reducer(set, { type: 'pause/confirmed', paused: true });
    expect(ok.status?.paused).toBe(true);
    expect(ok.pausedOverride).toBeNull();
    const bad = reducer(set, { type: 'pause/failed' });
    expect(isPaused(bad)).toBe(false);
    expect(bad.notice?.kind).toBe('pause_failed');
  });
  it('numbers notices so the same one can be said twice', () => {
    const a = reducer(loaded(), { type: 'pause/failed' });
    const b = reducer(a, { type: 'pause/failed' });
    expect(b.notice?.seq).toBe((a.notice?.seq ?? 0) + 1);
    expect(reducer(b, { type: 'notice/dismiss' }).notice).toBeNull();
  });
  it('names the problem for each kind of failure', () => {
    expect(problemOf({ kind: 'unreachable', code: null })).toBe('unreachable');
    expect(problemOf({ kind: 'api', code: 'unauthorized' })).toBe('auth');
    expect(problemOf({ kind: 'api', code: 'forbidden' })).toBe('auth');
    expect(problemOf({ kind: 'api', code: 'internal' })).toBe('generic');
    expect(problemOf({ kind: 'protocol', code: null })).toBe('generic');
  });
});

describe('tip', () => {
  const st = status();
  it('shows the first suggestion, and nothing when nothing is connected', () => {
    expect(chooseTip(st, 1, '2026-11-13', null)?.id).toBe('cerebras');
    expect(chooseTip(st, 0, '2026-11-13', null)).toBeNull();
    expect(chooseTip(null, 1, '2026-11-13', null)).toBeNull();
    expect(chooseTip({ suggestions: [] }, 1, '2026-11-13', null)).toBeNull();
  });
  it('shows no tip again on the day one was dismissed', () => {
    expect(chooseTip(st, 1, '2026-11-13', { day: '2026-11-13', id: 'cerebras', dismissed: true })).toBeNull();
  });
  it('never shows a second, different tip on the same day', () => {
    const changed = { suggestions: [{ id: 'cloudflare', name: 'Cloudflare' }] };
    expect(chooseTip(changed, 1, '2026-11-13', { day: '2026-11-13', id: 'cerebras', dismissed: false })).toBeNull();
    expect(chooseTip(st, 1, '2026-11-13', { day: '2026-11-13', id: 'cloudflare', dismissed: false })?.id).toBe('cloudflare');
  });
  it('shows a tip again the next day', () => {
    expect(chooseTip(st, 1, '2026-11-14', { day: '2026-11-13', id: 'cerebras', dismissed: true })?.id).toBe('cerebras');
  });
  it('reads only its own stored shape', () => {
    expect(parseTipMemory('{"day":"2026-11-13","id":"x","dismissed":true}')).toEqual({ day: '2026-11-13', id: 'x', dismissed: true });
    expect(parseTipMemory('{"day":1}')).toBeNull();
    expect(parseTipMemory('not json')).toBeNull();
    expect(parseTipMemory(null)).toBeNull();
  });
});
